"""Model-free operator help and bounded, informational build provenance."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QVBoxLayout)

from . import __version__


MAX_RELEASE_MANIFEST_BYTES = 2 * 1024 * 1024
YOUTUBE_ENCODER_HELP = "https://support.google.com/youtube/answer/2853702?hl=en"


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

SETUP AND FIRST RUN
You can play music, record and broadcast without installing lyric models. Add recordings first, choose Picture & sound options, then show Program Output to check the picture. Use Local recording for a file on this computer, Direct livestream for a streaming service, or capture Program Output with OBS. Starting an output is always an explicit action.

LYRIC MODEL SETUP
Open Lyric models > Set up / find models. Start with Find existing models: it inspects compatible model files and Python environments without downloading or moving models. If the summary finds everything, go to Review and install and choose Validate and use existing models. That validates the selected local components and saves their locations after success.

If components are missing, review each proposed download, the selected Python, destination folder and disk requirement before choosing Install missing components. On Linux select an existing Python 3.11–3.13; setup can create an isolated model environment. On Windows the plan can include a compatible Python. Do not select an unrelated Python just because its version number matches. A compatible existing lyric environment avoids installing packages again. Cancel stops the current setup and keeps the previous active settings; partial downloaded files may be reused on retry. Closing this window alone does not install anything.

The four audio components have different jobs: Whisper large-v3 recognizes words, Whisper medium.en provides a second opinion, HTDemucs separates vocals, and wav2vec2 CTC aligns words to sound. Discovery reports candidate problems as well as compatible choices; read the Selected lyric runtime line before treating every diagnostic as a failure. CPU processing is supported; setup preserves the current GPU preference and does not prove recognition accuracy. Ollama is an optional text assistant and is not required to extract song lyrics.

BROADCAST AND BITRATE
Choose Local recording for a new output file, or Direct livestream for a service's RTMP/RTMPS server address and stream key. Keep the key in the masked Stream key field when your service provides a separate key. Choose the picture size and enter Video bitrate in kbps (kilobits per second). 28,500 kbps is 28.5 Mbps. A bitrate recommendation depends on codec, picture size, frame rate and available upload capacity; a higher number alone does not improve the source picture.

This app outputs H.264 at the frame rate displayed with Picture size. YouTube's official encoder guidance, checked 25 October 2026, lists different H.264 recommendations for each resolution and frame rate. Match its current guidance to the selected output rather than applying 28,500 to every stream. The YouTube encoder help button opens the official guidance in your browser; all other help here is bundled and works offline.

Check the destination, picture and saved-state message before Start output. Program Output preview alone does not send a stream. Direct output contains the program picture and its music; it does not mix microphone or other desktop audio. Monitor volume changes local listening only. Prepare and review lyrics before beginning an output.

SAVE AND RELOAD A STREAM PROFILE
Save profile stores reusable broadcast settings for this station data folder, independently of a playlist. Saving a playlist keeps its recording selection and appearance; it does not save a streaming secret. Read the visible profile status after each save and after changing a field. Changing a field does not update the saved profile until you save again. Reload saved restores the saved profile, so save pending changes first if you want to keep them.

Stream keys stay masked. Enable Remember stream key in OS keyring only when you want the key (or a complete private target) remembered in protected native storage on this computer. Without that option, the key is for this session only. A normal public server address, picture size and bitrate can be saved without a keyring. Ordinary server paths are limited to known static names (live, live2, app and application); custom paths may contain a key and require protected remembering. Do not put a key, password or token in an ordinary saved server address. If the keyring is missing or locked, use the current fields for this session or unlock/configure the desktop keyring yourself; the app does not silently fall back to plaintext secret storage. A message that settings saved but protected storage failed means the key was not confirmed saved; correct the keyring issue and retry deliberately.

Clear fields clears the current session; Forget saved removes the saved profile. Read and confirm any discard or forget prompt before proceeding; Cancel keeps the existing fields and saved selection. A profile belongs to its station data folder, so a different launcher or station may have a different saved profile. Saving or reloading never starts output. Private targets and keys are excluded from portable playlists, diagnostics and logs.

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

KEYBOARD ACCESS
Use Tab and Shift+Tab to move between controls, arrow keys to change a list or numeric value, and Space to activate the focused button or checkbox. Help has a topic list and a Find field (Ctrl+F); Enter or Find next advances to the next match. Model setup includes Back/Next navigation and F1 for its bundled help. Escape closes help without closing the player.

WHEN SOMETHING LOOKS WRONG
Broadcast profile: if changes disappear, check the saved/unsaved message, press Save profile, then verify the same station data folder after reopening. A locked or missing keyring can prevent remembering a key while ordinary settings still save. No plaintext fallback is used. If Direct livestream rejects the address, use a complete RTMP/RTMPS server URL without spaces; use the masked key field for the key. A local recording needs a new writable filename and cannot overwrite an existing file.

Models: if setup cannot find compatible files, add the specific model/cache folder or choose the intended model Python, then scan again. Review the selected runtime summary and the plan for missing components. Low disk space requires another destination or more free space. If installation fails, retain the existing locations, read progress details, fix the reported cause and retry. Automatic extraction needs human checking; installed models do not guarantee correct words.

If Save fails or reports a conflict, keep the editor open and read the status. A newer saved revision or changed recording must be resolved before overwriting; Reload saved discards the pending edits for that recording. A partial save can mean the lyric JSON committed but its session link still needs a retry. Do not assume the lyrics are saved until the status confirms success.

If lyrics will not appear on Program Output, check Picture & sound > Show sing-along lyrics and Lyrics > More lyric options > Preview unapproved saved lyrics on Program Output. Preview defaults on for a new station and remembers your last choice in its saved playlist/session. Lyrics-ready tracks only independently limits playback to approved recordings. Save pending corrections before previewing their saved timing. Extraction failures or uncertain recognition need review; preserve the raw findings rather than guessing corrections.

Lyric models checks existing local runtime/model locations. On Linux it can reuse the shared /var/lib/gnb-lyrics and /opt runtime locations when present. Custom paths remain selected; missing custom files are explained. Empty component overrides show Automatic with the effective path. Discovery does not download or copy models. The optional Ollama text-model tag is not required for audio lyric extraction.

If controls or behavior seem older than expected, compare this running version and recorded build identity with the intended package. An already open older window keeps its old code. Check desktop, menu and panel shortcut targets; close the older window yourself before launching the intended build. Help never replaces the app or starts a download.
"""


