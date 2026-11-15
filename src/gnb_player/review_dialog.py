"""Private one-track review; never uses the program player or starts a broadcast."""
from __future__ import annotations
from pathlib import Path
from dataclasses import asdict, replace
import copy, html, json, os, sys, uuid
from PySide6.QtCore import Qt, QUrl, QTimer, QProcess, QProcessEnvironment, Signal, QItemSelectionModel
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QPlainTextEdit,
    QCheckBox,QComboBox,QTableWidget,QTableWidgetItem,QAbstractItemView,QSlider,QFileDialog,
    QMessageBox,QSplitter,QWidget,QScrollArea,QTabWidget,QFormLayout,QDoubleSpinBox,QGroupBox,QLineEdit,QLayout,QStackedWidget)
from .lyrics import LyricsDocument, load_lyrics, word_is_active
from .review_core import (ReviewStore,ReviewConfig,embedded_lyrics,mark_unreviewed,edit_phrase,
                          set_phrase_bounds,review_markers,now,alignment_input,verify_job_revision,
                          hold_saved_repetition,exclude_held_passage,review_provenance_label,recover_word_highlights)
from .review_partial import partial_tokens
from .lyric_review import document_revision, is_approved
from .review_timing import edit_bounds, offset as timing_offset, reset_timing
from .review_vocabulary import stage_suggestions, decide_suggestion
from .recognition_policy import audio_only_policy
from .review_qwen import edit_section, qwen_alignment_input, use_supported_phrase_timing
from .review_passage import passage_alignment_input, merge_passage_alignment
from .settings import atomic_json
from .extraction import parse_worker_event

