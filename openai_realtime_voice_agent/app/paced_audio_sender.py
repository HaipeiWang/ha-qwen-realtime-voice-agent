"""Smooth bursty realtime audio into clocked packets for Voice PE.

OpenAI Realtime commonly delivers several hundred milliseconds of PCM at once,
then nothing for up to a second.  Forwarding those bursts directly makes the
small playback buffers on Voice PE repeatedly run dry.  This processor keeps a
server-side queue, primes it once per bot-speaking segment, and emits PCM at
wall-clock speed in 20 ms packets.
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional, Union

from pipecat.frames.frames import (
    BotInterruptionFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    InterruptionTaskFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    StartFrame,
    StartInterruptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)


@dataclass
class _AudioItem:
    data: bytearray
    sample_rate: int
    num_channels: int
    response_id: int


@dataclass
class _ControlItem:
    frame: Frame
    direction: FrameDirection
    response_id: int


_QueueItem = Union[_AudioItem, _ControlItem]


class PacedAudioSender(FrameProcessor):
    """Buffer output PCM and release it at a fixed, realtime cadence."""

    def __init__(self, prime_ms: int = 1400, packet_ms: int = 20, **kwargs):
        super().__init__(**kwargs)
        self._prime_ms = max(packet_ms, prime_ms)
        self._packet_ms = max(10, packet_ms)
        self._items: Deque[_QueueItem] = deque()
        self._condition = asyncio.Condition()
        self._worker: Optional[asyncio.Task] = None
        self._boundary_task: Optional[asyncio.Task] = None
        self._primed = False
        self._closed = False
        self._response_id = 0
        self._response_active = False
        # After an explicit interruption, late PCM from the cancelled response
        # must be discarded until a new LLMFullResponseStartFrame arrives.
        self._drop_audio_until_start = False
        self._first_pcm_logged_for_response = False

    def _cancel_boundary_timeout(self) -> None:
        if self._boundary_task is not None and not self._boundary_task.done():
            self._boundary_task.cancel()
        self._boundary_task = None

    def _arm_boundary_timeout(self, delay_s: float = 4.0) -> None:
        """Synthesize a safe response end if upstream loses the real boundary."""
        self._cancel_boundary_timeout()
        response_id = self._response_id

        async def _finish_if_still_current():
            try:
                await asyncio.sleep(delay_s)
                async with self._condition:
                    if (
                        self._closed
                        or not self._response_active
                        or response_id != self._response_id
                    ):
                        return
                    self._items.append(
                        _ControlItem(
                            LLMFullResponseEndFrame(),
                            FrameDirection.DOWNSTREAM,
                            response_id,
                        )
                    )
                    self._response_active = False
                    self._condition.notify_all()
                logger.warning(
                    "Paced response %u had no full-response boundary for %.1fs; "
                    "queued safe synthetic end",
                    response_id,
                    delay_s,
                )
            except asyncio.CancelledError:
                return

        self._boundary_task = asyncio.create_task(_finish_if_still_current())

    async def reset(self, reason: str, *, wait_for_start: bool = True) -> None:
        """Atomically discard the current response and reset pacing state."""
        self._cancel_boundary_timeout()
        async with self._condition:
            dropped = sum(
                len(item.data) for item in self._items if isinstance(item, _AudioItem)
            )
            self._items.clear()
            self._response_id += 1
            self._response_active = False
            self._primed = False
            self._drop_audio_until_start = wait_for_start
            self._condition.notify_all()
        logger.info(
            "Paced output reset (%s): response=%u, dropped=%u bytes",
            reason,
            self._response_id,
            dropped,
        )

    async def _start_response(self) -> None:
        # A new response owns a fresh queue. If the prior response lost its end
        # boundary, never concatenate its tail with this response's first sample.
        self._cancel_boundary_timeout()
        async with self._condition:
            dropped = sum(
                len(item.data) for item in self._items if isinstance(item, _AudioItem)
            )
            self._items.clear()
            self._response_id += 1
            self._response_active = True
            self._primed = False
            self._drop_audio_until_start = False
            self._first_pcm_logged_for_response = False
            self._condition.notify_all()
        logger.info(
            "Paced response %u started with independent queue%s",
            self._response_id,
            f" (discarded {dropped} stale bytes)" if dropped else "",
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            if self._worker is None or self._worker.done():
                self._closed = False
                self._worker = asyncio.create_task(self._send_loop())
            await self.push_frame(frame, direction)
            return

        # These frames represent a real pipeline interruption. Clear both PCM
        # and boundary/control frames so a cancelled response cannot leak into
        # the next one. The explicit device interrupt callback also invokes
        # reset(), covering cancellations that intentionally bypass Pipecat's
        # interruption machinery.
        if isinstance(
            frame,
            (StartInterruptionFrame, InterruptionFrame, InterruptionTaskFrame, BotInterruptionFrame),
        ):
            await self.reset(type(frame).__name__)
            await self.push_frame(frame, direction)
            return

        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, LLMFullResponseStartFrame
        ):
            await self._start_response()
            await self.push_frame(frame, direction)
            return

        # In Pipecat 0.0.97 the assistant context aggregator can consume the
        # LLMFullResponseStartFrame before it reaches this processor.  The
        # Realtime service's TTSStartedFrame is emitted immediately before the
        # first PCM delta and is therefore also an authoritative response
        # boundary.  Accept it only when no response is active, so sentence-
        # level restarts can never clear already queued audio.
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSStartedFrame):
            if self._drop_audio_until_start or not self._response_active:
                await self._start_response()
                logger.info("Realtime TTS start accepted as response boundary")
            await self.push_frame(frame, direction)
            return

        # Only pace assistant playback travelling toward the websocket.
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, OutputAudioRawFrame):
            if not frame.audio:
                return
            if self._drop_audio_until_start:
                logger.debug("Dropping late PCM from interrupted response until next response start")
                return
            if not self._response_active:
                # Compatibility guard for providers that omit a start frame.
                # This is never allowed immediately after an interruption.
                await self._start_response()
                logger.warning("PCM arrived without LLMFullResponseStartFrame; opened implicit response")
            async with self._condition:
                # Merge adjacent PCM frames with identical format. This lets the
                # sender make exact 20 ms packets even when upstream chunk sizes
                # are irregular.
                if (
                    self._items
                    and isinstance(self._items[-1], _AudioItem)
                    and self._items[-1].sample_rate == frame.sample_rate
                    and self._items[-1].num_channels == frame.num_channels
                    and self._items[-1].response_id == self._response_id
                ):
                    self._items[-1].data.extend(frame.audio)
                else:
                    self._items.append(
                        _AudioItem(
                            bytearray(frame.audio),
                            frame.sample_rate,
                            frame.num_channels,
                            self._response_id,
                        )
                    )
                self._condition.notify()
            if not self._first_pcm_logged_for_response:
                self._first_pcm_logged_for_response = True
                logger.info(
                    "Paced response %u received first PCM: %u bytes, %u Hz",
                    self._response_id, len(frame.audio), frame.sample_rate,
                )
            self._arm_boundary_timeout()
            return

        # TTS/Bot stop can be sentence-level, so it is not itself a boundary.
        # It does tell us output has gone quiet; use a shorter fallback timer,
        # cancelled by any subsequent PCM or the real full-response end.
        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (TTSStoppedFrame, BotStoppedSpeakingFrame)
        ):
            if self._response_active:
                self._arm_boundary_timeout(3.0)
            await self.push_frame(frame, direction)
            return

        # Only the FULL response end is an audio boundary. OpenAI emits
        # TTSStarted/TTSStopped around sentence-sized subsegments; treating
        # those as boundaries released 350-400 ms fragments and repeatedly
        # reset priming mid-answer. LLMFullResponseEndFrame permits a genuinely
        # short final response to drain below the normal prime threshold.
        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (LLMFullResponseEndFrame, EndFrame)
        ):
            self._cancel_boundary_timeout()
            async with self._condition:
                self._items.append(_ControlItem(frame, direction, self._response_id))
                if isinstance(frame, LLMFullResponseEndFrame):
                    self._response_active = False
                self._condition.notify()
            return

        await self.push_frame(frame, direction)

    def _bytes_before_boundary(self) -> tuple[int, bool]:
        total = 0
        for item in self._items:
            if isinstance(item, _ControlItem):
                return total, True
            total += len(item.data)
        return total, False

    async def _send_loop(self):
        try:
            while not self._closed:
                frame_to_send: Optional[OutputAudioRawFrame] = None
                control_to_send: Optional[_ControlItem] = None
                packet_duration = self._packet_ms / 1000.0

                async with self._condition:
                    while not self._items and not self._closed:
                        await self._condition.wait()
                    if self._closed:
                        return

                    available, has_boundary = self._bytes_before_boundary()
                    first = self._items[0]

                    if isinstance(first, _ControlItem):
                        control_to_send = self._items.popleft()
                        if isinstance(control_to_send.frame, LLMFullResponseEndFrame):
                            self._primed = False
                    else:
                        bytes_per_second = first.sample_rate * first.num_channels * 2
                        prime_bytes = bytes_per_second * self._prime_ms // 1000
                        packet_bytes = bytes_per_second * self._packet_ms // 1000

                        if not self._primed and available < prime_bytes and not has_boundary:
                            await self._condition.wait()
                            continue
                        if not self._primed:
                            self._primed = True
                            logger.info(
                                "🎚️ Paced output primed: %.0f ms buffered",
                                available * 1000 / bytes_per_second,
                            )

                        # At a boundary the final packet may be shorter than
                        # 20 ms. Between bursts, wait for a complete packet.
                        if len(first.data) < packet_bytes and not has_boundary:
                            await self._condition.wait()
                            continue
                        take = min(packet_bytes, len(first.data))
                        payload = bytes(first.data[:take])
                        del first.data[:take]
                        if not first.data:
                            self._items.popleft()
                        packet_duration = take / bytes_per_second
                        frame_to_send = OutputAudioRawFrame(
                            audio=payload,
                            sample_rate=first.sample_rate,
                            num_channels=first.num_channels,
                        )

                    item_response_id = first.response_id

                if control_to_send is not None:
                    if control_to_send.response_id != self._response_id:
                        continue
                    await self.push_frame(control_to_send.frame, control_to_send.direction)
                    if isinstance(control_to_send.frame, EndFrame):
                        self._closed = True
                        return
                    continue

                if frame_to_send is not None:
                    if item_response_id != self._response_id:
                        continue
                    await self.push_frame(frame_to_send, FrameDirection.DOWNSTREAM)
                    # Schedule from the completion of the send, never from an
                    # old deadline. A delayed network write therefore cannot be
                    # followed by a damaging catch-up burst.
                    await asyncio.sleep(max(0.001, packet_duration))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("❌ Paced audio sender failed")

    async def cleanup(self):
        self._closed = True
        self._cancel_boundary_timeout()
        async with self._condition:
            self._condition.notify_all()
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        await super().cleanup()
