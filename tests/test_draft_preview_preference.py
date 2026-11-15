"""Shared disposable station fixtures for the mandatory publication gate."""
import json
import os
import time

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND', 'ffmpeg')

from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog, QScrollArea

from gnb_player.app import MainWindow
from gnb_player.library import Track
from gnb_player.lyric_review import approve_document, is_approved
from gnb_player.lyrics import Cue, LyricsDocument, fingerprint, load_lyrics, save_lyrics
from gnb_player.settings import read_profile, write_profile


KEY = 'preview_unapproved_lyrics'


def settle(window, condition=lambda: True, seconds=4):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        QApplication.processEvents()
        if not window.tasks and not window._saving_lyrics and condition():
            return
        QTest.qWait(10)
    raise AssertionError('Temporary station did not finish its pending work')


@pytest.fixture
def station(tmp_path):
    app = QApplication.instance() or QApplication([])
    windows = []

    def create(name='station'):
        window = MainWindow(tmp_path / name)
        windows.append(window)
        settle(window)
        return window

    yield create
    for window in windows:
        settle(window)
        window._lyric_buffers.clear()
        window._closing = True
        window.close()
        app.processEvents()


def recordings(window, tmp_path, *, approved_second=False):
    tracks, targets = [], []
    for index in range(2):
        audio = tmp_path / f'take-{index}.mp3'
        audio.write_bytes(f'fixture recording {index}'.encode())
        track = Track(audio, f'Recording {index}', duration=10)
        document = LyricsDocument([Cue(f'Lyric {index}', 1, 3)], 10,
                                  track.title, fingerprint(audio))
        if index == 1 and approved_second:
            document = approve_document(document, fingerprint(audio))
        target = tmp_path / f'lyrics-{index}.json'
        save_lyrics(document, target)
        tracks.append(track)
        targets.append(target)
    window.all_tracks = tracks
    window.lyric_files = {str(track.path): str(target) for track, target in zip(tracks, targets)}
    window._apply_queue()
    for track, target in zip(tracks, targets):
        window._lyrics_ready(track, target, load_lyrics(target))
    window.playlist.setCurrentRow(0)
    settle(window)
    return tracks, targets
