"""Private, explicitly selected station snapshots and CAS-guarded data restore.

No models, media, native credential store, installer state or process control.
Planning/inspection are read-only. Mutations require approval and closed writers.
"""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

RESERVE_BYTES = 20 * 1024**3
MAX_FILE_BYTES = 16 * 1024**2
MAX_TOTAL_BYTES = 512 * 1024**2
MAX_FILES = 10000
_FILES = {'settings.json', 'session.json', 'broadcast-settings.json',
          'broadcast-profile.json', 'broadcast-profile-attempt.json', 'output-diagnostics.json'}
_TREES = {'lyrics-cache/lyrics', 'lyrics-cache/recognition-drafts',
          'lyrics-cache/failures', 'lyrics', 'history', 'references', 'playlists'}
_SUFFIXES = {'.json', '.lrc', '.srt', '.vtt', '.txt', '.m3u', '.m3u8'}


class SnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class Station:
    name: str
    data_dir: str | Path
    paths: tuple[str, ...]
    external_files: tuple[str | Path, ...] = ()


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False) + '\n').encode()


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _absolute(value):
    path = Path(value).absolute()
    if '..' in path.parts or any(c in str(path) for c in '\x00\n\r'):
        raise SnapshotError('Paths must be absolute plain paths without traversal or control characters.')
    return path


def _relative(value):
    path = Path(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or '.' in path.parts or any(c in str(path) for c in '\x00\n\r'):
        raise SnapshotError('Selected paths must be plain relative paths.')
    return path.as_posix()


@contextmanager
def _directory(path, *, create=False):
    path = _absolute(path)
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        raise SnapshotError('Secure directory-relative snapshots require the supported POSIX filesystem operations.')
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                opened = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                if not create: raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                opened = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = opened
        yield descriptor
    except OSError as error:
        raise SnapshotError('A directory is missing, inaccessible or linked; nothing was followed through a symlink.') from error
    finally:
        os.close(descriptor)


def _stamp(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _metadata(info, raw):
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw),
            'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns,
            'atime_ns': info.st_atime_ns, 'stamp': _stamp(info)}


def _read_at(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 0 <= before.st_size <= MAX_FILE_BYTES:
            raise SnapshotError('Selected content must be a bounded, unlinked regular metadata/lyric file.')
        chunks = []
        size = 0
        while True:
            block = os.read(fd, 65536)
            if not block: break
            size += len(block)
            if size > MAX_FILE_BYTES: raise SnapshotError('Selected metadata exceeds the file limit.')
            chunks.append(block)
        raw = b''.join(chunks)
        after = os.fstat(fd)
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _stamp(before) != _stamp(after) or _stamp(before) != _stamp(current) or len(raw) != before.st_size:
            raise SnapshotError('A selected file changed while reading; retry only after writers are closed.')
        return raw, _metadata(before, raw)
    finally:
        os.close(fd)


def _read(path):
    path = _absolute(path)
    with _directory(path.parent) as directory:
        return _read_at(directory, path.name)


def _exists_plain(path):
    path = _absolute(path)
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for index, part in enumerate(path.parts[1:]):
            try: info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError: return False
            if stat.S_ISLNK(info.st_mode): raise SnapshotError('Linked paths are refused.')
            if index == len(path.parts[1:]) - 1: return True
            if not stat.S_ISDIR(info.st_mode): raise SnapshotError('A path component is not a directory.')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor); descriptor = child
        return True
    finally:
        os.close(descriptor)


def _read_optional(path):
    if not _exists_plain(path): return None, None
    return _read(path)


def _semantic(metadata):
    if metadata is None: return None
    return {key: metadata[key] for key in ('sha256', 'size', 'mode', 'mtime_ns')}


def _space(path, required):
    if required > MAX_TOTAL_BYTES * 3:
        raise SnapshotError('Snapshot/restore budget exceeds the bounded station-data limit.')
    if shutil.disk_usage(path).free - required < RESERVE_BYTES:
        raise SnapshotError('The operation would cross the 20 GiB free-space reserve.')


