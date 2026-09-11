"""Provider-neutral ordered boundaries used by the Pipecat transport."""

from dataclasses import dataclass
from pipecat.frames.frames import DataFrame
from app.core.events import EventScope


@dataclass
class ResponseStartedFrame(DataFrame):
    scope: EventScope | None = None


@dataclass
class SpokenTextFinalFrame(DataFrame):
    scope: EventScope | None = None
    text: str = ""


@dataclass
class ResponseDoneFrame(DataFrame):
    """Must travel behind PCM through the pacer before reporting drain."""
    generation: int = 0
    scope: EventScope | None = None
    status: str = "completed"
