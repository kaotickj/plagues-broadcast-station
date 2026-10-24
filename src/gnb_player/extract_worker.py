#!/usr/bin/env python3
"""External model process. JSONL stdout is a stable GUI protocol; logs go to stderr."""
from __future__ import annotations

from pathlib import Path
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from extraction import ExtractionConfig, config_status, media_environment, effective_model_paths, required_model_files
from lyrics import lyric_write_lock, lyric_target_bytes, lyric_target_revision, MAX_LYRIC_BYTES
from lyric_timing import normalize_generated_timing

OUTPUT = sys.stdout
OWN_PROCESS_GROUP = False
SUPPORTED = {".wav", ".flac", ".mp3", ".ogg", ".oga", ".m4a", ".aac", ".opus", ".mp4", ".mkv", ".webm", ".aiff", ".aif", ".wma"}
_UNSPECIFIED_TARGET = object()


class Cancelled(BaseException):
    pass


def emit(event: dict) -> None:
    OUTPUT.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
    OUTPUT.flush()


def progress(phase: str, fraction: float, message: str) -> None:
    emit({"type": "progress", "phase": phase, "fraction": max(0, min(1, float(fraction))), "message": message})


def cancel(signum, frame) -> None:
    # The worker owns its own process group. Terminating it also terminates its
    # FFmpeg / Vulkan / ROCm children, so cancelled inference cannot outlive it.
    if os.name != "nt" and OWN_PROCESS_GROUP:
        signal.signal(signum, signal.SIG_IGN)
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise Cancelled()


def isolate_process_group() -> None:
    global OWN_PROCESS_GROUP
    if os.name != "nt":
        try:
            os.setsid()
        except PermissionError:
            pass
        OWN_PROCESS_GROUP = os.getpgrp() == os.getpid()
        try:
            os.nice(10)
        except OSError:
            pass
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)


def source_fingerprint(source: Path) -> str:
    with source.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def validate_source(source: Path, media_library_path: str = "") -> tuple[int, int, str, float]:
    if not source.is_file() or source.suffix.lower() not in SUPPORTED:
        raise ValueError("Choose a local audio or video recording for lyric extraction.")
    stat = source.stat()
    if not 0 < stat.st_size <= 2 * 1024**3:
        raise ValueError("Recordings for lyric extraction must be nonempty and smaller than 2 GB.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("Install FFmpeg (including ffprobe) and make it available on PATH before extracting lyrics.")
    result = subprocess.run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "format=duration", "-of", "json", str(source)],
                            capture_output=True, text=True, timeout=30, check=True,
                            env=media_environment(media_library_path))
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if not 1 <= duration <= 900:
        raise ValueError("Extract one song at a time: supported recording length is 1 second to 15 minutes. The source will not be truncated.")
    return stat.st_size, stat.st_mtime_ns, source_fingerprint(source), duration


def prepare_environment(config: ExtractionConfig) -> None:
    cache = Path(config.cache_dir).expanduser()
    if not config.cache_dir:
        raise ValueError("Choose a writable private extraction cache directory.")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    models = effective_model_paths(config)
    values = {
        "GNB_LYRICS_MODEL_ROOT": config.model_root,
        "TORCH_HOME": config.torch_home,
        "GNB_LYRICS_TRANSCRIPTION_MODEL": str(models["transcription"]),
        "GNB_LYRICS_REVIEW_MODEL": str(models["review"]),
        "GNB_LYRICS_SEPARATOR_MODEL": str(models["separator"]),
        "GNB_LYRICS_ALIGNMENT_MODEL": str(models["alignment"]),
        "GNB_LYRICS_TRANSCRIPTION_ENGINE": config.engine,
        "GNB_LYRICS_LANGUAGE": config.recognition_language,
        "GNB_LYRICS_TASK": config.recognition_task,
        "GNB_LYRICS_PROMPT": config.recognition_prompt,
        "GNB_LYRICS_GPU_PYTHON": config.gpu_python_path,
        "GNB_LYRICS_VULKAN_BINARY": config.vulkan_binary,
        "GNB_LYRICS_VULKAN_MODEL": config.vulkan_model,
        "GNB_LYRICS_GPU_LOCK": config.gpu_lock,
        "GNB_LYRICS_MEDIA_LIBRARY_PATH": config.media_library_path,
        "GNB_LYRICS_GPU": "1" if config.use_gpu else "0",
        "GNB_LYRICS_PHASE_GPU": "1",
        "GNB_LYRICS_GPU_WAIT": "1" if config.wait_for_gpu else "0",
        "GNB_LYRICS_GPU_WAIT_SECONDS": str(config.gpu_wait_seconds),
        "GNB_LYRICS_ROCM_SEPARATION": "0",
        "GNB_LYRICS_FRESH_RECOGNITION": "0" if config.reuse_recognition else "1",
        "GNB_LYRICS_CPU_THREADS": str(config.cpu_threads),
        "OMP_NUM_THREADS": str(config.cpu_threads),
        "GNB_LYRICS_CACHE_DIR": str(cache / "derived"),
        "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false",
        "XDG_CACHE_HOME": str(cache / "runtime"), "MIOPEN_USER_DB_PATH": str(cache / "miopen"),
        "AMD_COMGR_CACHE_DIR": str(cache / "comgr"), "NUMBA_CACHE_DIR": str(cache / "numba"),
    }
    os.environ.update(values)
    for name in ("derived", "runtime", "miopen", "comgr", "numba"):
        (cache / name).mkdir(parents=True, exist_ok=True, mode=0o700)


