"""Independent failure-recovery GUI integration; synthetic bytes and no devices.

No recognizer, stream, native credentials, user media or existing settings are
accessed. Timed lyric fixtures deliberately remain distinct from untimed drafts.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import time
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND', 'ffmpeg')
import pytest
from PySide6.QtCore import QByteArray, QObject, QProcess, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from gnb_player.broadcast_profile import BroadcastProfileStore
from gnb_player.extraction import ExtractionConfig
from gnb_player.extract_worker import retain_failed_recognition
from gnb_player.library import Track
from gnb_player.lyric_review import approve_document, document_revision, is_approved
from gnb_player.lyrics import Cue, LyricsDocument, fingerprint, load_lyrics, save_lyrics
from gnb_player.recognition_drafts import load_draft, save_user_draft


class NoDeviceAudio(QObject):
    def setVolume(self, value): self._volume = value
    def volume(self): return getattr(self, '_volume', .8)


class Pending:
    def __init__(self, work, done):
        self.work, self.done = work, done
        self.failed = self
        self.failures = []
    def connect(self, callback): self.failures.append(callback)
    def finish(self):
        try: value = self.work()
        except Exception as error:
            for callback in self.failures: callback(str(error))
        else: self.done(value)


@pytest.fixture
def station(tmp_path, monkeypatch):
    from gnb_player import app as appmod, playback
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(playback, 'QAudioOutput', NoDeviceAudio)
    monkeypatch.setattr(QMediaPlayer, 'setAudioOutput', lambda *a: None)
    monkeypatch.setattr(appmod, 'discover_config', lambda: ExtractionConfig())
    monkeypatch.setattr(appmod, 'resolve_existing_models', lambda config: (config, {
        'fields': {}, 'notes': [], 'effective_model_paths': {}, 'resolved_values': {}}))
    monkeypatch.setattr(appmod, 'config_status', lambda config: {
        'ready': False, 'missing_models': [], 'python_available': False,
        'ffmpeg_available': True, 'ffprobe_available': True})
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Cancel)
    pending, windows, keyring_calls = [], [], []
    def deferred(self, work, done):
        task = Pending(work, done); pending.append(task); return task
    monkeypatch.setattr(appmod.MainWindow, '_task', deferred)
    def forbidden_keyring():
        keyring_calls.append('native access')
        raise AssertionError('Recovery tests must not access native credentials')
    def make():
        data = tmp_path / 'isolated station'
        window = appmod.MainWindow(data, credential_store=BroadcastProfileStore(data, backend_factory=forbidden_keyring))
        window.preview_timer.stop(); window.save_timer.stop()
        monkeypatch.setattr(window, '_next_extraction', lambda: None)
        monkeypatch.setattr(window.export, 'start', Mock(side_effect=AssertionError('No output in recovery tests')))
        monkeypatch.setattr(window.extract_process, 'start', Mock(side_effect=AssertionError('No recognizer in recovery tests')))
        windows.append(window)
        return window
    window = make()
    yield window, pending, make
    for current in reversed(windows):
        for child in list(current._recovery_dialogs.values()):
            wait_dialog(child)
            if child.snapshot is not None: child.text.setPlainText(child.snapshot.text)
            child.close()
        current._closing = True; current.close(); current.save_timer.stop()
    app.processEvents()
    assert not keyring_calls


def drain(pending):
    count = 0
    while pending:
        count += 1
        assert count < 80, 'GUI work should complete in a bounded number of callbacks'
        pending.pop(0).finish()
    QApplication.processEvents()


def wait_dialog(dialog):
    deadline = time.monotonic() + 5
    while dialog.busy and time.monotonic() < deadline:
        QApplication.processEvents(); QTest.qWait(5)
    assert not dialog.busy, 'Small local draft I/O did not finish'
    QApplication.processEvents()


def seed(window, tmp_path, pending, *, approved=False, count=1):
    tracks = []
    for i in range(count):
        source = tmp_path / f'fixture-recording-{i}.mp3'
        if not source.exists(): source.write_bytes(f'not decoded; identity fixture {i}'.encode())
        tracks.append(Track(source, f'Disposable recording {i}'))
    window.all_tracks = tracks; window._apply_queue(); window.playlist.setCurrentRow(0)
    drain(pending)
    track = tracks[0]
    target = window.data_dir / 'saved-human-timed-lyrics.json'
    if not target.exists():
        doc = LyricsDocument([Cue('Original human timed line', 1, 2)], 3,
                             source_sha256=fingerprint(track.path), quality={'status': 'review'})
        if approved: doc = approve_document(doc, fingerprint(track.path))
        save_lyrics(doc, target)
    window._lyrics_ready(track, target, load_lyrics(target)); window.playlist.setCurrentRow(0)
    drain(pending)
    return tracks, target


def failure(window, track, text='Machine recognition line', *, cache=None):
    evidence = {'primary': {'segments': [{'text': text, 'start': 0., 'end': 1., 'words': []}]},
                'pre_alignment': [{'text': text, 'start': 0., 'end': 1., 'words': []}]}
    return retain_failed_recognition(ValueError('Synthetic alignment failed'), evidence,
                                    Path(cache or window.extract_config.cache_dir), fingerprint(track.path))


def deliver_error(window, track, error, monkeypatch, *, overrides=None, finish=True):
    event = {'type': 'error', 'message': 'Failure message has no path instructions',
             'source_sha256': error.source_sha256,
             'diagnostic_path': error.diagnostic_path}
    if hasattr(error, 'recognition_draft_path'): event['recognition_draft_path'] = error.recognition_draft_path
    event.update(overrides or {})
    chunks = [QByteArray((json.dumps(event) + '\n').encode())]
    monkeypatch.setattr(window.extract_process, 'readAllStandardOutput', lambda: chunks.pop(0) if chunks else QByteArray())
    window._extract_current = track
    window._extract_context = {'track': track, 'target': window.data_dir / 'unused-output.json'}
    window._process_bytes = b''; window._extract_log = ''; window._extract_failure = ''
    window._extract_output()
    if finish: window._extract_finished(1, QProcess.ExitStatus.NormalExit)
    return event


def visible_dialogs(window):
    return [dialog for dialog in window._recovery_dialogs.values() if dialog.isVisible()]


def open_recovery(window, pending):
    assert window.review_recognition_button.isEnabled()
    window.review_recognition_button.click(); drain(pending)
    opened = visible_dialogs(window)
    assert len(opened) == 1, window.recovery_status.text()
    return opened[0]


@pytest.mark.parametrize('approved', [False, True])
def test_structured_failure_edit_copy_save_and_reopen_preserve_timed_human_state(station, tmp_path, monkeypatch, approved):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending, approved=approved)
    track = tracks[0]; key = str(track.path)
    window._extract_current = track  # Simulate a correction typed after a job began.
    window.lyric_text.item(0, 2).setText('Unsaved human timed correction — 雨')
    original_document = window.documents[key]
    original = {'target': target.read_bytes(), 'rows': window.lyric_text.raw_rows(),
                'buffers': deepcopy(window._lyric_buffers), 'approval': deepcopy(original_document.human_review),
                'revision': document_revision(original_document), 'ready': window._has_lyrics(track),
                'stage': window.stage._lyrics, 'source': track.path.read_bytes()}
    retained = failure(window, track)
    raw_failure = Path(retained.diagnostic_path).read_bytes()
    deliver_error(window, track, retained, monkeypatch)
    dialog = open_recovery(window, pending)
    assert not dialog.text.isReadOnly()
    assert dialog.original.isReadOnly() and dialog.diagnostics.isReadOnly()
    assert dialog.original.toPlainText() == 'Machine recognition line'
    edited = 'Corrected untimed words — 安全\nSecond line'
    dialog.text.setPlainText(edited); dialog.copy_button.click()
    assert QApplication.clipboard().text() == edited
    dialog.save_button.click(); wait_dialog(dialog)
    saved = load_draft(retained.recognition_draft_path, expected_source_sha256=fingerprint(track.path))
    assert saved.text == edited and saved.payload['edited_by'] == 'human'
    assert saved.payload['status'] == 'unaligned' and saved.payload['needs_review'] is True
    assert not {'segments', 'human_review', 'publication', 'approved'} & saved.payload.keys()
    assert target.read_bytes() == original['target'] and track.path.read_bytes() == original['source']
    assert Path(retained.diagnostic_path).read_bytes() == raw_failure
    assert window.documents[key] is original_document
    assert document_revision(window.documents[key]) == original['revision']
    assert original_document.human_review == original['approval']
    assert is_approved(load_lyrics(target)) is approved
    assert window.lyric_text.raw_rows() == original['rows'] and window._lyric_buffers == original['buffers']
    assert window._has_lyrics(track) == original['ready'] and window.stage._lyrics is original['stage']
    assert saved not in window.documents.values()
    dialog.close(); QApplication.processEvents()
    reopened = open_recovery(window, pending)
    assert reopened.text.toPlainText() == edited
    assert reopened.original.toPlainText() == 'Machine recognition line'
    window.export.start.assert_not_called(); window.extract_process.start.assert_not_called()


def test_saved_untimed_text_reopens_after_station_restart_without_worker_event(station, tmp_path, monkeypatch):
    window, pending, make = station
    tracks, target = seed(window, tmp_path, pending)
    retained = failure(window, tracks[0]); before = target.read_bytes()
    opened = load_draft(retained.recognition_draft_path, expected_source_sha256=fingerprint(tracks[0].path))
    save_user_draft(opened.path, 'Human recovery retained over restart', expected_revision=opened.revision,
                    expected_source_sha256=opened.source_sha256)
    second = make(); seed(second, tmp_path, pending)
    assert not second._recovery_records
    dialog = open_recovery(second, pending)
    assert dialog.text.toPlainText() == 'Human recovery retained over restart'
    assert target.read_bytes() == before


def test_automatic_retry_keeps_edited_draft_and_both_original_reports(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    first = failure(window, track, 'First failed words')
    opened = load_draft(first.recognition_draft_path, expected_source_sha256=fingerprint(track.path))
    human = save_user_draft(opened.path, 'Saved human recovery words', expected_revision=opened.revision,
                            expected_source_sha256=opened.source_sha256)
    second = failure(window, track, 'Different retry guess')
    deliver_error(window, track, second, monkeypatch)
    dialog = open_recovery(window, pending)
    assert dialog.text.toPlainText() == human.text
    assert dialog.original.toPlainText() == 'First failed words'
    assert 'Different retry guess' in dialog.diagnostics.toPlainText()
    assert Path(first.diagnostic_path).exists() and Path(second.diagnostic_path).exists()
    assert load_draft(human.path, expected_source_sha256=human.source_sha256).raw_payload == human.raw_payload
    assert load_lyrics(target).segments[0].text == 'Original human timed line'


def test_late_open_completion_after_selection_change_cannot_replace_new_editor(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending, count=2)
    retained = failure(window, tracks[0]); deliver_error(window, tracks[0], retained, monkeypatch)
    window.review_recognition_button.click()
    assert len(pending) == 1
    opening = pending.pop(); result = opening.work()
    window.playlist.setCurrentRow(1); drain(pending)
    before = (window._editor_track, window.lyric_text.raw_rows(), deepcopy(window._lyric_buffers), target.read_bytes())
    opening.done(result); QApplication.processEvents()
    assert not visible_dialogs(window)
    assert (window._editor_track, window.lyric_text.raw_rows(), window._lyric_buffers, target.read_bytes()) == before
    assert window._selected_track().path == tracks[1].path


def test_recording_replaced_after_async_read_cannot_open_stale_draft(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    retained = failure(window, track); deliver_error(window, track, retained, monkeypatch)
    window.review_recognition_button.click(); opening = pending.pop(); result = opening.work()
    before = (target.read_bytes(), window.lyric_text.raw_rows(), deepcopy(window._lyric_buffers))
    track.path.write_bytes(b'new recording at old filename')
    opening.done(result); QApplication.processEvents()
    assert not visible_dialogs(window) and 'changed' in window.recovery_status.text().lower()
    assert (target.read_bytes(), window.lyric_text.raw_rows(), window._lyric_buffers) == before


@pytest.mark.parametrize('kind', ['wrong-sha', 'outside-cache', 'traversal', 'wrong-draft-name', 'message-only'])
def test_unbound_or_malicious_worker_metadata_never_opens_or_replaces_lyrics(station, tmp_path, monkeypatch, kind):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    retained = failure(window, track, cache=tmp_path / 'outside-cache')
    event = {'source_sha256': retained.source_sha256, 'recognition_draft_path': retained.recognition_draft_path}
    if kind == 'wrong-sha': event['source_sha256'] = 'f' * 64
    elif kind == 'traversal': event['recognition_draft_path'] = str(Path(retained.recognition_draft_path).parent / '..' / 'recognition-drafts' / Path(retained.recognition_draft_path).name)
    elif kind == 'wrong-draft-name': event['recognition_draft_path'] = str(Path(window.extract_config.cache_dir) / 'recognition-drafts' / ('a' * 64 + '.json'))
    elif kind == 'message-only':
        event = {'source_sha256': None, 'diagnostic_path': None, 'recognition_draft_path': None,
                 'message': 'Diagnostics: ' + retained.diagnostic_path + ' Draft: ' + retained.recognition_draft_path}
    before = (target.read_bytes(), window.lyric_text.raw_rows(), deepcopy(window._lyric_buffers))
    deliver_error(window, track, retained, monkeypatch, overrides=event)
    window.review_recognition_button.click(); drain(pending)
    assert not visible_dialogs(window), window.recovery_status.text()
    assert (target.read_bytes(), window.lyric_text.raw_rows(), window._lyric_buffers) == before
    assert 'not opened' in window.recovery_status.text().lower()


def test_stale_revision_and_cancel_keep_child_buffer_and_parent_timed_rows(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    retained = failure(window, track); deliver_error(window, track, retained, monkeypatch)
    window.show(); dialog = open_recovery(window, pending)
    original = dialog.snapshot
    newer = save_user_draft(original.path, 'Another window saved this revision',
                           expected_revision=original.revision, expected_source_sha256=original.source_sha256)
    pending_text = 'Keep this unsaved child correction'
    dialog.text.setPlainText(pending_text)
    before = (target.read_bytes(), window.lyric_text.raw_rows(), deepcopy(window._lyric_buffers))
    dialog.save_button.click(); wait_dialog(dialog)
    assert dialog.text.toPlainText() == pending_text and dialog.is_dirty
    assert load_draft(original.path, expected_source_sha256=original.source_sha256).raw_payload == newer.raw_payload
    assert 'changed' in dialog.status.text().lower()
    dialog.reload_button.click(); QApplication.processEvents()  # Cancel default.
    assert dialog.text.toPlainText() == pending_text
    assert not dialog.close() and dialog.isVisible()
    assert not window.close() and window.isVisible()  # A dirty child Cancel also stops station close.
    assert (target.read_bytes(), window.lyric_text.raw_rows(), window._lyric_buffers) == before


def test_switching_selected_recording_blocks_child_save_without_losing_text(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending, count=2)
    retained = failure(window, tracks[0]); deliver_error(window, tracks[0], retained, monkeypatch)
    dialog = open_recovery(window, pending); original = dialog.snapshot.raw_payload
    dialog.text.setPlainText('Human text kept when another song is selected')
    window.playlist.setCurrentRow(1); drain(pending)
    other_rows = window.lyric_text.raw_rows()
    dialog.save_button.click(); wait_dialog(dialog)
    assert dialog.text.toPlainText() == 'Human text kept when another song is selected'
    assert dialog.snapshot.path.read_bytes() == original
    assert window.lyric_text.raw_rows() == other_rows
    assert window._selected_track().path == tracks[1].path


@pytest.mark.parametrize('kind', ['missing', 'corrupt', 'symlink'])
def test_unavailable_draft_only_opens_read_only_raw_evidence_without_replacing_it(station, tmp_path, monkeypatch, kind):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    retained = failure(window, track); path = Path(retained.recognition_draft_path)
    outside = tmp_path / 'unrelated-private-draft.txt'
    if kind == 'missing': path.unlink()
    elif kind == 'corrupt': path.write_bytes(b'Preserved unknown manual draft format')
    else:
        outside.write_text('Unrelated external text stays untouched')
        path.unlink(); path.symlink_to(outside)
    before = target.read_bytes()
    draft_before = path.read_bytes() if path.exists() else None
    deliver_error(window, track, retained, monkeypatch)
    dialog = open_recovery(window, pending)
    assert dialog.snapshot is None and dialog.text.isReadOnly()
    assert not dialog.save_button.isEnabled() and not dialog.reload_button.isEnabled()
    assert dialog.original.isReadOnly() and dialog.diagnostics.isReadOnly()
    assert dialog.original.toPlainText() == 'Machine recognition line'
    dialog.copy_button.click()
    assert QApplication.clipboard().text() == 'Machine recognition line'
    assert target.read_bytes() == before
    assert (path.read_bytes() if path.exists() else None) == draft_before
    if kind == 'symlink':
        assert path.is_symlink() and outside.read_text() == 'Unrelated external text stays untouched'


def test_new_failed_retry_cannot_reload_an_already_open_unsaved_child(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    first = failure(window, track, 'Original machine words')
    deliver_error(window, track, first, monkeypatch)
    dialog = open_recovery(window, pending)
    dialog.text.setPlainText('Human buffer must survive another failed attempt')
    before = (dialog.snapshot.raw_payload, target.read_bytes(), window.lyric_text.raw_rows())
    newer = failure(window, track, 'Different machine guess from retry')
    deliver_error(window, track, newer, monkeypatch)
    window.review_recognition_button.click(); drain(pending)
    assert visible_dialogs(window) == [dialog]
    assert dialog.text.toPlainText() == 'Human buffer must survive another failed attempt'
    assert dialog.is_dirty and dialog.snapshot.raw_payload == before[0]
    assert target.read_bytes() == before[1] and window.lyric_text.raw_rows() == before[2]
    assert dialog.original.toPlainText() == 'Original machine words'


def test_keyboard_save_in_child_cannot_save_or_approve_parent_timed_edits(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    window.lyric_text.item(0, 2).setText('Pending timed correction stays unsaved')
    before = (target.read_bytes(), deepcopy(window._lyric_buffers), window.lyric_text.raw_rows())
    retained = failure(window, track); deliver_error(window, track, retained, monkeypatch)
    dialog = open_recovery(window, pending)
    dialog.text.setPlainText('Keyboard-saved separate untimed draft')
    dialog.text.setFocus(); QApplication.processEvents()
    QTest.keyClick(dialog.text, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    wait_dialog(dialog)
    saved = load_draft(retained.recognition_draft_path, expected_source_sha256=fingerprint(track.path))
    assert saved.text == 'Keyboard-saved separate untimed draft'
    assert (target.read_bytes(), window._lyric_buffers, window.lyric_text.raw_rows()) == before
    assert not is_approved(load_lyrics(target))


def test_visible_old_child_is_not_presented_as_current_after_recording_bytes_change(station, tmp_path, monkeypatch):
    window, pending, _ = station
    tracks, target = seed(window, tmp_path, pending); track = tracks[0]
    retained = failure(window, track); deliver_error(window, track, retained, monkeypatch)
    dialog = open_recovery(window, pending)
    old_sha = dialog.source_sha256
    dialog.text.setPlainText('Preserve these edits bound to the original recording')
    before = (target.read_bytes(), window.lyric_text.raw_rows(), dialog.snapshot.raw_payload)
    raised = Mock(); monkeypatch.setattr(dialog, 'raise_', raised)
    track.path.write_bytes(b'Replaced recording with a different exact identity')
    window.review_recognition_button.click()
    assert pending, 'Existing visible children must not bypass fresh source validation'
    drain(pending)
    raised.assert_not_called()
    assert 'not opened' in window.recovery_status.text().lower()
    assert visible_dialogs(window) == [dialog] and dialog.source_sha256 == old_sha
    assert dialog.text.toPlainText() == 'Preserve these edits bound to the original recording'
    dialog.save_button.click(); wait_dialog(dialog)
    assert 'recording changed' in dialog.status.text().lower()
    assert (target.read_bytes(), window.lyric_text.raw_rows(), dialog.snapshot.raw_payload) == before


def test_review_action_and_child_controls_remain_visible_at_minimum_window_sizes(station, tmp_path, monkeypatch):
    from PySide6.QtCore import QPoint
    from gnb_player.app import STYLE
    window, pending, _ = station
    tracks, _ = seed(window, tmp_path, pending)
    retained = failure(window, tracks[0]); deliver_error(window, tracks[0], retained, monkeypatch)
    window.setStyleSheet(STYLE)
    window.resize(1030, 720); window.show(); window.tabs.setCurrentIndex(1)
    QApplication.processEvents()
    page = window.tabs.widget(1)
    action_rect = window.review_recognition_button.rect().translated(window.review_recognition_button.mapTo(page, QPoint()))
    assert window.review_recognition_button.isVisible() and page.rect().contains(action_rect)
    footer = [window.save_lyrics_button, window.approve_lyrics_button,
              window.reload_lyrics_button, window.export_lyrics_button,
              window.review_recognition_button]
    footer_rects = [control.rect().translated(control.mapTo(page, QPoint())) for control in footer]
    for index, rect in enumerate(footer_rects):
        assert page.rect().contains(rect), footer[index].text()
        for earlier in footer_rects[:index]:
            assert not rect.intersects(earlier), 'Timed lyric and untimed recovery actions must not overlap'
    dialog = open_recovery(window, pending); dialog.resize(620, 480); QApplication.processEvents()
    for control in (dialog.text, dialog.save_button, dialog.reload_button, dialog.copy_button, dialog.close_button):
        rect = control.rect().translated(control.mapTo(dialog, QPoint()))
        assert control.isVisible() and dialog.rect().contains(rect)
    assert dialog.original.isReadOnly() and dialog.diagnostics.isReadOnly()
