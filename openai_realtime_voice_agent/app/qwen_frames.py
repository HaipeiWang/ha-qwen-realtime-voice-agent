"""Provider-specific ordered frames used by the Qwen realtime pipeline."""

from dataclasses import dataclass

from pipecat.frames.frames import DataFrame


@dataclass
class QwenResponseDoneFrame(DataFrame):
    """Ordered provider boundary that generic assistant aggregators don't consume."""

    generation: int = 0
