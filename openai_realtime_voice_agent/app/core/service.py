"""Provider-neutral realtime controller and Pipecat boundary.

All cloud I/O uses RealtimeProvider. A connection, input turn, tool job and
response retain their original owners across awaits. No vendor wire messages
or vendor SDK state belong here.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from pipecat.frames.frames import (
    AggregationType, CancelFrame, EndFrame, InputAudioRawFrame, InterruptionFrame,
    LLMFullResponseStartFrame, LLMFullResponseEndFrame, LLMTextFrame, LLMContextFrame,
    TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame, TTSTextFrame,
    TranscriptionFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.utils.time import time_now_iso8601

from app.core.arbitration import TranscriptGate, light_effects
from app.core.clarification import PendingClarification
from app.core.events import AudioChunk, CanonicalEvent, EventKind, EventScope, ErrorKind, INPUT_FORMAT
from app.core.evidence import ControlEvidence
from app.core.frames import ResponseDoneFrame, ResponseStartedFrame, SpokenTextFinalFrame
from app.core.lifecycle import ConversationLifecycle
from app.core.playback import ResponsePlayback
from app.phase_emitter import TURN_LIVENESS
from app.providers.base import ProviderSession
from app.tools.execution import ExecutionLedger
from app.tools.legacy import callback_backend, outcome_payload
from app.tools.registry import ToolOutcome, ToolRegistry
from app.tools.schema import CanonicalToolRequest, CanonicalToolResult
from app.tool_names import canonical_tool_name, canonical_wire_map

logger = logging.getLogger(__name__)


@dataclass
class InputTurn:
    scope: EventScope
    transcript: TranscriptGate = field(default_factory=TranscriptGate)
    evidence: ControlEvidence = field(default_factory=ControlEvidence)
    ledger: ExecutionLedger | None = None
    input_item: str | None = None
    input_started_at: float | None = None
    jobs: set = field(default_factory=set)
    call_ids: set[str] = field(default_factory=set)
    needs_response: bool = False
    reply_pending: bool = False
    cancelled: bool = False
    route_started: bool = False
    tool_proposals: int = 0
    exhausted: bool = False
    regeneration_waiting: bool = False


@dataclass
class Response:
    scope: EventScope
    turn: InputTurn
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    speaking: bool = False
    cancelled: bool = False
    text: str = ""
    watchdog: asyncio.Task | None = None
    playback: ResponsePlayback = field(init=False)

    def __post_init__(self):
        self.playback = ResponsePlayback(self.scope)

    @property
    def audio_received(self):
        return self.playback.total_bytes > 0

    @property
    def drained(self):
        return self.playback.drained


class RealtimeCoreService(FrameProcessor):
    def __init__(self, *, provider_factory, session: ProviderSession,
                 tool_timeout_s=15.0, follow_up_seconds=8.0,
                 follow_up_open_delay_s=0.7, router_enabled=True,
                 enforce_cancel_boundary=False, response_timeout_s=45.0, **kwargs):
        super().__init__(**kwargs)
        self.provider_factory = provider_factory
        self.session = session
        self.tool_registry = ToolRegistry(timeout_s=tool_timeout_s)
        self.tool_definitions = {t.name: t for t in session.tools}
        self.control_dispatch = None
        self.control_router = None
        self.entity_catalog = None
        self.router_enabled = router_enabled
        self.enforce_cancel_boundary = enforce_cancel_boundary
        self.response_timeout_s = response_timeout_s
        self.lifecycle = ConversationLifecycle(follow_up_seconds=follow_up_seconds)
        self.follow_up_open_delay_s = follow_up_open_delay_s
        self.provider = None
        self.context = None
        self.conversation_active = False
        self.tools_ready = False
        self._ready = asyncio.Event()
        self._reader = None
        self._close_task = None
        self._retry_tasks = set()
        self._tool_jobs = set()
        self._watchdogs = set()
        self._pending_response_timer = None
        self._lifecycle_lock = asyncio.Lock()
        self._provider_close_handler = None
        self._functions = {}  # Pipecat MCP callback registration, not provider state.
        self._responses = {}
        self._active_response = None
        self._buffered_input = bytearray()
        self._awaiting_mic = False
        self._wake_guard_until = 0.0
        self._speech_seen = False
        self._opening_input = False
        self._cancel_on_open = False
        self._register_event_handler("on_response_started")
        self.begin_wake_turn()

    @property
    def control_evidence(self):
        return self.turn.evidence

    @property
    def response_active(self):
        return self._active_response is not None or self.turn.reply_pending or self.playback_pending or self.turn.regeneration_waiting

    @property
    def playback_pending(self):
        return any(r.turn is self.turn and r.audio_received and not r.drained and not r.cancelled
                   for r in self._responses.values())

    @property
    def is_ready(self):
        return self._ready.is_set() and self.provider is not None

    @property
    def tool_calls_pending(self):
        return bool(self.turn.jobs)

    def evidence_for_response(self, scope):
        response = self._responses.get(scope.response_id) if scope else None
        return response.turn.evidence if response and response.scope == scope else None

    def _install_turn(self, context):
        self.turn = InputTurn(context.scope)
        self._speech_seen = False
        self._awaiting_mic = False
        if self.provider is not None:
            self.provider.begin_turn(context.scope)

    def begin_wake_turn(self, guard_ms=0):
        self._clarification = PendingClarification()
        self._cancel_close()
        self._opening_input = True
        self._cancel_on_open = self._active_response is not None
        for response in self._responses.values():
            response.finished.set()
        self._responses.clear()
        self._active_response = None
        for task in tuple(self._retry_tasks):
            task.cancel()
        for task in tuple(self._watchdogs):
            task.cancel()
        if hasattr(self, "turn"):
            self.turn.cancelled = True
        self._install_turn(self.lifecycle.wake())
        self._wake_guard_until = time.monotonic() + max(0, guard_ms) / 1000
        self._buffered_input.clear()

    def _current(self, turn, provider=None):
        return (turn is self.turn and (provider is None or provider is self.provider)
                and (not turn.cancelled or not self.enforce_cancel_boundary))

    def set_provider_close_handler(self, handler):
        self._provider_close_handler = handler

    def _instructions(self):
        names = ", ".join(self.tool_definitions)
        catalog = self.entity_catalog
        entities = "; ".join(f"{e.name} ({e.domain}, {e.area})" for e in catalog.entities) if catalog else ""
        return (self.session.instructions + "\n只调用实际注册工具：" + names
                + "。控制前必须执行工具，按执行证据简短确认。名称/别名优先，歧义先澄清。"
                + "不要虚构当前时间、天气或设备状态；使用相应读取工具。"
                + "\nAssist 暴露实体：" + entities)

    async def open_conversation(self, timeout_s=6.0):
        async with self._lifecycle_lock:
            self._cancel_close()
            self.conversation_active = True
            if self.is_ready:
                if self._cancel_on_open:
                    await self.provider.cancel_response()
                    self._cancel_on_open = False
                await self.provider.update_instructions(self._instructions())
                await self.provider.clear_input_audio()
                await self._flush_opening_audio()
                return True
            if self.provider is not None:
                await self._close("replace unavailable provider")
                self.begin_wake_turn()
                self.conversation_active = True
            provider = self.provider_factory()
            self.provider = provider
            try:
                await provider.connect(self.turn.scope)
                self._reader = asyncio.create_task(self._receive(provider), name="realtime-core-reader")
                await provider.configure_session(replace(self.session, instructions=self._instructions()))
                await asyncio.wait_for(self._ready.wait(), timeout_s)
                if provider is not self.provider:
                    raise ConnectionError("Provider changed during initialization")
                await provider.clear_input_audio()
                await self._flush_opening_audio()
                return True
            except BaseException:
                await self._close("provider initialization failed")
                raise

    async def _flush_opening_audio(self):
        audio = bytes(self._buffered_input)
        self._buffered_input.clear()
        self._opening_input = False
        if audio:
            await self.provider.send_audio(AudioChunk(audio, INPUT_FORMAT))

    async def _close(self, reason):
        self._cancel_close()
        self.conversation_active = False
        provider, self.provider = self.provider, None
        reader, self._reader = self._reader, None
        self._ready.clear()
        self.tools_ready = False
        self.turn.cancelled = True
        self._buffered_input.clear()
        self._opening_input = False
        for response in self._responses.values():
            response.finished.set()
        for task in tuple(self._retry_tasks):
            task.cancel()
        for task in tuple(self._watchdogs):
            if task is not asyncio.current_task():
                task.cancel()
        if reader and reader is not asyncio.current_task():
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        try:
            if provider is not None:
                await provider.disconnect()
        finally:
            self._active_response = None
            self._responses.clear()
            self.lifecycle.recover()
            if self._provider_close_handler:
                await self._provider_close_handler(reason)
        # Tool jobs remain alive to record already-dispatched HA outcomes.
        # Their captured turn/provider prevent delivery into the next session.

    async def close_conversation(self, reason="conversation complete"):
        self._clarification.clear()
        async with self._lifecycle_lock:
            await self._close(reason)

    async def reset_conversation(self):
        was_active = self.conversation_active
        await self.close_conversation("connection recovery")
        self.begin_wake_turn()
        if was_active:
            await self.open_conversation()

    def _cancel_close(self):
        task, self._close_task = self._close_task, None
        if task and task is not asyncio.current_task():
            task.cancel()

    def schedule_conversation_close(self, delay_s, reason):
        self._cancel_close()
        provider, turn = self.provider, self.turn
        async def later():
            await asyncio.sleep(max(0, delay_s))
            if provider is self.provider and turn is self.turn:
                await self.close_conversation(reason)
        self._close_task = asyncio.create_task(later(), name="realtime-core-close")

    async def clear_input_audio(self):
        self._buffered_input.clear()
        if self.provider and self.is_ready:
            await self.provider.clear_input_audio()

    async def cancel_response(self):
        self._clarification.clear()
        response = self._active_response
        self.turn.cancelled = True
        self.lifecycle.cancel()
        for old in self._responses.values():
            if old.turn is self.turn:
                old.cancelled = True
        for task in tuple(self._retry_tasks):
            task.cancel()
        if response:
            response.cancelled = True
            if self.provider:
                await self.provider.cancel_response()
            if response.speaking:
                await self.push_frame(TTSStoppedFrame())
                response.speaking = False
            await self.push_frame(LLMFullResponseEndFrame())

    async def send_interrupt(self):
        await self.clear_input_audio()
        await self.cancel_response()

    async def refresh_idle_session(self):
        if not self.is_ready or self.response_active or self.tool_calls_pending:
            return False
        await self.provider.update_instructions(self._instructions())
        return True

    async def on_output_drained(self, scope=None):
        if scope is None:
            return  # Synthetic ends and old unscoped callbacks cannot open mic.
        response = self._responses.get(scope.response_id)
        if not response or response.scope != scope or not self._current(response.turn):
            return
        if not response.playback.mark_drained(scope):
            return
        if response.turn.cancelled:
            self.schedule_conversation_close(0, "cancelled turn drained")
            return
        if not self.lifecycle.output_drained(scope):
            return
        if self.turn.needs_response:
            await self._maybe_respond(self.turn)
        if self.response_active or self.tool_calls_pending or self.turn.needs_response:
            return
        if self.control_dispatch:
            self._clarification.offer(response.turn.evidence.text, response.text,
                self.control_dispatch.arbiter, self.lifecycle.follow_up_seconds + self.follow_up_open_delay_s)
        self._awaiting_mic = True
        self.schedule_conversation_close(self.lifecycle.follow_up_seconds + self.follow_up_open_delay_s + 2,
                                         "playback drained without further input")

    async def _input_audio(self, frame):
        if not self.conversation_active or time.monotonic() < self._wake_guard_until:
            return
        if self._awaiting_mic:
            if self.lifecycle.microphone_opened():
                context = self.lifecycle.follow_up()
                if context:
                    self._install_turn(context)
                    await self.provider.update_instructions(self._instructions())
                    self.schedule_conversation_close(self.lifecycle.follow_up_seconds, "follow-up expired")
        chunk = AudioChunk(frame.audio, INPUT_FORMAT)
        if frame.sample_rate != INPUT_FORMAT.sample_rate or frame.num_channels != 1:
            raise ValueError("Core input requires 16 kHz mono PCM16")
        if not self.is_ready or self._opening_input:
            self._buffered_input.extend(chunk.data)
            del self._buffered_input[:-INPUT_FORMAT.bytes_per_second * 5]
            return
        await self.provider.send_audio(chunk)

    def register_function(self, function_name, handler, start_callback=None, *, cancel_on_interruption=False):
        definition = self.tool_definitions[function_name]
        self.tool_registry.register(definition, callback_backend(handler))
        async def bound(params):
            outcome = await self.execute_tool(self.turn, params.tool_call_id, params.function_name, params.arguments)
            await params.result_callback(outcome_payload(outcome))
        self._functions[function_name] = SimpleNamespace(handler=bound)

    def has_function(self, name):
        return name in self._functions

    async def execute_tool(self, turn, call_id, name, arguments, source="model", *, queued=False):
        if turn.evidence.regeneration_active:
            return ToolOutcome(CanonicalToolResult(call_id, "failed", detail="confirmation_replay_no_tools"))
        if not self._current(turn):
            return ToolOutcome(CanonicalToolResult(call_id, "failed", detail="old_input_turn"))
        request = CanonicalToolRequest(turn.scope.conversation_id, turn.scope.turn_id, call_id, name, arguments, source=source)
        if turn.ledger is None:
            turn.ledger = ExecutionLedger(self.tool_registry, request.conversation_id, request.turn_id)
        definition = self.tool_definitions.get(name)
        track = definition is not None and not definition.read_only
        if track and not queued:
            turn.evidence.in_flight += 1
        TURN_LIVENESS.tool_started()
        try:
            if self.control_dispatch:
                outcome = await self.control_dispatch.execute(request, turn.ledger, turn.transcript,
                                                              lambda: self._current(turn))
            else:
                outcome = await turn.ledger.execute(request)
            if track:
                turn.evidence.record(outcome)
            logger.info("Tool completed: turn=%s call=%s tool=%s status=%s execution=%s",
                        request.turn_id, call_id, name, outcome.result.status, outcome.result.execution_id)
            return outcome
        finally:
            TURN_LIVENESS.tool_finished()
            if track and not queued:
                turn.evidence.in_flight -= 1

    def _launch_tool(self, turn, call_id, name, arguments, source="model"):
        if call_id in turn.call_ids:
            return
        turn.call_ids.add(call_id)
        provider = self.provider
        turn.tool_proposals += 1
        proposal = turn.tool_proposals
        definition = self.tool_definitions.get(name)
        track = definition is None or not definition.read_only
        if track:
            turn.evidence.in_flight += 1
        async def run():
            try:
                if proposal > 9:
                    if self._current(turn, provider):
                        turn.needs_response = False
                        self.schedule_conversation_close(0, "tool loop exhausted")
                    return
                if not self.tools_ready:
                    outcome = ToolOutcome(CanonicalToolResult(call_id, "failed", detail="tool_registration_not_verified"))
                elif proposal > 8:
                    turn.exhausted = True
                    outcome = ToolOutcome(CanonicalToolResult(call_id, "failed", detail="turn_tool_limit_reached"))
                else:
                    outcome = await self.execute_tool(turn, call_id, name, arguments, source, queued=True)
                if track:
                    turn.evidence.record(outcome)
                if not self._current(turn, provider):
                    return
                result = replace(outcome.result, data=outcome.data)
                if source == "model":
                    await provider.send_tool_result(call_id, result)
                else:
                    await provider.provide_execution_context(result)
                if turn.exhausted:
                    turn.evidence.regeneration_active = True
                    await provider.update_instructions(self._instructions() + "\n已达到工具调用上限。只说明未完成部分，不再调用工具。")
                # A routed result enriches the automatic response already owed
                # by the provider; it must not enqueue another spoken response.
                if source == "model" or not provider.automatic_responses:
                    turn.needs_response = True
            except Exception:
                logger.exception("Tool result delivery failed")
                if self._current(turn, provider):
                    await self.push_error(error_msg="realtime receive loop: tool result delivery failed")
            finally:
                if track:
                    turn.evidence.in_flight -= 1
                turn.jobs.discard(asyncio.current_task())
                if self._current(turn, provider):
                    if not turn.jobs and turn.evidence.response_waiting_for_tools:
                        turn.evidence.response_waiting_for_tools = False
                        if turn.evidence.reviewer.request_regeneration():
                            await self.regenerate_control_confirmation(turn.evidence)
                    await self._maybe_respond(turn)
        task = asyncio.create_task(run(), name="realtime-core-tool")
        turn.jobs.add(task)
        self._tool_jobs.add(task)
        task.add_done_callback(self._tool_jobs.discard)

    async def _maybe_respond(self, turn):
        if not self._current(turn) or not self.is_ready or turn.jobs or self.response_active or not turn.needs_response:
            return
        turn.needs_response = False
        turn.reply_pending = True
        self._cancel_close()
        try:
            await self.provider.create_response(turn.scope)
            if turn.reply_pending:
                self._pending_response_timer = self._arm_response_timeout(turn)
        except Exception:
            turn.reply_pending = False
            await self.push_error(error_msg="realtime receive loop: response request failed")

    def _arm_response_timeout(self, turn, response=None):
        async def watch():
            await asyncio.sleep(self.response_timeout_s)
            pending = not response.finished.is_set() if response else turn.reply_pending
            if self._current(turn) and pending:
                await self.push_frame(LLMFullResponseEndFrame())
                await self.push_error(error_msg="Provider response timed out")
                await self.close_conversation("response timeout")
        task = asyncio.create_task(watch(), name="realtime-core-response-timeout")
        self._watchdogs.add(task)
        task.add_done_callback(self._watchdogs.discard)
        return task

    def _route(self, turn):
        if turn.route_started or not self.router_enabled or not self.control_router or not self.tools_ready:
            return False
        turn.route_started = True
        intent = self.control_router.resolve(turn.evidence.text)
        if intent is None:
            return False
        if canonical_tool_name(intent.tool) == "HassLightSet" and not light_effects(turn.evidence.text).issubset(intent.arguments):
            return False
        name = canonical_wire_map(self.tool_definitions).get(canonical_tool_name(intent.tool))
        if not name or name not in self._functions:
            return False
        self._launch_tool(turn, "router-" + uuid.uuid4().hex, name, intent.arguments, "router")
        return True

    async def regenerate_control_confirmation(self, evidence):
        turn, provider = self.turn, self.provider
        if evidence is not turn.evidence or evidence.regeneration_active or turn.cancelled:
            return
        evidence.regeneration_active = True
        turn.regeneration_waiting = True
        response = self._active_response
        async def retry():
            try:
                if response:
                    await asyncio.wait_for(response.finished.wait(), 3)
                if not self._current(turn, provider) or turn.cancelled:
                    return
                await provider.update_instructions(self._instructions() + "\n" + evidence.retry_instruction())
                turn.regeneration_waiting = False
                turn.needs_response = True
                await self._maybe_respond(turn)
            except Exception:
                logger.exception("Confirmation regeneration failed")
                if self._current(turn, provider):
                    self.schedule_conversation_close(0, "confirmation regeneration failed")
            finally:
                turn.regeneration_waiting = False
                self._retry_tasks.discard(asyncio.current_task())
        task = asyncio.create_task(retry(), name="realtime-core-confirmation")
        self._retry_tasks.add(task)

    async def handle_event(self, event: CanonicalEvent):
        if not self.conversation_active:
            return False
        kind, scope = event.kind, event.scope
        current = self.turn.scope
        if (scope.generation, scope.conversation_id) != (current.generation, current.conversation_id):
            return False
        if kind == EventKind.CONNECTED:
            return True
        if kind == EventKind.READY:
            self.tools_ready = event.tools_valid and set(event.tool_names) == set(self.tool_definitions) and all(
                name in self._functions for name in self.tool_definitions)
            self._ready.set()
            if not self.tools_ready and self.provider:
                await self.provider.update_instructions(self._instructions() + "\n控制工具未就绪；不得执行或宣称成功。")
            return True
        if kind in {EventKind.ERROR, EventKind.CLOSED}:
            if kind == EventKind.CLOSED or (event.error and event.error.kind in {ErrorKind.CONNECTION, ErrorKind.AUTHENTICATION}):
                self._ready.clear()
            await self.push_frame(LLMFullResponseEndFrame())
            await self.push_error(error_msg="realtime receive loop: provider unavailable" if not self.is_ready else "Provider response failed")
            if self.is_ready:
                self.schedule_conversation_close(0, "provider response failed")
            return True
        if scope.turn_id != current.turn_id:
            return False
        turn = self.turn
        if turn.cancelled and self.enforce_cancel_boundary:
            return False
        if kind == EventKind.SPEECH_STARTED:
            if time.monotonic() < self._wake_guard_until or self.response_active:
                return False
            if turn.cancelled and not self.enforce_cancel_boundary:
                # Compatibility mode lets late VAD clear the device-side kill
                # window while its turn scope remains cancelled.
                self._cancel_close()
                await self.push_frame(UserStartedSpeakingFrame())
                return True
            if turn.input_item is not None and turn.input_item != scope.item_id:
                return False
            turn.input_item = scope.item_id
            turn.input_started_at = time.monotonic()
            self._speech_seen = True
            self._cancel_close()
            # Strict compositions use the cancelled-turn check above.
            self.lifecycle.observe(event)
            await self.push_frame(UserStartedSpeakingFrame())
        elif kind in {EventKind.SPEECH_STOPPED, EventKind.TRANSCRIPT_FINAL, EventKind.TRANSCRIPT_FAILED}:
            if not self._speech_seen or not scope.item_id or scope.item_id != turn.input_item:
                return False
            self.lifecycle.observe(event)
            if kind == EventKind.SPEECH_STOPPED:
                await self.push_frame(UserStoppedSpeakingFrame())
                if self.provider and not self.provider.automatic_responses:
                    async def after_transcript():
                        await turn.transcript.wait()
                        if self._current(turn) and not self._route(turn):
                            turn.needs_response = True
                            await self._maybe_respond(turn)
                    task = asyncio.create_task(after_transcript(), name="realtime-core-final-input")
                    self._retry_tasks.add(task)
                    task.add_done_callback(self._retry_tasks.discard)
            elif kind == EventKind.TRANSCRIPT_FINAL:
                text = (self._clarification.consume(event.text, self.control_dispatch.arbiter,
                                                    input_started_at=turn.input_started_at)
                        if self.control_dispatch else event.text)
                turn.transcript.finish(text)
                turn.evidence.text = text
                await self.push_frame(TranscriptionFrame(event.text, "", time_now_iso8601()), FrameDirection.UPSTREAM)
                if self.provider and self.provider.automatic_responses:
                    self._route(turn)
            else:
                turn.transcript.finish("")
        elif kind == EventKind.RESPONSE_STARTED:
            if not scope.response_id or scope.response_id in self._responses:
                return False
            if self._active_response is not None and not self._active_response.finished.is_set():
                return False
            if self.playback_pending:
                return False
            self._cancel_close()
            turn.reply_pending = False
            if self._pending_response_timer:
                self._pending_response_timer.cancel()
                self._pending_response_timer = None
            response = Response(scope, turn)
            response.watchdog = self._arm_response_timeout(turn, response)
            self._responses[scope.response_id] = response
            self._active_response = response
            self.lifecycle.observe(event)
            await self.push_frame(ResponseStartedFrame(scope=scope))
            await self.push_frame(LLMFullResponseStartFrame())
            await self._call_event_handler("on_response_started", scope)
        else:
            response = self._responses.get(scope.response_id)
            if response is None or response.scope != scope or response.finished.is_set():
                return False
            if kind != EventKind.RESPONSE_ENDED and response.cancelled:
                return False
            self.lifecycle.observe(event)
            if kind == EventKind.TOOL_CALL:
                self._launch_tool(turn, event.call_id, event.tool_name, event.arguments)
            elif kind == EventKind.AUDIO and event.audio:
                if not response.playback.append(scope, event.audio):
                    return False
                if not response.speaking:
                    response.speaking = True
                    await self.push_frame(TTSStartedFrame())
                for chunk in response.playback.release():
                    await self.push_frame(TTSAudioRawFrame(audio=chunk.data,
                        sample_rate=chunk.format.sample_rate, num_channels=chunk.format.channels))
            elif kind == EventKind.AUDIO_ENDED:
                if response.speaking:
                    response.speaking = False
                    await self.push_frame(TTSStoppedFrame())
            elif kind == EventKind.TEXT_DELTA:
                if event.text_is_spoken:
                    response.text += event.text
                    frame = TTSTextFrame(event.text, aggregated_by=AggregationType.SENTENCE)
                    frame.includes_inter_frame_spaces = True
                    await self.push_frame(frame)
                else:
                    await self.push_frame(LLMTextFrame(event.text))
            elif kind == EventKind.TEXT_FINAL and event.text_is_spoken:
                if not response.text:
                    await self.push_frame(TTSTextFrame(event.text, aggregated_by=AggregationType.SENTENCE))
                response.text = event.text
                await self.push_frame(SpokenTextFinalFrame(scope=scope, text=event.text))
            elif kind == EventKind.RESPONSE_ENDED:
                response.playback.generated = True
                if response.speaking:
                    response.speaking = False
                    await self.push_frame(TTSStoppedFrame())
                status = event.status.value if event.status else "unknown"
                await self.push_frame(ResponseDoneFrame(generation=scope.generation, scope=scope, status=status))
                await self.push_frame(LLMFullResponseEndFrame())
                response.finished.set()
                if response.watchdog:
                    response.watchdog.cancel()
                if self._active_response is response:
                    self._active_response = None
                await self._maybe_respond(turn)
        return True

    async def _receive(self, provider):
        try:
            async for event in provider.receive_events():
                if provider is not self.provider:
                    return
                await self.handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Provider receive loop failed")
            if provider is self.provider:
                self._ready.clear()
                await self.push_error(error_msg="realtime receive loop ended unexpectedly")

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (EndFrame, CancelFrame)):
            await self.close_conversation("pipeline stopped")
        elif isinstance(frame, InputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM:
            try:
                await self._input_audio(frame)
            except Exception:
                await self.push_error(error_msg="realtime receive loop: input send failed")
            return
        elif isinstance(frame, LLMContextFrame):
            self.context = frame.context
            return  # Context aggregation must never start an unsolicited response.
        elif isinstance(frame, InterruptionFrame):
            await self.send_interrupt()
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self.close_conversation("pipeline cleanup")
        # Do not cancel dispatched HA operations when their audio session ends.
        if self._tool_jobs:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.gather(*tuple(self._tool_jobs), return_exceptions=True)),
                                       2 * self.tool_registry.timeout_s + 5)
            except TimeoutError:
                logger.error("Shutdown while tool outcome remains unknown; never retry automatically")
        await super().cleanup()
