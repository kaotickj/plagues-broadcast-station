"""Recording-bound untimed editor, separate from timed lyrics and approval."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Callable

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTabWidget, QVBoxLayout,
)
from .lyrics import fingerprint
from .recognition_drafts import RecognitionDraft, RecognitionDraftError, load_draft, save_user_draft


class _DraftOperation(QThread):
    def __init__(self, work, parent=None):
        super().__init__(parent)
        self.work, self.value, self.error = work, None, None

    def run(self):
        try:
            self.value = self.work()
        except RecognitionDraftError as error:
            self.error = str(error)
        except Exception:
            self.error = 'The recording or recognition draft could not be read or saved. Keep your text and retry after checking the files.'


class RecognitionDraftDialog(QDialog):
    """Nonmodal untimed editor with fresh source and revision checks.

    verify_source is a GUI-thread selection predicate checked before/after IO.
    Actual recording SHA256 is checked separately by the worker. No snapshot
    opens read-only evidence and never creates a replacement draft.
    """
    saved = Signal(object)
    operation_finished = Signal(str, bool)

    def __init__(self, snapshot: RecognitionDraft | None, parent=None, *,
                 source_path: str | Path, source_sha256: str | None = None,
                 recording_title: str = '', diagnostics_text: str = '',
                 original_text: str = '', verify_source: Callable[[], bool | None] | None = None):
        super().__init__(parent)
        identity = source_sha256 or (snapshot.source_sha256 if snapshot else '')
        if (not isinstance(identity, str) or not re.fullmatch(r'[a-f0-9]{64}', identity)
                or snapshot is not None and snapshot.source_sha256 != identity):
            raise RecognitionDraftError('Open recognition evidence for the exact selected recording before editing.')
        self.snapshot, self.source_path, self.source_sha256 = snapshot, Path(source_path), identity
        self.verify_source = verify_source
        self._worker = None
        self._close_after_save = False
        self.setObjectName('recognition_draft_dialog')
        self.setWindowTitle('Untimed recognition draft — review required')
        self.setAccessibleName('Untimed recognition draft editor')
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(920, 700)
        self.setMinimumSize(620, 480)
        layout = QVBoxLayout(self)
        self.notice = QLabel('UNTIMED · REVIEW REQUIRED\n'
            'Saving this text does not align words, replace saved lyrics, approve airing or publish anything. '
            'It cannot appear on Program Output as lyrics.')
        self.notice.setObjectName('recognition_draft_notice')
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        self.recording = QLabel('Recording: ' + (recording_title or self.source_path.name) + '\nSHA-256: ' + identity)
        self.recording.setObjectName('recognition_draft_recording')
        self.recording.setTextFormat(Qt.TextFormat.PlainText)
        self.recording.setWordWrap(True)
        layout.addWidget(self.recording)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName('Recognition draft and immutable evidence')
        self.text = QPlainTextEdit()
        self.text.setObjectName('recognition_draft_text')
        self.text.setAccessibleName('Untimed recognition draft text; editing does not align or approve lyrics')
        self.text.setPlainText(snapshot.text if snapshot else original_text)
        # Qt normalizes CRLF/paragraph separators for display. The unchanged
        # editor must not rewrite the raw saved snapshot solely for that.
        self._baseline_text = self.text.toPlainText()
        self.text.setReadOnly(snapshot is None)
        self.tabs.addTab(self.text, 'Edit untimed draft' if snapshot else 'Recognition text (read-only)')
        self.original = QPlainTextEdit()
        self.original.setObjectName('recognition_draft_original')
        self.original.setAccessibleName('Original recognition text, unchanged and read-only')
        self.original.setReadOnly(True)
        self.original.setPlainText(original_text)
        self.tabs.addTab(self.original, 'Original recognition')
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setObjectName('recognition_draft_diagnostics')
        self.diagnostics.setAccessibleName('Original failure diagnostics, unchanged and read-only')
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setPlainText(diagnostics_text)
        self.tabs.addTab(self.diagnostics, 'Failure details')
        layout.addWidget(self.tabs, 1)
        self.status = QLabel()
        self.status.setObjectName('recognition_draft_status')
        self.status.setAccessibleName('Recognition draft saved or unsaved status')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.save_button = self._button('Save draft &text', 'recognition_draft_save', self.save_draft)
        self.save_button.setToolTip('Save this separate untimed draft for the original recording (Ctrl+S). No filename picker.')
        buttons.addWidget(self.save_button)
        self.reload_button = self._button('&Reload saved', 'recognition_draft_reload', self.reload_saved)
        buttons.addWidget(self.reload_button)
        self.copy_button = self._button('&Copy draft text', 'recognition_draft_copy', self.copy_text)
        buttons.addWidget(self.copy_button)
        buttons.addStretch()
        self.close_button = self._button('Close', 'recognition_draft_close', self.close)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self.save_shortcut = QShortcut(QKeySequence.StandardKey.Save, self)
        self.save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.save_shortcut.activated.connect(self.save_draft)
        self.text.textChanged.connect(self._text_changed)
        self._text_changed()

    def _button(self, title, name, action):
        button = QPushButton(title)
        button.setObjectName(name)
        button.setAccessibleName(title.replace('&', ''))
        button.setAutoDefault(False)
        button.clicked.connect(action)
        return button

    @property
    def busy(self):
        return self._worker is not None

    @property
    def is_dirty(self):
        return self.snapshot is not None and self.text.toPlainText() != self._baseline_text

    def _controls(self):
        self.text.setReadOnly(self.snapshot is None or self.busy)
        self.save_button.setEnabled(self.snapshot is not None and self.is_dirty and not self.busy)
        self.reload_button.setEnabled(self.snapshot is not None and not self.busy)
        self.close_button.setEnabled(not self.busy)

    def _text_changed(self):
        self._controls()
        if self.busy:
            return
        if self.snapshot is None:
            self.status.setText('No editable draft was retained. This is read-only failure evidence; you can copy the text. No lyrics were saved or aligned.')
        elif self.is_dirty:
            self.status.setText('Unsaved draft text · Save draft text keeps this separate, untimed correction (Ctrl+S).')
        else:
            self.status.setText('Saved untimed draft · review and alignment are still required before any lyric approval.')

    def _selection_matches(self):
        try:
            return self.verify_source is None or bool(self.verify_source())
        except Exception:
            return False

    def _selection_notice(self):
        self.status.setText('The selected recording changed. Your text is kept here. Select this original recording again before saving or reloading.')

    def _source_stamp(self):
        info = self.source_path.stat()
        return (info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def _fresh_source(self):
        """Hash a stable source and return the verified metadata generation."""
        try:
            before = self._source_stamp()
            matches = fingerprint(self.source_path) == self.source_sha256
            after = self._source_stamp()
        except OSError as error:
            raise RecognitionDraftError('The original recording is missing or unreadable. Your draft text is kept; restore the matching recording before saving or reloading.') from error
        if not matches or before != after:
            raise RecognitionDraftError('The recording changed since this draft opened or while its identity was checked. Your text is kept; open the matching recording before saving or reloading.')
        return after

    def _final_source_stamp(self, original_stamp):
        # A successful write remains an original-SHA draft if the source later
        # changes. Return no current-source claim rather than an unsaved error.
        try:
            verified = self._fresh_source()
        except RecognitionDraftError:
            return None
        return verified if verified == original_stamp else None

    def _begin(self, action, work):
        task = _DraftOperation(work, self)
        self._worker = task
        self._controls()
        self.status.setText('Checking the exact recording and ' + ('saving draft text…' if action == 'save' else 'loading the saved draft…'))
        task.finished.connect(lambda: self._completed(task, action))
        task.start()

    def _completed(self, task, action):
        if task is not self._worker:
            return
        value, error = task.value, task.error
        self._worker = None
        task.deleteLater()
        selected = self._selection_matches()
        if error:
            self._close_after_save = False
            self._controls()
            self.status.setText(('Draft not saved · ' if action == 'save' else 'Draft not reloaded · ') + error + ' Your edit buffer is kept.')
            self.operation_finished.emit(action, False)
            return
        updated, verified_stamp = value
        try:
            # The worker may have finished before this queued callback runs.
            # Recheck the exact metadata generation whose bytes it hashed.
            source_unchanged = (verified_stamp is not None
                                and self._source_stamp() == verified_stamp)
        except OSError:
            source_unchanged = False
        if action == 'save':
            # Track a committed old-recording revision without replacing visible
            # text when selection changed while IO was pending.
            self.snapshot = updated
            self._baseline_text = updated.text
            self.saved.emit(updated)
            self._controls()
            if not selected or not source_unchanged:
                self._close_after_save = False
                self.status.setText('Untimed draft text was saved for the original recording, but the selected recording or its file changed. Your text is kept here; select the matching original recording before continuing.')
                self.operation_finished.emit(action, True)
                return
            self.status.setText('Saved draft text · separate and untimed. Existing lyrics, timing and approval are unchanged.')
            close = self._close_after_save
            self._close_after_save = False
            self.operation_finished.emit(action, True)
            if close:
                self.close()
            return
        self._close_after_save = False
        if not selected or not source_unchanged:
            self._controls()
            self._selection_notice()
            self.operation_finished.emit(action, False)
            return
        self.snapshot = updated
        self.text.setPlainText(updated.text)
        self._baseline_text = self.text.toPlainText()
        self._text_changed()
        self.status.setText('Reloaded saved untimed draft · no aligned lyrics or approval changed.')
        self.operation_finished.emit(action, True)

    def save_draft(self, checked=False, *, close_after=False):
        if self.busy or self.snapshot is None:
            return
        if not self._selection_matches():
            self._selection_notice()
            return
        if not self.is_dirty:
            self.status.setText('Draft text already matches the saved untimed revision.')
            if close_after:
                self.close()
            return
        opened, text = self.snapshot, self.text.toPlainText()
        self._close_after_save = close_after
        def work():
            original_stamp = self._fresh_source()
            saved = save_user_draft(opened.path, text, expected_revision=opened.revision,
                                    expected_source_sha256=self.source_sha256)
            return saved, self._final_source_stamp(original_stamp)
        self._begin('save', work)

    def reload_saved(self):
        if self.busy or self.snapshot is None:
            return
        if not self._selection_matches():
            self._selection_notice()
            return
        if self.is_dirty:
            answer = QMessageBox.question(self, 'Reload saved recognition draft?',
                'Replace this window’s unsaved draft text with the saved untimed revision?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                return
        opened = self.snapshot
        def work():
            original_stamp = self._fresh_source()
            loaded = load_draft(opened.path, expected_source_sha256=self.source_sha256)
            return loaded, self._final_source_stamp(original_stamp)
        self._begin('reload', work)

    def copy_text(self):
        QApplication.clipboard().setText(self.text.toPlainText())

    def reject(self):
        self.close()  # Escape must use the same discard guard.

    def closeEvent(self, event):
        if self.busy:
            self.status.setText('Wait for the current draft operation. Your text is kept here.')
            event.ignore()
            return
        if not self.is_dirty:
            event.accept()
            return
        answer = QMessageBox.question(self, 'Keep recognition draft edits?',
            'Save the separate untimed draft text before closing? Discard only removes this window’s unsaved edits.',
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Discard:
            event.accept()
        elif answer == QMessageBox.StandardButton.Save:
            event.ignore()
            self.save_draft(close_after=True)
        else:
            event.ignore()