_HELP_TOPICS = (
    ('details', 'Version and app details', "Plague's Broadcast Station"),
    ('basics', 'Playback and Program Output', 'BASIC USE'),
    ('setup', 'Setup and first run', 'SETUP AND FIRST RUN'),
    ('models', 'Lyric models and optional Ollama', 'LYRIC MODEL SETUP'),
    ('broadcast', 'Broadcast and bitrate', 'BROADCAST AND BITRATE'),
    ('profiles', 'Save and reload stream settings', 'SAVE AND RELOAD A STREAM PROFILE'),
    ('lyrics', 'Edit, save and approve lyrics', "EDIT AND SAVE THE SELECTED SONG'S LYRICS"),
    ('storage', 'Storage on this device', 'STORAGE ON THIS DEVICE'),
    ('website', 'Website and radio stores', 'WEBSITE AND RADIO STORES'),
    ('keyboard', 'Keyboard access', 'KEYBOARD ACCESS'),
    ('troubleshooting', 'Troubleshooting', 'WHEN SOMETHING LOOKS WRONG'),
)


def _help_sections(content):
    # Paths and recorded release names can contain a topic title. Only a full
    # heading line marks a section boundary.
    offsets = [re.search(r'^' + re.escape(heading) + r'$', content, re.MULTILINE).start()
               for _, _, heading in _HELP_TOPICS]
    return [(key, title, content[offsets[index]:offsets[index + 1] if index + 1 < len(offsets) else len(content)].strip())
            for index, (key, title, _) in enumerate(_HELP_TOPICS)]


