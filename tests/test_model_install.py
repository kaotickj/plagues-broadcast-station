import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from gnb_player.model_install import (COMPONENTS, MODEL_ASSETS, PYTHON_ASSET, InstallPlan, create_plan,
                                     describe_plan, installer_command, save_plan)

WORKER=Path(__file__).resolve().parents[1]/'src/gnb_player/model_install_worker.py'
spec=importlib.util.spec_from_file_location('setup_worker_for_test',WORKER)
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)


def test_quality_plan_is_local_and_reuses_models_without_copying(tmp_path):
    reused={key:str(tmp_path/key) for key in COMPONENTS}
    with patch('urllib.request.build_opener',side_effect=AssertionError('Planning must not access network')):
        plan=create_plan(tmp_path/'managed',runtime_python=sys.executable,reuse=reused)
        report=describe_plan(plan)
    assert report['engine']=='large-v3' and report['model_download_bytes']==0
    assert report['runtime_action']=='reuse' and all(step['action']=='reuse' for step in report['steps'])
    assert not (tmp_path/'managed').exists()


def test_missing_quality_bundle_reports_large_models_disk_and_no_tiny_fallback(tmp_path):
    report=describe_plan(create_plan(tmp_path/'AI models',base_python=sys.executable))
    assert report['model_download_bytes']>5_000_000_000
    assert report['required_free_bytes']>10_000_000_000
    assert any('large-v3' in s['label'] for s in report['steps'])
    assert any('medium.en' in s['label'] for s in report['steps'])
    assert not any('tiny' in str(s) for s in report['steps'])


def test_unknown_settings_relative_paths_and_runtime_conflicts_are_rejected(tmp_path):
    values=[{'install_dir':'relative'},{'install_dir':str(tmp_path),'unexpected':True},
            {'install_dir':str(tmp_path),'reuse':{'other':'x'}},
            {'install_dir':str(tmp_path),'reuse':{'alignment_model_path':'relative'}},
            {'install_dir':str(tmp_path),'runtime_python':'python','base_python':'other'},
            {'install_dir':str(tmp_path),'base_python':'python','bootstrap_python':True}]
    for value in values:
        with pytest.raises(ValueError):InstallPlan.from_dict(value)


def test_saved_plan_and_commands_preserve_spaces_and_private_permissions(tmp_path,monkeypatch):
    plan=create_plan(tmp_path/'AI models',base_python=sys.executable)
    path=save_plan(plan,tmp_path/'setup plan.json')
    args=installer_command(path,tmp_path/'events file.jsonl',tmp_path/'result file.json')
    assert args[args.index('--plan-file')+1]==str(path)
    assert '--allow-downloads' not in args and '--reuse-only' not in args
    assert json.loads(path.read_text())==plan.to_dict()
    if os.name!='nt':assert path.stat().st_mode&0o077==0
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    frozen=installer_command(path,tmp_path/'e',tmp_path/'r')
    assert frozen[:2]==[sys.executable,'--model-install-worker']


def test_plan_size_warning_is_based_on_selected_missing_components(tmp_path):
    usage=type('Disk',(),{'free':1})()
    with patch('gnb_player.model_install.shutil.disk_usage',return_value=usage):
        assert not describe_plan(create_plan(tmp_path/'models',base_python=sys.executable))['enough_space']


def test_all_downloads_are_pinned_to_official_https_sources():
    assets=[asset for group in MODEL_ASSETS.values() for asset in group]+[PYTHON_ASSET]
    for asset in assets:
        assert worker.allowed_url(asset['url'])
        assert asset.get('sha256') or asset.get('git_blob')
        assert asset['size']>0
    assert not worker.allowed_url('http://huggingface.co/file')
    assert not worker.allowed_url('https://huggingface.co.evil.invalid/file')
    assert not worker.allowed_url('https://user:secret@huggingface.co/file')
    assert not worker.allowed_url('file:///model.bin')


def test_redirects_outside_publisher_hosts_are_rejected():
    with pytest.raises(ValueError):
        worker.PublisherRedirect().redirect_request(None,None,302,'',{},'https://unrelated.invalid/model.bin')


def test_checksum_and_git_object_integrity(tmp_path):
    path=tmp_path/'asset';payload=b'hello model';path.write_bytes(payload)
    sha=hashlib.sha256(payload).hexdigest();git=hashlib.sha1(b'blob '+str(len(payload)).encode()+b'\0'+payload).hexdigest()
    assert worker.verify_download(path,{'size':len(payload),'sha256':sha})
    assert worker.verify_download(path,{'size':len(payload),'git_blob':git})
    path.write_bytes(b'wrong model')
    assert not worker.verify_download(path,{'size':len(payload),'sha256':sha})


