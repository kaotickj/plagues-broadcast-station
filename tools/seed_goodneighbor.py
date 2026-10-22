#!/usr/bin/env python3
"""Build a portable media pack from an explicitly selected local collection.

The album-plan JSON contains ``albums`` with ``title`` and ``tracks`` lists.
Each track supplies ``source``, ``title``, ``duration`` and ``audio_hash`` (the
SHA256 hash of its encoded audio packets). Sources may be relative to the plan.
An optional lyric snapshot maps recording IDs under ``lyrics`` to documents and
their verified ``audio_hash``; tracks reference those IDs with ``lyric_matches``.
No collection, application deployment or external service is discovered.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
import zipfile

PACKET = re.compile(r'^SHA256=([a-fA-F0-9]{64})$')
MEDIA_BACKGROUND = {'.webp', '.png', '.jpg', '.jpeg', '.avif', '.mp4', '.webm'}


def file_hash(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def packet_hash(path: Path) -> str:
    """Hash encoded audio packets, excluding ID3/artwork; no audio inference."""
    process = subprocess.run(
        ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe',
         '-i', str(path), '-map', '0:a:0', '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'],
        check=True, capture_output=True, timeout=90,
    )
    value = process.stdout.decode('ascii').strip()
    if not PACKET.fullmatch(value):
        raise ValueError('The recording did not produce a valid audio packet hash.')
    return value


def verify_packet_identity(expected: str, actual: str) -> None:
    if not PACKET.fullmatch(expected) or not PACKET.fullmatch(actual) or expected.lower() != actual.lower():
        raise ValueError('Audio payload mismatch: lyrics must not be attached by title alone.')


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def safe_copy(source: Path, destination: Path) -> tuple[str, bool]:
    if source.is_symlink() or not source.is_file():
        raise ValueError('Use a regular original recording, not a symlink.')
    before = (source.stat().st_size, source.stat().st_mtime_ns)
    digest = file_hash(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise ValueError('Refusing to replace a symlink in the media pack.')
    if destination.exists():
        if file_hash(destination) != digest:
            raise ValueError('An existing pack file differs; keep it and choose a new pack folder.')
        return digest, False
    temporary = destination.with_name('.' + destination.name + '.' + uuid.uuid4().hex)
    try:
        shutil.copyfile(source, temporary)
        if file_hash(temporary) != digest or before != (source.stat().st_size, source.stat().st_mtime_ns):
            raise ValueError('The source changed while it was copied.')
        temporary.chmod(0o644)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return digest, True


def prepare_imported_lyrics(entry: dict, copied_track: Path, copied_packet_hash: str, title: str) -> dict:
    """Retarget selected timed lyrics only after encoded-audio verification."""
    verify_packet_identity(entry['audio_hash'], copied_packet_hash)
    document = deepcopy(entry['document'])
    old_hash = document.get('source_sha256') or entry.get('source_sha256', '')
    document['source_sha256'] = file_hash(copied_track)
    document['source_size'] = copied_track.stat().st_size
    document['source_mtime'] = int(copied_track.stat().st_mtime)
    document['title'] = title
    document['provenance'] = {
        'source': entry.get('source_label', 'Imported timed lyrics'), 'recording_id': document.get('song_id', ''),
        'original_source_sha256': old_hash, 'original_source_name': entry.get('source_name', ''),
        'verified_audio_packet_hash': copied_packet_hash,
        'publication_method': entry.get('publication_method', 'human'), 'imported_at': int(time.time()),
    }
    return document


def relative_profile(profile: dict, source_parent: Path, target_parent: Path) -> dict:
    result = deepcopy(profile)
    def convert(value):
        path = Path(value)
        return Path(os.path.relpath(path if path.is_absolute() else source_parent/path, target_parent)).as_posix()
    for track in result['tracks']:
        track['path'] = convert(track['path'])
    result['backgrounds'] = [convert(value) for value in result['backgrounds']]
    result['lyrics'] = {convert(key): convert(value) for key, value in result['lyrics'].items()}
    result['lyrics_ready'] = [convert(value) for value in result['lyrics_ready']]
    return result


def make_pack(plan: Path, snapshot: Path | None, pack: Path, backgrounds: Path | None,
              user_profile: Path | None, *, archive: bool = False, workers: int = 2,
              name: str = 'My Station', tagline: str = 'Original music. Stay awhile.') -> dict:
    albums = json.loads(plan.read_text(encoding='utf-8-sig'))['albums']
    published = json.loads(snapshot.read_text(encoding='utf-8-sig'))['lyrics'] if snapshot else {}
    pack.mkdir(parents=True, exist_ok=True)
    if pack.is_symlink():
        raise ValueError('Choose a real media-pack folder.')
    numbered = [(index, album, track) for index, (album, track) in enumerate(((a, t) for a in albums for t in a['tracks']), 1)]
    def copy_recording(row):
        index, album, track = row
        source = Path(track['source']).expanduser()
        if not source.is_absolute():
            source = plan.parent / source
        slug = re.sub(r'[^a-z0-9]+', '-', str(track.get('slug') or track['title']).casefold()).strip('-')[:100] or 'recording'
        filename = f"recording-{index:04d}-{slug}{source.suffix.lower()}"
        target = pack / 'music' / filename
        original_packets = packet_hash(source)
        verify_packet_identity(track['audio_hash'], original_packets)
        digest, copied = safe_copy(source, target)
        copied_packets = packet_hash(target)
        verify_packet_identity(original_packets, copied_packets)
        metadata = {'path': target.relative_to(pack).as_posix(), 'title': track['title'], 'artist': track.get('artist') or album.get('artist') or '', 'album': album['title'], 'duration': track['duration']}
        ready, lyric_path, mismatch = False, None, []
        for song in track.get('lyric_matches', []):
            entry = published.get(song)
            if not entry:
                continue
            try:
                doc = prepare_imported_lyrics(entry, target, copied_packets, track['title'])
                lyric_path = pack / 'lyrics' / (target.stem + '.json')
                atomic_json(lyric_path, doc)
                ready = True
                break
            except ValueError as error:
                mismatch.append({'song_id': song, 'reason': str(error)})
        return metadata, lyric_path.relative_to(pack).as_posix() if ready else None, {'title': track['title'], 'source_name': source.name, 'path': metadata['path'], 'source_sha256': digest, 'audio_packet_hash': copied_packets, 'lyric_ready': ready, 'rejected_lyrics': mismatch, 'copied': copied}
    with ThreadPoolExecutor(max_workers=max(1, min(4, workers))) as executor:
        rows = list(executor.map(copy_recording, numbered))
    art, video_bytes = [], 0
    for source in sorted(backgrounds.rglob('*')) if backgrounds else []:
        if not source.is_file() or source.is_symlink() or source.suffix.lower() not in MEDIA_BACKGROUND:
            continue
        if source.resolve().is_relative_to(pack.resolve()):
            continue
        if source.suffix.lower() in {'.mp4', '.webm'}:
            if video_bytes + source.stat().st_size > 150*1024**2:
                continue
            video_bytes += source.stat().st_size
        target = pack / 'backgrounds' / source.relative_to(backgrounds)
        safe_copy(source, target)
        art.append(target.relative_to(pack).as_posix())
    tracks = [metadata for metadata, _, _ in rows]
    lyrics = {metadata['path']: lyric for metadata, lyric, _ in rows if lyric}
    profile = {'version': 1, 'name': name, 'tracks': tracks, 'backgrounds': art,
               'lyrics': lyrics, 'settings': {'brand': name, 'tagline': tagline},
               'lyrics_ready': list(lyrics)}
    atomic_json(pack / 'profile.json', profile)
    if user_profile:
        atomic_json(user_profile, relative_profile(profile, pack, user_profile.parent))
    (pack / 'All recordings.m3u').write_text('#EXTM3U\n' + ''.join(f"#EXTINF:{round(t['duration'])},{t['title']}\n{t['path']}\n" for t in tracks), encoding='utf-8')
    (pack / 'Songs with lyrics.m3u').write_text('#EXTM3U\n' + ''.join(f"#EXTINF:{round(t['duration'])},{t['title']}\n{t['path']}\n" for t in tracks if t['path'] in lyrics), encoding='utf-8')
    report = {'version': 1, 'created_at': int(time.time()), 'profile': str(pack/'profile.json'), 'music_tracks': len(tracks), 'lyrics_ready': len(lyrics),
              'still_backgrounds': sum(Path(p).suffix.lower() not in {'.mp4', '.webm'} for p in art), 'video_backgrounds': sum(Path(p).suffix.lower() in {'.mp4', '.webm'} for p in art),
              'total_bytes': sum(p.stat().st_size for p in pack.rglob('*') if p.is_file()), 'records': [record for _, _, record in rows], 'originals_modified': False}
    atomic_json(pack / 'seed-report.json', report)
    if archive:
        archive_path = pack.parent/(pack.name+'-portable-media.zip')
        with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as output:
            for path in sorted(pack.rglob('*')):
                if path.is_file() and not path.is_symlink():
                    output.write(path, Path(pack.name)/path.relative_to(pack))
        report['archive'] = str(archive_path)
        report['archive_bytes'] = archive_path.stat().st_size
        atomic_json(pack/'seed-report.json', report)
    return {key: value for key, value in report.items() if key != 'records'}


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True, help='Album-plan JSON for your selected local recordings.')
    parser.add_argument('--snapshot', type=Path, help='Optional timed-lyric snapshot with verified audio hashes.')
    parser.add_argument('--pack', type=Path, required=True, help='Destination media-pack folder.')
    parser.add_argument('--backgrounds', type=Path, help='Optional local artwork/video folder.')
    parser.add_argument('--user-profile', type=Path, help='Optional second profile using relative paths to the pack.')
    parser.add_argument('--name', default='My Station', help='Station name and on-screen brand.')
    parser.add_argument('--tagline', default='Original music. Stay awhile.')
    parser.add_argument('--zip', action='store_true')
    args = parser.parse_args(argv)
    result = make_pack(args.plan,args.snapshot,args.pack,args.backgrounds,args.user_profile,
                       archive=args.zip,name=args.name,tagline=args.tagline)
    print(json.dumps(result, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
