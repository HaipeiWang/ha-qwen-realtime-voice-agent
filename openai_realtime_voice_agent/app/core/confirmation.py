"""Evidence-based instructions shared by realtime providers.

These instructions constrain generation; playback review is a separate boundary.
They are not proof that the model will obey or that a physical state was read back.
"""

import re
from dataclasses import dataclass

from app.tools.registry import ToolOutcome


def confirmation_instruction(action: str, outcome: ToolOutcome) -> str:
    result = outcome.result
    policy = {
        "completed": "工具报告本次操作完成；只确认本次记录的动作，不补充未执行的参数或动作。",
        "partial": "只完成部分操作；只确认 successful_targets，说明 failed_targets 未完成；不得说全部成功。",
        "failed": "本次操作失败或被拒绝；如实说明未完成，不得声称已执行成功。",
        "accepted": "指令已受理，但尚未验证最终状态；不得声称失败，也不得声称已经确认完成。",
        "unknown": "本次执行结果未知；说明暂时无法确认，不得声称成功或确定失败，也不得重新执行。",
    }[result.status]
    targets = ""
    reasons = {
        "scheduling_not_implemented": "尚不支持定时操作；没有创建任务，不会稍后自动执行。",
        "explicit_brightness_required": "缺少明确亮度数值，需要询问用户目标亮度。",
        "explicit_temperature_required": "缺少明确色温数值，需要询问用户目标色温。",
        "state_query_does_not_authorize_write": "用户是在查询，不允许更改设备。",
        "target_requires_clarification": "目标不明确，需要询问具体设备名称。",
        "explicit_action_required": "缺少明确动作，需要询问用户希望怎样操作。",
        "compound_actions_require_separate_requests": "目前需要将开关和参数调节拆成两次请求。",
    }
    explanation = reasons.get(result.detail, result.detail)
    if result.status == "partial":
        targets = f" successful_targets={result.successful_targets!r}; failed_targets={result.failed_targets!r}。"
    return (
        f"[系统] 本次请求：{action}。{policy}{targets}结果分类原因：{explanation}。"
        "不得把缺参、需要澄清或暂不支持解释为设备不存在、未配置或离线。"
        "只生成自然简短的中文回执，建议15–40字，最多2句、80字；"
        "不调用工具，不重复执行。不把设备标识符当作人类可读名称。"
    )


@dataclass(frozen=True)
class ReplyDecision:
    allowed: bool
    reason: str = ""


class ConfirmationReview:
    """Bounded review of a completed spoken transcript and its matching audio.

    Language checks reduce false confirmations, but do not prove arbitrary
    natural language semantics. Missing speech text is never safe to approve.
    """
    def __init__(self):
        self.regenerations = 0

    def review(self, text: str, audio_seconds: float, outcomes: list[ToolOutcome],
               *, action_coverage_complete: bool, request_text: str = "") -> ReplyDecision:
        if not text.strip():
            return ReplyDecision(False, "spoken_text_missing")
        if len(text.strip()) > 80 or len(re.findall(r"[。！？!?]+", text)) > 2 or audio_seconds > 8:
            return ReplyDecision(False, "control_reply_too_long")
        # Remove explicit denials before looking for affirmative completion.
        positive = re.sub(r"(?:未|没有|无法|不能|尚未)(?:能|确认)?(?:完成|成功|打开|关闭|调节|设置)", "", text)
        claims_success = bool(re.search(r"(?:已|已经)(?:经)?(?:完成|成功|打开|关闭|调|设|关|开)|搞定|设置好了|关好了|开好了|完成了|成功了", positive))
        statuses = {o.result.status for o in outcomes}
        scheduling_rejected = any(o.result.detail == "scheduling_not_implemented" for o in outcomes) or bool(
            re.search(r"(?:分钟|小时|秒钟?)(?:以|之)?后|定时|闹钟", request_text))
        policy_rejected = any(o.result.detail in {"scheduling_not_implemented", "explicit_brightness_required",
            "explicit_temperature_required", "state_query_does_not_authorize_write", "target_requires_clarification",
            "explicit_action_required", "compound_actions_require_separate_requests"} for o in outcomes)
        if policy_rejected and re.search(r"没找到|未找到|不存在|未配置|没有配置|离线", text):
            return ReplyDecision(False, "fabricated_device_failure")
        if scheduling_rejected and not re.search(r"(?:无法|不能|不支持|暂不|尚不|未).{0,10}(?:定时|安排|设置|执行|分钟|秒|小时)", text):
            return ReplyDecision(False, "unsupported_scheduling_promise")
        promises_action = bool(re.search(r"(?:我会|将会|这就|稍后会|届时|到时会|已安排).{0,30}(?:打开|关闭|调|设|执行)", positive))
        if promises_action and (not action_coverage_complete or statuses & {"failed", "unknown"}):
            return ReplyDecision(False, "promise_without_execution_evidence")
        if claims_success and (not outcomes or not action_coverage_complete):
            return ReplyDecision(False, "success_without_complete_execution_evidence")
        if statuses & {"failed", "unknown", "accepted"} and claims_success and "completed" not in statuses and "partial" not in statuses:
            return ReplyDecision(False, "success_contradicts_outcome")
        mixed = "partial" in statuses or ("completed" in statuses and bool(statuses - {"completed"}))
        if mixed and (re.search(r"全部|都已|都完成|全好了", text) or not re.search(r"部分|但|未|失败|无法|尚", text)):
            return ReplyDecision(False, "partial_result_not_disclosed")
        if statuses and statuses <= {"unknown", "accepted"} and not re.search(r"尚|暂|未确认|无法确认|等待|受理|已发送|同步|未知", text):
            return ReplyDecision(False, "uncertainty_not_disclosed")
        return ReplyDecision(True)

    def request_regeneration(self) -> bool:
        if self.regenerations >= 1:
            return False
        self.regenerations += 1
        return True
