import asyncio
import unittest
from pacer_smoke import CapturePacer
from app.core.events import EventScope
from app.core.frames import ResponseDoneFrame
from pipecat.frames.frames import LLMFullResponseStartFrame, LLMFullResponseEndFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection


class ScopedPacerTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_response_scope_arrives_after_its_pcm(self):
        pacer = CapturePacer()
        received = []
        ready = asyncio.Event()
        async def drained(scope):
            received.append(scope)
            ready.set()
        pacer.set_response_drained_handler(drained, scoped=True)
        pacer._worker = asyncio.create_task(pacer._send_loop())
        scope = EventScope(7, "conversation", "turn", "response")
        try:
            for frame in (LLMFullResponseStartFrame(),
                          OutputAudioRawFrame(audio=b"\0\0" * 480, sample_rate=24000, num_channels=1),
                          ResponseDoneFrame(generation=7, scope=scope), LLMFullResponseEndFrame()):
                await pacer.process_frame(frame, FrameDirection.DOWNSTREAM)
            await asyncio.wait_for(ready.wait(), 1)
            self.assertEqual(received, [scope])
            self.assertEqual(sum(len(f.audio) for _, f, _ in pacer.sent if isinstance(f, OutputAudioRawFrame)), 960)
            self.assertIsInstance(pacer.sent[-1][1], LLMFullResponseEndFrame)
        finally:
            await pacer.cleanup()
