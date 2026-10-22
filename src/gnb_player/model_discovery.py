"""Read-only discovery for the optional lyric-model setup wizard.

``discover(extra_roots=..., settings=..., app_data=..., cancelled=...)`` finds
known model formats, checks separate Python runtimes without importing Torch,
and queries a loopback Ollama service. It never installs, downloads, starts a
service, deserializes a checkpoint, or edits an existing model directory.

``scan_models`` performs only the bounded local directory scan. A compatible
candidate means its format/layout passes cheap checks; the installer's final
runtime validation still checks that the selected model actually loads.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .extraction import default_data_dir

CTC_CHECKPOINT = "wav2vec2_fairseq_base_ls960_asr_ls960.pth"
_MAX_JSON = 256 * 1024
_MODEL_SETTINGS = (
    "model_root", "torch_home", "transcription_model_path", "review_model_path",
    "separator_model_path", "alignment_model_path",
)
_IGNORED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".npm", ".gradle"}
_BROAD_HOME_FOLDERS = {"desktop", "documents", "downloads", "pictures", "videos", "music"}
_PACKAGES = {
    "torch": "torch", "torchaudio": "torchaudio", "numpy": "numpy",
    "soundfile": "soundfile", "faster-whisper": "faster_whisper",
    "demucs": "demucs", "safetensors": "safetensors", "num2words": "num2words",
}


@dataclass
class ModelCandidate:
    kind: str
    path: str
    compatible: bool
    reason: str
    engine: str = ""
    format: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PythonRuntime:
    path: str
    version: str = ""
    packages: dict[str, str] = field(default_factory=dict)
    compatible: bool = False
    reason: str = "Not checked."

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OllamaInfo:
    base_url: str = "http://127.0.0.1:11434"
    reachable: bool = False
    models: list[dict] = field(default_factory=list)
    executable: str = ""
    reason: str = "Ollama has not been checked."

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DiscoveryResult:
    models: list[ModelCandidate] = field(default_factory=list)
    runtimes: list[PythonRuntime] = field(default_factory=list)
    ollama: OllamaInfo = field(default_factory=OllamaInfo)
    scanned_roots: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    truncated: bool = False
    entries_checked: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _values(settings) -> Mapping:
    if settings is None:
        return {}
    return settings if isinstance(settings, Mapping) else vars(settings)


def default_search_roots(app_data: Path | None = None, settings=None,
                         environ: Mapping[str, str] | None = None) -> list[Path]:
    """Known caches only; never recursively scan a user's home or all drives."""
    env = os.environ if environ is None else environ
    base = Path(app_data) if app_data else default_data_dir()
    home = Path.home()
    roots = [base / "models", base / "torch", base / "extraction-cache", base / "model-setup/versions"]
    if env.get("HF_HUB_CACHE"):
        roots.append(Path(env["HF_HUB_CACHE"]))
    if env.get("HUGGINGFACE_HUB_CACHE"):
        roots.append(Path(env["HUGGINGFACE_HUB_CACHE"]))
    hf = Path(env["HF_HOME"]) if env.get("HF_HOME") else home / ".cache/huggingface"
    roots.append(hf / "hub")
    torch = Path(env["TORCH_HOME"]) if env.get("TORCH_HOME") else home / ".cache/torch"
    roots += [torch, home / ".cache/whisper"]
    if env.get("OLLAMA_MODELS"):
        roots.append(Path(env["OLLAMA_MODELS"]))
    roots.append(home / ".ollama/models")
    if env.get("LOCALAPPDATA"):
        local = Path(env["LOCALAPPDATA"])
        roots += [local / "huggingface/hub", local / "torch", local / "whisper"]
    values = _values(settings)
    for key in _MODEL_SETTINGS:
        value = values.get(key)
        if isinstance(value, str) and value:
            roots.append(Path(value).expanduser())
    return roots


