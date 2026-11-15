"""No models or audio devices: review source, section offsets, approval and UI wiring."""
import copy,json,sys,wave
from pathlib import Path
from types import SimpleNamespace
import pytest
from gnb_player.lyrics import LyricsDocument,Cue,Word,fingerprint
from gnb_player.review_core import ReviewConfig,ReviewStore,exclude_held_passage
from gnb_player.review_qwen import edit_section,qwen_alignment_input,use_supported_phrase_timing
from gnb_player.lyric_review import is_approved,approve_document

@pytest.fixture
def source(tmp_path):
    p=tmp_path/'clip.wav'
    with wave.open(str(p),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\0'*16000*2*5)
    return p

def untimed(source):
    return LyricsDocument([],300,'Fixture',fingerprint(source),metadata={'transcription_only':True,
        'provenance':{'mode':'extract','engine':'qwen'},'recognition_regions':[
        {'start':0,'end':150,'text':'First wrong words','raw_text':'First wrong words'},
        {'start':150,'end':300,'text':'Second verse','raw_text':'Second verse'}]})

def test_config_defaults_and_explicit_fallback(tmp_path):
    c=ReviewConfig.discover(tmp_path)
    assert c.backend=='qwen' and c.qwen_asr_model.endswith('qwen3-asr-0.6b')
    assert c.qwen_aligner_model.endswith('qwen3-forced-aligner-0.6b')
    c.backend='invalid'
    with pytest.raises(ValueError):c.missing()
    c.backend='heart_ctc';c.ctc_model='/absent/ctc'
    assert '/absent/ctc' in c.missing('align')

def test_sections_correct_save_reload_history_and_exact_alignment(source,tmp_path):
    raw=untimed(source);before=copy.deepcopy(raw.to_dict());store=ReviewStore(tmp_path,source);store.bind_output(tmp_path/'cache');store.save_draft(raw)
    edited=edit_section(raw,0,'First corrected words');store.save_draft(edited)
    assert raw.to_dict()==before and store.load().metadata['recognition_regions'][0]['raw_text']=='First wrong words'
    request=qwen_alignment_input(store.load(),'STALE BOX',source='draft')
    assert request['reference_text']=='First corrected words\nSecond verse'
    assert [(r['start'],r['end']) for r in request['alignment_windows']]==[(0,150),(150,300)]
    target,accepted=store.approve(edited,tmp_path/'cache',confirmed=True)
    assert target.exists() and is_approved(accepted) and not accepted.segments
    assert accepted.metadata['transcription_only'] and list((store.root/'history').iterdir())

def test_exclusion_and_corrected_hold_do_not_align_raw_nonsense(source):
    raw=untimed(source);raw.metadata['held_passages']=[{'region':0,'start':0,'end':150,'text':'raw','status':'held'}]
    held_request=qwen_alignment_input(raw,'')
    assert held_request['held_passages'][0]['region']==0 # kept withheld, no mandatory text editing
    corrected=edit_section(raw,0,'Good corrected words')
    assert qwen_alignment_input(corrected,'')['reference_text'].startswith('Good corrected')
    excluded=exclude_held_passage(raw,0)
    request=qwen_alignment_input(excluded,'')
    assert request['reference_text']=='Second verse' and request['alignment_windows'][0]['start']==150
    assert excluded.metadata['recognition_regions'][0]['raw_text']=='First wrong words'

def test_supplied_long_text_not_proportionally_split(source):
    d=untimed(source);d.metadata={}
    with pytest.raises(ValueError,match='explicit sections'):qwen_alignment_input(d,'long supplied words',source='supplied')
    d.duration=60
    assert qwen_alignment_input(d,'[Intro] Repeat repeat',source='supplied')['reference_text']=='[Intro] Repeat repeat'

def test_manual_timing_and_phrase_edits_need_explicit_realignment(source):
    d=LyricsDocument([Cue('Corrected words',12,14,[Word('Corrected',12,13),Word('words',13.5,14)],{'region':0})],150,source_sha256=fingerprint(source),metadata={'alignment_windows':[{'start':0,'end':150,'text':'Old words','evidence':{'kind':'operator_selected','source':'fixture'}}]})
    with pytest.raises(ValueError,match='ALL'):qwen_alignment_input(d,'stale')
    r=qwen_alignment_input(d,'stale',replace_timing=True)
    assert r['reference_text']=='Corrected words' and r['alignment_windows'][0]['text']=='Corrected words'

def test_explicit_group_retains_raw_and_gaps_without_fabrication(source):
    issue={'text':'middle','token_index':1,'region':0,'reason':'zero','replacement_interval':None}
    d=LyricsDocument([Cue('one middle three',1,4,[Word('one',1,2),Word('three',3,4)],{'native_timing_issues':[issue]})],5,source_sha256=fingerprint(source))
    before=d.to_dict()
    with pytest.raises(ValueError):approve_document(d,d.source_sha256)
    grouped=use_supported_phrase_timing(d,0)
    assert d.to_dict()==before and grouped.segments[0].words[0].highlight_intervals==((1,2),(3,4))
    assert grouped.active_word(2.5) is None and grouped.segments[0].words[0].metadata['individual_word_boundaries'] is None
    assert not is_approved(grouped) and is_approved(approve_document(grouped,d.source_sha256))
    assert grouped.segments[0].metadata['resolved_native_timing_issues']==[issue]

def test_preparser_repetition_and_token_limit_hold_without_phrase_blacklist(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'src/gnb_player'))
    from review_qwen_worker import recognition_holds
    regions=[{'start':0,'end':30,'text':'Thank you.','raw_text':'Thank you. '*20,'flags':[]}]
    holds=recognition_holds(regions)
    assert len(holds)==1 and holds[0]['cleaned_text']=='Thank you.' and holds[0]['status']=='held'
    assert recognition_holds([{'start':0,'end':30,'text':'Thank you for being my friend','flags':[]}])==[]
    assert recognition_holds([{'start':0,'end':30,'text':'A real verse','flags':['token_limit']}])

