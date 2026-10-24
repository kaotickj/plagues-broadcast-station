"""Bounded shared-file discovery without real models, processes or downloads."""
from pathlib import Path
import json
import os
import pytest

from gnb_player import extraction as models


@pytest.fixture
def locations(tmp_path,monkeypatch):
    base=tmp_path/'user-data';shared=tmp_path/'shared'
    paths={'model_root':shared/'models','torch_home':shared/'torch',
           'python_path':shared/'runtime/bin/python','gpu_python_path':shared/'gpu/bin/python',
           'vulkan_binary':shared/'tools/whisper','vulkan_model':shared/'models/whisper-vulkan/model.bin',
           'gpu_lock':shared/'gpu.lock'}
    monkeypatch.setattr(models,'default_data_dir',lambda:base)
    monkeypatch.setattr(models,'LINUX_SHARED_PATHS',paths)
    monkeypatch.setattr(models.sys,'platform','linux')
    for key in list(os.environ):
        if key.startswith('GNB_PLAYER_'):monkeypatch.delenv(key)
    return base,paths


def touch(path,*,executable=False):
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture')
    path.chmod(0o755 if executable else 0o644)
    return path


def install_components(root,torch):
    for rel in ('large-v3/model.bin','medium.en/model.bin','htdemucs/spec.json','htdemucs/model.safetensors'):
        touch(root/rel)
    touch(torch/models._ALIGNMENT_CHECKPOINT)


def install_shared(paths):
    install_components(paths['model_root'],paths['torch_home'])
    for name in ('python_path','gpu_python_path','vulkan_binary'):touch(paths[name],executable=True)
    for name in ('vulkan_model','gpu_lock'):touch(paths[name])


def test_shared_install_is_resolved_without_mutating_gpu_switch_cache_or_blank_overrides(locations,monkeypatch):
    base,shared=locations;install_shared(shared)
    original=models.discover_config();original.cache_dir=str(base/'station-one-private-cache');before=original.to_dict()
    resolved,report=models.resolve_existing_models(original)
    assert original.to_dict()==before
    assert resolved.model_root==str(shared['model_root']) and resolved.torch_home==str(shared['torch_home'])
    assert resolved.python_path==str(shared['python_path']) and resolved.vulkan_binary==str(shared['vulkan_binary'])
    assert resolved.use_gpu is False and resolved.cache_dir==before['cache_dir']
    assert all(getattr(resolved,name)=='' for name in ('transcription_model_path','review_model_path','separator_model_path','alignment_model_path'))
    monkeypatch.setattr(models.shutil,'which',lambda name:'/tools/'+name if name in {'ffmpeg','ffprobe'} else None)
    status=models.config_status(original)
    assert status['ready'] and status['path_resolution']['fields']['python_path']['source']=='shared-linux'
    assert 'not been loaded or validated' in status['path_resolution']['validation']
    command=models.build_command(base/'song.mp3',base/'lyrics.json',original)
    encoded=json.loads(command[command.index('--config-json')+1])
    assert command[0]==str(shared['python_path'])
    assert encoded['cache_dir']==before['cache_dir'] and encoded['use_gpu'] is False
    assert {name:str(path) for name,path in models.effective_model_paths(models.ExtractionConfig.from_dict(encoded)).items()}==report['effective_model_paths']


def test_complete_user_local_install_is_kept_ahead_of_shared(locations):
    base,shared=locations;install_shared(shared)
    install_components(base/'models',base/'torch');runtime=touch(base/'model-runtime/bin/python',executable=True)
    resolved,report=models.resolve_existing_models(models.discover_config())
    assert resolved.model_root==str(base/'models') and resolved.torch_home==str(base/'torch')
    assert resolved.python_path==str(runtime)
    assert report['fields']['model_root']['source']=='user-local'


