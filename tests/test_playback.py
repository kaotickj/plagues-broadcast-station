import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import random
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QByteArray
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QAudioBuffer, QAudioFormat

from gnb_player.library import Track
from gnb_player.playback import PlaybackEngine, QueueOrder, pcm_to_mono, pcm_to_stereo_bytes


def test_shuffle_plays_each_track_before_repeat_without_adjacent_repeats():
    order = QueueOrder(8, random.Random(2049))
    first = order.select(0)
    cycle = [first] + [order.next(True, True) for _ in range(7)]
    assert set(cycle) == set(range(8))
    following = order.next(True, True)
    assert following != cycle[-1]
    next_cycle = [following] + [order.next(True, True) for _ in range(7)]
    assert set(next_cycle) == set(range(8))


def test_nonrepeat_queue_finishes_and_previous_uses_actual_shuffle_history():
    order = QueueOrder(4, random.Random(19))
    order.select(2)
    next_index = order.next(True, False)
    assert order.previous() == 2
    assert order.next(True, False) == next_index
    order.next(True, False)
    order.next(True, False)
    assert order.next(True, False) is None
    sequential = QueueOrder(2)
    assert sequential.next(False, False) == 0
    assert sequential.next(False, False) == 1
    assert sequential.next(False, False) is None


def test_single_track_loops_only_when_repeat_is_enabled():
    order = QueueOrder(1)
    order.select(0)
    assert order.next(True, False) is None
    assert order.next(True, True) == 0
    empty = QueueOrder()
    assert empty.next() is None
    assert empty.previous() is None


def test_pcm_normalizes_actual_stereo_and_silence():
    pcm = np.array([[32767, -32768], [16384, 16384], [0, 0]], dtype=np.int16)
    mono = pcm_to_mono(pcm.tobytes(), QAudioFormat.Int16, 2)
    assert mono.dtype == np.float32
    np.testing.assert_allclose(mono, [-1 / 65536, .5, 0], atol=1e-6)
    np.testing.assert_array_equal(pcm_to_mono(bytes([128, 128]), QAudioFormat.UInt8, 1), [0, 0])
    np.testing.assert_array_equal(pcm_to_mono(np.zeros(40, dtype=np.float32).tobytes(), QAudioFormat.Float, 1), np.zeros(40))


def test_pcm_is_bounded_finite_and_rejects_unknown_formats():
    mono = pcm_to_mono(np.array([np.nan, np.inf, -np.inf, .25], dtype=np.float32).tobytes(), QAudioFormat.Float, 1)
    np.testing.assert_array_equal(mono, [0, 1, -1, .25])
    assert len(pcm_to_mono(np.zeros(60000, dtype=np.int16).tobytes(), QAudioFormat.Int16, 2)) == 4096
    assert len(pcm_to_mono(b"", QAudioFormat.Int16, 1)) == 0
    assert len(pcm_to_mono(b"abc", QAudioFormat.Unknown, 1)) == 0
    assert len(pcm_to_mono(b"abc", QAudioFormat.Int16, 0)) == 0


def test_cleanfeed_keeps_stereo_channels_and_rejects_wrong_sample_rate():
    pcm = np.array([[.25, -.5], [np.nan, np.inf]], dtype=np.float32)
    packed = pcm_to_stereo_bytes(pcm.tobytes(), QAudioFormat.Float, 2, 48000)
    np.testing.assert_array_equal(np.frombuffer(packed, dtype="<f4").reshape(-1, 2), [[.25, -.5], [0, 1]])
    with pytest.raises(ValueError, match="44100"):
        pcm_to_stereo_bytes(pcm.tobytes(), QAudioFormat.Float, 2, 44100)


@pytest.fixture(scope="module")
def application():
    return QApplication.instance() or QApplication([])


def test_qt_buffer_callback_uses_real_pcm_and_pause_emits_silence(application):
    engine = PlaybackEngine()
    fmt = QAudioFormat()
    fmt.setSampleRate(48000)
    fmt.setChannelCount(1)
    fmt.setSampleFormat(QAudioFormat.Int16)
    pcm = np.array([8192, 16384, -8192, 0] * 256, dtype=np.int16)
    buffer = QAudioBuffer(QByteArray(pcm.tobytes()), fmt)
    observed = []
    cleanfeed = []
    engine.waveform_ready.connect(observed.append)
    engine.pcm_ready.connect(cleanfeed.append)
    engine._set_playing(True)
    engine._audio_buffer(buffer)
    assert len(observed) == 1
    np.testing.assert_allclose(observed[0][:4], [.25, .5, -.25, 0])
    assert len(cleanfeed[0]) == len(pcm) * 2 * 4
    np.testing.assert_allclose(np.frombuffer(cleanfeed[0], dtype="<f4")[:8], [.25,.25,.5,.5,-.25,-.25,0,0])
    engine.pause()
    np.testing.assert_array_equal(observed[-1], np.zeros(1024))
    engine.stop()
    assert engine.player.source().isEmpty()
    assert engine.playing is False


def test_all_missing_files_stop_after_one_bounded_pass(application, tmp_path):
    engine = PlaybackEngine()
    errors = []
    engine.error.connect(errors.append)
    engine.set_queue([Track(tmp_path / f"missing{index}.mp3", f"Missing {index}") for index in range(3)])
    engine.play(0)
    for _ in range(12):
        application.processEvents()
    assert engine.player.source().isEmpty()
    assert not engine.playing
    assert any("none of the queued" in error for error in errors)
    assert len(engine._failed) == 3


def test_reorder_and_add_preserve_real_media_clock_and_removal_advances(application, tmp_path):
    tracks = []
    for index in range(3):
        path = tmp_path / f"fixture{index}.wav"
        samples = (np.sin(np.arange(48000 * 5) / 48000 * 2 * np.pi * (220 + index*40)) * 500).astype("<i2")
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(samples.tobytes())
        tracks.append(Track(path, f"Fixture {index}", duration=5))
    engine = PlaybackEngine()
    engine.set_volume(0)  # Decode a real recording without audible test tones.
    engine.shuffle = False
    cleanfeed = []
    engine.pcm_ready.connect(cleanfeed.append)
    engine.set_queue(tracks[:2])
    engine.play(0)
    deadline = time.monotonic() + 4
    while (engine.position < .2 or not cleanfeed) and time.monotonic() < deadline:
        application.processEvents()
        QTest.qWait(10)
    assert engine.position >= .2
    assert cleanfeed, "The cleanfeed must contain actual decoded audio, even when monitoring volume is zero."
    assert any(np.any(np.frombuffer(buffer, dtype="<f4")) for buffer in cleanfeed)
    source, position = engine.player.source(), engine.position
    engine.set_queue([tracks[1], tracks[0]])
    assert engine.player.source() == source
    assert engine.position >= position - .02
    assert engine.current_index == 1 and engine.playing
    engine.set_queue([tracks[1], tracks[0], tracks[2]])
    assert engine.player.source() == source and engine.position >= position - .02
    engine.set_queue([tracks[1], tracks[2]])
    application.processEvents()
    assert engine.current_track.path == tracks[2].path
    assert Path(engine.player.source().toLocalFile()) == tracks[2].path
    engine.stop()
    assert engine.player.source().isEmpty()
