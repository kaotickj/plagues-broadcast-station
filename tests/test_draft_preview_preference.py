"""Station-local draft preview preference; native Qt, disposable data, no playback."""
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


def click_preview(window):
    window.show()
    window.tabs.setCurrentIndex(1)
    QApplication.processEvents()
    scroll = window.tabs.widget(1).findChild(QScrollArea)
    if window.lyric_options_panel.isHidden():
        scroll.ensureWidgetVisible(window.lyric_options)
        QApplication.processEvents()
        QTest.mouseClick(window.lyric_options, Qt.MouseButton.LeftButton)
    scroll.ensureWidgetVisible(window.allow_drafts)
    QApplication.processEvents()
    before = window.allow_drafts.isChecked()
    QTest.mouseClick(window.allow_drafts, Qt.MouseButton.LeftButton,
                     pos=QPoint(8, window.allow_drafts.height() // 2))
    settle(window)
    assert window.allow_drafts.isChecked() is not before


def saved_preference(window, expected):
    path = window.data_dir / 'session.json'

    def persisted():
        return path.exists() and read_profile(path)['settings'].get(KEY) is expected

    settle(window, persisted)


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


def test_new_station_and_legacy_session_without_preference_default_on(station, tmp_path):
    assert station().allow_drafts.isChecked()
    directory = tmp_path / 'legacy'
    directory.mkdir()
    write_profile(directory / 'session.json', [], [], {}, {'lyrics': True})
    restored = station('legacy')
    assert restored.allow_drafts.isChecked()
    assert restored.visual_settings()[KEY] is True


@pytest.mark.parametrize('value', [True, False])
def test_actual_toggle_autosaves_boolean_and_reopens_exact_choice(station, value):
    window = station()
    if window.allow_drafts.isChecked() is value:
        click_preview(window)
    click_preview(window)
    assert window.allow_drafts.isChecked() is value
    saved_preference(window, value)
    assert station().allow_drafts.isChecked() is value
    assert 'preview_unapproved_lyrics' not in json.loads(window.settings_file.read_text())


@pytest.mark.parametrize('value', [True, False])
def test_legacy_profile_does_not_overwrite_existing_explicit_choice(station, tmp_path, value):
    window = station()
    window.allow_drafts.setChecked(value)
    legacy = tmp_path / 'old-playlist.json'
    write_profile(legacy, [], [], {}, {'lyrics': True})
    window.load_profile(legacy)
    assert window.allow_drafts.isChecked() is value


@pytest.mark.parametrize('invalid', ['false', 0, None])
def test_non_boolean_profile_value_cannot_replace_explicit_off(station, tmp_path, invalid):
    window = station()
    window.allow_drafts.setChecked(False)
    profile = tmp_path / 'invalid-preference.json'
    write_profile(profile, [], [], {}, {KEY: invalid})
    window.load_profile(profile)
    assert not window.allow_drafts.isChecked()


@pytest.mark.parametrize('value', [True, False])
def test_track_selection_import_and_save_keep_operator_choice(station, tmp_path, monkeypatch, value):
    window = station()
    window.allow_drafts.setChecked(value)
    tracks, _ = recordings(window, tmp_path)
    for row in (1, 0):
        window.playlist.setCurrentRow(row)
        settle(window)
        assert window.allow_drafts.isChecked() is value
    incoming = tmp_path / 'incoming.json'
    save_lyrics(LyricsDocument([Cue('Imported draft', 1, 3)], 10,
                              source_sha256=fingerprint(tracks[0].path)), incoming)
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a, **kw: (str(incoming), 'Timed lyrics'))
    window.import_lyrics()
    settle(window)
    assert window.allow_drafts.isChecked() is value
    assert window.lyric_text.item(0, 2).text() == 'Imported draft'
    window.lyric_text.item(0, 2).setText('Saved correction')
    window._save_lyric_changes()
    settle(window)
    assert window.allow_drafts.isChecked() is value
    assert window.lyric_text.item(0, 2).text() == 'Saved correction'
    assert not is_approved(window._editor_document)


def test_actual_checkbox_updates_shared_output_and_persists_without_approval(station, tmp_path):
    window = station()
    tracks, targets = recordings(window, tmp_path)
    before = [target.read_bytes() for target in targets]
    window.engine.order.select(0)
    window._update_program_lyrics(tracks[0], window.documents[str(tracks[0].path)])
    assert window.stage._lyrics is not None
    click_preview(window)
    assert window.stage._lyrics is None
    saved_preference(window, False)
    click_preview(window)
    assert window.stage._lyrics is not None
    saved_preference(window, True)
    assert [target.read_bytes() for target in targets] == before
    assert not window._has_lyrics(tracks[0])
    assert not window.review_confirm.isChecked() and not window.approve_lyrics_button.isEnabled()
    assert not window.engine.playing and not window.export.running


def test_default_preview_retains_dirty_buffer_and_recording_identity_guards(station, tmp_path):
    window = station()
    tracks, targets = recordings(window, tmp_path)
    window.engine.order.select(0)
    window._update_program_lyrics(tracks[0], window.documents[str(tracks[0].path)])
    assert window.stage._lyrics is not None
    window.lyric_text.item(0, 2).setText('Unsaved correction')
    assert window.stage._lyrics is None
    click_preview(window)
    click_preview(window)
    assert window.stage._lyrics is None
    assert window.lyric_text.raw_rows()[0][2] == 'Unsaved correction'
    assert load_lyrics(targets[0]).segments[0].text == 'Lyric 0'
    window._lyric_buffers.clear()
    document = window.documents[str(tracks[0].path)]
    tracks[0].path.write_bytes(b'replaced audio bytes')
    window._update_program_lyrics(tracks[0], document)
    assert window.stage._lyrics is None
    assert not window._has_lyrics(tracks[0])


def test_preview_preference_does_not_change_approved_only_queue_filter(station, tmp_path):
    window = station()
    tracks, targets = recordings(window, tmp_path, approved_second=True)
    before = [target.read_bytes() for target in targets]
    window.show()
    QApplication.processEvents()
    QTest.mouseClick(window.ready_only, Qt.MouseButton.LeftButton,
                     pos=QPoint(8, window.ready_only.height() // 2))
    settle(window)
    assert window.ready_only.isChecked()
    assert [track.path for track in window.engine.tracks] == [tracks[1].path]
    for expected in (False, True):
        click_preview(window)
        assert window.allow_drafts.isChecked() is expected
        assert window.ready_only.isChecked()
        assert [track.path for track in window.engine.tracks] == [tracks[1].path]
        assert not window._has_lyrics(tracks[0]) and window._has_lyrics(tracks[1])
    saved_preference(window, True)
    restored = station()
    assert restored.allow_drafts.isChecked() and restored.ready_only.isChecked()
    assert [track.path for track in restored.engine.tracks] == [tracks[1].path]
    assert [target.read_bytes() for target in targets] == before


def test_preference_is_scoped_to_each_station_data_directory(station):
    first, second = station('first'), station('second')
    second.save_session()
    second_before = (second.data_dir / 'session.json').read_bytes()
    click_preview(first)
    saved_preference(first, False)
    assert second.allow_drafts.isChecked()
    assert (second.data_dir / 'session.json').read_bytes() == second_before
    assert not station('first').allow_drafts.isChecked()
    assert station('second').allow_drafts.isChecked()
