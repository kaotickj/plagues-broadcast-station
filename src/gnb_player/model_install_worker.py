#!/usr/bin/env python3
"""Explicit model installer worker. Stdlib controller; heavy work stays external."""
from __future__ import annotations

import argparse
import contextlib
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
from model_install import (InstallPlan, COMPONENTS, LABELS, MODEL_ASSETS, PYTHON_ASSET,
                           PYTHON_RELEASE, RUNTIME_REQUIREMENTS, atomic_json, describe_plan)

EVENTS = None
OWN_GROUP = False
CHILD = None
ALLOWED_HOSTS = {"www.python.org", "python.org", "huggingface.co", "dl.fbaipublicfiles.com", "download.pytorch.org"}
ALLOWED_SUFFIXES = (".huggingface.co", ".hf.co", ".xethub.hf.co")


class Cancelled(BaseException):
    pass


def emit(event: dict) -> None:
    line = json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
    if EVENTS is not None:
        EVENTS.write(line)
        EVENTS.flush()
    if sys.stdout is not None:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except (OSError, UnicodeError):
            pass


def progress(phase: str, message: str, fraction: float = 0) -> None:
    emit({"type": "progress", "phase": phase, "message": message, "fraction": max(0, min(1, fraction))})


def clean_error(error: BaseException) -> str:
    return re.sub(r"https?://\S+", "[download source]", str(error))[-1500:]


def cancel(signum, frame):
    if CHILD is not None and CHILD.poll() is None:
        stop_child(CHILD)
    if os.name != "nt" and OWN_GROUP:
        signal.signal(signum, signal.SIG_IGN)
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except ProcessLookupError:
            pass
    raise Cancelled()


def isolate():
    global OWN_GROUP
    if os.name != "nt":
        try:
            os.setsid()
        except PermissionError:
            pass
        OWN_GROUP = os.getpgrp() == os.getpid()
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)


def external_environment() -> dict:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('PIP_'):
            env.pop(key,None)
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "HF_HUB_DISABLE_TELEMETRY": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1", "PIP_CONFIG_FILE": os.devnull, "PYTHONNOUSERSITE": "1"})
    for key in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        env.pop(key, None)
    if getattr(sys, "frozen", False) and os.name != "nt":
        original = env.pop("LD_LIBRARY_PATH_ORIG", "")
        if original:
            env["LD_LIBRARY_PATH"] = original
        else:
            env.pop("LD_LIBRARY_PATH", None)
    return env


@contextlib.contextmanager
def external_dll_directory():
    if not (os.name == "nt" and getattr(sys, "frozen", False)):
        yield
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    setter = kernel.SetDllDirectoryW
    setter.argtypes = [ctypes.c_wchar_p]
    setter.restype = ctypes.c_int
    if not setter(None):
        raise OSError(ctypes.get_last_error(), "Could not isolate the model installer runtime.")
    try:
        yield
    finally:
        setter(str(sys._MEIPASS))