def test_primary_extract_routes_to_review_not_legacy_job():
    from gnb_player.app import MainWindow
    calls=[]
    fake=SimpleNamespace(_selected_track=lambda:object(),open_assisted_review=lambda **kw:calls.append(kw),reference=SimpleNamespace(toPlainText=lambda:'Known words'))
    MainWindow.queue_extraction(fake,False);MainWindow.queue_extraction(fake,True)
    assert calls==[{'supplied_text':None,'prepare':True},{'supplied_text':'Known words','prepare':True}]

@pytest.fixture
def dialog(tmp_path,source,monkeypatch):
    from PySide6.QtCore import QObject,Signal
    from PySide6.QtWidgets import QApplication
    import gnb_player.review_dialog as module
    class Media(QObject):
        positionChanged=Signal(int);playbackStateChanged=Signal(int);durationChanged=Signal(int);mediaStatusChanged=Signal(int);errorOccurred=Signal(int)
        PlaybackState=module.QMediaPlayer.PlaybackState
        def __init__(self,*a):super().__init__();self.pos=0
        def setAudioOutput(self,a):pass
        def setSource(self,s):pass
        def position(self):return self.pos
        def duration(self):return 300000
        def playbackState(self):return self.PlaybackState.PausedState
        def pause(self):pass
        def setPosition(self,n):self.pos=n
        def stop(self):pass
    class Audio(QObject):
        def setVolume(self,v):pass
    monkeypatch.setattr(module,'QMediaPlayer',Media);monkeypatch.setattr(module,'QAudioOutput',Audio)
    monkeypatch.setattr(module,'embedded_lyrics',lambda _: {'kind':'no_embedded_lyrics','text':'','tags':{},'trusted':False})
    app=QApplication.instance() or QApplication([])
    d=module.LyricsReviewDialog(SimpleNamespace(path=source,duration=300,title='Fixture'),tmp_path/'data',tmp_path/'cache')
    yield d
    d.dirty=False;d.timer.stop();d.close();app.processEvents()

def test_ui_section_edits_seek_save_reload_and_engine_choice(dialog,source):
    d=dialog;d.set_document(untimed(source));assert d.reference.toPlainText()=='First wrong words'
    d.reference.setPlainText('First corrected words');d.section_select.setCurrentIndex(1)
    assert d.document.metadata['recognition_regions'][0]['text']=='First corrected words'
    assert d.reference.toPlainText()=='Second verse'
    d.reference.setPlainText('Second corrected verse');assert d.save_draft()
    d.restore_draft();d.section_select.setCurrentIndex(1)
    assert d.reference.toPlainText()=='Second corrected verse'
    d.seek(151.25);assert '151.250' in d.clock.text() and d.document.active_word(151.25) is None
    assert d.extract_button.text()=='Extract & align' and d.align_button.text()=='Retry alignment'
    d.backend_select.setCurrentIndex(1);assert d.config.backend=='heart_ctc'
    d.backend_select.setCurrentIndex(0);assert d.config.backend=='qwen'
    assert not d.store.output_target.exists()

def test_ui_no_generation_when_cancelled_no_changes_to_approved_output(dialog,source):
    d=dialog;d.set_document(untimed(source));d.cancel_job()
    assert not d.busy and not d.store.output_target.exists()
    d.reference.setPlainText('New words');d.commit_text_section();d.undo_edit()
    assert d.reference.toPlainText()=='First wrong words'
    d.redo_edit();assert d.reference.toPlainText()=='New words'

def test_aligned_result_replaces_last_section_input_with_full_reference(dialog,source):
    d=dialog;d.set_document(untimed(source));d.section_select.setCurrentIndex(1)
    aligned=LyricsDocument([Cue('Full reference',1,3,[Word('Full',1,2),Word('reference',2,3)])],300,source_sha256=fingerprint(source),metadata={'provenance':{'mode':'align','reference_original':'Full reference from every reviewed section'}})
    d.set_document(aligned)
    assert d._section_index==-1 and d.reference.toPlainText()=='Full reference from every reviewed section'
