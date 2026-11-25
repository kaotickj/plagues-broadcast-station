"""Portable profiles and private session storage; broadcast keys are never saved."""
from pathlib import Path
import json, os, tempfile
from .library import Track

MAX_PROFILE_BYTES = 8 * 1024 * 1024

def _atomic_json(path: Path, data: dict) -> None:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.station-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def atomic_json(path: Path, data: dict) -> None:
    if Path(path).name in {'settings.json','session.json','broadcast-settings.json','lyrics-review-runtime.json'}:
        from .studio_settings import studio_lock
        with studio_lock(Path(path).parent):_atomic_json(path,data)
    else:_atomic_json(path,data)

def resolve_path(value: str, base: Path) -> Path:
    if not isinstance(value, str) or '\x00' in value: raise ValueError('Invalid media path in playlist.')
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()

def read_profile(path: Path) -> dict:
    path = Path(path)
    if path.stat().st_size > MAX_PROFILE_BYTES: raise ValueError('This playlist is too large.')
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or data.get('version', 1) != 1: raise ValueError('Unsupported broadcast playlist.')
    rows = data.get('tracks', [])
    if not isinstance(rows, list) or len(rows) > 20000: raise ValueError('Invalid playlist track list.')
    if not isinstance(data.get('backgrounds',[]),list) or len(data.get('backgrounds',[]))>20000:raise ValueError('Invalid backdrop list.')
    if not isinstance(data.get('lyrics',{}),dict) or len(data.get('lyrics',{}))>20000:raise ValueError('Invalid lyric-file map.')
    if not isinstance(data.get('settings',{}),dict):raise ValueError('Invalid station settings.')
    tracks = []
    for row in rows:
        if not isinstance(row, dict): raise ValueError('Invalid playlist track.')
        tracks.append(Track(path=resolve_path(row['path'], path.parent), title=str(row.get('title', '')),
                            artist=str(row.get('artist', '')), album=str(row.get('album', '')),
                            duration=max(0, float(row.get('duration', 0)))))
    backgrounds = [resolve_path(p, path.parent) for p in data.get('backgrounds', [])]
    lyrics = {str(resolve_path(k, path.parent)): str(resolve_path(v, path.parent)) for k, v in data.get('lyrics', {}).items()}
    return {**data, 'tracks': tracks, 'backgrounds': backgrounds, 'lyrics': lyrics}

def write_profile(path: Path, tracks: list[Track], backgrounds: list[Path], lyrics: dict,
                  settings: dict | None = None, name: str = 'My station') -> None:
    path = Path(path).resolve()
    def relative(p):
        try: return Path(os.path.relpath(Path(p).resolve(), path.parent)).as_posix()
        except ValueError: return Path(p).resolve().as_posix()
    data = {'version': 1, 'name': name, 'tracks': [{'path': relative(t.path), 'title': t.title, 'artist': t.artist,
             'album': t.album, 'duration': t.duration} for t in tracks], 'backgrounds': [relative(p) for p in backgrounds],
             'lyrics': {relative(k): relative(v) for k, v in lyrics.items()}, 'settings': settings or {}}
    atomic_json(path, data)

def starter_profile(data_dir: Path, roots: list[Path]) -> Path | None:
    """Use a supplied media pack once, then restore the operator's saved session."""
    if (Path(data_dir) / 'session.json').is_file(): return None
    for root in roots:
        candidate = Path(root) / 'media-packs/goodneighbor/profile.json'
        if candidate.is_file(): return candidate
    return None

def load_m3u(path: Path) -> list[Path]:
    if path.stat().st_size > MAX_PROFILE_BYTES: raise ValueError('This playlist is too large.')
    result = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'): continue
        if '://' in line: raise ValueError('Use local audio or video files in this playlist.')
        result.append(resolve_path(line, path.parent))
    return result

def save_m3u(path: Path, tracks: list[Track]) -> None:
    lines = ['#EXTM3U']
    for track in tracks:
        title = track.title.replace('\n', ' ').replace('\r', ' ')
        lines.extend([f'#EXTINF:{int(track.duration)},{title}', str(track.path)])
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def _save_model_preferences_locked(path, baseline, desired):
    """Commit changed model fields only; never replace another instance's edits."""
    from PySide6.QtCore import QLockFile
    path = Path(path)
    lock = QLockFile(str(path) + '.model-settings.lock')
    if not lock.tryLock(1000):
        raise OSError('Model settings are being saved by another instance. Try again.')
    try:
        try:
            settings = json.loads(path.read_text())
        except FileNotFoundError:
            settings = {}
        if not isinstance(settings, dict) or not isinstance(settings.get('extraction', {}), dict):
            raise ValueError('Existing model settings are invalid; repair them before saving.')
        latest = settings.get('extraction', {}).copy()
        changes = {key: value for key, value in desired.items()
                   if key not in baseline or value != baseline[key]}
        missing = object()
        conflicts = [key for key, value in changes.items()
                     if latest.get(key, missing) != baseline.get(key, missing)
                     and latest.get(key, missing) != value]
        if conflicts:
            raise ValueError('Model settings changed in another instance; reload before saving: ' + ', '.join(conflicts))
        latest.update(changes)
        from .extraction import ExtractionConfig
        ExtractionConfig.from_dict(latest)
        settings['extraction'] = latest
        atomic_json(path, settings)
        return latest
    finally:
        lock.unlock()


MODEL_BACKUP_KIND = 'plagues-broadcast-station-model-settings'

def export_model_preferences(path, values):
    """Export only validated model fields, never stream profiles or credentials."""
    from .extraction import ExtractionConfig
    config = ExtractionConfig.from_dict(values)
    atomic_json(Path(path), {'kind': MODEL_BACKUP_KIND, 'version': 1,
                             'extraction': config.to_dict()})


def import_model_preferences(path):
    """Read and validate a backup without touching live preferences."""
    from .extraction import ExtractionConfig
    with Path(path).open('rb') as handle:
        raw = handle.read(131073)
    if len(raw) > 131072:
        raise ValueError('Model settings backup is too large.')
    data = json.loads(raw)
    if (not isinstance(data, dict) or set(data) != {'kind', 'version', 'extraction'}
            or data.get('kind') != MODEL_BACKUP_KIND or type(data.get('version')) is not int
            or data['version'] != 1):
        raise ValueError('Choose a version 1 Broadcast Station model settings backup.')
    values = data['extraction']
    if not isinstance(values, dict) or set(values) != set(ExtractionConfig.__dataclass_fields__):
        raise ValueError('The backup has missing or unknown model settings.')
    return ExtractionConfig.from_dict(values)


def save_model_preferences(path, baseline, desired):
    from .studio_settings import studio_lock
    with studio_lock(Path(path).parent):
        return _save_model_preferences_locked(path, baseline, desired)
