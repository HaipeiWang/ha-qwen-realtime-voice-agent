"""Hold control speech until its spoken text and execution evidence are reviewed."""

import logging
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection
from pipecat.frames.frames import (
    LLMFullResponseStartFrame, LLMFullResponseEndFrame,
    OutputAudioRawFrame, TTSTextFrame, TTSStartedFrame, TTSStoppedFrame,
    InterruptionFrame, EndFrame, CancelFrame,
)
from app.core.frames import ResponseDoneFrame, ResponseStartedFrame, SpokenTextFinalFrame

logger = logging.getLogger(__name__)


class ConfirmationProcessor(FrameProcessor):
    def __init__(self, evidence, regenerate, response_evidence=None, **kwargs):
        super().__init__(**kwargs)
        self._get_evidence = evidence
        self._regenerate = regenerate
        self._response_evidence = response_evidence
        self._next_scope = None
        self._clear()

    def _clear(self):
        self._evidence = None
        self._held = []
        self._text = ""
        self._seconds = 0.0
        self._overflow = False
        self._guarded = False
        self._orphaned = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction != FrameDirection.DOWNSTREAM:
            await self.push_frame(frame, direction)
            return
        if isinstance(frame, ResponseStartedFrame):
            self._next_scope = frame.scope
            await self.push_frame(frame, direction)
            return
        if self._orphaned and isinstance(frame, (OutputAudioRawFrame, TTSTextFrame, TTSStartedFrame, TTSStoppedFrame)):
            return
        if self._guarded and isinstance(frame, SpokenTextFinalFrame):
            self._text = frame.text
            if len(self._text) > 80:
                self._overflow = True
                self._held.clear()
            return
        if self._guarded and self._evidence.text and not self._evidence.control_requested and not self._overflow:
            for held in self._held:
                await self.push_frame(held, direction)
            self._held.clear()
            self._guarded = False
        if isinstance(frame, (InterruptionFrame, EndFrame, CancelFrame)):
            self._clear()
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._clear()
            self._evidence = (self._response_evidence(self._next_scope)
                              if self._next_scope and self._response_evidence else self._get_evidence())
            self._orphaned = self._evidence is None
            self._next_scope = None
            self._guarded = self._evidence is not None and (
                not self._evidence.text or self._evidence.control_requested)
        elif self._guarded and isinstance(frame, (OutputAudioRawFrame, TTSTextFrame, TTSStartedFrame, TTSStoppedFrame)):
            if isinstance(frame, OutputAudioRawFrame):
                self._seconds += len(frame.audio) / (frame.sample_rate * frame.num_channels * 2)
            elif isinstance(frame, TTSTextFrame):
                self._text += frame.text
            if self._seconds > 8 or len(self._text) > 80:
                self._overflow = True
                self._held.clear()
            if not self._overflow:
                self._held.append(frame)
            return
        elif self._guarded and isinstance(frame, ResponseDoneFrame):
            # Tool-only responses carry no spoken receipt to review. The Core
            # owns the post-tool response; do not consume its retry allowance.
            if not self._text.strip() and self._seconds == 0:
                self._held.clear()
                self._guarded = False
                await self.push_frame(frame, direction)
                return
            evidence = self._evidence
            decision = evidence.reviewer.review(self._text, self._seconds, list(evidence.outcomes.values()),
                                                action_coverage_complete=evidence.coverage_complete,
                                                request_text=evidence.text)
            if decision.allowed and not self._overflow and not evidence.in_flight and frame.status == "completed":
                for held in self._held:
                    await self.push_frame(held, direction)
            else:
                logger.warning("Control audio withheld: reason=%s pending_tools=%s", decision.reason, evidence.in_flight)
                if evidence.in_flight:
                    evidence.response_waiting_for_tools = True
                if not evidence.in_flight and evidence.reviewer.request_regeneration():
                    # The callback must queue generation after the current
                    # response ends. It is never given a tool executor.
                    await self._regenerate(evidence)
            self._held.clear()
            self._guarded = False
        elif self._guarded and isinstance(frame, LLMFullResponseEndFrame):
            # A synthetic cancellation/error boundary cannot release speech.
            self._held.clear()
            self._guarded = False
        await self.push_frame(frame, direction)
