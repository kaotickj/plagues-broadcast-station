"""Read-only failed-recognition inspection, separate from timed lyric documents.

Nothing here saves lyrics, repairs timing, approves a revision, or registers
content with Program Output. The caller supplies the current recording's exact
fingerprint and its private failure directory; titles never establish identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
import os
import re
import stat

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTabWidget, QVBoxLayout,
)


MAX_RECOVERY_BYTES = 8 * 1024 * 1024
MAX_RECOVERY_RECORDS = 10000


class RecoveryError(ValueError):
    """A failure artifact could not safely be associated with this recording."""


@dataclass(frozen=True)
class RecoveryCandidate:
    """Immutable inspection data, deliberately not a LyricsDocument.

    ``raw_payload`` retains the original file bytes. ``payload`` and ``evidence``
    return fresh JSON objects so inspecting a candidate cannot change its data.
    ``text`` uses original decoder text rows separated by newlines; it does not
    normalize, filter, or try to correct the recognized words.
    """
    path: Path
    source_sha256: str
    raw_payload: bytes
    text: str
    raw_word_count: int
    raw_text_record_count: int
    pre_alignment_word_count: int
    zero_duration_word_count: int
    message: str

    @property
    def payload(self) -> dict:
        return json.loads(self.raw_payload)

    @property
    def evidence(self) -> dict:
        return self.payload['recognition_evidence']

    @property
    def summary(self) -> str:
        return (
            'Recovered recognition — timing unverified\n'
            'This is failed recognition evidence. No replacement lyrics were saved. '
            'It cannot be approved, exported as timed lyrics, or shown on Program Output here.\n'
            f'Raw word records: {self.raw_word_count}; raw text rows: {self.raw_text_record_count}; '
            f'pre-alignment word records: {self.pre_alignment_word_count}.\n'
            f'Zero-duration word records in raw evidence: {self.zero_duration_word_count}. '
            'Original measurements are retained without guessed timing.\n'
            f'Failure: {self.message}\n'
            f'Recording SHA-256: {self.source_sha256}\nArtifact: {self.path}'
        )

    @property
    def diagnostics_text(self) -> str:
        return self.summary + '\n\nOriginal failure artifact (unchanged):\n' + self.raw_payload.decode('utf-8')


def _absolute_plain_path(value) -> Path:
    path = Path(value)
    if '..' in path.parts:
        raise RecoveryError('The recovery path must not traverse parent directories.')
    return Path(os.path.abspath(path))


def _read_bounded(fd: int) -> bytes:
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_RECOVERY_BYTES:
        raise RecoveryError('The recovery artifact must be a bounded regular file.')
    chunks = []
    remaining = MAX_RECOVERY_BYTES + 1
    while remaining:
        chunk = os.read(fd, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    data = b''.join(chunks)
    after = os.fstat(fd)
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                              value.st_mtime_ns, value.st_ctime_ns)
    if len(data) > MAX_RECOVERY_BYTES or len(data) != before.st_size or identity(before) != identity(after):
        raise RecoveryError('The recovery artifact changed or exceeded its limit while reading.')
    return data


def _read_artifact(directory: Path, path: Path) -> bytes:
    """Use no-follow directory-relative reads where the platform supports them."""
    nofollow = getattr(os, 'O_NOFOLLOW', 0)
    if os.open in os.supports_dir_fd and nofollow and hasattr(os, 'O_DIRECTORY'):
        directory_fd = os.open(directory.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in directory.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | nofollow, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(path.name, os.O_RDONLY | nofollow | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory_fd)
            try:
                return _read_bounded(fd)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
    # Windows lacks openat/O_NOFOLLOW. Reject linked components before opening,
    # then verify that the opened regular file still matches the checked path.
    def check_links():
        for component in (path, *path.parents):
            if component.is_symlink() or getattr(component, 'is_junction', lambda: False)():
                raise RecoveryError('Linked recovery paths are not supported.')

    check_links()
    checked = path.stat()
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        if not os.path.samestat(checked, os.fstat(fd)):
            raise RecoveryError('The recovery artifact changed while opening.')
        data = _read_bounded(fd)
        check_links()
        if not os.path.samestat(path.stat(), os.fstat(fd)):
            raise RecoveryError('The recovery artifact changed while reading.')
        return data
    finally:
        os.close(fd)


def _reject_constant(value):
    raise RecoveryError('The recovery artifact contains a nonfinite number.')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryError('The recovery artifact contains duplicate JSON fields.')
        result[key] = value
    return result


def _validate_tree(value, depth=0):
    if depth > 40:
        raise RecoveryError('The recovery artifact is nested too deeply.')
    if isinstance(value, float) and not math.isfinite(value):
        _reject_constant(value)
    if isinstance(value, str):
        value.encode('utf-8')  # Reject invalid Unicode before it reaches Qt.
    elif isinstance(value, dict):
        for key, child in value.items():
            key.encode('utf-8')
            _validate_tree(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_tree(child, depth + 1)


def _rows(value) -> list:
    if not isinstance(value, list) or len(value) > MAX_RECOVERY_RECORDS:
        raise RecoveryError('The recovery artifact has an invalid or oversized record list.')
    return value


def _text(row) -> str:
    if not isinstance(row, dict) or not isinstance(row.get('text'), str):
        raise RecoveryError('A recovered text record is malformed.')
    return row['text']


def _interval(row, start='start', end='end'):
    a, b = row.get(start), row.get(end)
    if (not all(type(v) in (int, float) and math.isfinite(v) for v in (a, b))
            or a < 0 or b < a):
        raise RecoveryError('A recovered measurement is malformed.')
    return a, b  # Zero and unusually long durations remain evidence, not valid lyric timing.


def _word(row):
    _text(row)
    a, b = _interval(row)
    score = row.get('score')
    if score is not None and (type(score) not in (int, float) or not 0 <= score <= 1):
        raise RecoveryError('A recovered confidence measurement is malformed.')
    return int(a == b)


def _recognition_details(evidence):
    primary = evidence.get('primary')
    if not isinstance(primary, dict):
        raise RecoveryError('The recovery artifact has no primary recognition evidence.')
    text_rows = []
    raw_count = zero_count = text_record_count = 0
    if 'segments' in primary:
        for segment in _rows(primary['segments']):
            text_rows.append(_text(segment))
            text_record_count += 1
            _interval(segment)
            for word in _rows(segment.get('words', [])):
                zero_count += _word(word)
                raw_count += 1
    elif 'windows' in primary:
        for window in _rows(primary['windows']):
            if not isinstance(window, dict) or not isinstance(window.get('raw'), dict):
                raise RecoveryError('A recovered recognition window is malformed.')
            rows = []
            for row in _rows(window['raw'].get('transcription')):
                text = _text(row)
                bounds = row.get('offsets')
                if not isinstance(bounds, dict):
                    raise RecoveryError('A recovered decoder interval is malformed.')
                _interval(bounds, 'from', 'to')
                if text:
                    rows.append(text)
                    text_record_count += 1
            # Decoder rows can be individual words. Retain their exact spacing
            # within a window rather than inventing a lyric line per word.
            if rows:
                text_rows.append(''.join(rows))
    pre_count = 0
    pre_text = []
    for cue in _rows(evidence.get('pre_alignment', [])):
        pre_text.append(_text(cue))
        _interval(cue)
        for word in _rows(cue.get('words', [])):
            _word(word)
            pre_count += 1
    if raw_count > MAX_RECOVERY_RECORDS or pre_count > MAX_RECOVERY_RECORDS:
        raise RecoveryError('The recovery artifact contains too many word records.')
    text = '\n'.join(text_rows if text_rows else pre_text)
    if not text.strip():
        raise RecoveryError('The failure artifact contains no recoverable recognition text.')
    return text, raw_count, text_record_count, pre_count, zero_count


def load_failure_candidate(path, *, failures_dir, expected_source_sha256: str) -> RecoveryCandidate:
    """Read one direct-child failure artifact bound to the current audio bytes.

    An older attempt for the same fingerprint can be reopened intentionally; an
    artifact for an earlier/different recording fingerprint is always rejected.
    No directories, files, cache timestamps, or application state are written.
    """
    if not isinstance(expected_source_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_source_sha256):
        raise RecoveryError('The selected recording has no valid exact source fingerprint.')
    try:
        directory = _absolute_plain_path(failures_dir)
        artifact = _absolute_plain_path(path)
        if artifact.parent != directory or not re.fullmatch(expected_source_sha256 + r'-[a-f0-9]{32}\.json', artifact.name):
            raise RecoveryError('The recovery artifact is outside this recording’s private failure directory.')
        raw = _read_artifact(directory, artifact)
        data = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant, object_pairs_hook=_unique_object)
        _validate_tree(data)
        if (not isinstance(data, dict) or data.get('version') != 'extraction-failure-1'
                or data.get('status') != 'failed' or data.get('source_sha256') != expected_source_sha256
                or type(data.get('created_at')) is not int or data['created_at'] < 0
                or not isinstance(data.get('message'), str)
                or not isinstance(data.get('recognition_evidence'), dict)):
            raise RecoveryError('The failure artifact is malformed or belongs to a different recording.')
        details = _recognition_details(data['recognition_evidence'])
        return RecoveryCandidate(artifact, expected_source_sha256, raw, *details, data['message'])
    except RecoveryError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise RecoveryError('The recovery artifact could not be read safely.') from error


class RecoveryDialog(QDialog):
    """Nonmodal, copy-only evidence inspection; closing leaves the parent open."""

    def __init__(self, candidate: RecoveryCandidate, parent=None):
        super().__init__(parent)
        self.setObjectName('lyric_recovery_dialog')
        self.setWindowTitle('Recovered recognition — timing unverified')
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(900, 680)
        layout = QVBoxLayout(self)
        notice = QLabel(candidate.summary)
        notice.setTextFormat(Qt.TextFormat.PlainText)
        notice.setWordWrap(True)
        notice.setObjectName('lyric_recovery_summary')
        layout.addWidget(notice)
        tabs = QTabWidget()
        self.text = QPlainTextEdit()
        self.text.setObjectName('lyric_recovery_text')
        self.text.setAccessibleName('Recovered recognition text; timing unverified')
        self.text.setReadOnly(True)
        self.text.setPlainText(candidate.text)
        tabs.addTab(self.text, 'Recognition text')
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setObjectName('lyric_recovery_diagnostics')
        self.diagnostics.setAccessibleName('Original failed recognition evidence and measurements')
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setPlainText(candidate.diagnostics_text)
        tabs.addTab(self.diagnostics, 'Diagnostics')
        layout.addWidget(tabs, 1)
        buttons = QHBoxLayout()
        copy_text = QPushButton('Copy recognition text')
        copy_text.setObjectName('lyric_recovery_copy_text')
        copy_text.clicked.connect(lambda: QApplication.clipboard().setText(candidate.text))
        buttons.addWidget(copy_text)
        copy_diagnostics = QPushButton('Copy diagnostics')
        copy_diagnostics.setObjectName('lyric_recovery_copy_diagnostics')
        copy_diagnostics.clicked.connect(lambda: QApplication.clipboard().setText(candidate.diagnostics_text))
        buttons.addWidget(copy_diagnostics)
        buttons.addStretch()
        close = QPushButton('Close')
        close.setObjectName('lyric_recovery_close')
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
