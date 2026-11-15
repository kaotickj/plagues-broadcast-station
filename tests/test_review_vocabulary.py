"""Software-only vocabulary decisions; no model or audio device use."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import wave

import pytest
from gnb_player.lyrics import LyricsDocument, Cue, Word, fingerprint
from gnb_player.lyric_review import approve_document, is_approved, document_revision
from gnb_player.review_core import ReviewStore, alignment_input, edit_phrase
from gnb_player.recognition_policy import audio_only_policy
from gnb_player.review_vocabulary import stage_suggestions, decide_suggestion, unresolved_suggestions


@pytest.fixture
def source(tmp_path):
    path=tmp_path/'recording.wav'
    with wave.open(str(path),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0\0'*80000)
    return path


def sample(source, text='Merkwater blues'):
    tokens=text.split();words=[Word(t,1+i*.6,1.2+i*.6) for i,t in enumerate(tokens)]
    return LyricsDocument([Cue(text,words[0].start,words[-1].end,words)],5,'Fixture',fingerprint(source),
                          metadata={'provenance':{'mode':'extract','raw_evidence':'untouched/raw'}})


def profile(doc, form='Merkwater', canonical='Murkwater'):
    return {'entries':[{'canonical':canonical,'source':{'audio_sha256':doc.source_sha256,
        'kind':'owner_accepted_full_song_reference','sha256':'b'*64},
        'observed_confusions':[{'form':form,'basis':'owner-confirmed recording-specific correction'}]}]}


def first(doc):return unresolved_suggestions(doc)[0]['id']


def test_default_no_hint_title_and_profile_metadata_are_not_model_inputs(source):
    d=sample(source)
    assert audio_only_policy({'title':'Murkwater Blues','vocabulary_review':profile(d)})=={
        'automatic_vocabulary_injection':False,'context':'','track_title_used_as_prompt':False,'policy':'audio_only_v1'}


@pytest.mark.parametrize('key',['context','hotwords','vocabulary','vocabulary_context','initial_prompt','prompt','prompt_ids','decoder_input_ids','automatic_vocabulary_injection'])
@pytest.mark.parametrize('in_config',[False,True])
def test_nonempty_model_hints_rejected(key,in_config):
    request={key:'Hancock'}
    with pytest.raises(ValueError,match='disabled'):audio_only_policy({'config':request} if in_config else request)


def test_non_fallout_or_catalog_only_profile_makes_no_suggestions(source):
    d=sample(source,'Bartender pour me one');p=profile(d,form='Bartender')
    p['entries'][0]['source']['audio_sha256']='c'*64
    assert stage_suggestions(d,p).to_dict()==d.to_dict()
    p['entries'][0]['source']['audio_sha256']=d.source_sha256
    p['entries'][0]['source']['kind']='catalog_title_only'
    assert stage_suggestions(d,p).to_dict()==d.to_dict()
    assert stage_suggestions(d,profile(d)).to_dict()==d.to_dict()


def test_stage_separate_accept_version_provenance_timing_and_alignment(source):
    original=approve_document(sample(source),fingerprint(source));before=copy.deepcopy(original.to_dict())
    pending=stage_suggestions(original,profile(original))
    assert original.to_dict()==before and is_approved(original)
    assert pending.segments==original.segments and not is_approved(pending)
    with pytest.raises(ValueError,match='Accept or reject'):alignment_input(pending,'',replace_timing=True)
    with pytest.raises(ValueError):approve_document(pending,pending.source_sha256)
    corrected=decide_suggestion(pending,first(pending),accept=True)
    assert corrected.segments[0].text=='Murkwater blues'
    assert [(w.start,w.end,w.highlight_intervals) for w in corrected.segments[0].words]==[(w.start,w.end,w.highlight_intervals) for w in original.segments[0].words]
    assert not is_approved(corrected) and document_revision(corrected)!=document_revision(original)
    evidence=corrected.metadata['vocabulary_review']
    assert evidence['original_segments']==before['segments']
    assert evidence['original_provenance']==original.metadata['provenance']
    assert evidence['decisions'][0]['before_document_revision']==document_revision(pending)
    assert evidence['decisions'][0]['decision']=='accepted'
    assert alignment_input(corrected,'STALE',replace_timing=True)['reference_text']=='Murkwater blues'
    assert alignment_input(corrected,'STALE',replace_timing=True)['vocabulary_review']==evidence
    assert 'vocabulary_review' not in alignment_input(corrected,'Different supplied words',source='supplied',replace_timing=True)


def test_reject_preserves_all_words_timing_raw_and_can_align_unchanged(source):
    d=sample(source);pending=stage_suggestions(d,profile(d))
    rejected=decide_suggestion(pending,first(pending),accept=False)
    assert rejected.segments==d.segments and rejected.metadata['provenance']==d.metadata['provenance']
    assert not unresolved_suggestions(rejected)
    assert alignment_input(rejected,'',replace_timing=True)['reference_text']=='Merkwater blues'
    assert stage_suggestions(rejected,profile(d)).to_dict()==rejected.to_dict()


def test_count_change_group_keeps_instrumental_gap_and_original_native_spans(source):
    d=sample(source,'Merge water');staged=stage_suggestions(d,profile(d,'Merge water'))
    corrected=decide_suggestion(staged,first(staged),accept=True)
    group=corrected.segments[0].words[0]
    assert group.text=='Murkwater' and group.metadata['individual_word_boundaries'] is None
    assert group.highlight_intervals==tuple((w.start,w.end) for w in d.segments[0].words)
    assert corrected.active_word(1.4) is None
    assert group.metadata['native_constituents']==[w.to_dict() for w in d.segments[0].words]


def test_second_occurrence_rebased_and_stale_edits_never_overwritten(source):
    d=sample(source,'Merkwater Merkwater');pending=stage_suggestions(d,profile(d))
    once=decide_suggestion(pending,first(pending),accept=True)
    twice=decide_suggestion(once,first(once),accept=True)
    assert twice.segments[0].text=='Murkwater Murkwater'
    with pytest.raises(ValueError,match='pending'):decide_suggestion(twice,pending.metadata['vocabulary_review']['suggestions'][0]['id'],accept=True)
    edited=edit_phrase(pending,0,'My own words')
    with pytest.raises(ValueError,match='changed'):decide_suggestion(edited,first(edited),accept=True)
    assert decide_suggestion(edited,first(edited),accept=False).segments==edited.segments


def test_exact_words_not_substrings_and_wrong_recording_rejected(source):
    d=sample(source,'Merkwaters Merkwater')
    pending=stage_suggestions(d,profile(d));assert len(unresolved_suggestions(pending))==1
    changed=copy.deepcopy(pending);changed.source_sha256='d'*64
    with pytest.raises(ValueError,match='another recording'):decide_suggestion(changed,first(changed),accept=True)


def test_save_reload_history_and_no_output_approval(tmp_path,source):
    store=ReviewStore(tmp_path/'data',source);store.bind_output(tmp_path/'cache')
    original=sample(source);store.save_draft(original);original_bytes=store.draft_path.read_bytes()
    staged=stage_suggestions(original,profile(original));store.save_draft(staged)
    corrected=decide_suggestion(store.load(),first(staged),accept=True);store.save_draft(corrected)
    assert store.load().to_dict()==corrected.to_dict()
    assert original_bytes in [p.read_bytes() for p in (store.root/'history').iterdir()]
    assert not store.output_target.exists() and not is_approved(corrected)


def test_worker_rejects_injection_before_lock_decode_or_model(tmp_path,source):
    request={'source':str(source),'source_sha256':fingerprint(source),'config':{},'mode':'extract','context':'Murkwater'}
    path=tmp_path/'request.json';path.write_text(json.dumps(request))
    worker=Path(__file__).parents[1]/'src/gnb_player/review_worker.py'
    result=subprocess.run([sys.executable,str(worker),'--request',str(path)],capture_output=True,text=True,timeout=5)
    receipt=json.loads((tmp_path/'receipt.json').read_text())
    assert result.returncode==2 and 'disabled' in result.stdout and receipt['calls']==[]
    assert not (tmp_path/'audio.wav').exists() and receipt['workers_stopped']


def test_review_controls_accept_reject_undo_with_fake_media(tmp_path,source,monkeypatch):
    # Real Qt widgets; fake media avoids the known denied audio-device path.
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    import gnb_player.review_dialog as module
    class Media(QObject):
        positionChanged=Signal(int);playbackStateChanged=Signal(int);durationChanged=Signal(int)
        mediaStatusChanged=Signal(int);errorOccurred=Signal(int)
        PlaybackState=module.QMediaPlayer.PlaybackState
        def __init__(self,*args):super().__init__();self.pos=0
        def setAudioOutput(self,a):pass
        def setSource(self,s):pass
        def position(self):return self.pos
        def duration(self):return 5000
        def playbackState(self):return self.PlaybackState.PausedState
        def pause(self):pass
        def setPosition(self,n):self.pos=n
        def stop(self):pass
    class Audio(QObject):
        def setVolume(self,v):pass
    monkeypatch.setattr(module,'QMediaPlayer',Media);monkeypatch.setattr(module,'QAudioOutput',Audio)
    monkeypatch.setattr(module,'embedded_lyrics',lambda _: {'kind':'no_embedded_lyrics','text':'','tags':{},'trusted':False})
    app=QApplication.instance() or QApplication([])
    from types import SimpleNamespace
    dialog=module.LyricsReviewDialog(SimpleNamespace(path=source,duration=5,title='Fixture'),tmp_path/'data',tmp_path/'cache')
    d=sample(source);dialog.set_document(d)
    profile_path=tmp_path/'profile.json';profile_path.write_text(json.dumps(profile(d)))
    monkeypatch.setattr(module.QFileDialog,'getOpenFileName',lambda *a:(str(profile_path),''))
    dialog.load_vocabulary();assert dialog.edit_tabs.tabText(3)=='Names'
    assert dialog.document.segments==d.segments and 'Merkwater' in dialog.name_evidence.toPlainText()
    dialog.decide_name(False);assert dialog.document.segments==d.segments
    dialog.undo_edit();assert unresolved_suggestions(dialog.document)
    dialog.decide_name(True);assert dialog.document.segments[0].text=='Murkwater blues' and not dialog.confirm.isChecked()
    dialog.undo_edit();assert dialog.document.segments[0].text=='Merkwater blues'
    dialog.redo_edit();assert dialog.document.segments[0].text=='Murkwater blues'
    assert dialog.save_draft() and not dialog.store.output_target.exists()
    dialog.restore_draft();assert dialog.document.segments[0].text=='Murkwater blues'
    dialog.timer.stop();dialog.close()
