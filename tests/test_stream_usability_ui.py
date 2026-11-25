"""Independent operator checks using disposable profiles and no audio devices.

Every credential backend here is an in-memory fake. These tests never start
output, install models, discover model locations, or access the native keyring.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND', 'ffmpeg')
import pytest
from PySide6.QtCore import QObject, Qt
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox

import gnb_player.app as app_module
import gnb_player.playback as playback_module
from gnb_player.app import MainWindow
from gnb_player.broadcast_profile import BroadcastProfileStore, ProfileError
from gnb_player.extraction import ExtractionConfig
from gnb_player.settings import write_profile

SERVER = 'rtmps://broadcast.invalid/live2'
KEY = 'DUMMY-KEY-DO-NOT-AIR'


class FakeCredentials:
    def __init__(self):
        self.values = {}
        self.calls = []

    def get_password(self, service, username):
        self.calls.append('get')
        return self.values.get((service, username))

    def set_password(self, service, username, value):
        self.calls.append('set')
        self.values[service, username] = value

    def delete_password(self, service, username):
        self.calls.append('delete')
        self.values.pop((service, username), None)


class NoDeviceAudio(QObject):
    """Keep the real playback controller without opening sandbox audio devices."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._volume = 0.8

    def setVolume(self, value):
        self._volume = value

    def volume(self):
        return self._volume


def no_discovery(config):
    return config, {'fields': {}, 'notes': [], 'effective_model_paths': {}, 'resolved_values': {}}


def no_model_status(config):
    return {'ready': False, 'missing_models': [], 'python_available': False,
            'ffmpeg_available': True, 'ffprobe_available': True}


@pytest.fixture
def operator(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(playback_module, 'QAudioOutput', NoDeviceAudio)
    monkeypatch.setattr(QMediaPlayer, 'setAudioOutput', lambda self, output: None)
    monkeypatch.setattr(app_module, 'discover_config', lambda: ExtractionConfig())
    monkeypatch.setattr(app_module, 'resolve_existing_models', no_discovery)
    monkeypatch.setattr(app_module, 'config_status', no_model_status)
    monkeypatch.setattr(MainWindow, '_task', lambda self, work, done: done(work()))
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Cancel)
    backend = FakeCredentials()
    data = tmp_path / 'disposable station'
    windows = []

    def make_window(factory=None):
        store = BroadcastProfileStore(data, backend_factory=factory or (lambda: backend))
        window = MainWindow(data, credential_store=store)
        window.preview_timer.stop()
        window.save_timer.stop()
        window.export.start = Mock(side_effect=AssertionError('This test must not start output.'))
        windows.append(window)
        return window

    window = make_window()
    yield window, backend, make_window
    for current in reversed(windows):
        if getattr(current, '_help_dialog', None):
            current._help_dialog.close()
        current._closing = True
        current.close()
        current.save_timer.stop()
    app.processEvents()


def enter_stream(window, *, destination=SERVER, key=KEY, remember=False, bitrate=28500):
    window.output_mode.setCurrentIndex(1)
    window.destination.setText(destination)
    window.stream_key.setText(key)
    window.remember_stream_key.setChecked(remember)
    window.resolution.setCurrentIndex(1)
    window.bitrate.setValue(bitrate)


def test_ordinary_save_reopen_and_repeated_save_without_native_keyring(operator):
    window, backend, reopen = operator
    enter_stream(window)
    window.save_stream_button.click()
    first = window.credential_store.settings_file.read_bytes()
    assert window.stream_profile_status.text().startswith('Saved')
    assert window.bitrate.value() == 28500
    assert backend.calls == [] and not window.credential_store.marker.exists()
    assert KEY.encode() not in first
    window.save_stream_button.click()
    assert window.credential_store.settings_file.read_bytes() == first
    restored = reopen()
    assert restored.output_mode.currentIndex() == 1
    assert restored.destination.text() == SERVER
    assert restored.bitrate.value() == 28500 and restored.resolution.currentIndex() == 1
    assert restored.stream_key.text() == ''
    assert not restored.remember_stream_key.isChecked()
    assert backend.calls == []
    window.export.start.assert_not_called()
    restored.export.start.assert_not_called()


