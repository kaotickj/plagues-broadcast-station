"""Private batch routing and orchestration; no real model calls."""
import json
import pytest
from gnb_player.batch_review import batch_plan
from gnb_player.lyrics import LyricsDocument, Cue, fingerprint, load_lyrics
from gnb_player.lyric_review import approve_document, is_approved
from gnb_player.review_core import ReviewConfig
from test_draft_preview_preference import station, recordings, settle

def doc(duration=120,metadata=None):
    return LyricsDocument([],duration,'Fixture','a'*64,metadata=metadata or {})

def tags(text):
    return {'kind':'embedded_unverified','text':text,'tags':{'lyrics':text}}

def test_batch_audit_does_not_trust_old_vocal_approval():
    old=LyricsDocument([Cue('Wrong words',1,3)],120,'Fixture','a'*64)
    old=approve_document(old,old.source_sha256)
    assert is_approved(old)
    assert batch_plan(old,{},existing=old)[0]=='extract'
    assert batch_plan(old,{},existing=old,missing_only=True)[0]=='existing'
    mode,_,sheet=batch_plan(old,tags('[Verse 1]\nCorrect words'),existing=old)
    assert mode=='align' and sheet['sung_text'].strip()=='Correct words'
    assert sheet['origin']=='embedded_batch_unverified'
    assert sheet['import_source']['trusted'] is False

@pytest.mark.parametrize('embedded',[tags('[Style: blues]\n[Sax solo]'),
    tags('[Possibly sung words]'),tags('Sung words\n[Maybe more words]'),
    {'kind':'probe_failed','text':''},
    {'text':'one\ntwo','tags':{'lyrics':'one','USLT':'two'}}])
def test_ambiguous_metadata_never_starts_models(embedded):
    assert batch_plan(doc(),embedded)[0]=='review'

def test_explicit_instrumentals_and_long_alignment():
    instrumental=doc(metadata={'instrumental':True})
    assert batch_plan(instrumental,{})[0]=='instrumental'
    assert batch_plan(doc(),{},existing=instrumental)[0]=='instrumental'
    assert batch_plan(doc(240),tags('Sung words'))[0]=='review'
    assert batch_plan(doc(240),tags('Sung words'),allow_long_ctc=True)[0]=='align'
    assert batch_plan(doc(240),tags('Sung words'),backend='heart_ctc')[0]=='align'

def test_batch_keeps_approved_files_and_advances_past_failed_job(station,tmp_path,monkeypatch):
    from gnb_player.batch_review_dialog import BatchReviewDialog
    import gnb_player.review_dialog as review
    w=station();tracks,targets=recordings(w,tmp_path,approved_second=True)
    before=[p.read_bytes() for p in targets]
    monkeypatch.setattr(review,'embedded_lyrics',lambda p: tags('[Instrumental]') if p==tracks[0].path else tags('Actual words'))
    monkeypatch.setattr(ReviewConfig,'missing',lambda *args:['absent model'])
    batch=BatchReviewDialog(w,tracks)
    batch.start()
    settle(w,lambda:batch.finished)
    assert [r['status'] for r in batch.rows]==['review','failed']
    assert batch.rows[1]['previous_approval'] is True
    assert batch.rows[1]['route']=='align'
    assert [p.read_bytes() for p in targets]==before
    report=json.loads(batch.report.read_text())
    assert report['accuracy_measured'] is False and report['previous_approvals_trusted'] is False
    assert report['accuracy_target']==.95 and report['hallucination_target']==0
    batch.close()

def test_pause_and_cancel_before_starting_next_recording(station,tmp_path,monkeypatch):
    from gnb_player.batch_review_dialog import BatchReviewDialog
    w=station();tracks,_=recordings(w,tmp_path)
    batch=BatchReviewDialog(w,tracks)
    batch.start();batch.toggle_pause()
    batch._next()
    assert batch.current is None and batch.index==0
    batch.cancel()
    assert batch.finished and all(r['status']=='cancelled' for r in batch.rows)
    batch.close()