class Response(io.BytesIO):
    def __init__(self,data,status=200,headers=None):
        super().__init__(data);self.status=status;self.headers=headers or {}


def fixture_asset(data=b'0123456789'):
    return {'name':'fixture.bin','url':'https://huggingface.co/publisher/repo/resolve/fixed/fixture.bin',
            'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}


def test_verified_cache_is_reused_without_network(tmp_path):
    data=b'0123456789';asset=fixture_asset(data)
    path=tmp_path/(asset['sha256']+'-'+asset['name']);path.write_bytes(data)
    with patch.object(worker.urllib.request,'build_opener',side_effect=AssertionError('No download needed')):
        assert worker.download(asset,tmp_path,'fixture')==path


def test_partial_download_resumes_and_validates_before_promotion(tmp_path):
    data=b'0123456789';asset=fixture_asset(data)
    partial=tmp_path/(asset['sha256']+'-'+asset['name']+'.part');partial.write_bytes(data[:4])
    requests=[]
    class Opener:
        def open(self,request,timeout):
            requests.append(request);return Response(data[4:],206,{'Content-Range':'bytes 4-9/10'})
    with patch.object(worker.urllib.request,'build_opener',return_value=Opener()):
        target=worker.download(asset,tmp_path,'fixture')
    assert target.read_bytes()==data and not partial.exists()
    assert requests[0].headers['Range']=='bytes=4-'


def test_corrupt_complete_cache_is_preserved_and_retry_downloads_again(tmp_path):
    data=b'0123456789';asset=fixture_asset(data)
    bad=tmp_path/(asset['sha256']+'-'+asset['name']+'.part');bad.write_bytes(b'x'*len(data))
    class Opener:
        def open(self,request,timeout):return Response(data)
    with patch.object(worker.urllib.request,'build_opener',return_value=Opener()):
        target=worker.download(asset,tmp_path,'fixture')
    assert target.read_bytes()==data
    preserved=list(tmp_path.glob('*.unverified-*'))
    assert len(preserved)==1 and preserved[0].read_bytes()==b'x'*len(data)


def test_wrong_download_fingerprint_is_never_promoted(tmp_path):
    asset=fixture_asset()
    class Opener:
        def open(self,request,timeout):return Response(b'x'*asset['size'])
    with patch.object(worker.urllib.request,'build_opener',return_value=Opener()):
        with pytest.raises(ValueError,match='fingerprint'):worker.download(asset,tmp_path,'fixture')
    assert not (tmp_path/(asset['sha256']+'-'+asset['name'])).exists()


def test_quiet_child_timeout_is_bounded_and_owned_child_is_reaped():
    began=time.monotonic()
    with pytest.raises(TimeoutError):
        worker.run_command([sys.executable,'-c','import time;time.sleep(30)'],'runtime',timeout=.2)
    assert time.monotonic()-began<5 and worker.CHILD is None


def test_python_bootstrap_is_per_user_and_does_not_change_path_or_associations(tmp_path):
    args=worker.python_bootstrap_command(tmp_path/'python.exe',tmp_path/'private python')
    assert 'InstallAllUsers=0' in args and 'PrependPath=0' in args and 'AssociateFiles=0' in args
    assert 'Include_launcher=0' in args and '/quiet' in args
    assert 'TargetDir='+str(tmp_path/'private python') in args


def test_worker_refuses_network_without_explicit_install_action(tmp_path):
    events=tmp_path/'events.jsonl';result=tmp_path/'result.json';result.write_text('{"keep":true}')
    process=subprocess.run([sys.executable,str(WORKER),'--events-file',str(events),'--result-file',str(result)],capture_output=True,text=True,timeout=10)
    assert process.returncode==1
    assert json.loads(events.read_text().splitlines()[-1])['type']=='error'
    assert 'explicit Install' in events.read_text()
    assert json.loads(result.read_text())=={'keep':True}


def test_external_environment_keeps_model_python_separate_from_gui(monkeypatch):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setenv('PYTHONHOME','bad');monkeypatch.setenv('PYTHONPATH','bad')
    monkeypatch.setenv('LD_LIBRARY_PATH','bundle');monkeypatch.setenv('LD_LIBRARY_PATH_ORIG','model-libs')
    monkeypatch.setenv('PIP_EXTRA_INDEX_URL','https://unwanted.invalid')
    environment=worker.external_environment()
    assert 'PYTHONHOME' not in environment and 'PYTHONPATH' not in environment
    assert 'PIP_EXTRA_INDEX_URL' not in environment and environment['PIP_CONFIG_FILE']==os.devnull
    if os.name!='nt':assert environment['LD_LIBRARY_PATH']=='model-libs'


def test_offline_reuse_rejects_missing_parts_before_network_or_changes(tmp_path):
    plan=create_plan(tmp_path/'new install',runtime_python=sys.executable,reuse={})
    with patch.object(worker,'download',side_effect=AssertionError('No network')),patch.object(worker,'run_command',side_effect=AssertionError('No pip/runtime changes')):
        with pytest.raises(ValueError,match='Offline reuse'):worker.controller(plan,reuse_only=True)
    assert not (tmp_path/'new install').exists()


def test_offline_controller_only_runs_validation_and_preserves_existing_runtime(tmp_path):
    reuse={key:str(tmp_path/key) for key in COMPONENTS}
    plan=create_plan(tmp_path/'new install',runtime_python=sys.executable,reuse=reuse)
    calls=[]
    def validate(command,phase,**kwargs):
        calls.append(command)
        assert '--prepare-models' in command and '--reuse-only' in command and '--allow-downloads' not in command
        result=Path(command[command.index('--result-file')+1])
        result.write_text(json.dumps({**reuse,'model_root':str(tmp_path/'models'),'torch_home':str(tmp_path/'torch')}))
    with patch.object(worker,'verify_component'),patch.object(worker,'check_python'),patch.object(worker,'download',side_effect=AssertionError('No network')),patch.object(worker,'run_command',side_effect=validate):
        result=worker.controller(plan,reuse_only=True)
    assert len(calls)==1 and result['python_path']==sys.executable
    assert result['engine']=='large-v3' and result['review_model_path']==reuse['review_model_path']
    assert 'use_gpu' not in result and 'gpu_lock' not in result and 'gpu_python_path' not in result
    assert not any('pip' in command or 'venv' in command for command in calls)


def test_offline_helper_rejects_missing_models_before_importing_runtime(tmp_path):
    request=tmp_path/'request.json'
    request.write_text(json.dumps({'version_dir':str(tmp_path),'cache':str(tmp_path/'cache'),'reuse':{}}))
    with patch.object(worker,'download',side_effect=AssertionError('No network')):
        with pytest.raises(ValueError,match='Offline reuse'):worker.prepare_models(request,allow_downloads=False)


def test_new_runtime_installs_only_into_isolated_venv_with_cpu_pins(tmp_path):
    reuse={key:str(tmp_path/key) for key in COMPONENTS}
    plan=create_plan(tmp_path/'models with spaces',base_python=sys.executable,reuse=reuse)
    calls=[]
    def fake_command(command,phase,**kwargs):
        calls.append(command)
        if '--prepare-models' in command:
            output=Path(command[command.index('--result-file')+1])
            output.write_text(json.dumps({**reuse,'model_root':str(tmp_path/'models'),'torch_home':str(tmp_path/'torch')}))
    with patch.object(worker,'verify_component'),patch.object(worker,'check_python'),patch.object(worker,'download',side_effect=AssertionError('Reused models do not download')),patch.object(worker,'run_command',side_effect=fake_command):
        result=worker.controller(plan)
    assert calls[0][:4]==[sys.executable,'-I','-m','venv']
    pip_commands=[c for c in calls if 'pip' in c]
    assert len(pip_commands)==2 and all(c[0]==result['python_path'] and c[0]!=sys.executable for c in pip_commands)
    assert 'https://download.pytorch.org/whl/cpu' in pip_commands[0]
    assert 'torch==2.8.0' in pip_commands[0] and 'torchaudio==2.8.0' in pip_commands[0]
    constraints=Path(pip_commands[1][pip_commands[1].index('--constraint')+1]).read_text()
    assert 'torch==2.8.0' in constraints and 'torchaudio==2.8.0' in constraints
    assert calls[-1][-1]=='--allow-downloads'
    assert result['use_gpu'] is False


def test_postflight_rejects_smaller_or_wrong_language_whisper_models():
    large=SimpleNamespace(model=SimpleNamespace(n_mels=128,is_multilingual=True))
    english=SimpleNamespace(model=SimpleNamespace(n_mels=80,is_multilingual=False))
    worker.verify_whisper_shape(large,'transcription_model_path',2*1024**3)
    worker.verify_whisper_shape(english,'review_model_path',1024**3)
    with pytest.raises(ValueError,match='smaller'):worker.verify_whisper_shape(large,'transcription_model_path',100*1024**2)
    with pytest.raises(ValueError,match='quality bundle'):worker.verify_whisper_shape(english,'transcription_model_path',2*1024**3)
    with pytest.raises(ValueError,match='quality bundle'):worker.verify_whisper_shape(large,'review_model_path',2*1024**3)
