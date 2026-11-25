import copy
import json
import pytest
from test_qwen_review_flow import source, dialog
from test_qwen_automatic_ui import stage, feed
from gnb_player.review_sheet import bracket_preview, make_sheet, SheetStore, alignment_plan, embedded_instrumental_candidate
from gnb_player.supplied_text_contract import prepared_text
from gnb_player.lyrics import LyricsDocument, Cue, Word
from gnb_player.review_core import ReviewStore

SHEET = '[Intro: piano answers]\nMy own line\n[Chorus]\nMy own line\n[oh baby]\nSing [with me] now\n'

def sheet(d, original=SHEET):
    preview, rows = bracket_preview(original)
    return make_sheet(original, preview, rows, source_sha256=d.store.digest,
                      duration=d.document.duration, title=d.track.title)

def test_direction_preview_retains_repeats_lines_ambiguous_words_and_original():
    text, rows = bracket_preview(SHEET)
    assert text == '\nMy own line\n\nMy own line\noh baby\nSing with me now\n'
    assert [r['action'] for r in rows] == ['exclude', 'exclude', 'keep', 'keep']
    text, rows = bracket_preview(SHEET, {2: 'exclude', 0: 'keep'})
    assert text.startswith('Intro: piano answers\n') and '\noh baby\n' not in text
    assert SHEET.startswith('[Intro')
    assert bracket_preview('A [unfinished')[0] == 'A [unfinished'
    assert bracket_preview('[unknown words]')[0] == 'unknown words'

@pytest.mark.parametrize('text,expected', [
    ('[Intro: piano]\n[tenor sax and slow brushed drums]\n[No vocals]',True),
    ('[oh baby]',True),
    ('[Intro]\nActual sung words',False),
    ('[unclosed',False),
    ('[]',False),
    ('',False),
])
def test_bracket_only_is_candidate_not_proof(text,expected):
    assert embedded_instrumental_candidate({'text':text}) is expected


def test_conflicting_embedded_sources_are_not_instrumental_candidates():
    assert not embedded_instrumental_candidate({
        'text':'[Instrumental]',
        'sources':[{'text':'[Instrumental]'},{'text':'Actual sung words'}]})
    assert not embedded_instrumental_candidate({'kind':'probe_failed','text':'[Instrumental]'})


def test_private_sheet_save_restart_history_no_draft_or_output_changes(dialog):
    d = dialog; doc = LyricsDocument([Cue('old', 1, 2, [Word('old', 1, 2)])], 300, source_sha256=d.store.digest)
    d.set_document(doc); d.save_draft(); old = d.store.draft_path.read_bytes()
    store = SheetStore(d.store); value = sheet(d); store.save(value)
    assert SheetStore(ReviewStore(d.data_dir, d.track.path)).load() == value
    second = {**value, 'sung_text': value['sung_text'] + 'Another line\n'}; store.save(second)
    history = list((d.store.root/'history').glob('*supplied-sheet.json'))
    assert len(history) == 1 and json.loads(history[0].read_text()) == value
    assert d.store.draft_path.read_bytes() == old and not d.store.output_target.exists()

def test_same_title_different_recording_rejected_and_concurrent_sheet_preserved(dialog, tmp_path):
    d = dialog; one = SheetStore(d.store); two = SheetStore(d.store)
    one.save(sheet(d))
    with pytest.raises(ValueError, match='another window'): two.save(sheet(d))
    other = tmp_path/'alternate.wav'; other.write_bytes(b'other version')
    target = ReviewStore(tmp_path/'otherdata', other)
    with pytest.raises(ValueError, match='different recording'): SheetStore(target).save(sheet(d))
    assert not (target.root/'supplied-sheet.json').exists()

def test_save_failure_is_honest(dialog, monkeypatch):
    import gnb_player.review_sheet as module
    store = SheetStore(dialog.store); store.save(sheet(dialog)); before = store.path.read_bytes()
    monkeypatch.setattr(module, 'atomic_json', lambda *a: (_ for _ in ()).throw(OSError('disk failure')))
    with pytest.raises(OSError, match='disk failure'): store.save({**sheet(dialog), 'sung_text': 'New'})
    assert store.path.read_bytes() == before

@pytest.mark.parametrize('duration,expected', [(60,'qwen'),(180,'qwen'),(180.01,'heart_ctc'),(221.28325,'heart_ctc'),(899,'heart_ctc')])
def test_duration_route_uses_whole_audio_without_fabricated_windows(dialog,duration,expected):
    d=dialog; d.document.duration=duration
    backend,reason,request=alignment_plan(sheet(d),d.document,'qwen')
    assert backend==expected and 'alignment_windows' not in request
    assert request['reference_original']==SHEET and request['repeat_override']
    assert prepared_text({**request,'mode':'align','source_sha256':d.store.digest})==sheet(d)['sung_text']
    assert request['reference_text'].count('\n')==SHEET.count('\n')

