"""Program/recording gate: exact saved local approval, never draft preference."""
from copy import deepcopy
import json
import pytest
from PySide6.QtWidgets import QApplication
from gnb_player.lyrics import Cue,LyricsDocument,fingerprint,load_lyrics,save_lyrics
from gnb_player.lyric_review import approve_document,invalidate_review,is_approved,document_revision
from gnb_player.review_core import ReviewStore,edit_phrase
from gnb_player.stage import BroadcastStage
from gnb_player.settings import write_profile,read_profile
from test_draft_preview_preference import station,recordings,settle

def select_program(window,track,index):
    window.engine.order.select(index)
    window.stage.set_track(track.title)
    window.stage.set_playback(2,10,False)
    window._update_program_lyrics(track,window.documents.get(str(track.path)))

@pytest.mark.parametrize('legacy',[True,False])
def test_old_preference_is_preserved_but_cannot_enable_draft_output(station,tmp_path,legacy):
    w=station();tracks,targets=recordings(w,tmp_path)
    profile=tmp_path/'legacy.json';write_profile(profile,tracks,[],w.lyric_files,{'lyrics':True,'preview_unapproved_lyrics':legacy})
    before=[p.read_bytes() for p in targets];w.load_profile(profile);settle(w)
    select_program(w,tracks[0],0)
    assert w.visual_settings()['preview_unapproved_lyrics'] is legacy
    assert not hasattr(w,'allow_drafts') and 'approved' in w.publication_notice.text()
    assert w.stage._lyrics is None and w.stage._lyric_rows()==(None,'','')
    w.save_session();assert read_profile(w.data_dir/'session.json')['settings']['preview_unapproved_lyrics'] is legacy
    assert [p.read_bytes() for p in targets]==before
    assert w.lyric_text.rowCount()>0 # editor still exposes the draft

def test_new_default_closed_and_plain_compositor_fails_closed(station):
    w=station();assert w.visual_settings()['preview_unapproved_lyrics'] is False
    stage=BroadcastStage()
    try:
        stage.set_lyrics(LyricsDocument([Cue('Unapproved',0,5)],10))
        stage.set_playback(1,10,False);assert stage._lyric_rows()==(None,'','')
    finally:stage.shutdown();stage.close()

def test_approved_exact_saved_record_shows_and_wrong_track_clears(station,tmp_path):
    w=station();tracks,targets=recordings(w,tmp_path,approved_second=True)
    select_program(w,tracks[1],1);assert w.stage._lyric_rows()[0].text=='Lyric 1'
    approved_frame=w.stage.render_frame(640,360)
    w.engine.order.select(0);w.stage.render_frame(640,360)
    assert w.stage._lyric_rows()==(None,'','') # even before async track callback
    w.stage.set_lyrics(w.documents[str(tracks[1].path)]);assert w.stage._lyrics is None
    w._track_changed(tracks[0]);assert w.stage._lyrics is None
    settle(w);assert w.stage._lyrics is None and not w.export.running and not w.engine.playing
    select_program(w,tracks[1],1);assert w.stage._lyrics is not None
    w._track_changed(None);assert w.stage._lyric_rows()==(None,'','')

def test_in_memory_approval_without_saved_publication_is_not_enough(station,tmp_path):
    w=station();tracks,targets=recordings(w,tmp_path)
    doc=approve_document(w.documents[str(tracks[0].path)],fingerprint(tracks[0].path))
    w.engine.order.select(0);w._remember_document(tracks[0],doc);w.stage.set_lyrics(doc)
    assert is_approved(doc) and w.stage._lyrics is None
    assert not is_approved(load_lyrics(targets[0]))

@pytest.mark.parametrize('change',['revoke','delete','edit','replace_media'])
def test_frame_guard_detects_persisted_revocation_without_ui_callback(station,tmp_path,change):
    w=station();tracks,targets=recordings(w,tmp_path,approved_second=True)
    select_program(w,tracks[1],1);assert w.stage._lyrics is not None
    if change=='delete':targets[1].unlink()
    elif change=='replace_media':tracks[1].path.write_bytes(b'new recording bytes')
    else:
        doc=load_lyrics(targets[1])
        if change=='revoke':doc=invalidate_review(doc,'Owner revoked approval')
        else:doc.segments[0].text='Changed without a new approval'
        save_lyrics(doc,targets[1])
    image=w.stage.render_frame(640,360)
    assert w.stage._lyrics is None and w.stage._lyric_rows()==(None,'','')
    assert image==w.stage.render_frame(640,360) # no remaining lyric panel/highlight

