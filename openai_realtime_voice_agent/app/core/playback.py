"""Response-owned PCM staging and cloud-completion/playback-drain boundaries."""

from dataclasses import dataclass, field
from app.core.events import AudioChunk, EventScope, OUTPUT_FORMAT
from app.core.confirmation import ReplyDecision


@dataclass
class ResponsePlayback:
    scope: EventScope
    guarded: bool = False
    chunks: list[AudioChunk] = field(default_factory=list, repr=False)
    text: str = ""
    generated: bool = False
    drained: bool = False
    discarded: bool = False
    approved: bool = False
    total_bytes: int = 0

    def append(self, scope: EventScope, chunk: AudioChunk) -> bool:
        if scope != self.scope or self.generated or self.discarded:
            return False
        if chunk.format != OUTPUT_FORMAT:
            raise ValueError("Playback requires 24 kHz mono PCM16")
        self.total_bytes += len(chunk.data)
        if self.guarded and self.total_bytes > OUTPUT_FORMAT.bytes_per_second * 8:
            self.discard()
            return False
        self.chunks.append(chunk)
        return True

    @property
    def seconds(self) -> float:
        return self.total_bytes / OUTPUT_FORMAT.bytes_per_second

    def discard(self) -> None:
        self.chunks.clear()
        self.discarded = True

    def release(self) -> tuple[AudioChunk, ...]:
        if self.discarded or (self.guarded and (not self.generated or not self.approved)):
            return ()
        chunks = tuple(self.chunks)
        self.chunks.clear()
        return chunks

    def review_completed(self, decision: ReplyDecision) -> None:
        if not self.generated:
            raise ValueError("Cannot approve unfinished audio")
        self.approved = decision.allowed and not self.discarded
        if not self.approved:
            self.discard()

    def mark_drained(self, scope: EventScope) -> bool:
        if scope != self.scope or not self.generated or self.chunks:
            return False
        self.drained = True
        return True

    @property
    def may_follow_up(self) -> bool:
        return self.generated and self.drained
