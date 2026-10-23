"""Selected existing caches are routed end to end without copying/downloading."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import importlib.util
import os
import sys

import pytest

from gnb_player.extraction import ExtractionConfig, effective_model_paths, config_status
from gnb_player import extract_worker as worker


def load_backend(name):
    path = Path(worker.__file__).with_name("extraction_backend") / (name + ".py")
    spec = importlib.util.spec_from_file_location("path_test_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def selected_config(tmp_path, **changes):
    selected = {
        "python_path": sys.executable, "model_root": str(tmp_path / "old models"),
        "torch_home": str(tmp_path / "old torch"), "cache_dir": str(tmp_path / "cache"),
        "transcription_model_path": str(tmp_path / "first cache/large-v3"),
        "review_model_path": str(tmp_path / "second cache/medium.en"),
        "separator_model_path": str(tmp_path / "third cache/htdemucs"),
        "alignment_model_path": str(tmp_path / "fourth cache/selected-alignment.pth"),
    }
    selected.update(changes)
    return ExtractionConfig.from_dict(selected)


def create_required_files(config):
    paths = effective_model_paths(config)
    for path in [paths["transcription"] / "model.bin", paths["review"] / "model.bin",
                 paths["separator"] / "spec.json", paths["separator"] / "model.safetensors",
                 paths["alignment"]]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")


def test_old_config_keeps_its_original_model_and_torch_layout(tmp_path):
    config = ExtractionConfig.from_dict({"model_root": str(tmp_path / "models"), "torch_home": str(tmp_path / "torch")})
    paths = effective_model_paths(config)
    assert paths == {
        "transcription": tmp_path / "models/large-v3",
        "review": tmp_path / "models/medium.en",
        "separator": tmp_path / "models/htdemucs",
        "alignment": tmp_path / "torch/hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth",
    }


def test_status_and_worker_preflight_reuse_scattered_component_locations(tmp_path):
    config = selected_config(tmp_path)
    create_required_files(config)
    with patch("gnb_player.extraction.shutil.which", return_value="available"):
        assert config_status(config)["ready"]
    with patch.object(worker.importlib.util, "find_spec", return_value=object()):
        worker.require_runtime(config, "")
    assert not Path(config.model_root).exists() and not Path(config.torch_home).exists()
    effective_model_paths(config)["review"].joinpath("model.bin").unlink()
    with patch.object(worker.importlib.util, "find_spec", return_value=object()):
        with pytest.raises(ValueError, match="medium.en"):
            worker.require_runtime(config, "")


def test_review_still_requires_separator_when_primary_separation_is_disabled(tmp_path):
    config = selected_config(tmp_path, separate_vocals=False, auto_review=True)
    create_required_files(config)
    path = effective_model_paths(config)["separator"] / "spec.json"
    path.unlink()
    status = config_status(config)
    assert not status["ready"] and str(path) in status["missing_models"]


def test_exact_lyric_alignment_does_not_require_a_primary_asr_model(tmp_path):
    config = selected_config(tmp_path, separate_vocals=False, auto_review=False)
    path = effective_model_paths(config)["alignment"]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture")
    with patch.object(worker.importlib.util, "find_spec", return_value=object()):
        worker.require_runtime(config, "The exact words as sung")


def test_worker_passes_resolved_component_paths_and_selected_engine(tmp_path):
    config = selected_config(tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        worker.prepare_environment(config)
        paths = effective_model_paths(config)
        assert os.environ["GNB_LYRICS_TRANSCRIPTION_MODEL"] == str(paths["transcription"])
        assert os.environ["GNB_LYRICS_REVIEW_MODEL"] == str(paths["review"])
        assert os.environ["GNB_LYRICS_SEPARATOR_MODEL"] == str(paths["separator"])
        assert os.environ["GNB_LYRICS_ALIGNMENT_MODEL"] == str(paths["alignment"])
        assert os.environ["GNB_LYRICS_TRANSCRIPTION_ENGINE"] == "large-v3"
        assert os.environ["HF_HUB_OFFLINE"] == "1"


def test_path_router_keeps_primary_secondary_and_large_rechecks_distinct(tmp_path):
    config = selected_config(tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        worker.prepare_environment(config)
        backend = load_backend("lyrics_pipeline")
        assert backend.model_path("large-v3") == Path(config.transcription_model_path)
        assert backend.model_path("large-v3", review=True) == Path(config.transcription_model_path)
        assert backend.model_path("medium.en", review=True) == Path(config.review_model_path)
        assert backend.separator_path() == Path(config.separator_model_path)
        os.environ["GNB_LYRICS_TRANSCRIPTION_ENGINE"] = "medium.en"
        assert backend.model_path("medium.en") == Path(config.transcription_model_path)
        assert backend.model_path("medium.en", review=True) == Path(config.review_model_path)
        assert backend.model_path("large-v3") == Path(config.model_root) / "large-v3"


def test_alignment_loader_uses_exact_preexisting_file_with_no_default_cache_lookup(tmp_path):
    backend = load_backend("lyrics_pipeline")
    checkpoint = tmp_path / "separate cache/custom-name.pth"
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"fixture")
    model = Mock()
    model.to.return_value = model
    bundle = SimpleNamespace(get_model=Mock(return_value=model))
    assert backend.load_alignment_model(bundle, checkpoint, "cpu") is model
    bundle.get_model.assert_called_once_with(dl_kwargs={"model_dir": str(checkpoint.parent), "file_name": checkpoint.name})
    model.to.assert_called_once_with("cpu")
    bundle.get_model.reset_mock()
    checkpoint.unlink()
    with pytest.raises(ValueError, match="not been installed"):
        backend.load_alignment_model(bundle, checkpoint, "cpu")
    bundle.get_model.assert_not_called()


def test_transcription_selects_existing_large_model_and_preserves_cpu_int8(tmp_path):
    chosen = tmp_path / "existing cache/large-v3"
    with patch.dict(os.environ, {"GNB_LYRICS_TRANSCRIPTION_ENGINE": "large-v3", "GNB_LYRICS_TRANSCRIPTION_MODEL": str(chosen)}):
        backend = load_backend("lyrics_pipeline")
        word = SimpleNamespace(word="Blues", start=0.1, end=0.8, probability=.99)
        segment = SimpleNamespace(words=[word], avg_logprob=0, compression_ratio=1, end=1)
        model = SimpleNamespace(transcribe=lambda *a, **kw: ([segment], SimpleNamespace(duration=2)))
        factory = Mock(return_value=model)
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=factory)}), patch.object(backend, "release_memory"):
            cues, warnings = backend.transcribe_whisper([], "large-v3", lambda *a: None)
    assert cues and not warnings
    assert factory.call_args.args == (str(chosen),)
    assert factory.call_args.kwargs["compute_type"] == "int8"
    assert factory.call_args.kwargs["local_files_only"] is True


def test_independent_review_uses_its_own_existing_model(tmp_path):
    primary = tmp_path / "primary/large-v3"
    secondary = tmp_path / "review/medium.en"
    with patch.dict(os.environ, {"GNB_LYRICS_TRANSCRIPTION_ENGINE": "large-v3", "GNB_LYRICS_TRANSCRIPTION_MODEL": str(primary), "GNB_LYRICS_REVIEW_MODEL": str(secondary)}):
        backend = load_backend("lyrics_pipeline")
        review = load_backend("lyrics_review")
        word = SimpleNamespace(word="Blues", start=0.1, end=0.8, probability=.99)
        model = SimpleNamespace(transcribe=lambda *a, **kw: ([SimpleNamespace(words=[word])], None))
        factory = Mock(return_value=model)
        cache = SimpleNamespace(evidence_path=lambda *a: tmp_path / "unused", read_evidence=lambda *a: None, write_evidence=lambda *a: None)
        modules = {"faster_whisper": SimpleNamespace(WhisperModel=factory), "lyrics_pipeline": backend,
                   "lyrics_acceleration": SimpleNamespace(enabled=lambda: False, recognize_windows=None, GpuCapacityDeferred=ValueError), "lyrics_cache": cache}
        with patch.dict(sys.modules, modules), patch.object(backend, "release_memory"):
            output = review.recognize([0] * 16000, 16000, [("full", 0, 1)], "medium.en", lambda *a: None)
    assert output["full"] and factory.call_args.args == (str(secondary),)
    assert factory.call_args.kwargs["local_files_only"] is True


def test_ollama_settings_are_optional_and_local_only(tmp_path):
    config = selected_config(tmp_path, ollama_url="http://localhost:11434/", ollama_model="qwen3:8b")
    assert config.ollama_url == "http://127.0.0.1:11434"
    assert config.ollama_model == "qwen3:8b"
    create_required_files(config)
    with patch("gnb_player.extraction.shutil.which", return_value="available"):
        assert config_status(config)["ready"]  # No Ollama connection is necessary for ASR.
    for address in ["http://example.invalid:11434", "http://192.0.2.1:11434", "http://user:pass@localhost:11434", "http://localhost:11434/api"]:
        with pytest.raises(ValueError):
            selected_config(tmp_path, ollama_url=address)
    with pytest.raises(ValueError):
        selected_config(tmp_path, ollama_model="bad\nmodel")
