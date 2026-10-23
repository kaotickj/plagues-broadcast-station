"""Operator desk for Plague's Broadcast Station. The program window stays clean."""
from __future__ import annotations
import argparse, json, os, sys
from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QProcess, QProcessEnvironment, QSize
from PySide6.QtGui import QPixmap, QFont, QIcon, QKeySequence, QShortcut, QColor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QAbstractItemView,
    QFileDialog, QMessageBox, QTabWidget, QLineEdit, QPlainTextEdit, QSlider,
    QCheckBox, QDoubleSpinBox, QSpinBox, QFormLayout, QComboBox, QProgressBar,
    QSplitter, QGroupBox, QScrollArea, QColorDialog,
)
from .broadcast_profile import BroadcastProfileStore, StreamProfile, ProfileError
from . import __version__
from .library import Track, scan_media, MEDIA_EXTENSIONS
from .playback import PlaybackEngine
from .stage import BroadcastStage, IMAGE_SUFFIXES, VIDEO_SUFFIXES, DEFAULT_WAVEFORM_COLOR, waveform_color
from .lyrics import load_lyrics, save_lyrics, find_lyrics, fingerprint, cached_lyrics_path
from .lyric_review import document_revision, is_approved, edit_document, retarget_review, save_review_document
from .lyric_editor import TimedLyricsEditor, stored_review_notes, parse_seconds
from .extraction import ExtractionConfig, discover_config, config_status, build_command, parse_worker_event, default_data_dir
from .settings import atomic_json, read_profile, write_profile, load_m3u, save_m3u, starter_profile

APP_NAME = "Plague's Broadcast Station"

def application_icon() -> QIcon:
    """Use the same sized artwork in source, wheel and standalone builds."""
    icon = QIcon()
    assets = Path(__file__).resolve().with_name('assets')
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        icon.addFile(str(assets / f'broadcast-icon-{size}.png'), QSize(size, size))
    return icon

STYLE = """
QWidget { background:#101c24; color:#e9e8e1; font-family:'DejaVu Sans'; font-size:13px; }
QMainWindow, QTabWidget::pane { background:#101c24; }
QLabel#masthead { font-size:25px; font-weight:700; color:#e7bd78; }
QLabel#nowTitle { font-size:21px; font-weight:600; color:#f6e8ca; }
QLabel#muted { color:#b4c3ca; }
QPushButton { background:#203845; border:1px solid #536570; border-radius:6px; padding:9px 13px; min-height:23px; }
QPushButton:hover { background:#2c4858; border-color:#e7bd78; }
QPushButton:focus, QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus, QComboBox:focus { border:2px solid #81c1ef; }
QPushButton:disabled { color:#82939b; background:#182830; }
QPushButton#primary { background:#deb775; color:#171a1e; border-color:#edcf9b; font-weight:700; }
QPushButton#primary:disabled { color:#82939b; background:#182830; border-color:#536570; }
QPushButton#danger { background:#6c3034; color:#fff; }
QLineEdit, QPlainTextEdit, QListWidget, QComboBox, QSpinBox, QDoubleSpinBox { background:#0a141b; border:1px solid #526671; border-radius:5px; padding:7px; }
QListWidget::item { padding:11px; border-bottom:1px solid #253b47; }
QListWidget::item:selected { background:#294959; color:#fff2d6; }
QTabBar::tab { padding:12px 17px; background:#1b2e39; border-bottom:2px solid #1b2e39; }
QTabBar::tab:selected { border-bottom-color:#e7bd78; color:#f1d39e; }
QGroupBox { border:1px solid #3c5360; border-radius:7px; margin-top:13px; padding-top:18px; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; color:#e7bd78; }
QCheckBox { padding:8px 4px; } QCheckBox::indicator { width:18px; height:18px; }
QSlider::groove:horizontal { height:6px; background:#314b59; border-radius:3px; }
QSlider::handle:horizontal { width:18px; margin:-6px 0; background:#e7bd78; border-radius:9px; }
QProgressBar { border:1px solid #526671; border-radius:4px; text-align:center; min-height:22px; }
QProgressBar::chunk { background:#447466; }
QStatusBar { background:#0a141b; color:#c6d5dc; }
"""

def clock(seconds: float) -> str:
    seconds = max(0, int(seconds)); return f'{seconds//60}:{seconds%60:02d}'

class Task(QThread):
    done = Signal(object)
    failed = Signal(str)
    def __init__(self, fn, parent=None): super().__init__(parent); self.fn = fn
    def run(self):
        try: self.done.emit(self.fn())
        except Exception as error: self.failed.emit(str(error))

class PlaylistWidget(QListWidget):
    reordered = Signal()
    def dropEvent(self, event): super().dropEvent(event); self.reordered.emit()