class HelpDialog(QDialog):
    """Reusable nonmodal help window; closing it leaves its operator window open."""

    def __init__(self, parent=None, *, data_dir: Path, topic: str | None = None):
        super().__init__(parent)
        self.setObjectName('help_dialog')
        self.setWindowTitle("Plague's Broadcast Station — Help and app details")
        self.setAccessibleName('Broadcast Station help and app details')
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(800, 700)
        self.setMinimumSize(540, 400)
        layout = QVBoxLayout(self)
        self._content = _help_text(data_dir)
        self._sections = _help_sections(self._content)
        navigation = QHBoxLayout()
        label = QLabel('&Topic')
        self.topics = QComboBox()
        self.topics.setObjectName('help_topics')
        self.topics.setAccessibleName('Help topic')
        self.topics.addItem('All help and app details', 'all')
        for key, title, _ in self._sections:
            self.topics.addItem(title, key)
        label.setBuddy(self.topics)
        navigation.addWidget(label)
        navigation.addWidget(self.topics, 1)
        layout.addLayout(navigation)
        search = QHBoxLayout()
        self.find = QLineEdit()
        self.find.setObjectName('help_find')
        self.find.setAccessibleName('Find in this help topic')
        self.find.setPlaceholderText('Find in this topic (Ctrl+F)')
        next_match = QPushButton('Find &next')
        next_match.setAutoDefault(False)
        next_match.setAccessibleName('Find next help match')
        next_match.clicked.connect(self.find_next)
        self.find.returnPressed.connect(self.find_next)
        search.addWidget(self.find, 1)
        search.addWidget(next_match)
        layout.addLayout(search)
        self.text = QPlainTextEdit()
        self.text.setObjectName('help_text')
        self.text.setAccessibleName('Broadcast Station help content')
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.text.setPlainText(self._content)
        layout.addWidget(self.text, 1)
        self.search_status = QLabel('Bundled help is available offline.')
        self.search_status.setWordWrap(True)
        self.search_status.setTextFormat(Qt.TextFormat.PlainText)
        self.search_status.setAccessibleName('Help search and link status')
        layout.addWidget(self.search_status)
        self.topics.currentIndexChanged.connect(self._topic_changed)
        self.find_shortcut = QShortcut(QKeySequence.StandardKey.Find, self)
        self.find_shortcut.activated.connect(self._focus_find)
        buttons = QHBoxLayout()
        self.youtube_help = QPushButton('YouTube encoder help ↗')
        self.youtube_help.setAutoDefault(False)
        self.youtube_help.setAccessibleName('Open official YouTube encoder guidance in a browser')
        self.youtube_help.setToolTip('Opens support.google.com in your browser. Internet access is needed.')
        self.youtube_help.clicked.connect(self.open_youtube_help)
        buttons.addWidget(self.youtube_help)
        buttons.addStretch()
        close = QPushButton('Close')
        close.setAutoDefault(False)
        close.setObjectName('help_close')
        close.setAccessibleName('Close help')
        close.clicked.connect(self.close)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        if topic:
            self.show_topic(topic)

    def show_topic(self, topic: str):
        """Select a local topic without starting discovery, downloads or output."""
        aliases = {'version': 'details', 'save': 'profiles', 'troubleshooting': 'troubleshooting'}
        index = self.topics.findData(aliases.get(topic, topic))
        self.topics.setCurrentIndex(max(0, index))

    def _topic_changed(self):
        key = self.topics.currentData()
        content = next((text for name, _, text in self._sections if name == key), self._content)
        self.text.setPlainText(content)
        self.search_status.setText('Bundled help is available offline.')

    def _focus_find(self):
        self.find.setFocus()
        self.find.selectAll()

    def find_next(self):
        query = self.find.text()
        if not query:
            self.search_status.setText('Enter a word or phrase to find in this topic.')
            return
        found = self.text.find(query)
        if not found:
            cursor = self.text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.text.setTextCursor(cursor)
            found = self.text.find(query)
        self.search_status.setText('Match selected. Find next continues from here.' if found else
                                   'No match in this topic. Choose All help and app details to search everything.')

    def open_youtube_help(self):
        # A fixed informational URL; no operator field or private target is used.
        opened = QDesktopServices.openUrl(QUrl(YOUTUBE_ENCODER_HELP))
        self.search_status.setText('Opened official YouTube encoder guidance in your browser.' if opened else
                                   'Could not open a browser. Official guidance: ' + YOUTUBE_ENCODER_HELP)