def require_runtime(config: ExtractionConfig, reference: str) -> None:
    # find_spec avoids initializing Torch/GPU during the dependency check.
    dependencies = ["torch", "torchaudio", "numpy", "soundfile", "num2words"]
    if not reference:
        dependencies += ["faster_whisper"]
    if config.separate_vocals or config.auto_review:
        dependencies += ["demucs", "safetensors"]
    missing = [name for name in dependencies if importlib.util.find_spec(name) is None]
    if missing:
        raise ValueError("The selected model Python is missing: " + ", ".join(missing) + ". Choose the existing lyric-model Python in Extraction settings, or install the optional lyric-runtime dependencies there.")
    required = required_model_files(config, reference=bool(reference))
    absent = [str(path) for path in required if not path.is_file()]
    if absent:
        raise ValueError("Local lyric models are not configured. Missing: " + "; ".join(absent) + ". Nothing was downloaded and existing lyrics were kept.")


def atomic_result(target: Path, data: dict, expected_revision=_UNSPECIFIED_TARGET) -> None:
    """Commit a model result, optionally against the raw target seen before inference.

    The two-argument compatibility API remains an immediate serialized write.
    Long-running worker jobs must pass their captured raw revision (including
    explicit None for an initially absent target).
    """
    target = Path(target)
    with lyric_write_lock(target):
        original = lyric_target_bytes(target)
        actual = hashlib.sha256(original).hexdigest() if original is not None else None
        if expected_revision is not _UNSPECIFIED_TARGET and actual != expected_revision:
            raise ValueError('The lyric file changed during extraction. Newer edits were kept; reopen them before retrying.')
        _atomic_result_unlocked(target, data, original)


