import unittest
from dataclasses import replace
from app.core.events import CanonicalEvent, EventKind
from app.core.lifecycle import ConversationLifecycle
from app.providers.base import RealtimeProvider
from fake_provider import FakeProvider


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_fake_provider_drives_canonical_events(self):
        provider = FakeProvider()
        self.assertIsInstance(provider, RealtimeProvider)
        core = ConversationLifecycle()
        turn = core.wake()
        await provider.connect(turn.scope)
        provider.begin_turn(turn.scope)
        scope = replace(turn.scope, response_id="r")
        await provider.events.put(CanonicalEvent(EventKind.RESPONSE_STARTED, scope))
        await provider.events.put(CanonicalEvent(EventKind.RESPONSE_ENDED, scope))
        await provider.disconnect()
        async for event in provider.receive_events():
            self.assertTrue(core.observe(event))
        self.assertFalse(core.microphone_opened())
        self.assertTrue(core.output_drained(scope))
        self.assertTrue(core.microphone_opened())
        self.assertIsNotNone(core.follow_up())

    async def test_all_cancelled_event_types_rejected_until_wake(self):
        core = ConversationLifecycle()
        old = core.wake()
        scope = replace(old.scope, response_id="r")
        core.observe(CanonicalEvent(EventKind.RESPONSE_STARTED, scope))
        core.cancel()
        for kind in (EventKind.SPEECH_STARTED, EventKind.TRANSCRIPT_FINAL, EventKind.AUDIO,
                     EventKind.TOOL_CALL, EventKind.RESPONSE_STARTED, EventKind.RESPONSE_ENDED):
            self.assertFalse(core.observe(CanonicalEvent(kind, scope)))
        self.assertIsNone(core.follow_up())
        fresh = core.wake()
        self.assertFalse(core.observe(CanonicalEvent(EventKind.AUDIO, scope)))
        self.assertTrue(core.observe(CanonicalEvent(EventKind.SPEECH_STARTED, replace(fresh.scope, item_id="new"))))

    async def test_followup_deadline_starts_at_microphone_reopen(self):
        now = [0.0]
        core = ConversationLifecycle(clock=lambda: now[0])
        scope = replace(core.wake().scope, response_id="r")
        core.observe(CanonicalEvent(EventKind.RESPONSE_STARTED, scope))
        core.observe(CanonicalEvent(EventKind.RESPONSE_ENDED, scope))
        now[0] = 20
        core.output_drained(scope)
        self.assertIsNone(core.follow_up_deadline)
        now[0] = 22
        core.microphone_opened()
        self.assertEqual(core.follow_up_deadline, 30)
        now[0] = 30
        self.assertIsNone(core.follow_up())

    async def test_recovery_invalidates_old_conversation(self):
        core = ConversationLifecycle()
        old = core.wake()
        core.recover()
        core.wake()
        self.assertFalse(core.observe(CanonicalEvent(EventKind.SPEECH_STARTED, old.scope)))