def test_ordinary_save_survives_a_fresh_process_without_keyring(operator):
    window, _, _ = operator
    enter_stream(window)
    window.save_stream_profile()
    source = Path(app_module.__file__).parents[1]
    test_dir = Path(__file__).parent
    script = r'''
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtMultimedia import QMediaPlayer
import gnb_player.app as appmod
import gnb_player.playback as playback
from gnb_player.extraction import ExtractionConfig
from gnb_player.broadcast_profile import BroadcastProfileStore
from test_stream_usability_ui import NoDeviceAudio, no_discovery, no_model_status
playback.QAudioOutput = NoDeviceAudio
QMediaPlayer.setAudioOutput = lambda self, output: None
appmod.discover_config = lambda: ExtractionConfig()
appmod.resolve_existing_models = no_discovery
appmod.config_status = no_model_status
appmod.MainWindow._task = lambda self, work, done: done(work())
def forbidden():
    raise AssertionError('Ordinary restart must not contact native credentials')
app = QApplication([])
window = appmod.MainWindow(Path(sys.argv[1]), credential_store=BroadcastProfileStore(sys.argv[1], backend_factory=forbidden))
window.preview_timer.stop()
assert window.destination.text() == 'rtmps://broadcast.invalid/live2'
assert window.stream_key.text() == '' and window.bitrate.value() == 28500
assert window.resolution.currentIndex() == 1 and not window.export.running
print('fresh-process-profile-restored')
window._closing = True
window.close()
'''
    env = os.environ.copy()
    env.update(PYTHONPATH=os.pathsep.join((str(source), str(test_dir))), PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run([sys.executable, '-B', '-c', script, str(window.data_dir)],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'fresh-process-profile-restored' in result.stdout
    assert KEY not in result.stdout + result.stderr


def test_remember_is_explicit_and_can_be_turned_off_without_using_old_key(operator):
    window, backend, reopen = operator
    enter_stream(window, remember=True)
    window.save_stream_profile()
    assert 'set' in backend.calls and window.credential_store.marker.exists()
    assert KEY not in window.credential_store.settings_file.read_text()
    restored = reopen()
    assert restored.stream_key.text() == KEY and restored.remember_stream_key.isChecked()
    restored.remember_stream_key.setChecked(False)
    restored.save_stream_profile()
    calls = list(backend.calls)
    ordinary = reopen()
    assert ordinary.destination.text() == SERVER and ordinary.stream_key.text() == ''
    assert backend.calls == calls
    assert window.credential_store.marker.exists()  # Explicit Forget removes old protected copies.
    ordinary.export.start.assert_not_called()


def test_missing_keyring_still_saves_ordinary_settings_and_reports_partial_state(operator):
    window, _, reopen = operator
    def unavailable():
        raise ProfileError('Protected credential storage is unavailable; unlock the keyring and retry.')
    window.credential_store._factory = unavailable
    enter_stream(window, remember=True)
    window.save_stream_profile()
    assert window.credential_store.settings_file.exists()
    saved = json.loads(window.credential_store.settings_file.read_text())
    assert saved['remember'] is False and saved['destination'] == SERVER
    assert 'key was not remembered' in window.stream_profile_status.text()
    assert window.stream_key.text() == KEY
    restored = reopen(unavailable)
    assert restored.destination.text() == SERVER and restored.bitrate.value() == 28500
    assert restored.stream_key.text() == ''


def test_reload_and_forget_cancel_preserve_fields_files_and_fake_credentials(operator):
    window, backend, _ = operator
    enter_stream(window, remember=True)
    window.save_stream_profile()
    saved_bytes = window.credential_store.settings_file.read_bytes()
    marker_bytes = window.credential_store.marker.read_bytes()
    credentials = dict(backend.values)
    window.destination.setText('rtmps://changed.invalid/app')
    window.stream_key.setText('DUMMY-UNSAVED-KEY')
    calls = list(backend.calls)
    window.restore_stream_button.click()
    assert window.destination.text() == 'rtmps://changed.invalid/app'
    assert window.stream_key.text() == 'DUMMY-UNSAVED-KEY'
    window.forget_stream_button.click()
    assert window.credential_store.settings_file.read_bytes() == saved_bytes
    assert window.credential_store.marker.read_bytes() == marker_bytes
    assert backend.values == credentials and backend.calls == calls
    assert 'Unsaved' in window.stream_profile_status.text()


def test_dirty_state_returns_to_saved_when_the_operator_reverts_values(operator):
    window, _, _ = operator
    enter_stream(window)
    window.save_stream_profile()
    window.bitrate.setValue(28501)
    assert 'Unsaved changes' in window.stream_profile_status.text()
    window.bitrate.setValue(28500)
    assert window.stream_profile_status.text().startswith('Saved')
    window.destination.setText(SERVER + '-changed')
    assert 'Unsaved changes' in window.stream_profile_status.text()
    window.destination.setText(SERVER)
    assert window.stream_profile_status.text().startswith('Saved')


def test_mode_switch_keeps_recording_filename_and_stream_target_separate(operator, tmp_path):
    window, _, _ = operator
    recording = str(tmp_path / 'test recording.mp4')
    window.destination.setText(recording)
    assert window.choose_recording_button.isEnabled()
    enter_stream(window)
    assert not window.choose_recording_button.isEnabled() and window.stream_key.isEnabled()
    assert window.destination.echoMode() == QLineEdit.EchoMode.Password
    window.output_mode.setCurrentIndex(0)
    assert window.destination.text() == recording
    assert window.destination.echoMode() == QLineEdit.EchoMode.Normal
    assert not window.stream_key.isEnabled() and not window.save_stream_button.isEnabled()
    window.output_mode.setCurrentIndex(1)
    assert window.destination.text() == SERVER and window.stream_key.text() == KEY
    window.export.start.assert_not_called()


def test_bitrate_typing_presets_and_keyboard_save_cannot_start_output(operator):
    window, backend, _ = operator
    enter_stream(window)
    window.show()
    broadcast_tab = next(i for i in range(window.tabs.count()) if window.tabs.tabText(i) == 'Broadcast')
    window.tabs.setCurrentIndex(broadcast_tab)
    editor = window.bitrate.lineEdit()
    editor.selectAll()
    QTest.keyClicks(editor, '28500')
    QTest.keyClick(editor, Qt.Key.Key_Tab)
    assert window.bitrate.value() == 28500
    assert '28.5 Mbps' in window.bitrate_hint.text()
    assert window.bitrate.maximum() >= 28500
    window._apply_quality_preset(1)
    assert window.bitrate.value() == 8000 and window.resolution.currentIndex() == 0
    window._apply_quality_preset(2)
    assert window.bitrate.value() == 14000 and window.resolution.currentIndex() == 1
    window.bitrate.setValue(28500)
    window.stream_key.setFocus()
    QApplication.processEvents()
    QTest.keyClick(window.stream_key, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    assert window.credential_store.settings_file.exists()
    assert backend.calls == []
    window.export.start.assert_not_called()


@pytest.mark.parametrize('destination', [
    'rtmps://dummy:DUMMY-PASSWORD@private.invalid/live2',
    'rtmps://private.invalid/live2?token=DUMMY-QUERY',
    'rtmps://private.invalid/live2/DUMMY-INLINE-KEY',
])
def test_credential_bearing_url_is_not_written_to_ordinary_state_or_exports(operator, tmp_path, destination):
    window, backend, _ = operator
    enter_stream(window, destination=destination)
    assert window.stream_key.echoMode() == QLineEdit.EchoMode.Password
    window.save_stream_profile()
    assert 'Not saved' in window.stream_profile_status.text()
    assert not window.credential_store.settings_file.exists() and backend.calls == []
    assert window.save_session()
    exported = tmp_path / 'portable playlist.json'
    write_profile(exported, window.all_tracks, window.backgrounds, window.lyric_files,
                  window.visual_settings(), window.profile_name)
    window._broadcast_error(f'{destination} {KEY} DUMMY-PASSWORD DUMMY-QUERY DUMMY-INLINE-KEY')
    public = (window.settings_file.read_text() + (window.data_dir / 'session.json').read_text()
              + exported.read_text() + window.output_diagnostics.path.read_text())
    feedback = window.broadcast_label.text() + window.statusBar().currentMessage()
    for value in (destination, KEY):
        assert value not in public and value not in feedback
    for value in ('DUMMY-PASSWORD', 'DUMMY-QUERY', 'DUMMY-INLINE-KEY'):
        if value in destination:
            assert value not in feedback and value not in public


def test_help_button_opens_bundled_broadcast_topic_without_external_browser(operator, monkeypatch):
    window, _, _ = operator
    from gnb_player import help_dialog
    opened = []
    monkeypatch.setattr(help_dialog.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    window.broadcast_help_button.click()
    dialog = window._help_dialog
    assert dialog.isVisible() and dialog.topics.currentData() == 'broadcast'
    assert '28,500 kbps is 28.5 Mbps' in dialog.text.toPlainText()
    assert opened == []
    dialog.close()
    assert window._help_dialog is dialog
    window.export.start.assert_not_called()


def test_missing_protected_reference_keeps_ordinary_settings_available(operator):
    window, _, reopen = operator
    enter_stream(window, remember=True)
    window.save_stream_profile()
    window.credential_store.marker.unlink()  # Disposable fake reference only.
    restored = reopen()
    assert restored.destination.text() == SERVER
    assert restored.bitrate.value() == 28500 and restored.resolution.currentIndex() == 1
    assert restored.stream_key.text() == ''
    assert 'unavailable' in restored.stream_profile_status.text().lower()
    restored.export.start.assert_not_called()


def test_invalid_typed_bitrate_preserves_saved_profile_and_explains_range(operator):
    window, backend, _ = operator
    enter_stream(window)
    window.save_stream_profile()
    saved = window.credential_store.settings_file.read_bytes()
    window.bitrate.lineEdit().setText('100001 kbps')
    window.save_stream_profile()
    assert window.credential_store.settings_file.read_bytes() == saved
    assert 'Not saved' in window.stream_profile_status.text()
    assert '100,000 kbps' in window.stream_profile_status.text()
    assert backend.calls == []
    window.export.start.assert_not_called()


def test_saved_state_compares_canonical_server_and_key_values(operator):
    window, _, _ = operator
    enter_stream(window, destination='  '+SERVER+'  ', key='  '+KEY+'  ')
    window.save_stream_profile()
    saved = json.loads(window.credential_store.settings_file.read_text())
    assert saved['destination'] == SERVER
    window.destination.setText(SERVER)
    window.stream_key.setText(KEY)
    assert window.stream_profile_status.text().startswith('Saved')


@pytest.mark.parametrize('size', [(1370, 900), (1030, 720)])
def test_saved_state_and_output_actions_stay_visible_at_supported_window_sizes(operator, size):
    window, _, _ = operator
    window.setStyleSheet(app_module.STYLE)
    enter_stream(window)
    window.save_stream_profile()
    broadcast_tab = next(i for i in range(window.tabs.count()) if window.tabs.tabText(i) == 'Broadcast')
    window.tabs.setCurrentIndex(broadcast_tab)
    window.resize(*size)
    window.show()
    QApplication.processEvents()
    assert not window.monitor_panel.isVisible()
    assert not window.destination.visibleRegion().isEmpty()
    for control in (window.save_stream_button, window.restore_stream_button,
                    window.stream_profile_status, window.start_output, window.stop_output):
        region = control.visibleRegion().boundingRect()
        assert not region.isEmpty()
        assert region.height() == control.height()
    window.export.start.assert_not_called()


def test_broadcast_preview_is_explicit_and_restores_playback_access_without_starting(operator):
    window, _, _ = operator
    enter_stream(window)
    window.show()
    broadcast_tab = next(i for i in range(window.tabs.count()) if window.tabs.tabText(i) == 'Broadcast')
    window.tabs.setCurrentIndex(broadcast_tab)
    QApplication.processEvents()
    before = (window.engine.playing, window.engine.position, window.engine.current_track, window.stage.isVisible())
    assert not window.monitor_panel.isVisible()
    window.broadcast_preview_toggle.setChecked(True)
    assert window.monitor_panel.isVisible() and window.play_button.isVisible()
    window.broadcast_preview_toggle.setChecked(False)
    assert not window.monitor_panel.isVisible()
    window.tabs.setCurrentIndex(0)
    assert window.monitor_panel.isVisible()
    assert (window.engine.playing, window.engine.position, window.engine.current_track, window.stage.isVisible()) == before
    window.export.start.assert_not_called()


def test_forget_after_reopen_removes_an_interrupted_first_protected_copy(operator, monkeypatch):
    window, backend, reopen = operator
    def interrupted_marker(*args):
        raise OSError('Dummy interruption before the protected selection commits')
    window.credential_store._write_marker = interrupted_marker
    enter_stream(window, remember=True)
    window.save_stream_profile()
    assert backend.values and not window.credential_store.marker.exists()
    assert window.credential_store.protected_attempt_file.exists()
    restored = reopen()
    assert not restored.remember_stream_key.isChecked() and not restored.stream_key.text()
    monkeypatch.setattr(QMessageBox, 'question', lambda *a, **k: QMessageBox.StandardButton.Yes)
    restored.forget_stream_profile()
    assert not backend.values
    assert not restored.credential_store.protected_attempt_file.exists()
    assert not restored.credential_store.settings_file.exists()
    assert 'Forgot saved' in restored.stream_profile_status.text()
