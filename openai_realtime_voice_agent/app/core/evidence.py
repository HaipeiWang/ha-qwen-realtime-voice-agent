"""Turn-owned evidence consumed by confirmation, never by HA execution."""

from dataclasses import dataclass, field
import re
from app.core.arbitration import light_effects
from app.core.confirmation import ConfirmationReview, confirmation_instruction
from app.tools.registry import ToolOutcome


@dataclass
class ControlEvidence:
    text: str = ""
    outcomes: dict[str, ToolOutcome] = field(default_factory=dict)
    in_flight: int = 0
    regeneration_active: bool = False
    response_waiting_for_tools: bool = False
    reviewer: ConfirmationReview = field(default_factory=ConfirmationReview)

    @property
    def control_requested(self) -> bool:
        return bool(self.outcomes or self.in_flight or light_effects(self.text) or
                    re.search(r"打开|关闭|关掉|开启|启动|熄灭|调到|调成|设置|停止|停下|开灯|关灯|^(?:开|关).*(?:灯|电视|空调|风扇)", self.text))

    def record(self, outcome: ToolOutcome) -> None:
        self.outcomes[outcome.result.execution_id] = outcome

    @property
    def coverage_complete(self) -> bool:
        if self.in_flight or not self.outcomes:
            return False
        requested = light_effects(self.text)
        performed = set()
        for outcome in self.outcomes.values():
            data = outcome.data if isinstance(outcome.data, dict) else {}
            if outcome.result.status == "completed":
                performed.update((data.get("executed_arguments") or {}).keys())
        return requested.issubset(performed)

    def retry_instruction(self) -> str:
        notes = [confirmation_instruction(self.text, o) for o in self.outcomes.values()]
        if not notes:
            notes = ["没有工具执行证据。说明尚未执行，不得声称已经完成。"]
            if re.search(r"(?:分钟|小时|秒钟?)(?:以|之)?后|定时|闹钟", self.text):
                notes.append("目前不支持定时和闹钟，没有创建任务，不会稍后执行；简短说明暂不支持。")
        return "\n".join(notes) + "\n只重新生成短回执，不调用任何工具。"
