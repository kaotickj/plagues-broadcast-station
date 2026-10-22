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
