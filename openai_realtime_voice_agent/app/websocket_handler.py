"""WebSocket handler for managing WebSocket connections and pipelines."""
import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Callable, Awaitable, Dict

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.transports.websocket.server import WebsocketServerTransport, WebsocketServerParams
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame, EndFrame, ErrorFrame
from pipecat.audio.utils import create_stream_resampler
from pipecat.services.openai.realtime import events as openai_rt_events

from app.raw_audio_serializer import RawAudioSerializer
from app.session_manager import SessionManager
from app.audio_recording_service import AudioRecordingService
from app.phase_emitter import PhaseEmitter
from app.paced_audio_sender import PacedAudioSender
from app.transcript_logger import TranscriptLogger

logger = logging.getLogger(__name__)

# Qwen Realtime accepts the Voice PE's native 16 kHz PCM16 microphone stream
# and returns 24 kHz PCM16 to the speaker. Keeping input at 16 kHz avoids the
# OpenAI-only upsampling path that would otherwise alter speech timing.
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000


class SessionActivityTracker(FrameProcessor):
    """Processor that tracks session activity by monitoring audio frames."""
    
    def __init__(self, activity_callback, **kwargs):
        super().__init__(**kwargs)
        self.activity_callback = activity_callback
    
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, StartFrame):
            logger.debug("🎬 SessionActivityTracker: Received StartFrame")
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return
        elif isinstance(frame, EndFrame):
            logger.debug("🏁 SessionActivityTracker: Received EndFrame")
            await self.push_frame(frame, direction)
            return
        
        # Track activity on any audio frame
        if isinstance(frame, (InputAudioRawFrame, OutputAudioRawFrame)):
            if self.activity_callback:
                self.activity_callback()
            logger.debug(f"🎵 SessionActivityTracker: Processing {type(frame).__name__} ({len(frame.audio)} bytes)")
        
        # Pass frame through to next processor
        await self.push_frame(frame, direction)


class InputResampler(FrameProcessor):
    """Upsample incoming device mic audio to the OpenAI Realtime input rate.

    The Voice PE streams 16 kHz PCM16. pipecat 0.0.97's websocket input transport
    forwards those frames unchanged, and OpenAI Realtime reads pcm16 input at a
    fixed 24 kHz — so without this the audio is interpreted ~1.5x too fast,
    badly degrading transcription (e.g. first word dropped, words mangled). This
    sits right after transport.input() and resamples each InputAudioRawFrame to
    out_rate. Uses a streaming resampler so there are no per-chunk edge artifacts.
    """

    def __init__(self, out_rate: int = INPUT_SAMPLE_RATE, **kwargs):
        super().__init__(**kwargs)
        self._out_rate = out_rate
        self._resampler = create_stream_resampler()
        self._logged = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and frame.sample_rate != self._out_rate:
            if not frame.audio:
                return  # nothing to resample / forward; don't emit empty audio
            try:
                resampled = await self._resampler.resample(
                    frame.audio, frame.sample_rate, self._out_rate
                )
            except Exception as e:
                logger.warning(f"⚠️ input resample {frame.sample_rate}->{self._out_rate} failed: {e!r}")
                return  # drop rather than forward wrong-rate audio
            # The streaming resampler buffers internally and can return empty
            # bytes while priming or on a tiny chunk. OpenAI rejects an
            # input_audio_buffer.append with empty audio ("got empty bytes"), so
            # drop those frames — the samples stay buffered and come out next call.
            if not resampled:
                return
            if not self._logged:
                logger.info(
                    f"🎙️ Resampling device input {frame.sample_rate}Hz -> {self._out_rate}Hz for OpenAI"
                )
                self._logged = True
            frame = InputAudioRawFrame(
                audio=resampled,
                sample_rate=self._out_rate,
                num_channels=frame.num_channels,
            )
        await self.push_frame(frame, direction)