def test_unsaved_library_edit_clears_then_draft_save_needs_new_approval(station,tmp_path):
    w=station();tracks,targets=recordings(w,tmp_path,approved_second=True)
    w.playlist.setCurrentRow(1);settle(w);select_program(w,tracks[1],1)
    assert w.stage._lyrics is not None;before=targets[1].read_bytes()
    w.lyric_text.item(0,2).setText('Reviewed words changed')
    assert w.stage._lyrics is None and targets[1].read_bytes()==before
    w._save_lyric_changes();settle(w)
    assert not is_approved(load_lyrics(targets[1])) and w.stage._lyrics is None
    w.review_confirm.setChecked(True);w._save_lyric_changes(approve=True);settle(w)
    assert is_approved(load_lyrics(targets[1]))
    w._update_program_lyrics(tracks[1],w.documents[str(tracks[1].path)])
    assert w.stage._lyric_rows()[0].text=='Reviewed words changed'

def test_separate_private_review_draft_keeps_existing_approved_artifact(station,tmp_path):
    w=station();tracks,targets=recordings(w,tmp_path,approved_second=True)
    select_program(w,tracks[1],1);before=targets[1].read_bytes()
    store=ReviewStore(w.data_dir,tracks[1].path)
    private=edit_phrase(w.documents[str(tracks[1].path)],0,'Private draft edit')
    store.save_draft(private)
    assert not is_approved(store.load()) and targets[1].read_bytes()==before
    assert w.stage._lyric_rows()[0].text=='Lyric 1'
    assert w.stage.render_frame(640,360).isNull() is False

@pytest.mark.parametrize('held',[ [{'status':'held'}], [{'status':'unknown'}], 'bad metadata'])
def test_held_candidates_cannot_be_approved_or_rendered(station,tmp_path,held):
    w=station();tracks,targets=recordings(w,tmp_path,approved_second=True)
    doc=load_lyrics(targets[1]);doc.metadata['held_passages']=held
    with pytest.raises(ValueError,match='Unresolved'):approve_document(doc,doc.source_sha256)
    # Even a structurally recomputed approval cannot override unresolved holds.
    doc.human_review['document_revision']=document_revision(doc)
    assert not is_approved(doc)
    save_lyrics(doc,targets[1]);w._remember_document(tracks[1],doc)
    select_program(w,tracks[1],1);assert w.stage._lyric_rows()==(None,'','')

def test_in_memory_edit_and_guard_exception_fail_closed(station,tmp_path,monkeypatch):
    w=station();tracks,targets=recordings(w,tmp_path,approved_second=True)
    select_program(w,tracks[1],1);w.stage._lyrics.segments[0].text='Dirty memory'
    w.stage.render_frame(640,360);assert w.stage._lyrics is None
    doc=load_lyrics(targets[1]);w._remember_document(tracks[1],doc);w._update_program_lyrics(tracks[1],doc)
    monkeypatch.setattr(w.stage,'_publication_guard',lambda doc:(_ for _ in ()).throw(OSError('read failure')))
    w.stage.render_frame(640,360);assert w.stage._lyrics is None

def test_approved_instrumental_is_valid_but_has_no_lyric_display(station,tmp_path):
    w=station();tracks,targets=recordings(w,tmp_path)
    doc=LyricsDocument([],10,tracks[0].title,fingerprint(tracks[0].path),metadata={'instrumental':True})
    doc=approve_document(doc,doc.source_sha256);save_lyrics(doc,targets[0]);w._remember_document(tracks[0],doc)
    select_program(w,tracks[0],0)
    assert w._has_lyrics(tracks[0]) and w.stage._lyric_rows()==(None,'','')