def _atomic_result_unlocked(target: Path, data: dict, original: bytes | None) -> None:
    data = dict(data)
    # An explicit refresh of the same recording keeps its origin mapping, but
    # never carries an earlier publication/approval into newly recognized text.
    if original is not None:
        try:
            previous = json.loads(original.decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            previous = None
        identity = data.get('source_sha256')
        if (isinstance(previous, dict) and isinstance(identity, str) and len(identity) == 64
                and previous.get('source_sha256') == identity):
            for key in ('song_id', 'recording_id', 'origin_id', 'source_id', 'origin_song_id',
                        'source', 'origin', 'provenance', 'import_provenance'):
                if key not in data and key in previous:
                    data[key] = previous[key]
    # Model output can never inherit an earlier explicit human approval.
    data.pop("human_review", None)
    if 'reviewed' in data:
        data['reviewed'] = False
    data.pop('publication', None)
    if target.is_symlink():
        raise ValueError("Save lyrics to a regular JSON file rather than a symbolic link.")
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')
    if len(payload) > MAX_LYRIC_BYTES:
        raise ValueError('The extracted lyric file exceeds the supported 8 MB saved-file size.')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".extract-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if lyric_target_bytes(target) != original:
            raise ValueError('The lyric file changed while saving extraction. Newer edits were kept.')
        if original is not None:
            history = target.parent / "history"
            if history.is_symlink():
                raise ValueError("Refusing a symbolic-link lyric history directory.")
            history.mkdir(exist_ok=True, mode=0o700)
            backup = history / (target.stem + "-" + str(time.time_ns()) + '-' + uuid.uuid4().hex[:8] + ".json")
            with backup.open('xb') as handle:
                handle.write(original); handle.flush(); os.fsync(handle.fileno())
            backup.chmod(0o600)
        if lyric_target_bytes(target) != original:
            raise ValueError('The lyric file changed while saving extraction. Newer edits were kept.')
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_failure_evidence(cache: Path, evidence: dict, fingerprint: str, message: str) -> Path:
    """Keep failed decoder evidence separate from all editable lyric revisions."""
    payload = json.dumps({'version': 'extraction-failure-1', 'source_sha256': fingerprint,
                          'created_at': int(time.time()), 'status': 'failed',
                          'message': message, 'recognition_evidence': evidence},
                         ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(payload) > MAX_LYRIC_BYTES:
        raise ValueError('The failed-extraction diagnostic exceeds the private file limit.')
    directory = cache / 'failures'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / (fingerprint + '-' + uuid.uuid4().hex + '.json')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.failure-', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        temporary.replace(target)
        return target
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def run(source: Path, output: Path, config: ExtractionConfig, reference: str = "") -> dict:
    source = source.expanduser().resolve(strict=True)
    output = output.expanduser().absolute()
    if output.suffix.lower() != ".json" or output.resolve(strict=False) == source:
        raise ValueError("Save extracted lyrics to a separate .json file.")
    if len(reference) > 20000:
        raise ValueError("The supplied lyric sheet is limited to 20,000 characters.")
    with lyric_write_lock(output):
        expected_output_revision = lyric_target_revision(output)
    before_size, before_mtime, fingerprint, duration = validate_source(source, config.media_library_path)
    require_runtime(config, reference)
    prepare_environment(config)
    sys.path.insert(0, str(Path(__file__).resolve().with_name("extraction_backend")))
    from lyrics_acceleration import reset_gpu_timings, gpu_timings
    from lyrics_pipeline import extract
    from lyrics_review import review
    from lyrics_quality import assess
    from lyrics_recognition import transcript_snapshot
    reset_gpu_timings()
    with tempfile.TemporaryDirectory(prefix="gnb-player-lyrics-") as folder:
        work = Path(folder)
        try:
            data = extract(source, work, config.engine, config.separate_vocals, reference, progress, title=source.stem)
        except Exception as error:
            evidence = getattr(error, 'recognition_evidence', None)
            if isinstance(evidence, dict) and evidence:
                try:
                    diagnostic = save_failure_evidence(Path(config.cache_dir).expanduser(), evidence, fingerprint, str(error))
                except Exception as diagnostic_error:
                    print('Failed-extraction diagnostics could not be saved:', type(diagnostic_error).__name__, file=sys.stderr, flush=True)
                else:
                    if isinstance(error, ValueError):
                        raise ValueError(str(error)+' Existing lyrics were kept. Diagnostics: '+str(diagnostic)) from error
            raise
        data.setdefault('recognition_evidence',{})['before_review']=transcript_snapshot(data)
        if config.auto_review and assess(data)["status"] != "pass":
            try:
                data = review(source, work, data, progress, preserve_text=bool(reference))
            except Exception as error:
                data.setdefault("warnings", []).append("Independent model review could not complete; check these lyrics before broadcasting.")
                print("Independent review failed:", type(error).__name__, str(error)[:300], file=sys.stderr, flush=True)
        data.setdefault('recognition_evidence',{})['after_review']=transcript_snapshot(data)
        # Review's centisecond timestamps can differ from retained CTC
        # milliseconds. Keep raw evidence and normalize only the final result.
        try:
            normalize_generated_timing(data)
        except ValueError as error:
            message = str(error) + ' Existing lyrics were kept.'
            try:
                diagnostic = save_failure_evidence(Path(config.cache_dir).expanduser(),
                    data.get('recognition_evidence', {}), fingerprint, message)
            except Exception as diagnostic_error:
                print('Failed-extraction diagnostics could not be saved:', type(diagnostic_error).__name__, file=sys.stderr, flush=True)
            else:
                message += ' Diagnostics: ' + str(diagnostic)
            raise ValueError(message) from error
    if source.stat().st_size != before_size or source.stat().st_mtime_ns != before_mtime or source_fingerprint(source) != fingerprint:
        raise ValueError("The recording changed during extraction. Existing lyrics were kept; retry once the copy or edit finishes.")
    if abs(float(data.get("duration", 0)) - duration) > 2:
        raise ValueError("Extracted audio duration did not match the recording. Existing lyrics were kept.")
    data.update(title=source.stem, source_sha256=fingerprint, source_size=before_size,
                source_mtime=before_mtime / 1e9, created_at=int(time.time()), reviewed=False)
    data.setdefault("pipeline",{})["gpu_schedule"]={"policy":"phase-scoped-exclusive","phases":gpu_timings()}
    data["quality"] = assess(data)
    atomic_result(output, data, expected_output_revision)
    return data


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config-json", required=True)
    parser.add_argument("--reference-text", default="")
    parser.add_argument("--reference-file", type=Path)
    args = parser.parse_args(argv)
    isolate_process_group()
    try:
        if len(args.config_json) > 65536:
            raise ValueError("Extraction settings are too large.")
        config = ExtractionConfig.from_dict(json.loads(args.config_json))
        reference = args.reference_file.read_text(encoding="utf-8") if args.reference_file else args.reference_text
        with contextlib.redirect_stdout(sys.stderr):
            data = run(args.source, args.output, config, reference)
        emit({"type": "done", "output": str(args.output.absolute()), "quality": data["quality"],
              "words": sum(len(c.get("words", [])) for c in data["segments"]), "lines": len(data["segments"])})
        return 0
    except Cancelled:
        emit({"type": "cancelled", "message": "Extraction cancelled. Existing lyrics and the recording were kept."})
        return 130
    except Exception as error:
        message = str(error) if isinstance(error, (ValueError, FileNotFoundError, PermissionError)) else "Local lyric extraction failed. Check the model runtime and logs; existing lyrics were kept."
        print(type(error).__name__ + ": " + str(error)[:1000], file=sys.stderr, flush=True)
        emit({"type": "error", "message": message[:4000]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
