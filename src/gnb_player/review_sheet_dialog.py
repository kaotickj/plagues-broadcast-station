"""Paste one recording's sheet, inspect directions, save privately or align."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QComboBox, QSlider, QFileDialog, QMessageBox)
from copy import deepcopy
from .review_sheet import SheetStore, bracket_preview, make_sheet, alignment_plan, embedded_sources, import_text_file


class SuppliedLyricsDialog(QDialog):
    def __init__(self, review):
        super().__init__(review)
        self.review = review; self.store = SheetStore(review.store)
        self.saved_for_alignment = None; self.saved_for_untimed = None; self.manual = False; self.updating = False
        self.import_source=None
        self.rows = []; self.choices = {}; self.resize(980, 740); self.setMinimumSize(760, 600)
        self.setWindowTitle('Lyrics sheet · ' + review.track.title)
        layout = QVBoxLayout(self)
        identity = QLabel(f'{review.track.title}\nRecording: {review.track.path.name}\n'
                          f'{review.document.duration:.3f} seconds · audio {review.store.digest[:12]}')
        identity.setTextFormat(Qt.TextFormat.PlainText); identity.setWordWrap(True); layout.addWidget(identity)
        note = QLabel('Paste a sheet, import a text file, or use embedded lyrics. The right side shows the cleaned words. '
                      'Style/editorial notes are excluded; other bracketed words are kept unless you choose otherwise. '
                      'Save and use untimed needs no models or timestamps. Originals and prior drafts are preserved.')
        note.setWordWrap(True); layout.addWidget(note)
        sources=QHBoxLayout();self.file_button=QPushButton('Import text file…');self.file_button.clicked.connect(self.import_file);sources.addWidget(self.file_button)
        self.embedded_select=QComboBox();self.embedded_select.setAccessibleName('Embedded lyric source (unverified)')
        self.embedded_select.setMinimumContentsLength(12);self.embedded_select.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.embedded_values=embedded_sources(review.embedded)
        for value in self.embedded_values:self.embedded_select.addItem(value['label'])
        sources.addWidget(self.embedded_select,1)
        self.embedded_button=QPushButton('Use embedded lyrics');self.embedded_button.setEnabled(bool(self.embedded_values));self.embedded_button.clicked.connect(self.use_embedded);sources.addWidget(self.embedded_button)
        layout.addLayout(sources)
        self.source_note=QLabel('Source: pasted text. Embedded lyrics are unverified; media tags are never rewritten.')
        self.source_note.setTextFormat(Qt.TextFormat.PlainText);self.source_note.setWordWrap(True);layout.addWidget(self.source_note)
        row = QHBoxLayout(); self.play_button = QPushButton('Play / pause this recording')
        self.play_button.clicked.connect(review.toggle_play); row.addWidget(self.play_button)
        self.clock = QLabel(); row.addWidget(self.clock)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, round(review.document.duration * 1000))
        self.slider.setAccessibleName('Selected recording playback position')
        self.slider.sliderMoved.connect(lambda ms: review.seek(ms / 1000)); row.addWidget(self.slider, 1)
        layout.addLayout(row)
        split = QSplitter(); layout.addWidget(split, 1)
        from PySide6.QtWidgets import QWidget
        def editor(label):
            panel = QWidget(); box = QVBoxLayout(panel); box.addWidget(QLabel(label))
            field = QPlainTextEdit(); field.setAccessibleName(label); box.addWidget(field); split.addWidget(panel)
            return field
        self.original = editor('Original lyric sheet — preserved')
        self.original.setPlaceholderText('Paste untimed lyrics here, including any [Suno directions].')
        self.preview = editor('Cleaned lyrics — editable; timing optional')
        self.original.textChanged.connect(self.original_changed)
        self.preview.textChanged.connect(self.preview_changed)
        self.brackets = QTableWidget(0, 3); self.brackets.setHorizontalHeaderLabels(['Bracketed text', 'Handling', 'Suggestion'])
        self.brackets.setMaximumHeight(140); self.brackets.setAccessibleName('Bracket handling; original remains unchanged')
        layout.addWidget(self.brackets)
        self.refresh_button = QPushButton('Refresh preview from sheet and bracket choices')
        self.refresh_button.clicked.connect(self.rebuild_preview)
        cleanup=QHBoxLayout();cleanup.addWidget(self.refresh_button)
        self.exclude_all_button=QPushButton('Exclude all bracketed text');self.exclude_all_button.clicked.connect(self.exclude_all);cleanup.addWidget(self.exclude_all_button)
        layout.addLayout(cleanup)
        self.route = QLabel(); self.route.setWordWrap(True); layout.addWidget(self.route)
        scope = QLabel('Save and use untimed replaces only the private draft. Library approval stays separate. Save and align is optional.')
        scope.setWordWrap(True); layout.addWidget(scope)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.TextFormat.PlainText); layout.addWidget(self.status)
        buttons = QHBoxLayout(); self.save_button = QPushButton('Save sheet'); self.untimed_button=QPushButton('Save and use untimed');self.use_button = QPushButton('Save and align'); self.cancel_button = QPushButton('Cancel')
        for b in (self.save_button, self.untimed_button, self.use_button, self.cancel_button):
            b.setAutoDefault(False); buttons.addWidget(b)
        self.save_button.clicked.connect(lambda: self.save(False))
        self.untimed_button.clicked.connect(lambda:self.save(False,untimed=True))
        self.use_button.clicked.connect(lambda: self.save(True)); self.cancel_button.clicked.connect(self.reject)
        layout.addLayout(buttons)
        saved = self.store.load() or deepcopy(review.document.metadata.get('supplied_sheet'))
        self.original.setPlainText(saved['original'] if saved else '')
        if saved:
            self.import_source=deepcopy(saved.get('import_source'));self.show_source()
            self.choices = {i: r['action'] for i, r in enumerate(saved['brackets'])}
            self.render_brackets()
            self.updating = True; self.preview.setPlainText(saved['sung_text']); self.updating = False
            self.manual = saved.get('preview_manually_edited', False)
        self.update_route()
        self.timer = QTimer(self); self.timer.setInterval(30); self.timer.timeout.connect(self.render_clock); self.timer.start()
        self.render_clock() # never autoplay

    def render_clock(self):
        position = self.review.player.position()
        self.clock.setText(f'Playback clock: {position / 1000:.3f} s')
        if not self.slider.isSliderDown(): self.slider.setValue(position)

    def show_source(self):
        source=self.import_source or {}
        if source.get('kind')=='embedded_unverified':label='Embedded lyrics (unverified): '+source['selected']['label']
        elif source.get('kind')=='text_file':label='Imported text file: '+source['name']
        else:label='Pasted text'
        self.source_note.setText('Source: '+label+'. Original source content is preserved; no media tags are changed.')

    def load_input(self,text,source):
        if self.original.toPlainText() and (self.original.toPlainText()!=text or self.manual):
            answer=QMessageBox.question(self,'Replace sheet input?',
                'Replace this window’s input and preview? Saved sheets remain recoverable. Unsaved changes in this window will be discarded.',
                QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
            if answer!=QMessageBox.StandardButton.Ok:return
        self.import_source=deepcopy(source);self.manual=False
        self.original.setPlainText(text);self.choices={};self.render_brackets();self.rebuild_preview();self.show_source()

    def use_embedded(self):
        index=self.embedded_select.currentIndex()
        if 0<=index<len(self.embedded_values):
            selected=deepcopy(self.embedded_values[index])
            self.load_input(selected['text'],{'kind':'embedded_unverified','selected':selected,'trusted':False})

    def import_file(self):
        path,_=QFileDialog.getOpenFileName(self,'Import untimed lyric sheet','','Text lyrics (*.txt);;All files (*)')
        if path:
            try:
                text,source=import_text_file(path);self.load_input(text,source)
            except (OSError,ValueError) as error:self.status.setText(str(error)+' Current sheet retained.')

    def exclude_all(self):
        if self.manual:
            answer=QMessageBox.question(self,'Refresh cleaned text?',
                'Rebuild the preview from the original sheet, excluding all bracketed text?',
                QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
            if answer!=QMessageBox.StandardButton.Ok:return
        self.choices={i:'exclude' for i in range(len(bracket_preview(self.original.toPlainText())[1]))}
        self.render_brackets();self.rebuild_preview()

    def original_changed(self):
        if self.updating: return
        self.choices = {}; self.render_brackets()
        if not self.manual: self.rebuild_preview()
        else: self.status.setText('Original changed. Your edited sung preview was kept; refresh it only if you want to replace those edits.')

    def render_brackets(self):
        try: _, self.rows = bracket_preview(self.original.toPlainText(), self.choices)
        except ValueError as error: self.status.setText(str(error)); return
        # Qt defers deletion of replaced cell widgets; hide them immediately so
        # a refreshed source cannot leave a stale selector over the note text.
        for row in range(self.brackets.rowCount()):
            previous=self.brackets.cellWidget(row,1)
            if previous:previous.hide()
        self.brackets.setRowCount(len(self.rows))
        for index, row in enumerate(self.rows):
            for col, value in ((0, row['text']), (2, row['suggestion'])):
                item = QTableWidgetItem(value); item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.brackets.setItem(index, col, item)
            combo = QComboBox(); combo.addItem('Keep sung words', 'keep'); combo.addItem('Exclude direction', 'exclude')
            combo.setCurrentIndex(combo.findData(row['action']))
            combo.currentIndexChanged.connect(lambda _, i=index, c=combo: self.change_bracket(i, c.currentData()))
            self.brackets.setCellWidget(index, 1, combo)
        self.brackets.resizeColumnsToContents(); self.brackets.setVisible(bool(self.rows))

    def change_bracket(self, index, action):
        self.choices[index] = action
        if self.manual: self.status.setText('Bracket choice recorded. Your edited preview is unchanged; Refresh preview applies bracket choices again.')
        else: self.rebuild_preview()

    def rebuild_preview(self):
        try:
            preview, self.rows = bracket_preview(self.original.toPlainText(), self.choices)
            self.updating = True; self.preview.setPlainText(preview); self.updating = False; self.manual = False
            self.status.setText('Preview refreshed. Original sheet unchanged. You can edit sung words directly; line breaks are kept.')
        except ValueError as error: self.updating = False; self.status.setText(str(error))

    def preview_changed(self):
        if not self.updating: self.manual = True

    def sheet(self):
        _, rows = bracket_preview(self.original.toPlainText(), self.choices)
        return make_sheet(self.original.toPlainText(), self.preview.toPlainText(), rows,
                          source_sha256=self.review.store.digest, duration=self.review.document.duration,
                          title=self.review.track.title, manual_preview=self.manual,import_source=self.import_source)

    def update_route(self):
        duration = self.review.document.duration
        if duration > 180:
            self.route.setText('Optional alignment: whole-song CTC (installed local model). Qwen needs audio sections for songs over 3 minutes. No new transcription or invented section boundaries.')
        else:
            name = 'Qwen' if self.review.config.backend == 'qwen' else 'CTC'
            self.route.setText(f'Optional alignment: {name}, using the supplied sung words. No new transcription.')

    def save(self, align, *, untimed=False):
        try:
            sheet = self.sheet()
            if untimed and not sheet['sung_text'].strip():raise ValueError('Add sung words before using an untimed sheet.')
            if align: alignment_plan(sheet, self.review.document, self.review.config.backend)
            saved = self.store.save(sheet)
            self.status.setText('Sheet and sung preview saved for this recording. Timed draft and approved library lyrics unchanged.')
            if align:
                self.saved_for_alignment = saved
                self.accept()
            elif untimed:
                self.saved_for_untimed=saved
                self.accept()
        except Exception as error: self.status.setText(str(error))

    def done(self, result):
        self.timer.stop(); self.review.player.pause()
        super().done(result)
