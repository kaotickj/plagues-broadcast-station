"""Launch optional, installed local lyric models outside the media-player process."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
import json
import ipaddress
import os
import shutil
import sys
from urllib.parse import urlsplit


MODEL_PATH_FIELDS = ('python_path', 'model_root', 'torch_home', 'gpu_python_path',
                     'vulkan_binary', 'vulkan_model', 'gpu_lock', 'transcription_model_path',
                     'review_model_path', 'separator_model_path', 'alignment_model_path')
LINUX_SHARED_PATHS = {
    'model_root': Path('/var/lib/gnb-lyrics/models'),
    'torch_home': Path('/var/lib/gnb-lyrics/torch'),
    'python_path': Path('/opt/gnb-lyrics-quality/bin/python'),
    'gpu_python_path': Path('/opt/gnb-lyrics-gpu/bin/python'),
    'vulkan_binary': Path('/opt/gnb-whisper-vulkan-src/build/bin/whisper-cli'),
    'vulkan_model': Path('/var/lib/gnb-lyrics/models/whisper-vulkan/ggml-large-v3-q5_0.bin'),
    'gpu_lock': Path('/run/lock/gnb-ai-gpu.lock'),
}
_ALIGNMENT_CHECKPOINT = Path('hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth')


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "PlaguesBroadcastStation"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "plagues-broadcast-station"


@dataclass
class ExtractionConfig:
    python_path: str = ""
    model_root: str = ""
    torch_home: str = ""
    gpu_python_path: str = ""
    vulkan_binary: str = ""
    vulkan_model: str = ""
    gpu_lock: str = ""
    cache_dir: str = ""
    media_library_path: str = ""
    transcription_model_path: str = ""
    review_model_path: str = ""
    separator_model_path: str = ""
    alignment_model_path: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    use_gpu: bool = True
    wait_for_gpu: bool = True
    gpu_wait_seconds: int = 900
    separate_vocals: bool = True
    reuse_recognition: bool = True
    auto_review: bool = True
    cpu_threads: int = 2
    engine: str = "large-v3"
    recognition_language: str = "en"
    recognition_task: str = "transcribe"
    recognition_prompt: str = ""
    explicit_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict) -> "ExtractionConfig":
        if not isinstance(values, dict):
            raise ValueError("Extraction settings must be a JSON object.")
        known = cls.__dataclass_fields__
        config = cls(**{key: value for key, value in values.items() if key in known})
        if (not isinstance(config.explicit_paths, list) or len(config.explicit_paths) > len(MODEL_PATH_FIELDS)
                or any(not isinstance(name, str) or name not in MODEL_PATH_FIELDS for name in config.explicit_paths)):
            raise ValueError('Explicit model paths must name supported model/runtime settings.')
        config.explicit_paths = list(dict.fromkeys(config.explicit_paths))
        for name in ("python_path", "model_root", "torch_home", "gpu_python_path", "vulkan_binary", "vulkan_model", "gpu_lock", "cache_dir", "media_library_path", "transcription_model_path", "review_model_path", "separator_model_path", "alignment_model_path", "ollama_url", "ollama_model", "engine"):
            value = getattr(config, name)
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise ValueError("Invalid extraction path or model setting.")
        for name in ("use_gpu", "wait_for_gpu", "separate_vocals", "reuse_recognition", "auto_review"):
            if not isinstance(getattr(config, name), bool):
                raise ValueError("Extraction switches must be true or false.")
        if isinstance(config.cpu_threads, bool) or not isinstance(config.cpu_threads, int) or not 1 <= config.cpu_threads <= 8:
            raise ValueError("Choose between one and eight model CPU threads.")
        if isinstance(config.gpu_wait_seconds, bool) or not isinstance(config.gpu_wait_seconds, int) or not 0 <= config.gpu_wait_seconds <= 3600:
            raise ValueError("GPU wait must be between zero and 3600 seconds.")
        if config.engine not in {"large-v3", "medium.en", "small.en"}:
            raise ValueError("Choose an installed Whisper model: large-v3, medium.en or small.en.")
        language = config.recognition_language
        if not isinstance(language, str):
            raise ValueError("Choose auto or a two/three-letter recognition language code.")
        language = language.strip().lower()
        if language != "auto" and not (len(language) in (2, 3) and language.isascii() and language.isalpha()):
            raise ValueError("Choose auto or a two/three-letter recognition language code.")
        config.recognition_language = language
        if config.recognition_task != "transcribe":
            raise ValueError("Lyrics must use transcription, not translation.")
        prompt = config.recognition_prompt
        if (not isinstance(prompt, str) or len(prompt) > 500
                or any((ord(c) < 32 and c not in "\n\t") or ord(c) == 127 for c in prompt)):
            raise ValueError("Recognition context must be plain text of at most 500 characters.")
        config.ollama_url = local_ollama_url(config.ollama_url)
        if len(config.ollama_model) > 300 or any(ord(c) < 32 for c in config.ollama_model):
            raise ValueError("Choose a valid installed Ollama model name.")
        return config


def local_ollama_url(value: str) -> str:
    """A text-model connection stays on this machine and contains no credentials."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Use a local Ollama URL such as http://127.0.0.1:11434.")
    host = parsed.hostname or ""
    if host.lower() == "localhost":
        host = "127.0.0.1"
    try:
        address = ipaddress.ip_address(host)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("The Ollama URL must use a valid local loopback address.") from error
    if not address.is_loopback or not 1 <= port <= 65535:
        raise ValueError("The Ollama URL must use a valid local loopback address and port.")
    hostname = f"[{address}]" if address.version == 6 else str(address)
    return f"{parsed.scheme}://{hostname}:{port}"


