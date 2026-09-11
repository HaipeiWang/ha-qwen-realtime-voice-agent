"""Explicit ownership of a conversation, input turn and its response children."""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from app.core.events import EventScope


class ConversationState(str, Enum):
    IDLE = "idle"
    CONNECTING = "connecting"
    READY = "ready"
    LISTENING = "listening"
    PROCESSING = "processing"
    TOOL_EXECUTING = "tool_executing"
    SPEAKING = "speaking"
    FOLLOW_UP = "follow_up"
    CLOSING = "closing"
    ERROR = "error"


@dataclass
class TurnContext:
    conversation_id: str
    generation: int
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancelled: bool = False
    response_ids: set[str] = field(default_factory=set)
    input_item_ids: set[str] = field(default_factory=set)
    timestamps: dict[str, float] = field(default_factory=dict)

    @property
    def scope(self) -> EventScope:
        return EventScope(self.generation, self.conversation_id, self.turn_id)

    def accepts(self, scope: EventScope) -> bool:
        if self.cancelled or (
            scope.generation != self.generation
            or scope.conversation_id != self.conversation_id
            or scope.turn_id != self.turn_id
        ):
            return False
        if scope.response_id and scope.response_id not in self.response_ids:
            return False
        if not scope.response_id and scope.item_id and scope.item_id not in self.input_item_ids:
            return False
        return True

    def mark(self, event: str) -> None:
        self.timestamps.setdefault(event, time.monotonic())

    def latency_ms(self, start: str, end: str) -> float | None:
        if start not in self.timestamps or end not in self.timestamps:
            return None
        return max(0.0, (self.timestamps[end] - self.timestamps[start]) * 1000)