def _normal_stations(stations):
    normalized = []
    roots = []
    names = set()
    for station in stations:
        if isinstance(station, Station): station = vars(station)
        if not isinstance(station, dict) or set(station) != {'name', 'data_dir', 'paths', 'external_files'}:
            raise SnapshotError('Explicit station name, data directory, selected paths and external files are required.')
        name = station['name']
        if not isinstance(name, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name) is None or name in names:
            raise SnapshotError('Station names must be unique plain identifiers.')
        names.add(name)
        root = _absolute(station['data_dir'])
        if any(root == prior or root.is_relative_to(prior) or prior.is_relative_to(root) for prior in roots):
            raise SnapshotError('Station data directories must be independent and cannot overlap.')
        with _directory(root): pass
        roots.append(root)
        selected = [_relative(value) for value in station['paths']]
        if not selected or len(set(selected)) != len(selected):
            raise SnapshotError('Select at least one unique metadata/lyric scope per station.')
        for value in selected:
            if value not in _FILES and value not in _TREES:
                raise SnapshotError('Unknown scope; models, runtime, media and credential stores are excluded.')
        if any(a != b and (Path(a).is_relative_to(Path(b)) or Path(b).is_relative_to(Path(a))) for a in selected for b in selected):
            raise SnapshotError('Selected scopes cannot overlap.')
        external = [str(_absolute(value)) for value in station['external_files']]
        if len(set(external)) != len(external): raise SnapshotError('External files cannot be duplicated.')
        for value in external:
            path = Path(value)
            if path.suffix.lower() not in {'.json', '.lrc', '.srt', '.vtt', '.txt'} or path.is_relative_to(root):
                raise SnapshotError('External lyric/reference files must be explicit separate metadata files.')
        normalized.append({'name': name, 'data_dir': str(root), 'paths': sorted(selected), 'external_files': sorted(external)})
    if not normalized: raise SnapshotError('At least one station selection is required.')
    all_external = [value for station in normalized for value in station['external_files']]
    if len(set(all_external)) != len(all_external) or any(Path(value).is_relative_to(root) for value in all_external for root in roots):
        raise SnapshotError('External files must not overlap any selected station data directory.')
    return normalized


def _known_lock(path, key):
    relative=key.split('/local/',1)[-1]
    if relative.startswith('lyrics-cache/recognition-drafts/'):
        return re.fullmatch(r'\.[a-f0-9]{64}\.json\.lock',path.name) is not None
    if relative.startswith(('lyrics-cache/lyrics/','lyrics/','history/')):
        return re.fullmatch(r'\.[^/]+\.json\.review\.lock',path.name) is not None
    return False


def _inventory(stations, *, missing=False):
    entries = {}
    directories = {}
    missing_scopes = []
    total = 0
    def file_entry(station, path, key):
        nonlocal total
        lock = _known_lock(path, key)
        if path.suffix.lower() not in _SUFFIXES and not lock: raise SnapshotError('A selected tree contains a model, media or unknown file type.')
        raw, metadata = _read(path)
        if lock and raw not in (b'',b'\0'):raise SnapshotError('A known lock placeholder contains unexpected content.')
        total += len(raw)
        if total > MAX_TOTAL_BYTES or len(entries) >= MAX_FILES: raise SnapshotError('Selected station state exceeds the bounded snapshot limit.')
        entries[key] = {'path': str(path), **metadata}
    def walk(station, path, relative):
        with _directory(path) as descriptor:
            info = os.fstat(descriptor)
            directories[station['name'] + '/' + relative] = {'path': str(path), 'mode': stat.S_IMODE(info.st_mode), 'mtime_ns': info.st_mtime_ns}
            children = sorted(os.listdir(descriptor))
            for name in children:
                item = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                child = path / name
                rel = (Path(relative) / name).as_posix() if relative else name
                if stat.S_ISDIR(item.st_mode): walk(station, child, rel)
                elif stat.S_ISREG(item.st_mode): file_entry(station, child, station['name'] + '/local/' + rel)
                else: raise SnapshotError('Symlinks and nonregular entries are refused in selected scopes.')
            if sorted(os.listdir(descriptor)) != children: raise SnapshotError('A selected directory changed while reading.')
    for station in stations:
        root = Path(station['data_dir'])
        parents = {root}
        for value in station['paths']:
            parent = (root / value).parent
            while parent.is_relative_to(root):
                parents.add(parent)
                if parent == root:break
                parent=parent.parent
        for parent in parents:
            if _exists_plain(parent):
                with _directory(parent) as fd:
                    info=os.fstat(fd)
                    relative = '' if parent == root else parent.relative_to(root).as_posix()
                    directories[station['name']+'/'+relative]={'path':str(parent),'mode':stat.S_IMODE(info.st_mode),'mtime_ns':info.st_mtime_ns}
        for selected in station['paths']:
            path = root / selected
            if missing and not _exists_plain(path):
                missing_scopes.append(station['name'] + '/' + selected)
                continue
            try:
                with _directory(path.parent) as parent:
                    info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except (FileNotFoundError, SnapshotError):
                if not missing: raise SnapshotError('An explicitly selected scope is missing or linked.')
                # Only genuinely absent paths are treated as absent; links fail.
                if path.exists() or path.is_symlink(): raise
                missing_scopes.append(station['name'] + '/' + selected)
                continue
            if selected in _FILES:
                if not stat.S_ISREG(info.st_mode): raise SnapshotError('A selected metadata file is not a regular file.')
                file_entry(station, path, station['name'] + '/local/' + selected)
            else:
                if not stat.S_ISDIR(info.st_mode): raise SnapshotError('A selected lyric tree is not a regular directory.')
                walk(station, path, selected)
        for index, value in enumerate(station['external_files']):
            path = Path(value)
            if missing and not _exists_plain(path):
                missing_scopes.append(station['name'] + '/external/' + str(index))
                continue
            try: file_entry(station, path, station['name'] + '/external/' + str(index))
            except (FileNotFoundError, SnapshotError):
                if not missing or _exists_plain(path): raise SnapshotError('An explicitly selected external file is missing or linked.')
                missing_scopes.append(station['name'] + '/external/' + str(index))
    return {'entries': entries, 'directories': directories, 'missing_scopes': sorted(missing_scopes), 'total_bytes': total}