class ConnectionRecovery(FrameProcessor):
    """Auto-reconnect the OpenAI Realtime session when its WebSocket dies.

    pipecat 0.0.97's OpenAIRealtimeLLMService has NO reconnect logic: when the
    OpenAI WS drops (1011 keepalive ping timeout, 1001 going away on the 60-min
    cap, 1006, or any send/receive failure) it treats the send error as fatal and
    floods ErrorFrame — ~15/s, one per forwarded mic frame — forever. The single
    persistent session is then dead until the add-on restarts, so the device gets
    no answer to any further turn (observed live: a 1011 flood after which the
    next question got silence).

    This processor watches the ErrorFrames as they travel upstream to the task
    source, and on the first connection-death signature it:
      1. emits `idle` to the device so it unsticks (LED + mic reset), and
      2. calls service.reset_conversation() — the one PUBLIC method that does
         _disconnect() + _connect() + re-sends the session config (instructions,
         tools, turn detection) — to bring the session back IN PLACE. No pipeline
         rebuild: the running pipeline keeps the same service object, which is
         exactly the one reset_conversation reconnects.
    A guard + cooldown collapse the error flood into a single reconnect attempt,
    retrying at most every RECONNECT_COOLDOWN_S while the link stays down.
    """

    # Substrings that mark a dead/closed OpenAI websocket (vs an app-level error
    # like a tool failure, which we must NOT reconnect on). These appear on the
    # SEND-side flood ("Error sending client event: …"), so they're paired with
    # the "client event" check below to avoid reacting to a device disconnect.
    _DEATH_MARKERS = (
        "keepalive ping timeout",
        "going away",
        "no close frame",
        "ConnectionClosed",
        "connection is closed",
        "sent 1011",
        "sent 1001",
        "1006",
    )
    # Substrings that UNAMBIGUOUSLY mean OUR OpenAI session is gone and must be
    # reconnected, regardless of how the error surfaced. The 60-minute cap can
    # arrive as a proactive OpenAI *error event* (code='session_expired', "Your
    # session hit the maximum duration of 60 minutes.") with NO "client event"
    # send-flood and NO close-code marker — so the paired check above misses it
    # and the session stays dead until the add-on restarts. These markers force a
    # reconnect on their own. They can only come from OpenAI (not a device close),
    # so no "client event" guard is needed.
    _SESSION_DEAD_MARKERS = (
        "session_expired",
        "maximum duration",
        "user_idle_timeout",
        "session was closed",
    )
    RECONNECT_COOLDOWN_S = 5.0
    IDLE_UNSTICK_COOLDOWN_S = 2.0
    # Qwen closes an otherwise healthy Realtime response stream after about
    # 300 seconds of inactivity. Send a side-effect-free session.update at four
    # minutes, and only while the Voice PE has sent no microphone audio and no
    # assistant response is in flight. If that checked send fails, fall back to
    # the normal reconnect path.
    REFRESH_AGE_S = 4 * 60    # stay ahead of Qwen's 300-second idle ceiling
    REFRESH_QUIET_S = 15.0    # never refresh during mic/follow-up activity
    REFRESH_CHECK_S = 10.0    # leave enough margin before the provider close
    # Qwen's *session* lifetime is 120 minutes ("单次会话最长可持续 120 分钟").
    # A session kept alive past that by session.update turns into a zombie: the
    # WebSocket still answers session.update, but the server stops processing
    # upstream audio (VAD/ASR dead) — no transcript, no reply. Force a real
    # reconnect (close + reopen the WebSocket) comfortably before that cap.
    SESSION_MAX_AGE_S = 90 * 60

    def __init__(self, openai_service, emit_idle=None, phase_emitter=None, **kwargs):
        super().__init__(**kwargs)
        self._service = openai_service
        self._emit_idle = emit_idle  # async callable(value:str), e.g. broadcast_phase
        # Preferred idle route: PhaseEmitter.force_idle() keeps the emitter's
        # phase state consistent AND suppresses the racing `thinking` from VAD
        # stop events still in flight (observed: a raw broadcast idle was
        # overridden 400 ms later and the device sat in `thinking` with an
        # open mic for 44 s). emit_idle stays as fallback wiring.
        self._phase_emitter = phase_emitter
        self._reconnecting = False
        self._last_attempt = 0.0
        self._last_idle_unstick = 0.0
        # Diagnostics: when the current OpenAI session connected, so we can log its
        # age at a drop (the 60-min cap shows up as ~3600 s) and the reconnect
        # duration (the brief gap the user hears).
        self._connected_at = time.monotonic()
        # When we last sent a side-effect-free session.update keepalive. Paced
        # independently of _connected_at so the *true* session age still accrues
        # toward SESSION_MAX_AGE_S.
        self._last_keepalive = 0.0
        # Proactive-refresh state. This processor sits right behind
        # transport.input(), so every mic frame passes through it — the cheapest
        # possible "is anyone interacting?" signal (the device only streams the
        # mic during an active turn or the follow-up window).
        self._last_input_audio = time.monotonic()
        self._refresh_task = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        # Provider lifetime is now conversation-scoped: device wake opens Qwen
        # and the follow-up cut-off closes it. Do not start the legacy permanent
        # keepalive loop, which caused a predictable response_idle_timeout while
        # the Voice PE was asleep.
        if isinstance(frame, InputAudioRawFrame):
            # Only kept for the proactive-refresh "is anyone interacting?" check.
            # (Stale-audio clearing is now done at the cut-off source — the device
            # sends {"type":"flush"} when a follow-up window times out — not
            # reactively on mic-resume, which disturbed the VAD and caused garbage.)
            self._last_input_audio = time.monotonic()
        if isinstance(frame, ErrorFrame) and not self._reconnecting:
            msg = str(getattr(frame, "error", "") or "")
            # Two reconnect triggers:
            #  (a) the OpenAI send-side flood ("Error sending client event: …" +
            #      a close-code marker) — OUR WS died mid-send. We require the
            #      "client event" signature so a normal DEVICE-side disconnect
            #      (also 1011/ConnectionClosed, but the device went away) does NOT
            #      trigger an OpenAI reconnect.
            #  (b) an unambiguous OpenAI session-dead error event (session_expired
            #      / "maximum duration") — this is the 60-min cap surfacing as a
            #      proactive error event with NO send-flood, so (a) misses it.
            #      It can only come from OpenAI, so it needs no "client event" guard.
            send_flood = "client event" in msg and any(m in msg for m in self._DEATH_MARKERS)
            session_dead = any(m in msg for m in self._SESSION_DEAD_MARKERS)
            # (c) the OpenAI READ side died or ended (network drop / silent
            #     server close). pipecat produces no ErrorFrame for these at
            #     all — SafeRealtimeLLMService wraps the receive loop and
            #     reports them with this message. Without it the session sat
            #     deaf for hours until the next utterance hit the dead socket.
            reader_dead = "realtime receive loop" in msg
            if send_flood or session_dead or reader_dead:
                now = time.monotonic()
                if now - self._last_attempt >= self.RECONNECT_COOLDOWN_S:
                    self._reconnecting = True
                    self._last_attempt = now
                    asyncio.create_task(self._recover(msg))
            else:
                # Non-connection-death error that ENDS a turn without a reply:
                # most importantly an OpenAI rate-limit ("Rate limit reached …"),
                # but also any other transient response.create failure. No bot
                # speech was produced, so PhaseEmitter never fires
                # BotStopped→idle; the device is left stuck in `thinking`
                # (LED keeps blinking) with no device-side watchdog to recover.
                # Emit one `idle` to unstick it so the user can just try again.
                # Guarded by a short cooldown so a rare flood collapses to one.
                now = time.monotonic()
                if now - self._last_idle_unstick >= self.IDLE_UNSTICK_COOLDOWN_S:
                    self._last_idle_unstick = now
                    asyncio.create_task(self._unstick_idle(msg))
        await self.push_frame(frame, direction)

    async def _recover(self, reason: str):
        t0 = time.monotonic()
        age_s = t0 - self._connected_at
        if not getattr(self._service, "_conversation_active", True):
            logger.info("Qwen recovery skipped because no device conversation is active")
            self._reconnecting = False
            return
        try:
            logger.warning(
                f"🔌 OpenAI Realtime connection lost after {age_s:.0f}s "
                f"({reason[:90]}) — reconnecting…"
            )
            # Unstick the device first, regardless of how the reconnect goes.
            try:
                await self._go_idle(f"reconnect: {reason[:60]}")
            except Exception as e:
                logger.warning(f"⚠️ could not emit idle during recovery: {e!r}")
            reset = getattr(self._service, "reset_conversation", None)
            if reset is None:
                logger.error("❌ service has no reset_conversation(); cannot reconnect in place")
                return
            await reset()
            self._connected_at = time.monotonic()
            logger.info(
                f"✅ OpenAI Realtime session reconnected in {self._connected_at - t0:.1f}s "
                f"(gap the user may have heard)"
            )
        except Exception as e:
            logger.error(f"❌ OpenAI reconnect attempt failed: {e!r}")
        finally:
            self._reconnecting = False

    async def _proactive_refresh_loop(self):
        """Keep the provider session healthy during real idle.

        Two jobs, both gated on "quiet" (no mic audio for REFRESH_QUIET_S and no
        assistant response in flight):

          1. Every REFRESH_AGE_S send a side-effect-free session.update to stay
             ahead of Qwen's short idle ceiling, avoiding the slow graceful-close
             handshake for brief idle gaps.
          2. When the *true* session age reaches SESSION_MAX_AGE_S, force a real
             reconnect (close + reopen the WebSocket) before Qwen's 120-minute
             lifetime cap turns the session into a deaf zombie.
        """
        while True:
            try:
                await asyncio.sleep(self.REFRESH_CHECK_S)
                if self._reconnecting:
                    continue
                now = time.monotonic()
                age = now - self._connected_at
                quiet = now - self._last_input_audio
                busy = getattr(self._service, "_current_assistant_response", None) is not None
                if not (quiet >= self.REFRESH_QUIET_S and not busy
                        and now - self._last_attempt >= self.RECONNECT_COOLDOWN_S):
                    continue

                # Real reconnect before the 120-minute lifetime zombie.
                if age >= self.SESSION_MAX_AGE_S:
                    self._reconnecting = True
                    self._last_attempt = now
                    logger.info(
                        f"🔄 proactive session reconnect (session {age/60:.0f} min old) "
                        f"— forcing a fresh Qwen session before its lifetime cap"
                    )
                    try:
                        await self._recover("proactive reconnect before Qwen lifetime cap")
                    except Exception as e:
                        logger.warning(f"⚠️ proactive session reconnect failed: {e!r}")
                    finally:
                        self._reconnecting = False
                    continue

                # Idle keepalive, paced independently of the true session age.
                if now - self._last_keepalive >= self.REFRESH_AGE_S:
                    self._last_keepalive = now
                    self._reconnecting = True
                    self._last_attempt = now
                    logger.info(
                        f"🔄 proactive session keepalive (session {age/60:.0f} min old, "
                        f"quiet for {quiet:.0f}s) — staying ahead of provider idle cap"
                    )
                    try:
                        refresh = getattr(self._service, "refresh_idle_session", None)
                        if refresh is None:
                            await self._recover(
                                "proactive refresh before provider idle/session cap"
                            )
                        elif await refresh():
                            logger.info("✅ provider idle keepalive completed without reconnect")
                    except Exception as e:
                        logger.warning(
                            f"⚠️ provider idle keepalive failed; reconnecting: {e!r}"
                        )
                        await self._recover("provider idle keepalive failed")
                    finally:
                        self._reconnecting = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"⚠️ proactive refresh loop error: {e!r}")

    async def _go_idle(self, reason: str) -> None:
        """Put the device in idle for a dead turn — via PhaseEmitter when wired."""
        if self._phase_emitter is not None:
            await self._phase_emitter.force_idle(reason)
        elif self._emit_idle is not None:
            await self._emit_idle("idle")

    async def _unstick_idle(self, reason: str):
        """Emit `idle` to the device after a turn-ending error (e.g. rate limit).

        The session is still alive (no reconnect needed) — we just nudge the
        device out of its stuck `thinking` blink so the user can retry.
        """
        try:
            logger.warning(f"⚠️ turn ended on error, emitting idle to unstick device ({reason[:90]})")
            await self._go_idle(f"turn ended on error: {reason[:60]}")
        except Exception as e:
            logger.warning(f"⚠️ could not emit idle after turn-ending error: {e!r}")


