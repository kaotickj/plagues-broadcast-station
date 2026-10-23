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

class DeferredLyricsTask:
    """Keep background completion deterministic without any model/runtime work."""
    def __init__(self,work,done):
        self.work=work;self.done=done;self.failed=self;self.failures=[]
    def connect(self,callback):self.failures.append(callback)
    def finish(self):self.done(self.work())

@pytest.fixture
def lyric_tasks(window,monkeypatch):
    pending=[]
    def defer(work,done):
        task=DeferredLyricsTask(work,done);pending.append(task);return task
    monkeypatch.setattr(window,'_task',defer)
    return pending

def add_fixture_tracks(window,tmp_path,count=2):
    tracks=[]
    for i in range(count):
        source=tmp_path/f'recording-{i}.mp3';source.write_bytes(f'fixture-{i}'.encode())
        tracks.append(Track(source,f'Recording {i}'))
    window.all_tracks=tracks;window._apply_queue();return tracks

def lyric_fixture(track,status='pass'):
    return LyricsDocument([Cue('Fixture lyric passage',1,2)],3,source_sha256=fingerprint(track.path),quality={'status':status})

def test_completed_alignment_updates_idle_selected_track_and_keeps_selection(window,tmp_path,lyric_tasks):
    tracks=add_fixture_tracks(window,tmp_path);window.playlist.setCurrentRow(1)
    track=tracks[1];doc=lyric_fixture(track);target=tmp_path/'prepared.json';save_lyrics(doc,target)
    window._lyrics_ready(track,target,doc)
    assert window.engine.current_track is None
    assert window._selected_track()==track
    assert 'LYRICS READY' in window.playlist.currentItem().text()
    assert window.lyric_text.toPlainText()=='Fixture lyric passage'
    assert window.lyric_status.text().startswith('Lyrics ready')
    assert window.engine.tracks==tracks
    # A sidecar search started before completion must not erase the fresh result.
    lyric_tasks[0].done(None)
    assert window._has_lyrics(track) and 'LYRICS READY' in window.playlist.currentItem().text()
    assert window.lyric_text.toPlainText()=='Fixture lyric passage'

def test_saved_review_draft_is_visible_but_stays_out_of_ready_filter(window,tmp_path,lyric_tasks):
    track=add_fixture_tracks(window,tmp_path,1)[0];doc=lyric_fixture(track,'review')
    doc.quality['reasons']=[{'code':'hallucination','message':'A passage resembles a common transcription artifact.'}]
    target=tmp_path/'flagged.json';save_lyrics(doc,target);original=target.read_bytes()
    window._lyrics_ready(track,target,doc)
    assert 'LYRICS NEED REVIEW' in window.playlist.item(0).text()
    assert 'MUSIC ONLY' not in window.playlist.item(0).text()
    assert window.lyric_status.text().startswith('Preparation complete · lyrics need review')
    assert 'A passage resembles a common transcription artifact.' in window.lyric_status.text()
    assert window.lyric_text.toPlainText()=='Fixture lyric passage'
    assert not window._has_lyrics(track) and window.stage._lyrics is None
    window.ready_only.setChecked(True)
    assert not window.engine.tracks and window.playlist.count()==0
    assert doc.quality['status']=='review' and target.read_bytes()==original

@pytest.mark.parametrize('reload_existing',[False,True])
def test_async_sidecar_load_refreshes_label_without_resetting_queue(window,tmp_path,lyric_tasks,reload_existing):
    tracks=add_fixture_tracks(window,tmp_path);track=tracks[0]
    window.engine.order.select(0);window.engine.order.bag=[1];window.engine.order.back=[1]
    save_lyrics(lyric_fixture(track),track.path.with_suffix('.lyrics.json'))
    assert 'MUSIC ONLY' in window.playlist.item(0).text()
    if reload_existing:window._reload_current_lyrics()
    else:window._track_changed(track)
    lyric_tasks[-1].finish()
    assert 'LYRICS READY' in window.playlist.item(0).text()
    assert window.engine.current_track==track and window.engine.tracks==tracks
    assert window.engine.order.bag==[1] and window.engine.order.back==[1]
    assert window.lyric_status.text().startswith('Lyrics ready')

@pytest.mark.parametrize('location',['sidecar','cache'])
def test_idle_selection_discovers_existing_lyrics_and_refreshes_its_row(window,tmp_path,lyric_tasks,location):
    from gnb_player.lyrics import cached_lyrics_path
    tracks=add_fixture_tracks(window,tmp_path);track=tracks[1]
    target=track.path.with_suffix('.lyrics.json') if location=='sidecar' else cached_lyrics_path(track.path,window.extract_config.cache_dir)
    save_lyrics(lyric_fixture(track),target)
    window.playlist.setCurrentRow(1);lyric_tasks[-1].finish()
    assert window.engine.current_track is None and window._selected_track()==track
    assert 'LYRICS READY' in window.playlist.currentItem().text()
    assert window.lyric_text.toPlainText()=='Fixture lyric passage'
    assert window.stage._lyrics is None  # Choosing an idle row never switches program output.

