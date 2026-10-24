"""Model setup must not select CPU/GPU policy; temporary files and mocked jobs."""
import json
from pathlib import Path
import sys

import pytest
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication

from gnb_player.extraction import ExtractionConfig
from gnb_player.model_install import COMPONENTS, create_plan
from gnb_player import model_install_worker as worker
from gnb_player.model_setup_dialog import ModelSetupDialog
from test_app import window


def prepared_settings(tmp_path):
    return {
        'python_path': str(tmp_path / 'new-runtime/bin/python'),
        'model_root': str(tmp_path / 'new-models'),
        'torch_home': str(tmp_path / 'new-torch'),
        **{name: str(tmp_path / 'validated-models' / name) for name in COMPONENTS},
        'engine': 'large-v3', 'auto_review': True, 'separate_vocals': True,
    }


@pytest.mark.parametrize('create_cpu_dependencies', [False, True])
def test_controller_runtime_creation_is_independent_of_acceleration_policy(tmp_path, monkeypatch, create_cpu_dependencies):
    settings = prepared_settings(tmp_path)
    plan = create_plan(tmp_path / 'managed-models',
                       **({'base_python': sys.executable} if create_cpu_dependencies else {'runtime_python': sys.executable}))
    calls = []
    def command(args, phase, **kwargs):
        calls.append((args, phase))
        if '--prepare-models' in args:
            Path(args[args.index('--result-file') + 1]).write_text(json.dumps(settings))
    monkeypatch.setattr(worker, 'run_command', command)
    monkeypatch.setattr(worker, 'check_python', lambda selected: None)
    monkeypatch.setattr(worker, 'describe_plan', lambda selected: {'enough_space': True, 'profile': 'fixture quality runtime'})
    monkeypatch.setattr(worker, 'progress', lambda *args: None)
    result = worker.controller(plan)
    assert 'use_gpu' not in result
    assert not any(name in result for name in ('vulkan_binary', 'vulkan_model', 'gpu_python_path', 'gpu_lock'))
    assert result['engine'] == 'large-v3'
    assert result['python_path'] == (str(next((tmp_path/'managed-models/versions').iterdir()) / 'runtime' / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')) if create_cpu_dependencies else sys.executable)
    for name in COMPONENTS:
        assert result[name] == settings[name]
    runtime_calls = [args for args, phase in calls if phase == 'runtime']
    if create_cpu_dependencies:
        assert any('-m' in args and 'venv' in args for args in runtime_calls)
        assert any('https://download.pytorch.org/whl/cpu' in args for args in runtime_calls)
    else:
        assert not runtime_calls
    # These are controller fixtures, not real creation or installation claims.
    assert not any(p.name == 'python.exe' or p.name == 'python' for p in tmp_path.rglob('*'))


@pytest.mark.parametrize('current_gpu', [False, True])
@pytest.mark.parametrize('legacy_worker_gpu', [None, False, True])
def test_setup_finish_preserves_current_gpu_choice_paths_and_other_preferences(
        tmp_path, current_gpu, legacy_worker_gpu):
    app = QApplication.instance() or QApplication([])
    paths = {
        'gpu_python_path': str(tmp_path / 'existing-acceleration/bin/python'),
        'vulkan_binary': str(tmp_path / 'existing-acceleration/whisper-cli'),
        'vulkan_model': str(tmp_path / 'existing-models/large-v3-q5_0.bin'),
        'gpu_lock': str(tmp_path / 'existing-coordination/gpu.lock'),
    }
    original = ExtractionConfig(use_gpu=current_gpu, wait_for_gpu=False, gpu_wait_seconds=71,
                                cpu_threads=4, reuse_recognition=True, separate_vocals=False, auto_review=False,
                                explicit_paths=list(paths), recognition_prompt='Operator supplied context', **paths)
    prior = original.to_dict()
    dialog = ModelSetupDialog(original, tmp_path / 'station')
    try:
        settings = prepared_settings(tmp_path)
        if legacy_worker_gpu is not None:
            settings['use_gpu'] = legacy_worker_gpu
        # Older/external results cannot erase configured acceleration paths,
        # flip wait/reuse policy or discard the explicit-custom-path markers.
        settings.update({name: '' for name in paths})
        settings.update(wait_for_gpu=True, gpu_wait_seconds=0, reuse_recognition=False,
                        explicit_paths=[], cpu_threads=1, recognition_prompt='Unexpected worker context')
        dialog._result_file = tmp_path / 'worker-result.json'
        dialog._result_file.write_text(json.dumps({'settings': settings}))
        applied = []
        dialog.configured.connect(applied.append)
        dialog._finished(0, QProcess.ExitStatus.NormalExit)
        assert len(applied) == 1 and dialog.result_config is applied[0]
        result = applied[0]
        assert result.use_gpu is current_gpu
        for name, value in paths.items():
            assert getattr(result, name) == value
        assert result.wait_for_gpu is False and result.gpu_wait_seconds == 71
        assert result.reuse_recognition is True and result.cpu_threads == 4
        assert result.separate_vocals is False and result.auto_review is False
        assert result.explicit_paths == list(paths)
        assert result.recognition_prompt == 'Operator supplied context'
        assert result.python_path == settings['python_path']
        assert result.model_root == settings['model_root'] and result.torch_home == settings['torch_home']
        assert result.cache_dir == str(tmp_path / 'station/lyrics-cache')
        for name in COMPONENTS:
            assert getattr(result, name) == settings[name]
        assert original.to_dict() == prior, 'Setup must copy the incoming config, not mutate its caller.'
        assert dialog.status.text().startswith('Quality lyric models are ready.')
    finally:
        dialog.poll.stop();dialog._busy = False;dialog.close();app.processEvents()


@pytest.mark.parametrize('gpu_before_open', [False, True])
@pytest.mark.parametrize('change_while_setup_open', [False, True])
def test_setup_open_and_finish_use_current_visible_preferences_not_stale_config(
        window, tmp_path, gpu_before_open, change_while_setup_open):
    app = QApplication.instance()
    # Deliberately leave the internal config opposite to the visible checkbox.
    window.extract_config.use_gpu = not gpu_before_open
    window.extract_config.separate_vocals = True
    window.extract_config.reuse_recognition = True
    window.gpu.setChecked(gpu_before_open)
    window.separate_vocals.setChecked(False)
    window.reuse_recognition.setChecked(False)
    window.open_model_setup()
    dialog = window.model_setup_dialog
    try:
        assert dialog.config.use_gpu is gpu_before_open
        assert dialog.config.separate_vocals is False
        assert dialog.config.reuse_recognition is False
        assert dialog.process.state() == QProcess.ProcessState.NotRunning
        gpu_at_finish = not gpu_before_open if change_while_setup_open else gpu_before_open
        window.gpu.setChecked(gpu_at_finish)
        if change_while_setup_open:
            window.separate_vocals.setChecked(True)
            window.reuse_recognition.setChecked(True)
        settings = prepared_settings(tmp_path)
        settings.update(use_gpu=not gpu_at_finish, separate_vocals=True, reuse_recognition=True)
        dialog._result_file = tmp_path / 'finished-setup.json'
        dialog._result_file.write_text(json.dumps({'settings': settings}))
        dialog._finished(0, QProcess.ExitStatus.NormalExit)
        assert window.extract_config.use_gpu is gpu_at_finish
        assert window.gpu.isChecked() is gpu_at_finish
        assert window.extract_config.separate_vocals is change_while_setup_open
        assert window.separate_vocals.isChecked() is change_while_setup_open
        assert window.extract_config.reuse_recognition is change_while_setup_open
        assert window.reuse_recognition.isChecked() is change_while_setup_open
        assert window.extract_config.python_path == settings['python_path']
        assert dialog.process.state() == QProcess.ProcessState.NotRunning
        assert not dialog.tasks, 'This test must not trigger discovery or model jobs.'
    finally:
        dialog.poll.stop();dialog._busy = False;dialog.close();app.processEvents()
