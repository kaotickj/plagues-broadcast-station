"""Start-output button feedback with a fake encoder and disposable destinations."""
from unittest.mock import Mock

import pytest

from test_broadcast_profile_ui import desk


@pytest.fixture
def output_desk(desk, monkeypatch):
    window, _, _ = desk
    window.output_mode.setCurrentIndex(0)
    window.destination.clear()
    starts = []
    frames = Mock()
    play = Mock()
    monkeypatch.setattr('gnb_player.broadcast.shutil.which', lambda _: '/fixture/ffmpeg')
    monkeypatch.setattr(window.stage, 'render_frame', lambda *args: None)
    monkeypatch.setattr(window.export, 'set_video_frame', frames)
    monkeypatch.setattr(window, 'toggle_play', play)

    def start(config):
        config.validate()
        starts.append(config)
        window.export.status.emit('Starting local recording…')

    monkeypatch.setattr(window.export, 'start', start)
    return window, starts, frames, play


def test_start_button_routes_a_valid_local_destination_to_encoder(output_desk, tmp_path):
    window, starts, frames, play = output_desk
    destination = tmp_path / 'new recording.mp4'
    window.destination.setText(str(destination))
    window.start_output.click()
    assert len(starts) == 1
    assert starts[0].mode == 'record' and starts[0].target == str(destination)
    frames.assert_called_once()
    play.assert_called_once()
    assert 'Starting local recording' in window.broadcast_label.text()
    assert not destination.exists()


def test_blank_recording_destination_uses_picker_then_starts(output_desk, tmp_path, monkeypatch):
    window, starts, _, _ = output_desk
    destination = tmp_path / 'picked recording.mkv'
    picker = Mock(return_value=(str(destination), 'Matroska recording (*.mkv)'))
    monkeypatch.setattr('gnb_player.app.QFileDialog.getSaveFileName', picker)
    window.start_output.click()
    picker.assert_called_once()
    assert window.output_mode.currentIndex() == 0
    assert len(starts) == 1 and starts[0].target == str(destination)
    assert starts[0].mode == 'record' and not destination.exists()


def test_cancelled_recording_picker_leaves_persistent_not_started_feedback(output_desk, monkeypatch):
    window, starts, frames, play = output_desk
    monkeypatch.setattr('gnb_player.app.QFileDialog.getSaveFileName', lambda *args: ('', ''))
    window.start_output.click()
    window.statusBar().clearMessage()
    feedback = window.broadcast_label.text().lower()
    assert 'not started' in feedback and ('recording' in feedback or 'output' in feedback)
    assert not starts
    frames.assert_not_called()
    play.assert_not_called()
    assert window.start_output.isEnabled() and not window.stop_output.isEnabled()


@pytest.mark.parametrize('case,expected', [
    ('extension', '.mp4 or .mkv'),
    ('existing', 'already exists'),
    ('missing-parent', 'existing folder'),
])
def test_invalid_recording_target_reports_persistently_without_starting(
        output_desk, tmp_path, case, expected):
    window, starts, _, play = output_desk
    destination = tmp_path / 'new recording.mp4'
    if case == 'extension':
        destination = tmp_path / 'unsupported.wav'
    elif case == 'existing':
        destination.write_bytes(b'preserve existing recording')
    else:
        destination = tmp_path / 'missing folder' / 'recording.mp4'
    window.destination.setText(str(destination))
    window.start_output.click()
    window.statusBar().clearMessage()
    assert expected in window.broadcast_label.text()
    assert not starts
    play.assert_not_called()
    assert window.start_output.isEnabled() and not window.stop_output.isEnabled()
    if case == 'existing':
        assert destination.read_bytes() == b'preserve existing recording'


def test_synchronous_permission_failure_is_persistent_and_redacted(output_desk, tmp_path, monkeypatch):
    window, starts, _, play = output_desk
    destination = tmp_path / 'new recording.mp4'
    secret = 'PRIVATE-OUTPUT-KEY'
    window.destination.setText(str(destination))
    window.stream_key.setText(secret)

    def fail(config):
        raise PermissionError(f'Permission denied: {config.target}; key={secret}')

    monkeypatch.setattr(window.export, 'start', fail)
    window.start_output.click()
    window.statusBar().clearMessage()
    assert 'Permission denied' in window.broadcast_label.text()
    assert secret not in window.broadcast_label.text()
    assert str(destination) not in window.broadcast_label.text()
    assert not starts and not destination.exists()
    play.assert_not_called()
    assert window.start_output.isEnabled() and not window.stop_output.isEnabled()


def test_async_failure_survives_finished_status_and_resets_on_next_attempt(output_desk, tmp_path):
    window, starts, _, _ = output_desk
    destination = tmp_path / 'new recording.mp4'
    secret = 'PRIVATE-OUTPUT-KEY'
    window.destination.setText(str(destination))
    window.stream_key.setText(secret)
    window.start_output.click()
    assert len(starts) == 1
    window.export.error.emit(f'Encoder fixture failed key={secret}')
    window.export.status.emit('Recording finished')
    window.export.finished.emit()
    window.statusBar().clearMessage()
    assert 'Encoder fixture failed' in window.broadcast_label.text()
    assert secret not in window.broadcast_label.text()
    assert 'Recording finished' not in window.broadcast_label.text()
    assert window.start_output.isEnabled() and not window.stop_output.isEnabled()

    window.start_output.click()
    assert len(starts) == 2
    assert 'Starting local recording' in window.broadcast_label.text()
    assert 'Encoder fixture failed' not in window.broadcast_label.text()
    assert not destination.exists()


def test_blank_stream_destination_never_opens_recording_picker_or_switches_mode(
        output_desk, tmp_path, monkeypatch):
    window, starts, frames, play = output_desk
    secret = 'PRIVATE-STREAM-KEY'
    window.output_mode.setCurrentIndex(1)
    window.destination.clear()
    window.stream_key.setText(secret)
    picker = Mock(return_value=(str(tmp_path / 'must-not-record.mp4'), ''))
    monkeypatch.setattr('gnb_player.app.QFileDialog.getSaveFileName', picker)
    window.start_output.click()
    picker.assert_not_called()
    assert window.output_mode.currentIndex() == 1
    assert not starts
    frames.assert_not_called()
    play.assert_not_called()
    window.statusBar().clearMessage()
    text = window.broadcast_label.text()
    assert 'destination' in text.lower() and ('RTMP' in text or 'stream' in text.lower())
    assert secret not in text
