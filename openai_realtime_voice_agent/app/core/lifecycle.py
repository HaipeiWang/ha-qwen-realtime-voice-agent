"""Shared provider-independent session and turn lifecycle.

Strict rejection of late events after cancellation is available as a composition
policy; the preview composition keeps the established Stop behavior by default.
"""

import time
import uuid

from app.core.events import CanonicalEvent, EventKind
from app.core.turn import ConversationState, TurnContext


class ConversationLifecycle:
    def __init__(self, clock=time.monotonic, follow_up_seconds: float = 8):
        self.clock = clock
        self.follow_up_seconds = follow_up_seconds
        self.conversation_id = uuid.uuid4().hex
        self.generation = 0
        self.turn: TurnContext | None = None
        self.state = ConversationState.IDLE
        self.follow_up_deadline: float | None = None
        self.requires_wake = False
        self.response_generated: set[str] = set()
        self.response_drained: set[str] = set()

    def _new_turn(self) -> TurnContext:
        self.turn = TurnContext(self.conversation_id, self.generation)
        self.follow_up_deadline = None
        self.response_generated.clear()
        self.response_drained.clear()
        self.state = ConversationState.LISTENING
        return self.turn

    def wake(self) -> TurnContext:
        if self.turn:
            self.turn.cancelled = True
        if self.generation == 0:
            self.generation = 1
        self.requires_wake = False
        return self._new_turn()

    def follow_up(self) -> TurnContext | None:
        if self.requires_wake or self.state != ConversationState.FOLLOW_UP:
            return None
        if self.follow_up_deadline is None or self.clock() >= self.follow_up_deadline:
            self.close()
            return None
        return self._new_turn()

    def cancel(self) -> None:
        if self.turn:
            self.turn.cancelled = True
        self.requires_wake = True
        self.follow_up_deadline = None
        self.state = ConversationState.IDLE

    def close(self) -> None:
        if self.turn:
            self.turn.cancelled = True
        self.follow_up_deadline = None
        self.state = ConversationState.IDLE

    def recover(self) -> None:
        self.close()
        self.conversation_id = uuid.uuid4().hex
        self.generation += 1
        self.requires_wake = True

    def observe(self, event: CanonicalEvent) -> bool:
        turn = self.turn
        if turn is None or turn.cancelled:
            return False
        scope = event.scope
        if (scope.conversation_id, scope.generation, scope.turn_id) != (
                turn.conversation_id, turn.generation, turn.turn_id):
            return False
        if event.kind == EventKind.RESPONSE_STARTED:
            if not scope.response_id:
                return False
            turn.response_ids.add(scope.response_id)
        elif event.kind == EventKind.SPEECH_STARTED and scope.item_id:
            turn.input_item_ids.add(scope.item_id)
        if not turn.accepts(scope):
            return False
        if event.kind == EventKind.SPEECH_STOPPED:
            self.state = ConversationState.PROCESSING
        elif event.kind == EventKind.TOOL_CALL:
            self.state = ConversationState.TOOL_EXECUTING
        elif event.kind == EventKind.AUDIO:
            self.state = ConversationState.SPEAKING
        elif event.kind == EventKind.RESPONSE_ENDED and scope.response_id:
            self.response_generated.add(scope.response_id)
        elif event.kind == EventKind.ERROR:
            self.state = ConversationState.ERROR
        return True

    def output_drained(self, scope) -> bool:
        if self.turn is None or not self.turn.accepts(scope) or scope.response_id not in self.response_generated:
            return False
        self.response_drained.add(scope.response_id)
        return True

    def microphone_opened(self) -> bool:
        """Start follow-up only after all generated audio drained and mic reopened."""
        if self.requires_wake or not self.turn or self.turn.cancelled:
            return False
        if not self.turn.response_ids or self.turn.response_ids != self.response_generated or self.response_generated != self.response_drained:
            return False
        if self.state != ConversationState.FOLLOW_UP:
            self.follow_up_deadline = self.clock() + self.follow_up_seconds
            self.state = ConversationState.FOLLOW_UP
        return True
