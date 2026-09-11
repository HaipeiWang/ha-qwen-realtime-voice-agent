"""Future durable scheduling boundary. No scheduler or exposed tools yet.

Creating a job acknowledges persistence, not device execution. A job outlives
its originating voice turn. Executors must recheck HA exposure at due time.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ScheduledJob:
    job_id: str
    idempotency_key: str
    due_at: datetime
    timezone: str
    kind: str  # device_action or alarm
    tool_name: str
    arguments: dict = field(repr=False)

    def __post_init__(self):
        if self.due_at.tzinfo is None or self.kind not in {"device_action", "alarm"}:
            raise ValueError("Job requires an aware due time and supported kind")
        if not self.job_id or not self.idempotency_key:
            raise ValueError("Durable job and idempotency identities are required")


class ScheduleStore(Protocol):
    async def create(self, job: ScheduledJob) -> ScheduledJob: ...
    async def get(self, job_id: str) -> ScheduledJob | None: ...
    async def cancel(self, job_id: str) -> bool: ...
    async def pending(self) -> tuple[ScheduledJob, ...]: ...
