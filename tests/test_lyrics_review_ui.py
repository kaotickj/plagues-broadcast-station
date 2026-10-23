"""Disposable operator-desk tests: no audio, recognizers, or live user data."""
import json
import os
from pathlib import Path

import pytest
from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox
from PySide6.QtTest import QTest

from gnb_player.app import MainWindow
from gnb_player.lyrics import Cue, LyricsDocument, Word, cached_lyrics_path, fingerprint, load_lyrics, save_lyrics
from gnb_player.lyric_review import approve_document, document_revision, invalidate_review, is_approved
from gnb_player.lyric_editor import stored_review_notes
from test_app import window, add_fixture_tracks


class PendingTask:
    def __init__(self,work,done):
        self.work=work; self.done=done; self.failed=self; self.failures=[]
    def connect(self,callback):self.failures.append(callback)
    def finish(self):
        try:result=self.work()
        except Exception as error:
            for callback in self.failures:callback(str(error))
        else:self.done(result)


@pytest.fixture
def pending(window,monkeypatch):
    tasks=[]
    def start(work,done):
        task=PendingTask(work,done);tasks.append(task);return task
    monkeypatch.setattr(window,'_task',start)
    return tasks


def open_draft(window,tmp_path,pending,*,approved=False,count=1):
    tracks=add_fixture_tracks(window,tmp_path,count)
    track=tracks[0]
    document=LyricsDocument([
        Cue('A quiet evening carries me',1,4,[Word('A',1,1.2),Word('quiet',1.2,1.7),Word('evening',1.7,2.4),Word('carries',2.4,3),Word('me',3,4)]),
        Cue('The old road brings me home',6,9,[Word('The',6,6.4),Word('old',6.4,6.8),Word('road',6.8,7.4),Word('brings',7.4,8),Word('me',8,8.4),Word('home',8.4,9)]),
    ],12,track.title,fingerprint(track.path),['Stored summary from the recognizer.'],
        {'status':'review','reasons':[{'code':'uncertain','message':'Check this phrase against the recording.','at':1.2}]})
    if approved:document=approve_document(document,fingerprint(track.path))
    target=tmp_path/'lyrics.json';save_lyrics(document,target)
    window._lyrics_ready(track,target,load_lyrics(target))
    return tracks,target


def test_notes_show_only_stored_timestamps_and_real_passage_context(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending)
    notes=window.review_notes.toPlainText()
    assert '0:01.20' in notes and 'line 1' in notes and 'A quiet evening' in notes
    assert 'Stored summary from the recognizer.' in notes
    doc=load_lyrics(target);doc.quality={'status':'review','reasons':[{'code':'confidence','message':'Recognition confidence was low.'}]}
    notes=stored_review_notes(doc)
    assert 'Summary — Recognition confidence was low.' in notes
    assert 'no passage timestamps were stored' in notes
    assert 'line 1' not in notes and '0:01' not in notes
    doc.quality={'status':'review'};doc.warnings=[]
    assert 'No passage-level review notes were stored' in stored_review_notes(doc)


def test_qc_pass_and_alignment_completion_never_autoapprove(window,tmp_path,pending,monkeypatch):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    doc=load_lyrics(target);doc.quality={'status':'pass'};doc.human_review={};save_lyrics(doc,target)
    window._extract_current=track;window._extract_context={'track':track,'target':target}
    event=QByteArray((json.dumps({'type':'done','output':str(target)})+'\n').encode())
    monkeypatch.setattr(window.extract_process,'readAllStandardOutput',lambda:event)
    window._extract_output()
    assert 'LYRICS NEED REVIEW' in window.playlist.item(0).text()
    assert not window._has_lyrics(track) and window.stage._lyrics is None
    assert not window.review_confirm.isChecked() and not window.approve_lyrics_button.isEnabled()
    assert not is_approved(load_lyrics(target))


