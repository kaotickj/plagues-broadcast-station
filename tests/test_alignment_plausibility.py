"""Model-free guards against forcing wrong recognized text across a recording."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gnb_player.extraction_backend import lyrics_acceleration as acceleration


def word(text, start, end, score=.8):
    return {"text": text, "start": start, "end": end, "score": score}


class AlignmentPlausibilityTests(unittest.TestCase):
    def setUp(self):
        self.original = [word("The", 1, 1.4, .99), word("rain", 1.5, 2, .99)]
        self.aligned = [word("The", 1.1, 1.5), word("rain", 1.6, 2.2)]

    def assert_rejected(self, aligned, reason):
        original = deepcopy(self.original)
        candidate = deepcopy(aligned)
        with self.assertRaisesRegex(acceleration.AlignmentRejected, reason) as caught:
            acceleration.validate_alignment(self.original, aligned, 221.283)
        self.assertIs(type(caught.exception), acceleration.AlignmentRejected)
        self.assertEqual(self.original, original)
        self.assertEqual(aligned, candidate)

    def test_alignment_rejection_is_a_distinct_actionable_value_error(self):
        self.assertTrue(issubclass(acceleration.AlignmentRejected, ValueError))
        self.assertIsNot(acceleration.AlignmentRejected, ValueError)
        with self.assertRaises(ValueError) as caught:
            acceleration.validate_alignment(self.original, self.aligned[:1], 221.283)
        self.assertIs(type(caught.exception), acceleration.AlignmentRejected)
        self.assertIn("No replacement lyrics were saved", str(caught.exception))
        self.assertIn("align a known lyric sheet", str(caught.exception))

    def test_observed_94_second_word_is_rejected(self):
        self.original[0]["text"] = "transcript"
        self.aligned[0] = word("transcript", 2.642, 96.863, .99)
        self.aligned[1] = word("rain", 97, 97.5, .99)
        self.assert_rejected(self.aligned, "stretched over more than 12 seconds")

    def test_sustained_word_and_long_instrumental_pause_are_allowed(self):
        aligned = [word("The", 1, 10.5), word("rain", 180, 181)]
        before = deepcopy(aligned)
        acceleration.validate_alignment(self.original, aligned, 221.283)
        self.assertEqual(aligned, before)

    def test_changed_missing_or_extra_words_are_rejected(self):
        for changed in [self.aligned[:1], self.aligned + [word("again", 3, 4)],
                        [{**self.aligned[0], "text": "A"}, self.aligned[1]],
                        [{k: v for k, v in self.aligned[0].items() if k != "text"}, self.aligned[1]]]:
            with self.subTest(changed=changed):
                self.assert_rejected(changed, "coverage changed|text changed")

    def test_nonfinite_missing_and_out_of_range_timing_are_rejected(self):
        for change in [{"start": float("nan")}, {"end": float("inf")},
                       {"start": None}, {"start": True}, {"start": -1},
                       {"end": 222}, {"start": 1.5, "end": 1.5},
                       {"start": 2, "end": 1}]:
            aligned = [{**self.aligned[0], **change}, self.aligned[1]]
            with self.subTest(change=change):
                self.assert_rejected(aligned, "timing")

    def test_backward_timing_is_rejected(self):
        self.assert_rejected([word("The", 2, 3), word("rain", 1.5, 2)], "backward")

    def test_small_rounding_overlap_is_allowed(self):
        acceleration.validate_alignment(self.original,
                                        [word("The", 1, 1.5), word("rain", 1.46, 2)], 3)

    def test_high_asr_confidence_cannot_replace_missing_ctc_confidence(self):
        for score in [None, float("nan"), float("inf"), -.1, 1.1, True]:
            with self.subTest(score=score):
                self.assert_rejected([{**self.aligned[0], "score": score}, self.aligned[1]],
                                     "CTC confidence is missing or invalid")
        no_score = {key: value for key, value in self.aligned[0].items() if key != "score"}
        self.assert_rejected([no_score, self.aligned[1]], "CTC confidence is missing or invalid")

    def test_broadly_low_ctc_confidence_is_rejected(self):
        self.assert_rejected([word("The", 1, 1.4, .1), word("rain", 1.5, 2, .1)],
                             "confidence is broadly too low")

    def test_majority_weak_ctc_is_rejected_even_if_average_is_higher(self):
        self.original = [word("The", 1, 1.4), word("rain", 1.5, 2), word("falls", 2.1, 2.5)]
        self.assert_rejected([word("The", 1, 1.4, .05), word("rain", 1.5, 2, .05),
                              word("falls", 2.1, 2.5, .99)], "confidence is broadly too low")

    def test_isolated_weak_word_is_retained_for_separate_review(self):
        acceleration.validate_alignment(self.original,
                                        [word("The", 1, 1.4, .05), word("rain", 1.5, 2, .9)], 3)

    def test_invalid_duration_is_rejected(self):
        for duration in [None, float("nan"), float("inf"), 0, -1, True]:
            with self.subTest(duration=duration), self.assertRaisesRegex(acceleration.AlignmentRejected, "duration is invalid"):
                acceleration.validate_alignment(self.original, self.aligned, duration)

    def run_realign(self, aligned, *, gpu=False, return_backend=False):
        fake_soundfile = SimpleNamespace(write=lambda *args, **kwargs: None)
        fake_pipeline = SimpleNamespace(align_reference=lambda *args: deepcopy(aligned))

        def helper(operation, source, target, work, progress):
            target.write_text(json.dumps(aligned))

        with tempfile.TemporaryDirectory() as folder:
            with patch.dict("sys.modules", {"soundfile": fake_soundfile, "lyrics_pipeline": fake_pipeline}), \
                    patch.object(acceleration, "enabled", return_value=gpu), \
                    patch.object(acceleration, "helper", side_effect=helper):
                return acceleration.realign([0] * 1000, 100, self.original, Path(folder),
                                            lambda *args: None, return_backend=return_backend)

    def test_cpu_realign_keeps_asr_and_ctc_confidence_separate_without_mutation(self):
        original = deepcopy(self.original)
        result = self.run_realign(self.aligned)
        self.assertEqual(result[0]["score"], .99)
        self.assertEqual(result[0]["alignment_score"], .8)
        self.assertEqual((result[0]["start"], result[0]["end"]), (1.1, 1.5))
        self.assertEqual(self.original, original)
        self.assertIsNot(result[0], self.original[0])

    def test_return_backend_compatibility_for_cpu_and_gpu(self):
        for gpu, expected in [(False, "cpu"), (True, "radeon-rocm")]:
            with self.subTest(gpu=gpu):
                result, backend = self.run_realign(self.aligned, gpu=gpu, return_backend=True)
                self.assertEqual(backend, expected)
                self.assertEqual(len(result), len(self.original))

    def test_realign_rejection_keeps_original_words_unchanged(self):
        original = deepcopy(self.original)
        for gpu in (False, True):
            with self.subTest(gpu=gpu), self.assertRaisesRegex(acceleration.AlignmentRejected, "confidence is broadly too low"):
                self.run_realign([word("The", 1, 1.4, .01), word("rain", 1.5, 2, .01)], gpu=gpu)
        self.assertEqual(self.original, original)


if __name__ == "__main__":
    unittest.main()
