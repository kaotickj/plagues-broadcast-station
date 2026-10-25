"""Private, untimed recognition drafts; never canonical lyrics or approval.

The initial machine draft is create-only. Human saves use a raw-file revision
and a shared draft lock. Every caller must bind a load/save to the currently
selected recording's SHA256. This module does not align, publish or play text.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

VERSION = 'recognition-draft-1'
MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_TEXT_CHARACTERS = 100_000
MAX_FAILURE_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r'[a-f0-9]{64}')
_KEYS = {'version', 'source_sha256', 'status', 'needs_review', 'text',
         'origin_failure', 'created_at', 'updated_at', 'edited_by', 'edit_revision'}


class RecognitionDraftError(ValueError):
    """The draft was not accepted or saved; keep the caller's edit buffer."""


@dataclass(frozen=True)
class RecognitionDraft:
    path: Path
    source_sha256: str
    text: str
    raw_payload: bytes

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.raw_payload).hexdigest()

    @property
    def payload(self) -> dict:
        return json.loads(self.raw_payload)


def _fingerprint(value) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value.lower()) is None:
        raise RecognitionDraftError('Select a recording with a valid SHA256 before opening or saving its recognition draft.')
    return value.lower()


def _absolute(value) -> Path:
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError, RuntimeError) as error:
        raise RecognitionDraftError('The recognition draft needs a valid local path.') from error
    if '..' in path.parts or '\x00' in str(path):
        raise RecognitionDraftError('A recognition draft path must not traverse parent directories.')
    return path.absolute()


def _text(value) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT_CHARACTERS:
        raise RecognitionDraftError('Recognition draft text must contain at most 100,000 characters; nothing was truncated.')
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise RecognitionDraftError('Recognition draft text must be valid Unicode; nothing was saved.') from error
    return value


def text_from_evidence(evidence: dict) -> str:
    """Retain measured line text, including whitespace, with no phrase filters.

    Prefer the original pre-alignment cue text. If none exists, retain raw
    decoder text rows. An empty result is not a claim that the audio is silent.
    """
    if not isinstance(evidence, dict):
        raise RecognitionDraftError('Recognition evidence must be an object.')
    cues = evidence.get('pre_alignment')
    if isinstance(cues, dict):
        cues = cues.get('segments')
    if isinstance(cues, list) and cues:
        rows = cues
    else:
        primary = evidence.get('primary') or {}
        windows = primary.get('windows', []) if isinstance(primary, dict) else []
        if not isinstance(windows, list) or len(windows) > 10000:
            raise RecognitionDraftError('Recognition evidence has an invalid window list.')
        rows = []
        for window in windows:
            if not isinstance(window, dict):
                raise RecognitionDraftError('Recognition evidence has an invalid window.')
            raw = window.get('recognition') or window.get('raw') or {}
            if not isinstance(raw, dict):
                raise RecognitionDraftError('Recognition evidence has invalid raw decoder data.')
            values = raw.get('segments', raw.get('transcription', []))
            if not isinstance(values, list):
                raise RecognitionDraftError('Recognition evidence has invalid decoder text rows.')
            rows.extend(values)
            if len(rows) > 10000:
                raise RecognitionDraftError('Recognition evidence has too many text rows.')
    if len(rows) > 10000:
        raise RecognitionDraftError('Recognition evidence has too many text rows.')
    texts = []
    length = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str):
            raise RecognitionDraftError('Recognition evidence has a non-text row; no partial draft was created.')
        length += len(row['text']) + bool(texts)
        if length > MAX_TEXT_CHARACTERS:
            raise RecognitionDraftError('Recognition draft text is too large; raw evidence remains available without truncation.')
        texts.append(row['text'])
    return _text('\n'.join(texts))


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextmanager
def _directory(path: Path, *, create=False):
    """Hold a no-follow directory capability rather than reopening ancestors.

    These capabilities are supported by the current Linux target. A platform
    without them fails closed, rather than silently using weaker path checks.
    """
    descriptor = None
    try:
        if os.open not in os.supports_dir_fd or not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW'):
            raise RecognitionDraftError('Secure recognition-draft storage is unavailable on this platform; raw failure evidence remains available.')
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(path.anchor, flags)
        for component in path.parts[1:]:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    except RecognitionDraftError:
        raise
    except OSError as error:
        raise RecognitionDraftError('The recognition-draft directory is missing, unreadable, or contains a symbolic link. Nothing was replaced.') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stable_directory(path: Path, descriptor: int):
    current = path.stat(follow_symlinks=False)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
        raise RecognitionDraftError('The recognition-draft directory changed. Keep your text and reopen the latest draft.')


