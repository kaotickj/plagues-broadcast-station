"""Qt multimedia playback and a waveform sampled from the decoded recording."""
from __future__ import annotations

import math
import random
import sys
import time
from typing import Sequence

import numpy as np
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtMultimedia import QAudioBufferOutput, QAudioFormat, QAudioOutput, QMediaPlayer, QVideoSink

from .library import Track


class QueueOrder:
    """A shuffle bag avoids repeats within a cycle and keeps a back history."""
    def __init__(self, size: int = 0, rng=None):
        self.rng = rng or random.Random()
        self.reset(size)

    def reset(self, size: int):
        self.size, self.current = max(0, size), -1
        self.bag: list[int] | None = None
        self.back: list[int] = []
        self.forward: list[int] = []
        self._shuffle = None

    def select(self, index: int) -> int | None:
        if not 0 <= index < self.size:
            return None
        if self.current >= 0 and index != self.current:
            self.back.append(self.current)
            self.back = self.back[-1000:]
        self.current = index
        self.forward.clear()
        if self.bag is not None and index in self.bag:
            self.bag.remove(index)
        return index

    def next(self, shuffle: bool = True, repeat: bool = True) -> int | None:
        if not self.size:
            return None
        if self.forward:
            chosen = self.forward.pop()
            if self.current >= 0:
                self.back.append(self.current)
            self.current = chosen
            return chosen
        if self._shuffle != shuffle:
            self.bag = None
            self._shuffle = shuffle
        if shuffle:
            if self.bag is None:
                self.bag = [i for i in range(self.size) if i != self.current]
                self.rng.shuffle(self.bag)
            if not self.bag:
                if not repeat:
                    return None
                self.bag = list(range(self.size))
                self.rng.shuffle(self.bag)
                if self.size > 1 and self.bag[-1] == self.current:
                    self.bag[-1], self.bag[0] = self.bag[0], self.bag[-1]
                if not self.bag:
                    return self.current if self.current >= 0 else 0
            chosen = self.bag.pop()
        else:
            chosen = self.current + 1
            if chosen >= self.size:
                if not repeat:
                    return None
                chosen = 0
        return self.select(chosen)

    def previous(self, repeat: bool = True) -> int | None:
        if not self.size:
            return None
        if self.back:
            if self.current >= 0:
                self.forward.append(self.current)
            self.current = self.back.pop()
            return self.current
        return self.current if self.current >= 0 else 0


