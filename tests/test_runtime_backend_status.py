"""Cheap configured-route reporting never loads models or claims execution."""
from pathlib import Path
from gnb_player.extraction import ExtractionConfig, config_status, discover_config


def configured(tmp_path, **switches):
    paths = {}
    for field in ('vulkan_binary', 'vulkan_model', 'gpu_python_path'):
        p = tmp_path / field
        p.write_text('fixture; never executed')
        p.chmod(0o700)
        paths[field] = str(p)
    return ExtractionConfig(**paths, **switches)


def test_existing_gpu_routes_report_phase_limits(tmp_path):
    status = config_status(configured(tmp_path, use_gpu=True))
    assert status['gpu_configured']
    plan = status['processing_plan']
    assert plan['recognition'] == 'Vulkan GPU when available'
    assert plan['alignment'] == 'ROCm GPU when available; CPU fallback'
    assert plan['separation'] == 'CPU'
    assert plan['review'] == 'CPU, independent medium.en model'
    assert 'Configured routes only' in status['processing_note']


def test_saved_gpu_off_and_original_mix_remain_explicit(tmp_path):
    cfg = configured(tmp_path, use_gpu=False, separate_vocals=False, auto_review=False)
    status = config_status(ExtractionConfig.from_dict(cfg.to_dict()))
    assert not status['gpu_configured']
    assert 'off for this station' in status['processing_plan']['recognition']
    assert status['processing_plan']['alignment'] == 'CPU'
    assert status['processing_plan']['separation'] == 'Disabled (original mix)'
    assert status['processing_plan']['review'] == 'Disabled'


def test_missing_gpu_paths_diagnose_cpu_fallback(tmp_path):
    cfg = configured(tmp_path, use_gpu=True)
    Path(cfg.vulkan_binary).unlink()
    status = config_status(cfg)
    assert not status['gpu_configured']
    assert 'unavailable' in status['processing_plan']['recognition']
    assert status['processing_plan']['alignment'] == 'CPU'


def test_non_large_model_never_claims_gpu_primary(tmp_path):
    status = config_status(configured(tmp_path, use_gpu=True, engine='medium.en'))
    assert status['processing_plan']['recognition'].startswith('CPU (medium.en;')


def test_fresh_defaults_try_existing_gpu_but_saved_false_survives(monkeypatch):
    for key in list(__import__('os').environ):
        if key.startswith('GNB_PLAYER_'): monkeypatch.delenv(key)
    assert discover_config().use_gpu is True
    assert ExtractionConfig.from_dict({'use_gpu': False}).use_gpu is False
