import json,os,time
from pathlib import Path
import pytest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND','ffmpeg')
from PySide6.QtWidgets import QApplication
from gnb_player.app import MainWindow
from gnb_player.library import Track
from gnb_player.lyrics import LyricsDocument,Cue,save_lyrics,fingerprint
from gnb_player.settings import write_profile

@pytest.fixture
def window(tmp_path):
    app=QApplication.instance() or QApplication([])
    w=MainWindow(tmp_path/'state')
    yield w
    w._closing=True
    for task in w.tasks:task.wait(5000)
    w.close();app.processEvents()

def test_flagged_or_missing_lyrics_are_not_ready(window,tmp_path):
    track=Track(tmp_path/'recording.mp3','Recording');track.path.write_bytes(b'fixture')
    doc=LyricsDocument([Cue('Held words',1,2)],3,source_sha256=fingerprint(track.path),quality={'status':'review'})
    path=tmp_path/'held.json';save_lyrics(doc,path);window.lyric_files[str(track.path)]=str(path)
    assert not window._has_lyrics(track)
    window.documents.clear();path.unlink();assert not window._has_lyrics(track)

def test_appearance_roundtrip_and_stream_key_never_saved(window,tmp_path):
    window.brand.setText('Night Station');window.interval.setValue(33);window.motion.setChecked(False);window.wave.setChecked(False)
    window.stream_key.setText('PRIVATE-KEY-TEST');window.destination.setText('rtmps://host.invalid/app/PRIVATE-KEY-TEST')
    window.save_session();settings=(window.data_dir/'settings.json').read_text();session=(window.data_dir/'session.json').read_text()
    assert 'PRIVATE-KEY-TEST' not in settings+session
    window.motion.setChecked(True);window.load_profile(window.data_dir/'session.json')
    assert window.interval.value()==33 and not window.motion.isChecked() and not window.wave.isChecked()

def test_same_track_signal_does_not_clear_live_audio_or_lyrics(window,tmp_path,monkeypatch):
    track=Track(tmp_path/'recording.mp3','Recording');window._displayed_track=track.path
    called=[];monkeypatch.setattr(window.export,'clear_audio',lambda:called.append('audio'))
    monkeypatch.setattr(window.stage,'set_lyrics',lambda d:called.append('lyrics'))
    window._track_changed(track);assert called==[]

def test_sidecar_lookup_returns_a_document(window,tmp_path):
    source=tmp_path/'song.mp3';source.write_bytes(b'fixture')
    doc=LyricsDocument([Cue('Words stay here',1,2)],3,source_sha256=fingerprint(source))
    save_lyrics(doc,source.with_suffix('.lyrics.json'))
    loaded=window._find_document(Track(source,'Song'))
    assert isinstance(loaded,LyricsDocument) and loaded.segments[0].text=='Words stay here'

def test_replaced_recording_cannot_restore_cached_lyrics(window,tmp_path):
    source=tmp_path/'changed.mp3';source.write_bytes(b'original')
    track=Track(source,'Changed');doc=LyricsDocument([Cue('Original words',1,2)],3,source_sha256=fingerprint(source))
    sidecar=source.with_suffix('.lyrics.json');save_lyrics(doc,sidecar);window.lyric_files[str(source)]=str(sidecar)
    window._remember_document(track,doc);assert window._has_lyrics(track)
    source.write_bytes(b'replacement recording')
    assert not window._has_lyrics(track) and str(source) not in window.documents
    with pytest.raises(ValueError):window._find_document(track)
    window._remember_document(track,None)
    assert str(source) not in window.documents and not window._has_lyrics(track)

def test_keyboard_slider_seeks_without_feedback_from_position_updates(window,monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    values=[]
    monkeypatch.setattr(type(window.engine),'duration',property(lambda engine:100.0))
    monkeypatch.setattr(window.engine,'seek',values.append)
    window.seek.setValue(5000);QTest.keyClick(window.seek,Qt.Key.Key_Right)
    assert len(values)==1 and values[0]>50
    window._position(10)
    assert len(values)==1