def stop_child(process) -> None:
    if process.poll() is not None:
        return
    if os.name != 'nt':
        try:
            os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        # The controller owns this process tree. No broad process-name kill.
        taskkill=str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/taskkill.exe')
        try:
            subprocess.run([taskkill,'/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
        except (OSError,subprocess.TimeoutExpired):
            if process.poll() is None:process.kill()


def run_command(command: list[str], phase: str, *, timeout: int = 3600, env: dict | None = None) -> None:
    global CHILD
    progress(phase, "Preparing the isolated runtime…" if phase == "runtime" else "Validating local lyric models…")
    with external_dll_directory():
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace", env=env or external_environment(),start_new_session=os.name!='nt')
    CHILD = process
    timed_out=threading.Event()
    def expire():
        timed_out.set();stop_child(process)
    timer=threading.Timer(timeout,expire);timer.daemon=True;timer.start()
    began = time.monotonic()
    lines = []
    try:
        for line in process.stdout:
            if time.monotonic() - began > timeout:
                raise TimeoutError("This setup step timed out. Existing models and settings were preserved.")
            line = line.strip()
            if not line:
                continue
            lines.append(line)
            lines = lines[-12:]
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("type") == "progress":
                    emit(event)
                    continue
            except ValueError:
                pass
            progress(phase, clean_error(Exception(line))[-400:])
        code = process.wait(timeout=30)
        if timed_out.is_set():
            raise TimeoutError('This setup step timed out. Existing models and settings were preserved.')
        if code:
            raise RuntimeError("Setup step failed: " + clean_error(RuntimeError("\n".join(lines[-6:]))))
    finally:
        timer.cancel()
        if process.poll() is None:
            stop_child(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        CHILD = None


def allowed_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").lower()
        return parsed.scheme == "https" and not parsed.username and not parsed.password and parsed.port in (None, 443) and (host in ALLOWED_HOSTS or host.endswith(ALLOWED_SUFFIXES))
    except ValueError:
        return False


class PublisherRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if not allowed_url(newurl):
            raise ValueError("The download redirected outside the approved publisher hosts.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def verify_download(path: Path, asset: dict) -> bool:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != asset["size"]:
        return False
    if asset.get("sha256"):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest() == asset["sha256"]
    if asset.get("git_blob"):
        digest = hashlib.sha1()
        digest.update(b"blob " + str(path.stat().st_size).encode() + b"\0")
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == asset["git_blob"]
    raise ValueError("This download has no pinned integrity check.")


def download(asset: dict, cache: Path, label: str) -> Path:
    if not allowed_url(asset["url"]):
        raise ValueError("Unapproved model download source.")
    if cache.is_symlink():
        raise ValueError('Choose a real download-cache folder, not a symbolic link.')
    cache.mkdir(parents=True, exist_ok=True)
    token = asset.get("sha256") or asset.get("git_blob")
    target = cache / (token + "-" + asset["name"])
    if target.is_symlink():
        raise ValueError("The download cache contains an unexpected symbolic link.")
    if verify_download(target, asset):
        progress("download", "Reusing verified download: " + label, 1)
        return target
    if target.exists():
        target.rename(target.with_name(target.name+'.unverified-'+uuid.uuid4().hex))
        progress('verifying','An unverified cached download was preserved separately; downloading a verified copy.')
    partial = target.with_name(target.name + ".part")
    if partial.is_symlink():
        raise ValueError("The partial download is not a regular file.")
    offset = partial.stat().st_size if partial.is_file() else 0
    if offset > asset["size"]:
        partial.rename(partial.with_name(partial.name+'.unverified-'+uuid.uuid4().hex));offset=0
    if offset == asset["size"] and verify_download(partial, asset):
        partial.replace(target)
        return target
    if offset==asset['size']:
        partial.rename(partial.with_name(partial.name+'.unverified-'+uuid.uuid4().hex));offset=0
    request = urllib.request.Request(asset["url"], headers={"User-Agent": "PlaguesBroadcastStation-ModelSetup/1", **({"Range": f"bytes={offset}-"} if offset else {})})
    opener = urllib.request.build_opener(PublisherRedirect())
    progress("download", "Downloading " + label + "; existing models are unchanged.", offset / asset["size"])
    with opener.open(request, timeout=40) as response:
        append = bool(offset and response.status == 206)
        if append and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
            raise ValueError("The publisher returned an unexpected resume range.")
        written = offset if append else 0
        last = time.monotonic()
        with partial.open("ab" if append else "wb") as file:
            while chunk := response.read(1024 * 1024):
                written += len(chunk)
                if written > asset["size"]:
                    raise ValueError("The download exceeded its expected size.")
                file.write(chunk)
                if time.monotonic() - last >= .5:
                    progress("download", f"Downloading {label}: {written / 1024**2:.0f} / {asset['size'] / 1024**2:.0f} MB", written / asset["size"])
                    last = time.monotonic()
    progress("verifying", "Checking publisher fingerprint: " + label)
    if not verify_download(partial, asset):
        raise ValueError("A downloaded file did not match the pinned publisher fingerprint; it was not installed.")
    partial.replace(target)
    return target


def check_python(python: str) -> None:
    code = "import sys,struct,json;print(json.dumps({'v':list(sys.version_info[:2]),'bits':struct.calcsize('P')*8}))"
    with external_dll_directory():
        result = subprocess.run([python, "-I", "-c", code], capture_output=True, text=True, encoding="utf-8", timeout=20, check=True, env=external_environment())
    data = json.loads(result.stdout)
    if data.get("bits") != 64 or not (3, 11) <= tuple(data.get("v", ())) <= (3, 13):
        raise ValueError("Choose 64-bit Python 3.11, 3.12 or 3.13 for this lyric runtime.")


def verify_component(path: Path, component: str) -> None:
    if component in ("transcription_model_path", "review_model_path"):
        needed = ["model.bin", "config.json", "tokenizer.json"]
        if not all((path / name).is_file() for name in needed) or not any((path / name).is_file() for name in ("vocabulary.json", "vocabulary.txt")):
            raise ValueError("The selected Whisper folder is not a complete CTranslate2 model.")
    elif component == "separator_model_path":
        if not (path / "spec.json").is_file() or not (path / "model.safetensors").is_file():
            raise ValueError("The selected vocal separator needs spec.json and model.safetensors.")
    elif component == "alignment_model_path":
        if not path.is_file() or path.stat().st_size < 1024:
            raise ValueError("The selected word-alignment checkpoint is unavailable.")


def python_bootstrap_command(executable: Path, target: Path) -> list[str]:
    return [str(executable), "/quiet", "InstallAllUsers=0", "TargetDir=" + str(target), "Include_launcher=0", "PrependPath=0",
            "Include_test=0", "Include_doc=0", "Include_tcltk=0", "AssociateFiles=0", "Shortcuts=0", "Include_pip=1"]


def install_python(cache: Path, version_dir: Path) -> str:
    if os.name != "nt":
        raise ValueError("Choose an existing compatible Python on Linux. Automatic Python bootstrap is for Windows x64.")
    import platform
    if platform.machine().lower() not in ("amd64", "x86_64"):
        raise ValueError("Automatic Python setup currently supports Windows x64. Select an existing compatible 64-bit Python.")
    # A late registry check avoids asking the PSF installer to repair or change
    # an existing same-version per-user Python that a folder scan missed.
    registered=registered_python()
    if registered:
        progress('runtime','Found an existing registered Python; using it only to create the isolated model runtime.')
        return registered
    executable = download({"name": f"python-{PYTHON_RELEASE}-amd64.exe", **PYTHON_ASSET}, cache, "official Python " + PYTHON_RELEASE)
    target = version_dir / "python-base"
    if target.exists():
        raise ValueError("The private Python target already exists; choose a new setup folder.")
    run_command(python_bootstrap_command(executable,target), "runtime", timeout=900)
    python = str(target / "python.exe")
    check_python(python)
    return python


def registered_python() -> str:
    if os.name!='nt':return ''
    import winreg
    for root in (winreg.HKEY_CURRENT_USER,winreg.HKEY_LOCAL_MACHINE):
        for version in ('3.13','3.12','3.11'):
            try:
                with winreg.OpenKey(root,'Software\\Python\\PythonCore\\'+version+'\\InstallPath',0,winreg.KEY_READ|winreg.KEY_WOW64_64KEY) as key:
                    try:python=str(winreg.QueryValueEx(key,'ExecutablePath')[0])
                    except OSError:python=str(Path(winreg.QueryValueEx(key,'')[0])/'python.exe')
                check_python(python)
                return python
            except (OSError,ValueError,subprocess.SubprocessError):
                continue
    return ''


def controller(plan: InstallPlan, *, reuse_only: bool=False) -> dict:
    plan = InstallPlan.from_dict(plan.to_dict())
    if reuse_only and (not plan.runtime_python or set(plan.reuse)!=set(COMPONENTS)):
        raise ValueError('Offline reuse requires an existing compatible runtime and all four model components. No download was attempted.')
    description = describe_plan(plan)
    if not description["enough_space"]:
        raise ValueError("There is not enough free space for the selected quality bundle and installation cache.")
    for key, value in plan.reuse.items():
        verify_component(Path(value).expanduser(), key)
    if not (plan.runtime_python or plan.base_python or plan.bootstrap_python):
        raise ValueError("Choose a compatible Python runtime or enable the Windows Python download.")
    root = Path(plan.install_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (root/'versions').is_symlink():
        raise ValueError('Choose a real managed-runtime folder, not a symbolic link.')
    version_dir = root / "versions" / ("quality-" + uuid.uuid4().hex)
    version_dir.mkdir(parents=True, mode=0o700)
    cache = root / "download-cache"
    runtime = plan.runtime_python
    if runtime:
        check_python(runtime)
        progress("runtime", "Reusing the selected model Python without changing its installed packages.")
    else:
        base = install_python(cache, version_dir) if plan.bootstrap_python else plan.base_python
        check_python(base)
        environment = version_dir / "runtime"
        run_command([base, "-I", "-m", "venv", str(environment)], "runtime", timeout=300)
        runtime = str(environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        # User-local environment only. Package indexes remain official PyPI and
        # the official CPU wheel index; no system Python packages are modified.
        run_command([runtime, "-I", "-m", "pip", "install", "--index-url", "https://download.pytorch.org/whl/cpu",
                     "torch==2.8.0", "torchaudio==2.8.0"], "runtime")
        constraints=version_dir/'runtime-constraints.txt';constraints.write_text('torch==2.8.0\ntorchaudio==2.8.0\n',encoding='utf-8')
        run_command([runtime, "-I", "-m", "pip", "install", "--index-url", "https://pypi.org/simple",'--constraint',str(constraints), *RUNTIME_REQUIREMENTS[2:]], "runtime")
    helper_request = version_dir / "prepare.json"
    helper_result = version_dir / "prepared.json"
    atomic_json(helper_request, {"version_dir": str(version_dir), "cache": str(cache), "reuse": plan.reuse})
    run_command([runtime, "-u", str(Path(__file__).resolve()), "--prepare-models", str(helper_request), "--result-file", str(helper_result),'--reuse-only' if reuse_only else '--allow-downloads'], "models", timeout=6 * 3600)
    if not helper_result.is_file():
        raise ValueError("Model validation did not produce a completed result; prior settings were preserved.")
    settings = json.loads(helper_result.read_text(encoding="utf-8"))
    settings.update({"python_path": runtime, "engine": "large-v3", "auto_review": True, "separate_vocals": True})
    if not plan.runtime_python:settings['use_gpu']=False
    atomic_json(version_dir / "installation.complete.json", {"version": 1, "settings": settings, "created_at": time.time(), "profile": description["profile"]})
    return settings


def prepare_models(request_file: Path, *, allow_downloads: bool=False) -> dict:
    """Executed only by the selected external model Python, never by the GUI."""
    request = json.loads(request_file.read_text(encoding="utf-8"))
    version_dir = Path(request["version_dir"])
    cache = Path(request["cache"])
    paths = dict(request.get("reuse", {}))
    if not allow_downloads and set(paths)!=set(COMPONENTS):
        raise ValueError('Offline reuse is missing a model component; no download was attempted.')
    models = version_dir / "models"
    torch_home = version_dir / "torch"
    os.environ.update({"HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "TOKENIZERS_PARALLELISM": "false", "TORCH_HOME": str(torch_home)})
    try:
        import torch
        import torchaudio.functional as alignment
        import demucs
        import safetensors.torch
        import num2words
        import soundfile
        from faster_whisper import WhisperModel
    except (ImportError,OSError) as error:
        hint=' Check that the selected Python contains the recommended matching model packages.'
        if os.name=='nt' and ('dll' in str(error).lower() or isinstance(error,OSError)):
            hint=' Windows could not load a native model library. Install the Microsoft Visual C++ 2015–2022 x64 runtime if it is missing, then retry; existing models are preserved.'
        raise ValueError('The selected model runtime could not be loaded.'+hint+' '+clean_error(error)) from error
    if not callable(getattr(alignment, "forced_align", None)):
        raise ValueError("This model Python lacks the required TorchAudio forced-alignment API. Create the isolated recommended runtime.")
    torch.set_num_threads(2)
    for component in COMPONENTS:
        if component in paths:
            verify_component(Path(paths[component]), component)
            progress("models", "Reusing " + LABELS[component] + " in its current location.", 1)
            continue
        assets = MODEL_ASSETS[component]
        downloads = [download(asset, cache, LABELS[component] + " / " + asset["name"]) for asset in assets]
        if component in ("transcription_model_path", "review_model_path"):
            destination = models / ("large-v3" if component == "transcription_model_path" else "medium.en")
            destination.mkdir(parents=True)
            for asset, downloaded in zip(assets, downloads):
                link_or_copy(downloaded, destination / asset["name"])
            paths[component] = str(destination)
        elif component == "separator_model_path":
            destination = models / "htdemucs"
            convert_separator(downloads[0], destination)
            paths[component] = str(destination)
        else:
            destination = torch_home / "hub/checkpoints" / assets[0]["name"]
            destination.parent.mkdir(parents=True)
            link_or_copy(downloads[0], destination)
            paths[component] = str(destination)
    # The expensive read validates that reused folders really load as the
    # expected backend, without starting GPU inference or any download.
    for component in ("transcription_model_path", "review_model_path"):
        progress("validating", "Loading " + LABELS[component] + " on CPU to verify compatibility…")
        model = WhisperModel(paths[component], device="cpu", compute_type="int8", cpu_threads=2, num_workers=1, local_files_only=True)
        verify_whisper_shape(model,component,(Path(paths[component])/'model.bin').stat().st_size)
        del model
        gc.collect()
    progress("validating", "Validating vocal separation and word alignment on CPU…")
    validate_separator(Path(paths["separator_model_path"]))
    if not verify_download(Path(paths['alignment_model_path']).resolve(),MODEL_ASSETS['alignment_model_path'][0]):
        raise ValueError('The alignment checkpoint does not match the supported official wav2vec2 model.')
    state = torch.load(paths["alignment_model_path"], map_location="cpu", weights_only=True)
    if not isinstance(state, dict) or not state or not all(isinstance(value, torch.Tensor) for value in state.values()):
        raise ValueError("The alignment checkpoint is not a compatible tensor state dictionary.")
    del state
    paths.update({"model_root": str(models), "torch_home": str(torch_home)})
    progress("complete", "Quality lyric models are ready and verified. Existing model folders were preserved.", 1)
    return paths


def verify_whisper_shape(model, component: str, file_size: int) -> None:
    large=component=='transcription_model_path'
    if model.model.n_mels!=(128 if large else 80) or bool(model.model.is_multilingual)!=large:
        raise ValueError('The selected Whisper model does not match the requested quality bundle.')
    minimum=1024**3 if large else 500*1024**2
    if file_size<minimum:
        raise ValueError('The selected Whisper weights are smaller than the requested quality model. No smaller fallback was installed.')


def link_or_copy(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise ValueError("Refusing to overwrite an existing model file.")
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def convert_separator(checkpoint: Path, destination: Path) -> None:
    import torch
    import numpy as np
    from fractions import Fraction
    from demucs.htdemucs import HTDemucs
    from safetensors.torch import save_file
    progress("converting", "Preparing the verified HTDemucs checkpoint for offline vocal separation…")
    # Only this pinned official checkpoint is accepted. Use the restricted
    # weights loader with known model and scalar metadata classes, never an
    # unrestricted pickle loader. The original training metadata used NumPy 1.
    if not verify_download(checkpoint, MODEL_ASSETS["separator_model_path"][0]):
        raise ValueError("The vocal separator checkpoint fingerprint is invalid.")
    scalar=np._core.multiarray.scalar if hasattr(np,'_core') else np.core.multiarray.scalar
    safe=[HTDemucs,Fraction,np.dtype,(scalar,'numpy.core.multiarray.scalar')]
    safe += [type(np.dtype(kind)) for kind in (np.float16,np.float32,np.float64,np.int32,np.int64)]
    with torch.serialization.safe_globals(safe):
        package = torch.load(checkpoint, map_location="cpu", weights_only=True)
    if not isinstance(package, dict) or package.get("klass") is not HTDemucs:
        raise ValueError("Unexpected official vocal separator checkpoint format.")
    spec = {"args": package["args"], "kwargs": package["kwargs"]}
    def numeric_metadata(value):
        if isinstance(value,Fraction):return float(value)
        if isinstance(value,np.generic):return value.item()
        raise TypeError('Unsupported separator constructor metadata.')
    spec=json.loads(json.dumps(spec,default=numeric_metadata,allow_nan=False))
    state = package.get("state")
    if not isinstance(state, dict) or not state or not all(isinstance(v, torch.Tensor) for v in state.values()):
        raise ValueError("The vocal separator is not a tensor-only checkpoint.")
    destination.mkdir(parents=True)
    atomic_json(destination / "spec.json", spec)
    save_file({k: value.contiguous() for k, value in state.items()}, str(destination / "model.safetensors"))
    del package, state
    gc.collect()
    validate_separator(destination)


def validate_separator(destination: Path) -> None:
    from demucs.htdemucs import HTDemucs
    from safetensors.torch import load_file
    spec = json.loads((destination / "spec.json").read_text(encoding="utf-8"))
    model = HTDemucs(*spec["args"], **spec["kwargs"])
    model.load_state_dict(load_file(str(destination / "model.safetensors")), strict=True)
    if "vocals" not in model.sources:
        raise ValueError("The separator has no vocals output.")
    del model
    gc.collect()


def main(argv=None) -> int:
    global EVENTS
    parser = argparse.ArgumentParser(description="Plague's Broadcast Station optional model setup")
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--events-file", type=Path)
    parser.add_argument("--result-file", type=Path, required=True)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument("--allow-downloads", action="store_true")
    mode.add_argument('--reuse-only',action='store_true')
    parser.add_argument("--prepare-models", type=Path)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.events_file:
        args.events_file.parent.mkdir(parents=True, exist_ok=True)
        if args.events_file.is_symlink():
            raise ValueError("The setup event log must be a regular local file.")
        EVENTS = args.events_file.open("w", encoding="utf-8")
        try:
            os.chmod(args.events_file, 0o600)
        except OSError:
            pass
    isolate()
    try:
        if not (args.allow_downloads or args.reuse_only):
            raise ValueError("Model setup needs an explicit Install action before it can download or change anything.")
        if args.prepare_models:
            result = prepare_models(args.prepare_models,allow_downloads=args.allow_downloads)
        else:
            if not args.plan_file or args.plan_file.stat().st_size > 1024 * 1024:
                raise ValueError("Choose a valid local installation plan.")
            result = controller(InstallPlan.from_dict(json.loads(args.plan_file.read_text(encoding="utf-8"))),reuse_only=args.reuse_only)
        atomic_json(args.result_file, result)
        emit({"type": "done", "result": result})
        return 0
    except Cancelled:
        emit({"type": "cancelled", "message": "Setup cancelled. Existing models and settings were preserved; verified downloads remain reusable."})
        return 130
    except Exception as error:
        emit({"type": "error", "message": clean_error(error)})
        return 1
    finally:
        if EVENTS is not None:
            EVENTS.close()
            EVENTS = None


if __name__ == "__main__":
    raise SystemExit(main())
