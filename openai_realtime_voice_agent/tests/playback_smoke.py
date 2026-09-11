import unittest
from dataclasses import replace
from app.core.events import AudioChunk, EventScope, INPUT_FORMAT
from app.core.playback import ResponsePlayback
from app.core.confirmation import ReplyDecision


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.scope = EventScope(1, "c", "t", "r")

    def test_cloud_done_does_not_open_followup(self):
        playback = ResponsePlayback(self.scope)
        playback.append(self.scope, AudioChunk(b"\0\0" * 240))
        playback.generated = True
        self.assertFalse(playback.may_follow_up)
        self.assertFalse(playback.mark_drained(self.scope))
        self.assertEqual(len(playback.release()), 1)
        self.assertTrue(playback.mark_drained(self.scope))
        self.assertTrue(playback.may_follow_up)

    def test_no_cross_response_audio_or_drain(self):
        playback = ResponsePlayback(self.scope)
        other = replace(self.scope, response_id="old")
        self.assertFalse(playback.append(other, AudioChunk(b"\0\0")))
        playback.generated = True
        self.assertFalse(playback.mark_drained(other))

    def test_guarded_audio_requires_review(self):
        playback = ResponsePlayback(self.scope, guarded=True)
        playback.append(self.scope, AudioChunk(b"\0\0"))
        self.assertEqual(playback.release(), ())
        playback.generated = True
        self.assertEqual(playback.release(), ())
        playback.review_completed(ReplyDecision(True))
        self.assertEqual(len(playback.release()), 1)

    def test_failed_review_discards_audio(self):
        playback = ResponsePlayback(self.scope, guarded=True)
        playback.append(self.scope, AudioChunk(b"\0\0"))
        playback.generated = True
        playback.review_completed(ReplyDecision(False, "false_success"))
        self.assertEqual(playback.release(), ())

    def test_guarded_audio_is_bounded(self):
        playback = ResponsePlayback(self.scope, guarded=True)
        self.assertFalse(playback.append(self.scope, AudioChunk(b"\0\0" * (24000 * 8 + 1))))
        self.assertTrue(playback.discarded)
        self.assertEqual(playback.chunks, [])

    def test_input_rate_cannot_be_played_as_output(self):
        with self.assertRaises(ValueError):
            ResponsePlayback(self.scope).append(self.scope, AudioChunk(b"\0\0", INPUT_FORMAT))
