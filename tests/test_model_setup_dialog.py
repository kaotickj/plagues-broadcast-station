import json
import os
from pathlib import Path
import sys

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication
import pytest

from gnb_player.extraction import ExtractionConfig
from gnb_player.model_discovery import DiscoveryResult,ModelCandidate,PythonRuntime,OllamaInfo
from gnb_player.model_setup_dialog import ModelSetupDialog,COMPONENTS


@pytest.fixture
def wizard(tmp_path):
    app=QApplication.instance() or QApplication([])
    widget=ModelSetupDialog(ExtractionConfig(),tmp_path/'state')
    yield widget
    widget.poll.stop();widget._busy=False
    for task in widget.tasks:task.requestInterruption();task.wait(6000)
    widget.close();app.processEvents()


def ready_discovery(tmp_path):
    models=[]
    for key,(kind,_,_) in COMPONENTS.items():
        models.append(ModelCandidate(kind,str(tmp_path/key),True,'Compatible fixture'))
    runtime=PythonRuntime(sys.executable,'3.13.5',{},True,'Compatible fixture')
    return DiscoveryResult(models=models,runtimes=[runtime],ollama=OllamaInfo(reachable=True,models=[{'name':'qwen3:8b'}]))


def test_opening_wizard_never_starts_installation(wizard):
    assert wizard.process.state()==QProcess.ProcessState.NotRunning
    assert not wizard.data_dir.exists()
    assert not wizard.reuse.isEnabled()


def test_discovered_models_are_reused_at_original_separate_locations(wizard,tmp_path):
    data=ready_discovery(tmp_path);wizard._found(data)
    plan=wizard._plan()
    assert plan.runtime_python==sys.executable
    assert len(plan.reuse)==4
    assert plan.reuse['transcription_model_path']==data.models[0].path
    assert wizard.reuse.isEnabled()
    assert 'large-v3' in wizard.plan_text.toPlainText()
    assert '0.0 GiB' in wizard.plan_text.toPlainText()


def test_reuse_button_uses_offline_worker_flag_and_saves_no_settings_until_success(wizard,tmp_path,monkeypatch):
    wizard._found(ready_discovery(tmp_path));calls=[];applied=[]
    wizard.configured.connect(applied.append)
    monkeypatch.setattr(wizard.process,'start',lambda executable,arguments:calls.append([executable,*arguments]))
    wizard.start_reuse()
    assert '--reuse-only' in calls[0] and '--allow-downloads' not in calls[0]
    assert applied==[] and wizard._busy
    assert json.loads(next((wizard.data_dir/'model-setup-jobs').glob('*/plan.json')).read_text())['runtime_python']==sys.executable
    wizard._finished(1,QProcess.ExitStatus.NormalExit)
    assert applied==[] and not wizard._busy


def test_cancelled_success_result_cannot_replace_existing_settings(wizard,tmp_path):
    wizard._result_file=tmp_path/'result.json';wizard._result_file.write_text(json.dumps({'python_path':'changed'}))
    wizard._cancelled=True;applied=[];wizard.configured.connect(applied.append)
    wizard._finished(0,QProcess.ExitStatus.NormalExit)
    assert applied==[] and wizard.config.python_path==''


def test_progress_file_handles_partial_utf8_lines(wizard,tmp_path):
    wizard._events=tmp_path/'events.jsonl'
    line=(json.dumps({'type':'progress','message':'Checking the café model','fraction':.5},ensure_ascii=False)+'\n').encode()
    split=line.index('é'.encode())+1
    wizard._events.write_bytes(line[:split]);wizard._read_events()
    assert wizard.progress.value()==-1
    with wizard._events.open('ab') as file:file.write(line[split:])
    wizard._read_events()
    assert wizard.progress.value()==50 and 'café' in wizard.log.toPlainText()


def test_ollama_unavailable_preserves_saved_model_and_audio_configuration(wizard):
    wizard.config.ollama_model='qwen3:8b'
    wizard._ollama_found(OllamaInfo(reachable=False,reason='Service is stopped.'))
    assert wizard.ollama_model.currentData()=='qwen3:8b'
    wizard.apply_ollama()
    assert wizard.result_config.ollama_model=='qwen3:8b'
    assert wizard.result_config.engine=='large-v3'


