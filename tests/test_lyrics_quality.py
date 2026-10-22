"""Model-free checks hold recognizable ASR artifacts without editing lyrics."""
from copy import deepcopy
import unittest

from gnb_player.extraction_backend.lyrics_quality import assess


def timed_draft(lines, *, duration=None):
    segments = []
    cursor = 0.0
    for text in lines:
        words = []
        for token in text.split():
            words.append({"text": token, "start": cursor, "end": cursor + .4, "score": .99})
            cursor += .5
        segments.append({"text": text, "start": words[0]["start"],
                         "end": words[-1]["end"], "words": words})
        cursor += .5
    return {"duration": duration or max(10, cursor + 1), "segments": segments,
            "warnings": [], "metrics": {"recognition_confidence": .99,
                                         "low_confidence_fraction": 0}}


class LyricsQualityTests(unittest.TestCase):
    def test_artifacts_are_held_even_with_high_confidence_and_valid_timing(self):
        for artifact in [
            "Thank you for listening.",
            "THANK YOU FOR LISTENING!",
            "Thanks for listening…",
            "© transcript Emily Beynon",
            "Copyright 2026 transcription by Emily Beynon",
            "(c) Transcript Emily Beynon",
            "Transcript by Emily Beynon",
            "Transcription by Emily Beynon",
            "Transcribed by Emily Beynon",
            "Thank you for watching.",
            "Subscribe to my channel",
            "Subtitles by Emily Beynon",
            "Amara.org",
        ]:
            with self.subTest(artifact=artifact):
                draft = timed_draft([artifact, "The evening carries every dream back home"])
                original = deepcopy(draft)

                quality = assess(draft)

                self.assertEqual(quality["status"], "review")
                self.assertEqual(quality["reasons"], [{
                    "code": "hallucination",
                    "message": "A passage resembles a common transcription artifact.", "at": 0.0}])
                self.assertEqual(draft, original)

    def test_reported_instrumental_artifact_cannot_pass_with_high_recognizer_scores(self):
        # Three "Thank you" cues evade the existing four-cue repetition threshold;
        # this duration also clears sparsity. Credits/sign-offs must still hold it.
        draft = timed_draft(["um", "Thank you", "Thank you", "Thank you",
                             "© transcript Emily Beynon", "Thank you for listening."],
                            duration=45)
        original = deepcopy(draft)

        quality = assess(draft)

        self.assertEqual(quality["status"], "review")
        self.assertEqual([reason["code"] for reason in quality["reasons"]], ["hallucination"])
        self.assertEqual(quality["reasons"][0]["at"], draft["segments"][4]["start"])
        self.assertEqual(draft, original)

    def test_legitimate_thanks_listening_and_transcript_lyrics_remain_usable(self):
        draft = timed_draft([
            "Um I am finding a way to say thank you",
            "Thank you for the light you bring",
            "Thank you for listening to my heartbeat",
            "The transcript of our love is written in the stars",
            "Copyright cannot own the melody inside me",
        ])
        original = deepcopy(draft)

        self.assertEqual(assess(draft)["status"], "pass")
        self.assertEqual(draft, original)

    def test_existing_repetition_gate_still_holds_repeated_bare_thanks(self):
        draft = timed_draft(["Thank you"] * 4)

        quality = assess(draft)

        self.assertEqual(quality["status"], "review")
        self.assertEqual([reason["code"] for reason in quality["reasons"]], ["repetition"])

    def test_sparse_high_confidence_words_are_held_without_declaring_instrumental(self):
        draft = timed_draft(["um", "Thank you"], duration=180)

        quality = assess(draft)

        self.assertEqual(quality["status"], "review")
        self.assertEqual([reason["code"] for reason in quality["reasons"]], ["sparse"])

    def test_legitimate_text_still_requires_recognition_confidence(self):
        draft = timed_draft(["Thank you for the light you bring"])
        draft["metrics"]["recognition_confidence"] = .4

        quality = assess(draft)

        self.assertEqual(quality["status"], "review")
        self.assertEqual([reason["code"] for reason in quality["reasons"]], ["confidence"])


if __name__ == "__main__":
    unittest.main()