def _mapped_entry(stations,key):
    for station in stations:
        prefix=station['name']+'/local/'
        if key.startswith(prefix):
            relative=_relative(key[len(prefix):])
            if not any(relative==scope or scope in _TREES and Path(relative).is_relative_to(Path(scope)) for scope in station['paths']):
                raise SnapshotError('Entry escapes its explicitly selected scope.')
            path=Path(station['data_dir'])/relative
            if path.suffix.lower() not in _SUFFIXES and not _known_lock(path,key):raise SnapshotError('Unknown metadata entry type.')
            return str(path)
        prefix=station['name']+'/external/'
        if key.startswith(prefix):
            index=key[len(prefix):]
            if not index.isdigit() or str(int(index))!=index or int(index)>=len(station['external_files']):raise SnapshotError('Invalid external entry mapping.')
            return station['external_files'][int(index)]
    raise SnapshotError('Unknown station entry.')


def _validate_catalog(stations,inventory,*,backups=False):
    if set(inventory)!={'entries','directories','missing_scopes','total_bytes'} or not isinstance(inventory['entries'],dict) or not isinstance(inventory['directories'],dict):
        raise SnapshotError('Invalid station inventory fields.')
    seen=set();total=0
    fields={'path','sha256','size','mode','mtime_ns','atime_ns','stamp'}|({'backup'} if backups else set())
    for key,entry in inventory['entries'].items():
        if not isinstance(key,str) or not isinstance(entry,dict) or set(entry)!=fields:raise SnapshotError('Invalid entry metadata fields.')
        mapped=_mapped_entry(stations,key)
        if entry['path']!=mapped or mapped in seen:raise SnapshotError('Duplicate or changed entry target.')
        seen.add(mapped)
        if not isinstance(entry['sha256'],str) or re.fullmatch('[a-f0-9]{64}',entry['sha256']) is None:raise SnapshotError('Invalid file checksum.')
        if (type(entry['size']) is not int or not 0<=entry['size']<=MAX_FILE_BYTES
                or type(entry['mode']) is not int or not 0<=entry['mode']<=0o777
                or any(type(entry[field]) is not int or entry[field]<0 for field in ('mtime_ns','atime_ns'))
                or not isinstance(entry['stamp'],list) or len(entry['stamp'])!=5 or any(type(value) is not int or value<0 for value in entry['stamp'])):
            raise SnapshotError('Invalid bounded file metadata.')
        total+=entry['size']
    if len(seen)>MAX_FILES or total>MAX_TOTAL_BYTES or type(inventory['total_bytes']) is not int or total!=inventory['total_bytes']:
        raise SnapshotError('Invalid bounded inventory total.')
    for key,entry in inventory['directories'].items():
        if not isinstance(key,str) or not isinstance(entry,dict) or set(entry)!={'path','mode','mtime_ns'}:raise SnapshotError('Invalid directory metadata fields.')
        matches=[station for station in stations if key.startswith(station['name']+'/')]
        if len(matches)!=1:raise SnapshotError('Unknown directory station.')
        station=matches[0];relative=key[len(station['name'])+1:]
        if relative:relative=_relative(relative)
        path=Path(station['data_dir'])/relative
        allowed=not relative or any(
            relative==scope or Path(scope).is_relative_to(Path(relative))
            or scope in _TREES and Path(relative).is_relative_to(Path(scope))
            for scope in station['paths'])
        if (not allowed or entry['path']!=str(path) or type(entry['mode']) is not int or not 0<=entry['mode']<=0o777
                or type(entry['mtime_ns']) is not int or entry['mtime_ns']<0):
            raise SnapshotError('Directory metadata escapes selected station scopes.')
    allowed_missing={station['name']+'/'+scope for station in stations for scope in station['paths']}
    allowed_missing.update(station['name']+'/external/'+str(index) for station in stations for index in range(len(station['external_files'])))
    if not isinstance(inventory['missing_scopes'],list) or len(set(inventory['missing_scopes']))!=len(inventory['missing_scopes']) or not set(inventory['missing_scopes'])<=allowed_missing:
        raise SnapshotError('Unknown missing station scopes.')


def _comparable(inventory):
    return {'entries': {key: {k: v for k, v in value.items() if k != 'atime_ns'} for key, value in inventory['entries'].items()},
            'directories': inventory['directories'], 'missing_scopes': inventory['missing_scopes']}


def make_plan(stations):
    stations = _normal_stations(stations)
    inventory = _inventory(stations)
    return {'schema': 'station-snapshot-plan-1', 'stations': stations, 'inventory': inventory,
            'inventory_sha256': _digest(_comparable(inventory))}