def test_edit_save_preserves_other_line_word_times_and_requires_human_review(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    original=target.read_bytes();before=load_lyrics(target)
    window.lyric_text.item(0,2).setText('A quiet evening calls to me')
    assert window.save_lyrics_button.isEnabled() and 'Unsaved' in window.lyric_save_status.text()
    assert target.read_bytes()==original
    window._save_lyric_changes()
    assert window.lyric_save_status.text().startswith('Saving') and target.read_bytes()==original
    pending[-1].finish()
    saved=load_lyrics(target)
    assert saved.segments[0].text=='A quiet evening calls to me' and saved.segments[0].words==[]
    assert (saved.segments[0].start,saved.segments[0].end)==(1,4)
    assert saved.segments[1]==before.segments[1]
    assert saved.metadata['review_original_quality']==before.quality
    assert any(path.read_bytes()==original for path in (target.parent/'history').glob('*.json'))
    assert not is_approved(saved) and not window._has_lyrics(track)
    assert window.lyric_save_status.text().startswith('Saved · Draft') and not window._lyric_buffers
    assert 'Edited lines 1 use line timing' in window.review_notes.toPlainText()


def test_failed_atomic_save_keeps_edits_and_prior_file(window,tmp_path,pending,monkeypatch):
    _,target=open_draft(window,tmp_path,pending);original=target.read_bytes()
    window.lyric_text.item(0,2).setText('The corrected line remains here')
    def fail(*args,**kwargs):raise OSError('Read-only storage fixture')
    monkeypatch.setattr('gnb_player.app.save_review_document',fail)
    window._save_lyric_changes();pending[-1].finish()
    assert window.lyric_save_status.text().startswith('Not saved')
    assert 'still here' in window.lyric_save_status.text()
    assert window.lyric_text.item(0,2).text()=='The corrected line remains here'
    assert window._lyric_buffers and target.read_bytes()==original
    assert window.save_lyrics_button.isEnabled() and not window.approve_lyrics_button.isEnabled()


def test_explicit_approval_persists_exact_revision_and_keeps_qc_findings(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    initial=load_lyrics(target);window.engine.order.select(0)
    queued=len(pending);window._save_lyric_changes(approve=True)
    assert len(pending)==queued and not is_approved(load_lyrics(target))
    window.review_confirm.setChecked(True)
    assert window.approve_lyrics_button.isEnabled()
    window._save_lyric_changes(approve=True);pending[-1].finish()
    saved=load_lyrics(target)
    assert is_approved(saved,fingerprint(track.path)) and window._has_lyrics(track)
    assert saved.metadata['review_original_quality']==initial.quality and saved.warnings==initial.warnings
    assert window.stage._lyrics.to_dict()==window.documents[str(track.path)].to_dict()
    assert 'LYRICS READY' in window.playlist.item(0).text()
    assert 'Saved · Reviewed and approved' in window.lyric_save_status.text()
    # An edit immediately holds even the old approved version off-air.
    window.lyric_text.item(0,2).setText('An edit needs its own review')
    assert window.stage._lyrics is None and not window._has_lyrics(track)
    assert not window.approve_lyrics_button.isEnabled() and not window.review_confirm.isChecked()


def test_track_switch_and_background_save_cannot_approve_the_other_recording(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending,count=2);first,second=tracks
    window.lyric_text.item(0,2).setText('The first recording has this edit')
    window._save_lyric_changes();save_task=pending[-1]
    second_doc=LyricsDocument([Cue('Second recording words',1,3)],12,second.title,fingerprint(second.path))
    other=tmp_path/'other.json';save_lyrics(second_doc,other);window._lyrics_ready(second,other,load_lyrics(other))
    window.playlist.setCurrentRow(1)
    assert window._editor_track==second and window.lyric_text.toPlainText()=='Second recording words'
    save_task.finish()
    assert window._editor_track==second and window.lyric_text.toPlainText()=='Second recording words'
    assert load_lyrics(target).segments[0].text=='The first recording has this edit'
    assert not is_approved(load_lyrics(other)) and not window.review_confirm.isChecked()
    window.playlist.setCurrentRow(0)
    assert window.lyric_text.item(0,2).text()=='The first recording has this edit'


def test_unsaved_buffers_survive_switch_and_old_load_callback(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending,count=2);first,second=tracks
    window._load_track_lyrics(first);old_task=pending[-1]
    window.lyric_text.item(0,2).setText('Keep this unsaved correction')
    old_task.finish()
    assert window.lyric_text.item(0,2).text()=='Keep this unsaved correction'
    window.playlist.setCurrentRow(1);window.playlist.setCurrentRow(0)
    assert window.lyric_text.item(0,2).text()=='Keep this unsaved correction'
    assert window.save_lyrics_button.isEnabled() and not window.review_confirm.isChecked()


def test_stale_disk_revision_and_replaced_source_cannot_be_approved(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    window.review_confirm.setChecked(True)
    changed=load_lyrics(target);changed.segments[0].text='A new saved revision';save_lyrics(changed,target)
    latest=target.read_bytes()
    window._save_lyric_changes(approve=True);pending[-1].finish()
    assert target.read_bytes()==latest and not is_approved(load_lyrics(target))
    assert window.lyric_save_status.text().startswith('Not saved') and not window._has_lyrics(track)
    window._display_document(track,load_lyrics(target));window.review_confirm.setChecked(True)
    track.path.write_bytes(b'a replaced recording')
    window._save_lyric_changes(approve=True);pending[-1].finish()
    assert target.read_bytes()==latest and not is_approved(load_lyrics(target))
    assert 'recording changed' in window.lyric_save_status.text().lower()


def test_saved_draft_and_approval_restore_after_session_reload(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    window.lyric_text.item(0,2).setText('A correction saved for restart')
    window._save_lyric_changes();pending[-1].finish();window.save_session()
    window.documents.clear();window._lyric_stamps.clear();window.load_profile(window.data_dir/'session.json')
    assert not window._has_lyrics(track)
    window._display_document(track,window._find_document(track))
    assert window.lyric_text.item(0,2).text()=='A correction saved for restart'
    window.review_confirm.setChecked(True);window._save_lyric_changes(approve=True);pending[-1].finish();window.save_session()
    window.documents.clear();window.load_profile(window.data_dir/'session.json')
    assert window._has_lyrics(track) and is_approved(window._find_document(track),fingerprint(track.path))


def test_invalid_time_edits_are_not_saved_and_removing_line_retains_other_timings(window,tmp_path,pending):
    _,target=open_draft(window,tmp_path,pending);original=target.read_bytes();before=load_lyrics(target)
    window.lyric_text.item(0,1).setText('13')
    count=len(pending);window._save_lyric_changes()
    assert len(pending)==count and target.read_bytes()==original and 'Not saved' in window.lyric_save_status.text()
    window.lyric_text.item(0,1).setText('4');window.lyric_text.selectRow(0);window.lyric_text.remove_lines()
    window._save_lyric_changes();pending[-1].finish()
    assert load_lyrics(target).segments==[before.segments[1]]


def test_line_timing_control_repairs_unusable_word_times_without_inventing_words(window,tmp_path,pending):
    _,target=open_draft(window,tmp_path,pending)
    doc=load_lyrics(target);doc.segments[0].words=[Word('A quiet evening carries me',1,3),Word('overlap',2,4)]
    save_lyrics(doc,target);window._lyrics_ready(window._editor_track,target,load_lyrics(target))
    original_text=window.lyric_text.toPlainText()
    window.lyric_text.selectRow(0);window._use_line_timing();window._save_lyric_changes();pending[-1].finish()
    saved=load_lyrics(target)
    assert saved.segments[0].words==[] and saved.segments[1].words
    assert window.lyric_text.toPlainText()==original_text
    window.review_confirm.setChecked(True);window._save_lyric_changes(approve=True);pending[-1].finish()
    assert is_approved(load_lyrics(target))


def test_closing_with_unsaved_edits_can_cancel_or_save_all_drafts(window,tmp_path,pending,monkeypatch):
    _,target=open_draft(window,tmp_path,pending)
    window.lyric_text.item(0,2).setText('Keep me when closing')
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**kw:QMessageBox.StandardButton.Cancel)
    event=QCloseEvent();window.closeEvent(event)
    assert not event.isAccepted() and window._lyric_buffers and not window._closing
    monkeypatch.setattr(QMessageBox,'question',lambda *a,**kw:QMessageBox.StandardButton.Save)
    event=QCloseEvent();window.closeEvent(event)
    assert not event.isAccepted() and window.lyric_save_status.text().startswith('Saving')
    pending[-1].finish()
    assert load_lyrics(target).segments[0].text=='Keep me when closing' and not window._lyric_buffers


def test_ctrl_s_commits_the_active_cell_before_saving(window,tmp_path,pending):
    _,target=open_draft(window,tmp_path,pending)
    window.tabs.setCurrentIndex(1);window.show();QApplication.processEvents()
    table=window.lyric_text;table.setCurrentCell(0,2);table.setFocus();QTest.keyClick(table,Qt.Key.Key_F2)
    editor=QApplication.focusWidget()
    assert isinstance(editor,QLineEdit)
    editor.setText('Committed through the keyboard shortcut')
    count=len(pending);QTest.keyClick(editor,Qt.Key.Key_S,Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    assert len(pending)==count+1 and window.lyric_save_status.text().startswith('Saving')
    pending[-1].finish()
    assert load_lyrics(target).segments[0].text=='Committed through the keyboard shortcut'


def test_importing_an_approved_document_creates_an_unapproved_saved_revision(window,tmp_path,pending,monkeypatch):
    tracks,target=open_draft(window,tmp_path,pending,approved=True);track=tracks[0]
    cached=cached_lyrics_path(track.path,window.extract_config.cache_dir)
    save_lyrics(load_lyrics(target),cached);window._lyrics_ready(track,cached,load_lyrics(cached))
    imported=load_lyrics(target);imported.segments[0].text='Imported words need their own human check';imported.segments[0].words=[]
    imported=approve_document(imported,fingerprint(track.path))
    incoming=tmp_path/'imported.json';save_lyrics(imported,incoming)
    monkeypatch.setattr('gnb_player.app.QFileDialog.getOpenFileName',lambda *a,**kw:(str(incoming),'Timed lyrics'))
    window.allow_drafts.setChecked(True)
    window.import_lyrics();pending[-1].finish()
    result=load_lyrics(cached)
    assert result.segments[0].text==imported.segments[0].text and not is_approved(result)
    assert 'LYRICS NEED REVIEW' in window.playlist.item(0).text()
    assert not window.allow_drafts.isChecked() and not window._has_lyrics(track)


def test_intentionally_empty_draft_can_be_saved_but_never_approved(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    window.lyric_text.selectAll();window.lyric_text.remove_lines();window._save_lyric_changes();pending[-1].finish()
    assert load_lyrics(target).segments==[] and not is_approved(load_lyrics(target))
    assert window.lyric_save_status.text().startswith('Saved · Draft')
    assert 'Saved empty draft' in window.lyric_status.text()
    assert window.add_line_button.isEnabled() and not window.approve_lyrics_button.isEnabled()
    assert not window._has_lyrics(track)


def test_known_recording_duration_also_bounds_line_edits(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    track.duration=8
    window.lyric_text.item(0,2).setText('A correction cannot extend the recording')
    before=target.read_bytes();count=len(pending);window._save_lyric_changes()
    assert len(pending)==count and target.read_bytes()==before
    assert 'measured duration' in window.lyric_save_status.text()


def test_playback_clock_holds_old_approval_after_saved_file_is_replaced(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending,approved=True);track=tracks[0]
    window.engine.order.select(0);window._display_document(track,window.documents[str(track.path)])
    assert window.stage._lyrics is not None
    changed=invalidate_review(load_lyrics(target),'A replacement requires review.')
    save_lyrics(changed,target)
    window._position(1)
    assert window.stage._lyrics is None and not window._has_lyrics(track)


def test_late_save_callback_never_restores_revoked_approval(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending);track=tracks[0]
    window.review_confirm.setChecked(True);window._save_lyric_changes(approve=True)
    save_task=pending[-1];saved_target,signed=save_task.work()
    revoked=invalidate_review(load_lyrics(target),'New preparation replaced this approval.')
    save_lyrics(revoked,target)
    save_task.done((saved_target,signed))
    assert not is_approved(load_lyrics(target)) and not window._has_lyrics(track)
    assert 'saved revision changed' in window.lyric_save_status.text().lower()
    assert window.stage._lyrics is None


def test_auto_discovered_lrc_edits_save_as_json_without_overwriting_the_lrc(window,tmp_path,pending):
    track=add_fixture_tracks(window,tmp_path,1)[0]
    sidecar=track.path.with_suffix('.lrc');sidecar.write_text('[00:01]A fixture line\n[00:04]Another fixture line')
    original=sidecar.read_bytes();document=window._find_document(track)
    window._remember_document(track,document);window._display_document(track,document)
    window.lyric_text.item(0,2).setText('A corrected fixture line')
    window._save_lyric_changes();pending[-1].finish()
    target=Path(window.lyric_files[str(track.path)])
    assert target.suffix=='.json' and sidecar.read_bytes()==original
    assert load_lyrics(target).segments[0].text=='A corrected fixture line'
    assert not is_approved(load_lyrics(target)) and not window._lyric_buffers


def test_approval_does_not_trust_a_replaced_recording_with_preserved_size_and_mtime(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending,approved=True);track=tracks[0]
    window.engine.order.select(0);window._display_document(track,window.documents[str(track.path)])
    before=track.path.stat();original=track.path.read_bytes()
    track.path.write_bytes(b'x'*len(original));os.utime(track.path,ns=(before.st_atime_ns,before.st_mtime_ns))
    assert track.path.stat().st_size==before.st_size and track.path.stat().st_mtime_ns==before.st_mtime_ns
    window._position(1)
    assert window.stage._lyrics is None and not window._has_lyrics(track)