def pcm_to_mono(raw, sample_format, channels: int, max_samples: int = 4096) -> np.ndarray:
    """Normalize native-endian Qt interleaved PCM, retaining real silence."""
    mapping = {
        QAudioFormat.SampleFormat.UInt8: (np.uint8, 128.0, 128.0),
        QAudioFormat.SampleFormat.Int16: (np.int16, 32768.0, 0.0),
        QAudioFormat.SampleFormat.Int32: (np.int32, 2147483648.0, 0.0),
        QAudioFormat.SampleFormat.Float: (np.float32, 1.0, 0.0),
    }
    if sample_format not in mapping or not 1 <= channels <= 32:
        return np.zeros(0, dtype=np.float32)
    dtype, divisor, bias = mapping[sample_format]
    try:
        buffer = memoryview(raw)
        samples = np.frombuffer(buffer, dtype=dtype, count=len(buffer) // np.dtype(dtype).itemsize)
        count = len(samples) // channels
        if count == 0:
            return np.zeros(0, dtype=np.float32)
        # A GUI tick never needs the beginning of a large decoder block.
        frames = samples[:count*channels].reshape(count, channels)[-max_samples:]
        mono = ((frames.astype(np.float32) - bias) / divisor).mean(axis=1)
        return np.clip(np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0).astype(np.float32, copy=False)
    except (TypeError, ValueError, BufferError):
        return np.zeros(0, dtype=np.float32)


def pcm_to_stereo_bytes(raw, sample_format, channels: int, sample_rate: int) -> bytes:
    """Cleanfeed contract: exactly 48 kHz stereo little-endian float32 PCM."""
    if sample_rate != 48000 or channels not in (1, 2):
        raise ValueError(f"Decoder supplied {sample_rate} Hz / {channels} channels; 48000 Hz stereo was requested.")
    mapping = {
        QAudioFormat.SampleFormat.UInt8: (np.uint8, 128.0, 128.0),
        QAudioFormat.SampleFormat.Int16: (np.int16, 32768.0, 0.0),
        QAudioFormat.SampleFormat.Int32: (np.int32, 2147483648.0, 0.0),
        QAudioFormat.SampleFormat.Float: (np.float32, 1.0, 0.0),
    }
    if sample_format not in mapping:
        raise ValueError("Decoder supplied an unsupported PCM sample format.")
    dtype, divisor, bias = mapping[sample_format]
    samples = np.frombuffer(memoryview(raw), dtype=dtype)
    count = len(samples) // channels
    samples = samples[:count*channels].reshape(count, channels)
    normalized = (samples.astype(np.float32) - bias) / divisor
    normalized = np.clip(np.nan_to_num(normalized, nan=0.0, posinf=1.0, neginf=-1.0), -1, 1)
    if channels == 1:
        normalized = np.repeat(normalized, 2, axis=1)
    return normalized.astype("<f4", copy=False).tobytes()


class PlaybackEngine(QObject):
    position_changed = Signal(float)
    duration_changed = Signal(float)
    track_changed = Signal(object)
    playing_changed = Signal(bool)
    waveform_ready = Signal(object)
    pcm_ready = Signal(bytes)
    video_frame_ready = Signal(object)
    queue_changed = Signal()
    error = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracks: list[Track] = []
        self.order = QueueOrder()
        self.shuffle = True
        self.repeat = True
        self._playing = False
        self._generation = 0
        self._switching = False
        self._failed: set[int] = set()
        self._pending_error = False
        self._last_pcm_at = 0.0
        self._pcm_error_reported = False
        self._last_wave = np.zeros(1024, dtype=np.float32)
        self.player = QMediaPlayer(self)
        self.output = QAudioOutput(self)
        self.output.setVolume(0.8)
        self.player.setAudioOutput(self.output)
        target_format = QAudioFormat()
        target_format.setSampleRate(48000)
        target_format.setChannelCount(2)
        target_format.setSampleFormat(QAudioFormat.SampleFormat.Float)
        self.buffers = QAudioBufferOutput(target_format, self)
        self.player.setAudioBufferOutput(self.buffers)
        self.buffers.audioBufferReceived.connect(self._audio_buffer)
        self.video_sink = QVideoSink(self)
        self.player.setVideoSink(self.video_sink)
        self.video_sink.videoFrameChanged.connect(self._video_frame)
        self.player.positionChanged.connect(lambda value: self.position_changed.emit(value / 1000.0))
        self.player.durationChanged.connect(lambda value: self.duration_changed.emit(value / 1000.0))
        self.player.playbackStateChanged.connect(self._state_changed)
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.errorOccurred.connect(self._media_error)

    @property
    def current_track(self) -> Track | None:
        return self.tracks[self.order.current] if 0 <= self.order.current < len(self.tracks) else None

    @property
    def position(self) -> float:
        return self.player.position() / 1000.0

    @property
    def duration(self) -> float:
        return self.player.duration() / 1000.0

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def current_index(self) -> int:
        return self.order.current

    @property
    def volume(self) -> float:
        return self.output.volume()

    def set_queue(self, tracks: Sequence[Track], preserve_current: bool = True):
        old_tracks, old_order, current = self.tracks, self.order, self.current_track
        replacement = list(tracks)
        key = lambda track: str(track.path.resolve())
        by_path = {key(track): index for index, track in enumerate(replacement)}
        if preserve_current and current is not None and key(current) in by_path:
            # Reordering/adding other tracks never touches the audio element.
            self.tracks = replacement
            order = QueueOrder(len(replacement), old_order.rng)
            order.current = by_path[key(current)]
            translate = lambda indices: [by_path[key(old_tracks[i])] for i in indices if 0 <= i < len(old_tracks) and key(old_tracks[i]) in by_path]
            order.back, order.forward = translate(old_order.back), translate(old_order.forward)
            order._shuffle = old_order._shuffle
            if old_order.bag is not None:
                previous_paths = {key(track) for track in old_tracks}
                added = [index for index, track in enumerate(replacement) if key(track) not in previous_paths]
                order.bag = translate(old_order.bag) + added
            self.order = order
            self.track_changed.emit(self.current_track)
            self.queue_changed.emit()
            return
        was_playing = self.playing
        successor = next((track for track in old_tracks[old_order.current+1:] if key(track) in by_path), None) if current else None
        self.stop()
        self.tracks = replacement
        self.order = QueueOrder(len(replacement), old_order.rng)
        if preserve_current and current is not None and replacement:
            self.order.select(by_path[key(successor)] if successor else 0)
            self.track_changed.emit(self.current_track)
            if was_playing:
                self._load_current()
        else:
            self.track_changed.emit(None)
        self.queue_changed.emit()

    def play(self, index: int | None = None):
        if index is not None:
            if self.order.select(index) is None:
                self.error.emit("Choose a recording in the playlist.")
                return
            self._failed.clear()
            self._load_current()
            return
        if not self.tracks:
            self.error.emit("Add recordings before starting playback.")
            return
        if self.current_track is None:
            self.order.select(0)
        if self.player.source().isEmpty():
            self._failed.clear()
            self._load_current()
        else:
            self.player.play()

    def pause(self):
        self.player.pause()
        self.waveform_ready.emit(np.zeros(1024, dtype=np.float32))

    def toggle(self):
        self.pause() if self.playing else self.play()

    def stop(self):
        self._generation += 1
        self._pcm_error_reported = False
        self._pending_error = False
        self._switching = True
        self.player.stop()
        self.player.setSource(QUrl())
        self._switching = False
        self._set_playing(False)
        self.position_changed.emit(0.0)
        self.waveform_ready.emit(np.zeros(1024, dtype=np.float32))
        self.video_frame_ready.emit(None)

    def next(self):
        if self.order.next(self.shuffle, self.repeat) is None:
            self.stop()
            return
        self._failed.clear()
        self._load_current()

    def previous(self):
        if self.position > 3:
            self.seek(0)
            return
        if self.order.previous(self.repeat) is not None:
            self._failed.clear()
            self._load_current()

    def seek(self, seconds: float):
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            return
        maximum = self.duration or (self.current_track.duration if self.current_track else 0)
        self.player.setPosition(round(max(0.0, min(float(seconds), maximum or float(seconds))) * 1000))

    def set_volume(self, volume: float):
        if isinstance(volume, (int, float)) and math.isfinite(volume):
            self.output.setVolume(max(0.0, min(1.0, float(volume))))

    def _load_current(self):
        track = self.current_track
        if track is None:
            return
        self._generation += 1
        self._pending_error = False
        self._switching = True
        self.player.stop()
        self.video_frame_ready.emit(None)
        self.waveform_ready.emit(np.zeros(1024, dtype=np.float32))
        self.track_changed.emit(track)
        self.duration_changed.emit(track.duration)
        self.player.setSource(QUrl.fromLocalFile(str(track.path.resolve())))
        self._switching = False
        if not track.path.is_file():
            self._media_error(QMediaPlayer.Error.ResourceError, "Recording is missing or unavailable.")
        else:
            self.player.play()

    def _set_playing(self, playing: bool):
        if self._playing != playing:
            self._playing = playing
            self.playing_changed.emit(playing)

    def _state_changed(self, state):
        self._set_playing(state == QMediaPlayer.PlaybackState.PlayingState)

    def _media_status(self, status):
        if self._switching:
            return
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            generation = self._generation
            QTimer.singleShot(0, lambda: self.next() if generation == self._generation else None)

    def _media_error(self, code, message=""):
        if code == QMediaPlayer.Error.NoError or self._switching or self._pending_error:
            return
        self._pending_error = True
        self._failed.add(self.order.current)
        track = self.current_track
        self.error.emit(f"{track.title if track else 'Recording'}: {message or self.player.errorString()}")
        generation = self._generation
        QTimer.singleShot(0, lambda: self._skip_failed(generation))

    def _skip_failed(self, generation: int):
        if generation != self._generation:
            return
        self._pending_error = False
        if not self.tracks or len(self._failed) >= len(self.tracks):
            self.stop()
            self.error.emit("Playback stopped: none of the queued recordings could be opened.")
            return
        for _ in self.tracks:
            index = self.order.next(self.shuffle, self.repeat)
            if index is None:
                self.stop()
                return
            if index not in self._failed:
                self._load_current()
                return
        self.stop()

    def _audio_buffer(self, buffer):
        if not buffer.isValid() or not self.playing:
            return
        try:
            # PySide returns a sized memoryview on current Qt bindings.
            raw = buffer.constData() if hasattr(buffer, "constData") else buffer.data()
            fmt = buffer.format()
            try:
                self.pcm_ready.emit(pcm_to_stereo_bytes(raw, fmt.sampleFormat(), fmt.channelCount(), fmt.sampleRate()))
            except (ValueError, TypeError, BufferError) as error:
                if not self._pcm_error_reported:
                    self._pcm_error_reported = True
                    self.error.emit("Broadcast audio format unavailable: " + str(error))
            now = time.monotonic()
            if now - self._last_pcm_at < 1 / 35:
                return
            self._last_pcm_at = now
            mono = pcm_to_mono(raw, fmt.sampleFormat(), fmt.channelCount())
            if mono.size:
                self._last_wave = mono
                self.waveform_ready.emit(mono)
        except (AttributeError, TypeError, ValueError, BufferError):
            # Older bindings without direct buffer access should show a flat
            # line rather than pretending that an animation represents sound.
            self.waveform_ready.emit(np.zeros(1024, dtype=np.float32))

    def _video_frame(self, frame):
        if frame.isValid():
            image = frame.toImage()
            if not image.isNull():
                self.video_frame_ready.emit(image)