def _atomic(path, raw, *, mode=0o600):
    path = _absolute(path)
    _space(path.parent,len(raw)+4096)
    with _directory(path.parent) as directory:
        temporary = '.snapshot-' + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, mode, dir_fd=directory, follow_symlinks=False)
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try: os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError: pass


def _gates(approved, apps_closed):
    if approved is not True or apps_closed is not True:
        raise SnapshotError('Mutation requires explicit approval and confirmed closed station writers.')


def _no_overlap(destination, stations):
    for station in stations:
        root = Path(station['data_dir'])
        if destination.is_relative_to(root) or root.is_relative_to(destination):
            raise SnapshotError('Snapshot/work directories must not contain or be inside station data.')
        for value in station['external_files']:
            if Path(value).is_relative_to(destination) or destination == Path(value).parent:
                raise SnapshotError('Snapshot/work directory overlaps an external lyric file.')


def capture_snapshot(plan, destination, *, approved=False, apps_closed=False):
    _gates(approved, apps_closed)
    stations = _normal_stations(plan.get('stations', []))
    _validate_catalog(stations,plan['inventory'])
    if plan.get('schema') != 'station-snapshot-plan-1' or plan.get('inventory_sha256') != _digest(_comparable(plan['inventory'])):
        raise SnapshotError('Snapshot plan is invalid or was changed.')
    if _comparable(_inventory(stations)) != _comparable(plan['inventory']):
        raise SnapshotError('Station state changed since planning; no snapshot was started.')
    destination = _absolute(destination); _no_overlap(destination, stations)
    with _directory(destination.parent): pass
    _space(destination.parent, plan['inventory']['total_bytes'] + max(1024**2,len(_json(plan))*2+65536))
    with _directory(destination.parent) as directory: os.mkdir(destination.name, 0o700, dir_fd=directory)
    (destination / 'content').mkdir(mode=0o700)
    try:
        entries = {}
        for index, (key, entry) in enumerate(sorted(plan['inventory']['entries'].items())):
            _space(destination, entry['size'] + 1024**2)
            raw, current = _read(entry['path'])
            if {k:v for k,v in current.items() if k != 'atime_ns'} != {k:v for k,v in entry.items() if k not in ('path','atime_ns')}:
                raise SnapshotError('A selected file changed during snapshot; no complete marker was written.')
            backup = 'content/' + format(index, '05d') + '.bin'
            _atomic(destination / backup, raw)
            os.utime(destination / backup, ns=(entry['atime_ns'], entry['mtime_ns']), follow_symlinks=False)
            entries[key] = {**entry, 'backup': backup}
        if _comparable(_inventory(stations)) != _comparable(plan['inventory']):
            raise SnapshotError('Station state changed during snapshot; no complete marker was written.')
        manifest = {'schema': 'station-snapshot-1', 'status': 'complete', 'created_ns': time.time_ns(),
                    'stations': stations, 'entries': entries, 'directories': plan['inventory']['directories'],
                    'total_bytes': plan['inventory']['total_bytes']}
        raw = _json(manifest)
        if len(raw)>MAX_FILE_BYTES:raise SnapshotError('Snapshot metadata exceeds its supported bounded manifest size; no complete marker was written.')
        digest = hashlib.sha256(raw).hexdigest()
        _atomic(destination / 'manifest.json', raw)
        _atomic(destination / 'complete.json', _json({'schema': 'station-snapshot-complete-1', 'manifest_sha256': digest}))
        return {'path': str(destination), 'manifest_sha256': digest, 'total_bytes': manifest['total_bytes']}
    except BaseException:
        # Keep partial files for diagnosis; absence of complete.json forbids use.
        raise


def _load_json(path):
    raw, _ = _read(path)
    def unique(pairs):
        result={}
        for key,item in pairs:
            if key in result:raise SnapshotError('Duplicate JSON fields are refused.')
            result[key]=item
        return result
    try: value = json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:(_ for _ in ()).throw(SnapshotError('Nonfinite JSON values are refused.')))
    except (ValueError, UnicodeError) as error: raise SnapshotError('Snapshot/journal JSON is invalid.') from error
    return raw, value


