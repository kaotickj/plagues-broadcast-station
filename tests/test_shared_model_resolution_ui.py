"""Automatic local model paths shown/saved through Qt; synthetic files only."""
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND', 'ffmpeg')

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from gnb_player import extraction
from gnb_player.app import MainWindow


COMPONENT_FIELDS = {
    'transcription': 'transcription_model_path',
    'review': 'review_model_path',
    'separator': 'separator_model_path',
    'alignment': 'alignment_model_path',
}


def settle(window, condition=lambda: True, seconds=4):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        QApplication.processEvents()
        if not window.tasks and condition():
            return
        QTest.qWait(10)
    raise AssertionError('Temporary station settings did not settle')


@pytest.fixture
def layout(tmp_path, monkeypatch):
    userbase = tmp_path / 'user-default'
    shared = tmp_path / 'shared'
    paths = {
        'model_root': shared / 'models',
        'torch_home': shared / 'torch',
        'python_path': shared / 'runtime/bin/python',
        'gpu_python_path': shared / 'gpu-runtime/bin/python',
        'vulkan_binary': shared / 'vulkan/bin/whisper-cli',
        'vulkan_model': shared / 'vulkan/ggml-large-v3-q5_0.bin',
        'gpu_lock': shared / 'gpu.lock',
    }
    for name in tuple(os.environ):
        if name.startswith('GNB_PLAYER_'):
            monkeypatch.delenv(name)
    monkeypatch.setattr(extraction, 'default_data_dir', lambda: userbase)
    monkeypatch.setattr(extraction, 'LINUX_SHARED_PATHS', paths)

    def file(path, *, executable=False):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic presence sentinel; never executed or loaded.')
        if executable:
            path.chmod(0o755)
        return path

    def complete_models(model_root, torch_home):
        config = extraction.ExtractionConfig(model_root=str(model_root), torch_home=str(torch_home),
                                             explicit_paths=['model_root', 'torch_home'])
        for path in extraction.required_model_files(config):
            file(path)

    complete_models(paths['model_root'], paths['torch_home'])
    for key, path in paths.items():
        if key not in ('model_root', 'torch_home'):
            file(path, executable=key in ('python_path', 'gpu_python_path', 'vulkan_binary'))
    return SimpleNamespace(paths=paths, userbase=userbase, file=file, complete_models=complete_models)


@pytest.fixture
def station(tmp_path, layout):
    app = QApplication.instance() or QApplication([])
    windows = []

    def create(name='station', config=None):
        directory = tmp_path / name
        if config is not None:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'settings.json').write_text(json.dumps({'extraction': config.to_dict()}))
        window = MainWindow(directory)
        windows.append(window)
        settle(window)
        return window

    yield create
    for window in windows:
        dialog = getattr(window, 'model_setup_dialog', None)
        if dialog is not None:
            dialog.close()
        settle(window)
        window._lyric_buffers.clear()
        window._closing = True
        window.close()
        app.processEvents()


def show_models(window):
    window.show()
    window.tabs.setCurrentIndex(3)
    QApplication.processEvents()
    return window.tabs.widget(3)


def save_through_gui(window):
    scroll = show_models(window)
    button = next(button for button in window.findChildren(QPushButton)
                  if button.text() == 'Save model settings')
    scroll.ensureWidgetVisible(button)
    QApplication.processEvents()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle(window)
    expected = window.extract_config.to_dict()

    def persisted():
        try:
            return json.loads(window.settings_file.read_text())['extraction'] == expected
        except (FileNotFoundError, KeyError):
            return False

    settle(window, persisted)