def _entry(descriptor: int, name: str):
    try:
        return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _read(descriptor: int, name: str, *, maximum=MAX_DOCUMENT_BYTES) -> bytes:
    fd = None
    try:
        before = _entry(descriptor, name)
        if before is None or not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise RecognitionDraftError('The recognition draft is missing, nonregular, empty, or too large. Keep your text and reopen it.')
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        with os.fdopen(fd, 'rb') as stream:
            fd = None
            opened = os.fstat(stream.fileno())
            if _stamp(opened) != _stamp(before):
                raise RecognitionDraftError('The recognition draft changed while opening. Keep your text and reopen it.')
            raw = stream.read(maximum + 1)
            after = _entry(descriptor, name)
            if (len(raw) > maximum or after is None or _stamp(os.fstat(stream.fileno())) != _stamp(opened)
                    or _stamp(after) != _stamp(opened)):
                raise RecognitionDraftError('The recognition draft changed while reading. Keep your text and reopen it.')
            return raw
    except RecognitionDraftError:
        raise
    except OSError as error:
        raise RecognitionDraftError('The recognition draft is missing, unreadable or changed. Nothing was saved.') from error
    finally:
        if fd is not None:
            os.close(fd)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecognitionDraftError('The recognition draft contains duplicate fields.')
        result[key] = value
    return result


def _decode(raw: bytes, fingerprint: str) -> dict:
    try:
        data = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    except (ValueError, UnicodeError) as error:
        raise RecognitionDraftError('The recognition draft is malformed; its original file was kept.') from error
    if not isinstance(data, dict) or set(data) != _KEYS:
        raise RecognitionDraftError('This is not an untimed recognition draft. Timed lyrics, approval and publication fields are not accepted.')
    if (data['version'] != VERSION or data['source_sha256'] != fingerprint
            or data['status'] != 'unaligned' or data['needs_review'] is not True
            or data['edited_by'] not in ('machine', 'human')):
        raise RecognitionDraftError('This recognition draft has a different recording or unsupported state.')
    _text(data['text'])
    for key in ('created_at', 'updated_at', 'edit_revision'):
        if type(data[key]) is not int or not 0 <= data[key] < 2**63:
            raise RecognitionDraftError('The recognition draft has invalid revision metadata.')
    origin = data['origin_failure']
    if (not isinstance(origin, dict) or set(origin) != {'file_name', 'sha256'}
            or not isinstance(origin['file_name'], str)
            or re.fullmatch(re.escape(fingerprint) + r'-[a-f0-9]{32}\.json', origin['file_name']) is None
            or not isinstance(origin['sha256'], str) or _SHA.fullmatch(origin['sha256']) is None):
        raise RecognitionDraftError('The recognition draft has invalid original-failure provenance.')
    return data


def _target(path, fingerprint):
    path = _absolute(path)
    if path.parent.name != 'recognition-drafts' or path.name != fingerprint + '.json':
        raise RecognitionDraftError('Recognition drafts belong in the selected recording’s separate managed draft folder.')
    return path


def draft_path(cache, fingerprint) -> Path:
    """Return the deterministic separate path, without creating any directory."""
    return _absolute(cache) / 'recognition-drafts' / (_fingerprint(fingerprint) + '.json')


