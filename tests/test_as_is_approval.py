"""One basic final confirmation; immutable text/timing, honest persistence."""
import copy
import pytest
from PySide6.QtWidgets import QMessageBox
from test_qwen_review_flow import source, dialog
from test_track_acceptance_ui import saved_case
from gnb_player.lyrics import LyricsDocument, Cue, Word, load_lyrics
from gnb_player.lyric_review import is_approved
from gnb_player.review_acceptance import accept_current_request, validate_acceptance


def setup(d, monkeypatch, answer=QMessageBox.StandardButton.Ok):
    d.set_document(saved_case(d))
    d.recover_words_button.click()
    calls=[]
    monkeypatch.setattr(QMessageBox, 'question', lambda *a:(calls.append(a),answer)[1])
    return calls


def test_one_ok_saves_applies_exact_recovered_case_and_history(dialog, monkeypatch):
    d=dialog;calls=setup(d,monkeypatch)
    d.save_draft();old_draft=d.store.draft_path.read_bytes()
    before=copy.deepcopy(d.document.to_dict());emitted=[]
    d.approved.connect(lambda *a:emitted.append(a))
    d.confirm.setChecked(True);d.approve_button.click()
    assert len(calls)==1 and calls[0][2]=='Save and use these lyrics and timings as they are?'
    assert calls[0][3]==QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel
    assert len(emitted)==1 and is_approved(d.document),d.status.text()
    assert d.status.text()=='Saved and applied to this app’s library/output. Lyrics and timing kept as-is.'
    for restored in [load_lyrics(d.store.output_target),d.store.load()]:
        assert is_approved(restored,d.store.digest)
        assert restored.to_dict()['segments']==before['segments']
        assert restored.metadata['held_passages']==before['held_passages']
        assert restored.metadata['timing_recovery_history']==before['timing_recovery_history']
    assert any(p.read_bytes()==old_draft for p in (d.store.root/'history').glob('*draft.json'))
    old_output=d.store.output_target.read_bytes()
    d.approve_button.click()
    assert len(calls)==2
    assert any(p.read_bytes()==old_output for p in (d.store.output_target.parent/'history').glob('*.json'))


@pytest.mark.parametrize('action',['unchecked','cancel','busy'])
def test_no_confirmed_action_no_changes(dialog,monkeypatch,action):
    d=dialog;calls=setup(d,monkeypatch,QMessageBox.StandardButton.Cancel)
    d.save_draft();saved=d.store.draft_path.read_bytes();before=d.document.to_dict()
    d.confirm.setChecked(action!='unchecked');d.busy=action=='busy'
    d.approve_button.click();d.busy=False
    assert len(calls)==(1 if action=='cancel' else 0)
    assert not d.store.output_target.exists()
    assert d.store.draft_path.read_bytes()==saved and d.document.to_dict()==before


@pytest.mark.parametrize('warning',['repetition','missing_section','pending_name','missing_all_timing'])
def test_quality_evidence_is_retained_without_approval_gate(dialog,monkeypatch,warning):
    d=dialog;calls=setup(d,monkeypatch)
    if warning=='repetition':
        d.document.metadata['held_passages'].append({'status':'held','detector':'repetition','text':'raw raw raw','start':0,'end':120})
    elif warning=='missing_section':
        d.document.metadata['recognition_regions'].append({'start':290,'end':300,'text':'raw missing words'})
    elif warning=='pending_name':
        d.document.metadata['vocabulary_review']={'source_sha256':d.store.digest,'suggestions':[{'id':'test','status':'pending','replacement':'Name'}]}
    else:
        d.document.segments=[];d.document.metadata['transcription_only']=True
    before=d.document.to_dict()
    d.confirm.setChecked(True);d.approve_button.click()
    assert len(calls)==1 and is_approved(d.document),d.status.text()
    assert d.document.to_dict()['segments']==before['segments']
    for key in ('held_passages','recognition_regions','vocabulary_review','transcription_only'):
        assert d.document.metadata.get(key)==before.get(key)
    assert not d.document.metadata.get('instrumental')


def test_real_io_failure_is_not_success(dialog,monkeypatch):
    d=dialog;calls=setup(d,monkeypatch);before=d.document.to_dict();emitted=[]
    d.approved.connect(lambda *a:emitted.append(a))
    import gnb_player.lyric_review as core
    monkeypatch.setattr(core.tempfile,'mkstemp',lambda **k:(_ for _ in ()).throw(OSError('No space left on device')))
    d.confirm.setChecked(True);d.approve_button.click()
    assert len(calls)==1 and not emitted and not d.store.output_target.exists()
    assert 'No space left on device' in d.status.text() and d.document.to_dict()==before


def test_as_is_acceptance_cannot_transfer_after_edit_or_to_another_track():
    doc=LyricsDocument([Cue('one',1,2,[Word('one',1,2)])],5,source_sha256='a'*64)
    request=accept_current_request(doc)
    assert validate_acceptance(doc,request)
    for key in ('text','time','source','evidence'):
        changed=copy.deepcopy(doc)
        if key=='text':changed.segments[0].text='two'
        elif key=='time':changed.segments[0].end=2.5
        elif key=='source':changed.source_sha256='b'*64
        else:changed.metadata['held_passages']=[{'text':'new','status':'held'}]
        with pytest.raises(ValueError):validate_acceptance(changed,request)
