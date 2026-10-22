"""Portable profiles and private session storage; broadcast keys are never saved."""
from pathlib import Path
import json, os, tempfile
from .library import Track

MAX_PROFILE_BYTES = 8 * 1024 * 1024

def atomic_json(path: Path, data: dict) -> None:
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.station-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

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
