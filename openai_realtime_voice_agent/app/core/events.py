"""Internal events. Cloud protocol dictionaries never form the Core API."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int
    channels: int = 1
    encoding: str = "pcm16"

    def __post_init__(self):
        if self.sample_rate <= 0 or self.channels != 1 or self.encoding != "pcm16":
            raise ValueError("Audio must be mono PCM16 with a positive sample rate")

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.channels * 2


INPUT_FORMAT = AudioFormat(16000)
OUTPUT_FORMAT = AudioFormat(24000)


@dataclass(frozen=True)
class AudioChunk:
    data: bytes = field(repr=False)
    format: AudioFormat = OUTPUT_FORMAT

    def __post_init__(self):
        if not isinstance(self.data, bytes) or len(self.data) % 2:
            raise ValueError("PCM16 audio must contain complete samples")

    @property
    def duration_seconds(self) -> float:
        return len(self.data) / self.format.bytes_per_second


class EventKind(str, Enum):
    CONNECTED = "connected"
    READY = "ready"
    SPEECH_STARTED = "speech_started"
    SPEECH_STOPPED = "speech_stopped"
    TRANSCRIPT_FINAL = "transcript_final"
    TRANSCRIPT_FAILED = "transcript_failed"
    RESPONSE_STARTED = "response_started"
    TEXT_DELTA = "text_delta"
    TEXT_FINAL = "text_final"
    AUDIO = "audio"
    AUDIO_ENDED = "audio_ended"
    TOOL_CALL = "tool_call"
    RESPONSE_ENDED = "response_ended"
    ERROR = "error"
    CLOSED = "closed"


class ResponseStatus(str, Enum):
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


class ErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    CONNECTION = "connection"
    PROTOCOL = "protocol"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    INTERNAL = "internal"


@dataclass(frozen=True)
class EventScope:
    """Association established at input/response creation, never on late delivery."""

    generation: int
    conversation_id: str
    turn_id: str | None = None
    response_id: str | None = None
    item_id: str | None = None


@dataclass(frozen=True)
class ProviderError:
    kind: ErrorKind
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class CanonicalEvent:
    kind: EventKind
    scope: EventScope
    text: str = field(default="", repr=False)
    text_is_spoken: bool = False
    audio: AudioChunk | None = field(default=None, repr=False)
    call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict, repr=False)
    status: ResponseStatus | None = None
    error: ProviderError | None = None
    tool_names: tuple[str, ...] = ()
    tools_valid: bool = False