def _network_path(path: Path) -> bool:
    raw = str(path)
    if raw.startswith(("\\\\", "//")):
        return True
    if os.name == "nt" and path.anchor:
        try:
            import ctypes
            return ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) == 4
        except (AttributeError, OSError):
            return True  # Do not traverse an unclassified Windows drive.
    return False


def _contained(path: Path, roots: list[Path]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _safe_file(path: Path, roots: list[Path]) -> bool:
    try:
        resolved = path.resolve(strict=True)
        return _contained(resolved, roots) and resolved.is_file() and resolved.stat().st_size > 0
    except (OSError, RuntimeError):
        return False


def _small_json(path: Path, roots: list[Path]) -> dict:
    if not _safe_file(path, roots) or path.stat().st_size > _MAX_JSON:
        raise ValueError("Missing or oversized model configuration.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Model configuration must be an object.")
    return data


def _whisper_engine(folder: Path) -> str:
    text = "/".join(folder.parts).lower()
    # The turbo/distilled weights are different recognizers, not large-v3.
    if "turbo" in text or "distil" in text:
        return ""
    if re.search(r"(?:^|[-_/])(?:faster[-_]whisper[-_])?large[-_.]v3(?:$|[-_/])", text):
        return "large-v3"
    if re.search(r"medium[._-]en(?:$|[-_/])", text):
        return "medium.en"
    return ""


def _inspect_ct2(folder: Path, roots: list[Path]) -> ModelCandidate:
    engine = _whisper_engine(folder)
    kind = "transcription" if engine == "large-v3" else "review" if engine == "medium.en" else "model"
    try:
        model = folder / "model.bin"
        if not _safe_file(model, roots):
            raise ValueError("The model file is absent, empty, or points outside the selected cache.")
        with model.open("rb") as handle:
            if b"WhisperSpec\x00" not in handle.read(64):
                raise ValueError("model.bin is not a recognized CTranslate2 Whisper file.")
        _small_json(folder / "config.json", roots)
        if not _safe_file(folder / "tokenizer.json", roots):
            raise ValueError("The offline tokenizer.json is missing or outside the selected cache.")
        if not engine:
            raise ValueError("CTranslate2 Whisper found, but its large-v3/medium.en identity is not established. Select a named compatible model folder.")
        return ModelCandidate(kind, str(folder), True,
                              "Compatible CTranslate2 Whisper layout; setup will validate model loading.", engine, "ctranslate2")
    except (OSError, ValueError, RuntimeError) as error:
        return ModelCandidate(kind, str(folder), False, str(error), engine, "ctranslate2")


def _inspect_separator(folder: Path, roots: list[Path]) -> ModelCandidate:
    try:
        spec = _small_json(folder / "spec.json", roots)
        if not isinstance(spec.get("args"), list) or not isinstance(spec.get("kwargs"), dict):
            raise ValueError("The separator specification is incomplete.")
        if "vocals" not in spec["kwargs"].get("sources", []):
            raise ValueError("The separator specification has no vocals output.")
        model = folder / "model.safetensors"
        if not _safe_file(model, roots):
            raise ValueError("The safetensors separator weights are missing or outside the selected cache.")
        with model.open("rb") as handle:
            length = int.from_bytes(handle.read(8), "little")
            if not 2 <= length <= _MAX_JSON or length + 8 >= model.stat().st_size:
                raise ValueError("Invalid or oversized safetensors header.")
            header = json.loads(handle.read(length))
        if not isinstance(header, dict) or not any(isinstance(v, dict) and "data_offsets" in v for v in header.values()):
            raise ValueError("The separator has no valid safetensors tensor metadata.")
        return ModelCandidate("separator", str(folder), True,
                              "Compatible HTDemucs specification and safetensors layout; no checkpoint was deserialized.", format="safetensors")
    except (OSError, ValueError, RuntimeError) as error:
        return ModelCandidate("separator", str(folder), False, str(error), format="safetensors")


def _inspect_alignment(path: Path, roots: list[Path]) -> ModelCandidate:
    try:
        if not _safe_file(path, roots):
            raise ValueError("Alignment checkpoint is missing, empty, or outside the selected cache.")
        with path.open("rb") as handle:
            magic = handle.read(4)
        if not (magic.startswith(b"PK\x03\x04") or magic.startswith(b"\x80")):
            raise ValueError("The file is not a recognized Torch checkpoint container.")
        return ModelCandidate("alignment", str(path), True,
                              "Known wav2vec2 checkpoint container; setup will validate loading without deserializing it during discovery.", format="torch-checkpoint")
    except (OSError, ValueError, RuntimeError) as error:
        return ModelCandidate("alignment", str(path), False, str(error), format="torch-checkpoint")


def scan_models(extra_roots: Iterable[str | Path] = (), *, settings=None,
                app_data: Path | None = None, cancelled: Callable[[], bool] | None = None,
                max_files: int = 10000, max_depth: int = 6, max_seconds: float = 8,
                include_defaults: bool = True, progress: Callable[[str], None] | None = None) -> DiscoveryResult:
    """Inspect exact model metadata, bounded by entries, depth, elapsed time and cancellation.

    Directory symlinks/reparse points are not traversed. HF snapshot file
    symlinks are accepted only when their target stays inside a selected root.
    ``include_defaults=False`` is useful for a targeted rescan or tests.
    """
    if max_files < 1 or max_depth < 0 or max_seconds <= 0:
        raise ValueError("Choose positive scan entry/time limits and a nonnegative depth.")
    result = DiscoveryResult()
    requested = (default_search_roots(app_data, settings) if include_defaults else []) + [Path(p) for p in extra_roots]
    trusted: list[Path] = []
    direct_files: list[Path] = []
    home = Path.home()
    for raw in requested:
        path = Path(raw).expanduser()
        if _network_path(path):
            result.warnings.append("Skipped a network model location. Choose a local model folder.")
            continue
        if any(part.lower().startswith("onedrive") for part in path.parts):
            result.warnings.append("Skipped OneDrive to avoid hydrating cloud files. Choose a local model copy.")
            continue
        try:
            path = path.resolve(strict=True)
            if _network_path(path) or any(part.lower().startswith("onedrive") for part in path.parts):
                result.warnings.append("Skipped a redirected network or OneDrive model location. Choose a local model copy.")
                continue
            if path == home or path.parent == path or (path.parent == home and path.name.lower() in _BROAD_HOME_FOLDERS):
                result.warnings.append("Skipped a broad personal/drive folder. Select the model or cache folder itself.")
                continue
            if path.is_file():
                direct_files.append(path)
                path = path.parent
            if path.is_dir() and path not in trusted:
                trusted.append(path)
        except (OSError, RuntimeError):
            continue
    # Keep broad selected caches ahead of their nested selected model folders.
    roots = [r for r in trusted if not any(r != other and r.is_relative_to(other) for other in trusted)]
    result.scanned_roots = [str(r) for r in roots]
    began = time.monotonic()
    seen_models: set[tuple[str, str]] = set()

    def halted() -> bool:
        if cancelled and cancelled():
            result.cancelled = True
            return True
        if result.entries_checked >= max_files or time.monotonic() - began >= max_seconds:
            result.truncated = True
            return True
        return False

    def add(candidate: ModelCandidate) -> None:
        key = (candidate.kind, os.path.normcase(candidate.path))
        if key not in seen_models:
            seen_models.add(key)
            result.models.append(candidate)

    for file in direct_files:
        if halted():
            break
        if file.name == CTC_CHECKPOINT:
            add(_inspect_alignment(file, roots))
    stack = [(root, 0) for root in reversed(roots)]
    while stack and not halted():
        folder, depth = stack.pop()
        if folder.name.lower() == "models" and folder.parent.name.lower() in {".ollama", "ollama"}:
            add(ModelCandidate("ollama-store", str(folder), False,
                               "Ollama text-model storage. Configure its local service separately; these weights are not Whisper audio models.", format="ollama"))
            continue
        if progress:
            progress(str(folder))
        entries = {}
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    if halted():
                        break
                    result.entries_checked += 1
                    entries[entry.name] = entry
        except OSError:
            result.warnings.append("A model folder was not readable; other selected folders were checked.")
            continue
        if "model.bin" in entries:
            add(_inspect_ct2(folder, roots))
        if "spec.json" in entries and "model.safetensors" in entries:
            add(_inspect_separator(folder, roots))
        if CTC_CHECKPOINT in entries:
            add(_inspect_alignment(folder / CTC_CHECKPOINT, roots))
        for name, entry in entries.items():
            if halted():
                break
            path = folder / name
            lower = name.lower()
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < max_depth and lower not in _IGNORED and not lower.startswith("onedrive"):
                        if lower == "blobs" and folder.name.startswith("models--"):
                            continue  # HF snapshots address these blobs directly.
                        # Windows junctions/reparse directories must not expand the scan.
                        if hasattr(path, "is_junction") and path.is_junction():
                            continue
                        if os.name == "nt" and getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400:
                            continue
                        stack.append((path, depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False) and not entry.is_symlink():
                    continue
                if lower.endswith((".gguf", ".ggml")) or (lower.endswith(".bin") and lower.startswith("ggml-")):
                    add(ModelCandidate("model", str(path), False,
                                       "GGUF/GGML weights require a different runtime; they cannot be reused as CTranslate2 Whisper.", format="gguf" if lower.endswith(".gguf") else "ggml"))
                elif lower.endswith(".pt") and any(token in lower for token in ("whisper", "large", "medium", "small", "base", "tiny")):
                    add(ModelCandidate("model", str(path), False,
                                       "OpenAI/PyTorch Whisper checkpoint; conversion to CTranslate2 is required before this backend can use it.", format="torch-whisper"))
                elif lower.endswith(".th") and ("demucs" in str(folder).lower() or re.fullmatch(r"[a-f0-9]{8}-[a-f0-9]{8}\.th", lower)):
                    add(ModelCandidate("model", str(path), False,
                                       "Original Demucs checkpoint; setup must convert it to the supported specification and safetensors format.", format="demucs-checkpoint"))
            except OSError:
                continue
    result.models.sort(key=lambda model: (not model.compatible, model.kind, model.path.lower()))
    if result.truncated:
        result.warnings.append("Discovery reached its scan limit. Add a specific model/cache folder to continue.")
    if cancelled and cancelled():
        result.cancelled = True
    return result


_PROBE_SCRIPT = """import importlib.metadata as m, importlib.util as u, json, sys
names = %r
packages = {}
for distribution, module in names.items():
    try:
        if u.find_spec(module) is not None: packages[distribution] = m.version(distribution)
    except (m.PackageNotFoundError, ValueError, ImportError): pass
print(json.dumps({'version': list(sys.version_info[:3]), 'packages': packages}))
""" % _PACKAGES


def runtime_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if getattr(sys, "frozen", False) and os.name != "nt":
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


def _start_probe(command: list[str]):
    # A frozen Windows GUI's DLL directory must not contaminate another Python.
    if getattr(sys, "frozen", False) and os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        setter = kernel.SetDllDirectoryW
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_int
        if not setter(None):
            raise OSError("Could not isolate the selected Python's DLL directory.")
        try:
            return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=runtime_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            setter(str(sys._MEIPASS))
    return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=runtime_environment())


def probe_python(path: str | Path, *, timeout: float = 5) -> PythonRuntime:
    """Inspect package metadata with isolated Python; never import Torch/models."""
    result = PythonRuntime(str(path))
    try:
        process = _start_probe([str(path), "-I", "-c", _PROBE_SCRIPT])
        try:
            output, error = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=2)
            result.reason = "The selected Python did not answer before the check timed out."
            return result
        if process.returncode or len(output) > 32768:
            result.reason = "The selected Python could not complete its isolated package check."
            return result
        data = json.loads(output.decode("utf-8"))
        version = data["version"]
        if not isinstance(version, list) or len(version) != 3 or not all(isinstance(v, int) for v in version):
            raise ValueError("Invalid version report.")
        result.version = ".".join(map(str, version))
        result.packages = {str(k): str(v) for k, v in data.get("packages", {}).items() if k in _PACKAGES}
        if not (version[0] == 3 and 11 <= version[1] <= 13):
            result.reason = "The lyric runtime needs Python 3.11–3.13; setup can create a separate compatible environment."
            return result
        missing = [name for name in _PACKAGES if name not in result.packages]
        problems = []
        for name in ("torch", "torchaudio"):
            if name in result.packages and not re.match(r"^2\.8(?:\.|\+|$)", result.packages[name]):
                problems.append(name + " needs the compatible 2.8 series")
        if "demucs" in result.packages and result.packages["demucs"] != "4.0.1":
            problems.append("demucs needs version 4.0.1")
        ranges = {"numpy": ((1, 26, 0), (3, 0, 0)), "soundfile": ((0, 12, 0), (1, 0, 0)),
                  "faster-whisper": ((1, 1, 0), (2, 0, 0)), "safetensors": ((0, 4, 0), (1, 0, 0)),
                  "num2words": ((0, 5, 13), (1, 0, 0))}
        for name, (minimum, maximum) in ranges.items():
            if name not in result.packages:
                continue
            match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", result.packages[name])
            version_number = tuple(int(value or 0) for value in match.groups()) if match else ()
            if not minimum <= version_number < maximum:
                problems.append(name + " has an incompatible package version")
        if missing:
            problems.append("missing " + ", ".join(missing))
        result.compatible = not problems
        result.reason = "; ".join(problems) if problems else "Compatible Python and lyric-package versions found; no heavy library or GPU was loaded."
    except (OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError):
        result.reason = "This executable did not report a compatible Python runtime."
    return result


