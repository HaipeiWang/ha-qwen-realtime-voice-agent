import unittest
from pipecat.frames.frames import (LLMFullResponseStartFrame, TTSTextFrame,
                                  TTSAudioRawFrame, AggregationType)
from pipecat.processors.frame_processor import FrameDirection
from app.core.confirmation_processor import ConfirmationProcessor
from app.core.evidence import ControlEvidence
from app.core.frames import ResponseDoneFrame, SpokenTextFinalFrame
from app.tools.registry import ToolOutcome
from app.tools.schema import CanonicalToolResult


class Capture(ConfirmationProcessor):
    def __init__(self, evidence):
        self.sent = []
        self.retries = []
        async def retry(value):
            self.retries.append(value)
        super().__init__(lambda: evidence, retry)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.sent.append(frame)


class ConfirmationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def response(self, processor, text):
        for frame in (LLMFullResponseStartFrame(),
                      TTSAudioRawFrame(audio=b"\0\0" * 240, sample_rate=24000, num_channels=1),
                      TTSTextFrame(text, aggregated_by=AggregationType.SENTENCE)):
            await processor.process_frame(frame, FrameDirection.DOWNSTREAM)

    async def test_silent_tool_response_does_not_consume_retry(self):
        processor = Capture(ControlEvidence())
        await processor.process_frame(LLMFullResponseStartFrame(), FrameDirection.DOWNSTREAM)
        await processor.process_frame(ResponseDoneFrame(), FrameDirection.DOWNSTREAM)
        self.assertFalse(processor.retries)

    async def test_no_tool_success_audio_never_reaches_output(self):
        evidence = ControlEvidence(text="打开客厅灯")
        processor = Capture(evidence)
        await self.response(processor, "已经打开了。")
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in processor.sent))
        await processor.process_frame(ResponseDoneFrame(), FrameDirection.DOWNSTREAM)
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in processor.sent))
        self.assertEqual(len(processor.retries), 1)
        await self.response(processor, "已经打开了。")
        await processor.process_frame(ResponseDoneFrame(), FrameDirection.DOWNSTREAM)
        self.assertEqual(len(processor.retries), 1)

    async def test_verified_execution_releases_matching_audio_after_review(self):
        evidence = ControlEvidence(text="打开客厅灯")
        evidence.record(ToolOutcome(CanonicalToolResult("e", "completed"), {"executed_arguments": {"name": "客厅灯"}}))
        processor = Capture(evidence)
        await self.response(processor, "客厅灯已经打开了。")
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in processor.sent))
        await processor.process_frame(ResponseDoneFrame(), FrameDirection.DOWNSTREAM)
        self.assertEqual(sum(isinstance(f, TTSAudioRawFrame) for f in processor.sent), 1)

    async def test_ordinary_chat_remains_streamed(self):
        processor = Capture(ControlEvidence(text="你是谁"))
        await self.response(processor, "我是你的家庭助手。")
        self.assertTrue(any(isinstance(f, TTSAudioRawFrame) for f in processor.sent))

    async def test_inflight_tool_does_not_trigger_regeneration(self):
        evidence = ControlEvidence(text="打开客厅灯", in_flight=1)
        processor = Capture(evidence)
        await self.response(processor, "已经打开了。")
        await processor.process_frame(ResponseDoneFrame(), FrameDirection.DOWNSTREAM)
        self.assertFalse(processor.retries)
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in processor.sent))

    async def test_final_spoken_text_overrides_partial_delta_for_review(self):
        processor = Capture(ControlEvidence(text="打开客厅灯"))
        await self.response(processor, "好的")
        await processor.process_frame(SpokenTextFinalFrame(text="已经打开了。"), FrameDirection.DOWNSTREAM)
        await processor.process_frame(ResponseDoneFrame(), FrameDirection.DOWNSTREAM)
        self.assertFalse(any(isinstance(f, TTSAudioRawFrame) for f in processor.sent))
        self.assertEqual(len(processor.retries), 1)
