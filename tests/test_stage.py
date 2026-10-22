import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QImage,QColor

from gnb_player.stage import BroadcastStage


@pytest.fixture(scope='module')
def qapp():
    app=QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def stage(qapp):
    widget=BroadcastStage()
    yield widget
    widget.shutdown()
    widget.close()


def _art(path:Path,color:str):
    image=QImage(640,360,QImage.Format.Format_RGB32)
    image.fill(QColor(color));assert image.save(str(path))
    return path


def test_offscreen_compositor_renders_requested_frame_and_captions(stage):
    word=SimpleNamespace(text='Whiskey',start=2,end=3)
    cue=SimpleNamespace(text='Whiskey',start=1,end=4,words=[word])
    stage.set_track('Red Rocket Whiskey','Goodneighbor Blues','Whiskey Nights')
    stage.set_lyrics(SimpleNamespace(segments=[cue]))
    stage.set_playback(2.5,120,False)
    stage.set_waveform(np.sin(np.linspace(0,20,2048))*.2)
    frame=stage.render_frame(1920,1080)
    assert not frame.isNull() and frame.size().width()==1920 and frame.size().height()==1080
    assert stage._lyric_rows()[0] is cue


def test_paused_playback_holds_visual_and_lyric_clock(stage):
    stage.set_playback(12,100,False)
    before=stage._visual_seconds
    with patch('gnb_player.stage.time.monotonic',return_value=stage._last_tick+5):
        stage._tick()
        assert stage._visual_seconds==before
        assert stage.position==12
    stage.set_offset(-2)
    assert stage.lyric_position==10


def test_actual_audio_changes_waveform_and_zero_audio_stays_flat(stage):
    stage.set_playback(0,100,True)
    stage.set_waveform(np.ones(4096,dtype=np.float32)*.25)
    stage._tick()
    assert np.all(stage._wave_display>0)
    assert np.max(stage._wave_display)<.25
    stage.set_waveform(np.zeros(4096,dtype=np.float32))
    assert np.max(stage._wave_target)==0
    stage.set_waveform([float('nan'),float('inf')])
    assert np.isfinite(stage._wave_target).all()


def test_invalid_art_keeps_last_decoded_background(stage,tmp_path):
    art=_art(tmp_path/'blue.png','#123499')
    bad=tmp_path/'bad.png';bad.write_bytes(b'not an image')
    stage.set_backgrounds([art]);assert stage.current_background==art
    original=stage._backdrop.image.cacheKey()
    stage.set_backgrounds([bad])
    assert stage.current_background==art
    assert stage._backdrop.image.cacheKey()==original
    assert not stage.render_frame(640,360).isNull()


def test_shuffle_does_not_repeat_immediately_and_previous_restores(stage,tmp_path):
    a=_art(tmp_path/'a.png','#123499');b=_art(tmp_path/'b.png','#993412')
    stage.set_backgrounds([a,b]);assert stage.current_background==a
    stage.next_background();assert stage.current_background==b
    stage.previous_background();assert stage.current_background==a


def test_explicit_empty_backgrounds_clear_the_scene(stage,tmp_path):
    art=_art(tmp_path/'blue.png','#123499')
    stage.set_backgrounds([art]);assert stage.current_background==art
    stage.set_backgrounds([]);assert stage.current_background is None
    assert stage._backdrop.image is None


def test_line_only_lyrics_do_not_invent_word_timestamps(stage):
    cue=SimpleNamespace(text='A long and lonesome road',start=1,end=4,words=[])
    stage.set_lyrics(SimpleNamespace(segments=[cue]))
    stage.set_playback(2,20,False)
    before=list(cue.words)
    frame=stage.render_frame(1280,720)
    assert not frame.isNull() and cue.words==before
    stage.set_playback(7,20,False)
    assert stage._lyric_rows()[0] is None
    assert stage._lyric_rows()[1]==cue.text


def test_out_of_range_input_is_bounded_and_video_frame_can_be_cleared(stage):
    stage.set_visual_settings(interval_seconds=-5,fade_seconds=float('nan'),brand='Test')
    assert stage._settings['interval_seconds']==5
    assert stage._settings['fade_seconds']==1.8
    stage.set_offset(float('inf'));assert stage._offset==0
    image=QImage(8,8,QImage.Format.Format_RGB32);image.fill(QColor('#225544'))
    stage.set_video_frame(image);assert stage._song_frame is not None
    stage.set_video_frame(None);assert stage._song_frame is None
    assert not stage.render_frame(1,1).isNull()


def test_closing_presentation_does_not_freeze_native_composition(stage):
    stage.set_playback(1,100,True)
    stage.close()
    assert not stage._disposed and stage._timer.isActive()
    before=stage._visual_seconds
    with patch('gnb_player.stage.time.monotonic',return_value=stage._last_tick+.1):stage._tick()
    assert stage._visual_seconds>before
    stage.shutdown();assert stage._disposed and not stage._timer.isActive()