@contextmanager
def _lock(descriptor: int, name: str):
    fd = None
    try:
        import fcntl
        fd = os.open('.' + name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=descriptor)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RecognitionDraftError('Refusing a nonregular recognition-draft lock.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RecognitionDraftError('Another draft save is in progress. Keep your text and retry after it finishes.') from error
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                # Closing this owned descriptor also releases its lock.
                pass
    except RecognitionDraftError:
        raise
    except (OSError, ImportError) as error:
        raise RecognitionDraftError('The private recognition-draft lock is unavailable. Nothing was saved.') from error
    finally:
        if fd is not None:
            os.close(fd)


def _payload(data: dict) -> bytes:
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8') + b'\n'
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise RecognitionDraftError('The recognition draft exceeds its one MiB file limit; nothing was truncated or saved.')
    return raw


def _write(descriptor: int, path: Path, raw: bytes, *, original: bytes | None):
    temporary = '.draft-' + uuid.uuid4().hex + '.tmp'
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _stable_directory(path.parent, descriptor)
        if original is None:
            # Hard-link publication is atomic create-only, including against a
            # noncooperating writer that appeared after the initial check.
            try:
                os.link(temporary, path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
            except FileExistsError:
                return False
        else:
            if _read(descriptor, path.name) != original:
                raise RecognitionDraftError('The recognition draft changed while saving. Keep your text and reopen the latest revision.')
            os.replace(temporary, path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        # A failed directory fsync cannot truthfully turn an already committed
        # rename into an "unsaved" result. File data was fsynced before commit.
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        return True
    except RecognitionDraftError:
        raise
    except OSError as error:
        raise RecognitionDraftError('The recognition draft could not be saved. Existing text was kept; keep your edit buffer and retry.') from error
    finally:
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except OSError:
            pass


def create_initial_draft(cache, fingerprint, text, origin_failure) -> RecognitionDraft | None:
    """Create only an absent draft; never replace any existing machine/human file.

    Return None when a regular target already exists, even if it is malformed.
    Empty recognition keeps its raw failure report rather than creating a
    misleading empty "saved draft". A human may later save an empty draft.
    """
    fingerprint = _fingerprint(fingerprint)
    text = _text(text)
    if not text.strip():
        raise RecognitionDraftError('No recognition text is available for an editable draft; the failure evidence remains separate.')
    cache = _absolute(cache)
    path = draft_path(cache, fingerprint)
    failure = _absolute(origin_failure)
    if (failure.parent != cache / 'failures'
            or re.fullmatch(re.escape(fingerprint) + r'-[a-f0-9]{32}\.json', failure.name) is None):
        raise RecognitionDraftError('The original failure report must belong to this recording and this private cache.')
    with _directory(path.parent, create=True) as directory:
        with _lock(directory, path.name):
            existing = _entry(directory, path.name)
            if existing is not None:
                if not stat.S_ISREG(existing.st_mode):
                    raise RecognitionDraftError('Refusing a nonregular recognition draft; it was not replaced.')
                return None
            with _directory(failure.parent) as failures:
                original = _read(failures, failure.name, maximum=MAX_FAILURE_BYTES)
            try:
                source = json.loads(original.decode('utf-8'), object_pairs_hook=_object)
            except (ValueError, UnicodeError) as error:
                raise RecognitionDraftError('The original failure report is malformed; no draft was created.') from error
            if (not isinstance(source, dict) or source.get('version') != 'extraction-failure-1'
                    or source.get('source_sha256') != fingerprint or source.get('status') != 'failed'):
                raise RecognitionDraftError('The original failure report belongs to another recording or unsupported result.')
            now = int(time.time())
            data = {'version': VERSION, 'source_sha256': fingerprint, 'status': 'unaligned',
                    'needs_review': True, 'text': text,
                    'origin_failure': {'file_name': failure.name, 'sha256': hashlib.sha256(original).hexdigest()},
                    'created_at': now, 'updated_at': now, 'edited_by': 'machine', 'edit_revision': 0}
            raw = _payload(data)
            if not _write(directory, path, raw, original=None):
                return None
    return RecognitionDraft(path, fingerprint, text, raw)


def load_draft(path, *, expected_source_sha256) -> RecognitionDraft:
    fingerprint = _fingerprint(expected_source_sha256)
    path = _target(path, fingerprint)
    with _directory(path.parent) as directory:
        raw = _read(directory, path.name)
        data = _decode(raw, fingerprint)
        _stable_directory(path.parent, directory)
    return RecognitionDraft(path, fingerprint, data['text'], raw)


def save_user_draft(path, text, *, expected_revision, expected_source_sha256) -> RecognitionDraft:
    """Save explicit human text against the raw revision opened by the editor.

    Cooperating callers use this lock; a final byte comparison also notices
    external edits made before commit. This cannot serialize arbitrary programs
    that ignore the lock and write after that comparison.
    """
    fingerprint = _fingerprint(expected_source_sha256)
    text = _text(text)
    if not isinstance(expected_revision, str) or _SHA.fullmatch(expected_revision) is None:
        raise RecognitionDraftError('Open the current recognition draft before saving; a revision is required.')
    path = _target(path, fingerprint)
    with _directory(path.parent) as directory:
        with _lock(directory, path.name):
            original = _read(directory, path.name)
            if hashlib.sha256(original).hexdigest() != expected_revision:
                raise RecognitionDraftError('The recognition draft changed since it opened. Keep your text and reopen the latest revision.')
            data = _decode(original, fingerprint)
            if data['edit_revision'] == 2**63 - 1:
                raise RecognitionDraftError('This draft reached its supported revision limit. Keep your text; nothing was replaced.')
            data.update(text=text, updated_at=int(time.time()), edited_by='human', edit_revision=data['edit_revision'] + 1)
            raw = _payload(data)
            _write(directory, path, raw, original=original)
    return RecognitionDraft(path, fingerprint, text, raw)