def _configured_model_paths(config: ExtractionConfig) -> dict[str, Path]:
    """Explicit components can live in different caches without copying weights."""
    root = Path(config.model_root).expanduser()
    return {
        "transcription": Path(config.transcription_model_path).expanduser() if config.transcription_model_path else root / config.engine,
        "review": Path(config.review_model_path).expanduser() if config.review_model_path else root / "medium.en",
        "separator": Path(config.separator_model_path).expanduser() if config.separator_model_path else root / "htdemucs",
        "alignment": Path(config.alignment_model_path).expanduser() if config.alignment_model_path else Path(config.torch_home).expanduser() / _ALIGNMENT_CHECKPOINT,
    }


def _readable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.R_OK)
    except OSError:
        return False


def _executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _executable_location(value: str) -> Path | None:
    """Locate a configured executable or PATH command without executing it."""
    if not value:
        return None
    path = Path(value).expanduser()
    if _executable(path):
        return path
    found = shutil.which(value)
    return Path(found) if found and _executable(Path(found)) else None


def _existing_lock(path: Path) -> bool:
    # Keep a known cooperative lease selected even if this user cannot open it.
    # Omitting that existing lease would allow GPU work without coordination.
    try:
        return path.is_file()
    except OSError:
        return False


def _runtime_candidates(base: Path) -> list[Path]:
    suffix = Path('Scripts/python.exe') if os.name == 'nt' else Path('bin/python')
    return [base / 'model-runtime' / suffix, base / 'runtime' / suffix]


def _component_files(component: str, path: Path) -> list[Path]:
    if component in {'transcription', 'review'}:
        return [path / 'model.bin']
    if component == 'separator':
        return [path / 'spec.json', path / 'model.safetensors']
    return [path]


def _root_files(config: ExtractionConfig, root: Path) -> list[Path]:
    files = [root / config.engine / 'model.bin']
    if config.auto_review:
        files.append(root / 'medium.en/model.bin')
    if config.separate_vocals or config.auto_review:
        files += [root / 'htdemucs/spec.json', root / 'htdemucs/model.safetensors']
    return files


