"""Discovery reads model metadata, without model loads, downloads or broad scans."""
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import json
import os
import sys

import pytest

from gnb_player import model_discovery as discovery


def ct2(folder: Path):
    folder.mkdir(parents=True)
    (folder / "model.bin").write_bytes(b"\x06\x00\x00\x00\x0c\x00WhisperSpec\x00" + b"\x00" * 64)
    (folder / "config.json").write_text('{"alignment_heads": []}')
    (folder / "tokenizer.json").write_text('{"model": {}}')
    return folder


def separator(folder: Path):
    folder.mkdir(parents=True)
    (folder / "spec.json").write_text('{"args": [], "kwargs": {"sources": ["vocals", "other"]}}')
    header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    (folder / "model.safetensors").write_bytes(len(header).to_bytes(8, "little") + header + b"\0" * 4)
    return folder


def scan(root, **options):
    return discovery.scan_models([root], include_defaults=False, **options)


def test_recognizes_supported_components_and_serializes_without_loading_weights(tmp_path):
    ct2(tmp_path / "large-v3")
    ct2(tmp_path / "medium.en")
    separator(tmp_path / "htdemucs")
    (tmp_path / discovery.CTC_CHECKPOINT).write_bytes(b"PK\x03\x04fixture")
    # Personal documents are deliberately unrelated to model discovery.
    (tmp_path / "unrelated-document.pdf").write_bytes(b"Never inspect document contents")
    result = scan(tmp_path)
    assert {item.kind for item in result.models if item.compatible} == {"transcription", "review", "separator", "alignment"}
    assert result.entries_checked < 20 and not result.truncated
    assert all("unrelated-document" not in item.path for item in result.models)
    assert json.loads(json.dumps(result.to_dict()))["models"]


def test_configuration_and_header_checks_prevent_name_only_reuse(tmp_path):
    large = ct2(tmp_path / "large-v3")
    (large / "model.bin").write_bytes(b"Not an actual CTranslate2 model")
    medium = ct2(tmp_path / "medium.en")
    (medium / "tokenizer.json").unlink()
    result = scan(tmp_path)
    assert not any(item.compatible for item in result.models)
    assert any("not a recognized" in item.reason for item in result.models)
    assert any("tokenizer" in item.reason for item in result.models)


def test_turbo_and_unknown_named_models_are_not_claimed_as_large_v3(tmp_path):
    ct2(tmp_path / "large-v3-turbo")
    ct2(tmp_path / "renamed-model")
    result = scan(tmp_path)
    assert len(result.models) == 2
    assert all(not item.compatible and not item.engine for item in result.models)


def test_hf_cache_snapshot_keeps_safe_in_cache_file_symlinks(tmp_path):
    repo = tmp_path / "models--Systran--faster-whisper-large-v3"
    snapshot = repo / "snapshots/revision"
    snapshot.mkdir(parents=True)
    blobs = repo / "blobs"
    blobs.mkdir()
    for name, payload in {"model.bin": b"\x06\x00\x00\x00WhisperSpec\0" + b"\0" * 64,
                          "config.json": b"{}", "tokenizer.json": b"{}"}.items():
        source = blobs / name
        source.write_bytes(payload)
        try:
            (snapshot / name).symlink_to(source)
        except OSError:
            pytest.skip("This account cannot create file symlinks")
    result = scan(tmp_path)
    assert any(item.compatible and item.path == str(snapshot) for item in result.models)


