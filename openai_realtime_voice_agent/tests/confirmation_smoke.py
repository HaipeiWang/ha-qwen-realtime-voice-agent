import unittest

from app.core.confirmation import confirmation_instruction, ConfirmationReview
from app.tools.registry import normalize_result


class ConfirmationTests(unittest.TestCase):
    def test_empty_result_is_not_success_or_definite_failure(self):
        note = confirmation_instruction("关灯", normalize_result({}, "e"))
        self.assertIn("执行结果未知", note)
        self.assertNotIn("操作完成", note)

    def test_partial_retains_target_evidence(self):
        outcome = normalize_result({"status": "partial", "successful_targets": ["light.a"],
                                    "failed_targets": ["light.b"]}, "e")
        note = confirmation_instruction("关闭两盏灯", outcome)
        self.assertIn("只完成部分", note)
        self.assertIn("light.a", note)
        self.assertIn("light.b", note)

    def test_accepted_does_not_claim_failure(self):
        note = confirmation_instruction("关灯", normalize_result({"status": "accepted"}, "e"))
        self.assertIn("尚未验证最终状态", note)
        self.assertIn("不得声称失败", note)

    def test_control_reply_budget_and_no_reexecution(self):
        note = confirmation_instruction("关灯", normalize_result({"success": False}, "e"))
        self.assertIn("最多2句、80字", note)
        self.assertIn("不重复执行", note)

    def test_no_tool_or_missing_action_blocks_success(self):
        review = ConfirmationReview()
        self.assertFalse(review.review("灯已经关闭了。", 1, [], action_coverage_complete=False).allowed)

    def test_unknown_and_failed_cannot_confirm_success(self):
        review = ConfirmationReview()
        for status in ("unknown", "failed", "accepted"):
            outcome = normalize_result({"status": status}, "e")
            self.assertFalse(review.review("已经完成了。", 1, [outcome], action_coverage_complete=True).allowed)

    def test_one_regeneration_only(self):
        review = ConfirmationReview()
        self.assertTrue(review.request_regeneration())
        self.assertFalse(review.request_regeneration())

    def test_natural_uncertainty_allowed(self):
        review = ConfirmationReview()
        outcome = normalize_result({}, "e")
        self.assertTrue(review.review("暂时无法确认灯的状态。", 2, [outcome], action_coverage_complete=True).allowed)