def test_completed_background_result_does_not_replace_another_selected_transcript(window,tmp_path,lyric_tasks):
    tracks=add_fixture_tracks(window,tmp_path);window.playlist.setCurrentRow(1)
    selected=lyric_fixture(tracks[1]);window._remember_document(tracks[1],selected);window._display_document(tracks[1],selected)
    other=lyric_fixture(tracks[0]);other.segments[0].text='Other fixture passage'
    target=tmp_path/'other.json';save_lyrics(other,target)
    window._lyrics_ready(tracks[0],target,other)
    assert window._selected_track()==tracks[1]
    assert window.lyric_text.toPlainText()=='Fixture lyric passage'
    assert 'LYRICS READY' in window.playlist.item(0).text()

def test_failed_async_load_refreshes_stale_row(window,tmp_path,lyric_tasks):
    track=add_fixture_tracks(window,tmp_path,1)[0];doc=lyric_fixture(track)
    target=tmp_path/'missing.json';save_lyrics(doc,target);window.lyric_files[str(track.path)]=str(target)
    window._remember_document(track,doc);window._rebuild_list([track])
    assert 'LYRICS READY' in window.playlist.item(0).text()
    window._reload_current_lyrics();target.unlink()
    for callback in lyric_tasks[-1].failures:callback('Fixture unavailable')
    assert 'MUSIC ONLY' in window.playlist.item(0).text()
    assert not window._has_lyrics(track) and not window.lyric_text.toPlainText()

def test_async_result_keeps_replaced_recordings_off_air(window,tmp_path,lyric_tasks):
    track=add_fixture_tracks(window,tmp_path,1)[0];window.engine.order.select(0)
    save_lyrics(lyric_fixture(track),track.path.with_suffix('.lyrics.json'))
    window._track_changed(track);task=lyric_tasks[-1];previous=task.work()
    track.path.write_bytes(b'a replaced fixture recording')
    task.done(previous)
    assert not window._has_lyrics(track) and window.stage._lyrics is None
    assert 'MUSIC ONLY' in window.playlist.item(0).text()

def test_resume_restores_playing_transcript_without_reusing_changed_audio(window,tmp_path,lyric_tasks):
    track=add_fixture_tracks(window,tmp_path,1)[0];window.engine.order.select(0)
    document=lyric_fixture(track);window._remember_document(track,document)
    window._playing(True)
    assert window.stage._lyrics is document
    track.path.write_bytes(b'another fixture version')
    window._playing(True)
    assert window.stage._lyrics is None and not window._has_lyrics(track)

def test_async_review_draft_refresh_keeps_ready_only_queue_strict(window,tmp_path,lyric_tasks):
    track=add_fixture_tracks(window,tmp_path,1)[0];target=track.path.with_suffix('.lyrics.json')
    save_lyrics(lyric_fixture(track),target);window.lyric_files[str(track.path)]=str(target)
    window.ready_only.setChecked(True)
    assert window.engine.tracks==[track]
    save_lyrics(lyric_fixture(track,'review'),target);original=target.read_bytes()
    window._reload_current_lyrics();lyric_tasks[-1].finish()
    assert not window.engine.tracks and not window._has_lyrics(track)
    assert window.documents[str(track.path)].quality['status']=='review'
    assert target.read_bytes()==original

def test_passing_result_preserves_playing_queue_and_independent_selection(window,tmp_path,lyric_tasks,monkeypatch):
    tracks=add_fixture_tracks(window,tmp_path);track=tracks[0]
    window.engine.order.select(0);window.engine.order.bag=[1];window.engine.order.back=[1]
    window.engine._playing=True;window._displayed_track=track.path;window.playlist.setCurrentRow(1)
    audio_resets=[];monkeypatch.setattr(window.export,'clear_audio',lambda:audio_resets.append(True))
    document=lyric_fixture(track);target=tmp_path/'playing.json';save_lyrics(document,target)
    window._lyrics_ready(track,target,document)
    assert window.engine.playing and window.engine.current_track==track
    assert window.engine.tracks==tracks and window.engine.order.bag==[1] and window.engine.order.back==[1]
    assert window._selected_track()==tracks[1] and not audio_resets
    assert window.stage._lyrics is document and 'LYRICS READY' in window.playlist.item(0).text()


def test_waveform_color_picker_persists_profile_and_cancel_preserves_choice(window,tmp_path,monkeypatch):
    from PySide6.QtGui import QColor
    monkeypatch.setattr('gnb_player.app.QColorDialog.getColor',lambda *args:QColor('#39FF14'))
    window.wave_color.click()
    assert window.stage._settings['waveform_color']=='#39ff14'
    assert '#39FF14' in window.wave_color.text()
    window.save_session()
    saved=json.loads((window.data_dir/'session.json').read_text())
    assert saved['settings']['waveform_color']=='#39ff14'
    window.set_waveform_color('#0088ff')
    window.load_profile(window.data_dir/'session.json')
    assert window.visual_settings()['waveform_color']=='#39ff14'
    monkeypatch.setattr('gnb_player.app.QColorDialog.getColor',lambda *args:QColor())
    window.wave_color.click()
    assert window.visual_settings()['waveform_color']=='#39ff14'


def test_legacy_playlist_resets_waveform_color_to_original_gold(window,tmp_path):
    legacy=tmp_path/'legacy.json';write_profile(legacy,[],[],{},settings={'brand':'Old station'})
    window.set_waveform_color('#39ff14');window.load_profile(legacy)
    assert window.visual_settings()['waveform_color']=='#dfbd7f'
    assert window.stage._settings['waveform_color']=='#dfbd7f'
