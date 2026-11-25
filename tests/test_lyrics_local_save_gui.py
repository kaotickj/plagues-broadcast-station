"""Native operator routes for local Save lyrics; disposable data, no playback."""
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QLineEdit, QPushButton

import gnb_player.app as operator
from gnb_player import __version__
from gnb_player.app import MainWindow
from gnb_player.library import Track
from gnb_player.lyrics import Cue, LyricsDocument, cached_lyrics_path, fingerprint, load_lyrics, save_lyrics
from gnb_player.lyric_review import is_approved
from gnb_player.settings import read_profile
from test_app import window


def settle(window):
    deadline=time.monotonic()+6
    while time.monotonic()<deadline:
        QApplication.processEvents()
        if not window.tasks and not window._saving_lyrics:return
        QTest.qWait(10)
    raise AssertionError('Fixture lyric loading/saving did not complete')


def same_title_recordings(window,tmp_path):
    tracks=[];targets=[]
    for index in range(2):
        recording=tmp_path/f'take-{index}.mp3'
        recording.write_bytes(f'disposable recording identity {index}'.encode())
        track=Track(recording,'The same displayed song title',duration=12)
        document=LyricsDocument([Cue('First original line',1,3),Cue('Second original line',4,6),
                                 Cue('Third original line',7,9)],12,track.title,
                                fingerprint(recording),quality={'status':'review'},
                                metadata={'custom_vocabulary':{'accent':'coração'}})
        target=cached_lyrics_path(recording,Path(window.extract_config.cache_dir))
        save_lyrics(document,target);tracks.append(track);targets.append(target)
    window.all_tracks=tracks;window._apply_queue()
    for track,target in zip(tracks,targets):window._lyrics_ready(track,target,load_lyrics(target))
    window.tabs.setCurrentIndex(1);window.show();QApplication.processEvents();settle(window)
    assert targets[0]!=targets[1] and fingerprint(tracks[0].path)!=fingerprint(tracks[1].path)
    return tracks,targets


def select_row(window,index):
    item=window.playlist.item(index)
    QTest.mouseClick(window.playlist.viewport(),Qt.MouseButton.LeftButton,
                     pos=window.playlist.visualItemRect(item).center())
    settle(window)


def paste_line(window,row,text):
    table=window.lyric_text;table.setCurrentCell(row,2);table.setFocus()
    QTest.keyClick(table,Qt.Key.Key_F2);QApplication.processEvents()
    editor=QApplication.focusWidget();assert isinstance(editor,QLineEdit)
    QApplication.clipboard().setText(text)
    QTest.keyClick(editor,Qt.Key.Key_A,Qt.KeyboardModifier.ControlModifier)
    QTest.keyClick(editor,Qt.Key.Key_V,Qt.KeyboardModifier.ControlModifier)
    assert editor.text()==text


def trap_save_picker(monkeypatch):
    calls=[]
    def unexpected_picker(*args,**kwargs):
        calls.append(args)
        return ('','')
    monkeypatch.setattr(QFileDialog,'getSaveFileName',unexpected_picker)
    return calls


