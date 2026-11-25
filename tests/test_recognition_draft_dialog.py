"""Disposable untimed recognition editing; no models/audio/output/credentials."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from gnb_player import recognition_drafts as drafts
from gnb_player import recognition_draft_dialog as ui


@pytest.fixture
def desk(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / 'test recording.wav'
    source.write_bytes(b'Disposable recording bytes, not a real audio file')
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    cache = tmp_path / 'private cache'
    failures = cache / 'failures'
    failures.mkdir(parents=True)
    failure = failures / (sha + '-' + '1' * 32 + '.json')
    failure.write_text(json.dumps({'version': 'extraction-failure-1', 'source_sha256': sha,
                                   'status': 'failed', 'recognition_evidence': {}}))
    snapshot = drafts.create_initial_draft(cache, sha, 'Machine words\n原始', failure)
    canonical = cache / 'lyrics' / (sha + '.json')
    canonical.parent.mkdir()
    canonical.write_bytes(b'Preserve these existing human timed lyric bytes and approval')
    original = 'Original raw words\n雨\nUncertain measurement preserved'
    diagnostics = '<literal diagnostics> unaligned raw evidence'
    parent = QWidget()
    parent.show()
    dialogs = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Cancel)
    def make(opened=snapshot, **kwargs):
        dialog = ui.RecognitionDraftDialog(opened, parent, source_path=source, source_sha256=sha,
            recording_title='Disposable recording', original_text=original,
            diagnostics_text=diagnostics, **kwargs)
        dialogs.append(dialog)
        return dialog
    yield {'app': app, 'source': source, 'sha': sha, 'snapshot': snapshot, 'failure': failure,
           'canonical': canonical, 'original': original, 'diagnostics': diagnostics,
           'parent': parent, 'make': make}
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Discard)
    for dialog in dialogs:
        if dialog.busy:
            dialog._worker.wait(5000)
            app.processEvents()
        dialog.close()
    parent.close()
    app.processEvents()


def settle(dialog):
    deadline = time.monotonic() + 5
    while dialog.busy and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    QApplication.processEvents()
    assert not dialog.busy, 'Draft operation did not finish promptly'


def unchanged_files(desk):
    return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (desk['canonical'], desk['failure'])}


def assert_unchanged(files):
    for path, value in files.items():
        assert (path.read_bytes(), path.stat().st_mtime_ns) == value


def test_reading_is_distinct_nonmodal_and_does_not_change_original_evidence(desk):
    before = unchanged_files(desk)
    dialog = desk['make']()
    assert not dialog.isModal()
    assert not dialog.text.isReadOnly() and dialog.original.isReadOnly() and dialog.diagnostics.isReadOnly()
    assert dialog.text.toPlainText() == desk['snapshot'].text
    assert dialog.original.toPlainText() == desk['original']
    assert dialog.diagnostics.toPlainText() == desk['diagnostics']
    assert 'UNTIMED' in dialog.notice.text() and 'approve' in dialog.notice.text()
    assert 'Program Output' in dialog.notice.text()
    assert not dialog.is_dirty and not dialog.save_button.isEnabled()
    assert_unchanged(before)


def test_edit_copy_save_and_reopen_are_untimed_and_preserve_other_files(desk):
    before = unchanged_files(desk)
    dialog = desk['make']()
    edited = 'Human correction: café — 安全\n\nNext line\n'
    dialog.text.setPlainText(edited)
    assert dialog.is_dirty and dialog.save_button.isEnabled()
    assert 'Unsaved' in dialog.status.text()
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == edited
    saved = []
    dialog.saved.connect(saved.append)
    dialog.save_button.click()
    assert dialog.busy and dialog.text.isReadOnly() and not dialog.close_button.isEnabled()
    settle(dialog)
    assert len(saved) == 1 and dialog.text.toPlainText() == edited
    assert not dialog.is_dirty and not dialog.text.isReadOnly()
    assert 'Saved draft text' in dialog.status.text()
    opened = drafts.load_draft(desk['snapshot'].path, expected_source_sha256=desk['sha'])
    assert opened.text == edited and opened.payload['status'] == 'unaligned'
    assert opened.payload['needs_review'] is True and opened.payload['edited_by'] == 'human'
    assert not {'segments', 'approval', 'human_review', 'publication'} & opened.payload.keys()
    assert desk['make'](opened).text.toPlainText() == edited
    assert dialog.original.toPlainText() == desk['original']
    assert dialog.diagnostics.toPlainText() == desk['diagnostics']
    assert_unchanged(before)


def test_repeated_save_does_not_invent_new_revisions(desk):
    dialog = desk['make']()
    dialog.text.setPlainText('One explicit saved correction')
    dialog.save_draft()
    settle(dialog)
    before = (dialog.snapshot.path.read_bytes(), dialog.snapshot.path.stat().st_mtime_ns)
    dialog.save_draft()
    assert not dialog.busy
    assert (dialog.snapshot.path.read_bytes(), dialog.snapshot.path.stat().st_mtime_ns) == before
    assert dialog.snapshot.payload['edit_revision'] == 1


def test_stale_revision_preserves_unsaved_buffer_then_explicit_reload(desk, monkeypatch):
    dialog = desk['make']()
    saved_elsewhere = drafts.save_user_draft(desk['snapshot'].path, 'Other saved human words',
        expected_revision=desk['snapshot'].revision, expected_source_sha256=desk['sha'])
    dialog.text.setPlainText('Keep this unsaved correction')
    dialog.save_draft()
    settle(dialog)
    assert dialog.text.toPlainText() == 'Keep this unsaved correction'
    assert dialog.snapshot.revision == desk['snapshot'].revision
    assert 'changed since it opened' in dialog.status.text()
    assert dialog.snapshot.path.read_bytes() == saved_elsewhere.raw_payload
    dialog.reload_saved()  # Cancel is the fixture default.
    assert dialog.text.toPlainText() == 'Keep this unsaved correction' and not dialog.busy
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    dialog.reload_saved()
    settle(dialog)
    assert dialog.text.toPlainText() == 'Other saved human words'
    assert dialog.snapshot.revision == saved_elsewhere.revision and not dialog.is_dirty


@pytest.mark.parametrize('value', [False, None])
def test_current_selection_guard_blocks_save_and_reload_without_losing_text(desk, value):
    dialog = desk['make'](verify_source=lambda: value)
    original = dialog.snapshot.path.read_bytes()
    dialog.text.setPlainText('Keep selected-recording correction')
    dialog.save_draft()
    assert not dialog.busy and 'selected recording changed' in dialog.status.text()
    dialog.reload_saved()
    assert not dialog.busy and dialog.text.toPlainText() == 'Keep selected-recording correction'
    assert dialog.snapshot.path.read_bytes() == original


@pytest.mark.parametrize('operation', ['save', 'reload'])
def test_changed_recording_bytes_block_operations_and_keep_buffer(desk, monkeypatch, operation):
    dialog = desk['make']()
    dialog.text.setPlainText('Retain uncommitted words')
    before = dialog.snapshot.path.read_bytes()
    desk['source'].write_bytes(b'A different recording now uses the same filename')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    (dialog.save_draft if operation == 'save' else dialog.reload_saved)()
    settle(dialog)
    assert 'recording changed' in dialog.status.text()
    assert dialog.text.toPlainText() == 'Retain uncommitted words'
    assert dialog.snapshot.path.read_bytes() == before


@pytest.mark.parametrize('route', ['close', 'escape'])
def test_cancel_close_keeps_parent_and_unsaved_draft_open(desk, route):
    dialog = desk['make']()
    dialog.show()
    dialog.text.setPlainText('Do not discard this buffer')
    if route == 'close':
        dialog.close_button.click()
    else:
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert dialog.isVisible() and desk['parent'].isVisible()
    assert dialog.text.toPlainText() == 'Do not discard this buffer'
    assert dialog.snapshot.path.read_bytes() == desk['snapshot'].raw_payload


def test_discard_close_is_explicit_and_does_not_save(desk, monkeypatch):
    dialog = desk['make']()
    dialog.show()
    dialog.text.setPlainText('Discard only this window buffer')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Discard)
    dialog.close()
    QApplication.processEvents()
    assert not dialog.isVisible() and desk['parent'].isVisible()
    assert dialog.snapshot.path.read_bytes() == desk['snapshot'].raw_payload


def test_save_before_close_finishes_async_then_closes_child_only(desk, monkeypatch):
    dialog = desk['make']()
    dialog.show()
    dialog.text.setPlainText('Save before closing this child')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Save)
    dialog.close()
    assert dialog.isVisible() and dialog.busy
    settle(dialog)
    assert not dialog.isVisible() and desk['parent'].isVisible()
    assert drafts.load_draft(dialog.snapshot.path, expected_source_sha256=desk['sha']).text == 'Save before closing this child'


def test_ctrl_s_saves_only_the_separate_untimed_draft(desk):
    before = unchanged_files(desk)
    dialog = desk['make']()
    dialog.show()
    dialog.text.setPlainText('Keyboard correction')
    dialog.text.setFocus()
    QApplication.processEvents()
    QTest.keyClick(dialog.text, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    settle(dialog)
    assert dialog.snapshot.text == 'Keyboard correction'
    assert_unchanged(before)


def test_raw_only_fallback_never_creates_an_editable_draft(desk):
    dialog = desk['make'](None)
    assert dialog.text.isReadOnly() and not dialog.save_button.isEnabled() and not dialog.reload_button.isEnabled()
    assert dialog.text.toPlainText() == desk['original']
    assert 'No editable draft' in dialog.status.text()
    dialog.save_draft()
    dialog.reload_saved()
    assert not dialog.busy and dialog.snapshot is None
    dialog.copy_text()
    assert QApplication.clipboard().text() == desk['original']


def test_failed_save_preserves_buffer_and_all_original_bytes(desk, monkeypatch):
    dialog = desk['make']()
    before = unchanged_files(desk)
    def fail(*a, **k):
        raise drafts.RecognitionDraftError('Disposable simulated write failure')
    monkeypatch.setattr(ui, 'save_user_draft', fail)
    dialog.text.setPlainText('Keep correction through write failure')
    dialog.save_draft()
    settle(dialog)
    assert 'simulated write failure' in dialog.status.text()
    assert dialog.text.toPlainText() == 'Keep correction through write failure' and dialog.is_dirty
    assert dialog.snapshot.path.read_bytes() == desk['snapshot'].raw_payload
    assert_unchanged(before)


@pytest.mark.parametrize('operation', ['save', 'reload'])
def test_selection_change_during_operation_never_replaces_editor(desk, monkeypatch, operation):
    selected = [True]
    dialog = desk['make'](verify_source=lambda: selected[0])
    entered, release = threading.Event(), threading.Event()
    target = ui.save_user_draft if operation == 'save' else ui.load_draft
    def held(*a, **k):
        result = target(*a, **k)
        entered.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(ui, 'save_user_draft' if operation == 'save' else 'load_draft', held)
    dialog.text.setPlainText('Preserve visible buffer during async completion')
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    (dialog.save_draft if operation == 'save' else dialog.reload_saved)()
    try:
        assert entered.wait(3)
        selected[0] = False
    finally:
        release.set()
    settle(dialog)
    assert dialog.text.toPlainText() == 'Preserve visible buffer during async completion'
    assert 'recording' in dialog.status.text() and 'changed' in dialog.status.text()
    if operation == 'save':
        assert 'saved for the original recording' in dialog.status.text()
        assert dialog.snapshot.text == dialog.text.toPlainText()
    else:
        assert dialog.snapshot.revision == desk['snapshot'].revision


def test_invalid_or_mismatched_identity_is_rejected_before_editing(desk):
    with pytest.raises(drafts.RecognitionDraftError):
        ui.RecognitionDraftDialog(desk['snapshot'], source_path=desk['source'], source_sha256='b' * 64)
    with pytest.raises(drafts.RecognitionDraftError):
        ui.RecognitionDraftDialog(None, source_path=desk['source'])


def test_reopened_crlf_text_is_not_dirty_or_rewritten_without_an_edit(desk):
    snapshot = drafts.save_user_draft(desk['snapshot'].path, 'Human CRLF line\r\n\r\nNext line\r\n',
        expected_revision=desk['snapshot'].revision, expected_source_sha256=desk['sha'])
    before = (snapshot.path.read_bytes(), snapshot.path.stat().st_mtime_ns)
    dialog = desk['make'](snapshot)
    assert dialog.text.toPlainText() == 'Human CRLF line\n\nNext line\n'
    assert not dialog.is_dirty and not dialog.save_button.isEnabled()
    dialog.save_draft()
    assert not dialog.busy and dialog.snapshot.raw_payload == snapshot.raw_payload
    assert (snapshot.path.read_bytes(), snapshot.path.stat().st_mtime_ns) == before
    dialog.text.setPlainText('Actual correction\n')
    assert dialog.is_dirty
    dialog.save_draft()
    settle(dialog)
    assert not dialog.is_dirty and dialog.snapshot.text == 'Actual correction\n'
    dialog.reload_saved()
    settle(dialog)
    assert not dialog.is_dirty and dialog.text.toPlainText() == 'Actual correction\n'


@pytest.mark.parametrize('operation', ['save', 'reload'])
def test_recording_mutated_after_worker_result_before_queued_completion(desk, monkeypatch, operation):
    dialog = desk['make']()
    opened = dialog.snapshot
    if operation == 'reload':
        drafts.save_user_draft(opened.path, 'Newer saved text must not replace the open buffer',
            expected_revision=opened.revision, expected_source_sha256=desk['sha'])
    buffer = 'Keep this visible unsaved buffer after queued source mutation'
    dialog.text.setPlainText(buffer)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    original_completed = dialog._completed
    completed_values = []
    def mutate_before_completion(task, action):
        completed_values.append(task.value)
        desk['source'].write_bytes(b'Different recording after the worker returned its verified result')
        original_completed(task, action)
    monkeypatch.setattr(dialog, '_completed', mutate_before_completion)
    (dialog.save_draft if operation == 'save' else dialog.reload_saved)()
    settle(dialog)
    assert completed_values and completed_values[0][1] is not None
    assert dialog.text.toPlainText() == buffer
    assert 'recording' in dialog.status.text() and 'changed' in dialog.status.text()
    if operation == 'reload':
        assert dialog.snapshot.revision == opened.revision and dialog.is_dirty
        assert 'Reloaded saved' not in dialog.status.text()
    else:
        assert dialog.snapshot.text == buffer
        assert 'saved for the original recording' in dialog.status.text()
        assert 'Saved draft text · separate' not in dialog.status.text()


@pytest.mark.parametrize('operation', ['save', 'reload'])
@pytest.mark.parametrize('hash_call', [1, 2])
def test_recording_mutated_inside_fingerprint_after_hash_is_rejected(desk, monkeypatch, operation, hash_call):
    dialog = desk['make']()
    opened = dialog.snapshot
    if operation == 'reload':
        drafts.save_user_draft(opened.path, 'A newer revision from another editor',
            expected_revision=opened.revision, expected_source_sha256=desk['sha'])
    before = opened.path.read_bytes()
    buffer = 'Do not lose these visible edits when the recording changes after hashing'
    dialog.text.setPlainText(buffer)
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    original_fingerprint = ui.fingerprint
    calls = []
    def hash_then_mutate(path):
        value = original_fingerprint(path)
        calls.append(value)
        if len(calls) == hash_call:
            desk['source'].write_bytes(b'Mutated immediately after the file hash was calculated')
        return value
    monkeypatch.setattr(ui, 'fingerprint', hash_then_mutate)
    (dialog.save_draft if operation == 'save' else dialog.reload_saved)()
    settle(dialog)
    assert len(calls) == hash_call
    assert dialog.text.toPlainText() == buffer
    assert 'recording' in dialog.status.text() and 'changed' in dialog.status.text()
    if operation == 'reload' or hash_call == 1:
        assert dialog.snapshot.revision == opened.revision and dialog.is_dirty
        assert opened.path.read_bytes() == before
    else:
        assert dialog.snapshot.text == buffer
        assert 'saved for the original recording' in dialog.status.text()
        assert drafts.load_draft(opened.path, expected_source_sha256=desk['sha']).text == buffer