def resolve_existing_models(config: ExtractionConfig) -> tuple[ExtractionConfig, dict]:
    """Resolve only blank/known defaults through bounded existing-file checks.

    A custom value, including a missing one, is kept literally. Future explicit
    selections matching a historical default are protected by explicit_paths.
    Shared paths are Linux-only. No environment, cache, GPU switch or filesystem
    is changed; presence/readability is not model/dependency validation.
    """
    resolved = replace(config, explicit_paths=list(config.explicit_paths))
    base = default_data_dir()
    shared = LINUX_SHARED_PATHS if sys.platform.startswith('linux') else {}
    local_models, local_torch = base / 'models', base / 'torch'
    local_components = {'transcription': local_models / config.engine, 'review': local_models / 'medium.en',
                        'separator': local_models / 'htdemucs', 'alignment': local_torch / _ALIGNMENT_CHECKPOINT}
    shared_components = ({'transcription': shared['model_root'] / config.engine,
                          'review': shared['model_root'] / 'medium.en',
                          'separator': shared['model_root'] / 'htdemucs',
                          'alignment': shared['torch_home'] / _ALIGNMENT_CHECKPOINT} if shared else {})
    defaults = {'model_root': [local_models], 'torch_home': [local_torch],
                'python_path': _runtime_candidates(base), 'gpu_python_path': [],
                'vulkan_binary': [], 'vulkan_model': [local_models / 'whisper-vulkan/ggml-large-v3-q5_0.bin'],
                'gpu_lock': []}
    for name, path in shared.items():
        defaults.setdefault(name, []).append(path)
    for component, path in local_components.items():
        defaults[component + '_model_path'] = [path] + ([shared_components[component]] if shared else [])
    automatic = {name: name not in config.explicit_paths and
                 (not getattr(config, name) or getattr(config, name) in {str(p) for p in defaults.get(name, [])})
                 for name in MODEL_PATH_FIELDS}
    fields, notes = {}, []

    def remember(name, value, required, source, explanation):
        if name in {'python_path', 'gpu_python_path', 'vulkan_binary'}:
            executable = _executable_location(str(value))
            missing = [] if executable is not None else ([str(value)] if value else [])
        else:
            executable = None
            missing = [str(p) for p in required if not _readable(p)
                       or (name == 'gpu_lock' and not os.access(p, os.R_OK | os.W_OK))]
        fields[name] = {'configured': getattr(config, name), 'resolved': str(value), 'source': source,
                        'missing_files': missing, 'explanation': explanation}
        if executable is not None:
            fields[name]['resolved_executable'] = str(executable)

    def choose(name, candidates, required, available, fallback):
        original = getattr(config, name)
        if not automatic[name]:
            value = original
            reason = 'Explicit/custom path preserved; missing paths are not replaced automatically.'
            origin = 'configured'
        else:
            choices = ([Path(original).expanduser()] if original else []) + candidates
            found = next((p for p in choices if available(p)), None)
            value = str(found) if found is not None else str(fallback)
            origin = 'shared-linux' if found is not None and any(str(found) == str(p) for p in shared.values()) else ('user-local' if found is not None else 'fallback')
            reason = ('Using existing ' + origin + ' path.' if found is not None else 'No installed candidate found; retained the user-local/default fallback.')
        setattr(resolved, name, value)
        req = required(Path(value).expanduser()) if value else []
        remember(name, value, req, origin, reason)
        if value != original:
            notes.append(name + ': ' + reason + ' ' + value)

    roots = [local_models] + ([shared['model_root']] if shared else [])
    choose('model_root', roots, lambda p: _root_files(config, p),
           lambda p: all(_readable(f) for f in _root_files(config, p)), local_models)
    torch_roots = [local_torch] + ([shared['torch_home']] if shared else [])
    choose('torch_home', torch_roots, lambda p: [p / _ALIGNMENT_CHECKPOINT],
           lambda p: _readable(p / _ALIGNMENT_CHECKPOINT), local_torch)
    runtimes = _runtime_candidates(base) + ([shared['python_path']] if shared else [])
    choose('python_path', runtimes, lambda p: [p], _executable, '')
    for name in ('gpu_python_path', 'vulkan_binary', 'vulkan_model', 'gpu_lock'):
        candidates = ([Path(resolved.model_root) / 'whisper-vulkan/ggml-large-v3-q5_0.bin'] if name == 'vulkan_model' else [])
        candidates += [shared[name]] if shared else []
        check = _executable if name in {'gpu_python_path', 'vulkan_binary'} else (_existing_lock if name == 'gpu_lock' else _readable)
        choose(name, candidates, lambda p: [p], check,
               Path(resolved.model_root) / 'whisper-vulkan/ggml-large-v3-q5_0.bin' if name == 'vulkan_model' else '')

    effective = _configured_model_paths(resolved)
    for component, current in effective.items():
        name = component + '_model_path'
        group = 'torch_home' if component == 'alignment' else 'model_root'
        can_fallback = automatic[name] and automatic[group]
        candidates = [local_components[component], current] + ([shared_components[component]] if shared else [])
        found = next((p for p in candidates if all(_readable(f) for f in _component_files(component, p))), None) if can_fallback else None
        chosen = found if found is not None else current
        effective[component] = chosen
        # A blank override stays automatic when the operator later changes roots.
        if getattr(config, name) and automatic[name] and found is not None:
            setattr(resolved, name, str(chosen))
        source = 'configured' if not can_fallback else ('shared-linux' if shared and chosen == shared_components[component] else ('user-local' if found is not None else 'fallback'))
        reason = ('Custom component/root preserved; choose or clear it explicitly to change it.' if not can_fallback else
                  'Automatic component path selected from existing required files; the blank override remains automatic.')
        remember(name, chosen, _component_files(component, chosen), source, reason)
        if chosen != current:
            notes.append(component + ': ' + reason + ' ' + str(chosen))
    return resolved, {'resolved_values': {name: getattr(resolved, name) for name in MODEL_PATH_FIELDS},
                      'effective_model_paths': {name: str(path) for name, path in effective.items()},
                      'fields': fields, 'notes': notes,
                      'validation': 'Existing readable files/executables only; model formats and runtime dependencies have not been loaded or validated.'}


