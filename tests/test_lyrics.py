from pathlib import Path
import json
import math
import tempfile
import unittest
from gnb_player.lyrics import (Word, Cue, LyricsDocument, load_lyrics, save_lyrics,
                               parse_lrc, parse_subtitles, find_lyrics, fingerprint,
                               cached_lyrics_path)


def example():
    return LyricsDocument([Cue("Hello blues", 1, 3, [Word("Hello", 1, 2), Word("blues", 2, 3)]),
                           Cue("The train", 5, 7)], 9, "Fixture")


class LyricsTests(unittest.TestCase):
    def test_real_word_intervals_and_silent_gaps(self):
        doc = example()
        self.assertIsNone(doc.active_cue(0))
        self.assertEqual(doc.active_cue(1).text, "Hello blues")
        self.assertEqual(doc.active_word(1.5).text, "Hello")
        self.assertEqual(doc.active_word(2).text, "blues")
        self.assertIsNone(doc.active_cue(3))
        self.assertIsNone(doc.active_word(5.5), "Line-only subtitles must not invent word timing")
        self.assertIsNone(doc.active_cue(float("nan")))

    def test_site_schema_roundtrip_retains_text_times_and_quality(self):
        raw = {"title": "Blue Moon", "duration": 10, "source_sha256": "a" * 64,
               "quality": {"status": "review", "reasons": [{"code": "gap"}]},
               "segments": [{"start": 1, "end": 3, "text": "Whiskey, old friend.",
                             "words": [{"text": "Whiskey,", "start": 1, "end": 2, "score": .8},
                                       {"text": "old", "start": 2, "end": 2.5},
                                       {"text": "friend.", "start": 2.5, "end": 3}]}]}
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "song.json"
            doc = LyricsDocument.from_dict(raw)
            save_lyrics(doc, p)
            restored = load_lyrics(p)
            self.assertEqual(doc, restored)
            self.assertEqual(restored.segments[0].text, "Whiskey, old friend.")
            self.assertEqual(restored.quality["status"], "review")

    def test_invalid_word_timing_retains_line_and_warns(self):
        doc = LyricsDocument.from_dict({"duration": 10, "segments": [{"start": 1, "end": 3,
              "text": "Keep the words", "words": [{"text": "Keep", "start": 1, "end": 1}]}]})
        self.assertEqual(doc.segments[0].text, "Keep the words")
        self.assertFalse(doc.segments[0].words)
        self.assertTrue(doc.warnings)

    def test_invalid_line_or_nonfinite_fingerprint_is_rejected(self):
        for start, end in [(-1, 3), (1, 1), (float("nan"), 3), (1, float("inf"))]:
            with self.assertRaises(ValueError):
                LyricsDocument.from_dict({"segments": [{"start": start, "end": end, "text": "Invalid"}]})
        with self.assertRaises(ValueError):
            LyricsDocument.from_dict({"segments": [], "source_sha256": "wrong"})

    def test_lrc_duplicate_timestamps_and_offset_are_line_only(self):
        doc = parse_lrc("[ti:Night Train]\n[offset:200]\n[00:01.00][00:10.00]One more train\n[00:14.00]Last call", 20)
        self.assertEqual(doc.title, "Night Train")
        self.assertEqual([c.start for c in doc.segments], [1.2, 10.2, 14.2])
        self.assertEqual(doc.segments[0].end, 10.2)
        self.assertFalse(any(c.words for c in doc.segments))
        self.assertEqual(doc.segments[-1].end, 20)

    def test_srt_and_vtt_preserve_multiline_captions(self):
        text = "WEBVTT\n\n00:01.000 --> 00:03.500 align:middle\n<i>Night</i> train\nTake me home\n\n2\n00:04,000 --> 00:06,000\nLast call\n"
        doc = parse_subtitles(text, 10)
        self.assertEqual(len(doc.segments), 2)
        self.assertEqual(doc.segments[0].text, "Night train\nTake me home")
        self.assertEqual(doc.active_cue(4.5).text, "Last call")
        self.assertIsNone(doc.active_word(4.5))

    def test_sidecars_and_cache_match_recording_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);source = root / "song.mp3";source.write_bytes(b"recording")
            cache = root / "cache";doc = example();doc.source_sha256 = fingerprint(source)
            cached = cached_lyrics_path(source, cache);save_lyrics(doc, cached)
            self.assertEqual(find_lyrics(source, cache), cached)
            source.write_bytes(b"different recording")
            self.assertIsNone(find_lyrics(source, cache))
            sidecar = source.with_suffix(".lrc");sidecar.write_text("[00:01]My imported line")
            self.assertEqual(find_lyrics(source, cache), sidecar)

    def test_published_import_requires_exact_hash_and_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);source = root / "song.mp3";source.write_bytes(b"recording");published = root / "published";published.mkdir()
            raw = example().to_dict();raw["source_sha256"] = fingerprint(source)
            path = published / "station-id.json";path.write_text(json.dumps(raw))
            self.assertIsNone(find_lyrics(source, root / "cache", published))
            raw.update(quality={"status": "pass"}, publication={"method": "automatic"});path.write_text(json.dumps(raw))
            self.assertEqual(find_lyrics(source, root / "cache", published), path)
            raw["source_sha256"] = "b" * 64;path.write_text(json.dumps(raw))
            self.assertIsNone(find_lyrics(source, root / "cache", published))

    def test_atomic_save_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);target = root / "original";target.write_text("Keep me")
            link = root / "link.json"
            try:link.symlink_to(target)
            except OSError:self.skipTest("Symlinks unavailable")
            with self.assertRaises(ValueError):save_lyrics(example(), link)
            self.assertEqual(target.read_text(), "Keep me")


if __name__ == "__main__":
    unittest.main()
