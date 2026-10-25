"""GUI-only, read-only adapter for recording-bound untimed failure recovery.

This module never creates a draft, imports timed lyrics, approves or publishes.
The editable store and extraction worker retain ownership of their contracts.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re

from .lyrics import fingerprint
from .recognition_drafts import load_draft, draft_path, text_from_evidence, RecognitionDraftError
from .lyric_recovery import _read_artifact, _unique_object, _reject_constant, _validate_tree


class RecoveryOpenError(ValueError):
    pass


@dataclass(frozen=True)
class RecoveryOpenData:
    snapshot: object
    source_sha256: str
    source_stamp: tuple
    original_text: str
    diagnostics_text: str
    draft_error: str = ''


def source_stamp(path):
    item = Path(path).stat()
    return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)


def _path(value):
    path = Path(value)
    if '..' in path.parts or '\x00' in str(path):
        raise RecoveryOpenError('The recovery path must not traverse parent directories.')
    return Path(os.path.abspath(path))


def _failure(directory, path, sha, *, expected_digest=None):
    path = _path(path)
    if path.parent != directory or re.fullmatch(sha + r'-[a-f0-9]{32}\.json', path.name) is None:
        raise RecoveryOpenError('The failure report is outside this recording’s private failure folder.')
    raw = _read_artifact(directory, path)
    if expected_digest and hashlib.sha256(raw).hexdigest() != expected_digest:
        raise RecoveryOpenError('The original failure report changed; the draft and current editor were kept.')
    data = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    _validate_tree(data)
    if (not isinstance(data, dict) or data.get('version') != 'extraction-failure-1'
            or data.get('status') != 'failed' or data.get('source_sha256') != sha
            or not isinstance(data.get('recognition_evidence'), dict)):
        raise RecoveryOpenError('The failure report belongs to another recording or unsupported result.')
    return raw, data


def load_open_data(cache_dir, source_path, event=None):
    """Freshly fingerprint selected bytes; read only their managed recovery data.

    An absent draft can still expose a structured event's raw diagnostic report.
    Existing drafts reopen through their immutable original-report provenance,
    independently of later failed retries and of station restarts.
    """
    event = event or {}
    cache = _path(cache_dir)
    before = source_stamp(source_path)
    sha = fingerprint(Path(source_path))
    if source_stamp(source_path) != before:
        raise RecoveryOpenError('The recording changed while opening recovery. Select its current bytes and retry.')
    announced = event.get('source_sha256')
    if announced is not None and announced != sha:
        raise RecoveryOpenError('This failed result belongs to earlier recording bytes. The current lyrics and editor were kept.')
    target = draft_path(cache, sha)
    supplied = event.get('recognition_draft_path')
    if supplied is not None and _path(supplied) != target:
        raise RecoveryOpenError('The recognition draft is outside this recording’s managed draft folder.')
    snapshot = None
    draft_error = ''
    if target.exists() or target.is_symlink():
        try:
            snapshot = load_draft(target, expected_source_sha256=sha)
        except RecognitionDraftError as error:
            if not event.get('diagnostic_path'):raise
            draft_error = str(error)
    failures = cache / 'failures'
    reports = []
    original_text = ''
    origin_path = None
    if snapshot is not None:
        origin = snapshot.payload['origin_failure']
        origin_path = failures / origin['file_name']
        raw, payload = _failure(failures, origin_path, sha, expected_digest=origin['sha256'])
        try:
            original_text = text_from_evidence(payload['recognition_evidence'])
        except ValueError:
            original_text = 'Original text could not be summarized. The complete original report is retained in Diagnostics.'
        reports.append('Original failure report (unchanged):\n' + raw.decode('utf-8'))
    latest = event.get('diagnostic_path')
    if latest is not None:
        path = _path(latest)
        if path != origin_path:
            raw, payload = _failure(failures, path, sha)
            reports.append('Latest failure report (unchanged):\n' + raw.decode('utf-8'))
            if snapshot is None:
                try:
                    original_text = text_from_evidence(payload['recognition_evidence'])
                except ValueError:
                    original_text = ''
    if snapshot is None and not reports:
        raise RecoveryOpenError('No recognition draft is available for this recording. A failed automatic extraction may retain one; saved timed lyrics were kept.')
    # Rehash rather than trusting a cached title, path or recent metadata stamp.
    final_sha = fingerprint(Path(source_path))
    if final_sha != sha or source_stamp(source_path) != before:
        raise RecoveryOpenError('The recording changed while reading recovery. Keep the current editor and retry.')
    if draft_error:reports.insert(0,'Editable draft was not opened; its file was preserved: '+draft_error)
    return RecoveryOpenData(snapshot, sha, before, original_text, '\n\n'.join(reports), draft_error)
