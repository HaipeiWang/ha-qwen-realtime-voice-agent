"""One short-lived missing-target intent; never an execution or old turn ledger."""

import re
import time

from app.core.arbitration import normal


class PendingClarification:
    def __init__(self):
        self.clear()

    def clear(self):
        self.action = ""
        self.expires = 0.0

    def offer(self, text, reply, arbiter, seconds):
        self.clear()
        match = re.fullmatch(r"(打开|开启|关闭|关掉)(?:所有)?灯[。！？?！\s]*", text)
        if not match or not re.search(r"哪|请.*(?:指定|告诉|说明)|明确.*灯", reply):
            return
        tool = "HassTurnOn" if match[1] in ("打开", "开启") else "HassTurnOff"
        decision = arbiter.decide(text, tool, {})
        if decision.reason == "target_requires_clarification":
            self.action = match[1]
            self.expires = time.monotonic() + seconds

    def consume(self, text, arbiter, *, input_started_at=None):
        action, expires = self.action, self.expires
        self.clear()
        received_at = time.monotonic() if input_started_at is None else input_started_at
        if not action or received_at > expires:
            return text
        # Only a bare, uniquely named target may complete the missing slot.
        target_text = normal(text).strip("。！？?!，, ")
        matches = [e for e in arbiter.catalog.entities if target_text in
                   {normal(n) for n in (e.name, *e.aliases)} |
                   {re.sub(r"吸顶|吊|台|落地", "", normal(e.name))}]
        if len(matches) != 1:
            return text
        return action + matches[0].name