def verify_snapshot(snapshot, *, expected_manifest_sha256):
    snapshot = _absolute(snapshot)
    raw, manifest = _load_json(snapshot / 'manifest.json')
    _, complete = _load_json(snapshot / 'complete.json')
    digest = hashlib.sha256(raw).hexdigest()
    if any(stat.S_IMODE((snapshot/name).stat().st_mode)!=0o600 for name in ('manifest.json','complete.json')):raise SnapshotError('Snapshot metadata must remain private.')
    if digest != expected_manifest_sha256 or complete != {'schema':'station-snapshot-complete-1','manifest_sha256':digest}:
        raise SnapshotError('Snapshot manifest does not match its pinned complete receipt.')
    if manifest.get('schema') != 'station-snapshot-1' or manifest.get('status') != 'complete':
        raise SnapshotError('Snapshot is incomplete or unsupported.')
    stations = _normal_stations(manifest.get('stations', []))
    if set(manifest)!={'schema','status','created_ns','stations','entries','directories','total_bytes'} or type(manifest['created_ns']) is not int or manifest['created_ns']<0:raise SnapshotError('Invalid snapshot manifest fields.')
    _validate_catalog(stations,{'entries':manifest['entries'],'directories':manifest['directories'],'missing_scopes':[],'total_bytes':manifest['total_bytes']},backups=True)
    if stat.S_IMODE(snapshot.stat().st_mode)!=0o700 or stat.S_IMODE((snapshot/'content').stat().st_mode)!=0o700:raise SnapshotError('Snapshot directories must remain private.')
    with _directory(snapshot) as fd:
        if set(os.listdir(fd))!={'content','manifest.json','complete.json'}:raise SnapshotError('Unknown snapshot contents.')
    allowed = {}
    for station in stations:
        for key, entry in manifest.get('entries', {}).items():
            if key.startswith(station['name'] + '/local/'):
                rel = _relative(key[len(station['name'] + '/local/'):])
                if not any(rel == scope or scope in _TREES and Path(rel).is_relative_to(Path(scope)) for scope in station['paths']):
                    raise SnapshotError('Snapshot entry escapes its selected scope.')
                allowed[key] = str(Path(station['data_dir']) / rel)
            elif key.startswith(station['name'] + '/external/'):
                index = key[len(station['name'] + '/external/'):]
                if not index.isdigit() or int(index) >= len(station['external_files']): raise SnapshotError('Invalid external snapshot entry.')
                allowed[key] = station['external_files'][int(index)]
    if set(allowed) != set(manifest.get('entries', {})): raise SnapshotError('Snapshot contains unknown station entries.')
    total = 0
    backups = set()
    for key, entry in manifest['entries'].items():
        if entry.get('path') != allowed[key] or Path(allowed[key]).suffix.lower() not in _SUFFIXES and not _known_lock(Path(allowed[key]),key):
            raise SnapshotError('Snapshot source mapping changed.')
        backup = entry.get('backup','')
        if re.fullmatch(r'content/[0-9]{5}\.bin', backup) is None or backup in backups:
            raise SnapshotError('Snapshot backup mapping is invalid.')
        backups.add(backup)
        content, metadata = _read(snapshot / backup)
        if _known_lock(Path(allowed[key]),key) and content not in (b'',b'\0'):raise SnapshotError('Known snapshot lock placeholder content is invalid.')
        if metadata['sha256'] != entry.get('sha256') or len(content) != entry.get('size') or stat.S_IMODE((snapshot / backup).stat().st_mode) != 0o600:
            raise SnapshotError('Snapshot content changed or lost private permissions.')
        if type(entry.get('mode')) is not int or not 0 <= entry['mode'] <= 0o777 or type(entry.get('mtime_ns')) is not int:
            raise SnapshotError('Snapshot file metadata is invalid.')
        total += len(content)
    if total != manifest.get('total_bytes') or total > MAX_TOTAL_BYTES or len(backups) > MAX_FILES:
        raise SnapshotError('Snapshot total/entry limit is invalid.')
    with _directory(snapshot/'content') as fd:
        if set(os.listdir(fd))!={Path(value).name for value in backups}:raise SnapshotError('Unknown snapshot content files.')
    return manifest


def inspect_current(snapshot, *, expected_manifest_sha256):
    manifest = verify_snapshot(snapshot, expected_manifest_sha256=expected_manifest_sha256)
    return _inventory(manifest['stations'], missing=True)


def _build_intent(manifest, snapshot, digest, current):
    _validate_catalog(manifest['stations'],current)
    operations={}
    for index,(key,desired) in enumerate(sorted(manifest['entries'].items())):
        before=current['entries'].get(key)
        operations[key]={'path':desired['path'],'before':None if before is None else {k:v for k,v in before.items() if k!='path'},
                         'after':{k:desired[k] for k in ('sha256','size','mode','mtime_ns','atime_ns')},
                         'number':format(index,'05d')}
    created=[entry['path'] for key,entry in manifest['directories'].items() if key not in current['directories']]
    return {'schema':'station-restore-intent-1','snapshot':str(_absolute(snapshot)),
            'manifest_sha256':digest,'expected_current':current,'operations':operations,
            'directories_before':current['directories'],'directories_after':manifest['directories'],
            'created_directories':sorted(created,key=lambda value:len(Path(value).parts))}