def effective_model_paths(config: ExtractionConfig) -> dict[str, Path]:
    _, report = resolve_existing_models(config)
    return {name: Path(path) for name, path in report['effective_model_paths'].items()}


def required_model_files(config: ExtractionConfig, *, reference: bool = False) -> list[Path]:
    paths = effective_model_paths(config)
    required = [paths["alignment"]]
    if not reference:
        required += [paths["transcription"] / "model.bin"]
    if config.separate_vocals or config.auto_review:
        required += [paths["separator"] / "spec.json", paths["separator"] / "model.safetensors"]
    if config.auto_review:
        required += [paths["review"] / "model.bin"]
    return required


def discover_config() -> ExtractionConfig:
    """Portable defaults; explicit environment selections keep their intent."""
    base = default_data_dir()
    model_root = Path(os.environ.get("GNB_PLAYER_MODELS", str(base / "models")))
    gpu_python = os.environ.get("GNB_PLAYER_GPU_PYTHON", "")
    vulkan = os.environ.get("GNB_PLAYER_VULKAN_BINARY", "")
    config = ExtractionConfig(
        python_path=os.environ.get("GNB_PLAYER_PYTHON", ""),
        model_root=str(model_root),
        torch_home=os.environ.get("GNB_PLAYER_TORCH_HOME", str(base / "torch")),
        gpu_python_path=gpu_python,
        vulkan_binary=vulkan,
        vulkan_model=os.environ.get("GNB_PLAYER_VULKAN_MODEL", str(model_root / "whisper-vulkan/ggml-large-v3-q5_0.bin")),
        gpu_lock=os.environ.get("GNB_PLAYER_GPU_LOCK", ""),
        cache_dir=os.environ.get("GNB_PLAYER_CACHE_DIR", str(base / "extraction-cache")),
        media_library_path=os.environ.get("GNB_PLAYER_MEDIA_LIBRARY_PATH", str(getattr(sys, "_MEIPASS", ""))),
        transcription_model_path=os.environ.get("GNB_PLAYER_TRANSCRIPTION_MODEL", ""),
        review_model_path=os.environ.get("GNB_PLAYER_REVIEW_MODEL", ""),
        separator_model_path=os.environ.get("GNB_PLAYER_SEPARATOR_MODEL", ""),
        alignment_model_path=os.environ.get("GNB_PLAYER_ALIGNMENT_MODEL", ""),
        ollama_url=os.environ.get("GNB_PLAYER_OLLAMA_URL", "http://127.0.0.1:11434"),
        ollama_model=os.environ.get("GNB_PLAYER_OLLAMA_MODEL", ""),
        recognition_language=os.environ.get("GNB_PLAYER_RECOGNITION_LANGUAGE", "en"),
        recognition_prompt=os.environ.get("GNB_PLAYER_RECOGNITION_PROMPT", ""),
        use_gpu=bool(gpu_python or vulkan),
    )
    environment_paths = {'python_path': 'GNB_PLAYER_PYTHON', 'model_root': 'GNB_PLAYER_MODELS',
                         'torch_home': 'GNB_PLAYER_TORCH_HOME', 'gpu_python_path': 'GNB_PLAYER_GPU_PYTHON',
                         'vulkan_binary': 'GNB_PLAYER_VULKAN_BINARY', 'vulkan_model': 'GNB_PLAYER_VULKAN_MODEL',
                         'gpu_lock': 'GNB_PLAYER_GPU_LOCK', 'transcription_model_path': 'GNB_PLAYER_TRANSCRIPTION_MODEL',
                         'review_model_path': 'GNB_PLAYER_REVIEW_MODEL', 'separator_model_path': 'GNB_PLAYER_SEPARATOR_MODEL',
                         'alignment_model_path': 'GNB_PLAYER_ALIGNMENT_MODEL'}
    config.explicit_paths = [name for name, variable in environment_paths.items() if os.environ.get(variable)]
    return config


