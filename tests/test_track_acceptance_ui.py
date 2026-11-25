"""Actual saved-case review interactions; fake media, no recognition or live data."""
import copy
import json
from pathlib import Path

from PySide6.QtWidgets import QMessageBox
from test_qwen_review_flow import source, dialog
from gnb_player.lyrics import LyricsDocument, Cue, Word, load_lyrics
from gnb_player.lyric_review import is_approved


def saved_case(d):
    raw=json.loads((Path(__file__).parents[2]/'evidence/owner-saved-draft-readonly.json').read_text())
    # Disposable recording identity; native transcript/timings/evidence unchanged.
    raw['source_sha256']=d.store.digest
    return LyricsDocument.from_dict(raw)


def test_saved_case_cancel_approval_has_no_write(dialog,monkeypatch):
    d=dialog;d.set_document(saved_case(d));before=copy.deepcopy(d.document.to_dict());calls=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *a:(calls.append(a[2]),QMessageBox.StandardButton.Cancel)[1])
    d.confirm.setChecked(True);d.approve_button.click()
    assert calls==['Save and use these lyrics and timings as they are?']
    assert d.document.to_dict()==before and not d.store.output_target.exists()
    assert 'cancelled' in d.status.text().lower()


def test_saved_case_one_acceptance_no_per_line_edits_and_roundtrip(dialog,monkeypatch):
    d=dialog;d.set_document(saved_case(d));before=copy.deepcopy(d.document.to_dict());calls=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *a:(calls.append(a[2]),QMessageBox.StandardButton.Ok)[1])
    d.confirm.setChecked(True);d.approve_button.click()
    assert len(calls)==1 and d.store.output_target.exists(),d.status.text()
    restored=load_lyrics(d.store.output_target)
    assert is_approved(restored,d.store.digest)
    assert restored.to_dict()['segments']==before['segments']
    assert restored.metadata['held_passages']==before['held_passages']
    assert 'output_acceptance' in restored.human_review
    d.restore_draft();assert d.status.text().startswith('APPROVED')
    output=d.store.output_target.read_bytes()
    d.open_phrase_correction(0);d.phrase.setPlainText('An optional correction');d.apply_phrase()
    assert not is_approved(d.document) and not d.confirm.isChecked()
    assert d.save_draft() and d.store.output_target.read_bytes()==output


def test_advisory_flags_need_only_basic_confirmation_no_text_edit(dialog,monkeypatch):
    d=dialog
    doc=LyricsDocument([Cue('Correct words',1,2,[Word('Correct',1,1.5,{'flags':['low_ctc_score']}),Word('words',1.5,2)])],300,source_sha256=d.store.digest)
    d.set_document(doc)
    calls=[]
    monkeypatch.setattr(QMessageBox,'question',lambda *a:(calls.append(a[2]),QMessageBox.StandardButton.Ok)[1])
    d.confirm.setChecked(True);d.approve_button.click()
    assert is_approved(d.document),d.status.text()
    assert calls==['Save and use these lyrics and timings as they are?']


def test_recover_saved_groups_individual_preview_undo_no_model(dialog,monkeypatch):
    d=dialog;d.set_document(saved_case(d));before=copy.deepcopy(d.document.to_dict())
    monkeypatch.setattr(d.process,'start',lambda *a:(_ for _ in ()).throw(AssertionError('Recovery must not run a model')))
    d.recover_words_button.click()
    assert d.dirty and not d.confirm.isChecked() and not d.store.output_target.exists()
    cue=d.document.segments[2]
    assert cue.text=='He orders something amber and'
    assert [w.text for w in cue.words]==['He','orders','something','amber']
    assert [w.to_dict() for w in cue.words]==before['segments'][2]['words'][0]['native_constituents']
    for seconds,word in [(29.8,'He'),(30.2,'orders'),(30.5,'something'),(31.2,'amber')]:
        d.seek(seconds)
        assert d.highlight.text().count('text-decoration:underline')==1
        assert ('>'+word+'</span>') in d.highlight.text()
        assert 'and' in d.highlight.text() and '>and</span>' not in d.highlight.text()
    d.seek(32.0);assert 'text-decoration:underline' not in d.highlight.text()
    d.undo_button.click()
    assert d.document.to_dict()['segments']==before['segments']
    assert d.document.metadata['held_passages']==before['held_passages']


def test_saved_native_word_marker_is_not_repetition_or_time_exclusion(dialog):
    d=dialog;d.set_document(saved_case(d))
    labels=[d.jump.itemText(i) for i in range(d.jump.count())]
    marker=next(x for x in labels if 'excluded_by_operator' in x)
    assert 'untimed word: Nobody' in marker and 'processing window' in marker
    assert 'repetition passage' not in marker
    assert 'Untimed word: Nobody' in d.held_select.itemText(0)
    assert 'Processing window:' in d.held_text.toPlainText()
    d.open_phrase_correction(next(i for i,c in enumerate(d.document.segments) if c.text=="like he'd crossed some kind of line. Nobody knows"))
    assert d.player.position()==68280 and d.highlight.text()=='Waiting / no timed lyric span'
