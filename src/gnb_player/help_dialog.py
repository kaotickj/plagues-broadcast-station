"""Model-free operator help and bounded, informational build provenance."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout

from . import __version__


MAX_RELEASE_MANIFEST_BYTES = 2 * 1024 * 1024


def _release_manifest() -> dict | None:
    """Read only the frozen executable's own regular, bounded release manifest."""
    if not getattr(sys, 'frozen', False):
        return None
    try:
        path = Path(sys.executable).resolve().parent / '.pbs-release.json'
        if path.is_symlink():
            return None
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RELEASE_MANIFEST_BYTES:
                return None
            payload = stream.read(MAX_RELEASE_MANIFEST_BYTES + 1)
        if len(payload) > MAX_RELEASE_MANIFEST_BYTES:
            return None
        manifest = json.loads(payload.decode('utf-8'))
        if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int
                or manifest['schema'] != 1 or manifest.get('app_id') != 'plagues-broadcast-station'):
            return None
        return manifest
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _plain_value(value, limit=200) -> str | None:
    if isinstance(value, str) and value and len(value) <= limit and value.isprintable():
        return value
    return None


def _build_details() -> str:
    manifest = _release_manifest()
    if manifest is None:
        kind = 'release manifest missing, invalid or unavailable' if getattr(sys, 'frozen', False) else 'source or unpackaged run'
        return f'Build identity: unrecorded ({kind}).'
    lines = ['Build identity from the sibling .pbs-release.json (recorded metadata, not a fresh integrity check):']
    version = _plain_value(manifest.get('version'), 80)
    release = _plain_value(manifest.get('release'))
    if version:
        lines.append('Manifest version: ' + version)
        if version != __version__:
            lines.append('Version mismatch: this window is running ' + __version__ + '; check the launcher and installed build.')
    if release:
        lines.append('Release ID: ' + release)
    for key, title, width in [('source_commit', 'Source commit', 40),
                              ('source_tree_sha256', 'Source tree SHA-256', 64),
                              ('bundle_sha256', 'Package content SHA-256', 64)]:
        value = manifest.get(key)
        if isinstance(value, str) and re.fullmatch(r'[0-9a-fA-F]{' + str(width) + '}', value):
            lines.append(title + ': ' + value)
    if type(manifest.get('source_dirty')) is bool:
        lines.append('Build recorded uncommitted source changes: ' + ('yes' if manifest['source_dirty'] else 'no'))
    if len(lines) == 1:
        return 'Build identity: unrecorded (release manifest contains no usable identity fields).'
    return '\n'.join(lines)


def _help_text(data_dir: Path) -> str:
    location = Path(data_dir).resolve()
    return f"""Plague's Broadcast Station
Running app version: {__version__}
{_build_details()}

BASIC USE
Add files or a folder, select a recording, and use the playback controls. Open or save a playlist to keep a station selection. Show Program Output opens the separate program picture; it does not start a stream by itself. Choose your backdrop and appearance before recording or starting direct output. For OBS, capture Program Output and this app's audio.

EDIT AND SAVE THE SELECTED SONG'S LYRICS
Select the recording, then open Lyrics. Double-click a lyric cell or press F2 to edit words or line times. Save lyrics (or Ctrl+S inside the lyric editor) commits a draft for that recording without a filename picker. Export lyrics… is a separate action: it writes a saved JSON copy to a location you choose. Save pending changes before exporting.

A saved draft is not approval. Listen against this exact recording, confirm human review, then choose Approve for airing. Editing again creates a draft that needs a new review. Local Save lyrics and Approve for airing do not publish lyrics to a website. Automatic preparation does not approve lyrics or prove that the recognized words are correct.

STORAGE ON THIS DEVICE
Station data directory: {location}
Session and recording-to-lyrics links: {location / 'session.json'}
Managed lyric revisions: {location / 'lyrics-cache' / 'lyrics' / '<audio SHA256>.json'}
Existing native JSON can stay at its linked location. The managed lyric identity follows the recording's exact bytes, not its title. These paths are informational; Help does not read settings, lyrics or credentials, and it does not change storage.

WEBSITE AND RADIO STORES
Broadcast Station's local lyrics/session and the website CMS lyric store are independent. Use explicit import/export or snapshot workflows and review the destination copy. Matching titles alone do not establish matching recordings. There is no automatic website or AzuraCast synchronization, and local approval is separate from website publication.

WHEN SOMETHING LOOKS WRONG
If Save fails or reports a conflict, keep the editor open and read the status. A newer saved revision or changed recording must be resolved before overwriting; Reload saved discards the pending edits for that recording. A partial save can mean the lyric JSON committed but its session link still needs a retry. Do not assume the lyrics are saved until the status confirms success.

If lyrics will not appear on Program Output, check that the saved revision is approved for this recording. Draft preview is an explicit option. Extraction failures or uncertain recognition need review; preserve the raw findings rather than guessing corrections.

If controls or behavior seem older than expected, compare this running version and recorded build identity with the intended package. An already open older window keeps its old code. Check desktop, menu and panel shortcut targets; close the older window yourself before launching the intended build. Help never replaces the app or starts a download.
"""


class HelpDialog(QDialog):
    """Reusable nonmodal help window; closing it leaves its operator window open."""

    def __init__(self, parent=None, *, data_dir: Path):
        super().__init__(parent)
        self.setObjectName('help_dialog')
        self.setWindowTitle("Plague's Broadcast Station — Help and app details")
        self.setAccessibleName('Broadcast Station help and app details')
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(800, 700)
        self.setMinimumSize(540, 400)
        layout = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setObjectName('help_text')
        self.text.setAccessibleName('Broadcast Station help content')
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text.setPlainText(_help_text(data_dir))
        layout.addWidget(self.text, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        close = QPushButton('Close')
        close.setObjectName('help_close')
        close.setAccessibleName('Close help')
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)
