from pathlib import Path
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
import contextlib
import signal
from types import SimpleNamespace
from unittest.mock import patch
from gnb_player.extraction import (ExtractionConfig, discover_config, config_status,
                                    build_command, parse_worker_event, media_environment)
from gnb_player import extract_worker as worker


class ExtractionTests(unittest.TestCase):
    def test_discovery_uses_portable_folders_and_explicit_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "app-data"
            with patch.dict(os.environ, {}, clear=True), patch("gnb_player.extraction.default_data_dir", return_value=base):
                config = discover_config()
                self.assertEqual(config.python_path, "")
                self.assertEqual(config.model_root, str(base / "models"))
                self.assertEqual(config.torch_home, str(base / "torch"))
                self.assertEqual(config.cache_dir, str(base / "extraction-cache"))
                self.assertEqual(config.gpu_lock, "")
                self.assertEqual(config.gpu_python_path, "")
                self.assertEqual(config.vulkan_binary, "")
                self.assertFalse(config.use_gpu)
            values = {"GNB_PLAYER_PYTHON": str(base / "runtime/python"),
                      "GNB_PLAYER_MODELS": str(base / "configured-models"),
                      "GNB_PLAYER_TORCH_HOME": str(base / "configured-torch"),
                      "GNB_PLAYER_GPU_PYTHON": str(base / "gpu-runtime/python"),
                      "GNB_PLAYER_VULKAN_BINARY": str(base / "tools/whisper"),
                      "GNB_PLAYER_GPU_LOCK": str(base / "coordination.lock")}
            with patch.dict(os.environ, values, clear=True):
                configured = discover_config()
            self.assertEqual(configured.python_path, values["GNB_PLAYER_PYTHON"])
            self.assertEqual(configured.model_root, values["GNB_PLAYER_MODELS"])
            self.assertEqual(configured.gpu_lock, values["GNB_PLAYER_GPU_LOCK"])
            self.assertTrue(configured.use_gpu)

    def test_optional_accelerator_paths_have_no_machine_specific_fallback(self):
        backend_path = Path(worker.__file__).with_name("extraction_backend") / "lyrics_acceleration.py"
        spec = importlib.util.spec_from_file_location("test_portable_acceleration", backend_path)
        backend = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {}, clear=True):
            spec.loader.exec_module(backend)
        self.assertIsNone(backend.GPU_PYTHON)
        self.assertIsNone(backend.VULKAN)
        self.assertIsNone(backend.GGML)

    def test_command_keeps_paths_and_lyrics_as_literal_arguments(self):
        config = ExtractionConfig(python_path=sys.executable, model_root="/models", cache_dir="/cache")
        command = build_command(Path("song; never-run.mp3"), Path("lyrics output.json"), config, "It's a $5 blues; no shell")
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[-2:], ["--reference-text", "It's a $5 blues; no shell"])
        self.assertEqual(Path(command[2]).name, "extract_worker.py")
        self.assertEqual(json.loads(command[command.index("--config-json") + 1])["model_root"], "/models")


    @unittest.skipUnless(__import__("shutil").which("ffmpeg") and __import__("shutil").which("ffprobe"), "FFmpeg fixture probe")
    def test_actual_worker_missing_models_emits_json_and_keeps_sidecar(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/"recording with spaces.wav";output=root/"lyrics.json";output.write_text("Keep this corrected sidecar")
            with wave.open(str(source),"wb") as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(8000);audio.writeframes(b"\x00\x00"*9600)
            original=source.read_bytes()
            config=ExtractionConfig(python_path=sys.executable,model_root=str(root/"missing-models"),torch_home=str(root/"missing-torch"),cache_dir=str(root/"cache"),use_gpu=False,auto_review=False,separate_vocals=False)
            result=subprocess.run(build_command(source,output,config),capture_output=True,text=True,timeout=15)
            events=[parse_worker_event(line) for line in result.stdout.splitlines()]
            self.assertEqual(result.returncode,1);self.assertEqual(events[-1]["type"],"error")
            self.assertEqual(output.read_text(),"Keep this corrected sidecar");self.assertEqual(source.read_bytes(),original)

    def test_invalid_config_rejected_and_unknown_future_fields_ignored(self):
        for changes in [{"cpu_threads": 0}, {"cpu_threads": True}, {"use_gpu": "yes"}, {"gpu_wait_seconds": -1}, {"engine": "arbitrary"}, {"python_path": "bad\x00python"}]:
            with self.assertRaises(ValueError):ExtractionConfig.from_dict(changes)
        self.assertEqual(ExtractionConfig.from_dict({"future": 1}).engine, "large-v3")

    def test_relocated_frozen_bundle_refreshes_media_library_path(self):
        config = ExtractionConfig(python_path=sys.executable, media_library_path="/an-old-bundle")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "_MEIPASS", "/current-bundle/_internal", create=True):
            command = build_command(Path("song.wav"), Path("song.json"), config)
        encoded = json.loads(command[command.index("--config-json") + 1])
        self.assertEqual(encoded["media_library_path"], "/current-bundle/_internal")
        self.assertEqual(config.media_library_path, "/an-old-bundle")

    @unittest.skipIf(os.name == "nt", "Linux frozen library environment")
    def test_media_libraries_are_scoped_to_media_children_only(self):
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/model-libs"}):
            config = ExtractionConfig.from_dict({"media_library_path": "/bundle/_internal"})
            self.assertEqual(ExtractionConfig.from_dict(config.to_dict()).media_library_path, "/bundle/_internal")
            self.assertEqual(media_environment(config.media_library_path)["LD_LIBRARY_PATH"], "/bundle/_internal:/model-libs")
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/model-libs")
            backend_path = Path(worker.__file__).with_name("extraction_backend") / "lyrics_pipeline.py"
            spec = importlib.util.spec_from_file_location("test_lyrics_pipeline_media", backend_path)
            backend = importlib.util.module_from_spec(spec);spec.loader.exec_module(backend)
            with patch.dict(os.environ, {"GNB_LYRICS_MEDIA_LIBRARY_PATH": config.media_library_path}), patch.object(backend.subprocess, "run") as invoked:
                backend.decode(Path("input.wav"), Path("output.wav"))
            self.assertEqual(invoked.call_args.kwargs["env"]["LD_LIBRARY_PATH"], "/bundle/_internal:/model-libs")
            self.assertEqual(os.environ["LD_LIBRARY_PATH"], "/model-libs")

    def test_status_is_discovery_only_and_no_download_is_triggered(self):
        with tempfile.TemporaryDirectory() as folder:
            config = ExtractionConfig(python_path=sys.executable, model_root=folder, torch_home=folder)
            status = config_status(config)
            self.assertFalse(status["ready"])
            self.assertTrue(status["missing_models"])
            self.assertIn("no model is downloaded", status["note"].lower())

    def test_model_free_jsonl_event_parser(self):
        self.assertEqual(parse_worker_event(b'{"type":"progress","fraction":0.5}')["fraction"], .5)
        self.assertIsNone(parse_worker_event("Torch log line"))
        self.assertIsNone(parse_worker_event('{"type":"unknown"}'))
        self.assertIsNone(parse_worker_event("[1,2]"))

    def test_atomic_result_keeps_previous_revision_and_never_partial_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "song.json";path.write_text('{"words":"original"}')
            worker.atomic_result(path, {"words": "new"})
            self.assertEqual(json.loads(path.read_text()), {"words": "new"})
            versions = list((path.parent / "history").glob("*.json"))
            self.assertEqual(len(versions), 1)
            self.assertEqual(json.loads(versions[0].read_text()), {"words": "original"})
            with self.assertRaises(ValueError):worker.atomic_result(path, {"invalid": float("nan")})
            self.assertEqual(json.loads(path.read_text()), {"words": "new"})
            self.assertFalse(list(path.parent.glob(".extract-*")))

    def test_source_duration_is_checked_before_model_or_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "long.mp3";path.write_bytes(b"fixture")
            fake = subprocess.CompletedProcess([], 0, stdout='{"format":{"duration":"901"}}')
            with patch.object(worker.shutil, "which", return_value="fixture"), patch.object(worker.subprocess, "run", return_value=fake):
                with self.assertRaisesRegex(ValueError, "will not be truncated"):
                    worker.validate_source(path)

    def test_source_change_guard_and_failure_preserve_existing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);source = root / "song.mp3";source.write_bytes(b"fixture");output = root / "song.json";output.write_text("Keep original")
            config = ExtractionConfig(python_path=sys.executable, model_root=str(root), torch_home=str(root), cache_dir=str(root / "cache"))
            with patch.object(worker, "validate_source", return_value=(7, source.stat().st_mtime_ns, "a" * 64, 60)), patch.object(worker, "require_runtime", side_effect=ValueError("No models installed")):
                with self.assertRaisesRegex(ValueError, "No models"):
                    worker.run(source, output, config)
            self.assertEqual(output.read_text(), "Keep original")
            self.assertEqual(source.read_bytes(), b"fixture")

    def test_reference_sheet_length_rejected_without_model_import(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "song.mp3";source.write_bytes(b"fixture")
            with self.assertRaisesRegex(ValueError, "20,000"):
                worker.run(source, Path(folder) / "song.json", ExtractionConfig(), "x" * 20001)


    def test_changed_recording_cannot_commit_new_lyrics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);source = root / "song.mp3";source.write_bytes(b"fixture");output = root / "song.json";output.write_text("Keep original")
            stamp = source.stat();digest = worker.source_fingerprint(source)
            config = ExtractionConfig(python_path=sys.executable, model_root=str(root), torch_home=str(root), cache_dir=str(root / "cache"), use_gpu=False)
            @contextlib.contextmanager
            def reservation():yield {"available":False,"busy":False}
            def changed(*args,**kwargs):
                source.write_bytes(b"a replaced recording")
                return {"duration":60,"segments":[]}
            modules = {"lyrics_acceleration":SimpleNamespace(reservation=reservation),
                       "lyrics_pipeline":SimpleNamespace(extract=changed),
                       "lyrics_review":SimpleNamespace(review=lambda *a,**kw:a[2]),
                       "lyrics_quality":SimpleNamespace(assess=lambda data:{"status":"pass"})}
            with patch.object(worker,"validate_source",return_value=(stamp.st_size,stamp.st_mtime_ns,digest,60)),patch.object(worker,"require_runtime"),patch.dict(sys.modules,modules):
                with self.assertRaisesRegex(ValueError,"recording changed"):
                    worker.run(source,output,config)
            self.assertEqual(output.read_text(),"Keep original")

    @unittest.skipIf(os.name == "nt", "POSIX process-group cancellation")
    def test_cancel_terminates_only_the_workers_own_child_group(self):
        import select
        module_dir = str(Path(worker.__file__).parent)
        code = "import sys,subprocess,time;sys.path.insert(0,"+repr(module_dir)+");import extract_worker as w;w.isolate_process_group();child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);print(child.pid,flush=True)\ntry:time.sleep(30)\nexcept w.Cancelled:child.wait(timeout=5);raise SystemExit(130)"
        process = subprocess.Popen([sys.executable,"-u","-c",code],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            readable,_,_ = select.select([process.stdout],[],[],5)
            self.assertTrue(readable,"Worker did not initialize in time")
            child_pid = int(process.stdout.readline())
            process.terminate();process.communicate(timeout=8)
            self.assertEqual(process.returncode,130)
            with self.assertRaises(ProcessLookupError):os.kill(child_pid,0)
        finally:
            if process.poll() is None:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
