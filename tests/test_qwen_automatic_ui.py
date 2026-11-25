"""Default automatic action and optional correction, with fake media/processes."""
import json,copy
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_qwen_review_flow import source,dialog
from gnb_player.lyrics import LyricsDocument,Cue,Word
from gnb_player.review_core import ReviewConfig


def stage(dialog,monkeypatch):
    requests=[]
    monkeypatch.setattr(ReviewConfig,'missing',lambda *a:[])
    monkeypatch.setattr(dialog.process,'start',lambda exe,args:requests.append(json.loads(Path(args[-1]).read_text())))
    return requests


def feed(dialog,monkeypatch,event):
    monkeypatch.setattr(dialog.process,'readAllStandardOutput',lambda:(json.dumps(event)+'\n').encode())
    dialog._output()
    monkeypatch.setattr(dialog.process,'readAllStandardOutput',lambda:b'')


def test_default_click_no_text_input_and_double_click_cannot_duplicate(dialog,monkeypatch):
    d=dialog;requests=stage(d,monkeypatch)
    assert not d.reference.toPlainText() and d.reference.isHidden() and d.supplied_align_button.isHidden()
    d.extract_button.click();d.extract_button.click()
    assert len(requests)==1 and requests[0]['mode']=='extract'
    assert requests[0]['reference_text']=='' and requests[0]['config']['backend']=='qwen'
    assert d.busy and not d.store.output_target.exists()
    d.set_busy(False)


def test_bracket_only_embedded_metadata_holds_automatic_extraction(dialog,monkeypatch):
    d=dialog;requests=stage(d,monkeypatch)
    text='[Intro: saxophone]\n[Slow instrumental blues, no vocals]'
    d.embedded={'kind':'embedded_unverified','text':text,'tags':{'lyrics':text}}
    before=d.document.to_dict()
    d.begin_automatic()
    assert not requests and not d.store.output_target.exists()
    assert d.document.to_dict()==before and not d.instrumental.isChecked()
    assert 'Possible instrumental' in d.status.text()
    d.reference.setPlainText(text)
    d.start_job('align',source='supplied')
    assert not requests and 'Bracket-only' in d.status.text()
    d.instrumental.setChecked(True)
    d.begin_automatic()
    assert not requests and d.document.metadata['instrumental'] is True
    d.instrumental.setChecked(False)
    d.ignore_tags.setChecked(True)
    d.begin_automatic()
    assert len(requests)==1 and requests[0]['reference_text']==''
    d.set_busy(False)


def test_main_action_starts_automatic_job_after_dialog_opens(monkeypatch):
    import gnb_player.app as appmodule
    import gnb_player.review_dialog as review
    calls=[]
    class FakeDialog:
        def __init__(self,*args):
            self.preparation=SimpleNamespace(show=lambda:None)
            self.prepare_button=SimpleNamespace(setText=lambda _:None)
            self.status=SimpleNamespace(setText=lambda _:None)
            self.approved=SimpleNamespace(connect=lambda _:None)
        def begin_automatic(self):calls.append('automatic')
        def exec(self):calls.append('dialog');return 0
    monkeypatch.setattr(review,'LyricsReviewDialog',FakeDialog)
    callbacks=[];monkeypatch.setattr(appmodule.QTimer,'singleShot',lambda ms,cb:callbacks.append(cb))
    fake=SimpleNamespace(_selected_track=lambda:object(),export=SimpleNamespace(running=False),engine=SimpleNamespace(playing=False),
        extract_process=SimpleNamespace(state=lambda:appmodule.QProcess.ProcessState.NotRunning),_extract_queue=[],
        lyric_text=SimpleNamespace(commit_pending_edit=lambda:None),_lyric_buffers={},_saving_lyrics=set(),data_dir='data',
        extract_config=SimpleNamespace(cache_dir='cache'),_find_document=lambda _:None)
    appmodule.MainWindow.open_assisted_review(fake,prepare=True)
    assert calls==['dialog'] and len(callbacks)==1
    callbacks[0]();assert calls==['dialog','automatic']


def test_alignment_failure_exposes_saved_words_retry_without_manual_entry(dialog,monkeypatch,tmp_path):
    d=dialog;requests=stage(d,monkeypatch);d.extract_button.click()
    raw={'duration':300,'source_sha256':d.store.digest,'segments':[],'transcription_only':True,
         'recognition_regions':[{'start':0,'end':150,'text':'Saved words','raw_text':'Saved words'}],
         'provenance':{'engine':'qwen','mode':'extract'},'reviewed':False}
    path=tmp_path/'checkpoint.json';path.write_text(json.dumps(raw))
    feed(d,monkeypatch,{'type':'progress','checkpoint':str(path),'message':'Words saved'})
    feed(d,monkeypatch,{'type':'cancelled','message':'Cancelled'})
    d._finished(2,None)
    assert not d.busy and d.align_button.text()=='Retry alignment' and d.reference.isHidden()
    assert d.document.metadata['recognition_regions'][0]['text']=='Saved words'
    d.align_button.click()
    assert len(requests)==2 and requests[1]['mode']=='align' and requests[1]['reference_text']=='Saved words'
    assert not d.store.output_target.exists()
    d.set_busy(False)


def test_optional_text_edit_passage_request_preserves_neighbors_and_undo(dialog,monkeypatch):
    d=dialog;requests=stage(d,monkeypatch)
    doc=LyricsDocument([Cue('Before',1,2,[Word('Before',1,2)]),Cue('wrong words',4,6,[Word('wrong',4,5),Word('words',5,6)]),Cue('After',8,9,[Word('After',8,9)])],300,source_sha256=d.store.digest)
    d.set_document(doc);before=copy.deepcopy(d.document.to_dict()['segments'])
    d.open_phrase_correction(1);assert d.edit_tabs.currentIndex()==0 and d.player.position()==3000
    d.phrase.setPlainText('correct words');d.realign_passage_button.click()
    assert requests[0]['replacement_scope']=='selected_passage' and requests[0]['reference_text']=='correct words'
    assert requests[0]['alignment_windows'][0]['start']==2 and requests[0]['alignment_windows'][0]['end']==8
    assert d.document.to_dict()['segments'][0]==before[0] and d.document.to_dict()['segments'][2]==before[2]
    d.set_busy(False);d.undo_edit();assert d.document.segments[1].text=='wrong words'
    assert not d.store.output_target.exists()


def test_existing_candidate_replacement_remains_explicit_and_reversible(dialog,monkeypatch):
    import gnb_player.review_dialog as review
    d=dialog;requests=stage(d,monkeypatch)
    doc=LyricsDocument([Cue('keep me',1,2,[Word('keep',1,1.5),Word('me',1.5,2)])],300,source_sha256=d.store.digest)
    d.set_document(doc);before=d.document.to_dict()
    monkeypatch.setattr(review.QMessageBox,'question',lambda *a:review.QMessageBox.StandardButton.No)
    d.begin_automatic();assert not requests and d.document.to_dict()==before
    monkeypatch.setattr(review.QMessageBox,'question',lambda *a:review.QMessageBox.StandardButton.Yes)
    d.begin_automatic();assert len(requests)==1 and d.store.draft_path.is_file()
    assert not d.store.output_target.exists();d.set_busy(False)
