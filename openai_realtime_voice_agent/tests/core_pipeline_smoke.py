"""Boot and drain the actual Pipecat pipeline without a device or cloud."""

import asyncio
import unittest
from dataclasses import replace
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.task import PipelineTask, PipelineParams
from pipecat.pipeline.runner import PipelineRunner
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import StartFrame, EndFrame, OutputAudioRawFrame, LLMFullResponseStartFrame, InputAudioRawFrame
from app.core.service import RealtimeCoreService
from app.core.confirmation_processor import ConfirmationProcessor
from app.core.events import CanonicalEvent, EventKind, ResponseStatus, AudioChunk
from app.paced_audio_sender import PacedAudioSender
from app.providers.base import ProviderSession
from core_service_smoke import ReadyFake


class Sink(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.audio = bytearray()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()
        if isinstance(frame, OutputAudioRawFrame):
            self.audio.extend(frame.audio)
        await self.push_frame(frame, direction)


class ConsumeLLMStart(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMFullResponseStartFrame):
            await self.push_frame(frame, direction)


class CorePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pipeline_starts_streams_drains_and_closes(self):
        core = RealtimeCoreService(provider_factory=ReadyFake, session=ProviderSession("test"), router_enabled=False)
        review = ConfirmationProcessor(lambda: core.control_evidence, core.regenerate_control_confirmation,
                                       response_evidence=core.evidence_for_response)
        pacer, sink = PacedAudioSender(prime_ms=1400, packet_ms=20), Sink()
        drained = asyncio.Event()
        async def on_drained(scope):
            await core.on_output_drained(scope)
            drained.set()
        pacer.set_response_drained_handler(on_drained, scoped=True)
        task = PipelineTask(Pipeline([core, review, ConsumeLLMStart(), pacer, sink]), params=PipelineParams())
        runner = PipelineRunner(handle_sigint=False)
        running = asyncio.create_task(runner.run(task))
        try:
            await asyncio.wait_for(sink.started.wait(), 2)
            await core.open_conversation()
            item = replace(core.turn.scope, item_id="i")
            for kind in (EventKind.SPEECH_STARTED, EventKind.SPEECH_STOPPED):
                await core.handle_event(CanonicalEvent(kind, item))
            await core.handle_event(CanonicalEvent(EventKind.TRANSCRIPT_FINAL, item, text="你是谁"))
            silent = replace(core.turn.scope, response_id="tool-only")
            await core.handle_event(CanonicalEvent(EventKind.RESPONSE_STARTED, silent))
            await core.handle_event(CanonicalEvent(EventKind.RESPONSE_ENDED, silent, status=ResponseStatus.COMPLETED))
            await asyncio.wait_for(drained.wait(), 2)
            drained.clear()
            scope = replace(core.turn.scope, response_id="r")
            await core.handle_event(CanonicalEvent(EventKind.RESPONSE_STARTED, scope))
            await core.handle_event(CanonicalEvent(EventKind.AUDIO, scope, audio=AudioChunk(b"\1\0" * 480)))
            await core.handle_event(CanonicalEvent(EventKind.RESPONSE_ENDED, scope, status=ResponseStatus.COMPLETED))
            await asyncio.wait_for(drained.wait(), 2)
            self.assertEqual(sink.audio, b"\1\0" * 480)
            self.assertTrue(core._awaiting_mic)
            old_turn = core.turn.scope.turn_id
            await core._input_audio(InputAudioRawFrame(audio=b"\0\0" * 320, sample_rate=16000, num_channels=1))
            self.assertNotEqual(core.turn.scope.turn_id, old_turn)
        finally:
            await task.queue_frames([EndFrame()])
            await asyncio.wait_for(running, 3)
        self.assertIsNone(core.provider)