class LyricsReviewDialog(QDialog):
    approved = Signal(object,object)

    def __init__(self,track,data_dir,cache_dir,existing=None,parent=None):
        super().__init__(parent);self.track=track;self.data_dir=Path(data_dir);self.cache_dir=Path(cache_dir)
        self.store=ReviewStore(data_dir,track.path);self.config=ReviewConfig.discover(data_dir);self.store.bind_output(cache_dir)
        self.document=LyricsDocument([],track.duration,track.title,self.store.digest,quality={'status':'review'})
        self.embedded={};self.busy=False;self.dirty=False;self._closing=False;self._bytes=b'';self._stderr='';self._seen_result=False
        self._undo=[];self._redo=[];self._timing_baseline=copy.deepcopy(self.document);self._bound_origin=None
        self._job_revision=None;self._section_index=-1;self._loading_reference=False
        self._job_request=None;self._recoverable_candidate=None;self._preview_stop_at=None
        self.setWindowTitle('Review lyrics · '+track.title);self.resize(1120,820);self.setMinimumSize(850,650)
        self.setStyleSheet('QPushButton { padding: 5px 8px; min-height: 22px; } QDoubleSpinBox { min-height: 26px; padding: 2px 4px; } QComboBox { min-height: 26px; } QPlainTextEdit { padding: 4px; }')
        self.player=QMediaPlayer(self);self.audio=QAudioOutput(self);self.player.setAudioOutput(self.audio);self.audio.setVolume(.7)
        self.process=QProcess(self);self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._output);self.process.readyReadStandardError.connect(self._error_output)
        self.process.finished.connect(self._finished);self.process.errorOccurred.connect(self._process_error)
        self._build_ui();self.player.positionChanged.connect(self.render_clock);self.player.playbackStateChanged.connect(lambda _:self.render_clock())
        self.player.durationChanged.connect(self._duration);self.player.mediaStatusChanged.connect(lambda _:self.render_clock())
        self.player.errorOccurred.connect(lambda _:self.status.setText('Audio preview: '+self.player.errorString()))
        self.timer=QTimer(self);self.timer.setInterval(30);self.timer.timeout.connect(self.render_clock);self.timer.start()
        self.player.setSource(QUrl.fromLocalFile(str(track.path.resolve()))) # no play()
        try:
            self.embedded=embedded_lyrics(track.path)
            atomic_json(self.store.root/'embedded-source.json',self.embedded)
            self.source_label.setText('Embedded lyrics found — unverified; original tags preserved.' if self.embedded['text'] else 'No embedded lyrics. Extract & align runs automatically; supplying words is optional.')
            self.reference.setPlainText(self.embedded['text'])
        except Exception as e:
            self.source_label.setText('Tag check failed: '+str(e));self.embedded={'kind':'probe_failed','text':'','trusted':False,'error':str(e)}
        self.ignore_tags.setVisible(bool(self.embedded.get('text')) or self.embedded.get('kind')=='probe_failed')
        try:
            draft=self.store.load()
            if draft:self.set_document(draft)
            elif existing:
                doc=copy.deepcopy(existing);doc.metadata['imported_prior_state']=doc.metadata.get('review',{})
                self.set_document(mark_unreviewed(doc,'Existing lyrics copied for review; stored/generated status is not accuracy evidence.'))
            else:self.refresh()
        except Exception as e:self.status.setText(str(e))

    def button(self,label,slot):
        b=QPushButton(label);b.setAccessibleName(label);b.clicked.connect(slot);return b

    def _build_ui(self):
        layout=QVBoxLayout(self);title=QLabel(self.track.title);title.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(title)
        notice=QLabel('Private review only. Unknown tracks are not automatically classified. Unflagged words may still be wrong. Approve explicitly before using this candidate on Program Output.');notice.setWordWrap(True);layout.addWidget(notice)
        self.source_label=QLabel();self.source_label.setWordWrap(True);self.source_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.source_label)
        self.provenance_label=QLabel();self.provenance_label.setWordWrap(True);self.provenance_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.provenance_label)
        row=QHBoxLayout();self.instrumental=QCheckBox('I confirm this recording is instrumental');self.instrumental.toggled.connect(self.designate_instrumental);row.addWidget(self.instrumental)
        self.paste_button=self.button('Paste lyrics',self.open_sheet);row.addWidget(self.paste_button)
        self.import_button=self.button('Import timed lyrics',self.import_timed);row.addWidget(self.import_button);self.prepare_button=self.button('Hide preparation',self.toggle_preparation);row.addWidget(self.prepare_button);layout.addLayout(row)
        self.preparation=QWidget();prep_outer=QVBoxLayout(self.preparation);prep_outer.setContentsMargins(0,0,0,0);prep_outer.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        prep_inputs=QWidget();prep=QVBoxLayout(prep_inputs);prep.setContentsMargins(0,0,0,0)
        prep_scroll=QScrollArea();prep_scroll.setWidgetResizable(True);prep_scroll.setWidget(prep_inputs)
        prep_scroll.setMinimumHeight(40);prep_scroll.setMaximumHeight(185)
        prep_scroll.setAccessibleName('Preparation inputs; scroll for text and alignment options')
        prep_outer.addWidget(prep_scroll)
        self.ignore_tags=QCheckBox('I checked the embedded source; transcribe audio instead');prep.addWidget(self.ignore_tags)
        self.supplied_options=QCheckBox('Use supplied lyrics / edit extracted sections (optional)');prep.addWidget(self.supplied_options)
        self.reference=QPlainTextEdit();self.reference.setFixedHeight(65);self.reference.setPlaceholderText('Separate supplied text. Current-draft alignment reads the phrase table, not this box.');self.reference.setAccessibleName('Supplied lyric text for explicit whole-draft alignment');prep.addWidget(self.reference)
        self.reference.textChanged.connect(self.reference_changed)
        row=QHBoxLayout();self.backend_select=QComboBox();self.backend_select.addItem('Qwen · automatic extraction & alignment','qwen');self.backend_select.addItem('Heart / CTC · previous engine','heart_ctc');self.backend_select.setCurrentIndex(max(0,self.backend_select.findData(self.config.backend)));self.backend_select.currentIndexChanged.connect(self.select_backend);row.addWidget(self.backend_select)
        self.section_select=QComboBox();self.section_select.setAccessibleName('Extracted audio section for text correction');self.section_select.currentIndexChanged.connect(self.select_text_section);row.addWidget(self.section_select,1);prep.addLayout(row)
        self.replace_timing=QCheckBox('Replace ALL existing timing, including manual edits; preserve previous draft in history');prep.addWidget(self.replace_timing)
        self.repeat_override=QCheckBox('I listened: repeated supplied text is intentional; align it for review');prep.addWidget(self.repeat_override)
        compute=QHBoxLayout();self.review_device_select=QComboBox();self.review_device_select.setAccessibleName('Review processing device')
        self.review_device_select.addItem('CPU · FP32 (default)','cpu');self.review_device_select.addItem('AMD GPU · ROCm FP32 (optional)','rocm')
        self.review_device_select.setCurrentIndex(max(0,self.review_device_select.findData(self.config.review_device)))
        self.review_device_select.currentIndexChanged.connect(self.select_review_device);compute.addWidget(self.review_device_select)
        self.review_gpu_python=QLineEdit(self.config.review_gpu_python);self.review_gpu_python.setAccessibleName('AMD review Python executable');self.review_gpu_python.setPlaceholderText('Separate ROCm + Qwen Python runtime');compute.addWidget(self.review_gpu_python,1)
        self.save_review_compute_button=self.button('Save review compute settings',self.save_review_compute);compute.addWidget(self.save_review_compute_button);prep_outer.addLayout(compute)
        self.review_gpu_python.setEnabled(self.config.review_device=='rocm');self.review_device_select.setEnabled(self.config.backend=='qwen')
        row=QHBoxLayout();self.extract_button=self.button('Extract & align',self.begin_automatic);self.align_button=self.button('Realign current draft',self.align_current);self.supplied_align_button=self.button('Align supplied text',lambda:self.start_job('align',source='supplied'));self.cancel_button=self.button('Cancel model job',self.cancel_job);self.cancel_button.setEnabled(False)
        for w in [self.extract_button,self.align_button,self.supplied_align_button,self.cancel_button]:row.addWidget(w)
        prep_outer.addLayout(row);layout.addWidget(self.preparation)
        self.supplied_options.toggled.connect(self.show_supplied_options);self.show_supplied_options(False)
        self.held_panel=QWidget();held_layout=QVBoxLayout(self.held_panel)
        held_note=QLabel('These items remain in the raw evidence. An untimed word has no usable highlight interval; a repetition warning concerns recognition. Processing windows are not measured word times.');held_note.setWordWrap(True);held_layout.addWidget(held_note)
        self.held_select=QComboBox();self.held_select.currentIndexChanged.connect(self.show_held);held_layout.addWidget(self.held_select)
        self.held_text=QPlainTextEdit();self.held_text.setReadOnly(True);self.held_text.setMinimumHeight(100);held_layout.addWidget(self.held_text)
        self.held_jump=self.button('Listen near held passage',self.seek_held);self.held_exclude=self.button('I listened: exclude this text',self.exclude_held);self.held_copy=self.button('Copy complete text for correction',self.copy_review_text)
        for w in [self.held_jump,self.held_exclude,self.held_copy]:held_layout.addWidget(w)
        self.clock=QLabel('0.000 s');self.clock.setStyleSheet('font-size:22px;font-weight:600');layout.addWidget(self.clock)
        row=QHBoxLayout();self.play_button=self.button('Play / pause preview',self.toggle_play);row.addWidget(self.play_button)
        for seconds in [-1,-.05,.05,1]:row.addWidget(self.button(f'Seek {seconds:+g} s',lambda checked=False,n=seconds:self.seek(self.player.position()/1000+n)))
        self.jump=QComboBox();self.jump.setAccessibleName('Jump to flagged passage or gap');self.jump.activated.connect(lambda i:self.seek(float(self.jump.itemData(i))));row.addWidget(self.jump,1);layout.addLayout(row)
        self.slider=QSlider(Qt.Orientation.Horizontal);self.slider.setRange(0,round(self.track.duration*1000));self.slider.setAccessibleName('Private preview position');self.slider.sliderMoved.connect(lambda n:self.seek(n/1000));self.slider.actionTriggered.connect(self._slider_action);layout.addWidget(self.slider)
        self.highlight=QLabel('Waiting for lyrics');self.highlight.setWordWrap(True);self.highlight.setMinimumHeight(45);self.highlight.setTextFormat(Qt.TextFormat.RichText);layout.addWidget(self.highlight)
        self.span_label=QLabel();layout.addWidget(self.span_label)
        split=QSplitter();self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['Start','End','Phrase','Review hints']);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection);self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.setAccessibleName('Timed phrases; select one to edit');self.table.itemSelectionChanged.connect(self.select_phrase);self.lyric_views=QStackedWidget();self.lyric_views.addWidget(self.table)
        self.untimed_view=QPlainTextEdit();self.untimed_view.setReadOnly(True);self.untimed_view.setAccessibleName('Untimed lyric sheet; no synchronized highlighting');self.lyric_views.addWidget(self.untimed_view);split.addWidget(self.lyric_views)
        self.edit_tabs=QTabWidget();split.addWidget(self.edit_tabs)
        text_panel=QWidget();right=QVBoxLayout(text_panel);self.phrase=QPlainTextEdit();self.phrase.setMinimumHeight(70);self.phrase.setPlaceholderText('Select a phrase to correct its words. Empty text removes only that phrase.');right.addWidget(self.phrase)
        self.apply_button=self.button('Apply phrase correction',self.apply_phrase);right.addWidget(self.apply_button)
        tip=QLabel('Word-count changes preserve grouped timing and gaps. Use Timing to adjust a word, group or passage.');tip.setWordWrap(True);right.addWidget(tip);self.edit_tabs.addTab(text_panel,'Text')
        timing_panel=QWidget();timing=QVBoxLayout(timing_panel);timing.setContentsMargins(6,6,6,6)
        self.timing_target=QComboBox();self.timing_target.setAccessibleName('Timing target: phrase, word or group');self.timing_target.currentIndexChanged.connect(self.select_timing_target);timing.addWidget(self.timing_target)
        form=QFormLayout();self.start_bound=QDoubleSpinBox();self.end_bound=QDoubleSpinBox()
        for spin in [self.start_bound,self.end_bound]:spin.setDecimals(3);spin.setRange(0,max(1,self.track.duration));spin.setSingleStep(.01)
        form.addRow('Start (seconds)',self.start_bound);form.addRow('End (seconds)',self.end_bound);timing.addLayout(form)
        row=QHBoxLayout();self.bounds_button=self.button('Apply start / end',self.apply_bounds);row.addWidget(self.bounds_button)
        self.listen_selected_button=self.button('Seek to selection',self.seek_selection);row.addWidget(self.listen_selected_button);timing.addLayout(row)
        row=QHBoxLayout();self.start_now_button=self.button('Start at playback',lambda:self.bound_at_playback('start'));self.end_now_button=self.button('End at playback',lambda:self.bound_at_playback('end'));row.addWidget(self.start_now_button);row.addWidget(self.end_now_button);timing.addLayout(row)
        row=QHBoxLayout();row.addWidget(QLabel('Move'));self.timing_scope=QComboBox();self.timing_scope.addItems(['Selected item','Selected phrases','Whole draft']);self.timing_scope.setAccessibleName('Explicit timing offset scope');row.addWidget(self.timing_scope,1);timing.addLayout(row)
        row=QHBoxLayout();self.earlier_button=self.button('Earlier −0.050 s',lambda:self.move_timing(-.05));self.later_button=self.button('Later +0.050 s',lambda:self.move_timing(.05));row.addWidget(self.earlier_button);row.addWidget(self.later_button);timing.addLayout(row)
        row=QHBoxLayout();self.offset_seconds=QDoubleSpinBox();self.offset_seconds.setDecimals(3);self.offset_seconds.setRange(-86400,86400);self.offset_seconds.setSingleStep(.05);self.offset_seconds.setSuffix(' s');self.offset_seconds.setAccessibleName('Timing offset in seconds; positive is later');row.addWidget(self.offset_seconds)
        self.offset_button=self.button('Apply offset',lambda:self.move_timing(self.offset_seconds.value()));row.addWidget(self.offset_button);timing.addLayout(row)
        tip=QLabel('Bounds change outer edges; offsets move all selected anchors together. Internal gaps and instrumental exclusions remain. Ctrl/Shift selects multiple phrases.');tip.setWordWrap(True);timing.addWidget(tip)
        self.group_timing_button=self.button('Use supported phrase timing',self.group_uncertain_timing);self.group_timing_button.setToolTip('After listening, group a phrase with unresolved word times over its existing supported intervals. No individual word times are invented.');timing.addWidget(self.group_timing_button)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(timing_panel);self.edit_tabs.addTab(scroll,'Timing (advanced)');self.edit_tabs.setCurrentIndex(0)
        held_scroll=QScrollArea();held_scroll.setWidgetResizable(True);held_scroll.setWidget(self.held_panel);self.edit_tabs.addTab(held_scroll,'Held passages')
        names=QWidget();names_layout=QVBoxLayout(names)
        note=QLabel('Automatic vocabulary hints are OFF. Load a recording-specific profile to review known name confusions. Suggestions never enter the recognizer prompt or replace words without your decision.');note.setWordWrap(True);names_layout.addWidget(note)
        names_layout.addWidget(self.button('Load track vocabulary suggestions',self.load_vocabulary))
        self.name_select=QComboBox();self.name_select.setAccessibleName('Track-specific name suggestions');self.name_select.currentIndexChanged.connect(self.show_name_suggestion);names_layout.addWidget(self.name_select)
        self.name_evidence=QPlainTextEdit();self.name_evidence.setReadOnly(True);self.name_evidence.setAccessibleName('Original phrase and proposed name correction');names_layout.addWidget(self.name_evidence)
        row=QHBoxLayout();self.name_accept=self.button('Accept selected name correction',lambda:self.decide_name(True));self.name_reject=self.button('Reject suggestion; keep words',lambda:self.decide_name(False));row.addWidget(self.name_accept);row.addWidget(self.name_reject);names_layout.addLayout(row)
        names_layout.addWidget(self.button('Listen near suggested name',self.seek_name));self.edit_tabs.addTab(names,'Names')
        self.table.cellClicked.connect(self.open_phrase_correction)
        self.passage_actions=QWidget();passage_row=QHBoxLayout(self.passage_actions);passage_row.setContentsMargins(0,0,0,0)
        self.listen_passage_button=self.button('Listen to this passage',self.listen_passage);self.realign_passage_button=self.button('Re-align this passage',self.realign_passage)
        self.realign_passage_button.setToolTip('Uses the edited words in this selected passage. The rest of the track keeps its timing. No timestamps to enter.')
        passage_row.addWidget(self.listen_passage_button);passage_row.addWidget(self.realign_passage_button)
        self.recover_words_button=self.button('Recover word highlights',self.recover_highlights)
        self.recover_words_button.setToolTip('Recover individual word highlights from native times already stored in this draft. All recoverable automatic groups are handled together. No model run or invented timestamps; Undo restores the prior draft.')
        passage_row.addWidget(self.recover_words_button)
        split.setSizes([650,430]);layout.addWidget(split,1);layout.addWidget(self.passage_actions)
        row=QHBoxLayout();self.undo_button=self.button('Undo edit',self.undo_edit);self.redo_button=self.button('Redo edit',self.redo_edit);self.reset_timing_button=self.button('Reset timing to opened candidate',self.reset_opened_timing)
        for widget in [self.undo_button,self.redo_button,self.reset_timing_button]:row.addWidget(widget)
        layout.addLayout(row)
        row=QHBoxLayout();self.save_button=self.button('Save private draft',self.save_draft);row.addWidget(self.save_button);self.restore_button=self.button('Restore saved draft',self.restore_draft);row.addWidget(self.restore_button);self.confirm=QCheckBox('Approve this candidate for output');row.addWidget(self.confirm,1);self.approve_button=self.button('Approve & use in library',self.approve);row.addWidget(self.approve_button);layout.addLayout(row)
        self.status=QLabel('Ready — choose CPU or optional AMD GPU for new review jobs. No autoplay.');self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        row=QHBoxLayout();self.raw_button=self.button('View raw evidence / original text',self.show_evidence);row.addWidget(self.raw_button)
        self.check_repetition_button=self.button('Check saved extraction for repetition',self.check_saved_repetition);row.addWidget(self.check_repetition_button);layout.addLayout(row)

    def toggle_preparation(self):
        show=self.preparation.isHidden();self.preparation.setVisible(show)
        self.prepare_button.setText('Hide preparation' if show else 'Show preparation')

    def set_document(self,document):
        if document.source_sha256 and document.source_sha256!=self.store.digest:raise ValueError('Candidate belongs to another recording.')
        self.document=document;self.document.source_sha256=self.store.digest
        self._section_index=-1;self.section_select.blockSignals(True);self.section_select.clear()
        sections=document.metadata.get('recognition_regions',[]) if not document.segments else []
        for i,s in enumerate(sections):self.section_select.addItem(f"Section {i+1} · {s['start']:.1f}–{s['end']:.1f} s",i)
        self.section_select.blockSignals(False);self.section_select.setVisible(bool(sections) and self.supplied_options.isChecked())
        if sections:self.select_text_section(0)
        elif 'supplied_text_draft' in document.metadata:
            self._loading_reference=True;self.reference.setPlainText(document.metadata['supplied_text_draft']);self._loading_reference=False
        elif document.metadata.get('provenance',{}).get('mode')=='align':
            self._loading_reference=True;self.reference.setPlainText(document.metadata['provenance'].get('reference_original',''));self._loading_reference=False
        self.align_button.setText('Retry alignment' if sections else 'Realign current draft')
        self.instrumental.blockSignals(True);self.instrumental.setChecked(bool(document.metadata.get('instrumental')));self.instrumental.blockSignals(False)
        # Supplied text is a separate input and never silently replaces table edits.
        self.replace_timing.setChecked(False);self.repeat_override.setChecked(False)
        show=not bool(document.segments) and not bool(document.metadata.get('instrumental'));self.preparation.setVisible(show);self.prepare_button.setText('Hide preparation' if show else 'Show preparation')
        self._undo=[];self._redo=[];self._timing_baseline=copy.deepcopy(document)
        self.confirm.setChecked(False);self.dirty=False;self.refresh()
        self.show_supplied_options(self.supplied_options.isChecked())
        if document.metadata.get('held_passages'):self.edit_tabs.setCurrentIndex(2)

    def show_supplied_options(self,show):
        for widget in (self.reference,self.supplied_align_button,self.repeat_override):widget.setVisible(show)
        self.section_select.setVisible(show and bool(self.document.metadata.get('recognition_regions')) and not self.document.segments)

    def begin_automatic(self):
        if self.busy:return
        if self.instrumental.isChecked():
            self.status.setText('Known instrumental: generation skipped. Existing designation retained.');return
        if self.document.segments or self.document.metadata.get('recognition_regions'):
            answer=QMessageBox.question(self,'Create a new timed candidate?',
                'Extract and align this recording again? The previous draft stays in history; approved output is unchanged.',
                QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
            if answer!=QMessageBox.StandardButton.Yes:return
            self.replace_timing.setChecked(True)
        # Tags were inspected and preserved during opening. This explicit audio
        # extraction action does not silently treat generated tags as a reference.
        self.ignore_tags.setChecked(True)
        self.start_job('extract')

    def open_phrase_correction(self,row,column=0):
        if 0<=row<len(self.document.segments):
            self.table.selectRow(row);self.edit_tabs.setCurrentIndex(0)
            self.seek(max(0,self.document.segments[row].start-1))
            self.status.setText('Edit the selected words, then Re-align this passage. Other timings stay unchanged. Undo is available.')

    def listen_passage(self):
        row=self.table.currentRow()
        if not 0<=row<len(self.document.segments):self.status.setText('Select a phrase first.');return
        cue=self.document.segments[row];self._preview_stop_at=min(self.document.duration,cue.end+1)
        self.seek(max(0,cue.start-1));self.player.play()

    def realign_passage(self):
        if self.busy:return
        self.apply_phrase()
        self.start_job('align',source='passage')

    def recover_highlights(self):
        if self.busy:return
        try:
            changed=recover_word_highlights(self.document)
            if changed.to_dict()==self.document.to_dict():
                self.status.setText('No recoverable automatic groups. Existing timing is unchanged.');return
            self._accept_edit(changed,'Stored individual word highlights recovered. Words without timing remain plain; no timestamps were guessed. Undo is available')
        except Exception as e:self.status.setText(str(e)+' Existing draft retained.')

    def select_review_device(self,*unused):
        self.config.review_device=self.review_device_select.currentData()
        self.review_gpu_python.setEnabled(self.config.review_device=='rocm')
        self.status.setText(self.review_device_select.currentText()+' selected for new jobs. Save to remember this choice. No model started.')

    def save_review_compute(self):
        from .studio_settings import studio_lock
        try:
            self.config.review_device=self.review_device_select.currentData();self.config.review_gpu_python=self.review_gpu_python.text().strip()
            self.config.validate_compute()
            if self.config.review_device=='rocm':self.config.for_job()
            path=self.data_dir/'lyrics-review-runtime.json'
            with studio_lock(self.data_dir):
                saved=json.loads(path.read_text()) if path.exists() else {}
                if not isinstance(saved,dict):raise ValueError('Review settings must be a JSON object.')
                saved.update(review_device=self.config.review_device,review_gpu_python=self.config.review_gpu_python,review_precision='float32')
                atomic_json(path,saved)
            self.status.setText('Review compute settings saved. Applies to new jobs only; nothing started.')
        except Exception as error:self.status.setText('Could not save review compute settings: '+str(error))

    def select_backend(self,*unused):
        self.config.backend=self.backend_select.currentData()
        self.review_device_select.setEnabled(self.config.backend=='qwen')
        if self.config.backend!='qwen':self.review_device_select.setCurrentIndex(0)
        self.status.setText('Selected '+self.backend_select.currentText()+'. '+self.review_device_select.currentText()+'. Applies to new jobs only.')

    def reference_changed(self):
        if not self._loading_reference:
            self.dirty=True;self.confirm.setChecked(False)

    def commit_text_section(self):
        if self._section_index>=0 and not self.document.segments:
            changed=edit_section(self.document,self._section_index,self.reference.toPlainText())
            if changed.to_dict()!=self.document.to_dict():
                self._undo.append(copy.deepcopy(self.document));self._redo=[];self.document=changed
        elif not self._loading_reference:
            self.document.metadata['supplied_text_draft']=self.reference.toPlainText()

    def select_text_section(self,index):
        if self._loading_reference:return
        if self._section_index>=0:self.commit_text_section()
        sections=self.document.metadata.get('recognition_regions',[])
        self._section_index=index if 0<=index<len(sections) else -1
        if self._section_index>=0:
            self._loading_reference=True;self.reference.setPlainText(sections[index]['text']);self._loading_reference=False
            self.reference.setPlaceholderText('Optional correction to this extracted section. Retry alignment uses saved words without requiring edits.')

    def group_uncertain_timing(self):
        try:self._accept_edit(use_supported_phrase_timing(self.document,self.table.currentRow()),'Supported phrase timing selected; individual word boundaries remain unknown')
        except Exception as e:self.status.setText(str(e))

    def refresh(self):
        self.provenance_label.setText(review_provenance_label(self.document))
        untimed=bool(self.document.untimed_text)
        self.untimed_view.setPlainText(self.document.untimed_text);self.lyric_views.setCurrentIndex(1 if untimed else 0)
        self.edit_tabs.setVisible(not untimed);self.passage_actions.setVisible(not untimed)
        self.align_button.setText('Align this sheet (optional)' if untimed else ('Retry alignment' if self._section_index>=0 else 'Realign current draft'))
        self.confirm.setText('Approve this untimed sheet for the library' if untimed else 'Approve this candidate for output')
        selected=self.selected_rows();row=self.table.currentRow();word=self.timing_target.currentData()
        self.table.blockSignals(True);self.table.setRowCount(len(self.document.segments))
        for i,cue in enumerate(self.document.segments):
            flags=list(dict.fromkeys(f for w in cue.words for f in w.metadata.get('flags',[])))
            if cue.metadata.get('native_timing_issues'):
                try: partial=partial_tokens(cue)
                except ValueError: partial=None
                flags.append('some words have no individual highlight; usable word times retained' if partial else 'unresolved native timing — raw evidence retained')
            for j,text in enumerate([f'{cue.start:.3f}',f'{cue.end:.3f}',cue.text,', '.join(flags)]):self.table.setItem(i,j,QTableWidgetItem(text))
        if self.document.segments:
            row=min(max(0,row),len(self.document.segments)-1);self.table.setCurrentCell(row,0);self.table.clearSelection()
            for i in selected or [row]:
                if i<len(self.document.segments):self.table.selectionModel().select(self.table.model().index(i,0),QItemSelectionModel.SelectionFlag.Select|QItemSelectionModel.SelectionFlag.Rows)
        self.table.blockSignals(False);self.select_phrase(preserve_word=word);self.table.resizeColumnsToContents();self.table.horizontalHeader().setStretchLastSection(True)
        self.jump.clear()
        for t,label in review_markers(self.document):self.jump.addItem(label,t)
        held=self.document.metadata.get('held_passages',[]);self.held_select.blockSignals(True);self.held_select.clear()
        for i,h in enumerate(held):
            kind='Untimed word' if h.get('detector')=='unplaced_native_word' else 'Incomplete extraction' if h.get('detector')=='decoder_termination' else 'Repetition warning'
            self.held_select.addItem(f"{kind}: {h['text'][:70]} · {h['status']}",i)
        self.held_select.blockSignals(False);self.edit_tabs.setTabEnabled(2,bool(held));self.show_held()
        self.name_select.blockSignals(True);self.name_select.clear()
        for s in self.document.metadata.get('vocabulary_review',{}).get('suggestions',[]):
            self.name_select.addItem(f"{s['status']} · {s['match_text']} → {s['replacement']}",s['id'])
        self.name_select.blockSignals(False);self.show_name_suggestion()
        status='approved' if is_approved(self.document,self.store.digest) else self.document.metadata.get('review',{}).get('status','unreviewed')
        unresolved=sum(h.get('status')=='held' for h in held)
        self.status.setText(f'{status.upper()} · {len(self.document.segments)} phrases · {unresolved} held items. Advisory flags do not require line-by-line edits. Approve shows one summary of any untimed words.')
        if untimed:self.status.setText('Untimed lyric sheet · no timestamps or synchronized highlighting. Use Paste lyrics to edit. Alignment is optional; library approval is separate.')
        recoverable=any(w.metadata.get('native_timing_issues_before_automatic_grouping') for c in self.document.segments for w in c.words)
        self.recover_words_button.setVisible(recoverable)
        self.instrumental.blockSignals(True);self.instrumental.setChecked(bool(self.document.metadata.get('instrumental')));self.instrumental.blockSignals(False)
        self.set_busy(self.busy);self.render_clock()

    def _duration(self,ms):self.slider.setMaximum(ms or round(self.document.duration*1000))
    def _slider_action(self,action):
        if action!=QSlider.SliderAction.SliderNoAction:self.seek(self.slider.sliderPosition()/1000)
    def seek(self,seconds):
        self.player.pause();limit=(self.player.duration()/1000 or self.document.duration or self.track.duration)
        self.player.setPosition(round(max(0,min(limit,seconds))*1000));self.render_clock()
    def toggle_play(self):
        if self.busy:return
        self._preview_stop_at=None
        if self.player.playbackState()==QMediaPlayer.PlaybackState.PlayingState:self.player.pause()
        else:self.player.play()
    def render_clock(self,*unused):
        t=self.player.position()/1000;self.clock.setText(f'{t:.3f} s · playback clock (not guaranteed sample-accurate)')
        if not self.slider.isSliderDown():self.slider.blockSignals(True);self.slider.setValue(round(t*1000));self.slider.blockSignals(False)
        if self._preview_stop_at is not None and t>=self._preview_stop_at:
            self._preview_stop_at=None;self.player.pause()
        if self.document.untimed_text:
            self.highlight.setText('Untimed lyrics — read the sheet below.')
            self.span_label.setText('No timed cues or active word. Playback does not advance these lyrics.');return
        cue=self.document.active_cue(t);word=self.document.active_word(t)
        try: partial=partial_tokens(cue) if cue else None
        except ValueError: partial=None
        if cue and cue.metadata.get('native_timing_issues') and partial is None:word=None
        if cue:
            if partial is not None:
                self.highlight.setText(' '.join('<span style="text-decoration:underline;color:#ffcf87">'+html.escape(token)+'</span>' if index is not None and word_is_active(cue.words[index],t) else html.escape(token) for token,index in partial))
            elif cue.words and ' '.join(w.text for w in cue.words).split()==cue.text.split():
                self.highlight.setText(' '.join('<span style="text-decoration:underline;color:#ffcf87">'+html.escape(w.text)+'</span>' if word_is_active(w,t) else html.escape(w.text) for w in cue.words))
            else:self.highlight.setText(html.escape(cue.text))
        else:self.highlight.setText('♪ Instrumental designation — no lyrics' if self.instrumental.isChecked() else 'Waiting / no timed lyric span')
        self.span_label.setText(f'{word.start:.3f} → {word.end:.3f} s'+(' · grouped phrase; individual new boundaries unknown' if word.metadata.get('grouped_text') else '') if word else 'Some words have no individual time; text remains plain' if partial is not None else 'Unresolved word timing — phrase text only' if cue and cue.metadata.get('native_timing_issues') else 'No active word highlight')

    def selected_rows(self):
        return sorted({index.row() for index in self.table.selectionModel().selectedRows()})

    def select_phrase(self,*,preserve_word=None):
        i=self.table.currentRow();self.timing_target.blockSignals(True);self.timing_target.clear()
        if 0<=i<len(self.document.segments):
            cue=self.document.segments[i];self.phrase.setPlainText(cue.text)
            self.timing_target.addItem('Phrase — outer start / end',None)
            for n,word in enumerate(cue.words):
                label='Group' if word.metadata.get('grouped_text') else 'Word'
                self.timing_target.addItem(f'{label} {n+1}: {word.text}',n)
            if preserve_word is not None and preserve_word<len(cue.words):self.timing_target.setCurrentIndex(preserve_word+1)
        else:self.phrase.clear()
        self.timing_target.blockSignals(False);self.select_timing_target()

    def select_timing_target(self,*unused):
        i=self.table.currentRow();self._bound_origin=None
        if 0<=i<len(self.document.segments):
            cue=self.document.segments[i];wi=self.timing_target.currentData();item=cue if wi is None else cue.words[wi]
            self._bound_origin=(item.start,item.end);self.start_bound.setValue(item.start);self.end_bound.setValue(item.end)

    def _accept_edit(self,document,message):
        if document.to_dict()==self.document.to_dict():return
        self._undo.append(copy.deepcopy(self.document));self._undo=self._undo[-100:];self._redo=[]
        self.document=document;self.dirty=True;self.confirm.setChecked(False);self.refresh();self.status.setText(message+' · Private change; not saved or published.')

    def apply_phrase(self):
        i=self.table.currentRow()
        if i<0:return
        try:self._accept_edit(edit_phrase(self.document,i,self.phrase.toPlainText()),'Text correction applied')
        except Exception as e:self.status.setText(str(e))

    def load_vocabulary(self):
        name,_=QFileDialog.getOpenFileName(self,'Load recording-specific vocabulary for review','','Vocabulary profile (*.json)')
        if not name:return
        try:
            raw=Path(name).read_bytes()
            if len(raw)>1024*1024:raise ValueError('Vocabulary profile exceeds 1 MiB.')
            changed=stage_suggestions(self.document,json.loads(raw))
            if changed.to_dict()==self.document.to_dict():
                self.status.setText('No new exact-name suggestions for this recording. Nothing changed; no recognition hints were enabled.');return
            self._accept_edit(changed,'Name suggestions loaded separately. Accept or reject each; raw evidence is unchanged.')
            self.edit_tabs.setCurrentIndex(3)
        except Exception as e:self.status.setText(str(e))

    def selected_name(self):
        return next((s for s in self.document.metadata.get('vocabulary_review',{}).get('suggestions',[])
                     if s['id']==self.name_select.currentData()),None)

    def show_name_suggestion(self,*unused):
        s=self.selected_name()
        self.name_evidence.setPlainText((f"Original phrase: {s['cue_text']}\n\nSuggestion: {s['match_text']} → {s['replacement']}\nStatus: {s['status']}\n{s['note']}\n\nSpelling evidence:\n"+json.dumps(s['spelling_source'],ensure_ascii=False,indent=2)) if s else 'No name suggestions loaded. Audio-only extraction remains the default.')
        for button in [self.name_accept,self.name_reject]:button.setEnabled(bool(s and s['status']=='pending') and not self.busy)

    def decide_name(self,accept):
        if self.busy:return
        try:
            changed=decide_suggestion(self.document,self.name_select.currentData(),accept=accept)
            self._accept_edit(changed,'Name correction accepted; review required again.' if accept else 'Suggestion rejected; words and timing retained.')
        except Exception as e:self.status.setText(str(e))

    def seek_name(self):
        s=self.selected_name()
        if s and 0<=s['cue_index']<len(self.document.segments):self.seek(max(0,self.document.segments[s['cue_index']].start-.5))

    def show_held(self,*unused):
        i=self.held_select.currentData();held=self.document.metadata.get('held_passages',[])
        self.held_text.setPlainText((held[i]['reason']+'\n'+held[i]['text']+f"\nProcessing window: {held[i]['start']:.2f}–{held[i]['end']:.2f}s; not an exact word timestamp.") if i is not None else '')

    def seek_held(self):
        i=self.held_select.currentData()
        if i is not None:self.seek(max(0,self.document.metadata['held_passages'][i]['start']-.5))

    def exclude_held(self):
        try:
            self.commit_text_section();self._accept_edit(exclude_held_passage(self.document,self.held_select.currentData()),'Held text explicitly excluded; raw evidence retained');self.sync_section_text()
        except Exception as e:self.status.setText(str(e))

    def copy_review_text(self):
        if not self.document.segments and self.document.metadata.get('recognition_regions'):
            i=self.held_select.currentData()
            if i is not None:self.section_select.setCurrentIndex(self.document.metadata['held_passages'][i]['region'])
            self.preparation.show();self.prepare_button.setText('Hide preparation');self.reference.setFocus()
            self.status.setText('Correct the selected audio section above. Raw evidence remains in Held passages. Align supplied text rechecks all corrected sections.');return
        rows=[(c.start,c.text) for c in self.document.segments]
        rows.extend((h['start'],h['text']) for h in self.document.metadata.get('held_passages',[]) if h.get('status')=='held')
        self.reference.setPlainText('\n'.join(text for _,text in sorted(rows)))
        self.preparation.show();self.prepare_button.setText('Hide preparation');self.reference.setFocus()
        self.status.setText('Copied to separate supplied-text input. Correct words there; whole-draft realignment requires explicit timing replacement. Nothing changed in the timed draft.')

    def check_saved_repetition(self):
        try:
            doc=hold_saved_repetition(self.document)
            if doc.to_dict()==self.document.to_dict():self.status.setText('No extreme consecutive repetition found. This does not establish lyric accuracy.');return
            self._accept_edit(doc,'Repetitive extraction passages held; use Undo to restore, or review their retained raw text')
            self.edit_tabs.setCurrentIndex(2)
        except Exception as e:self.status.setText(str(e))

    def _exact_bound_values(self):
        if self._bound_origin is None:raise ValueError('Select a phrase or word first.')
        # A displayed 3-decimal value must not round an untouched native anchor.
        return tuple(original if value==round(original,3) else value for value,original in zip((self.start_bound.value(),self.end_bound.value()),self._bound_origin))

    def apply_bounds(self):
        try:
            start,end=self._exact_bound_values()
            self._accept_edit(edit_bounds(self.document,self.table.currentRow(),self.timing_target.currentData(),start,end),'Timing bounds applied')
        except Exception as e:self.status.setText(str(e)+' Nothing changed.')

    def bound_at_playback(self,boundary):
        try:
            if self._bound_origin is None:raise ValueError('Select a phrase or word first.')
            start,end=self._bound_origin;t=self.player.position()/1000
            self._accept_edit(edit_bounds(self.document,self.table.currentRow(),self.timing_target.currentData(),t if boundary=='start' else start,t if boundary=='end' else end),'Boundary set from playback clock')
        except Exception as e:self.status.setText(str(e)+' Nothing changed.')

    def seek_selection(self):
        if self._bound_origin:self.seek(max(0,self._bound_origin[0]-.5))

    def move_timing(self,seconds):
        try:
            scope=self.timing_scope.currentIndex();wi=None
            if scope==2:indices=None
            elif scope==1:indices=self.selected_rows()
            else:indices=[self.table.currentRow()];wi=self.timing_target.currentData()
            candidate=timing_offset(self.document,seconds,indices=indices,word_index=wi)
            self._accept_edit(candidate,f'{self.timing_scope.currentText()} moved {seconds:+.3f} s')
        except Exception as e:self.status.setText(str(e)+' Nothing changed.')

    def undo_edit(self):
        if not self.busy:self.commit_text_section()
        if self.busy or not self._undo:return
        self._redo.append(copy.deepcopy(self.document));self.document=mark_unreviewed(self._undo.pop(),'Undo restored previous candidate; review before approval.')
        self.sync_section_text();self.dirty=True;self.confirm.setChecked(False);self.refresh();self.status.setText('Undo applied; saved output remains unchanged.')

    def redo_edit(self):
        if self.busy or not self._redo:return
        self._undo.append(copy.deepcopy(self.document));self.document=mark_unreviewed(self._redo.pop(),'Redo restored edited candidate; review before approval.')
        self.sync_section_text();self.dirty=True;self.confirm.setChecked(False);self.refresh();self.status.setText('Redo applied; saved output remains unchanged.')

    def sync_section_text(self):
        sections=self.document.metadata.get('recognition_regions',[])
        if 0<=self._section_index<len(sections) and not self.document.segments:
            self._loading_reference=True;self.reference.setPlainText(sections[self._section_index]['text']);self._loading_reference=False

    def reset_opened_timing(self):
        try:self._accept_edit(reset_timing(self.document,self._timing_baseline),'Opened timing restored; current text retained')
        except Exception as e:self.status.setText(str(e))

    def designate_instrumental(self,value):
        before=copy.deepcopy(self.document)
        if value:
            self.document.metadata.setdefault('before_instrumental',self.document.to_dict())
            self.document.segments=[];self.document.metadata['instrumental']=True
        else:
            saved=self.document.metadata.pop('before_instrumental',None)
            if saved:self.document=LyricsDocument.from_dict(saved)
            self.document.metadata.pop('instrumental',None)
        changed=mark_unreviewed(self.document,'Operator instrumental designation changed; no automatic detection.');self.document=before;self._accept_edit(changed,'Instrumental designation changed')
    def save_draft(self):
        try:
            self.commit_text_section()
            self.document=self.store.save_draft(self.document);self.dirty=False;self.status.setText('Private draft saved automatically for this recording. Previous files remain in history.');return True
        except Exception as e:self.status.setText(str(e));return False
    def restore_draft(self):
        try:
            document=self.store.load()
            if document:self.set_document(document)
        except Exception as e:self.status.setText(str(e))
    def import_timed(self):
        name,_=QFileDialog.getOpenFileName(self,'Import a copy for review','','Timed lyrics (*.json *.lrc *.srt *.vtt)')
        if name:
            try:self.set_document(self.store.import_document(name))
            except Exception as e:self.status.setText(str(e))
    def open_sheet(self):
        if self.busy:return
        if self.instrumental.isChecked():
            self.status.setText('This recording is designated instrumental. Uncheck that designation before supplying sung lyrics.');return
        try:
            from .review_sheet_dialog import SuppliedLyricsDialog
            dialog=SuppliedLyricsDialog(self)
            dialog.exec()
            if dialog.saved_for_alignment is not None:
                self.start_job('align',source='sheet',supplied_sheet=dialog.saved_for_alignment)
            elif dialog.saved_for_untimed is not None:self.use_untimed_sheet(dialog.saved_for_untimed)
        except Exception as e:self.status.setText(str(e)+' Current timed draft retained.')
    def use_untimed_sheet(self,sheet):
        from .review_sheet import untimed_document
        new=untimed_document(sheet,self.document)
        self.store.save_draft(self.document)
        new=self.store.save_draft(new)
        self._accept_edit(new,'Untimed sheet applied');self.dirty=False
        self.status.setText('Untimed draft saved. No model or timestamps used. Previous draft is recoverable; approved library lyrics are unchanged. Undo is available.')
    def align_current(self):
        if self.document.untimed_text:self.start_job('align',source='sheet',supplied_sheet=self.document.metadata['supplied_sheet'])
        else:self.start_job('align')
    def approve(self):
        if self.busy:return
        try:
            if self.confirm.isChecked():
                answer=QMessageBox.question(self,'Save and use lyrics?',
                    'Save and use these untimed lyrics in the library?' if self.document.untimed_text else 'Save and use these lyrics and timings as they are?',
                    QMessageBox.StandardButton.Ok|QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel)
                if answer!=QMessageBox.StandardButton.Ok:
                    self.status.setText('Cancelled. Lyrics, timing and saved output are unchanged.');return
            output,approved=self.store.approve(self.document,self.cache_dir,confirmed=self.confirm.isChecked())
            self.document=approved;self.dirty=False;self.approved.emit(output,approved)
            try:self.store.save_draft(approved)
            except Exception as warning:
                self.status.setText('Approved and attached to library; private draft copy could not be saved: '+str(warning));return
            self.status.setText('Saved and applied to this app’s library/output. Lyrics and timing kept as-is.')
            if approved.untimed_text:self.status.setText('Untimed lyrics saved to the library. No synchronized singalong or word highlighting is available without timing.')
        except Exception as e:self.status.setText(str(e))
    def set_busy(self,value):
        self.busy=value
        for w in [self.instrumental,self.import_button,self.paste_button,self.reference,self.ignore_tags,self.apply_button,self.bounds_button,self.save_button,self.restore_button,self.approve_button,self.confirm,self.play_button,self.replace_timing,self.repeat_override,self.held_panel,self.check_repetition_button]:w.setEnabled(not value)
        for w in [self.extract_button,self.align_button,self.supplied_align_button,self.realign_passage_button,self.listen_passage_button,self.supplied_options]:w.setEnabled(not value and not self.instrumental.isChecked())
        for w in [self.edit_tabs,self.table,self.timing_scope,self.timing_target,self.offset_button,self.earlier_button,self.later_button,self.start_now_button,self.end_now_button,self.reset_timing_button]:w.setEnabled(not value)
        self.undo_button.setEnabled(not value and bool(self._undo));self.redo_button.setEnabled(not value and bool(self._redo))
        self.cancel_button.setEnabled(value)
        self.recover_words_button.setEnabled(not value)
        self.review_device_select.setEnabled(not value and self.config.backend=='qwen');self.review_gpu_python.setEnabled(not value and self.config.review_device=='rocm');self.save_review_compute_button.setEnabled(not value)
        self.backend_select.setEnabled(not value);self.section_select.setEnabled(not value);self.group_timing_button.setEnabled(not value)
    def start_job(self,mode,*,source='draft',supplied_sheet=None):
        if self.busy or self.instrumental.isChecked():return
        try:
            self.store.verify()
            if mode=='extract' and (self.embedded.get('text') or self.embedded.get('kind')=='probe_failed') and not self.ignore_tags.isChecked():raise ValueError('Check the embedded source first. Align supplied text, or explicitly choose audio transcription instead.')
            if mode=='extract' and (self.document.segments or self.document.metadata.get('recognition_regions')) and not self.replace_timing.isChecked():raise ValueError('Extraction replaces the whole candidate and its timing. Confirm replacement first; previous draft will be retained.')
            if source!='sheet':self.commit_text_section()
            self.config.review_device=self.review_device_select.currentData();self.config.review_gpu_python=self.review_gpu_python.text().strip()
            job_config=self.config
            if mode=='align' and source=='sheet':
                from .review_sheet import alignment_plan
                backend,route,alignment=alignment_plan(supplied_sheet,self.document,self.config.backend)
                job_config=replace(self.config,backend=backend)
            elif mode=='align' and source=='passage':
                if self.config.backend!='qwen':raise ValueError('Selected-passage realignment uses Qwen; choose it in preparation.')
                alignment=passage_alignment_input(self.document,self.table.currentRow())
            elif mode=='align' and self.config.backend=='qwen':
                alignment=qwen_alignment_input(self.document,self.reference.toPlainText(),source=source,replace_timing=self.replace_timing.isChecked(),use_sections=self._section_index>=0)
            else:alignment=alignment_input(self.document,self.reference.toPlainText(),source=source,replace_timing=self.replace_timing.isChecked()) if mode=='align' else {}
            job_config=job_config.for_job()
            missing=job_config.missing(mode)
            if missing:raise ValueError('Installed review resources unavailable: '+', '.join(missing)+'. No download or permission change was attempted. Configure lyrics-review-runtime.json in the app data directory if paths changed.')
            if source=='sheet':
                self.document=self.store.save_draft(self.document);self.dirty=False
            elif not self.save_draft():return
            self._job_revision=document_revision(self.document)
            self.player.pause()
            run=self.store.root/'runs'/uuid.uuid4().hex;run.mkdir(parents=True)
            request={'source':str(self.track.path.resolve()),'source_sha256':self.store.digest,'title':self.track.title,'mode':mode,'instrumental':False,'config':asdict(job_config),'local_lock':str(self.data_dir/'lyrics-review-model.lock'),'reference_text':'','reference_original':'','embedded':self.embedded,'media_library_path':str(getattr(sys,'_MEIPASS','')),**alignment,'repeat_override':mode=='align' and (source=='sheet' or (source=='supplied' and self.repeat_override.isChecked()))}
            request['recognition_policy']=audio_only_policy(request)
            atomic_json(run/'request.json',request);self._job_request=copy.deepcopy(request);self._recoverable_candidate=None;self._bytes=b'';self._stderr='';self._seen_result=False
            env=QProcessEnvironment.systemEnvironment();env.insert('PYTHONUTF8','1');env.insert('PYTHONIOENCODING','utf-8');env.insert('PYTHONDONTWRITEBYTECODE','1')
            if getattr(sys,'frozen',False):
                original=os.environ.get('LD_LIBRARY_PATH_ORIG')
                if original:env.insert('LD_LIBRARY_PATH',original)
                else:env.remove('LD_LIBRARY_PATH')
            self.process.setProcessEnvironment(env);self.set_busy(True)
            self.process.start(job_config.align_python,['-u',str(Path(__file__).with_name('review_worker.py')),'--request',str(run/'request.json')])
            self.status.setText((alignment.get('alignment_route','')+' ' if source=='sheet' else '')+'Starting private '+('AMD ROCm FP32' if job_config.review_device=='rocm' else 'CPU FP32')+' review · no automatic approval.')
        except Exception as e:self.status.setText(str(e))
    def _output(self):
        self._bytes+=bytes(self.process.readAllStandardOutput())
        while b'\n' in self._bytes:
            line,self._bytes=self._bytes.split(b'\n',1);event=parse_worker_event(line)
            if not event:continue
            if event['type']=='done':
                try:
                    verify_job_revision(self.document,self._job_revision)
                    doc=mark_unreviewed(load_lyrics(event['output']),'New model result; prior listening approval does not transfer.')
                    if self._job_request and self._job_request.get('replacement_scope')=='selected_passage':
                        doc=merge_passage_alignment(self.document,doc,self._job_request)
                        self._accept_edit(doc,'Selected passage aligned automatically; other timings preserved')
                        self.store.save_draft(self.document)
                    else:self.store.save_draft(doc);self.set_document(doc)
                    self._seen_result=True
                    count=sum(h.get('status')=='held' for h in doc.metadata.get('held_passages',[]))
                    self.status.setText(('Timed candidate ready. ' if doc.segments else 'Processing completed with exceptions; raw words retained. ')+f'{count} flagged passages. Corrections are optional. Nothing was published.')
                except Exception as e:self.status.setText(str(e))
            elif event['type'] in ['progress','error','cancelled']:
                if event.get('checkpoint'):self._recoverable_candidate=event['checkpoint']
                self.status.setText(event.get('message',''))
    def _error_output(self):self._stderr=(self._stderr+bytes(self.process.readAllStandardError()).decode(errors='replace'))[-4000:]
    def _finished(self,code,status):
        self._output();self.set_busy(False)
        if code and not self._seen_result:
            reason=self.status.text()
            if self._recoverable_candidate:
                try:
                    verify_job_revision(self.document,self._job_revision)
                    doc=mark_unreviewed(load_lyrics(self._recoverable_candidate),'Alignment stopped; extracted words checkpointed without approval.')
                    self.store.save_draft(doc);self.set_document(doc)
                    self.status.setText('Words saved. '+reason+' Use Retry alignment to continue without extracting again.')
                except Exception as error:self.status.setText('Checkpoint retained on disk; current edits kept. '+str(error))
            else:self.status.setText('Job stopped; prior drafts/output preserved. '+reason+' '+self._stderr[-250:])
        if self._closing:self.close()
    def _process_error(self,error):
        if error==QProcess.ProcessError.FailedToStart:self.set_busy(False);self.status.setText('Review runtime could not start. Existing data preserved.')
    def cancel_job(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            self.process.terminate();QTimer.singleShot(5000,self._kill_if_running)
            self.status.setText('Cancelling model worker and its child. Existing files remain preserved.')
    def _kill_if_running(self):
        if self.process.state()!=QProcess.ProcessState.NotRunning:
            if os.name!='nt':
                import signal
                try:os.killpg(int(self.process.processId()),signal.SIGKILL)
                except (ProcessLookupError,PermissionError):self.process.kill()
            else:self.process.kill()
    def show_evidence(self):
        raw={}
        folder=self.document.metadata.get('provenance',{}).get('raw_evidence')
        if folder:
            for name in ('heart.json','qwen-asr.json','qwen-align.json','receipt.json'):
                path=Path(folder)/name
                if path.is_file() and path.stat().st_size<2*1024*1024:
                    try:raw[name]=json.loads(path.read_text())
                    except (OSError,ValueError):pass
        text={'raw_model_evidence':raw,'embedded_original':self.embedded,'review':self.document.metadata,'quality':self.document.quality,'evidence_folder':str(self.store.root)}
        dialog=QDialog(self);dialog.setWindowTitle('Preserved lyric evidence');dialog.resize(800,550);layout=QVBoxLayout(dialog);view=QPlainTextEdit();view.setReadOnly(True);view.setPlainText(json.dumps(text,ensure_ascii=False,indent=2));layout.addWidget(view);dialog.exec()
    def _can_close(self):
        if self.busy:
            self._closing=True;self.cancel_job();return False
        if self.dirty:
            choice=QMessageBox.question(self,'Save private edits?','Save these edits as a private draft before closing?',QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel)
            if choice==QMessageBox.StandardButton.Cancel:return False
            if choice==QMessageBox.StandardButton.Save and not self.save_draft():return False
        self.player.stop();self.timer.stop();return True

    def reject(self):
        # Escape must obey the same save/cancel contract as the window close button.
        if self._can_close():super().reject()

    def closeEvent(self,event):
        if self._can_close():event.accept()
        else:event.ignore()