def test_valid_explicit_components_keep_independent_locations(locations,monkeypatch):
    base,shared=locations;install_shared(shared)
    custom=base.parent/'custom'
    config=models.ExtractionConfig(python_path=str(touch(custom/'python',executable=True)),
        transcription_model_path=str(custom/'primary'),review_model_path=str(custom/'review'),
        separator_model_path=str(custom/'separator'),alignment_model_path=str(custom/'alignment.pth'),
        use_gpu=False,cache_dir='station-two-cache')
    for field in ('transcription_model_path','review_model_path'):touch(Path(getattr(config,field))/'model.bin')
    for rel in ('spec.json','model.safetensors'):touch(Path(config.separator_model_path)/rel)
    touch(Path(config.alignment_model_path))
    resolved,report=models.resolve_existing_models(config)
    for field in ('python_path','transcription_model_path','review_model_path','separator_model_path','alignment_model_path'):
        assert getattr(resolved,field)==getattr(config,field)
        assert report['fields'][field]['source']=='configured'
    assert resolved.cache_dir=='station-two-cache' and resolved.use_gpu is False
    monkeypatch.setattr(models.shutil,'which',lambda name:'/tools/'+name if name in {'ffmpeg','ffprobe'} else None)
    assert models.config_status(config)['ready']


def test_missing_custom_roots_and_overrides_are_not_replaced_by_shared(locations):
    base,shared=locations;install_shared(shared)
    config=models.ExtractionConfig(python_path=str(base/'intentional-missing-python'),
        model_root=str(base/'intentional-model-root'),torch_home=str(base/'intentional-torch'),
        review_model_path=str(base/'intentional-review'),gpu_python_path=str(base/'intentional-gpu'),
        vulkan_binary=str(base/'intentional-vulkan'),vulkan_model=str(base/'intentional-ggml'),gpu_lock=str(base/'intentional-lock'))
    resolved,report=models.resolve_existing_models(config)
    for field in ('python_path','model_root','torch_home','review_model_path','gpu_python_path','vulkan_binary','vulkan_model','gpu_lock'):
        assert getattr(resolved,field)==getattr(config,field)
        assert report['fields'][field]['missing_files']
    assert report['effective_model_paths']['transcription']==str(Path(config.model_root)/config.engine)
    assert report['effective_model_paths']['alignment']==str(Path(config.torch_home)/models._ALIGNMENT_CHECKPOINT)


def test_marked_missing_canonical_defaults_and_nonempty_environment_values_remain_explicit(locations,monkeypatch):
    base,shared=locations;install_shared(shared)
    monkeypatch.setenv('GNB_PLAYER_MODELS',str(base/'models'))
    monkeypatch.setenv('GNB_PLAYER_PYTHON',str(base/'model-runtime/bin/python'))
    original=models.discover_config()
    assert set(original.explicit_paths)=={'model_root','python_path'}
    resolved,report=models.resolve_existing_models(original)
    assert resolved.model_root==str(base/'models') and resolved.python_path==original.python_path
    assert report['effective_model_paths']['transcription']==str(base/'models/large-v3')
    assert report['fields']['model_root']['source']=='configured'
    config=models.ExtractionConfig(model_root=str(shared['model_root']),explicit_paths=['model_root'])
    assert models.resolve_existing_models(config)[0].model_root==config.model_root


def test_partial_default_install_resolves_components_without_materializing_blank_overrides(locations):
    base,shared=locations
    touch(base/'models/large-v3/model.bin')
    for rel in ('medium.en/model.bin','htdemucs/spec.json','htdemucs/model.safetensors'):touch(shared['model_root']/rel)
    touch(shared['torch_home']/models._ALIGNMENT_CHECKPOINT)
    resolved,report=models.resolve_existing_models(models.discover_config())
    assert report['effective_model_paths']=={'transcription':str(base/'models/large-v3'),
        'review':str(shared['model_root']/'medium.en'),'separator':str(shared['model_root']/'htdemucs'),
        'alignment':str(shared['torch_home']/models._ALIGNMENT_CHECKPOINT)}
    assert all(getattr(resolved,field)=='' for field in ('transcription_model_path','review_model_path','separator_model_path','alignment_model_path'))


def test_valid_local_component_is_retained_when_shared_root_is_complete(locations):
    base,shared=locations;install_shared(shared)
    touch(base/'models/large-v3/model.bin')
    resolved,report=models.resolve_existing_models(models.discover_config())
    assert resolved.model_root==str(shared['model_root'])
    assert report['effective_model_paths']['transcription']==str(base/'models/large-v3')
    assert report['effective_model_paths']['review']==str(shared['model_root']/'medium.en')
    assert resolved.transcription_model_path==''


def test_no_install_retains_user_local_fallback_and_reports_missing_files(locations):
    base,_=locations
    resolved,report=models.resolve_existing_models(models.discover_config())
    assert resolved.model_root==str(base/'models') and resolved.torch_home==str(base/'torch')
    assert resolved.python_path==''
    assert report['fields']['transcription_model_path']['missing_files']==[str(base/'models/large-v3/model.bin')]
    assert not models.config_status(resolved)['ready']