def media_environment(library_path: str = "") -> dict[str, str]:
    """Native media tools need bundle libraries; model Python must stay clean."""
    environment = os.environ.copy()
    if library_path and os.name != "nt":
        inherited = environment.get("LD_LIBRARY_PATH", "")
        environment["LD_LIBRARY_PATH"] = library_path + (os.pathsep + inherited if inherited else "")
    return environment


def config_status(config: ExtractionConfig) -> dict:
    """Cheap discovery only; never load a model or contact a download service."""
    config, resolution = resolve_existing_models(config)
    requirements = required_model_files(config)
    missing = [str(path) for path in requirements if not _readable(path)]
    python_exists = _executable_location(config.python_path) is not None
    return {"ready": python_exists and not missing and bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")),
            "python_available": python_exists, "missing_models": missing,
            "ffmpeg_available": bool(shutil.which("ffmpeg")), "ffprobe_available": bool(shutil.which("ffprobe")),
            "gpu_configured": bool(config.use_gpu and config.vulkan_binary and _executable(Path(config.vulkan_binary).expanduser()) and config.vulkan_model and _readable(Path(config.vulkan_model).expanduser())),
            "resolved_values": resolution['resolved_values'], "effective_model_paths": resolution['effective_model_paths'],
            "path_resolution": resolution, "resolution_notes": resolution['notes'],
            "note": "The player works without models. Extraction requires the separate local model runtime; no model is downloaded automatically. File discovery does not validate model formats or runtime dependencies."}


def build_command(source: str | Path, output: str | Path, config: ExtractionConfig,
                  reference_text: str = "") -> list[str]:
    config = ExtractionConfig.from_dict(config.to_dict())
    config, _ = resolve_existing_models(config)
    if getattr(sys, "frozen", False):
        # This is a runtime bundle location, never a portable user preference.
        config.media_library_path = str(sys._MEIPASS)
    if not config.python_path:
        raise ValueError("Choose the Python executable for the lyric model runtime.")
    source_path, output_path = Path(source).expanduser().absolute(), Path(output).expanduser().absolute()
    if source_path == output_path:
        raise ValueError("Lyrics must be saved separately from the recording.")
    if len(reference_text) > 20000:
        raise ValueError("The lyric sheet is limited to 20,000 characters.")
    # The worker and backend are plain data files in frozen builds, so an external
    # Python runtime can import them without importing Qt or a frozen app binary.
    worker = Path(__file__).resolve().with_name("extract_worker.py")
    command = [config.python_path, "-u", str(worker), "--source", str(source_path),
               "--output", str(output_path), "--config-json", json.dumps(config.to_dict(), ensure_ascii=False)]
    if reference_text.strip():
        command += ["--reference-text", reference_text]
    return command


def parse_worker_event(line: bytes | str) -> dict | None:
    try:
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if len(line) > 65536:
            return None
        data = json.loads(line)
        return data if isinstance(data, dict) and data.get("type") in {"progress", "done", "error", "cancelled"} else None
    except (ValueError, TypeError):
        return None