def test_input_mismatch_missing_and_unbounded_duration_rejected(dialog):
    d=dialog; value=sheet(d)
    with pytest.raises(ValueError,match='empty'):alignment_plan({**value,'sung_text':' '},d.document,'qwen')
    with pytest.raises(ValueError,match='duration changed'):alignment_plan({**value,'duration_at_save':100},d.document,'qwen')
    _,_,r=alignment_plan(value,d.document,'qwen');r.update(mode='align',source_sha256=d.store.digest)
    with pytest.raises(ValueError,match='differs'):prepared_text({**r,'reference_text':'other words'})
    with pytest.raises(ValueError,match='different recording|another recording'):prepared_text({**r,'source_sha256':'wrong'})
    d.document.duration=901
    with pytest.raises(ValueError,match='15 minutes'):alignment_plan(value,d.document,'qwen')

def test_new_ui_save_without_models_cancel_and_seek(dialog,monkeypatch):
    from gnb_player.review_sheet_dialog import SuppliedLyricsDialog
    d=dialog; original_doc=copy.deepcopy(d.document.to_dict()); requests=stage(d,monkeypatch)
    ui=SuppliedLyricsDialog(d); ui.original.setPlainText(SHEET)
    assert ui.preview.toPlainText()==bracket_preview(SHEET)[0]
    assert 'CTC' in ui.route.text() and '221' not in ui.route.text()
    ui.slider.sliderMoved.emit(19234);ui.render_clock();assert '19.234' in ui.clock.text() and d.player.position()==19234
    ui.preview.setPlainText('My edited line\nSecond line\n');ui.save_button.click()
    assert ui.store.load()['sung_text']=='My edited line\nSecond line\n' and not requests
    ui.reject();assert d.document.to_dict()==original_doc and not d.store.output_target.exists()
    reopened=SuppliedLyricsDialog(d)
    assert reopened.original.toPlainText()==SHEET and reopened.preview.toPlainText()=='My edited line\nSecond line\n'
    reopened.original.appendPlainText('Changed original')
    assert reopened.preview.toPlainText()=='My edited line\nSecond line\n'
    reopened.reject()

def test_exact_save_align_flow_no_extra_warning_and_no_asr(dialog,monkeypatch):
    import gnb_player.review_dialog as module
    from gnb_player.review_sheet_dialog import SuppliedLyricsDialog
    d=dialog; d.document.duration=221.28325
    d.document.segments=[Cue('old nonsense',3,4,[Word('old nonsense',3,4)])]
    before=copy.deepcopy(d.document.to_dict());requests=stage(d,monkeypatch)
    monkeypatch.setattr(module.QMessageBox,'question',lambda *a:(_ for _ in ()).throw(AssertionError('No extra confirmation')))
    ui=SuppliedLyricsDialog(d);ui.original.setPlainText(SHEET);ui.use_button.click()
    assert ui.saved_for_alignment is not None
    d.start_job('align',source='sheet',supplied_sheet=ui.saved_for_alignment)
    assert len(requests)==1 and requests[0]['mode']=='align' and requests[0]['config']['backend']=='heart_ctc'
    assert d.config.backend=='qwen' and requests[0]['reference_text']==ui.preview.toPlainText()
    assert d.document.to_dict()==before and not d.store.output_target.exists()
    assert not d.paste_button.isEnabled()
    d.set_busy(False)

def test_qwen_short_sheet_worker_preserves_provenance_and_brackets(dialog,monkeypatch,tmp_path):
    import sys
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'src/gnb_player'))
    from review_qwen_worker import run_qwen
    d=dialog; d.document.duration=60
    value=sheet(d,'[Verse]\nOh [baby]\n')
    value['sung_text']='Oh [baby]\n' # user explicitly keeps brackets in editable preview
    _,_,r=alignment_plan(value,d.document,'qwen')
    r.update(mode='align',source_sha256=d.store.digest,title=d.track.title,decoded_duration=60,
             config={'align_python':'/fixture','qwen_aligner_model':'/fixture'})
    assert prepared_text(r)=='Oh [baby]\n'
    calls=[]
    def call(stage,args,timeout):
        calls.append(stage)
        # Fail before inference; the worker must retain supplied request unchanged.
        raise RuntimeError('fixture cancellation')
    with pytest.raises(RuntimeError,match='cancellation'):run_qwen(r,tmp_path,{},call)
    assert calls==['qwen-align']
    saved=json.loads((tmp_path/'alignment-request.json').read_text())
    assert saved['supplied_sheet']==value and saved['reference_text']=='Oh [baby]\n'