@pytest.mark.parametrize('platform',['darwin','win32'])
def test_shared_linux_paths_are_never_probed_on_other_platforms(locations,monkeypatch,platform):
    base,shared=locations;install_shared(shared)
    monkeypatch.setattr(models.sys,'platform',platform)
    observed=[];original=models._readable
    def readable(path):observed.append(str(path));return original(path)
    monkeypatch.setattr(models,'_readable',readable)
    resolved,_=models.resolve_existing_models(models.discover_config())
    assert resolved.model_root==str(base/'models') and resolved.python_path==''
    assert all(str(base.parent/'shared') not in path for path in observed)


def test_discovered_runtime_must_be_executable_and_incomplete_root_is_not_selected(locations):
    base,shared=locations
    touch(shared['python_path']);shared['model_root'].mkdir(parents=True)
    resolved,report=models.resolve_existing_models(models.discover_config())
    assert resolved.python_path=='' and resolved.model_root==str(base/'models')
    assert report['fields']['model_root']['missing_files']


def test_custom_path_runtime_command_remains_literal_with_correct_diagnostics(locations,monkeypatch):
    base,shared=locations;install_shared(shared)
    runtime=touch(base/'chosen-runtime/python3',executable=True)
    monkeypatch.setattr(models.shutil,'which',lambda value:str(runtime) if value=='python3' else ('/tools/'+value if value in {'ffmpeg','ffprobe'} else None))
    config=models.ExtractionConfig(python_path='python3',use_gpu=False)
    resolved,report=models.resolve_existing_models(config)
    assert resolved.python_path=='python3'
    assert report['fields']['python_path']['missing_files']==[]
    assert report['fields']['python_path']['resolved_executable']==str(runtime)
    assert models.config_status(config)['ready']
    assert models.build_command(base/'recording.mp3',base/'lyrics.json',config)[0]=='python3'


def test_gpu_status_requires_an_executable_tool_and_readable_model(locations):
    _,shared=locations;install_shared(shared)
    config=models.ExtractionConfig(vulkan_binary=str(shared['vulkan_binary']),vulkan_model=str(shared['vulkan_model']),use_gpu=True,explicit_paths=['vulkan_binary','vulkan_model'])
    assert models.config_status(config)['gpu_configured']
    shared['vulkan_binary'].chmod(0o644)
    assert not models.config_status(config)['gpu_configured']
    assert models.resolve_existing_models(config)[1]['fields']['vulkan_binary']['missing_files']==[str(shared['vulkan_binary'])]


def test_inaccessible_custom_paths_remain_visible_without_crashing(locations,monkeypatch):
    base,shared=locations;install_shared(shared)
    custom=base/'permission-denied';real_is_file=Path.is_file
    def inaccessible(path):
        if path==custom or custom in path.parents:raise PermissionError('Fixture inaccessible')
        return real_is_file(path)
    monkeypatch.setattr(Path,'is_file',inaccessible)
    config=models.ExtractionConfig(python_path=str(custom/'python'),model_root=str(custom/'models'))
    resolved,report=models.resolve_existing_models(config)
    assert resolved.python_path==config.python_path and resolved.model_root==config.model_root
    assert report['fields']['python_path']['missing_files']==[config.python_path]
    assert report['fields']['model_root']['missing_files']
    assert not models.config_status(config)['ready']


def test_existing_shared_lock_is_not_omitted_when_access_is_unavailable(locations,monkeypatch):
    _,shared=locations;install_shared(shared);real_access=os.access
    def denied(path,mode):return False if Path(path)==shared['gpu_lock'] else real_access(path,mode)
    monkeypatch.setattr(models.os,'access',denied)
    resolved,report=models.resolve_existing_models(models.discover_config())
    assert resolved.gpu_lock==str(shared['gpu_lock'])
    assert report['fields']['gpu_lock']['missing_files']==[str(shared['gpu_lock'])]


@pytest.mark.parametrize('markers',['python_path',['unknown'],[True],['python_path']*12])
def test_explicit_path_markers_are_validated(markers):
    with pytest.raises(ValueError,match='Explicit model paths'):
        models.ExtractionConfig.from_dict({'explicit_paths':markers})
