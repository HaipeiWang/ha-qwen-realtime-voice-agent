"""Small runtime smoke test for the server-side realtime audio pacer."""

import asyncio
import time

from app.paced_audio_sender import PacedAudioSender
from pipecat.frames.frames import (
    OutputAudioRawFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSStartedFrame,
)
from pipecat.processors.frame_processor import FrameDirection


class CapturePacer(PacedAudioSender):
    def __init__(self):
        super().__init__(prime_ms=1400, packet_ms=20)
        self.sent = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.sent.append((time.monotonic(), frame, direction))


async def main():
    pacer = CapturePacer()
    # A full PipelineTask supplies Pipecat's TaskManager for StartFrame. This
    # isolated smoke test starts only the pacer's own worker.
    pacer._worker = asyncio.create_task(pacer._send_loop())
    await pacer.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    # Deliberately shorter than the 1.4 s prime target. Only the full-response
    # end may release this tail; sentence-scoped TTS stops are not boundaries.
    await pacer.process_frame(
        OutputAudioRawFrame(audio=b"\0" * 4800, sample_rate=24000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await pacer.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.25)

    audio = [(t, f) for t, f, _ in pacer.sent if isinstance(f, OutputAudioRawFrame)]
    assert len(audio) == 5, len(audio)
    assert all(len(frame.audio) == 960 for _, frame in audio)
    assert audio[-1][0] - audio[0][0] >= 0.075
    stop_index = next(
        i for i, (_, f, _) in enumerate(pacer.sent) if isinstance(f, LLMFullResponseEndFrame)
    )
    last_audio_index = max(i for i, (_, f, _) in enumerate(pacer.sent) if isinstance(f, OutputAudioRawFrame))
    assert stop_index > last_audio_index

    # A new response must never inherit queued PCM or priming state from the
    # prior response. Queue an under-watermark old tail, replace it with a new
    # response, and verify only the new sample value is emitted.
    pacer.sent.clear()
    await pacer.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await pacer.process_frame(
        OutputAudioRawFrame(audio=bytes([1, 0]) * 2400, sample_rate=24000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await pacer.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
    await pacer.process_frame(
        OutputAudioRawFrame(audio=bytes([2, 0]) * 2400, sample_rate=24000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await pacer.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.25)
    emitted = b"".join(
        frame.audio for _, frame, _ in pacer.sent if isinstance(frame, OutputAudioRawFrame)
    )
    assert emitted == bytes([2, 0]) * 2400

    # Pipecat's assistant context aggregator may consume the LLM full-start
    # boundary.  After an interruption, a Realtime TTS start must open exactly
    # one fresh response while late PCM before that boundary remains discarded.
    pacer.sent.clear()
    # Call the same reset primitive used by every interruption path.  Feeding
    # an InterruptionFrame directly requires a full PipelineTask TaskManager,
    # which this deliberately isolated worker test does not construct.
    await pacer.reset("smoke interruption")
    await pacer.process_frame(
        OutputAudioRawFrame(audio=bytes([3, 0]) * 480, sample_rate=24000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await pacer.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
    await pacer.process_frame(
        OutputAudioRawFrame(audio=bytes([4, 0]) * 2400, sample_rate=24000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await pacer.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
    await asyncio.sleep(0.25)
    emitted = b"".join(
        frame.audio for _, frame, _ in pacer.sent if isinstance(frame, OutputAudioRawFrame)
    )
    assert emitted == bytes([4, 0]) * 2400
    pacer._closed = True
    async with pacer._condition:
        pacer._condition.notify_all()
    pacer._worker.cancel()
    try:
        await pacer._worker
    except asyncio.CancelledError:
        pass
    print("pacer short-tail smoke test: ok")


if __name__ == "__main__":
    asyncio.run(main())
