"""Small, model-free editor for measured lyric lines and stored review notes."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal, QPersistentModelIndex
from PySide6.QtWidgets import QApplication, QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem, QStyledItemDelegate, QLineEdit


class LyricCellDelegate(QStyledItemDelegate):
    """Expose in-progress typing before the table commits its model value."""
    def createEditor(self, parent, option, index):
        editor = super().createEditor(parent, option, index)
        table = self.parent()
        table._active_cell_editor = editor
        table._active_cell_index = QPersistentModelIndex(index)
        if isinstance(editor, QLineEdit):
            # textEdited excludes the initial setEditorData value. Waiting for
            # itemChanged leaves Save disabled while the user is still typing.
            editor.textEdited.connect(lambda _: table.edited.emit())
        return editor

    def destroyEditor(self, editor, index):
        table = self.parent()
        active = table._active_cell_editor is editor
        if active:
            table._active_cell_editor = None
            table._active_cell_index = QPersistentModelIndex()
        super().destroyEditor(editor, index)
        if active:
            # Escape may close the delegate without committing. Refresh the
            # draft from the model so canceled typing is never saved later.
            table.edited.emit()


def seconds_text(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def parse_seconds(value: str) -> float:
    fields = value.strip().split(":")
    if len(fields) == 1:
        return float(fields[0])
    if len(fields) not in (2, 3):
        raise ValueError("Use seconds or m:ss for line times.")
    parts = [float(field) for field in fields]
    if any(not math.isfinite(part) or part < 0 for part in parts):
        raise ValueError("Line times must be finite and positive.")
    if any(part >= 60 for part in parts[1:]) or any(part != int(part) for part in parts[:-1]):
        raise ValueError("Use m:ss or h:mm:ss, with seconds below 60.")
    return sum(part * 60 ** index for index, part in enumerate(reversed(parts)))


class TimedLyricsEditor(QTableWidget):
    edited = Signal()

    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["Start (s)", "End (s)", "Lyric line — double-click or press F2 to edit"])
        self.setAccessibleName("Editable timed lyric lines. Start, end, and lyric text. Tab moves between cells; F2 edits.")
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._active_cell_editor = None
        self._active_cell_index = QPersistentModelIndex()
        self.setItemDelegate(LyricCellDelegate(self))
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(0, 82)
        self.setColumnWidth(1, 82)
        self.verticalHeader().setDefaultSectionSize(36)
        self.setMinimumHeight(135)
        self.itemChanged.connect(lambda _: self.edited.emit())

    def set_rows(self, rows):
        blocked = self.blockSignals(True)
        self.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                self.setItem(row, column, QTableWidgetItem(str(value)))
        self.blockSignals(blocked)

    def set_document(self, document):
        self.set_rows([(seconds_text(cue.start), seconds_text(cue.end), cue.text)
                       for cue in document.segments] if document else [])

    def raw_rows(self):
        rows = [[self.item(row, column).text() if self.item(row, column) else ""
                 for column in range(3)] for row in range(self.rowCount())]
        index = self._active_cell_index
        editor = self._active_cell_editor
        if index.isValid() and index.row() < len(rows) and isinstance(editor, QLineEdit) and editor.isVisible():
            rows[index.row()][index.column()] = editor.text()
        return [tuple(row) for row in rows]

    def commit_pending_edit(self):
        focused=QApplication.focusWidget()
        if focused is not None and focused is not self and self.isAncestorOf(focused):
            # Focus-out commits the delegate's current text before Ctrl+S or a
            # background result can read/repopulate the table.
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def parsed_rows(self):
        result = []
        for number, (start, end, text) in enumerate(self.raw_rows(), 1):
            try:
                result.append((parse_seconds(start), parse_seconds(end), text))
            except ValueError as error:
                raise ValueError(f"Line {number}: {error}") from error
        return result

    def toPlainText(self):
        return "\n".join(row[2] for row in self.raw_rows())

    def add_line(self, duration):
        rows = self.raw_rows()
        index = self.currentRow() + 1 if self.currentRow() >= 0 else len(rows)
        start = parse_seconds(rows[index - 1][1]) if index else 0
        limit = parse_seconds(rows[index][0]) if index < len(rows) else duration
        if limit - start < .05:
            raise ValueError("Make room with the adjacent line times before adding a line here.")
        rows.insert(index, (seconds_text(start), seconds_text(min(start + 2, limit)), ""))
        self.set_rows(rows)
        self.setCurrentCell(index, 2)
        self.edited.emit()
        self.editItem(self.item(index, 2))

    def remove_lines(self):
        selected = sorted({index.row() for index in self.selectedIndexes()}, reverse=True)
        if not selected:
            return
        blocked = self.blockSignals(True)
        for row in selected:
            self.removeRow(row)
        self.blockSignals(blocked)
        self.edited.emit()


def stored_review_notes(document) -> str:
    """Only attach passage context when an actual stored timestamp exists."""
    if document is None:
        return "Select a recording with saved lyrics to see its review notes."
    original = document.metadata.get("review_original_quality")
    quality = original if isinstance(original, dict) else document.quality or {}
    notes = []
    reasons = quality.get("reasons", [])
    reasons = reasons if isinstance(reasons, list) else []
    for reason in reasons:
        if not isinstance(reason, dict):
            continue
        message = reason.get("message") or ("Review flag: " + str(reason.get("code", "unspecified")))
        at = reason.get("at")
        if isinstance(at, (float, int)) and not isinstance(at, bool) and math.isfinite(at) and at >= 0:
            stamp = f"{int(at) // 60}:{at % 60:05.2f}"
            context = next(((index, cue) for index, cue in enumerate(document.segments, 1)
                            if cue.start <= at <= cue.end), None)
            passage = f' · line {context[0]}: “{context[1].text[:110]}”' if context else ""
            notes.append(f"{stamp} — {message}{passage}")
        else:
            notes.append(f"Summary — {message}")
    notes.extend("Model note — " + str(warning) for warning in document.warnings if warning)
    if not notes:
        notes.append("No passage-level review notes were stored. Listen through the recording before approving its lyrics.")
    elif not any(isinstance(reason, dict) and isinstance(reason.get("at"), (int, float))
                 and not isinstance(reason.get("at"), bool) and math.isfinite(reason["at"])
                 for reason in reasons):
        notes.append("These are saved summaries; no passage timestamps were stored.")
    edited = document.human_review.get("alignment_needed", [])
    edited = [index for index in edited if isinstance(index, int) and not isinstance(index, bool)
              and 0 <= index < len(document.segments)] if isinstance(edited, list) else []
    if edited:
        notes.append("Edited lines " + ", ".join(str(index + 1) for index in edited)
                     + " use line timing. Align edited text only if you need new word highlights.")
    return "\n\n".join(notes)
