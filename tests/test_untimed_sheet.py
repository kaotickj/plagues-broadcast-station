import base64, copy, json, subprocess
from pathlib import Path
import pytest
from test_qwen_review_flow import source, dialog
from test_qwen_automatic_ui import stage
from gnb_player.review_sheet import bracket_preview, make_sheet, untimed_document, SheetStore, import_text_file
from gnb_player.review_sheet_dialog import SuppliedLyricsDialog
from gnb_player.review_core import ReviewStore, embedded_lyrics
from gnb_player.lyrics import LyricsDocument, Cue, Word
from gnb_player.lyric_review import is_approved

TEXT='[Verse 1: AAB]\nHello [whispered] world\n[editorial note: leave space]\nHello world\n[oh baby]\n'

def test_inline_styles_editorial_notes_and_ambiguous_words():
    clean, rows=bracket_preview(TEXT)
    assert clean=='\nHello world\n\nHello world\noh baby\n'
    assert [r['action'] for r in rows]==['exclude','exclude','exclude','keep']
    assert bracket_preview('one[style: blues]two')[0]=='one two'
    assert bracket_preview('[note: first\nsecond]\nline')[0]=='\n\nline'
    assert bracket_preview('[unfinished')[0]=='[unfinished'
    assert bracket_preview(TEXT,{1:'keep'})[0].splitlines()[1]=='Hello whispered world'

def test_file_import_preserves_bytes_encoding_linebreaks_and_file(tmp_path):
    path=tmp_path/'sheet.txt';raw=b'\xef\xbb\xbf[Verse]\r\nMy line\r\nMy line\r\n';path.write_bytes(raw)
    text,origin=import_text_file(path)
    assert text=='[Verse]\r\nMy line\r\nMy line\r\n'
    assert base64.b64decode(origin['original_bytes_base64'])==path.read_bytes()==raw
    assert origin['trusted'] is False
    path.write_bytes(b'\xff\xffbad')
    with pytest.raises(ValueError,match='UTF'):import_text_file(path)

def test_real_mp3_embedded_probe_is_read_only(tmp_path):
    source=tmp_path/'embedded.mp3'
    subprocess.run(['ffmpeg','-v','error','-nostdin','-f','lavfi','-i','anullsrc=r=8000:cl=mono',
        '-t','0.05','-c:a','libmp3lame','-metadata','lyrics='+TEXT,str(source)],check=True,timeout=15)
    before=source.read_bytes();result=embedded_lyrics(source)
    assert source.read_bytes()==before
    assert result['trusted'] is False and result['sources'][0]['text']==TEXT
    assert result['sources'][0]['location']=='format.tags'

def test_embedded_to_untimed_save_reload_undo_without_model_or_output(dialog,monkeypatch):
    d=dialog;requests=stage(d,monkeypatch)
    original=LyricsDocument([Cue('Existing',1,2,[Word('Existing',1,2)])],300,source_sha256=d.store.digest)
    d.set_document(original);d.store.save_draft(original)
    d.embedded={'text':TEXT,'tags':{'lyrics':TEXT},'trusted':False}
    ui=SuppliedLyricsDialog(d);ui.embedded_button.click()
    assert ui.original.toPlainText()==TEXT and ui.preview.toPlainText()==bracket_preview(TEXT)[0]
    ui.untimed_button.click();d.use_untimed_sheet(ui.saved_for_untimed)
    assert not requests and not d.document.segments and not d.store.output_target.exists()
    assert d.document.untimed_text==bracket_preview(TEXT)[0]
    assert d.untimed_view.toPlainText()==d.document.untimed_text and d.lyric_views.currentIndex()==1
    assert 'No timed cues' in d.span_label.text() and d.document.active_word(45) is None
    loaded=ReviewStore(d.data_dir,d.track.path).load()
    assert loaded.to_dict()==d.document.to_dict() and loaded.metadata['supplied_sheet']['import_source']['selected']['text']==TEXT
    assert list((d.store.root/'history').glob('*draft.json'))
    d.undo_edit();assert d.document.to_dict()['segments']==original.to_dict()['segments']
    assert not d.document.untimed_text and not is_approved(d.document)

def test_untimed_local_approval_exact_text_no_sync_or_fabricated_cues(dialog,monkeypatch):
    import gnb_player.review_dialog as module
    d=dialog;ui=SuppliedLyricsDialog(d);ui.original.setPlainText(TEXT);ui.untimed_button.click()
    d.use_untimed_sheet(ui.saved_for_untimed);d.confirm.setChecked(True)
    questions=[]
    monkeypatch.setattr(module.QMessageBox,'question',lambda *a:(questions.append(a[2]),module.QMessageBox.StandardButton.Ok)[1])
    d.approve();assert len(questions)==1 and 'untimed' in questions[0]
    saved=LyricsDocument.from_dict(json.loads(d.store.output_target.read_text()))
    assert is_approved(saved,d.store.digest) and saved.untimed_text and not saved.segments
    assert saved.active_cue(0) is None and saved.active_word(20) is None
    from gnb_player.app import MainWindow
    from types import SimpleNamespace
    fake=SimpleNamespace(engine=SimpleNamespace(current_track=d.track),_document_ready=lambda *a:True)
    assert MainWindow._program_publication_valid(fake,saved) is False

def test_untimed_can_save_unknown_duration_and_rejects_mixed_timing(dialog,monkeypatch):
    d=dialog;d.document.duration=0
    ui=SuppliedLyricsDialog(d);ui.original.setPlainText('A line\nAgain\n');ui.untimed_button.click()
    d.use_untimed_sheet(ui.saved_for_untimed);assert d.store.load().untimed_text=='A line\nAgain\n'
    import gnb_player.review_dialog as module
    monkeypatch.setattr(module.QMessageBox,'question',lambda *a:module.QMessageBox.StandardButton.Ok)
    d.confirm.setChecked(True);d.approve();assert is_approved(d.document) and not d.document.segments
    raw=d.document.to_dict();raw['segments']=[{'text':'wrong','start':0,'end':1,'words':[]}]
    with pytest.raises(ValueError,match='must not contain'):LyricsDocument.from_dict(raw)
    raw=d.document.to_dict();raw['source_sha256']='1'*64
    with pytest.raises(ValueError,match='recording-bound'):LyricsDocument.from_dict(raw)

def test_source_replacement_cancel_and_cleanup_are_reversible(dialog,monkeypatch):
    import gnb_player.review_sheet_dialog as module
    d=dialog;d.embedded={'text':TEXT,'tags':{'lyrics':TEXT},'trusted':False}
    ui=SuppliedLyricsDialog(d);ui.original.setPlainText('My unsaved sheet')
    monkeypatch.setattr(module.QMessageBox,'question',lambda *a:module.QMessageBox.StandardButton.Cancel)
    ui.embedded_button.click();assert ui.original.toPlainText()=='My unsaved sheet'
    monkeypatch.setattr(module.QMessageBox,'question',lambda *a:module.QMessageBox.StandardButton.Ok)
    ui.embedded_button.click();ui.exclude_all_button.click()
    assert 'oh baby' not in ui.preview.toPlainText() and '[oh baby]' in ui.original.toPlainText()
    ui.save_button.click();saved=SheetStore(d.store).load()
    assert saved['original']==TEXT and saved['import_source']['selected']['text']==TEXT
    ui.reject();assert not d.document.segments and not d.store.draft_path.exists()