def test_symlinks_cannot_escape_selected_root_and_directories_are_not_followed(tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    outside = ct2(tmp_path / "outside/large-v3")
    partial = ct2(selected / "large-v3")
    (partial / "model.bin").unlink()
    try:
        (partial / "model.bin").symlink_to(outside / "model.bin")
        (selected / "other-models").symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("This account cannot create model symlinks")
    result = scan(selected)
    assert len(result.models) == 1 and not result.models[0].compatible
    assert "outside" in result.models[0].reason


def test_checkpoint_and_other_model_formats_get_explicit_explanations(tmp_path):
    for name in ["large-v3.pt", "voice.gguf", "ggml-large-v3.bin", "955717e8-8726e21a.th"]:
        (tmp_path / name).write_bytes(b"fixture")
    result = scan(tmp_path)
    assert len(result.models) == 4
    assert not any(item.compatible for item in result.models)
    assert {item.format for item in result.models} == {"torch-whisper", "gguf", "ggml", "demucs-checkpoint"}


def test_scan_honors_depth_entry_and_cancellation_limits(tmp_path):
    ct2(tmp_path / "a/b/large-v3")
    assert not scan(tmp_path, max_depth=1).models
    for i in range(20):
        (tmp_path / f"irrelevant-{i}.txt").write_text("ignored")
    bounded = scan(tmp_path, max_files=3)
    assert bounded.truncated and bounded.entries_checked == 3
    cancelled = scan(tmp_path, cancelled=lambda: True)
    assert cancelled.cancelled and cancelled.entries_checked == 0


def test_scan_time_limit_is_not_silently_ignored(tmp_path):
    ct2(tmp_path / "large-v3")
    with patch.object(discovery.time, "monotonic", side_effect=[0, 2]):
        result = scan(tmp_path, max_seconds=1)
    assert result.truncated and not result.models


def test_network_onedrive_and_broad_personal_roots_are_skipped(tmp_path):
    drive = tmp_path / "OneDrive/models"
    drive.mkdir(parents=True)
    result = discovery.scan_models(["//remote-server/share/models", drive, Path.home()], include_defaults=False)
    assert not result.scanned_roots
    assert len(result.warnings) == 3


def test_default_roots_respect_cache_and_component_settings(tmp_path):
    settings = {"transcription_model_path": str(tmp_path / "selected/large-v3"),
                "review_model_path": str(tmp_path / "selected/medium.en"),
                "separator_model_path": str(tmp_path / "selected/htdemucs"),
                "alignment_model_path": str(tmp_path / discovery.CTC_CHECKPOINT)}
    env = {"HF_HUB_CACHE": str(tmp_path / "hf-cache"), "TORCH_HOME": str(tmp_path / "torch-cache")}
    roots = discovery.default_search_roots(tmp_path / "app", settings, env)
    assert Path(env["HF_HUB_CACHE"]) in roots
    assert Path(env["TORCH_HOME"]) in roots
    assert all(Path(value) in roots for value in settings.values())
    assert Path.home() not in roots


def test_duplicate_and_nested_search_roots_are_scanned_once(tmp_path):
    model = ct2(tmp_path / "large-v3")
    result = discovery.scan_models([tmp_path, model, tmp_path], include_defaults=False)
    assert result.scanned_roots == [str(tmp_path)]
    assert len(result.models) == 1


def test_ollama_storage_is_reported_separately_from_whisper(tmp_path):
    storage = tmp_path / ".ollama/models"
    storage.mkdir(parents=True)
    (storage / "unrelated-blob").write_bytes(b"not inspected")
    result = scan(tmp_path)
    assert len(result.models) == 1 and result.models[0].kind == "ollama-store"
    assert not result.models[0].compatible


def runtime_report(packages=None, version=None):
    values = {"torch": "2.8.0+cpu", "torchaudio": "2.8.0+cpu", "demucs": "4.0.1",
              "numpy": "2.0.0", "soundfile": "0.13.1", "faster-whisper": "1.2.0",
              "safetensors": "0.6.0", "num2words": "0.5.14"}
    if packages is not None:
        values = packages
    return json.dumps({"version": version or [3, 12, 10], "packages": values}).encode()


def test_runtime_probe_uses_isolated_python_metadata_and_no_shell():
    process = SimpleNamespace(returncode=0, communicate=lambda **kw: (runtime_report(), b""))
    with patch.object(discovery, "_start_probe", return_value=process) as start:
        result = discovery.probe_python("C:/runtime with spaces/python.exe")
    assert result.compatible and result.version == "3.12.10"
    command = start.call_args.args[0]
    assert command[:3] == ["C:/runtime with spaces/python.exe", "-I", "-c"]
    assert "import torch" not in command[-1]
    assert "importlib.metadata" in command[-1]


def test_runtime_probe_rejects_missing_or_incompatible_alignment_runtime():
    packages = {"torch": "2.9.0", "torchaudio": "2.9.0"}
    process = SimpleNamespace(returncode=0, communicate=lambda **kw: (runtime_report(packages), b""))
    with patch.object(discovery, "_start_probe", return_value=process):
        result = discovery.probe_python("python")
    assert not result.compatible and "2.8 series" in result.reason and "missing" in result.reason


def test_actual_python_metadata_probe_never_imports_models():
    # The probe may find no AI dependencies; ordinary playback Python is valid
    # to inspect, but must not be mistaken for a prepared model runtime.
    result = discovery.probe_python(sys.executable)
    assert result.version.startswith("3.")
    assert isinstance(result.packages, dict)


def test_versioned_installer_runtime_is_discovered_only_after_completion(tmp_path):
    version = tmp_path / "model-setup/versions/quality-fixture"
    runtime = version / "runtime/Scripts/python.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"fixture executable; never run by this test")
    with patch.object(discovery.shutil, "which", return_value=None):
        assert str(runtime) not in discovery.discover_python_paths(app_data=tmp_path)
        (version / "installation.complete.json").write_text(json.dumps({"settings": {"python_path": str(runtime)}}))
        assert str(runtime) in discovery.discover_python_paths(app_data=tmp_path)
        # A forged manifest must not choose an unrelated executable elsewhere.
        (version / "installation.complete.json").write_text(json.dumps({"settings": {"python_path": sys.executable}}))
        paths = discovery.discover_python_paths(app_data=tmp_path)
        assert str(runtime) not in paths


def test_ollama_url_restricts_probing_to_loopback():
    assert discovery.normalize_ollama_url("http://localhost:11434/") == "http://127.0.0.1:11434"
    assert discovery.normalize_ollama_url("http://[::1]:11434") == "http://[::1]:11434"
    for url in ["http://192.0.2.1:11434", "http://example.com:11434", "file:///models", "http://user:pass@localhost:11434", "http://localhost:11434/api/tags?token=x"]:
        with pytest.raises(ValueError):
            discovery.normalize_ollama_url(url)


def test_local_ollama_probe_lists_models_without_start_or_pull():
    body = json.dumps({"models": [{"name": "qwen3:8b", "size": 123,
                                  "details": {"family": "qwen3", "parameter_size": "8B"}}]}).encode()
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return body
    opener = SimpleNamespace(open=lambda *args, **kwargs: Response())
    with patch.object(discovery.urllib.request, "build_opener", return_value=opener):
        result = discovery.discover_ollama()
    assert result.reachable and result.models[0]["name"] == "qwen3:8b"
    assert "Whisper" in result.reason
    assert discovery._NoRedirect().redirect_request(None, None, 302, "", {}, "http://example.com") is None


def test_remote_ollama_rejection_never_makes_a_request():
    with patch.object(discovery.urllib.request, "build_opener") as opener:
        result = discovery.discover_ollama("http://192.0.2.1:11434")
    assert not result.reachable and "loopback" in result.reason
    opener.assert_not_called()


def test_cancelled_combined_discovery_never_probes_runtimes_or_services(tmp_path):
    with patch.object(discovery, "probe_python") as python, patch.object(discovery, "discover_ollama") as ollama:
        result = discovery.discover([tmp_path], include_defaults=False, cancelled=lambda: True)
    assert result.cancelled
    python.assert_not_called()
    ollama.assert_not_called()


def test_cancellation_during_last_runtime_probe_is_reported_even_without_ollama(tmp_path):
    stopped = [False]
    def probe(path):
        stopped[0] = True
        return discovery.PythonRuntime(path)
    with patch.object(discovery, "scan_models", return_value=discovery.DiscoveryResult()), \
         patch.object(discovery, "discover_python_paths", return_value=["selected-python"]), \
         patch.object(discovery, "probe_python", side_effect=probe):
        result = discovery.discover(app_data=tmp_path, probe_ollama=False, cancelled=lambda: stopped[0])
    assert result.cancelled