class WebSocketHandler:
    """Handles WebSocket transport initialization, pipeline building, and event management."""
    
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        session_manager: Optional[SessionManager] = None,
        audio_recording_service: Optional[AudioRecordingService] = None,
        follow_up_ms: int = 0,
        follow_up_open_delay_ms: int = 700,
        wake_open_delay_ms: int = 700,
        wake_audio_guard_ms: int = 0,
        playback_prebuffer_ms: int = 0,
    ):
        """
        Initialize WebSocket handler.

        Args:
            host: Host address to bind to
            port: Port to listen on
            session_manager: Session manager instance
            audio_recording_service: Audio recording service instance
            follow_up_ms: How long (ms) the device should keep the mic open
                after a reply so the user can answer without a wake word. Sent to
                the device in the `hello` handshake. 0 = turn-based (no window).
            follow_up_open_delay_ms: How long (ms) the device waits after a reply
                finishes before opening that follow-up mic (bridges the speaker
                hardware tail). Sent in the `hello` handshake.
            wake_open_delay_ms: How long (ms) the device waits after the wake
                chime before opening the mic, so the chime's hardware tail can't
                leak into the fresh mic as a ghost turn. Sent in `hello`.
            wake_audio_guard_ms: Optional compatibility guard for old firmware
                that opened capture before the wake chime drained. Current
                Voice PE firmware sends wake only after that boundary, so the
                default is zero to preserve the user's first word.
        """
        self.host = host
        self.port = port
        self.session_manager = session_manager
        self.audio_recording_service = audio_recording_service
        self.follow_up_ms = max(0, int(follow_up_ms))
        self.follow_up_open_delay_ms = max(0, int(follow_up_open_delay_ms))
        self.wake_open_delay_ms = max(0, int(wake_open_delay_ms))
        self.wake_audio_guard_ms = max(0, int(wake_audio_guard_ms))
        self.playback_prebuffer_ms = max(0, int(playback_prebuffer_ms))

        self.transport: Optional[WebsocketServerTransport] = None
        self.pipeline: Optional[Pipeline] = None
        self.runner: Optional[PipelineRunner] = None
        self.current_task: Optional[PipelineTask] = None
        # The serializer instance the transport reads through. Kept so
        # build_pipeline can wire its device-interrupt callback to the OpenAI
        # service.
        self._serializer: Optional[RawAudioSerializer] = None
        # Connected device websockets, used to push va_client control/phase
        # messages as TEXT frames (the audio path uses the binary serializer).
        self._websockets: set = set()
    
    def create_transport(self) -> WebsocketServerTransport:
        """
        Create and initialize WebSocket transport.
        
        Returns:
            WebsocketServerTransport instance
        """
        logger.info("Initializing WebSocket transport...")
        
        # Use RawAudioSerializer for binary PCM audio. It tags incoming frames
        # with the device mic rate (16 kHz for Voice PE); the transport
        # resamples in/out to the 24 kHz pipeline rate below.
        serializer = RawAudioSerializer()
        serializer.set_wake_audio_guard_ms(self.wake_audio_guard_ms)
        self._serializer = serializer
        # Bind diagnostics before WebsocketServerParams receives the serializer.
        # Some transport versions copy/freeze params at construction time; late
        # binding in build_pipeline then updates the wrong serializer instance
        # and produces header-only WAVs despite real PCM flowing.
        if self.audio_recording_service:
            serializer.set_input_audio_handler(
                self.audio_recording_service.record_input_audio
            )
            serializer.set_output_audio_handler(
                self.audio_recording_service.record_output_audio
            )

        # Create WebsocketServerTransport with WebsocketServerParams
        # The transport will start its own server automatically
        self.transport = WebsocketServerTransport(
            host=self.host,
            port=self.port,
            params=WebsocketServerParams(
                serializer=serializer,
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=INPUT_SAMPLE_RATE,
                audio_out_sample_rate=OUTPUT_SAMPLE_RATE,
            )
        )
        
        logger.info(f"✅ WebSocket transport created - will listen on ws://{self.host}:{self.port}/")
        return self.transport
    
    def build_pipeline(
        self,
        transport: WebsocketServerTransport,
        openai_service: OpenAIRealtimeLLMService,
        client_id: str,
        activity_callback: Optional[Callable[[], None]] = None
    ) -> tuple[Pipeline, PipelineRunner, PipelineTask]:
        """
        Build pipeline for a WebSocket transport connection.
        
        Args:
            transport: The WebSocket transport instance
            openai_service: The OpenAI service instance
            client_id: Unique identifier for the client device
            activity_callback: Optional callback for session activity tracking
            
        Returns:
            Tuple of (Pipeline, PipelineRunner, PipelineTask)
        """
        logger.info(f"🔗 Building pipeline for client: {client_id}")
        
        if openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")
        
        logger.info(f"🔗 Building pipeline with WebSocket transport and OpenAI service: {type(openai_service).__name__}")

        # Record at the real WebSocket boundary.  Pipecat's Realtime context
        # aggregators can consume or replace raw frame subclasses, which made
        # the former pipeline taps create header-only WAVs even though speech
        # reached OpenAI.  These dynamic service methods always target the
        # current session's recorder: input is native Voice PE 16 kHz and
        # output is exactly the paced 24 kHz PCM serialized to the device.
        if self.audio_recording_service and self._serializer:
            self._serializer.set_input_audio_handler(
                self.audio_recording_service.record_input_audio
            )
            self._serializer.set_output_audio_handler(
                self.audio_recording_service.record_output_audio
            )
        
        # Create activity trackers
        input_activity_tracker = SessionActivityTracker(
            activity_callback=activity_callback or (lambda: None)
        )
        output_activity_tracker = SessionActivityTracker(
            activity_callback=activity_callback or (lambda: None)
        )
        
        # Create context aggregator with cached context if available
        context_aggregator = None
        context_initializer = None
        if self.session_manager:
            context_aggregator = self.session_manager.create_context_aggregator(client_id)
            context_initializer = self.session_manager.create_context_initializer(client_id, context_aggregator)
        
        # Build pipeline components. InputResampler runs FIRST (right after the
        # transport) so every later stage — VAD, context aggregator, OpenAI
        # service — sees correctly-rated 24 kHz audio instead of the device's
        # raw 16 kHz (which OpenAI would otherwise read 1.5x too fast).
        # Built early so ConnectionRecovery can route its unstick/reconnect
        # idle through PhaseEmitter.force_idle() (consistent phase state +
        # racing-`thinking` suppression); it is APPENDED near the end of the
        # pipeline below, before transport.output().
        phase_emitter = PhaseEmitter(send_phase=self.broadcast_phase)

        pipeline_components = [
            transport.input(),
            # Watch for OpenAI connection-death ErrorFrames (they travel upstream
            # to the task source, so place this upstream of the service) and
            # reconnect in place. Without it a 1011/1001 drop bricks the session.
            ConnectionRecovery(openai_service=openai_service, emit_idle=self.broadcast_phase,
                               phase_emitter=phase_emitter),
            InputResampler(out_rate=INPUT_SAMPLE_RATE),
            input_activity_tracker,
        ]
        
        # Continue with rest of pipeline, with transcript-logging taps. The
        # assistant reply text (TTSTextFrame) flows DOWNSTREAM out of the LLM
        # while the user's TranscriptionFrame is pushed UPSTREAM (so the user
        # aggregator can consume it) — opposite directions, so they need taps on
        # opposite sides of the service (see transcript_logger.py): "user" before
        # the LLM, "assistant" after it.
        if context_aggregator:
            pipeline_components.extend([
                context_aggregator.user(),
                TranscriptLogger(capture="user"),
                openai_service,
                TranscriptLogger(capture="assistant"),
                context_aggregator.assistant(),
            ])
        else:
            pipeline_components.extend([
                TranscriptLogger(capture="user"),
                openai_service,
                TranscriptLogger(capture="assistant"),
            ])

        # OpenAI emits PCM in bursts. Prime 1.4 s server-side, then forward one
        # 20 ms packet every 20 ms so Voice PE sees a stable realtime stream.
        # This sits before activity/phase processing so BotStopped cannot make
        # the device idle before all queued audio has actually been sent.
        paced_audio_sender = PacedAudioSender(prime_ms=1400, packet_ms=20)
        output_drained = getattr(openai_service, "on_output_drained", None)
        if output_drained is not None:
            paced_audio_sender.set_response_drained_handler(output_drained)
        set_provider_close_handler = getattr(
            openai_service, "set_provider_close_handler", None
        )
        if set_provider_close_handler is not None:
            async def _reset_output_on_provider_close(reason: str):
                await paced_audio_sender.reset(
                    f"provider close: {reason}", wait_for_start=True
                )

            set_provider_close_handler(_reset_output_on_provider_close)
        pipeline_components.append(paced_audio_sender)

        pipeline_components.append(output_activity_tracker)

        # Emit va_client phase messages (listening/thinking/replying/idle) to
        # the device, derived from Pipecat speaking frames as they pass
        # downstream. Placed before transport.output() so it sees both the
        # user (UserStarted/Stopped) and bot (BotStarted/Stopped) frames.
        # (Constructed above, before ConnectionRecovery.)
        pipeline_components.append(phase_emitter)

        pipeline_components.append(transport.output())
        
        # Add context initializer if we have cached messages
        if context_initializer:
            pipeline_components.append(context_initializer)
        
        pipeline = Pipeline(pipeline_components)
        logger.info("✅ Pipeline created for WebSocket connection")
        
        # Audio recording is handled by AudioFrameRecorder processors in the pipeline
        if self.audio_recording_service:
            logger.info("🎙️ Audio recording enabled - will record input and output audio")
        
        # Create pipeline runner and task
        # Disable idle timeout - server should always stay ready for connections
        runner = PipelineRunner()
        # Pipecat 0.0.97's default turn-tracking observer occasionally destroys
        # its own proxy task mid-response.  This service does not consume turn
        # tracing data, so leave it disabled rather than risking an audible gap
        # in a live speech-to-speech reply.
        task = PipelineTask(
            pipeline,
            idle_timeout_secs=None,
            cancel_on_idle_timeout=False,
            enable_turn_tracking=False,
        )
        
        # Application.run() owns the runner lifecycle. Starting it here as well
        # creates two StartFrames for one PipelineTask and lets the same stateful
        # processors run concurrently.
        logger.info("✅ Pipeline prepared; runner lifecycle owned by Application.run")
        logger.info("✅ Pipeline initialized successfully")

        # Wire the device "stop" interrupt. The serializer calls this when it
        # sees {"type":"interrupt"} from the device.
        #
        # The DEVICE stops playback AUTHORITATIVELY: on "stop" its firmware
        # flushes the PSRAM queue and drops all further incoming TTS
        # (suppress_incoming_audio_) until the next turn boundary. So the backend
        # does NOT need to clear its own output here — the user already hears
        # silence. The backend's only job is to stop OpenAI generating MORE
        # tokens: a plain response.cancel, and ONLY while a response is actually
        # active (avoids the noisy response_cancel_not_active in the common
        # already-burst-finished case).
        #
        # We deliberately do NOT queue an InterruptionTaskFrame anymore. It made
        # pipecat run _handle_interruption → _truncate_current_audio_response(),
        # which tells OpenAI to truncate the assistant audio at the *playback*
        # position. But OpenAI bursts the reply faster than real-time, so that
        # position overshoots the audio that actually exists and OpenAI rejects
        # the truncate with invalid_request_error ("Audio content of N ms is
        # already shorter than M ms"). That error left the realtime session in a
        # broken state where the user's VERY NEXT turn got NO response — the
        # recurring "say stop, then immediately ask again → silence" bug. Since
        # the device already silenced playback, dropping the truncate costs us
        # nothing and keeps the next turn alive. (The backend still drains its
        # already-buffered output to the device, which the device discards —
        # minor wasted bandwidth, tracked as roadmap #3; no extra tokens because
        # response.cancel stops further generation.)
        # FOLLOW-UP-WINDOW STOP (the "stop heard as a question" bug). During the
        # post-reply follow-up window the device mic is OPEN and streaming, so by
        # the time the device's local wake-word detects "stop" and sends us the
        # interrupt, the stop word's audio is ALREADY in OpenAI's input buffer.
        # Left alone, the server VAD commits it as a user turn and — with
        # create_response=true — the model literally ANSWERS the word "stop"
        # ("Ik hou me stil…"). The device's local detection must therefore be
        # authoritative on the cloud side too, in two layers:
        #   1) input_audio_buffer.clear discards the not-yet-committed stop-word
        #      audio (the device closed its own mic gate in the same instant),
        #      so in the common case no turn is created at all;
        #   2) if the server VAD committed BEFORE our clear landed (tight race),
        #      OpenAI creates a response moments later anyway — so any assistant
        #      conversation item that appears within INTERRUPT_KILL_WINDOW_S of
        #      a device interrupt is cancelled on arrival (handler below). A
        #      legitimate next turn cannot fall inside that window: after a stop
        #      the mic is closed, and a fresh wake-word turn needs the chime +
        #      speech + VAD end-of-turn (> 2 s) before a response is created.
        _interrupt_kill_until = {"t": 0.0}
        INTERRUPT_KILL_WINDOW_S = 1.5
        # A device "stop" must cancel the NEXT assistant response too, not only
        # the one currently playing. After a stop, the only responses OpenAI can
        # still produce before the user speaks again are unwanted:
        #   - the cancelled reply's already-generated tail;
        #   - a slow tool's answer (web search ~2-4 s) the user stopped mid-run,
        #     created on the tool result OUTSIDE the 1.5 s time-window;
        #   - most common: OpenAI's STT hearing the user's spoken "stop" as a
        #     turn and the model REPLYING to it ("Okay, I'll stop"), which lands
        #     ~1.8 s later — just outside 1.5 s (observed 2026-06-14 22:51: the
        #     device flashed red but a fresh "I'll be quiet" reply played, so the
        #     user had to say stop twice).
        # The time-window alone misses the >1.5 s cases. This flag, armed on
        # EVERY device interrupt, makes _kill_racing_response cancel that one
        # next response regardless of timing. It is consumed when used and
        # cleared at the next genuine turn boundary (real speech via
        # on_real_speech, and {"type":"wake"}) — and a legitimate next turn needs
        # the user to actually speak — so it can never cancel a real turn.
        _kill_next_response = {"v": False}

        async def _on_device_interrupt():
            # The device is the authoritative cancellation boundary. Pipecat's
            # normal interruption frames are intentionally bypassed on this
            # path, so reset the paced queue explicitly before cancelling the
            # cloud response. Late PCM is dropped until the next response start.
            await paced_audio_sender.reset("device interrupt")
            _interrupt_kill_until["t"] = time.monotonic() + INTERRUPT_KILL_WINDOW_S
            # Arm the next-response kill on EVERY stop (see the flag comment):
            # the 1.5 s time-window alone misses responses that land later —
            # OpenAI replying to the spoken "stop", or a slow tool's answer.
            _kill_next_response["v"] = True
            try:
                await openai_service.send_client_event(openai_rt_events.InputAudioBufferClearEvent())
                logger.info("🛑 device interrupt → input_audio_buffer.clear sent (drop in-flight user audio)")
            except Exception as e:
                logger.info(f"🛑 device interrupt → input_audio_buffer.clear no-op ({e!r})")
            try:
                if getattr(openai_service, "_current_assistant_response", None) is not None:
                    await openai_service.send_client_event(openai_rt_events.ResponseCancelEvent())
                    logger.info("🛑 device interrupt → response.cancel sent (response was still active)")
                else:
                    logger.info("🛑 device interrupt → no active response to cancel (device already silenced)")
            except Exception as e:
                logger.info(f"🛑 device interrupt → response.cancel no-op ({e!r})")
            # response.cancel does not reliably emit BotStoppedSpeakingFrame.
            # Reset PhaseEmitter through its authoritative idle path so its
            # strict half-duplex reply latch cannot poison every later turn.
            await phase_emitter.force_idle("device interrupt")
            schedule_close = getattr(openai_service, "schedule_conversation_close", None)
            if schedule_close is not None:
                schedule_close(1.0, "device interrupt completed")

        @openai_service.event_handler("on_conversation_item_created")
        async def _kill_racing_response(service, item_id, item):
            # Pipecat fires this for every conversation.item.added; only an
            # ASSISTANT item right after a device interrupt is the racing
            # response to the stop word the user just cancelled.
            if getattr(item, "role", None) != "assistant":
                return
            within_window = time.monotonic() < _interrupt_kill_until["t"]
            kill_armed = _kill_next_response["v"]
            if not within_window and not kill_armed:
                return
            # Consume the flag: this assistant item is the unwanted response the
            # user's stop pre-empted — a stop-acknowledgement ("Okay, I'll stop"),
            # a stopped tool's answer, or the cancelled reply's tail.
            _kill_next_response["v"] = False
            try:
                await openai_service.send_client_event(openai_rt_events.ResponseCancelEvent())
                logger.info(
                    "🛑 response raced in right after a device interrupt → "
                    "response.cancel (post-stop)"
                )
            except Exception as e:
                logger.info(f"🛑 post-interrupt racing-response cancel no-op ({e!r})")

        async def _on_device_session_start():
            # va_client sends {"type":"start"} once per WebSocket CONNECTION
            # (on connect) — NOT per wake. A reconnect mid-utterance (wifi
            # blip, backend restart with session reuse) can leave half an
            # utterance in OpenAI's input buffer; start every (re)connection
            # with a clean one. The per-WAKE/follow-up stale-buffer case is
            # covered by the device's {"type":"flush"} on follow-up timeout.
            # A device reconnect can follow an abrupt socket loss while a reply
            # was active.  The pipeline survives that reconnect, so explicitly
            # clear all per-reply phase/latch state before accepting new audio.
            await paced_audio_sender.reset("device reconnect")
            await phase_emitter.force_idle("device reconnect")
            try:
                await openai_service.send_client_event(openai_rt_events.InputAudioBufferClearEvent())
                logger.info("🎬 device (re)connected → input_audio_buffer.clear (clean start)")
            except Exception as e:
                logger.debug(f"🎬 connect-time input clear no-op ({e!r})")

        async def _on_device_mic_flush():
            # ``flush`` is only an input-buffer command. Voice PE sends it both
            # when reply playback gates the mic and when a follow-up timer ends,
            # so it cannot define the provider conversation lifetime.
            try:
                await openai_service.send_client_event(openai_rt_events.InputAudioBufferClearEvent())
                logger.info("🧽 device flush → input_audio_buffer.clear only")
            except Exception as e:
                logger.debug(f"🧽 mic-flush input clear no-op ({e!r})")

        async def _on_device_conversation_end():
            close_conversation = getattr(openai_service, "close_conversation", None)
            if close_conversation is not None:
                await close_conversation("explicit device conversation_end")

        async def _on_device_wake():
            # va_client sends {"type":"wake"} on every wake (start_session). Mark
            # the turn boundary for the dangling-VAD guard (A): until the user
            # actually speaks, a server-VAD end-of-turn is a stale pre-wake
            # segment closing late → suppress its thinking + cancel its garbage
            # response (handled in PhaseEmitter via the kill-window callbacks).
            # A wake is a hard turn boundary. Ordinarily a residual reply first
            # sends device interrupt, but clearing here as well covers a lost or
            # reordered interrupt without ever concatenating two responses.
            await paced_audio_sender.reset("device wake")
            phase_emitter.note_wake()
            # Make wake a real upstream generation boundary. The service drops
            # any late VAD/transcript events from the old generation before we
            # clear the provider's uncommitted PCM buffer.
            begin_wake_turn = getattr(openai_service, "begin_wake_turn", None)
            if begin_wake_turn is not None:
                begin_wake_turn(self.wake_audio_guard_ms)
            # Current Voice PE firmware sends wake only after the wake chime,
            # its hardware-tail delay, and pre-roll discard. The service still
            # buffers PCM that races session.updated, so the first spoken word
            # is retained without a second backend rejection window.
            open_conversation = getattr(openai_service, "open_conversation", None)
            if open_conversation is not None:
                try:
                    await open_conversation()
                except Exception as e:
                    logger.exception("❌ Qwen on-wake connection failed")
                    await phase_emitter.force_idle(f"Qwen connection failed: {e!r}")
                    return
            try:
                await openai_service.send_client_event(
                    openai_rt_events.InputAudioBufferClearEvent()
                )
                logger.info(
                    "👋 device wake → input_audio_buffer.clear sent; guard=%ums",
                    self.wake_audio_guard_ms,
                )
            except Exception as e:
                logger.warning("👋 wake input clear failed: %r", e)
            # New turn boundary: drop any pending post-tool kill so it can't
            # leak onto this fresh turn's response.
            _kill_next_response["v"] = False

        # Wire the dangling-VAD guard's kill-window into the PhaseEmitter. It
        # reuses the SAME _interrupt_kill_until + _kill_racing_response machinery
        # as the device stop: on a dangling stop, arm it so the auto-created
        # garbage response is cancelled; on a real UserStartedSpeaking, clear it
        # so a genuine new turn's response is never cancelled.
        def _clear_kill_window():
            # Real user speech = a genuine new turn — disarm BOTH the time
            # window and the post-tool flag so neither can cancel it.
            _interrupt_kill_until["t"] = 0.0
            _kill_next_response["v"] = False

        phase_emitter.set_kill_window_handlers(
            on_dangling=lambda: _interrupt_kill_until.__setitem__(
                "t", time.monotonic() + INTERRUPT_KILL_WINDOW_S),
            on_real_speech=_clear_kill_window,
        )

        if self._serializer is not None:
            self._serializer.set_interrupt_handler(_on_device_interrupt)
            self._serializer.set_session_start_handler(_on_device_session_start)
            self._serializer.set_mic_flush_handler(_on_device_mic_flush)
            self._serializer.set_wake_handler(_on_device_wake)
            self._serializer.set_conversation_end_handler(_on_device_conversation_end)

        return pipeline, runner, task
    
    def extract_client_id(self, websocket) -> str:
        """
        Extract client ID from websocket connection.
        
        Args:
            websocket: WebSocket connection object
            
        Returns:
            Client ID string
        """
        client_ip = None
        if hasattr(websocket, 'client') and websocket.client:
            client_ip = websocket.client.host
        elif hasattr(websocket, 'remote_address'):
            client_ip = str(websocket.remote_address[0]) if websocket.remote_address else None
        
        if not client_ip:
            client_ip = f"unknown_{uuid.uuid4().hex[:8]}"
            logger.warning("⚠️ Could not extract client IP, using generated ID")

        return client_ip

    async def _send_json(self, websocket, obj: dict) -> None:
        """Send a JSON object to one device as a TEXT websocket frame.

        IMPORTANT: use COMPACT separators (no space after ':' or ','). The Voice
        PE va_client does a literal substring match on `"value":"<phase>"`
        (va_client.cpp handle_text_), so the default json.dumps output
        `"value": "listening"` (with a space) would NOT match and the device
        would silently ignore every phase. Compact output `"value":"listening"`
        matches. This is what made listening/thinking/replying never reach the
        device (LED stuck idle, no-speech watchdog never cancelled).
        """
        try:
            await websocket.send(json.dumps(obj, separators=(",", ":")))
        except Exception as e:
            logger.warning(f"⚠️ Could not send {obj.get('type')} to device: {e!r}")

    async def broadcast_json(self, obj: dict) -> None:
        """Send a JSON object to every connected device as a TEXT frame."""
        for ws in list(self._websockets):
            await self._send_json(ws, obj)

    async def broadcast_phase(self, value: str) -> None:
        """Send a va_client phase message to every connected device."""
        # TEMP instrumentation: log the broadcast + how many device sockets we
        # think are connected (was debug).
        logger.info(f"➡️ broadcast phase '{value}' to {len(self._websockets)} device(s)")
        await self.broadcast_json({"type": "phase", "value": value})
    
    def setup_event_handlers(
        self,
        transport: WebsocketServerTransport,
        on_client_connected_callback: Callable[[str], Awaitable[None]],
        on_client_disconnected_callback: Optional[Callable[[str], None]] = None,
        openai_service_getter: Optional[Callable[[str], Optional[OpenAIRealtimeLLMService]]] = None
    ):
        """
        Setup WebSocket event handlers.
        
        Args:
            transport: The WebSocket transport instance
            on_client_connected_callback: Async callback function(client_id) called when client connects
            on_client_disconnected_callback: Optional callback function(client_id) called when client disconnects
            openai_service_getter: Optional function(client_id) -> OpenAIRealtimeLLMService to get service for interrupt
        """
        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport: WebsocketServerTransport, websocket):
            """Handle new WebSocket client connection."""
            client_id = self.extract_client_id(websocket)
            logger.info(f"🔗 New WebSocket connection from IP: {client_id}")
            # Track the raw connection so we can push phase/control TEXT frames.
            self._websockets.add(websocket)
            # Handshake ack expected by the va_client protocol (server -> device
            # "hello"). The Voice PE firmware tolerates its absence, but sending
            # it keeps both sides in lockstep with the documented protocol.
            # follow_up_ms tells the device how long to keep the mic open after a
            # reply (post-reply follow-up window); 0/absent = turn-based. Sent on
            # every connect so an add-on config change takes effect on reconnect.
            await self._send_json(
                websocket,
                {
                    "type": "hello",
                    "audio_out": "pcm",
                    "follow_up_ms": self.follow_up_ms,
                    "follow_up_open_delay_ms": self.follow_up_open_delay_ms,
                    "wake_open_delay_ms": self.wake_open_delay_ms,
                    "playback_prebuffer_ms": self.playback_prebuffer_ms,
                },
            )
            await on_client_connected_callback(client_id)

        @transport.event_handler("on_client_disconnected")
        async def on_client_disconnected(transport: WebsocketServerTransport, websocket, *args, **kwargs):
            """Handle client disconnection."""
            self._websockets.discard(websocket)
            client_id = self.extract_client_id(websocket)
            if client_id:
                logger.info(f"🔌 Client {client_id} disconnected")
                if on_client_disconnected_callback:
                    on_client_disconnected_callback(client_id)
        
        # Handle text messages from client (e.g., interrupt messages)
        @transport.event_handler("on_client_message")
        async def on_client_message(transport: WebsocketServerTransport, websocket, message):
            """Handle text messages from WebSocket client."""
            try:
                client_id = self.extract_client_id(websocket)
                
                # Try to parse as JSON
                if isinstance(message, bytes):
                    message = message.decode('utf-8')
                
                try:
                    data = json.loads(message)
                    message_type = data.get("type")
                    
                    if message_type == "interrupt":
                        logger.info(f"🛑 Interrupt received from client {client_id}")
                        
                        # Get OpenAI service for this client
                        openai_service = None
                        if openai_service_getter:
                            openai_service = openai_service_getter(client_id)
                        
                        if openai_service:
                            # Send interrupt event to OpenAI Realtime API
                            # The interrupt event tells OpenAI to stop speaking and listen for user input
                            try:
                                # Try to send interrupt event directly to the service
                                # OpenAI Realtime API expects: {"type": "response.interrupt"}
                                if hasattr(openai_service, 'send_interrupt'):
                                    await openai_service.send_interrupt()
                                    logger.info(f"✅ Interrupt sent to OpenAI service for client {client_id}")
                                elif hasattr(openai_service, 'push_event'):
                                    # Send interrupt event via push_event
                                    await openai_service.push_event({"type": "response.interrupt"})
                                    logger.info(f"✅ Interrupt event sent to OpenAI service for client {client_id}")
                                elif hasattr(openai_service, '_send_event'):
                                    # Try private method if available
                                    await openai_service._send_event({"type": "response.interrupt"})
                                    logger.info(f"✅ Interrupt sent via _send_event to OpenAI service for client {client_id}")
                                else:
                                    # Fallback: log warning
                                    logger.warning(f"⚠️ Could not find method to send interrupt to OpenAI service. Available methods: {[m for m in dir(openai_service) if not m.startswith('__')]}")
                            except Exception as e:
                                logger.error(f"❌ Error sending interrupt to OpenAI service: {e}", exc_info=True)
                        else:
                            logger.warning(f"⚠️ No OpenAI service found for client {client_id}, cannot send interrupt")
                    elif message_type == "start":
                        # va_client sends {"type":"start"} on connect. The
                        # pipeline already streams continuously with server VAD,
                        # so there's nothing to start here — just acknowledge.
                        logger.debug(f"▶️ start from client {client_id}")
                    elif message_type == "ping":
                        # Keepalive. Reply with pong on the same connection.
                        await self._send_json(websocket, {"type": "pong"})
                    else:
                        logger.debug(f"📨 Received message from client {client_id}: {message_type}")
                        
                except json.JSONDecodeError:
                    logger.debug(f"📨 Received non-JSON message from client {client_id}: {message[:100]}")
                    
            except Exception as e:
                logger.error(f"❌ Error handling client message: {e}", exc_info=True)
    
    async def cleanup(self):
        """Cleanup WebSocket handler resources."""
        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as e:
                logger.warning(f"⚠️ Error cancelling runner: {e}")
        
        if self.transport:
            try:
                if hasattr(self.transport, 'stop'):
                    await self.transport.stop()
            except Exception as e:
                logger.warning(f"⚠️ Error stopping transport: {e}")
