"""Recording/revision-bound human review, lossless metadata and atomic saves."""
from copy import deepcopy
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gnb_player import extract_worker
from gnb_player.lyrics import LyricsDocument, fingerprint, load_lyrics, lyric_write_lock, save_lyrics
from gnb_player import lyric_review
from gnb_player.lyric_review import (approve_document, document_revision, edit_document,
                                     invalidate_review, is_approved, retarget_review, save_review_document)


class LyricReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lyric review paths with spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "Recording.wav"
        self.source.write_bytes(b"disposable representative recording")
        self.target = self.root / "lyrics/recording.json"
        self.target.parent.mkdir()
        self.raw = {"version": "broadcast-player-lyrics-1", "title": "Review fixture", "duration": 10,
                    "source_sha256": fingerprint(self.source), "warnings": ["Listen to the held phrase."],
                    "quality": {"status": "pass", "checked_at": 1234, "reasons": [{"code": "original", "message": "Original machine finding"}], "metrics": {"confidence": .93}},
                    "pipeline": {"recognizer": "local fixture", "private_evidence": {"id": 7}},
                    "machine_review": {"held_passages": [{"at": 1, "reason": "fixture"}]},
                    "unknown_model_field": ["preserve", {"nested": True}],
                    "segments": [{"start": 1, "end": 3, "text": "First words", "needs_review": True, "model_line": 12,
                                  "words": [{"start": 1, "end": 2, "text": "First", "score": .91, "speaker": "singer"},
                                            {"start": 2, "end": 3, "text": "words", "score": .95}]},
                                 {"start": 4, "end": 6, "text": "Second line", "model_line": 13,
                                  "words": [{"start": 4, "end": 5, "text": "Second", "score": .97},
                                            {"start": 5, "end": 6, "text": "line", "score": .92}]},
                                 {"start": 7, "end": 9, "text": "Last line", "model_line": 14, "words": []}]}
        self.original_bytes = (json.dumps(self.raw, indent=3) + "\n").encode()
        self.target.write_bytes(self.original_bytes)
        self.document = load_lyrics(self.target)

    def rows(self, document=None):
        return [(cue.start, cue.end, cue.text) for cue in (document or self.document).segments]

    def save(self, document, *, approve=False, expected=None):
        return save_review_document(document, self.target, self.source,
                                    document_revision(self.document) if expected is None else expected,
                                    approve=approve)

    def test_unknown_top_line_word_metadata_roundtrip_is_deep_and_lossless(self):
        data = self.document.to_dict()
        for name in ("pipeline", "machine_review", "unknown_model_field", "quality", "warnings"):
            self.assertEqual(data[name], self.raw[name])
        self.assertEqual(data["segments"], self.raw["segments"])
        data["pipeline"]["private_evidence"]["id"] = 99
        self.assertEqual(self.document.metadata["pipeline"]["private_evidence"]["id"], 7)
        self.assertEqual(self.document.segments[0].words[0].metadata["speaker"], "singer")

    def test_machine_qc_pass_alone_is_not_manual_approval(self):
        self.assertFalse(is_approved(self.document))
        self.document.metadata["reviewed"] = True
        self.document.metadata["publication"] = {"method": "automatic"}
        self.assertFalse(is_approved(self.document))

    def test_approval_survives_atomic_roundtrip_restart_and_self_audit(self):
        saved = self.save(self.document, approve=True)
        restored = load_lyrics(self.target)
        self.assertTrue(is_approved(saved, fingerprint(self.source)))
        self.assertTrue(is_approved(restored, fingerprint(self.source)))
        self.assertEqual(document_revision(saved), document_revision(restored))
        self.assertEqual(restored.human_review["document_revision"], document_revision(restored))
        self.assertEqual(restored.human_review["method"], "human")
        self.assertEqual(restored.quality["status"], "review", "Older app versions must keep this revision off-air.")
        self.assertEqual(restored.metadata["review_original_quality"], self.raw["quality"])
        self.assertEqual(restored.warnings, self.raw["warnings"])
        restored.human_review["reviewed_at"] += 1
        self.assertTrue(is_approved(restored), "Audit fields must not create a self-referential revision.")

    def test_any_payload_change_or_wrong_recording_invalidates_approval(self):
        approved = approve_document(self.document, fingerprint(self.source))
        for mutate in (lambda doc: setattr(doc.segments[0], "text", "changed"),
                       lambda doc: setattr(doc.segments[0], "start", .5),
                       lambda doc: doc.metadata["pipeline"].update(recognizer="changed"),
                       lambda doc: doc.quality.update(status="pass"),
                       lambda doc: setattr(doc, "source_sha256", "b" * 64)):
            with self.subTest(mutate=mutate):
                changed = deepcopy(approved)
                mutate(changed)
                self.assertFalse(is_approved(changed))
        self.assertFalse(is_approved(approved, "b" * 64))
        with self.assertRaises(ValueError):
            approve_document(self.document, "b" * 64)

    def test_changed_line_loses_only_its_word_timing_and_retains_qc(self):
        rows = self.rows()
        rows[0] = (1, 3, "Corrected words")
        edited = edit_document(self.document, rows)
        self.assertEqual(edited.segments[0].words, [])
        self.assertEqual((edited.segments[0].start, edited.segments[0].end), (1, 3))
        self.assertEqual(edited.segments[0].metadata, self.document.segments[0].metadata)
        self.assertEqual(edited.segments[1], self.document.segments[1])
        self.assertEqual(edited.human_review["alignment_needed"], [0])
        self.assertEqual(edited.human_review["status"], "draft")
        self.assertEqual(edited.metadata["review_original_quality"], self.raw["quality"])
        self.assertEqual(edited.quality["reasons"][0], self.raw["quality"]["reasons"][0])
        self.assertEqual(self.document.segments[0].text, "First words")
        self.assertTrue(self.document.segments[0].words)

    def test_removing_a_line_keeps_later_exact_word_timings_and_metadata(self):
        edited = edit_document(self.document, self.rows()[1:])
        self.assertEqual(edited.segments, self.document.segments[1:])
        self.assertNotIn("alignment_needed", edited.human_review)
        rows = self.rows()[1:]
        rows[1] = (7, 9, "Changed last line")
        edited = edit_document(self.document, rows)
        self.assertEqual(edited.segments[0], self.document.segments[1])
        self.assertEqual(edited.segments[1].metadata["model_line"], 14)
        self.assertEqual(edited.human_review["alignment_needed"], [1])

    def test_time_change_clears_changed_words_and_long_warning_is_retained(self):
        self.document.warnings.append("full QC detail " * 200)
        rows = self.rows()
        rows[0] = (.5, 3, rows[0][2])
        edited = edit_document(self.document, rows)
        self.assertFalse(edited.segments[0].words)
        self.assertEqual(edited.segments[1].words, self.document.segments[1].words)
        restored = LyricsDocument.from_dict(edited.to_dict())
        self.assertEqual(restored.warnings, self.document.warnings)

    def test_invalid_rows_and_word_intervals_cannot_be_approved(self):
        for rows in ([(1, 1, "bad")], [(-1, 2, "bad")], [(1, 11, "bad")], [(float("nan"), 2, "bad")],
                     [(1, float("inf"), "bad")], [(True, 2, "bad")], [(1, 2, " ")], [(1, 4, "one"), (3, 5, "two")],
                     [(4, 5, "later"), (1, 2, "earlier")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                edit_document(self.document, rows)
        bad = deepcopy(self.document)
        bad.segments[0].end = 1.5
        with self.assertRaises(ValueError):
            approve_document(bad, fingerprint(self.source))

    def test_intentionally_empty_draft_can_save_with_history_but_cannot_be_approved(self):
        empty = edit_document(self.document, [])
        saved = self.save(empty, approve=False)
        restored = load_lyrics(self.target)
        self.assertEqual(saved.segments, [])
        self.assertEqual(restored.human_review["status"], "draft")
        self.assertFalse(is_approved(restored))
        self.assertEqual(restored.metadata["review_original_quality"], self.raw["quality"])
        self.assertIn(self.original_bytes, [p.read_bytes() for p in (self.target.parent / "history").glob("*.json")])
        before = self.target.read_bytes()
        with self.assertRaises(ValueError):
            approve_document(saved, fingerprint(self.source))
        with self.assertRaises(ValueError):
            save_review_document(saved, self.target, self.source, document_revision(saved), approve=True)
        self.assertEqual(self.target.read_bytes(), before)

    def test_mismatched_word_labels_require_line_timing_and_alignment_flags_are_bounded(self):
        bad = deepcopy(self.document)
        bad.segments[0].text = "Different words than the timed labels"
        with self.assertRaisesRegex(ValueError, "word labels do not match"):
            approve_document(bad, fingerprint(self.source))
        bad.segments[0].words = []
        bad.human_review = {"status": "draft", "alignment_needed": [0, 0, -1, 99, True, "1", None, {}, 1]}
        approved = approve_document(bad, fingerprint(self.source))
        self.assertTrue(is_approved(approved))
        self.assertEqual(approved.human_review["alignment_needed"], [0, 1])
        self.assertEqual(invalidate_review(approved).human_review["alignment_needed"], [0, 1])
        punctuated = deepcopy(self.document)
        punctuated.segments[0].text = "FIRST, words!"
        self.assertTrue(is_approved(approve_document(punctuated, fingerprint(self.source))))

    def test_held_draft_can_keep_uncertain_word_evidence_until_explicit_line_timing(self):
        held = deepcopy(self.raw)
        held["segments"][0]["words"][0]["text"] = "Mismatched"
        self.target.write_text(json.dumps(held))
        original = load_lyrics(self.target)
        draft = edit_document(original, self.rows(original))
        self.assertEqual(draft.segments[0].words, original.segments[0].words)
        saved = save_review_document(draft, self.target, self.source, document_revision(original), approve=False)
        self.assertFalse(is_approved(saved))
        with self.assertRaisesRegex(ValueError, "word labels do not match"):
            save_review_document(saved, self.target, self.source, document_revision(saved), approve=True)
        line_timed = deepcopy(saved)
        line_timed.segments[0].words = []
        approved = save_review_document(line_timed, self.target, self.source, document_revision(saved), approve=True)
        self.assertTrue(is_approved(approved))

    def test_draft_save_retains_first_original_qc_and_never_reapproves(self):
        approved = self.save(self.document, approve=True)
        rows = self.rows(approved)
        rows[0] = (1, 3, "Human correction")
        edited = edit_document(approved, rows)
        saved = save_review_document(edited, self.target, self.source, document_revision(approved))
        self.assertFalse(is_approved(saved))
        self.assertEqual(saved.human_review["status"], "draft")
        self.assertEqual(saved.metadata["review_original_quality"], self.raw["quality"])
        self.assertEqual(saved.human_review["alignment_needed"], [0])
        self.assertEqual(load_lyrics(self.target).metadata["review_original_quality"], self.raw["quality"])

    def test_raw_prior_json_history_is_exact_and_immutable_across_saves(self):
        approved = self.save(self.document, approve=True)
        first = sorted((self.target.parent / "history").glob("*.json"))
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].read_bytes(), self.original_bytes)
        approved_bytes = self.target.read_bytes()
        save_review_document(approved, self.target, self.source, document_revision(approved), approve=False)
        history = list((self.target.parent / "history").glob("*.json"))
        self.assertEqual(len(history), 2)
        self.assertEqual(first[0].read_bytes(), self.original_bytes)
        self.assertIn(approved_bytes, [path.read_bytes() for path in history])

    def test_conflicting_payload_or_review_only_update_rejects_stale_approval(self):
        base = self.document
        changed = deepcopy(self.raw)
        changed["segments"][0]["text"] = "A concurrent change"
        self.target.write_text(json.dumps(changed))
        current = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed"):
            self.save(base, approve=True)
        self.assertEqual(self.target.read_bytes(), current)
        self.target.write_bytes(self.original_bytes)
        approved = self.save(base, approve=True)
        stale = load_lyrics(self.target)
        save_review_document(approved, self.target, self.source, document_revision(approved), approve=False)
        self.assertEqual(document_revision(stale), document_revision(load_lyrics(self.target)))
        with self.assertRaisesRegex(ValueError, "review/approval state changed"):
            save_review_document(stale, self.target, self.source, document_revision(stale), approve=True)
        self.assertFalse(is_approved(load_lyrics(self.target)))

    def test_source_changed_wrong_target_symlink_and_history_failure_preserve_existing(self):
        self.source.write_bytes(b"different recording")
        with self.assertRaisesRegex(ValueError, "recording"):
            self.save(self.document, approve=True)
        self.assertEqual(self.target.read_bytes(), self.original_bytes)
        self.source.write_bytes(b"disposable representative recording")
        with self.assertRaises(ValueError):
            save_review_document(self.document, self.source, self.source, None, approve=True)
        history = self.target.parent / "history"
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        try:
            history.symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "history"):
            self.save(self.document, approve=True)
        self.assertEqual(self.target.read_bytes(), self.original_bytes)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_atomic_write_failure_does_not_claim_or_persist_approval(self):
        with patch.object(lyric_review.os, "replace", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.save(self.document, approve=True)
        self.assertEqual(self.target.read_bytes(), self.original_bytes)
        self.assertFalse(is_approved(self.document))
        self.assertFalse(list(self.target.parent.glob(".lyric-review-*")))
        self.assertEqual(next((self.target.parent / "history").glob("*.json")).read_bytes(), self.original_bytes)

    def test_mid_save_recording_or_uncooperative_target_change_is_not_overwritten(self):
        original_history = lyric_review._history
        def change_source(target, original):
            original_history(target, original)
            self.source.write_bytes(b"source changed during save")
        with patch.object(lyric_review, "_history", side_effect=change_source):
            with self.assertRaisesRegex(ValueError, "recording changed while saving"):
                self.save(self.document, approve=True)
        self.assertEqual(self.target.read_bytes(), self.original_bytes)
        self.source.write_bytes(b"disposable representative recording")
        newer = b'{"concurrent":"legacy writer changed target"}'
        def change_target(target, original):
            original_history(target, original)
            target.write_bytes(newer)
        with patch.object(lyric_review, "_history", side_effect=change_target):
            with self.assertRaisesRegex(ValueError, "lyric file changed while saving"):
                self.save(self.document, approve=True)
        self.assertEqual(self.target.read_bytes(), newer)

    def test_new_target_requires_none_expected_revision_and_can_bind_import(self):
        destination = self.root / "new target.json"
        imported = deepcopy(self.document)
        imported.source_sha256 = ""
        saved = save_review_document(imported, destination, self.source, None, approve=True)
        self.assertTrue(is_approved(saved, fingerprint(self.source)))
        with self.assertRaises(ValueError):
            save_review_document(imported, destination, self.source, None, approve=True)

    def test_explicit_import_retargets_against_cache_and_discards_source_approval(self):
        imported = deepcopy(self.document)
        imported.segments[0].text = "A separately imported line"
        imported.segments[0].words = []
        imported = approve_document(imported, fingerprint(self.source))
        candidate = retarget_review(imported, self.document)
        self.assertFalse(is_approved(candidate))
        saved = self.save(candidate)
        self.assertEqual(saved.segments[0].text, "A separately imported line")
        self.assertFalse(is_approved(saved))
        self.assertIn(self.original_bytes, [p.read_bytes() for p in (self.target.parent / "history").glob("*.json")])
        stale_import = retarget_review(imported, saved)
        save_review_document(saved, self.target, self.source, document_revision(saved), approve=True)
        with self.assertRaisesRegex(ValueError, "review/approval state changed"):
            save_review_document(stale_import, self.target, self.source, document_revision(saved))

    def test_target_bound_to_another_recording_cannot_be_replaced(self):
        wrong = deepcopy(self.raw)
        wrong["source_sha256"] = "b" * 64
        self.target.write_text(json.dumps(wrong))
        current = load_lyrics(self.target)
        candidate = retarget_review(self.document, current)
        before = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, "target belongs to another recording"):
            save_review_document(candidate, self.target, self.source, document_revision(current), approve=True)
        self.assertEqual(self.target.read_bytes(), before)

    def test_shared_lock_blocks_import_review_and_extraction_and_worker_is_qt_free(self):
        with lyric_write_lock(self.target):
            for operation in (lambda: self.save(self.document), lambda: save_lyrics(self.document, self.target),
                              lambda: extract_worker.atomic_result(self.target, self.raw)):
                with self.assertRaisesRegex(ValueError, "write is in progress"):
                    operation()
            code = ("import sys;sys.path.insert(0,sys.argv[1]);from lyrics import lyric_write_lock\n"
                    "try:\n with lyric_write_lock(sys.argv[2]):pass\nexcept ValueError:raise SystemExit(77)\n")
            refused = subprocess.run([sys.executable, "-c", code, str(Path(extract_worker.__file__).parent), str(self.target)],
                                     capture_output=True, timeout=20)
            self.assertEqual(refused.returncode, 77, refused.stderr.decode(errors="replace"))
        code = "import sys;sys.path.insert(0,sys.argv[1]);import extract_worker;assert not any(n.startswith('PySide6') for n in sys.modules)"
        subprocess.run([sys.executable, "-c", code, str(Path(extract_worker.__file__).parent)], check=True, capture_output=True, timeout=20)

    def test_reextraction_never_inherits_human_approval_and_old_review_conflicts(self):
        approved = self.save(self.document, approve=True)
        before = self.target.read_bytes()
        output = approved.to_dict()
        extract_worker.atomic_result(self.target, output)
        restored = load_lyrics(self.target)
        self.assertFalse(is_approved(restored))
        self.assertNotIn("human_review", json.loads(self.target.read_text()))
        with self.assertRaisesRegex(ValueError, "review/approval state changed"):
            save_review_document(approved, self.target, self.source, document_revision(approved), approve=True)
        self.assertIn(before, [p.read_bytes() for p in (self.target.parent / "history").glob("*.json")])


if __name__ == "__main__":
    unittest.main()