def plan_restore(snapshot, *, expected_manifest_sha256, expected_current):
    manifest=verify_snapshot(snapshot,expected_manifest_sha256=expected_manifest_sha256)
    current=_inventory(manifest['stations'],missing=True)
    if _comparable(current)!=_comparable(expected_current):
        raise SnapshotError('Current station state does not match the explicitly reviewed expected inventory.')
    # External file parents are explicit boundaries, not directories to create.
    for station in manifest['stations']:
        for value in station['external_files']:
            with _directory(Path(value).parent):pass
    intent=_build_intent(manifest,snapshot,expected_manifest_sha256,current)
    if len(_json(intent))>MAX_FILE_BYTES:raise SnapshotError('Restore intent exceeds the bounded journal size; nothing was changed.')
    return {'schema':'station-restore-plan-1','snapshot':str(_absolute(snapshot)),
            'manifest_sha256':expected_manifest_sha256,'expected_current':current,
            'expected_current_sha256':_digest(_comparable(current)),
            'intent':intent,'intent_sha256':hashlib.sha256(_json(intent)).hexdigest()}


def _write_target(path,raw,metadata,*,expected_current):
    path=_absolute(path)
    _space(path.parent,len(raw)+4096)
    with _directory(path.parent) as directory:
        temporary='.restore-'+uuid.uuid4().hex
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.chmod(temporary,metadata['mode'],dir_fd=directory,follow_symlinks=False)
            os.utime(temporary,ns=(metadata.get('atime_ns',metadata['mtime_ns']),metadata['mtime_ns']),dir_fd=directory,follow_symlinks=False)
            try:_,now=_read_at(directory,path.name)
            except FileNotFoundError:now=None
            if _semantic(now)!=_semantic(expected_current):raise SnapshotError('A target changed before atomic replacement.')
            if expected_current and 'stamp' in expected_current and now['stamp']!=expected_current['stamp']:
                raise SnapshotError('A target revision changed before atomic replacement.')
            opened=os.fstat(directory);current=path.parent.stat(follow_symlinks=False)
            if (opened.st_dev,opened.st_ino)!=(current.st_dev,current.st_ino):raise SnapshotError('A target directory changed before replacement.')
            os.replace(temporary,path.name,src_dir_fd=directory,dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:os.unlink(temporary,dir_fd=directory)
            except FileNotFoundError:pass


def _restore_directories(directories,allowed):
    for entry in sorted(directories.values(),key=lambda value:len(Path(value['path']).parts),reverse=True):
        path=Path(entry['path'])
        if not _exists_plain(path):continue
        with _directory(path) as descriptor:
            observed=os.fstat(descriptor)
            modes={value['mode'] for value in allowed.get(str(path),[])}
            if stat.S_IMODE(observed.st_mode) not in modes:raise SnapshotError('A directory has unknown/newer permissions; metadata was not replaced.')
            current_path=path.stat(follow_symlinks=False)
            if (observed.st_dev,observed.st_ino,observed.st_mode,observed.st_mtime_ns)!=(current_path.st_dev,current_path.st_ino,current_path.st_mode,current_path.st_mtime_ns):raise SnapshotError('A directory changed before metadata replacement.')
            os.fchmod(descriptor,entry['mode'])
            current=os.fstat(descriptor)
            os.utime(descriptor,ns=(current.st_atime_ns,entry['mtime_ns']))


def _check_progress(intent,stations):
    current=_inventory(stations,missing=True)
    before=intent['expected_current']
    operations=intent['operations']
    allowed=set(before['entries'])|set(operations)
    if not set(current['entries'])<=allowed or not (set(before['entries'])-set(operations))<=set(current['entries']):
        raise SnapshotError('Unknown/newer station files appeared or disappeared; nothing was recovered.')
    for key,entry in current['entries'].items():
        if key in operations:
            operation=operations[key]
            if _semantic(entry) not in (_semantic(operation['before']),_semantic(operation['after'])):
                raise SnapshotError('A restore target now contains an unknown/newer edit. Nothing was recovered.')
        elif {k:v for k,v in entry.items() if k!='atime_ns'}!={k:v for k,v in before['entries'][key].items() if k!='atime_ns'}:
            raise SnapshotError('A preserved newer station file changed; nothing was recovered.')
    for key,operation in operations.items():
        if key not in current['entries'] and operation['before'] is not None:
            raise SnapshotError('A restore target disappeared; nothing was recovered.')
    allowed_dirs=set(intent['directories_before'])|set(intent['directories_after'])
    if not set(current['directories'])<=allowed_dirs or not set(intent['directories_before'])<=set(current['directories']):
        raise SnapshotError('Unknown/newer station directories changed; nothing was recovered.')
    for key,entry in current['directories'].items():
        candidates=[catalog[key] for catalog in (intent['directories_before'],intent['directories_after']) if key in catalog]
        modes={value['mode'] for value in candidates}
        if entry['path'] in intent['created_directories']:modes.add(0o700)
        if entry['mode'] not in modes:
            raise SnapshotError('A station directory has unknown/newer permissions; nothing was recovered.')
    return current


def _directory_allowed(intent):
    result={}
    for catalog in (intent['directories_before'],intent['directories_after']):
        for value in catalog.values():result.setdefault(value['path'],[]).append(value)
    for path in intent['created_directories']:result.setdefault(path,[]).append({'path':path,'mode':0o700})
    return result


def restore_snapshot(plan,work_dir,*,approved=False,apps_closed=False):
    _gates(approved,apps_closed)
    if plan.get('schema')!='station-restore-plan-1' or plan.get('expected_current_sha256')!=_digest(_comparable(plan['expected_current'])):
        raise SnapshotError('Restore plan was changed or is unsupported.')
    manifest=verify_snapshot(plan['snapshot'],expected_manifest_sha256=plan['manifest_sha256'])
    intent=_build_intent(manifest,plan['snapshot'],plan['manifest_sha256'],plan['expected_current'])
    if plan.get('intent')!=intent or plan.get('intent_sha256')!=hashlib.sha256(_json(intent)).hexdigest():
        raise SnapshotError('Restore intent does not match the pinned plan.')
    current=_inventory(manifest['stations'],missing=True)
    if _comparable(current)!=_comparable(plan['expected_current']):raise SnapshotError('Newer station state appeared after restore planning; nothing was overwritten.')
    work=_absolute(work_dir);_no_overlap(work,manifest['stations'])
    if work.is_relative_to(_absolute(plan['snapshot'])) or _absolute(plan['snapshot']).is_relative_to(work):raise SnapshotError('Restore journal and snapshot directories cannot overlap.')
    _space(work.parent,manifest['total_bytes']+current['total_bytes']+max(1024**2,len(_json(intent))*2+65536))
    for operation in intent['operations'].values():
        parent=Path(operation['path']).parent
        while not _exists_plain(parent):parent=parent.parent
        _space(parent,operation['after']['size']+4096)
    with _directory(work.parent) as directory:os.mkdir(work.name,0o700,dir_fd=directory)
    (work/'before').mkdir(mode=0o700);(work/'after').mkdir(mode=0o700)
    for key,operation in intent['operations'].items():
        before_raw,before_meta=_read_optional(operation['path'])
        if _semantic(before_meta)!=_semantic(operation['before']) or before_meta is not None and before_meta['stamp']!=operation['before']['stamp']:
            raise SnapshotError('A restore target changed during staging; nothing was committed.')
        if before_raw is not None:_atomic(work/'before'/operation['number'],before_raw)
        desired_raw,_=_read(Path(plan['snapshot'])/manifest['entries'][key]['backup'])
        _atomic(work/'after'/operation['number'],desired_raw)
    # The caller pins this immutable intent hash before mutations. Progress
    # journals cannot redefine before data, operation numbers or directories.
    _atomic(work/'intent.json',_json(intent))
    journal={'schema':'station-restore-journal-1','intent_sha256':plan['intent_sha256'],
             'status':'prepared','committed':[],'pending':None}
    journal_path=work/'journal.json';_atomic(journal_path,_json(journal))
    try:
        _check_progress(intent,manifest['stations'])
        for key,operation in intent['operations'].items():
            _space(work,1024**2)
            _,now=_read_optional(operation['path'])
            if _semantic(now)!=_semantic(operation['before']) or now is not None and now['stamp']!=operation['before']['stamp']:
                raise SnapshotError('A target changed before commit. Keep newer human edits and review the recovery journal.')
            journal['pending']=key;journal['status']='restoring';_atomic(journal_path,_json(journal))
            parent=Path(operation['path']).parent
            allowed=set(intent['created_directories'])
            missing=[];cursor=parent
            while not _exists_plain(cursor):missing.append(cursor);cursor=cursor.parent
            if any(str(path) not in allowed for path in missing):raise SnapshotError('An unplanned directory is missing; no directory was created.')
            with _directory(parent,create=True):pass
            raw,_=_read(work/'after'/operation['number'])
            if hashlib.sha256(raw).hexdigest()!=operation['after']['sha256']:raise SnapshotError('Staged restore data changed; use the recovery journal.')
            _write_target(operation['path'],raw,operation['after'],expected_current=operation['before'])
            journal['committed'].append(key);journal['pending']=None;_atomic(journal_path,_json(journal))
        _check_progress(intent,manifest['stations'])
        _restore_directories(intent['directories_after'],_directory_allowed(intent))
        journal['status']='complete';_atomic(journal_path,_json(journal))
        return {'journal':str(journal_path),'intent_sha256':plan['intent_sha256'],'manifest_sha256':plan['manifest_sha256'],
                'status':'complete','restored_files':len(intent['operations'])}
    except BaseException as error:
        failure=SnapshotError('Restore did not complete. Original/staged data and pinned intent are preserved at '+str(journal_path))
        failure.journal_path=str(journal_path);failure.intent_sha256=plan['intent_sha256']
        raise failure from error


def recover_restore(journal_path,*,expected_manifest_sha256,expected_intent_sha256,approved=False,apps_closed=False):
    """Recover only an original caller-pinned intent and known before/after data."""
    _gates(approved,apps_closed)
    journal_path=_absolute(journal_path);work=journal_path.parent
    if stat.S_IMODE(work.stat().st_mode)!=0o700 or any(stat.S_IMODE((work/name).stat().st_mode)!=(0o700 if name in ('before','after') else 0o600) for name in ('before','after','intent.json','journal.json')):raise SnapshotError('Recovery state must remain private.')
    raw,intent=_load_json(work/'intent.json')
    if hashlib.sha256(raw).hexdigest()!=expected_intent_sha256 or intent.get('manifest_sha256')!=expected_manifest_sha256:
        raise SnapshotError('Recovery intent does not match its explicitly pinned receipt.')
    manifest=verify_snapshot(intent['snapshot'],expected_manifest_sha256=expected_manifest_sha256)
    expected=_build_intent(manifest,intent['snapshot'],expected_manifest_sha256,intent['expected_current'])
    if intent!=expected:raise SnapshotError('Recovery intent contains unknown mappings or metadata.')
    _,journal=_load_json(journal_path)
    if set(journal)!={'schema','intent_sha256','status','committed','pending'} or journal['schema']!='station-restore-journal-1' or journal['intent_sha256']!=expected_intent_sha256:
        raise SnapshotError('Unknown or corrupted recovery journal.')
    keys=list(intent['operations'])
    committed=journal['committed']
    if (not isinstance(committed,list) or committed!=keys[:len(committed)]
            or journal['pending'] not in (None,keys[len(committed)] if len(committed)<len(keys) else None)
            or journal['status'] not in {'prepared','restoring','complete','recovering','recovered'}):
        raise SnapshotError('Recovery journal progress is invalid.')
    _check_progress(intent,manifest['stations'])
    for operation in intent['operations'].values():
        _,current=_read_optional(operation['path'])
        if _semantic(current) not in (_semantic(operation['before']),_semantic(operation['after'])):
            raise SnapshotError('A restore target now contains an unknown/newer edit. Nothing was recovered.')
        if operation['before'] is not None:
            raw,_=_read(work/'before'/operation['number'])
            if hashlib.sha256(raw).hexdigest()!=operation['before']['sha256']:raise SnapshotError('Recovery backup changed.')
    _space(work,manifest['total_bytes']+1024**2)
    journal['status']='recovering';_atomic(journal_path,_json(journal))
    for operation in reversed(list(intent['operations'].values())):
        _,current=_read_optional(operation['path'])
        if _semantic(current)==_semantic(operation['before']):continue
        if _semantic(current)!=_semantic(operation['after']):raise SnapshotError('A target changed during recovery; stop and retain the journal.')
        if operation['before'] is None:
            path=Path(operation['path'])
            with _directory(path.parent) as directory:
                _,again=_read_at(directory,path.name)
                if _semantic(again)!=_semantic(operation['after']):raise SnapshotError('A target changed before recovery removal.')
                os.unlink(path.name,dir_fd=directory);os.fsync(directory)
        else:
            raw,_=_read(work/'before'/operation['number'])
            _write_target(operation['path'],raw,operation['before'],expected_current=operation['after'])
    for value in reversed(intent['created_directories']):
        path=Path(value)
        if not _exists_plain(path):continue
        try:
            with _directory(path.parent) as directory:os.rmdir(path.name,dir_fd=directory)
        except OSError:pass  # Retain a now-nonempty directory; never remove data.
    _restore_directories(intent['directories_before'],_directory_allowed(intent))
    journal['status']='recovered';_atomic(journal_path,_json(journal))
    return {'status':'recovered','journal':str(journal_path),'intent_sha256':expected_intent_sha256}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','capture','inspect-current','restore-plan','restore','recover'])
    parser.add_argument('--input',type=Path,required=True,help='Explicit JSON selection, plan, or pinned snapshot reference')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--expected-current',type=Path)
    parser.add_argument('--approved',action='store_true')
    parser.add_argument('--apps-closed',action='store_true')
    args=parser.parse_args(argv)
    try:
        raw,value=_load_json(args.input)
        if args.action=='plan':result=make_plan(value['stations'])
        elif args.action=='capture':result=capture_snapshot(value,args.output,approved=args.approved,apps_closed=args.apps_closed)
        elif args.action=='inspect-current':result=inspect_current(value['path'],expected_manifest_sha256=value['manifest_sha256'])
        elif args.action=='restore-plan':
            if args.expected_current is None:raise SnapshotError('An explicitly reviewed current inventory is required.')
            _,expected=_load_json(args.expected_current)
            result=plan_restore(value['path'],expected_manifest_sha256=value['manifest_sha256'],expected_current=expected)
        elif args.action=='restore':result=restore_snapshot(value,args.output,approved=args.approved,apps_closed=args.apps_closed)
        else:result=recover_restore(value['journal'],expected_manifest_sha256=value['manifest_sha256'],expected_intent_sha256=value['intent_sha256'],approved=args.approved,apps_closed=args.apps_closed)
        print(json.dumps(result,sort_keys=True,indent=2))
        return 0
    except (SnapshotError,OSError,KeyError,TypeError) as error:
        parser.exit(1,'Snapshot: '+str(error)+'\n')


if __name__=='__main__':raise SystemExit(main())
