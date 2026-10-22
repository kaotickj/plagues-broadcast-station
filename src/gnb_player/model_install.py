"""Explicit, user-local installation plans for the optional lyric model runtime.

Planning and importing this module never access the network or change models.
The companion worker executes a saved plan only after an explicit install action.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import os
import shutil
import sys
import tempfile

COMPONENTS = ("transcription_model_path", "review_model_path", "separator_model_path", "alignment_model_path")
LABELS = {
    "transcription_model_path": "Whisper large-v3 transcription",
    "review_model_path": "Whisper medium.en independent review",
    "separator_model_path": "HTDemucs vocal separation",
    "alignment_model_path": "wav2vec2 word alignment",
}
PYTHON_RELEASE = "3.13.17"
PYTHON_ASSET = {
    "url": "https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe",
    "size": 29_941_280,
    "sha256": "5da012101ba2e17113a9a1cd901e9ce1441086f2b7ecb5de06ec94163899a190",
}
LARGE_REVISION = "edaa852ec7e145841d8ffdb056a99866b5f0a478"
REVIEW_REVISION = "a29b04bd15381511a9af671baec01072039215e3"


def _hf(repo: str, revision: str, name: str, size: int, *, sha256: str = "", git_blob: str = "") -> dict:
    return {"name": name, "url": f"https://huggingface.co/{repo}/resolve/{revision}/{name}",
            "size": size, "sha256": sha256, "git_blob": git_blob}


# Fixed official publisher revisions; large binary hashes are SHA256. Small
# repository files use the Git object IDs from that same immutable revision.
MODEL_ASSETS = {
    "transcription_model_path": [
        _hf("Systran/faster-whisper-large-v3", LARGE_REVISION, "model.bin", 3_087_284_237, sha256="69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
        _hf("Systran/faster-whisper-large-v3", LARGE_REVISION, "config.json", 2394, git_blob="75336feae814999bae6ccccdecf177639ffc6f9d"),
        _hf("Systran/faster-whisper-large-v3", LARGE_REVISION, "preprocessor_config.json", 340, git_blob="931c77a740890c46365c7ae0c9d350ba3cca908f"),
        _hf("Systran/faster-whisper-large-v3", LARGE_REVISION, "tokenizer.json", 2_480_617, git_blob="3a5e2ba63acdcac9a19ba56cf9bd27f185bfff61"),
        _hf("Systran/faster-whisper-large-v3", LARGE_REVISION, "vocabulary.json", 1_068_114, git_blob="0adcd01e7c237205d593b707e66dd5d7bc785d2d"),
    ],
    "review_model_path": [
        _hf("Systran/faster-whisper-medium.en", REVIEW_REVISION, "model.bin", 1_527_904_330, sha256="11b220779aea4c6f3ce9d2549c8a95ea869ed84066864b999531ef53e594fe5b"),
        _hf("Systran/faster-whisper-medium.en", REVIEW_REVISION, "config.json", 2643, git_blob="094bf1535b71e750d2c721808455dbb34237db98"),
        _hf("Systran/faster-whisper-medium.en", REVIEW_REVISION, "tokenizer.json", 2_128_466, git_blob="15d7bdf9ba25718ca2504eec6a8f02bc55af0a6a"),
        _hf("Systran/faster-whisper-medium.en", REVIEW_REVISION, "vocabulary.txt", 422_309, git_blob="ee695b8d3e3c10d488304e04468efec4ca27554a"),
    ],
    "separator_model_path": [{"name": "955717e8-8726e21a.th", "url": "https://dl.fbaipublicfiles.com/demucs/hybrid_transformer/955717e8-8726e21a.th", "size": 84_141_911,
                              "sha256": "8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4", "git_blob": ""}],
    "alignment_model_path": [{"name": "wav2vec2_fairseq_base_ls960_asr_ls960.pth", "url": "https://download.pytorch.org/torchaudio/models/wav2vec2_fairseq_base_ls960_asr_ls960.pth", "size": 377_664_473,
                              "sha256": "488fd4f16de84438ffc945334278c1b9fb9b7159a806c1080b16111a958c945d", "git_blob": ""}],
}
RUNTIME_REQUIREMENTS = ["torch==2.8.0", "torchaudio==2.8.0", "numpy>=1.26,<3", "soundfile>=0.12,<1",
                        "faster-whisper>=1.1,<2", "demucs==4.0.1", "safetensors>=0.4,<1", "num2words>=0.5.13,<1"]


@dataclass
class InstallPlan:
    install_dir: str
    base_python: str = ""
    runtime_python: str = ""
    reuse: dict[str, str] = field(default_factory=dict)
    bootstrap_python: bool = False
    version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InstallPlan":
        if not isinstance(data, dict) or data.get("version", 1) != 1:
            raise ValueError("Unsupported model installation plan.")
        allowed = cls.__dataclass_fields__
        if set(data) - set(allowed):
            raise ValueError("Unknown model installation setting.")
        plan = cls(**data)
        for name in ("install_dir", "base_python", "runtime_python"):
            value = getattr(plan, name)
            if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
                raise ValueError("Invalid model installation path.")
        if not plan.install_dir or not Path(plan.install_dir).expanduser().is_absolute():
            raise ValueError("Choose an absolute user-writable model installation folder.")
        folder = Path(plan.install_dir).expanduser()
        if folder == Path(folder.anchor):
            raise ValueError("Choose a folder below the drive root.")
        if not isinstance(plan.bootstrap_python, bool) or not isinstance(plan.reuse, dict) or set(plan.reuse) - set(COMPONENTS):
            raise ValueError("Invalid model reuse settings.")
        for key, value in plan.reuse.items():
            if not isinstance(value, str) or not value or len(value) > 4096 or "\0" in value or not Path(value).expanduser().is_absolute():
                raise ValueError("Choose an absolute existing model path.")
        if plan.runtime_python and (plan.base_python or plan.bootstrap_python):
            raise ValueError("Choose either an existing model runtime or a new isolated runtime.")
        if plan.base_python and plan.bootstrap_python:
            raise ValueError("Choose an existing base Python or download Python, not both.")
        return plan


def create_plan(install_dir: str | Path, base_python: str = "", runtime_python: str = "",
                reuse: dict[str, str] | None = None, bootstrap_python: bool = False) -> InstallPlan:
    return InstallPlan.from_dict(InstallPlan(str(Path(install_dir).expanduser().absolute()), base_python, runtime_python,
                                           dict(reuse or {}), bootstrap_python).to_dict())


def describe_plan(plan: InstallPlan) -> dict:
    """Estimate the explicit plan, without loading models or contacting services."""
    plan = InstallPlan.from_dict(plan.to_dict())
    missing = [key for key in COMPONENTS if key not in plan.reuse]
    model_bytes = sum(asset["size"] for key in missing for asset in MODEL_ASSETS[key])
    runtime_bytes = 0 if plan.runtime_python else 800 * 1024**2
    if plan.bootstrap_python:
        runtime_bytes += PYTHON_ASSET["size"]
    # Includes cache, converted separator, package extraction and installation
    # headroom. Existing models are referenced rather than copied.
    required = model_bytes * 2 + (3 * 1024**3 if not plan.runtime_python else 256 * 1024**2)
    parent = Path(plan.install_dir).expanduser()
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    try:
        free = shutil.disk_usage(parent).free
    except OSError:
        free = None
    steps = [{"component": key, "label": LABELS[key], "action": "reuse" if key in plan.reuse else "download",
              "path": plan.reuse.get(key, ""), "download_bytes": 0 if key in plan.reuse else sum(a["size"] for a in MODEL_ASSETS[key])}
             for key in COMPONENTS]
    return {"profile": "Quality: large-v3 + independent medium.en review", "engine": "large-v3", "steps": steps,
            "model_download_bytes": model_bytes, "estimated_download_bytes": model_bytes + runtime_bytes,
            "estimated_runtime_download_bytes": runtime_bytes, "required_free_bytes": required,
            "free_bytes": free, "enough_space": free is None or free >= required,
            "recommended_ram_gb": 16, "minimum_ram_gb": 8,
            "runtime_action": "reuse" if plan.runtime_python else ("download Python and create isolated runtime" if plan.bootstrap_python else "create isolated runtime"),
            "note": "Runtime download size is an estimate. Existing models and runtimes are never replaced. Ollama is optional text assistance and does not replace speech recognition."}


def save_plan(plan: InstallPlan, path: str | Path) -> Path:
    path = Path(path)
    atomic_json(path, InstallPlan.from_dict(plan.to_dict()).to_dict())
    return path


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, allow_nan=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def installer_command(plan_file: str | Path, events_file: str | Path, result_file: str | Path) -> list[str]:
    arguments = ["--plan-file", str(Path(plan_file).absolute()), "--events-file", str(Path(events_file).absolute()),
                 "--result-file", str(Path(result_file).absolute())]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--model-install-worker", *arguments]
    return [sys.executable, "-u", str(Path(__file__).with_name("model_install_worker.py")), *arguments]
