import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import json,math,shutil,subprocess,time

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QColor,QImage,QPainter

from gnb_player.broadcast import (
    AUDIO_FRAME_BYTES,AUDIO_RATE,BroadcastConfig,BroadcastSession,
    MAX_AUDIO_BYTES,build_command,redact,
)


def test_stream_and_recording_validation_do_not_touch_destinations(tmp_path):
    BroadcastConfig(target=str(tmp_path/'new.mp4')).validate()
    BroadcastConfig(mode='stream',target='rtmps://stream.example.invalid/live/private-key').validate()
    for target in ['https://example.invalid/key','file:///tmp/file','rtmp://','rtmp://host/key\noption']:
        with pytest.raises(ValueError):BroadcastConfig(mode='stream',target=target).validate()
    existing=tmp_path/'exists.mp4';existing.write_bytes(b'keep')
    with pytest.raises(ValueError):BroadcastConfig(target=str(existing)).validate()
    assert existing.read_bytes()==b'keep'
    with pytest.raises(ValueError):BroadcastConfig(target=str(tmp_path/'new.mp4'),width=321).validate()


def test_arguments_are_separate_and_no_capture_device_or_shell_is_used(tmp_path):
    target=tmp_path/'song $(not-a-command).mkv';config=BroadcastConfig(target=str(target))
    args=build_command(config,12345,12346)
    assert args[-1]==str(target.resolve())
    assert '-n' in args and 'libx264' in args and 'aac' in args
    assert 'tcp://127.0.0.1:12345?timeout=5000000' in args
    assert 'tcp://127.0.0.1:12346?timeout=5000000' in args
    assert not any(x in args for x in ['x11grab','gdigrab','dshow','pulse','alsa'])
    args=build_command(BroadcastConfig(mode='stream',target='rtmp://example.invalid/live/key'),1,2)
    assert args[-3:-1]==['flv','-flvflags'] or '-flvflags' in args


def test_errors_redact_entire_url_and_partial_ingest_key():
    target='rtmps://user:password@example.invalid/live/very-secret-stream-key?token=private'
    result=redact('Failed to open '+target+' key very-secret-stream-key',target)
    assert 'very-secret-stream-key' not in result and 'password' not in result
    assert 'example.invalid' not in result and 'token=private' not in result


def test_rgb_padding_and_audio_queue_are_bounded(tmp_path):
    config=BroadcastConfig(target=str(tmp_path/'out.mp4'),width=322,height=180)
    image=QImage(322,180,QImage.Format.Format_RGB32);image.fill(QColor(1,2,3))
    data=BroadcastSession._image_bytes(image,config)
    assert len(data)==322*180*3 and data[:9]==bytes([1,2,3])*3
    session=BroadcastSession();session._running=True
    session.push_audio(bytes(MAX_AUDIO_BYTES+4096))
    assert len(session._audio)==MAX_AUDIO_BYTES
    assert session._audio_dropped_frames==4096//AUDIO_FRAME_BYTES
    session.clear_audio();assert len(session._audio)==0
    session._running=False


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'),reason='FFmpeg integration requires installed executables')
def test_real_ten_second_recording_has_synchronized_video_and_actual_tone(tmp_path):
    """Local encode only: no external host, microphone or existing media."""
    app=QApplication.instance() or QApplication([])
    output=tmp_path/'native-program.mp4'
    config=BroadcastConfig(target=str(output),width=640,height=360,fps=15,bitrate_kbps=900)
    session=BroadcastSession();errors=[];messages=[]
    session.error.connect(errors.append);session.status.connect(messages.append)
    initial=QImage(640,360,QImage.Format.Format_RGB32);initial.fill(QColor('#112c37'))
    session.set_video_frame(initial);session.start(config)
    began=time.monotonic();sample=0;next_video=0
    while time.monotonic()-began<10 and session.running:
        count=960
        clock=(np.arange(count,dtype=np.float32)+sample)/AUDIO_RATE
        mono=(np.sin(2*np.pi*440*clock)*.15).astype('<f4')
        session.push_audio(np.column_stack((mono,mono)).astype('<f4').tobytes())
        sample+=count
        elapsed=time.monotonic()-began
        if elapsed>=next_video:
            image=QImage(640,360,QImage.Format.Format_RGB32);image.fill(QColor('#112c37'))
            painter=QPainter(image);painter.fillRect(int(elapsed*45)%600,130,40,100,QColor('#dfbd7f'));painter.end()
            session.set_video_frame(image);next_video+=1/config.fps
        app.processEvents();time.sleep(.02)
    session.stop()
    deadline=time.monotonic()+12
    while session.running and time.monotonic()<deadline:
        app.processEvents();time.sleep(.02)
    assert session.wait(1),'Encoder threads must finish after stop'
    app.processEvents()
    assert not errors,errors
    assert output.is_file() and output.stat().st_size>10000
    report=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(output)]))
    streams={s['codec_type']:s for s in report['streams']}
    assert streams['video']['codec_name']=='h264' and streams['audio']['codec_name']=='aac'
    assert streams['video']['width']==640 and streams['video']['height']==360
    assert 9<=float(report['format']['duration'])<=11
    assert abs(float(streams['video']['duration'])-float(streams['audio']['duration']))<.18
    decoded=subprocess.check_output(['ffmpeg','-v','error','-i',str(output),'-map','0:a:0','-t','3','-f','f32le','-ac','2','-ar','48000','-'])
    audio=np.frombuffer(decoded,dtype='<f4');assert math.sqrt(float(np.mean(audio**2)))>.04,'Output must contain supplied tone, not only silence'
    assert any('Recording clean' in message for message in messages)
    assert session._snapshot()['video_frames']>100


@pytest.mark.skipif(not shutil.which('ffmpeg'),reason='FFmpeg integration requires an installed executable')
def test_local_stream_refusal_is_bounded_reconnect_and_redacts_key():
    import socket
    app=QApplication.instance() or QApplication([])
    # Bound but not listening: reserve the local port while refusing connects.
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as reserved:
        reserved.bind(('127.0.0.1',0));port=reserved.getsockname()[1]
        secret='private-test-stream-key'
        session=BroadcastSession();errors=[];messages=[]
        session.error.connect(errors.append);session.status.connect(messages.append)
        frame=QImage(320,180,QImage.Format.Format_RGB32);frame.fill(QColor('#102c37'))
        session.set_video_frame(frame)
        session.start(BroadcastConfig(mode='stream',target=f'rtmp://127.0.0.1:{port}/live/{secret}',width=320,height=180,fps=10,bitrate_kbps=500,reconnect_attempts=1))
        deadline=time.monotonic()+15
        while session.running and time.monotonic()<deadline:
            app.processEvents();time.sleep(.02)
        if session.running:session.stop()
        assert session.wait(4)
        app.processEvents()
        assert not session.running
        assert errors and all(secret not in text for text in errors+messages)
        assert session._snapshot()['reconnects']==1
        assert any('Reconnecting (1/1)' in text for text in messages)
