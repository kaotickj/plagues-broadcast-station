"""Actual GUI failure delivery with disposable lyrics; no models or playback."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QByteArray, QProcess

from gnb_player.lyric_review import document_revision, is_approved
from gnb_player.lyrics import load_lyrics, save_lyrics
from test_app import window, add_fixture_tracks
from test_lyrics_review_ui import open_draft, pending


@pytest.mark.parametrize('approved', [False, True])
@pytest.mark.parametrize('editing_during_job', [False, True])
def test_failed_extraction_keeps_saved_revision_edits_and_actionable_error(
        window, tmp_path, pending, monkeypatch, approved, editing_during_job):
    tracks, target = open_draft(window, tmp_path, pending, approved=approved)
    track = tracks[0]
    key = str(track.path)
    original_bytes = target.read_bytes()
    original_source = track.path.read_bytes()
    original_document = window.documents[key]
    original_revision = document_revision(original_document)
    original_approval = deepcopy(original_document.human_review)

    # Extraction began before the operator typed a correction. Failure must not
    # reload the old rows over that current edit buffer.
    if editing_during_job:
        window.lyric_text.item(0, 2).setText('雨 keeps falling — corrected locally')
    original_buffer = deepcopy(window._lyric_buffers)
    expected_rows = window.lyric_text.toPlainText()
    window._extract_current = track
    window._extract_context = {'track': track, 'target': target}
    window._process_bytes = b''
    window._extract_log = 'model stderr without the actionable explanation'
    window.extract_progress.setValue(55)
    window.cancel_extract.setEnabled(True)
    window.preparation_progress.show()
    message = ('The separated vocal track contains too little usable signal. '
               'Check the recording or retry with vocal separation disabled. '
               'Existing lyrics were kept. Diagnostics: /disposable/failure.json')
    output = [QByteArray((json.dumps({'type': 'error', 'message': message}) + '\n').encode())]
    monkeypatch.setattr(window.extract_process, 'readAllStandardOutput',
                        lambda: output.pop(0) if output else QByteArray())
    ready = Mock()
    monkeypatch.setattr(window, '_lyrics_ready', ready)
    monkeypatch.setattr(window, '_next_extraction', lambda: None)

    window._extract_output()
    assert message in window.statusBar().currentMessage()
    window._extract_finished(1, QProcess.ExitStatus.NormalExit)

    ready.assert_not_called()
    assert message in window.lyric_status.text()
    assert 'too little usable signal' in window.lyric_status.text()
    assert 'separation disabled' in window.lyric_status.text()
    assert window.extract_progress.value() == 55
    assert not window.cancel_extract.isEnabled()
    assert window.preparation_progress.isHidden()
    assert window._extract_current is None and window._extract_context is None
    assert target.read_bytes() == original_bytes
    assert track.path.read_bytes() == original_source
    assert window.documents[key] is original_document
    assert document_revision(window.documents[key]) == original_revision
    assert load_lyrics(target).human_review == original_approval
    assert is_approved(load_lyrics(target)) is approved
    assert window._lyric_buffers == original_buffer
    assert window.lyric_text.toPlainText() == expected_rows
    assert not window.review_confirm.isChecked()
    assert not window.approve_lyrics_button.isEnabled()
    if editing_during_job:
        assert 'Unsaved' in window.lyric_save_status.text()
        assert window.save_lyrics_button.isEnabled()
    assert not (target.parent / 'history').exists()


@pytest.mark.parametrize('prior_state',['none','empty','nonempty'])
def test_failed_job_never_labels_old_or_empty_draft_as_newly_saved(window,tmp_path,pending,monkeypatch,prior_state):
    target=None
    if prior_state=='none':
        track=add_fixture_tracks(window,tmp_path,1)[0]
        window._display_document(track,None)
    else:
        tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
        if prior_state=='empty':
            doc=load_lyrics(target);doc.segments=[];save_lyrics(doc,target)
            window._lyrics_ready(track,target,load_lyrics(target))
            assert 'Saved empty draft' in window.lyric_save_status.text()
    before=target.read_bytes() if target else None
    window._extract_current=track;window._extract_failure='Rejected implausible recognition fixture.'
    monkeypatch.setattr(window,'_extract_output',lambda:None)
    monkeypatch.setattr(window,'_next_extraction',lambda:None)
    window._extract_finished(1,QProcess.ExitStatus.NormalExit)
    assert window.lyric_save_status.text().startswith('Extraction failed · No replacement saved.')
    expected={'none':'No saved lyrics for this recording.','empty':'Previous empty draft retained.','nonempty':'Previous saved lyrics retained.'}
    assert expected[prior_state] in window.lyric_save_status.text()
    if target:assert target.read_bytes()==before


def test_other_recording_failure_keeps_current_recording_status(window,tmp_path,pending,monkeypatch):
    tracks,target=open_draft(window,tmp_path,pending,count=2)
    displayed=window._editor_track
    failing=next(track for track in tracks if track.path!=displayed.path)
    before=(window.lyric_status.text(),window.lyric_save_status.text(),window.lyric_text.raw_rows())
    window._extract_current=failing;window._extract_failure='Another recording failed.'
    monkeypatch.setattr(window,'_extract_output',lambda:None)
    monkeypatch.setattr(window,'_next_extraction',lambda:None)
    window._extract_finished(1,QProcess.ExitStatus.NormalExit)
    assert (window.lyric_status.text(),window.lyric_save_status.text(),window.lyric_text.raw_rows())==before