def type_path(window, field, value):
    scroll = show_models(window)
    entry = window.model_fields[field]
    scroll.ensureWidgetVisible(entry)
    QApplication.processEvents()
    entry.setFocus()
    QTest.keyClick(entry, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    if value:
        QTest.keyClicks(entry, str(value))
    else:
        QTest.keyClick(entry, Qt.Key.Key_Backspace)


def test_shared_resolution_is_visible_without_populating_component_overrides(station, layout):
    window = station()
    for field, path in layout.paths.items():
        assert window.model_fields[field].text() == str(path)
    effective = extraction.effective_model_paths(window.extract_config)
    for component, field in COMPONENT_FIELDS.items():
        entry = window.model_fields[field]
        assert entry.text() == ''
        assert entry.placeholderText() == 'Automatic: ' + str(effective[component])
        assert str(effective[component]) in entry.toolTip()
    assert window.model_resolution_status.accessibleName() == 'Automatic model location discovery'
    assert 'shared' in window.model_resolution_status.text().lower()
    assert window.extract_config.cache_dir == str(window.data_dir / 'lyrics-cache')
    assert not window.engine.playing and not window.export.running


def test_legacy_missing_default_folders_use_existing_shared_locations(station, layout):
    legacy = extraction.ExtractionConfig(model_root=str(layout.userbase / 'models'),
                                         torch_home=str(layout.userbase / 'torch'), use_gpu=False)
    window = station(config=legacy)
    assert window.model_fields['model_root'].text() == str(layout.paths['model_root'])
    assert window.model_fields['torch_home'].text() == str(layout.paths['torch_home'])
    assert window.model_fields['python_path'].text() == str(layout.paths['python_path'])
    assert not window.gpu.isChecked() and not window.extract_config.use_gpu


def test_missing_shared_component_is_explained_without_creating_it(station, layout):
    alignment = extraction.effective_model_paths(extraction.ExtractionConfig(
        model_root=str(layout.paths['model_root']), torch_home=str(layout.paths['torch_home'])))['alignment']
    alignment.unlink()
    window = station()
    fallback = layout.userbase / 'torch' / alignment.relative_to(layout.paths['torch_home'])
    assert not extraction.config_status(window.extract_config)['ready']
    assert str(fallback) in window.model_status.text()
    assert not alignment.exists() and not fallback.exists()
    assert str(fallback) in window.model_fields['alignment_model_path'].placeholderText()


@pytest.mark.parametrize('exists', [True, False])
def test_custom_root_and_runtime_are_preserved_when_valid_or_missing(station, layout, tmp_path, exists):
    root, torch, runtime = tmp_path/'chosen-models', tmp_path/'chosen-torch', tmp_path/'chosen-python'
    if exists:
        layout.complete_models(root, torch)
        layout.file(runtime, executable=True)
    config = extraction.ExtractionConfig(model_root=str(root), torch_home=str(torch),
                                         python_path=str(runtime), use_gpu=False)
    window = station(config=config)
    assert window.model_fields['model_root'].text() == str(root)
    assert window.model_fields['torch_home'].text() == str(torch)
    assert window.model_fields['python_path'].text() == str(runtime)
    assert window.model_fields['transcription_model_path'].placeholderText() == 'Automatic: ' + str(root/'large-v3')
    if not exists:
        assert str(root/'large-v3/model.bin') in window.model_status.text()
    save_through_gui(window)
    restored = station()
    assert restored.extract_config.model_root == str(root)
    assert restored.extract_config.python_path == str(runtime)
    assert not restored.extract_config.use_gpu


@pytest.mark.parametrize('exists', [True, False])
def test_custom_component_override_stays_literal_across_gui_save(station, layout, tmp_path, exists):
    chosen = tmp_path/'chosen-alignment.pt'
    if exists:
        layout.file(chosen)
    config = extraction.ExtractionConfig(alignment_model_path=str(chosen), use_gpu=False)
    window = station(config=config)
    assert window.model_fields['alignment_model_path'].text() == str(chosen)
    if not exists:
        assert str(chosen) in window.model_status.text()
    type_path(window, 'alignment_model_path', chosen)
    save_through_gui(window)
    restored = station()
    assert restored.extract_config.alignment_model_path == str(chosen)
    assert 'alignment_model_path' in restored.extract_config.explicit_paths
    type_path(restored, 'alignment_model_path', '')
    save_through_gui(restored)
    assert restored.extract_config.alignment_model_path == ''
    assert 'alignment_model_path' not in restored.extract_config.explicit_paths
    assert str(layout.paths['torch_home']) in restored.model_fields['alignment_model_path'].placeholderText()


def test_typing_canonical_default_marks_intent_and_blank_restores_automatic(station, layout):
    window = station()
    canonical = layout.userbase/'models'
    type_path(window, 'model_root', canonical)
    save_through_gui(window)
    assert window.extract_config.model_root == str(canonical)
    assert 'model_root' in window.extract_config.explicit_paths
    restored = station()
    assert restored.extract_config.model_root == str(canonical)
    type_path(restored, 'model_root', '')
    save_through_gui(restored)
    assert restored.extract_config.model_root == str(layout.paths['model_root'])
    assert 'model_root' not in restored.extract_config.explicit_paths


def test_saving_untouched_automatic_fields_does_not_mark_them_explicit(station):
    window = station()
    save_through_gui(window)
    assert window.extract_config.explicit_paths == []
    assert station().extract_config.explicit_paths == []


def test_custom_runtime_command_on_path_has_no_false_missing_file_diagnostic(station, layout, tmp_path, monkeypatch):
    executable = layout.file(tmp_path/'command-runtime', executable=True)
    original_which = extraction.shutil.which
    monkeypatch.setattr(extraction.shutil, 'which',
                        lambda name: str(executable) if name == 'chosen-python' else original_which(name))
    window = station(config=extraction.ExtractionConfig(python_path='chosen-python', use_gpu=False))
    assert window.extract_config.python_path == 'chosen-python'
    assert extraction.config_status(window.extract_config)['python_available']
    assert window._model_resolution_report['fields']['python_path']['missing_files'] == []


def test_station_settings_and_cache_are_isolated(station, layout, tmp_path):
    first, second = station('first'), station('second')
    second.save_session()
    second_before = second.settings_file.read_bytes()
    custom = layout.file(tmp_path/'first-only-python', executable=True)
    type_path(first, 'python_path', custom)
    save_through_gui(first)
    assert second.settings_file.read_bytes() == second_before
    assert second.extract_config.python_path == str(layout.paths['python_path'])
    assert station('first').extract_config.python_path == str(custom)
    assert station('second').extract_config.python_path == str(layout.paths['python_path'])
    assert first.extract_config.cache_dir != second.extract_config.cache_dir


def test_model_setup_receives_resolved_config_without_starting_scan_or_install(station, layout):
    window = station()
    scroll = show_models(window)
    button = next(button for button in window.findChildren(QPushButton)
                  if button.text() == 'Set up / find models')
    scroll.ensureWidgetVisible(button)
    QApplication.processEvents()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    dialog = window.model_setup_dialog
    assert dialog.isVisible()
    assert dialog.config.python_path == str(layout.paths['python_path'])
    assert dialog.config.model_root == str(layout.paths['model_root'])
    assert not dialog.tasks and dialog.process.state() == dialog.process.ProcessState.NotRunning
    assert not dialog._busy
    dialog.close()
    assert window.isVisible()