def test_cancelled_discovery_preserves_user_selections(wizard,tmp_path):
    wizard._found(ready_discovery(tmp_path))
    before=wizard._plan()
    wizard._found(DiscoveryResult(cancelled=True))
    assert wizard._plan()==before


def test_rescan_preserves_selected_runtime(wizard,tmp_path):
    result=ready_discovery(tmp_path)
    wizard._found(result)
    result.runtimes.append(PythonRuntime('/other/python','3.13.17',{},True,'Ready'))
    wizard._found(result)
    assert wizard._plan().runtime_python==sys.executable


@pytest.mark.parametrize('result',[{},[],{'type':'error'}])
def test_invalid_success_result_cannot_replace_settings(wizard,tmp_path,result):
    wizard._result_file=tmp_path/'result.json'
    wizard._result_file.write_text(json.dumps(result))
    wizard._finished(0,QProcess.ExitStatus.NormalExit)
    assert wizard.result_config is None


def test_escape_uses_close_cancellation_path(wizard,monkeypatch):
    closed=[]
    monkeypatch.setattr(wizard,'close',lambda:closed.append(True))
    wizard.reject()
    assert closed==[True]


def test_automatic_selection_prefers_compatible_runtime_over_base_python(wizard,tmp_path):
    wizard.config.python_path='/system/base-python'
    result=ready_discovery(tmp_path)
    result.runtimes.insert(0,PythonRuntime('/system/base-python','3.13.5',{},False,'missing model packages'))
    wizard._found(result)
    assert wizard._plan().runtime_python==sys.executable
    assert 'Selected lyric runtime: '+sys.executable in wizard.findings.toPlainText()
    assert 'candidate diagnostics' in wizard.findings.toPlainText()


def test_deliberate_base_python_selection_survives_rescan(wizard,tmp_path):
    result=ready_discovery(tmp_path)
    result.runtimes.append(PythonRuntime('/system/base-python','3.13.5',{},False,'missing model packages'))
    wizard._found(result)
    wizard.runtime.setCurrentIndex(2)
    wizard.runtime.activated.emit(2)
    wizard._found(result)
    assert wizard._plan().base_python=='/system/base-python'
    assert wizard._plan().runtime_python==''


def test_step_navigation_and_help_never_start_jobs_or_write_state(wizard):
    app = QApplication.instance()
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    wizard.show(); wizard.activateWindow(); app.processEvents()
    assert not wizard.back.isEnabled() and wizard.next.isEnabled()
    wizard.next.click()
    assert wizard.tabs.currentIndex() == 1 and wizard.back.isEnabled()
    wizard.next.click()
    assert wizard.tabs.currentIndex() == 2 and not wizard.next.isEnabled()
    wizard.back.click()
    assert wizard.tabs.currentIndex() == 1
    QTest.keyClick(wizard, Qt.Key.Key_F1)
    app.processEvents()
    assert wizard.help_dialog.isVisible()
    assert wizard.help_dialog.topics.currentData() == 'models'
    assert 'Ollama' in wizard.help_dialog.text.toPlainText()
    QTest.keyClick(wizard.help_dialog, Qt.Key.Key_Escape)
    app.processEvents()
    assert not wizard.help_dialog.isVisible() and wizard.isVisible()
    assert wizard.process.state() == QProcess.ProcessState.NotRunning
    assert not wizard.tasks and wizard.result_config is None and not wizard.data_dir.exists()


def test_ready_plan_explains_offline_reuse_and_preserves_cancelled_state(wizard, tmp_path):
    wizard._found(ready_discovery(tmp_path))
    assert 'All components were found' in wizard.next_step.text()
    assert 'without downloads' in wizard.next_step.text()
    previous = wizard._plan()
    wizard._found(DiscoveryResult(cancelled=True))
    assert wizard._plan() == previous
    assert 'Scan cancelled' in wizard.next_step.text()
    assert wizard.result_config is None and not wizard.data_dir.exists()


def test_model_setup_steps_scroll_at_smaller_window_sizes(wizard):
    from PySide6.QtWidgets import QScrollArea
    wizard.resize(680, 480)
    for index in range(wizard.tabs.count()):
        assert isinstance(wizard.tabs.widget(index), QScrollArea)
        assert wizard.tabs.widget(index).widgetResizable()
    from PySide6.QtWidgets import QLabel
    assert 'broadcasting do not require lyric models' in wizard.findChild(QLabel, 'model_setup_intro').text()
