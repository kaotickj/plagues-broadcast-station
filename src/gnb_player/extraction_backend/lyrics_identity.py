"""Content identities for safe reuse; heavy hashes are memoized per worker only."""
from functools import lru_cache
from pathlib import Path
import hashlib
import importlib.metadata
import importlib.util
import os
import json
import platform
import subprocess
import sys
import time


def stamp(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


@lru_cache(maxsize=128)
def _digest(path, before):
    with path.open('rb') as stream:
        result = hashlib.file_digest(stream, 'sha256').hexdigest()
    if stamp(path) != before:
        raise RuntimeError('Recognition dependency changed while fingerprinting.')
    return result


def file_identity(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file(): raise ValueError('Recognition dependency is not a file.')
    before = stamp(path)
    # Some filesystems coalesce same-tick metadata updates. Do not memoize a
    # newly written dependency until its mtime/ctime have settled; otherwise a
    # same-size repair restoring mtime can hit an earlier hash within that tick.
    settled = time.time_ns() - max(before[3], before[4]) > 2_000_000_000
    sha = _digest(path, before) if settled else _digest.__wrapped__(path, before)
    result = {'path': str(path), 'bytes': before[2], 'sha256': sha}
    if stamp(path) != before: raise RuntimeError('Recognition dependency changed during cache lookup.')
    return result


def runtime_dependencies(binary):
    """Include linked libraries and separately loaded local GGML backends."""
    binary = Path(binary).resolve(strict=True)
    if sys.platform != 'linux':
        raise RuntimeError('Runtime library identity unavailable; recognition cache disabled.')
    process = subprocess.run(['ldd', str(binary)], capture_output=True, text=True, timeout=10)
    if process.returncode or 'not found' in process.stdout:
        raise RuntimeError('Cannot identify the recognizer shared libraries.')
    paths = set()
    for line in process.stdout.splitlines():
        value = line.split('=>', 1)[-1].strip().split(' ', 1)[0]
        if value.startswith('/'):
            paths.add(Path(value).resolve(strict=True))
    # ggml loads backend plugins dynamically, beyond ldd's dependency list.
    for base in (binary.parent, binary.parent.parent):
        for pattern in ('libggml*.so*', 'libwhisper*.so*', 'ggml*/**/libggml*.so*', 'src/libwhisper*.so*'):
            paths.update(p.resolve(strict=True) for p in base.glob(pattern) if p.is_file())
    icds = set()
    for folder in (Path('/usr/share/vulkan/icd.d'), Path('/etc/vulkan/icd.d')):
        icds.update(folder.glob('*.json'))
    for key in ('VK_ICD_FILENAMES', 'VK_DRIVER_FILES'):
        icds.update(Path(p) for p in os.environ.get(key, '').split(os.pathsep) if p)
    for icd in icds:
        data = json.loads(icd.read_text()); paths.add(icd.resolve(strict=True))
        library = Path(data['ICD']['library_path'])
        candidates = [library] if library.is_absolute() else [icd.parent/library] + [
            folder/library for folder in (Path('/usr/lib')/(platform.machine()+'-linux-gnu'),
                                          Path('/usr/lib64'), Path('/usr/lib'))]
        found = next((p for p in candidates if p.is_file()), None)
        if found is None: raise RuntimeError('Vulkan driver identity unavailable.')
        paths.add(found.resolve(strict=True))
    if not paths: raise RuntimeError('No runtime dependencies identified.')
    return sorted(paths)


def recognizer_identity(model_path, binary_path=None):
    model = Path(model_path).resolve(strict=True)
    if model.is_dir():
        files = sorted(p for p in model.iterdir() if p.is_file() and p.suffix in {'.bin', '.json', '.txt', '.model'})
        if not files or len(files) > 64 or not any(p.suffix == '.bin' for p in files):
            raise RuntimeError('Model directory identity is incomplete.')
    else: files = [model]
    result = {'version': 'recognizer-identity-1', 'model': [file_identity(p) for p in files]}
    if binary_path is not None:
        binary = Path(binary_path).resolve(strict=True)
        result['runtime'] = [file_identity(p) for p in sorted({binary, *runtime_dependencies(binary)})]
        result['backend'] = 'whisper.cpp-vulkan'
        result['environment'] = {key: os.environ.get(key, '') for key in (
            'GGML_VK_VISIBLE_DEVICES', 'VK_ICD_FILENAMES', 'VK_DRIVER_FILES', 'GGML_VULKAN_DISABLE_COOPMAT',
            'GGML_VULKAN_DISABLE_COOPMAT2', 'LD_LIBRARY_PATH')}
    else:
        runtime = []; versions = {}
        for name, distribution in [('faster_whisper', 'faster-whisper'), ('ctranslate2', 'ctranslate2')]:
            spec = importlib.util.find_spec(name)
            if spec is None or not spec.origin: raise RuntimeError('CPU runtime identity unavailable.')
            package = Path(spec.origin).parent
            versions[distribution] = importlib.metadata.version(distribution)
            runtime.extend(file_identity(p) for p in sorted(package.glob('*.py')))
            runtime.extend(file_identity(p) for p in sorted(package.glob('*.so')))
            runtime.extend(file_identity(p) for p in sorted(package.glob('*.pyd')))
            runtime.extend(file_identity(p) for p in sorted((package.parent/(name+'.libs')).glob('*.so*')))
        result.update(backend='faster-whisper', runtime=runtime, versions=versions)
    return result
