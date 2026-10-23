"""Mouse/keyboard regressions for the release candidate's editing workspace."""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit

from gnb_player.lyrics import load_lyrics
from gnb_player.lyric_review import is_approved
from gnb_player.settings import read_profile
from test_app import window
from test_lyrics_review_ui import pending, open_draft


def type_into_first_line(window, text):
    window.tabs.setCurrentIndex(1)
    window.show()
    QApplication.processEvents()
    table=window.lyric_text
    table.setCurrentCell(0,2)
    table.setFocus()
    QTest.keyClick(table,Qt.Key.Key_F2)
    editor=QApplication.focusWidget()
    assert isinstance(editor,QLineEdit)
    editor.selectAll()
    QTest.keyClicks(editor,text)
    return editor


def test_typing_enables_save_before_cell_focus_leaves(window,tmp_path,pending):
    _,target=open_draft(window,tmp_path,pending)
    before=target.read_bytes()
    editor=type_into_first_line(window,'Words typed directly into the active cell')
    assert QApplication.focusWidget() is editor
    assert window.save_lyrics_button.isEnabled()
    assert target.read_bytes()==before
    count=len(pending)
    QTest.mouseClick(window.save_lyrics_button,Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert len(pending)==count+1
    pending[-1].finish()
    assert load_lyrics(target).segments[0].text=='Words typed directly into the active cell'
    assert not window._lyric_buffers


def test_escape_cancels_only_the_active_cell_typing(window,tmp_path,pending):
    _,target=open_draft(window,tmp_path,pending)
    before=target.read_bytes()
    editor=type_into_first_line(window,'These words should be canceled')
    assert window.save_lyrics_button.isEnabled()
    QTest.keyClick(editor,Qt.Key.Key_Escape)
    QApplication.processEvents()
    assert target.read_bytes()==before
    assert window.lyric_text.item(0,2).text()=='A quiet evening carries me'
    assert not window._lyric_buffers
    assert not window.save_lyrics_button.isEnabled()


def test_resize_and_expand_preserve_active_text_cursor_and_selection(window,tmp_path,pending):
    open_draft(window,tmp_path,pending)
    editor=type_into_first_line(window,'Unsaved words survive expanding the workspace')
    editor.setSelection(8,5)
    position=editor.cursorPosition();selection=editor.selectedText()
    original_height=window.lyric_text.height()
    # Keyboard invocation keeps the in-progress delegate alive while layouts
    # change; a mouse click is also allowed to commit normally on focus-out.
    window.expand_lyrics_button.setChecked(True)
    window._toggle_expanded_lyrics(True)
    window.resize(1030,720)
    QApplication.processEvents()
    assert QApplication.focusWidget() is editor
    assert editor.text()=='Unsaved words survive expanding the workspace'
    assert editor.cursorPosition()==position and editor.selectedText()==selection
    assert not window.monitor_panel.isVisible()
    assert window.lyric_text.height()>=original_height
    assert window.save_lyrics_button.isVisible()
    assert window.save_lyrics_button.geometry().bottom()<=window.save_lyrics_button.parentWidget().height()
    window.tabs.setCurrentIndex(0);QApplication.processEvents()
    assert window.monitor_panel.isVisible()
    window.tabs.setCurrentIndex(1);QApplication.processEvents()
    assert not window.monitor_panel.isVisible()


def test_export_requires_saved_changes_and_writes_separate_copy(window,tmp_path,pending,monkeypatch):
    _,target=open_draft(window,tmp_path,pending)
    export=tmp_path/'separate-export.json'
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *a,**kw:(str(export),'Timed lyric JSON'))
    type_into_first_line(window,'Export this saved correction')
    window._export_saved_lyrics()
    assert not export.exists()
    assert 'Save changes before exporting' in window.lyric_save_status.text()
    window._save_lyric_changes();pending[-1].finish()
    original=target.read_bytes()
    QTest.mouseClick(window.export_lyrics_button,Qt.MouseButton.LeftButton)
    assert load_lyrics(export).segments[0].text=='Export this saved correction'
    assert target.read_bytes()==original
    assert not window._lyric_buffers


def test_export_cannot_overwrite_current_lyric_storage(window,tmp_path,pending,monkeypatch):
    _,target=open_draft(window,tmp_path,pending)
    before=target.read_bytes()
    monkeypatch.setattr(QFileDialog,'getSaveFileName',lambda *a,**kw:(str(target),'Timed lyric JSON'))
    window._export_saved_lyrics()
    assert target.read_bytes()==before
    assert 'Choose a separate file' in window.lyric_save_status.text()


def test_explicit_save_persists_session_link_before_reporting_saved(window,tmp_path,pending):
    tracks,target=open_draft(window,tmp_path,pending)
    window.lyric_text.item(0,2).setText('An immediate restart must retain this correction')
    window._save_lyric_changes();pending[-1].finish()
    # No processEvents or autosave delay: explicit save is the durable boundary.
    session=read_profile(window.data_dir/'session.json')
    assert session['lyrics'][str(tracks[0].path)]==str(target)
    assert window.lyric_save_status.text().startswith('Saved')


def test_session_failure_after_approval_is_retryable_without_rewriting_lyrics(window,tmp_path,pending,monkeypatch):
    tracks,target=open_draft(window,tmp_path,pending)
    window.review_confirm.setChecked(True)
    save_session=window.save_session
    monkeypatch.setattr(window,'save_session',lambda:False)
    window._save_lyric_changes(approve=True);pending[-1].finish()
    saved=target.read_bytes()
    assert is_approved(load_lyrics(target))
    assert 'session save did not complete' in window.lyric_save_status.text()
    assert window._lyric_buffers[str(tracks[0].path)]['session_only']
    assert window.save_lyrics_button.isEnabled()
    monkeypatch.setattr(window,'save_session',save_session)
    count=len(pending)
    window._save_lyric_changes()
    assert len(pending)==count
    assert target.read_bytes()==saved and is_approved(load_lyrics(target))
    assert not window._lyric_buffers
    assert read_profile(window.data_dir/'session.json')['lyrics'][str(tracks[0].path)]==str(target)
