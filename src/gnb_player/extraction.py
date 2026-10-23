"""Launch optional, installed local lyric models outside the media-player process."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import ipaddress
import os
import shutil
import sys
from urllib.parse import urlsplit


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
    auto_review: bool = True
    cpu_threads: int = 2
    engine: str = "large-v3"
    recognition_language: str = "en"
    recognition_task: str = "transcribe"
    recognition_prompt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, values: dict) -> "ExtractionConfig":
        if not isinstance(values, dict):
            raise ValueError("Extraction settings must be a JSON object.")
        known = cls.__dataclass_fields__
        config = cls(**{key: value for key, value in values.items() if key in known})
        for name in ("python_path", "model_root", "torch_home", "gpu_python_path", "vulkan_binary", "vulkan_model", "gpu_lock", "cache_dir", "media_library_path", "transcription_model_path", "review_model_path", "separator_model_path", "alignment_model_path", "ollama_url", "ollama_model", "engine"):
            value = getattr(config, name)
            if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
                raise ValueError("Invalid extraction path or model setting.")
        for name in ("use_gpu", "wait_for_gpu", "separate_vocals", "auto_review"):
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


def effective_model_paths(config: ExtractionConfig) -> dict[str, Path]:
    """Explicit components can live in different caches without copying weights."""
    root = Path(config.model_root).expanduser()
    return {
        "transcription": Path(config.transcription_model_path).expanduser() if config.transcription_model_path else root / config.engine,
        "review": Path(config.review_model_path).expanduser() if config.review_model_path else root / "medium.en",
        "separator": Path(config.separator_model_path).expanduser() if config.separator_model_path else root / "htdemucs",
        "alignment": Path(config.alignment_model_path).expanduser() if config.alignment_model_path else Path(config.torch_home).expanduser() / "hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth",
    }


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
    """Portable defaults; machine-specific runtimes are configured explicitly."""
    base = default_data_dir()
    model_root = Path(os.environ.get("GNB_PLAYER_MODELS", str(base / "models")))
    gpu_python = os.environ.get("GNB_PLAYER_GPU_PYTHON", "")
    vulkan = os.environ.get("GNB_PLAYER_VULKAN_BINARY", "")
    return ExtractionConfig(
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


def media_environment(library_path: str = "") -> dict[str, str]:
    """Native media tools need bundle libraries; model Python must stay clean."""
    environment = os.environ.copy()
    if library_path and os.name != "nt":
        inherited = environment.get("LD_LIBRARY_PATH", "")
        environment["LD_LIBRARY_PATH"] = library_path + (os.pathsep + inherited if inherited else "")
    return environment


def config_status(config: ExtractionConfig) -> dict:
    """Cheap discovery only; never load a model or contact a download service."""
    requirements = required_model_files(config)
    missing = [str(path) for path in requirements if not path.is_file()]
    python_exists = bool(config.python_path and (Path(config.python_path).is_file() or shutil.which(config.python_path)))
    return {"ready": python_exists and not missing and bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe")),
            "python_available": python_exists, "missing_models": missing,
            "ffmpeg_available": bool(shutil.which("ffmpeg")), "ffprobe_available": bool(shutil.which("ffprobe")),
            "gpu_configured": bool(config.use_gpu and config.vulkan_binary and Path(config.vulkan_binary).is_file() and Path(config.vulkan_model).is_file()),
            "note": "The player works without models. Extraction requires the separate local model runtime; no model is downloaded automatically."}


def build_command(source: str | Path, output: str | Path, config: ExtractionConfig,
                  reference_text: str = "") -> list[str]:
    config = ExtractionConfig.from_dict(config.to_dict())
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