def test_local_save_button_uses_recording_identity_and_reopens_unicode_lines(window,tmp_path,monkeypatch):
    tracks,targets=same_title_recordings(window,tmp_path)
    originals=[p.read_bytes() for p in targets];audio_hashes=[fingerprint(t.path) for t in tracks]
    dialogs=trap_save_picker(monkeypatch)
    assert window.save_lyrics_button.text()=='Save lyrics'
    assert window.export_lyrics_button.text()=='Export lyrics…'
    select_row(window,1)
    lines=['Café déjà vu — γειά σου','夜のブルース — снова домой','Última línea: coração ♥']
    for row,text in enumerate(lines):paste_line(window,row,text)
    assert window.save_lyrics_button.isEnabled()
    QTest.mouseClick(window.save_lyrics_button,Qt.MouseButton.LeftButton);settle(window)
    assert not dialogs
    saved=load_lyrics(targets[1])
    assert [cue.text for cue in saved.segments]==lines
    assert window.lyric_text.toPlainText()=='\n'.join(lines)
    assert targets[0].read_bytes()==originals[0]
    assert saved.source_sha256==audio_hashes[1] and not is_approved(saved)
    assert saved.metadata['custom_vocabulary']=={'accent':'coração'}
    assert not window._lyric_buffers and window.lyric_save_status.text().startswith('Saved')
    assert read_profile(window.data_dir/'session.json')['lyrics'][str(tracks[1].path)]==str(targets[1])
    second_saved=targets[1].read_bytes()
    select_row(window,0);paste_line(window,0,'Only the selected first recording changes')
    QTest.mouseClick(window.save_lyrics_button,Qt.MouseButton.LeftButton);settle(window)
    assert not dialogs and targets[1].read_bytes()==second_saved
    assert load_lyrics(targets[0]).source_sha256==audio_hashes[0]
    assert [fingerprint(t.path) for t in tracks]==audio_hashes
    restored=MainWindow(window.data_dir)
    try:
        restored.tabs.setCurrentIndex(1);restored.show();QApplication.processEvents()
        select_row(restored,1)
        assert restored.lyric_text.toPlainText()=='\n'.join(lines)
        assert not restored._has_lyrics(tracks[1])
        select_row(restored,0)
        assert restored.lyric_text.item(0,2).text()=='Only the selected first recording changes'
        assert not dialogs
    finally:
        settle(restored);restored._closing=True;restored.close();QApplication.processEvents()


def test_local_save_write_failure_keeps_typed_work_and_retries_without_picker(window,tmp_path,monkeypatch):
    tracks,targets=same_title_recordings(window,tmp_path)
    select_row(window,1);before=targets[1].read_bytes();other=targets[0].read_bytes()
    dialogs=trap_save_picker(monkeypatch)
    text='Édition conservée après échec — 歌詞'
    paste_line(window,0,text)
    def fail_write(*args,**kwargs):raise OSError('Fixture storage is read-only')
    writer=operator.save_review_document
    monkeypatch.setattr(operator,'save_review_document',fail_write)
    QTest.mouseClick(window.save_lyrics_button,Qt.MouseButton.LeftButton);settle(window)
    assert targets[1].read_bytes()==before and targets[0].read_bytes()==other
    assert window.lyric_text.raw_rows()[0][2]==text and window._lyric_buffers
    assert 'Not saved' in window.lyric_save_status.text() and 'read-only' in window.lyric_save_status.text()
    assert window.save_lyrics_button.isEnabled() and not dialogs
    monkeypatch.setattr(operator,'save_review_document',writer)
    QTest.mouseClick(window.save_lyrics_button,Qt.MouseButton.LeftButton);settle(window)
    assert load_lyrics(targets[1]).segments[0].text==text
    assert targets[0].read_bytes()==other and not is_approved(load_lyrics(targets[1]))
    assert not dialogs and not window._lyric_buffers


def test_visible_help_button_opens_current_details_and_keeps_operator_available(window):
    window.show();QApplication.processEvents()
    assert window.help_button.isVisible()
    assert window.help_button.accessibleName()=='Help and app details'
    QTest.mouseClick(window.help_button,Qt.MouseButton.LeftButton);QApplication.processEvents()
    dialog=window._help_dialog
    assert dialog.isVisible() and not dialog.isModal()
    body=dialog.text.toPlainText()
    assert __version__ in body and str(window.data_dir) in body
    assert 'Save lyrics' in body and 'Export lyrics' in body
    assert 'without a filename picker' in body
    assert 'no automatic website or AzuraCast synchronization' in body
    close=dialog.findChild(QPushButton,'help_close');assert close is not None
    QTest.mouseClick(close,Qt.MouseButton.LeftButton);QApplication.processEvents()
    assert not dialog.isVisible() and window.isVisible() and window.isEnabled()
    QTest.mouseClick(window.help_button,Qt.MouseButton.LeftButton);QApplication.processEvents()
    assert window._help_dialog is dialog and dialog.isVisible()
    dialog.close();QApplication.processEvents()