def discover_python_paths(*, app_data: Path | None = None, settings=None,
                          extra_paths: Iterable[str | Path] = ()) -> list[str]:
    base = Path(app_data) if app_data else default_data_dir()
    candidates = list(extra_paths)
    values = _values(settings)
    if values.get("python_path"):
        candidates.insert(0, values["python_path"])
    candidates += [base / "model-runtime/Scripts/python.exe", base / "model-runtime/bin/python",
                   base / "runtime/Scripts/python.exe", base / "runtime/bin/python"]
    versions = base / "model-setup/versions"
    if versions.is_dir() and not _network_path(versions):
        try:
            with os.scandir(versions) as entries:
                for index, entry in enumerate(entries):
                    if index >= 128:
                        break
                    if not entry.is_dir(follow_symlinks=False) or entry.is_symlink():
                        continue
                    version_dir = Path(entry.path).resolve()
                    try:
                        manifest = _small_json(version_dir / "installation.complete.json", [version_dir])
                        chosen = manifest.get("settings", {}).get("python_path", "")
                        if isinstance(chosen, str) and chosen:
                            runtime = Path(chosen).resolve(strict=True)
                            if runtime.is_relative_to(version_dir):
                                candidates.append(runtime)
                    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
                        continue
        except OSError:
            pass
    if not getattr(sys, "frozen", False):
        candidates.append(sys.executable)
    for name in ("python3.13", "python3.12", "python3.11", "python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    # The launcher lists registered interpreters; it does not run their code.
    launcher = shutil.which("py") if os.name == "nt" else None
    if launcher:
        try:
            found = subprocess.run([launcher, "-0p"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=3,
                                   env=runtime_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
            if len(found.stdout) < 32768:
                candidates += re.findall(r"([A-Za-z]:\\[^\r\n]*?python(?:\d+(?:\.\d+)*)?\.exe)\s*$", found.stdout, re.MULTILINE | re.IGNORECASE)
        except (OSError, subprocess.SubprocessError):
            pass
    result = []
    seen = set()
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if _network_path(path) or "windowsapps" in str(path).lower():
            continue
        try:
            path = path.resolve(strict=True)
            key = os.path.normcase(str(path))
            if path.is_file() and key not in seen:
                seen.add(key)
                result.append(str(path))
        except (OSError, RuntimeError):
            continue
    return result[:12]


def normalize_ollama_url(url: str) -> str:
    """Accept only literal loopback/localhost, no credentials, proxy or redirects."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Use a local Ollama address such as http://127.0.0.1:11434.")
    host = parsed.hostname or ""
    if host.lower() == "localhost":
        host = "127.0.0.1"  # Never rely on external hostname resolution.
    try:
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("Ollama discovery is restricted to the local computer.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError("Ollama discovery is restricted to a valid local loopback address.") from error
    if not 1 <= port <= 65535:
        raise ValueError("Choose a valid local Ollama port.")
    rendered = f"[{address}]" if address.version == 6 else str(address)
    return f"{parsed.scheme}://{rendered}:{port}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def discover_ollama(url: str = "http://127.0.0.1:11434", *, timeout: float = 2) -> OllamaInfo:
    result = OllamaInfo(base_url=url)
    candidates = [shutil.which("ollama")]
    for variable in ("LOCALAPPDATA", "ProgramFiles"):
        if os.environ.get(variable):
            root = Path(os.environ[variable])
            candidates += [str(root / "Programs/Ollama/ollama.exe"), str(root / "Ollama/ollama.exe")]
    result.executable = next((str(p) for p in candidates if p and Path(p).is_file() and not _network_path(Path(p))), "")
    try:
        result.base_url = normalize_ollama_url(url)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        request = urllib.request.Request(result.base_url + "/api/tags", headers={"Accept": "application/json"})
        with opener.open(request, timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("The local service returned an oversized model list.")
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("The local service did not return an Ollama model list.")
        for model in data["models"][:1000]:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str):
                continue
            item = {"name": model["name"][:300]}
            details = model.get("details", {})
            if isinstance(details, dict):
                item["details"] = {k: str(details[k])[:100] for k in ("family", "parameter_size", "quantization_level") if k in details}
            if isinstance(model.get("size"), int):
                item["size"] = model["size"]
            result.models.append(item)
        result.reachable = True
        result.reason = "Local Ollama found. These text models assist text work; Whisper handles audio recognition."
    except ValueError as error:
        result.reason = str(error)
    except (OSError, urllib.error.URLError, TimeoutError):
        result.reason = "Ollama is not answering at this local address. Start your existing Ollama app or configure its port; nothing was installed or started."
    return result


def discover(extra_roots: Iterable[str | Path] = (), *, settings=None,
             app_data: Path | None = None, python_paths: Iterable[str | Path] = (),
             ollama_url: str = "http://127.0.0.1:11434", probe_runtimes: bool = True,
             probe_ollama: bool = True, cancelled: Callable[[], bool] | None = None,
             **scan_options) -> DiscoveryResult:
    result = scan_models(extra_roots, settings=settings, app_data=app_data,
                         cancelled=cancelled, **scan_options)
    if not result.cancelled and probe_runtimes:
        for path in discover_python_paths(app_data=app_data, settings=settings, extra_paths=python_paths):
            if cancelled and cancelled():
                result.cancelled = True
                break
            if scan_options.get("progress"):
                scan_options["progress"]("Checking Python runtime: " + path)
            result.runtimes.append(probe_python(path))
    if not result.cancelled and probe_ollama:
        if cancelled and cancelled():
            result.cancelled = True
        else:
            result.ollama = discover_ollama(ollama_url)
    if cancelled and cancelled():
        result.cancelled = True
    return result