class MainWindow(QMainWindow):
    def __init__(self, data_dir: Path, profile: Path | None = None, *, credential_store=None):
        super().__init__()
        self.data_dir = Path(data_dir); self.data_dir.mkdir(parents=True, exist_ok=True)
        self.credential_store = credential_store or BroadcastProfileStore(self.data_dir)
        self._profile_busy = False
        self.settings_file = self.data_dir/'settings.json'
        try: self.saved = json.loads(self.settings_file.read_text())
        except (OSError, ValueError): self.saved = {}
        if not isinstance(self.saved,dict):self.saved={}
        self.setWindowTitle(APP_NAME + ' '+__version__+' · Operator Desk'); self.resize(1370, 900)
        self.setMinimumSize(1030, 720)
        self.all_tracks: list[Track] = []; self.backgrounds: list[Path] = []
        self.lyric_files: dict[str, str] = {}; self.documents = {}; self.tasks = []
        self._lyric_stamps={}; self._invalid_lyrics=set(); self._lyric_requests={}
        self._lyric_locations={}; self._lyric_file_stamps={}; self._lyric_buffers={}; self._saving_lyrics=set(); self._source_checks={}
        self._editor_track=None; self._editor_document=None; self._editor_revision=None
        self._last_review_poll=None
        self._extract_context=None
        self.profile_name = 'Goodneighbor Blues'; self._scan_busy = False
        self._extract_queue = []; self._extract_current = None; self._process_bytes = b''
        self._closing = False; self._seek_drag = False; self._track_generation = 0; self._displayed_track = None
        self.stage = BroadcastStage(); self.stage.setWindowTitle(APP_NAME + ' · Program Output')
        self.engine = PlaybackEngine(self)
        try: self.extract_config = ExtractionConfig.from_dict(self.saved.get('extraction', discover_config().to_dict()))
        except (TypeError, ValueError): self.extract_config = discover_config()
        self.extract_config.cache_dir = str(self.data_dir/'lyrics-cache')
        self.extract_process = QProcess(self)
        self.extract_process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.extract_process.readyReadStandardOutput.connect(self._extract_output)
        self.extract_process.readyReadStandardError.connect(self._extract_stderr)
        self.extract_process.finished.connect(self._extract_finished)
        self.extract_process.errorOccurred.connect(self._process_error)
        self._extract_log = ''
        from .broadcast import BroadcastSession
        self.export = BroadcastSession(self)
        self.export.status.connect(self._broadcast_status)
        self.export.error.connect(self._broadcast_error)
        self.export.finished.connect(self._export_finished)
        self.export.statistics.connect(self._broadcast_statistics)
        self._export_size = (1280,720); self._last_preview = 0
        self._build_ui(); self._connect()
        for label in [self.now_title,self.now_artist,self.lyric_status,self.broadcast_label,self.broadcast_stats]: label.setTextFormat(Qt.TextFormat.PlainText)
        self.preview_timer = QTimer(self); self.preview_timer.setInterval(50); self.preview_timer.timeout.connect(self._preview); self.preview_timer.start()
        self.save_timer = QTimer(self); self.save_timer.setSingleShot(True); self.save_timer.timeout.connect(self.save_session)
        start = profile or (self.data_dir/'session.json' if (self.data_dir/'session.json').is_file() else None)
        if start:
            try: self.load_profile(start)
            except Exception as error: self._message(f'Could not open playlist: {error}')
        self._message('Ready. Play a track, then show Program Output or start a local recording.')
        if self.credential_store.marker.exists():self.restore_stream_profile()

    def _button(self, label, slot, primary=False):
        button = QPushButton(label); button.clicked.connect(slot)
        button.setAccessibleName(label)
        if primary: button.setObjectName('primary')
        return button

    def _build_ui(self):
        root = QWidget(); layout = QVBoxLayout(root); layout.setContentsMargins(22,18,22,14); layout.setSpacing(14)
        heading = QHBoxLayout(); title = QLabel("PLAGUE’S BROADCAST STATION"); title.setObjectName('masthead')
        heading.addWidget(title); heading.addStretch()
        heading.addWidget(self._button('Show Program Output',self.show_output,True)); layout.addLayout(heading)
        intro = QLabel('Music, moving pictures, and words worth staying for.'); intro.setObjectName('muted'); layout.addWidget(intro)
        split = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget(); library = QVBoxLayout(left); library.setContentsMargins(0,0,12,0)
        add = QHBoxLayout(); add.addWidget(self._button('Add files', self.add_files)); add.addWidget(self._button('Add folder', self.add_folder)); library.addLayout(add)
        row = QHBoxLayout(); row.addWidget(self._button('Open playlist',self.open_playlist)); row.addWidget(self._button('Save playlist',self.save_playlist)); library.addLayout(row)
        self.search = QLineEdit(); self.search.setPlaceholderText('Find a song, artist, or album'); self.search.setAccessibleName('Search playlist'); self.search.textChanged.connect(self._filter_rows); library.addWidget(self.search)
        self.ready_only = QCheckBox('Lyrics-ready tracks only'); self.ready_only.setAccessibleName('Play only tracks with prepared lyrics'); self.ready_only.toggled.connect(self._apply_queue); library.addWidget(self.ready_only)
        self.playlist = PlaylistWidget(); self.playlist.setAccessibleName('Broadcast playlist. Drag songs to reorder. Double click to play.'); self.playlist.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.playlist.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove); self.playlist.setDefaultDropAction(Qt.DropAction.MoveAction); self.playlist.setWordWrap(True)
        self.playlist.itemDoubleClicked.connect(self._play_item); self.playlist.currentItemChanged.connect(self._selection_changed); self.playlist.reordered.connect(self._reordered); library.addWidget(self.playlist,1)
        remove = QHBoxLayout(); remove.addWidget(self._button('Remove selected',self.remove_selected)); self.library_count = QLabel('No tracks yet'); remove.addWidget(self.library_count); library.addLayout(remove)
        self.shuffle = QCheckBox('Shuffle'); self.shuffle.setChecked(True); self.repeat = QCheckBox('Repeat'); self.repeat.setChecked(True)
        toggles=QHBoxLayout(); toggles.addWidget(self.shuffle); toggles.addWidget(self.repeat); toggles.addStretch(); library.addLayout(toggles)
        split.addWidget(left)
        right=QWidget(); detail=QVBoxLayout(right); detail.setContentsMargins(12,0,0,0)
        self.monitor_panel=QWidget(); monitor=QVBoxLayout(self.monitor_panel); monitor.setContentsMargins(0,0,0,0)
        self._expanded_lyrics=False
        self.preview = QLabel(); self.preview.setMinimumSize(480,270); self.preview.setMaximumHeight(340); self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview.setAccessibleName('Program preview. The separate Program Output window is the clean broadcast picture.'); self.preview.setStyleSheet('background:#050b10;border:1px solid #536570;'); monitor.addWidget(self.preview)
        self.now_title=QLabel('Your next great broadcast'); self.now_title.setObjectName('nowTitle'); self.now_title.setWordWrap(True); monitor.addWidget(self.now_title)
        self.now_artist=QLabel('Choose a track from the playlist.'); self.now_artist.setObjectName('muted'); monitor.addWidget(self.now_artist)
        self.seek=QSlider(Qt.Orientation.Horizontal); self.seek.setRange(0,10000); self.seek.setAccessibleName('Recording position'); self.seek.sliderPressed.connect(lambda:self._set_seek_drag(True)); self.seek.sliderReleased.connect(self._seek_released); self.seek.actionTriggered.connect(self._seek_action); monitor.addWidget(self.seek)
        playback=QHBoxLayout()
        for text,slot in [('Previous',self.engine.previous),('Play',self.toggle_play),('Stop',self.engine.stop),('Next',self.engine.next)]:
            b=self._button(text,slot,text=='Play'); playback.addWidget(b)
            if text=='Play': self.play_button=b
        self.time_label=QLabel('0:00 / 0:00'); playback.addWidget(self.time_label); playback.addStretch(); monitor.addLayout(playback)
        detail.addWidget(self.monitor_panel)
        self.tabs=QTabWidget(); self.tabs.setAccessibleName('Operator settings'); detail.addWidget(self.tabs,1)
        self._appearance_tab(); self._lyrics_tab(); self._broadcast_tab(); self._models_tab()
        self.tabs.currentChanged.connect(self._operator_tab_changed)
        split.addWidget(right); split.setSizes([410,900]); layout.addWidget(split,1); self.setCentralWidget(root)

    def _appearance_tab(self):
        page=QWidget(); v=QVBoxLayout(page); form=QFormLayout()
        self.brand=QLineEdit('GOODNEIGHBOR BLUES'); self.tagline=QLineEdit('Original blues. Stay awhile.'); form.addRow('Station name',self.brand); form.addRow('Station line',self.tagline)
        self.interval=QSpinBox(); self.interval.setRange(5,300); self.interval.setValue(18); self.interval.setSuffix(' seconds'); form.addRow('Change backdrop every',self.interval)
        self.motion=QCheckBox('Gentle motion and crossfades'); self.motion.setChecked(True); self.wave=QCheckBox('Audio waveform'); self.wave.setChecked(True); self.lyrics_visible=QCheckBox('Show sing-along lyrics'); self.lyrics_visible.setChecked(True)
        form.addRow(self.motion); form.addRow(self.wave)
        self._waveform_color=DEFAULT_WAVEFORM_COLOR
        self.wave_color=self._button(self._waveform_color,self.choose_waveform_color)
        self.wave_color.setAccessibleName("Choose waveform color")
        self.wave_color.setToolTip("Changes the audio waveform in the preview, Program Output and recordings.")
        form.addRow("Waveform color",self.wave_color); self._update_waveform_color_button()
        form.addRow(self.lyrics_visible); v.addLayout(form)
        row=QHBoxLayout(); row.addWidget(self._button('Add artwork / videos',self.add_backgrounds)); row.addWidget(self._button('Previous backdrop',self.stage.previous_background)); row.addWidget(self._button('Next backdrop',self.stage.next_background)); v.addLayout(row)
        self.art_count=QLabel('No artwork loaded'); self.art_count.setObjectName('muted'); v.addWidget(self.art_count)
        for widget in [self.brand,self.tagline]: widget.textChanged.connect(self.apply_appearance)
        self.interval.valueChanged.connect(self.apply_appearance)
        for widget in [self.motion,self.wave,self.lyrics_visible]: widget.toggled.connect(self.apply_appearance)
        v.addStretch(); self.tabs.addTab(page,'Picture & sound')

    def _lyrics_tab(self):
        page=QWidget(); v=QVBoxLayout(page)
        row=QHBoxLayout(); self.lyric_status=QLabel('Select a recording to see its lyric status.'); self.lyric_status.setWordWrap(True); row.addWidget(self.lyric_status,1)
        self.expand_lyrics_button=self._button('Expand editor',self._toggle_expanded_lyrics); self.expand_lyrics_button.setCheckable(True); self.expand_lyrics_button.setAccessibleName('Expand or restore lyric editing workspace'); row.addWidget(self.expand_lyrics_button); v.addLayout(row)
        # Save/review actions stay visible while the transcript and notes scroll.
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        body=QWidget(); content=QVBoxLayout(body); content.setContentsMargins(0,0,6,0)
        self.prepare_actions=QWidget(); row=QHBoxLayout(self.prepare_actions); row.setContentsMargins(0,0,0,0); self.import_button=self._button('Import timed lyrics',self.import_lyrics); row.addWidget(self.import_button); self.extract_button=self._button('Extract & align',lambda:self.queue_extraction(False)); row.addWidget(self.extract_button); self.batch_button=self._button('Prepare missing',self.queue_missing); row.addWidget(self.batch_button); content.addWidget(self.prepare_actions)
        self.preparation_progress=QWidget(); row=QHBoxLayout(self.preparation_progress); row.setContentsMargins(0,0,0,0); self.extract_progress=QProgressBar(); self.extract_progress.setValue(0); self.extract_progress.setAccessibleName('Lyric extraction progress'); row.addWidget(self.extract_progress,1); self.cancel_extract=self._button('Cancel',self.cancel_extraction); self.cancel_extract.setEnabled(False); row.addWidget(self.cancel_extract); self.preparation_progress.hide(); content.addWidget(self.preparation_progress)
        self.lyrics_splitter=QSplitter(Qt.Orientation.Vertical); self.lyrics_splitter.setAccessibleName('Resize lyric editor and review notes'); self.lyrics_splitter.setChildrenCollapsible(False)
        notes=QWidget(); notes_layout=QVBoxLayout(notes); notes_layout.setContentsMargins(0,0,0,0)
        notes_title=QLabel('Review notes · original automatic findings'); notes_layout.addWidget(notes_title)
        self.review_notes=QPlainTextEdit(); self.review_notes.setReadOnly(True); self.review_notes.setAccessibleName('Stored lyric review notes, timestamps and passage context'); self.review_notes.setMinimumHeight(75); self.review_notes.setPlainText(stored_review_notes(None)); notes_layout.addWidget(self.review_notes); self.lyrics_splitter.addWidget(notes)
        self.lyric_text=TimedLyricsEditor(); self.lyric_text.edited.connect(self._lyric_edited); self.lyrics_splitter.addWidget(self.lyric_text); self.lyrics_splitter.setStretchFactor(1,1); self.lyrics_splitter.setSizes([100,300]); content.addWidget(self.lyrics_splitter,1)
        row=QHBoxLayout(); self.add_line_button=self._button('Add line',self._add_lyric_line); row.addWidget(self.add_line_button); self.remove_line_button=self._button('Remove line',self.lyric_text.remove_lines); row.addWidget(self.remove_line_button); self.line_timing_button=self._button('Line timing',self._use_line_timing); self.line_timing_button.setToolTip('Remove word highlights from selected lines; keep their text and line times. Useful for uncertain word timing.'); row.addWidget(self.line_timing_button); self.realign_button=self._button('Align text',self._align_edited_lyrics); self.realign_button.setToolTip('Optional full-recording re-alignment of saved text for new word highlights. Edited lines already work with line timing. Requires local models; new results need review.'); row.addWidget(self.realign_button); content.addLayout(row)
        self.lyric_options=self._button('More lyric options',self._toggle_lyric_options); content.addWidget(self.lyric_options)
        self.lyric_options_panel=QWidget(); options=QVBoxLayout(self.lyric_options_panel); options.setContentsMargins(0,0,0,0)
        self.reference=QPlainTextEdit(); self.reference.setPlaceholderText('Optional complete lyric sheet. To correct a prepared transcript, edit its lines above.'); self.reference.setAccessibleName('Optional complete lyric sheet for alignment'); self.reference.setMinimumHeight(80); self.reference.setMaximumHeight(180); options.addWidget(self.reference)
        self.align_button=self._button('Align supplied sheet',lambda:self.queue_extraction(True)); options.addWidget(self.align_button)
        row=QHBoxLayout(); self.offset=QDoubleSpinBox(); self.offset.setRange(-15,15); self.offset.setSingleStep(.1); self.offset.setDecimals(1); self.offset.setSuffix(' s'); self.offset.setAccessibleName('Move lyric highlights earlier or later'); self.offset.valueChanged.connect(self.stage.set_offset); row.addWidget(QLabel('Preview timing adjustment')); row.addWidget(self.offset); row.addWidget(self._button('Reset',lambda:self.offset.setValue(0))); options.addLayout(row)
        self.allow_drafts=QCheckBox('Preview unapproved saved lyrics on Program Output'); self.allow_drafts.setChecked(False); self.allow_drafts.toggled.connect(self._reload_current_lyrics); options.addWidget(self.allow_drafts)
        self.lyric_options_panel.hide(); content.addWidget(self.lyric_options_panel)
        scroll.setWidget(body); v.addWidget(scroll,1)
        self.lyric_save_status=QLabel('Edits are saved per recording. Automatic preparation does not approve lyrics.'); self.lyric_save_status.setWordWrap(True); self.lyric_save_status.setAccessibleName('Lyric save and review status'); self.lyric_save_status.setTextFormat(Qt.TextFormat.PlainText); v.addWidget(self.lyric_save_status)
        self.review_confirm=QCheckBox('I checked these lyrics against this recording.'); self.review_confirm.setAccessibleName('Confirm human review of this recording and saved lyric revision'); self.review_confirm.toggled.connect(self._update_review_controls); v.addWidget(self.review_confirm)
        actions=QGridLayout(); self.save_lyrics_button=self._button('Save draft',self._save_lyric_changes,True); self.save_lyrics_button.setToolTip('Save text and line times for this recording (Ctrl+S). Does not approve airing.'); actions.addWidget(self.save_lyrics_button,0,0)
        self.approve_lyrics_button=self._button('Approve for airing',lambda:self._save_lyric_changes(approve=True)); actions.addWidget(self.approve_lyrics_button,0,1)
        self.reload_lyrics_button=self._button('Reload saved',self._discard_lyric_edits); actions.addWidget(self.reload_lyrics_button,1,0)
        self.export_lyrics_button=self._button('Save lyrics as…',self._export_saved_lyrics); self.export_lyrics_button.setToolTip('Export a separate JSON copy of the saved lyrics. Save pending changes first.'); actions.addWidget(self.export_lyrics_button,1,1); v.addLayout(actions)
        self.lyric_save_shortcut=QShortcut(QKeySequence.StandardKey.Save,page); self.lyric_save_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut); self.lyric_save_shortcut.activated.connect(self._save_lyric_changes)
        self._update_review_controls()
        self.tabs.addTab(page,'Lyrics')

    def _broadcast_tab(self):
        page=QWidget(); v=QVBoxLayout(page); form=QFormLayout()
        self.output_mode=QComboBox(); self.output_mode.addItems(['Local recording','Direct livestream (RTMP / RTMPS)']); form.addRow('Output',self.output_mode)
        self.resolution=QComboBox(); self.resolution.addItems(['1280 × 720 · 30 fps','1920 × 1080 · 30 fps']); form.addRow('Picture size',self.resolution)
        self.bitrate=QSpinBox(); self.bitrate.setRange(1000,18000); self.bitrate.setValue(4500); self.bitrate.setSuffix(' kbps'); form.addRow('Video bitrate',self.bitrate)
        self.destination=QLineEdit(); self.destination.setPlaceholderText('Recording file, or rtmps://server/application'); self.destination.setAccessibleName('Recording path or streaming server'); form.addRow('Destination',self.destination)
        self.stream_key=QLineEdit(); self.stream_key.setEchoMode(QLineEdit.EchoMode.Password); self.stream_key.setPlaceholderText('Save securely with Save/update'); self.stream_key.setAccessibleName('Private stream key. Masked; saved only in native protected credentials.'); form.addRow('Stream key',self.stream_key)
        v.addLayout(form)
        row=QHBoxLayout(); self.save_stream_button=self._button('Save/update',self.save_stream_profile); row.addWidget(self.save_stream_button)
        self.restore_stream_button=self._button('Restore saved',self.restore_stream_profile); row.addWidget(self.restore_stream_button)
        self.forget_stream_button=self._button('Forget saved',self.forget_stream_profile); row.addWidget(self.forget_stream_button)
        self.clear_stream_button=self._button('Clear fields',self.clear_stream_fields); row.addWidget(self.clear_stream_button); v.addLayout(row)
        self.stream_profile_status=QLabel('Session-only fields. Save/update securely remembers a direct livestream on this computer.'); self.stream_profile_status.setWordWrap(True); self.stream_profile_status.setTextFormat(Qt.TextFormat.PlainText); self.stream_profile_status.setAccessibleName('Protected streaming profile status'); v.addWidget(self.stream_profile_status)
        for field in (self.destination,self.stream_key):field.textChanged.connect(self._stream_fields_changed)
        self.output_mode.currentIndexChanged.connect(self._stream_fields_changed); self.resolution.currentIndexChanged.connect(self._stream_fields_changed); self.bitrate.valueChanged.connect(self._stream_fields_changed)
        row=QHBoxLayout(); self.choose_recording_button=self._button('Choose recording file',self.choose_recording); row.addWidget(self.choose_recording_button); self.start_output=self._button('Start output',self.start_broadcast,True); row.addWidget(self.start_output); self.stop_output=self._button('Stop output',self.stop_broadcast); self.stop_output.setEnabled(False); row.addWidget(self.stop_output); v.addLayout(row)
        self.broadcast_label=QLabel('Offline · Program Output is ready for OBS capture.'); self.broadcast_label.setWordWrap(True); v.addWidget(self.broadcast_label)
        self.broadcast_stats=QLabel(''); self.broadcast_stats.setWordWrap(True); v.addWidget(self.broadcast_stats)
        volume=QHBoxLayout(); volume.addWidget(QLabel('Monitor volume')); self.volume=QSlider(Qt.Orientation.Horizontal); self.volume.setRange(0,100); self.volume.setValue(70); self.volume.setAccessibleName('Local monitoring volume. Streaming audio level is unchanged.'); self.volume.valueChanged.connect(lambda n:self.engine.set_volume(n/100)); volume.addWidget(self.volume); v.addLayout(volume)
        tip=QLabel('Direct output includes only this program picture and its music—no microphone or other desktop sounds. For OBS, capture the Program Output window and this app’s audio. Prepare lyrics before starting an output.'); tip.setWordWrap(True); tip.setObjectName('muted'); v.addWidget(tip); v.addStretch(); scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); self.tabs.addTab(scroll,'Broadcast')

    def _models_tab(self):
        page=QWidget(); v=QVBoxLayout(page); form=QFormLayout(); self.model_fields={}
        v.addWidget(self._button('Set up / find models',self.open_model_setup,True))
        for key,label in [('python_path','Lyric runtime Python'),('model_root','Model folder'),('torch_home','Torch model cache'),('transcription_model_path','Whisper large-v3 location'),('review_model_path','Independent review model location'),('separator_model_path','Vocal separator location'),('alignment_model_path','Word alignment checkpoint'),('recognition_language','Recognition language (en or auto)'),('recognition_prompt','Optional recognition context'),('ollama_url','Local Ollama address'),('ollama_model','Ollama text model'),('gpu_python_path','GPU runtime Python'),('vulkan_binary','Whisper Vulkan executable'),('vulkan_model','Whisper Vulkan model'),('gpu_lock','Shared GPU lock (optional)')]:
            entry=QLineEdit(str(getattr(self.extract_config,key))); entry.setAccessibleName(label); self.model_fields[key]=entry; form.addRow(label,entry)
        self.model_fields['recognition_prompt'].setMaxLength(500)
        self.model_fields['recognition_prompt'].setToolTip('Optional context can bias recognition. Leave empty to avoid introducing words; recording filenames are never used as prompts.')
        self.model_fields['recognition_language'].setToolTip('Use en for English, auto for language detection, or a supported language code. English-only models retain English.')
        v.addLayout(form); self.gpu=QCheckBox('Use GPU when available; wait while another job uses it'); self.gpu.setChecked(self.extract_config.use_gpu); v.addWidget(self.gpu)
        v.addWidget(self._button('Save model settings',self.save_model_settings)); self.model_status=QLabel(''); self.model_status.setWordWrap(True); v.addWidget(self.model_status)
        note=QLabel('Playback does not require AI models. Extraction uses a separate local Python runtime so heavy work stays outside the player. Configure your installed runtime and models here; local settings are restored on later launches.'); note.setWordWrap(True); note.setObjectName('muted'); v.addWidget(note); v.addStretch(); scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); self.tabs.addTab(scroll,'Lyric models'); self.update_model_status()

    def _connect(self):
        self.engine.position_changed.connect(self._position)
        self.engine.duration_changed.connect(lambda _:self._position(self.engine.position))
        self.engine.playing_changed.connect(self._playing)
        self.engine.track_changed.connect(self._track_changed)
        self.engine.waveform_ready.connect(self.stage.set_waveform)
        self.engine.pcm_ready.connect(self.export.push_audio)
        self.engine.transport_reset.connect(self.export.clear_audio)
        self.engine.video_frame_ready.connect(self.stage.set_video_frame)
        self.engine.error.connect(self._message)
        self.stage.background_error.connect(self._message)
        self.shuffle.toggled.connect(lambda value:setattr(self.engine,'shuffle',value))
        self.repeat.toggled.connect(lambda value:setattr(self.engine,'repeat',value))

    def _task(self, fn, done):
        task=Task(fn,self); self.tasks.append(task); task.done.connect(done); task.failed.connect(self._message)
        task.finished.connect(lambda:self._task_finished(task)); task.start(); return task

    def _task_finished(self, task):
        if task in self.tasks:self.tasks.remove(task)
        task.deleteLater()

    def _message(self, text): self.statusBar().showMessage(str(text),20000)
    def _set_seek_drag(self,value): self._seek_drag=value
    def _seek_released(self):
        self.engine.seek(self.seek.value()/10000*max(0,self.engine.duration)); self._seek_drag=False
    def _seek_action(self,action):
        if not self._seek_drag:self.engine.seek(self.seek.sliderPosition()/10000*max(0,self.engine.duration))
    def _position(self, position):
        self.stage.set_playback(position,self.engine.duration,self.engine.playing)
        self.time_label.setText(clock(position)+' / '+clock(self.engine.duration))
        if not self._seek_drag:self.seek.setValue(round(position/max(1,self.engine.duration)*10000))
        track=self.engine.current_track
        check=(str(track.path),int(position)) if track else None
        if track and check!=self._last_review_poll:
            self._last_review_poll=check
            self._update_program_lyrics(track,self.documents.get(str(track.path)))
    def _playing(self, playing):
        self.play_button.setText('Pause' if playing else 'Play'); self.stage.set_playback(self.engine.position,self.engine.duration,playing)
        if playing and self.engine.current_track:
            self._lyric_state(self.engine.current_track)
            document=self.documents.get(str(self.engine.current_track.path))
            self._update_program_lyrics(self.engine.current_track,document)
            self._display_relevant_document(self.engine.current_track,document)
        if not playing:self.export.clear_audio()
    def toggle_play(self):
        if self.engine.current_track is None and self.playlist.currentItem():self._play_item(self.playlist.currentItem())
        else:self.engine.toggle()
    def _play_item(self,item):
        path=Path(item.data(Qt.ItemDataRole.UserRole))
        index=next((i for i,t in enumerate(self.engine.tracks) if t.path==path),None)
        if index is not None:self.engine.play(index)
    def _track_changed(self,track):
        path=track.path if track else None
        if track and path==self._displayed_track:return
        self._displayed_track=path
        self._track_generation+=1; self.export.clear_audio()
        if not track:
            self.stage.set_track(''); self.stage.set_lyrics(None); return
        self.stage.set_track(track.title,track.artist,track.album); self.stage.set_lyrics(None)
        self.documents.pop(str(track.path),None)
        self.now_title.setText(track.title); self.now_artist.setText(' · '.join(x for x in [track.artist,track.album] if x))
        for i in range(self.playlist.count()):
            item=self.playlist.item(i)
            if Path(item.data(Qt.ItemDataRole.UserRole))==track.path:
                blocked=self.playlist.blockSignals(True); self.playlist.setCurrentItem(item); self.playlist.blockSignals(blocked); break
        self._load_track_lyrics(track)
    def _load_track_lyrics(self,track):
        key=str(track.path); request=self._lyric_requests.get(key,0)+1; self._lyric_requests[key]=request
        generation=self._track_generation
        try:source_stamp=(track.path.stat().st_size,track.path.stat().st_mtime_ns)
        except OSError:source_stamp=None
        def loaded(document):
            if generation!=self._track_generation or request!=self._lyric_requests.get(key):return
            try:unchanged=source_stamp==(track.path.stat().st_size,track.path.stat().st_mtime_ns)
            except OSError:unchanged=False
            if not unchanged:document=None
            self._remember_document(track,document)
            self._refresh_lyric_label(track)
            self._display_relevant_document(track,document)
        def failed(_):
            if generation!=self._track_generation or request!=self._lyric_requests.get(key):return
            self._remember_document(track,None); self._refresh_lyric_label(track); self._display_relevant_document(track,None)
        task=self._task(lambda:self._find_document(track),loaded)
        task.failed.connect(failed)
    def _remember_document(self,track,document):
        key=str(track.path)
        if document:
            self.documents[key]=document; self._invalid_lyrics.discard(key)
            if getattr(document,'_storage_path',None):self._lyric_locations[key]=Path(document._storage_path)
            location=self.lyric_files.get(key) or self._lyric_locations.get(key)
            if location:
                try:
                    state=Path(location).stat()
                    self._lyric_file_stamps[key]=(state.st_size,state.st_mtime_ns,state.st_ctime_ns,state.st_ino)
                except OSError:self._lyric_file_stamps.pop(key,None)
            try:self._lyric_stamps[key]=(track.path.stat().st_size,track.path.stat().st_mtime_ns)
            except OSError:self._invalid_lyrics.add(key)
        else:self.documents.pop(key,None);self._lyric_stamps.pop(key,None);self._invalid_lyrics.add(key)
    def _find_document(self,track):
        explicit=self.lyric_files.get(str(track.path))
        if explicit and Path(explicit).is_file():
            document=load_lyrics(explicit)
            if document.source_sha256 and document.source_sha256!=fingerprint(track.path):raise ValueError('The recording changed; its previous lyrics were kept off-air.')
            document._storage_path=Path(explicit)
            return document
        path=find_lyrics(track.path,Path(self.extract_config.cache_dir))
        if not path:return None
        document=load_lyrics(path); document._storage_path=path
        return document
    def _matches_recording(self,track,document):
        key=str(track.path)
        if not document:return False
        try:
            state=track.path.stat();stamp=(state.st_size,state.st_mtime_ns)
            check_stamp=(state.st_size,state.st_mtime_ns,state.st_ctime_ns,state.st_ino)
            if key in self._lyric_stamps and self._lyric_stamps[key]!=stamp:return False
            location=self.lyric_files.get(key) or self._lyric_locations.get(key)
            if location and key in self._lyric_file_stamps:
                stored=Path(location).stat()
                if self._lyric_file_stamps[key]!=(stored.st_size,stored.st_mtime_ns,stored.st_ctime_ns,stored.st_ino):return False
            checked=self._source_checks.get(key)
            if not checked or checked[0]!=check_stamp:
                identity=fingerprint(track.path)
                after=track.path.stat()
                if check_stamp!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns,after.st_ino):return False
                self._source_checks[key]=(check_stamp,identity)
            return not document.source_sha256 or document.source_sha256==self._source_checks[key][1]
        except OSError:return False
    def _document_ready(self,track,document):
        key=str(track.path)
        if not document or key in self._lyric_buffers or not is_approved(document):return False
        if self._extract_current and self._extract_current.path==track.path:return False
        return self._matches_recording(track,document)
    def _update_program_lyrics(self,track,document):
        if self.engine.current_track and self.engine.current_track.path==track.path:
            safe=self._document_ready(track,document)
            preview=self.allow_drafts.isChecked() and str(track.path) not in self._lyric_buffers and self._matches_recording(track,document)
            self.stage.set_lyrics(document if safe or preview else None)
    def _display_document(self,track,document):
        self.lyric_text.commit_pending_edit()
        key=str(track.path); changed_track=not self._editor_track or self._editor_track.path!=track.path
        previous_revision=self._editor_revision
        self._editor_track=track; self._editor_document=document
        self._editor_revision=document_revision(document) if document else None
        if changed_track or previous_revision!=self._editor_revision:self.review_confirm.setChecked(False)
        if changed_track:
            self.reference.clear()
            blocked=self.allow_drafts.blockSignals(True);self.allow_drafts.setChecked(False);self.allow_drafts.blockSignals(blocked)
            if self.engine.current_track:self._update_program_lyrics(self.engine.current_track,self.documents.get(str(self.engine.current_track.path)))
        buffer=self._lyric_buffers.get(key)
        if document:
            ready=self._document_ready(track,document)
            state='Lyrics ready · reviewed for this recording.' if ready else ('Lyrics need review before airing.' if document.segments else 'Saved empty draft · no lyrics approved.')
            self.lyric_status.setText(track.title+' · '+state)
            if buffer:
                if buffer['revision']!=self._editor_revision:buffer['conflict']=True
                self.lyric_text.set_rows(buffer['rows'])
                self.lyric_save_status.setText('Unsaved changes. '+('The saved revision changed; reload it before editing again.' if buffer.get('conflict') else 'Save draft to keep these changes for this recording.'))
            else:
                self.lyric_text.set_document(document)
                self.lyric_save_status.setText('Saved · '+('Reviewed and approved for this exact revision.' if ready else 'Draft saved on this device. Review notes and lyrics, then approve.'))
        else:
            self.lyric_status.setText(track.title+' · No prepared lyrics for this recording yet.')
            self.lyric_text.set_rows(buffer['rows']) if buffer else self.lyric_text.set_document(None)
            self.lyric_save_status.setText('Unsaved changes retained; the saved recording or lyrics are unavailable.' if buffer else 'Import timed lyrics or use Extract & align to create a saved draft.')
        self.review_notes.setPlainText(stored_review_notes(document))
        self.prepare_actions.setVisible(not document or not document.segments or not self.lyric_options_panel.isHidden())
        self._update_program_lyrics(track,document)
        self._update_review_controls()
    def _display_relevant_document(self,track,document):
        self._update_program_lyrics(track,document)
        shown=self._selected_track()
        if shown and shown.path==track.path:self._display_document(track,document)
    def _reload_current_lyrics(self):
        t=self._selected_track()
        if self.engine.current_track:
            self._update_program_lyrics(self.engine.current_track,self.documents.get(str(self.engine.current_track.path)))
        if not t:return
        self.documents.pop(str(t.path),None)
        if self.engine.current_track and self.engine.current_track.path==t.path:self.stage.set_lyrics(None)
        self._load_track_lyrics(t)
    def _selection_changed(self,current,previous):
        self.lyric_text.commit_pending_edit()
        if current:
            t=next((t for t in self.all_tracks if str(t.path)==current.data(Qt.ItemDataRole.UserRole)),None)
            if t:
                self.lyric_status.setText(t.title+' · '+self._lyric_state(t).title())
                self._display_document(t,self.documents.get(str(t.path)))
                self._load_track_lyrics(t)

    def _toggle_lyric_options(self):
        visible=self.lyric_options_panel.isHidden()
        self.lyric_options_panel.setVisible(visible)
        self.lyric_options.setText('Fewer lyric options' if visible else 'More lyric options')
        self.prepare_actions.setVisible(visible or not self._editor_document or not self._editor_document.segments)

    def _operator_tab_changed(self,index):
        # A smaller operator-only thumbnail leaves the lines and notes visible
        # while reviewing. The separate Program Output keeps its normal size.
        self.preview.setMinimumHeight(100 if index in (1,2) else 270)
        self.preview.setMaximumHeight(140 if index in (1,2) else 340)
        self.monitor_panel.setVisible(not (index==1 and self._expanded_lyrics))

    def _toggle_expanded_lyrics(self,checked=False):
        # Keep the same editor/delegate alive so in-progress text, selection and
        # caret survive expansion. Program Output is a separate window.
        self._expanded_lyrics=bool(checked)
        self.expand_lyrics_button.setText('Restore workspace' if checked else 'Expand editor')
        self._operator_tab_changed(self.tabs.currentIndex())

    def _update_review_controls(self,*_):
        track=self._editor_track; document=self._editor_document
        key=str(track.path) if track else None
        busy=bool(key in self._saving_lyrics or (track and self._extract_current and self._extract_current.path==track.path))
        editable=bool(document)
        available=bool(document and document.segments)
        buffer=self._lyric_buffers.get(key)
        self.lyric_text.setEnabled(editable and not busy)
        self.add_line_button.setEnabled(editable and not busy)
        self.remove_line_button.setEnabled(available and not busy)
        self.line_timing_button.setEnabled(available and not busy)
        self.save_lyrics_button.setEnabled(bool(buffer and not buffer.get('conflict') and not busy))
        self.save_lyrics_button.setText('Save changes' if buffer else 'Save draft')
        self.reload_lyrics_button.setEnabled(bool(track and not busy))
        self.export_lyrics_button.setEnabled(bool(document and not busy))
        self.review_confirm.setEnabled(available and not buffer and not busy and not self._document_ready(track,document))
        self.approve_lyrics_button.setEnabled(available and not buffer and not busy and self.review_confirm.isChecked() and not self._document_ready(track,document))
        missing_words=available and any(not cue.words for cue in document.segments)
        self.realign_button.setEnabled(bool(missing_words and not buffer and not busy and not self.export.running))
        if hasattr(self,'import_button'):self.import_button.setEnabled(not busy)

    def _lyric_edited(self):
        track=self._editor_track; document=self._editor_document
        if not track or not document:return
        key=str(track.path)
        rows=self.lyric_text.raw_rows()
        existing=self._lyric_buffers.get(key)
        if existing and not existing.get('clear_words') and not existing.get('conflict'):
            try:
                unchanged=[(parse_seconds(start),parse_seconds(end),text) for start,end,text in rows]==[(cue.start,cue.end,cue.text) for cue in existing['document'].segments]
            except (ValueError,TypeError):unchanged=False
            if unchanged and not existing.get('session_only'):
                self._lyric_buffers.pop(key,None)
                self.lyric_save_status.setText('Saved · No unsaved lyric changes.')
                self.lyric_status.setText(track.title+' · '+('Lyrics ready · reviewed for this recording.' if self._document_ready(track,document) else 'Lyrics need review before airing.'))
                self._update_program_lyrics(track,document);self._refresh_lyric_label(track);self._update_review_controls()
                return
            if not unchanged:existing.pop('session_only',None)
        if key not in self._lyric_buffers:
            self._lyric_buffers[key]={'track':track,'document':deepcopy(document),'revision':self._editor_revision,
                                     'location':self.lyric_files.get(key) or self._lyric_locations.get(key),
                                     'conflict':False}
        self._lyric_buffers[key]['rows']=rows
        # In-flight searches started before typing cannot replace this buffer.
        self._lyric_requests[key]=self._lyric_requests.get(key,0)+1
        self.review_confirm.setChecked(False)
        self.lyric_save_status.setText('Unsaved changes · Save draft to keep them. Changed lines keep line timing; old word highlights are cleared on save.')
        self.lyric_status.setText(track.title+' · Unsaved changes · lyrics need review.')
        self._update_program_lyrics(track,document)
        self._refresh_lyric_label(track)
        self._update_review_controls()

    def _add_lyric_line(self):
        if not self._editor_document:return
        try:self.lyric_text.add_line(self._editor_document.duration)
        except ValueError as error:self.lyric_save_status.setText(str(error))

    def _use_line_timing(self):
        selected={index.row() for index in self.lyric_text.selectedIndexes()}
        if not selected:return
        self._lyric_edited()
        buffer=self._lyric_buffers.get(str(self._editor_track.path))
        if not buffer:return
        clear=buffer.setdefault('clear_words',set())
        for index in selected:
            start,end,text=buffer['rows'][index]
            try:clear.add((parse_seconds(start),parse_seconds(end),text))
            except ValueError:continue
        self.lyric_save_status.setText('Unsaved changes · Selected lines will use line timing. Save draft to keep this change.')

    def _discard_lyric_edits(self):
        track=self._editor_track
        if not track or str(track.path) in self._saving_lyrics:return
        key=str(track.path)
        if key in self._lyric_buffers:
            answer=QMessageBox.question(self,'Reload saved lyrics?',
                'Discard unsaved changes for '+track.title+' and reload its saved revision?',
                QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
            if answer!=QMessageBox.StandardButton.Discard:return
            self._lyric_buffers.pop(key,None)
        self.review_confirm.setChecked(False)
        self._load_track_lyrics(track)

    def _save_lyric_changes(self,checked=False,*,approve=False):
        self.lyric_text.commit_pending_edit()
        track=self._editor_track; document=self._editor_document
        if not track or not document:return
        key=str(track.path)
        if approve and (not self.review_confirm.isChecked() or key in self._lyric_buffers):
            self.lyric_save_status.setText('Save your draft, then check the saved lyrics against this recording before approving.')
            return
        buffer=self._lyric_buffers.get(key)
        if not buffer:
            if not approve:return
            buffer={'track':track,'document':deepcopy(document),'revision':self._editor_revision,
                    'location':self.lyric_files.get(key) or self._lyric_locations.get(key),
                    'rows':self.lyric_text.raw_rows(),'conflict':False}
        self._start_lyric_save(buffer,approve=approve)

    def _start_lyric_save(self,buffer,*,approve=False):
        track=buffer['track']; key=str(track.path)
        if key in self._saving_lyrics or (self._extract_current and self._extract_current.path==track.path):
            self.lyric_save_status.setText('Wait for this recording’s current save or preparation to finish.'); return False
        if buffer.get('session_only'):
            # The lyric file already committed; retry only the session link.
            # Rewriting it would revoke a just-saved approval or create history
            # for a change that did not happen.
            try:
                current=load_lyrics(buffer['location'])
                if document_revision(current)!=buffer['revision']:
                    raise ValueError('The saved lyric file changed. Reload it before retrying.')
                if not self.save_session():raise OSError('Session save did not complete.')
            except (OSError,ValueError,TypeError) as error:
                self._close_after_lyric_save=False
                self.lyric_save_status.setText('Lyrics are saved; session save did not complete · '+str(error)+' Retry Save changes.');return False
            self._lyric_buffers.pop(key,None)
            self._lyrics_ready(track,Path(buffer['location']),current)
            self._update_review_controls()
            if getattr(self,'_close_after_lyric_save',False) and not self._lyric_buffers and not self._saving_lyrics:QTimer.singleShot(0,self.close)
            return True
        try:
            if buffer.get('conflict'):raise ValueError('The saved revision changed. Reload saved lyrics before editing again.')
            rows=[]
            for number,(start,end,text) in enumerate(buffer['rows'],1):
                try:rows.append((parse_seconds(start),parse_seconds(end),text))
                except ValueError as error:raise ValueError(f'Line {number}: {error}') from error
            if track.duration>0 and any(end>track.duration for _,end,_ in rows):
                raise ValueError('A line ends beyond this recording’s measured duration.')
            base=deepcopy(buffer['document'])
            cleared=[]
            for index,cue in enumerate(base.segments):
                if (cue.start,cue.end,cue.text) in buffer.get('clear_words',set()):cue.words=[];cleared.append(index)
            if cleared:base.human_review['alignment_needed']=sorted(set(base.human_review.get('alignment_needed',[]))|set(cleared))
            document=base if approve else edit_document(base,rows)
        except (ValueError,TypeError) as error:
            self.lyric_save_status.setText('Not saved · '+str(error)); self._close_after_lyric_save=False; return False
        expected=buffer['revision']; location=buffer['location']
        self._saving_lyrics.add(key); self.review_confirm.setChecked(False)
        self.lyric_save_status.setText('Saving '+track.title+'…'); self._update_review_controls()
        def work():
            target=Path(location) if location else cached_lyrics_path(track.path,Path(self.extract_config.cache_dir))
            candidate=document
            revision=expected if location or target.is_file() else None
            if target.suffix.lower()!='.json':
                # Auto-discovered subtitle sidecars stay in their own format.
                # Save the reviewed revision as source-bound JSON in our cache.
                if document_revision(load_lyrics(target))!=expected:raise ValueError('The original timed lyric file changed. Reload it before saving.')
                target=cached_lyrics_path(track.path,Path(self.extract_config.cache_dir))
                current=load_lyrics(target) if target.is_file() else None
                revision=document_revision(current) if current else None
                candidate=retarget_review(candidate,current)
            return target,save_review_document(candidate,target,track.path,revision,approve=approve)
        def done(result):
            target,saved=result; self._saving_lyrics.discard(key)
            if self._lyric_buffers.get(key) is buffer:self._lyric_buffers.pop(key,None)
            try:current=load_lyrics(target)
            except (OSError,ValueError,TypeError):current=None
            if current is None or current.to_dict()!=saved.to_dict():
                self._close_after_lyric_save=False
                self._load_track_lyrics(track)
                if self._editor_track and self._editor_track.path==track.path:self.lyric_save_status.setText('The saved revision changed again. Reload and review the current lyrics before approval.')
                self._update_review_controls(); return
            self._lyrics_ready(track,target,saved)
            if not self.save_session():
                self._close_after_lyric_save=False
                # Keep an explicit retryable state even though the atomic lyric
                # write succeeded. Never report a fully saved/reopenable edit
                # before its recording-to-file link is durable.
                self._lyric_buffers[key]={'track':track,'document':deepcopy(saved),
                    'revision':document_revision(saved),'location':str(target),
                    'rows':[(str(c.start),str(c.end),c.text) for c in saved.segments],
                    'conflict':False,'session_only':True}
                if self._editor_track and self._editor_track.path==track.path:
                    self.lyric_save_status.setText('Lyrics are saved; session save did not complete. Your lyrics are kept at '+str(target)+'. Retry Save changes before closing.')
                self._update_program_lyrics(track,saved);self._refresh_lyric_label(track);self._update_review_controls();return
            if self._editor_track and self._editor_track.path==track.path:
                self.lyric_save_status.setText('Saved · '+('Reviewed and approved for this recording and revision.' if is_approved(saved) else 'Draft saved. Check the lyrics, then approve for airing.'))
            self._update_review_controls()
            if getattr(self,'_close_after_lyric_save',False) and not self._lyric_buffers and not self._saving_lyrics:QTimer.singleShot(0,self.close)
        def failed(error):
            self._saving_lyrics.discard(key); self._close_after_lyric_save=False
            if self._editor_track and self._editor_track.path==track.path:self.lyric_save_status.setText('Not saved · '+str(error)+' Your edits are still here.')
            self._update_review_controls()
        task=self._task(work,done); task.failed.connect(failed)
        return True

    def _export_saved_lyrics(self):
        self.lyric_text.commit_pending_edit()
        track=self._editor_track; document=self._editor_document
        if not track or not document:return
        key=str(track.path)
        if key in self._lyric_buffers or key in self._saving_lyrics:
            self.lyric_save_status.setText('Save changes before exporting a separate copy of these lyrics.');return
        try:
            current=self._find_document(track)
            if current is None or document_revision(current)!=self._editor_revision:
                raise ValueError('The saved revision changed. Reload saved lyrics before exporting.')
            name,_=QFileDialog.getSaveFileName(self,'Export saved lyrics',str(Path.home()/(track.path.stem+'.lyrics.json')),'Timed lyric JSON (*.json)')
            if not name:return
            target=Path(name)
            if target.suffix.lower()!='.json':target=target.with_name(target.name+'.json')
            if target.resolve()==track.path.resolve():raise ValueError('Save lyrics separately from the recording.')
            location=self.lyric_files.get(key) or self._lyric_locations.get(key)
            if location and target.resolve()==Path(location).resolve():raise ValueError('Choose a separate file for export; Save changes updates the current lyric file.')
            save_lyrics(current,target)
            self.lyric_save_status.setText('Exported saved lyrics · '+str(target))
        except (OSError,ValueError,TypeError) as error:
            self.lyric_save_status.setText('Could not export · '+str(error))

    def _align_edited_lyrics(self):
        track=self._editor_track; document=self._editor_document
        if not track or not document:return
        if str(track.path) in self._lyric_buffers:
            self.lyric_save_status.setText('Save the edited lines before requesting new word highlights.'); return
        if self.export.running:
            self._message('Stop the output before preparing lyrics.'); return
        self._queue_extract([(track,'\n'.join(cue.text for cue in document.segments))])

    def _has_lyrics(self,track):
        return self._lyric_state(track)=='LYRICS READY'
    def _lyric_state(self,track):
        key=str(track.path)
        if key in self._invalid_lyrics:return 'MUSIC ONLY'
        try:stamp=(track.path.stat().st_size,track.path.stat().st_mtime_ns)
        except OSError:return 'MUSIC ONLY'
        if key in self._lyric_stamps and self._lyric_stamps[key]!=stamp:
            self.documents.pop(key,None);self._invalid_lyrics.add(key);return 'MUSIC ONLY'
        document=self.documents.get(key)
        if document is None and self.lyric_files.get(key):
            try: document=load_lyrics(self.lyric_files[key]); self._remember_document(track,document)
            except (OSError,ValueError,TypeError): return 'MUSIC ONLY'
        if not document or not document.segments:return 'MUSIC ONLY'
        return 'LYRICS READY' if self._document_ready(track,document) else 'LYRICS NEED REVIEW'
    def _playlist_text(self,track):
        return f'{track.title}\n{track.artist or "Goodneighbor Blues"}  ·  {clock(track.duration)}  ·  {self._lyric_state(track)}'
    def _refresh_lyric_label(self,track):
        if self.ready_only.isChecked() and any(t.path==track.path for t in self.engine.tracks) and not self._has_lyrics(track):
            self._apply_queue(); return
        for i in range(self.playlist.count()):
            item=self.playlist.item(i)
            if item.data(Qt.ItemDataRole.UserRole)==str(track.path):item.setText(self._playlist_text(track))
        self._filter_rows()
    def _apply_queue(self):
        selected=[t for t in self.all_tracks if not self.ready_only.isChecked() or self._has_lyrics(t)]
        self.engine.set_queue(selected); self._rebuild_list(selected); self._schedule_save()
    def _rebuild_list(self,tracks):
        current=self.playlist.currentItem(); old=current.data(Qt.ItemDataRole.UserRole) if current else None
        selected={item.data(Qt.ItemDataRole.UserRole) for item in self.playlist.selectedItems()}
        if old is None and self.engine.current_track:old=str(self.engine.current_track.path)
        self.playlist.blockSignals(True); self.playlist.clear()
        for t in tracks:
            item=QListWidgetItem(self._playlist_text(t))
            item.setData(Qt.ItemDataRole.UserRole,str(t.path)); item.setToolTip(str(t.path)); self.playlist.addItem(item)
            if old==str(t.path):self.playlist.setCurrentItem(item)
        if not self.playlist.currentItem() and self.playlist.count():self.playlist.setCurrentRow(0)
        if selected:
            for i in range(self.playlist.count()):self.playlist.item(i).setSelected(self.playlist.item(i).data(Qt.ItemDataRole.UserRole) in selected)
        self.playlist.blockSignals(False); self.library_count.setText(f'{len(tracks)} tracks'); self._filter_rows()
    def _filter_rows(self):
        query=self.search.text().casefold()
        for i in range(self.playlist.count()):self.playlist.item(i).setHidden(query not in self.playlist.item(i).text().casefold())
    def _reordered(self):
        order={self.playlist.item(i).data(Qt.ItemDataRole.UserRole):i for i in range(self.playlist.count())}
        shown=sorted([t for t in self.all_tracks if str(t.path) in order],key=lambda t:order[str(t.path)])
        self.all_tracks=shown+[t for t in self.all_tracks if str(t.path) not in order]; self._apply_queue()
    def remove_selected(self):
        paths={i.data(Qt.ItemDataRole.UserRole) for i in self.playlist.selectedItems()}
        self.all_tracks=[t for t in self.all_tracks if str(t.path) not in paths]; self._apply_queue()
    def add_files(self):
        paths,_=QFileDialog.getOpenFileNames(self,'Add recordings',str(Path.home()),'Media files ('+' '.join('*'+x for x in sorted(MEDIA_EXTENSIONS))+');;All files (*)')
        if paths:self.scan_paths(paths)
    def add_folder(self):
        folder=QFileDialog.getExistingDirectory(self,'Add music or album folder')
        if folder:self.scan_paths([folder])
    def scan_paths(self,paths):
        if self._scan_busy:self._message('A folder is already being scanned.'); return
        self._scan_busy=True; self._message('Reading recordings and album metadata…')
        def done(tracks):
            self._scan_busy=False; existing={t.path for t in self.all_tracks}; self.all_tracks.extend(t for t in tracks if t.path not in existing); self._apply_queue(); self._message(f'Added {len(tracks)} recordings. Original files were left in place.')
        task=self._task(lambda:scan_media(paths,cancelled=lambda:QThread.currentThread().isInterruptionRequested()),done); task.failed.connect(lambda _:setattr(self,'_scan_busy',False))
    def open_playlist(self):
        name,_=QFileDialog.getOpenFileName(self,'Open broadcast playlist','','Station playlist (*.json *.pbstation);;M3U playlist (*.m3u *.m3u8)')
        if not name:return
        try:
            if Path(name).suffix.lower() in {'.m3u','.m3u8'}:self.scan_paths(load_m3u(Path(name)))
            else:self.load_profile(Path(name))
        except Exception as error:self._message(str(error))
    def load_profile(self,path):
        data=read_profile(path); self.all_tracks=data['tracks']; self.backgrounds=data['backgrounds']; self.lyric_files=data['lyrics']; self.profile_name=data.get('name','My station')
        settings=data.get('settings',{}); self.brand.setText(settings.get('brand','GOODNEIGHBOR BLUES')); self.tagline.setText(settings.get('tagline','Original blues. Stay awhile.'))
        self.interval.setValue(max(5,min(300,int(settings.get('interval_seconds',18)))))
        for key,widget in [('motion',self.motion),('waveform',self.wave),('lyrics',self.lyrics_visible)]:widget.setChecked(bool(settings.get(key,True)))
        self.set_waveform_color(settings.get('waveform_color',DEFAULT_WAVEFORM_COLOR))
        self.documents.clear();self._lyric_stamps.clear();self._invalid_lyrics.clear();self.engine.set_queue([],preserve_current=False); self.ready_only.setChecked(bool(data.get('settings',{}).get('lyrics_ready_only',False))); self._apply_queue(); self.stage.set_backgrounds(self.backgrounds); self.art_count.setText(f'{len(self.backgrounds)} artwork / video backdrops'); self.apply_appearance()
        self._message(f'{self.profile_name}: {len(self.all_tracks)} recordings and {len(self.lyric_files)} prepared lyric files loaded.')
    def save_playlist(self):
        name,_=QFileDialog.getSaveFileName(self,'Save playlist',self.profile_name+'.json','Station playlist (*.json);;M3U playlist (*.m3u)')
        if not name:return
        try:
            if Path(name).suffix.lower()=='.m3u':save_m3u(Path(name),self.all_tracks)
            else:write_profile(Path(name),self.all_tracks,self.backgrounds,self.lyric_files,self.visual_settings(),self.profile_name)
            self._message('Playlist saved.')
        except Exception as error:self._message(str(error))
    def add_backgrounds(self):
        paths,_=QFileDialog.getOpenFileNames(self,'Add artwork or looping video','','Artwork and video ('+' '.join('*'+x for x in sorted(IMAGE_SUFFIXES|VIDEO_SUFFIXES))+')')
        self.backgrounds=list(dict.fromkeys(self.backgrounds+[Path(p).resolve() for p in paths])); self.stage.set_backgrounds(self.backgrounds); self.art_count.setText(f'{len(self.backgrounds)} artwork / video backdrops'); self._schedule_save()
    def visual_settings(self):return {'brand':self.brand.text(),'tagline':self.tagline.text(),'interval_seconds':self.interval.value(),'motion':self.motion.isChecked(),'waveform':self.wave.isChecked(),'waveform_color':self._waveform_color,'lyrics':self.lyrics_visible.isChecked(),'lyrics_ready_only':self.ready_only.isChecked()}
    def _update_waveform_color_button(self):
        color=QColor(self._waveform_color)
        text='#101c24' if color.lightnessF()>.5 else '#ffffff'
        self.wave_color.setText(self._waveform_color.upper()+' · Choose…')
        self.wave_color.setStyleSheet(f'background:{self._waveform_color};color:{text};')
    def set_waveform_color(self,value):
        self._waveform_color=waveform_color(value)
        self._update_waveform_color_button(); self.apply_appearance()
    def choose_waveform_color(self):
        color=QColorDialog.getColor(QColor(self._waveform_color),self,'Waveform color')
        if color.isValid():self.set_waveform_color(color.name())
    def apply_appearance(self):
        settings=self.visual_settings(); settings.pop('lyrics_ready_only',None); self.stage.set_visual_settings(**settings); self._schedule_save()
    def show_output(self):self.stage.show(); self.stage.raise_()
    def _preview(self):
        if self.export.running:
            image=self.stage.render_frame(*self._export_size); self.export.set_video_frame(image)
        else:image=self.stage.render_frame(640,360)
        self.preview.setPixmap(QPixmap.fromImage(image).scaled(self.preview.size(),Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation))

    def _selected_track(self):
        item=self.playlist.currentItem()
        return next((t for t in self.all_tracks if item and str(t.path)==item.data(Qt.ItemDataRole.UserRole)),self.engine.current_track)
    def import_lyrics(self):
        track=self._selected_track()
        if not track:return
        key=str(track.path)
        if key in self._lyric_buffers or key in self._saving_lyrics or (self._extract_current and self._extract_current.path==track.path):
            self.lyric_save_status.setText('Save or reload this recording’s edits and wait for preparation before importing.'); return
        name,_=QFileDialog.getOpenFileName(self,'Import timed lyrics','','Timed lyrics (*.json *.lrc *.srt *.vtt)')
        if not name:return
        self._saving_lyrics.add(key); self._update_review_controls()
        def work():
            document=load_lyrics(name); identity=fingerprint(track.path)
            if document.source_sha256 and document.source_sha256!=identity:raise ValueError('This lyric file belongs to a different recording. Import the matching recording, or align the exact words to this one.')
            document.source_sha256=identity; document.title=track.title
            target=cached_lyrics_path(track.path,Path(self.extract_config.cache_dir))
            current=load_lyrics(target) if target.is_file() else None
            expected=document_revision(current) if current else None
            document=retarget_review(document,current)
            return target,save_review_document(document,target,track.path,expected)
        def done(result):
            self._saving_lyrics.discard(key); self._lyrics_ready(track,*result); self._update_review_controls()
        def failed(error):
            self._saving_lyrics.discard(key)
            if self._editor_track and self._editor_track.path==track.path:self.lyric_save_status.setText('Import not saved · '+str(error))
            self._update_review_controls()
        task=self._task(work,done); task.failed.connect(failed)
    def _lyrics_ready(self,track,target,document):
        key=str(track.path); self._lyric_requests[key]=self._lyric_requests.get(key,0)+1
        self._lyric_locations[key]=Path(target)
        shown=self._editor_track
        if not is_approved(document) and ((shown and shown.path==track.path) or (self.engine.current_track and self.engine.current_track.path==track.path)):
            blocked=self.allow_drafts.blockSignals(True);self.allow_drafts.setChecked(False);self.allow_drafts.blockSignals(blocked)
        self.lyric_files[str(track.path)]=str(target);self._remember_document(track,document)
        self._apply_queue(); self._schedule_save()
        self._display_relevant_document(track,document)
    def queue_extraction(self,reference=False):
        track=self._selected_track()
        if not track:self._message('Choose a recording first.'); return
        text=self.reference.toPlainText().strip() if reference else ''
        if reference and not text:self._message('Paste the exact lyric sheet before aligning it.'); return
        self._queue_extract([(track,text)])
    def queue_missing(self):self._queue_extract([(t,'') for t in self.all_tracks if not self.lyric_files.get(str(t.path)) and str(t.path) not in self.documents])
    def _queue_extract(self,jobs):
        if self.export.running:self._message('Stop the output before preparing lyrics. Playback can continue.'); return
        if any(str(track.path) in self._lyric_buffers or str(track.path) in self._saving_lyrics for track,_ in jobs):
            self._message('Save or reload the edited draft before preparing this recording again.'); return
        if not self.save_model_settings():return
        for track,text in jobs:
            if all(t.path!=track.path for t,_ in self._extract_queue):self._extract_queue.append((track,text))
        self._next_extraction()
    def _next_extraction(self):
        if self.extract_process.state()!=QProcess.ProcessState.NotRunning or not self._extract_queue:return
        track,text=self._extract_queue.pop(0); self._extract_current=track; self._process_bytes=b''; self._extract_log=''
        try:
            target=cached_lyrics_path(track.path,Path(self.extract_config.cache_dir)); args=build_command(track.path,target,self.extract_config,text)
            self._extract_context={'track':track,'target':target}
            self.extract_progress.setValue(0); self.lyric_status.setText('Preparing lyrics · '+track.title); self.cancel_extract.setEnabled(True)
            self.preparation_progress.show()
            self.review_confirm.setChecked(False); self._update_review_controls()
            self._update_program_lyrics(track,self.documents.get(str(track.path)))
            environment=QProcessEnvironment.systemEnvironment()
            environment.insert('PYTHONUTF8','1')
            environment.insert('PYTHONIOENCODING','utf-8')
            if getattr(sys,'frozen',False) and os.name!='nt':
                original=os.environ.get('LD_LIBRARY_PATH_ORIG')
                if original:environment.insert('LD_LIBRARY_PATH',original)
                else:environment.remove('LD_LIBRARY_PATH')
            self.extract_process.setProcessEnvironment(environment)
            if getattr(sys,'frozen',False) and os.name=='nt':
                # The model Python must resolve its own Torch/runtime DLLs.
                import ctypes
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                set_directory=kernel.SetDllDirectoryW
                set_directory.argtypes=[ctypes.c_wchar_p];set_directory.restype=ctypes.c_int
                if not set_directory(None):raise OSError(ctypes.get_last_error(),'Could not isolate the model runtime DLL search path.')
                try:
                    self.extract_process.start(args[0],args[1:])
                    self.extract_process.waitForStarted(3000)
                finally:set_directory(str(sys._MEIPASS))
            else:self.extract_process.start(args[0],args[1:])
        except Exception as error:self._message(str(error)); self._extract_current=None; self._extract_context=None; self.preparation_progress.hide(); self._update_review_controls()
    def _extract_output(self):
        self._process_bytes+=bytes(self.extract_process.readAllStandardOutput())
        while b'\n' in self._process_bytes:
            line,self._process_bytes=self._process_bytes.split(b'\n',1); event=parse_worker_event(line)
            if not event:continue
            if event['type']=='progress':self.extract_progress.setValue(round(float(event.get('fraction',0))*100)); self.lyric_status.setText(str(event.get('message','Working…')))
            elif event['type']=='done':
                try:
                    target=Path(event['output'])
                    if not self._extract_current or not self._extract_context or target!=self._extract_context['target']:raise ValueError('An unexpected preparation result was ignored.')
                    document=load_lyrics(target)
                    # An extracted/aligned revision is always a draft. The worker
                    # never creates a human approval; reject an unexpected one.
                    if document.human_review.get('status')=='approved':raise ValueError('A preparation result unexpectedly contained an approval; reopen the saved draft for review.')
                    self._lyrics_ready(self._extract_current,target,document); self.extract_progress.setValue(100)
                except Exception as error:self._message(str(error))
            elif event['type']=='error':self._message(str(event.get('message','Lyric extraction failed.')))
    def _extract_stderr(self):self._extract_log=(self._extract_log+bytes(self.extract_process.readAllStandardError()).decode(errors='replace'))[-4000:]
    def _process_error(self,error):
        self._message(self.extract_process.errorString())
        if error==QProcess.ProcessError.FailedToStart:
            self._extract_queue.clear(); self._extract_current=None; self._extract_context=None; self.preparation_progress.hide(); self.cancel_extract.setEnabled(False); self.lyric_status.setText('The lyric runtime could not start. Check its Python path in Lyric models.'); self._update_review_controls()
    def _extract_finished(self,code,status):
        self._extract_output(); self.cancel_extract.setEnabled(False)
        if code and not self._closing:self.lyric_status.setText('Extraction stopped or failed. Existing lyrics were preserved. '+self._extract_log[-250:])
        completed=self._extract_current; self._extract_current=None; self._extract_context=None
        if not self._extract_queue:self.preparation_progress.hide()
        if completed:self._display_relevant_document(completed,self.documents.get(str(completed.path))); self._refresh_lyric_label(completed)
        self._update_review_controls()
        if not self._closing:QTimer.singleShot(300,self._next_extraction)
    def cancel_extraction(self):
        self._extract_queue.clear()
        if self.extract_process.state()!=QProcess.ProcessState.NotRunning:
            if os.name=='nt':
                import subprocess
                subprocess.run(['taskkill','/PID',str(self.extract_process.processId()),'/T','/F'],capture_output=True)
            else:self.extract_process.terminate()
            QTimer.singleShot(3500,self._kill_extraction_if_running)
        self._message('Cancelling extraction. Existing lyric files will be kept.')
    def _kill_extraction_if_running(self):
        if self.extract_process.state()==QProcess.ProcessState.NotRunning:return
        if os.name!='nt':
            import signal
            try:os.killpg(int(self.extract_process.processId()),signal.SIGKILL)
            except (ProcessLookupError,PermissionError):self.extract_process.kill()
        else:self.extract_process.kill()
    def save_model_settings(self):
        try:
            values=self.extract_config.to_dict(); values.update({key:entry.text().strip() for key,entry in self.model_fields.items()}); values['use_gpu']=self.gpu.isChecked(); self.extract_config=ExtractionConfig.from_dict(values); self.update_model_status(); self._schedule_save(); return True
        except Exception as error:self._message(str(error)); return False
    def open_model_setup(self):
        if self.export.running or self.extract_process.state()!=QProcess.ProcessState.NotRunning:
            self._message('Finish the current output or extraction before changing model setup.');return
        existing=getattr(self,'model_setup_dialog',None)
        if existing and existing.isVisible():existing.raise_();existing.activateWindow();return
        if existing:existing.deleteLater()
        from .model_setup_dialog import ModelSetupDialog
        self.model_setup_dialog=ModelSetupDialog(self.extract_config,self.data_dir,self)
        self.model_setup_dialog.configured.connect(self._model_setup_applied)
        self.model_setup_dialog.show()
    def _model_setup_applied(self,config):
        self.extract_config=ExtractionConfig.from_dict(config.to_dict())
        for key,entry in self.model_fields.items():entry.setText(str(getattr(self.extract_config,key)))
        self.gpu.setChecked(self.extract_config.use_gpu);self.update_model_status()
        if self.save_session():self._message('Model settings saved. Selected recognizer: '+self.extract_config.engine)
    def update_model_status(self):
        status=config_status(self.extract_config); self.model_status.setText('Local lyric models detected.' if status['ready'] else 'Playback is ready. Lyric extraction needs a configured runtime and model files. '+ '; '.join(status['missing_models'][:2]))

    def _stream_fields_changed(self, *_):
        if not self._profile_busy:self.stream_profile_status.setText('Current fields are session-only until Save/update. Clear fields does not forget a saved profile.')

    def _profile_controls(self):
        enabled=not self._profile_busy and not self.export.running
        for widget in (self.save_stream_button,self.restore_stream_button,self.forget_stream_button,self.clear_stream_button,
                       self.output_mode,self.destination,self.stream_key,self.resolution,self.bitrate,self.choose_recording_button):widget.setEnabled(enabled)
        self.start_output.setEnabled(enabled)

    def _profile_operation(self, action, profile=None):
        if self._profile_busy or self.export.running:
            self.stream_profile_status.setText('Stop output and wait for the current profile operation before changing saved credentials.'); return
        self._profile_busy=True; self._profile_controls()
        self.stream_profile_status.setText('Waiting for protected credential storage… Unlock your desktop keyring if prompted.')
        def work():
            try:
                if action=='save':self.credential_store.save(profile); return True,None
                if action=='restore':return True,self.credential_store.load()
                self.credential_store.forget(); return True,None
            except ProfileError as error:return False,str(error)
            except Exception:return False,'Protected credential storage could not finish. Current fields remain session-only; retry Save/update or Forget.'
        def finished(result):
            ok,value=result
            if ok and action=='restore' and value is not None:
                self.output_mode.setCurrentIndex(1); self.destination.setText(value.destination); self.stream_key.setText(value.stream_key)
                self.resolution.setCurrentIndex(value.picture_index); self.bitrate.setValue(value.bitrate_kbps)
            if ok and action=='forget':
                self.stream_key.clear()
                if self.output_mode.currentIndex()==1:self.destination.clear()
            self._profile_busy=False; self._profile_controls()
            if not ok:self.stream_profile_status.setText(value)
            elif action=='save':self.stream_profile_status.setText('Saved securely on this computer for this data folder. Restores next launch; Start output is always explicit.')
            elif action=='forget':self.stream_profile_status.setText('Forgot both protected copies and cleared the saved reference. Streaming fields cleared.')
            elif value is None:self.stream_profile_status.setText('No saved profile for this data folder. Current fields remain session-only.')
            else:self.stream_profile_status.setText('Restored securely. Check the destination and press Start output when ready.')
        self._task(work,finished)

    def save_stream_profile(self):
        if self.output_mode.currentIndex()!=1:
            self.stream_profile_status.setText('Choose Direct livestream to save a streaming profile. Recording filenames are not saved here.'); return
        profile=StreamProfile(self.destination.text().strip(),self.stream_key.text().strip(),self.resolution.currentIndex(),self.bitrate.value())
        try:profile.validate()
        except ProfileError as error:self.stream_profile_status.setText(str(error)); return
        self._profile_operation('save',profile)

    def restore_stream_profile(self):self._profile_operation('restore')
    def forget_stream_profile(self):self._profile_operation('forget')
    def clear_stream_fields(self):
        if self._profile_busy or self.export.running:return
        self.destination.clear(); self.stream_key.clear()
        self.stream_profile_status.setText('Cleared this session’s fields. A saved protected profile is still available through Restore saved.')

    def _redact_broadcast(self, message):
        from .broadcast import redact
        config=getattr(self.export,'_config',None)
        result=redact(message,self.destination.text(),(self.stream_key.text(),))
        if config and config.mode=='stream':result=redact(result,config.target,config.secrets)
        return result

    def _broadcast_error(self,message):self._message(self._redact_broadcast(message))

    def choose_recording(self):
        name,_=QFileDialog.getSaveFileName(self,'Save local broadcast recording',str(Path.home()/'broadcast.mp4'),'MP4 recording (*.mp4);;Matroska recording (*.mkv)')
        if name:self.destination.setText(name); self.output_mode.setCurrentIndex(0)
    def start_broadcast(self):
        if self._profile_busy:self._message('Wait for the protected profile operation before starting output.'); return
        if self.extract_process.state()!=QProcess.ProcessState.NotRunning:self._message('Finish or cancel lyric extraction before starting output.'); return
        target=self.destination.text().strip()
        if not target:self.choose_recording(); target=self.destination.text().strip()
        if not target:return
        from .broadcast import BroadcastConfig
        mode='stream' if self.output_mode.currentIndex()==1 else 'record'
        if mode=='stream' and self.stream_key.text().strip():target=target.rstrip('/')+'/'+self.stream_key.text().strip().lstrip('/')
        self._export_size=(1920,1080) if self.resolution.currentIndex()==1 else (1280,720)
        try:
            config=BroadcastConfig(mode=mode,target=target,width=self._export_size[0],height=self._export_size[1],fps=30,bitrate_kbps=self.bitrate.value(),secrets=(self.stream_key.text(),self.destination.text()) if mode=='stream' else ())
            self.export.set_video_frame(self.stage.render_frame(*self._export_size)); self.export.start(config)
            self.preview_timer.setInterval(33); self.start_output.setEnabled(False); self.stop_output.setEnabled(True); self._profile_controls()
            for b in [self.extract_button,self.batch_button,self.align_button]:b.setEnabled(False)
            if not self.engine.playing:self.toggle_play()
        except Exception as error:self._broadcast_error(str(error)); self._export_finished()
    def stop_broadcast(self):self.export.stop(); self.broadcast_label.setText('Finishing output…')
    def _export_finished(self):
        self.preview_timer.setInterval(50); self.start_output.setEnabled(True); self.stop_output.setEnabled(False); self._profile_controls()
        for b in [self.extract_button,self.batch_button,self.align_button]:b.setEnabled(True)
        if self._closing:QTimer.singleShot(0,self.close)
    def _broadcast_status(self,message):self.broadcast_label.setText(self._redact_broadcast(message))
    def _broadcast_statistics(self,data):
        self.broadcast_stats.setText(' · '.join(f'{key.replace("_"," ")}: {value}' for key,value in data.items() if key in {'running','elapsed_seconds','video_frames','duplicated_video_frames','replaced_video_inputs','audio_frames','audio_dropped_frames','silence_frames','audio_buffer_seconds','delivery_lag_seconds','reconnects'} and type(value) in (int,float,bool)))
    def _schedule_save(self):
        if hasattr(self,'save_timer'):self.save_timer.start(600)
    def save_session(self):
        try:
            write_profile(self.data_dir/'session.json',self.all_tracks,self.backgrounds,self.lyric_files,self.visual_settings(),self.profile_name)
            try:settings=json.loads(self.settings_file.read_text())
            except (OSError,ValueError):settings={}
            if not isinstance(settings,dict):settings={}
            settings['extraction']=self.extract_config.to_dict(); atomic_json(self.settings_file,settings)
            return True
        except Exception as error:self._message('Could not save session: '+str(error));return False
    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_F11:self.show_output(); self.stage.showFullScreen(); event.accept()
        elif event.key()==Qt.Key.Key_Space and not isinstance(QApplication.focusWidget(),(QLineEdit,QPlainTextEdit,TimedLyricsEditor)):
            self.toggle_play(); event.accept()
        else:super().keyPressEvent(event)
    def closeEvent(self,event):
        self.lyric_text.commit_pending_edit()
        if not self._closing and self._lyric_buffers:
            if self._saving_lyrics:
                self._message('Waiting for lyric saves before closing…'); event.ignore(); QTimer.singleShot(500,self.close); return
            answer=QMessageBox.question(self,'Save lyric changes?',
                f'{len(self._lyric_buffers)} recording(s) have unsaved lyric changes. Save drafts before closing?',
                QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if answer==QMessageBox.StandardButton.Cancel:event.ignore(); return
            if answer==QMessageBox.StandardButton.Save:
                self._close_after_lyric_save=True
                for buffer in list(self._lyric_buffers.values()):
                    if not self._start_lyric_save(buffer):self._close_after_lyric_save=False;break
                event.ignore(); return
            self._lyric_buffers.clear()
        setup=getattr(self,'model_setup_dialog',None)
        if setup and (setup._busy or setup.tasks):
            setup.close();event.ignore();QTimer.singleShot(500,self.close);return
        if self.export.running:
            answer=QMessageBox.question(self,'Finish broadcast?','Stop the output and close the station?',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No)
            if answer!=QMessageBox.StandardButton.Yes:event.ignore(); return
            self._closing=True; self.export.stop(); event.ignore(); return
        if self.extract_process.state()!=QProcess.ProcessState.NotRunning:
            self._closing=True; self.cancel_extraction(); event.ignore(); QTimer.singleShot(500,self.close); return
        if self.tasks:
            self._closing=True
            for task in self.tasks:task.requestInterruption()
            self._message('Finishing the current library task before closing…'); event.ignore(); QTimer.singleShot(500,self.close); return
        self._closing=True; self.save_session(); self.engine.stop(); self.stage.shutdown(); self.stage.close(); self.preview_timer.stop(); event.accept()

def main(argv=None):
    parser=argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument('--profile',type=Path); parser.add_argument('--data-dir',type=Path,default=default_data_dir())
    parser.add_argument('--show-output',action='store_true'); parser.add_argument('--autoplay',action='store_true')
    parser.add_argument('--setup-models',action='store_true',help='Open the guided local model setup')
    args=parser.parse_args(argv)
    os.environ.setdefault('QT_MEDIA_BACKEND','ffmpeg')
    if getattr(sys,'frozen',False):
        bundled=Path(sys._MEIPASS)/'bin'; os.environ['PATH']=str(bundled)+os.pathsep+os.environ.get('PATH','')
        roots=[Path(sys.executable).parent,Path(sys.executable).parent.parent.parent]
    else:roots=[Path(__file__).resolve().parents[2]]
    if args.profile is None:args.profile=starter_profile(args.data_dir,roots)
    app=QApplication.instance() or QApplication(sys.argv[:1]); app.setApplicationName(APP_NAME); app.setOrganizationName('Plague Broadcast'); app.setStyleSheet(STYLE)
    app.setWindowIcon(application_icon()); app.setDesktopFileName('plagues-broadcast-station')
    window=MainWindow(args.data_dir,args.profile); window.show()
    if args.show_output:window.show_output()
    if args.setup_models:QTimer.singleShot(0,window.open_model_setup)
    if args.autoplay:QTimer.singleShot(300,window.toggle_play)
    return app.exec()
