"""Operator desk for Plague's Broadcast Station. The program window stays clean."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QProcess, QProcessEnvironment, QSize
from PySide6.QtGui import QPixmap, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QListWidget, QListWidgetItem, QAbstractItemView,
    QFileDialog, QMessageBox, QTabWidget, QLineEdit, QPlainTextEdit, QSlider,
    QCheckBox, QDoubleSpinBox, QSpinBox, QFormLayout, QComboBox, QProgressBar,
    QSplitter, QGroupBox, QScrollArea,
)
from .library import Track, scan_media, MEDIA_EXTENSIONS
from .playback import PlaybackEngine
from .stage import BroadcastStage, IMAGE_SUFFIXES, VIDEO_SUFFIXES
from .lyrics import load_lyrics, save_lyrics, find_lyrics, fingerprint, cached_lyrics_path
from .extraction import ExtractionConfig, discover_config, config_status, build_command, parse_worker_event, default_data_dir
from .settings import atomic_json, read_profile, write_profile, load_m3u, save_m3u, starter_profile

APP_NAME = "Plague's Broadcast Station"
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
    def __init__(self, data_dir: Path, profile: Path | None = None):
        super().__init__()
        self.data_dir = Path(data_dir); self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = self.data_dir/'settings.json'
        try: self.saved = json.loads(self.settings_file.read_text())
        except (OSError, ValueError): self.saved = {}
        if not isinstance(self.saved,dict):self.saved={}
        self.setWindowTitle(APP_NAME + ' · Operator Desk'); self.resize(1370, 900)
        self.setMinimumSize(1030, 720)
        self.all_tracks: list[Track] = []; self.backgrounds: list[Path] = []
        self.lyric_files: dict[str, str] = {}; self.documents = {}; self.tasks = []
        self._lyric_stamps={}; self._invalid_lyrics=set()
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
        self.export.error.connect(self._message)
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
        self.preview = QLabel(); self.preview.setMinimumSize(480,270); self.preview.setMaximumHeight(340); self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter); self.preview.setAccessibleName('Program preview. The separate Program Output window is the clean broadcast picture.'); self.preview.setStyleSheet('background:#050b10;border:1px solid #536570;'); detail.addWidget(self.preview)
        self.now_title=QLabel('Your next great broadcast'); self.now_title.setObjectName('nowTitle'); self.now_title.setWordWrap(True); detail.addWidget(self.now_title)
        self.now_artist=QLabel('Choose a track from the playlist.'); self.now_artist.setObjectName('muted'); detail.addWidget(self.now_artist)
        self.seek=QSlider(Qt.Orientation.Horizontal); self.seek.setRange(0,10000); self.seek.setAccessibleName('Recording position'); self.seek.sliderPressed.connect(lambda:self._set_seek_drag(True)); self.seek.sliderReleased.connect(self._seek_released); detail.addWidget(self.seek)
        playback=QHBoxLayout()
        for text,slot in [('Previous',self.engine.previous),('Play',self.toggle_play),('Stop',self.engine.stop),('Next',self.engine.next)]:
            b=self._button(text,slot,text=='Play'); playback.addWidget(b)
            if text=='Play': self.play_button=b
        self.time_label=QLabel('0:00 / 0:00'); playback.addWidget(self.time_label); playback.addStretch(); detail.addLayout(playback)
        self.tabs=QTabWidget(); self.tabs.setAccessibleName('Operator settings'); detail.addWidget(self.tabs,1)
        self._appearance_tab(); self._lyrics_tab(); self._broadcast_tab(); self._models_tab()
        split.addWidget(right); split.setSizes([410,900]); layout.addWidget(split,1); self.setCentralWidget(root)

    def _appearance_tab(self):
        page=QWidget(); v=QVBoxLayout(page); form=QFormLayout()
        self.brand=QLineEdit('GOODNEIGHBOR BLUES'); self.tagline=QLineEdit('Original blues. Stay awhile.'); form.addRow('Station name',self.brand); form.addRow('Station line',self.tagline)
        self.interval=QSpinBox(); self.interval.setRange(5,300); self.interval.setValue(18); self.interval.setSuffix(' seconds'); form.addRow('Change backdrop every',self.interval)
        self.motion=QCheckBox('Gentle motion and crossfades'); self.motion.setChecked(True); self.wave=QCheckBox('Audio waveform'); self.wave.setChecked(True); self.lyrics_visible=QCheckBox('Show sing-along lyrics'); self.lyrics_visible.setChecked(True)
        form.addRow(self.motion); form.addRow(self.wave); form.addRow(self.lyrics_visible); v.addLayout(form)
        row=QHBoxLayout(); row.addWidget(self._button('Add artwork / videos',self.add_backgrounds)); row.addWidget(self._button('Previous backdrop',self.stage.previous_background)); row.addWidget(self._button('Next backdrop',self.stage.next_background)); v.addLayout(row)
        self.art_count=QLabel('No artwork loaded'); self.art_count.setObjectName('muted'); v.addWidget(self.art_count)
        for widget in [self.brand,self.tagline]: widget.textChanged.connect(self.apply_appearance)
        self.interval.valueChanged.connect(self.apply_appearance)
        for widget in [self.motion,self.wave,self.lyrics_visible]: widget.toggled.connect(self.apply_appearance)
        v.addStretch(); self.tabs.addTab(page,'Picture & sound')

    def _lyrics_tab(self):
        page=QWidget(); v=QVBoxLayout(page)
        self.lyric_status=QLabel('Select a recording to see its lyric status.'); self.lyric_status.setWordWrap(True); v.addWidget(self.lyric_status)
        row=QHBoxLayout(); row.addWidget(self._button('Import lyric file',self.import_lyrics)); self.extract_button=self._button('Extract & align',lambda:self.queue_extraction(False),True); row.addWidget(self.extract_button); self.batch_button=self._button('Prepare missing lyrics',self.queue_missing); row.addWidget(self.batch_button); v.addLayout(row)
        self.reference=QPlainTextEdit(); self.reference.setPlaceholderText('Have the exact words? Paste them here, then choose Align these lyrics. Otherwise use Extract & align.'); self.reference.setAccessibleName('Supplied lyric sheet for exact-word alignment'); self.reference.setMaximumHeight(110); v.addWidget(self.reference)
        row=QHBoxLayout(); self.align_button=self._button('Align these lyrics',lambda:self.queue_extraction(True)); row.addWidget(self.align_button); self.cancel_extract=self._button('Cancel extraction',self.cancel_extraction); self.cancel_extract.setEnabled(False); row.addWidget(self.cancel_extract); v.addLayout(row)
        self.extract_progress=QProgressBar(); self.extract_progress.setValue(0); self.extract_progress.setAccessibleName('Lyric extraction progress'); v.addWidget(self.extract_progress)
        self.lyric_text=QPlainTextEdit(); self.lyric_text.setReadOnly(True); self.lyric_text.setPlaceholderText('Prepared lyrics appear here.'); self.lyric_text.setAccessibleName('Prepared lyric transcript'); v.addWidget(self.lyric_text)
        row=QHBoxLayout(); self.offset=QDoubleSpinBox(); self.offset.setRange(-15,15); self.offset.setSingleStep(.1); self.offset.setDecimals(1); self.offset.setSuffix(' s'); self.offset.setAccessibleName('Move lyric highlights earlier or later'); self.offset.valueChanged.connect(self.stage.set_offset); row.addWidget(QLabel('Timing adjustment')); row.addWidget(self.offset); row.addWidget(self._button('Reset',lambda:self.offset.setValue(0))); v.addLayout(row)
        self.allow_drafts=QCheckBox('Preview flagged draft lyrics on the program output'); self.allow_drafts.setChecked(False); self.allow_drafts.toggled.connect(self._reload_current_lyrics); v.addWidget(self.allow_drafts)
        self.tabs.addTab(page,'Lyrics')

    def _broadcast_tab(self):
        page=QWidget(); v=QVBoxLayout(page); form=QFormLayout()
        self.output_mode=QComboBox(); self.output_mode.addItems(['Local recording','Direct livestream (RTMP / RTMPS)']); form.addRow('Output',self.output_mode)
        self.resolution=QComboBox(); self.resolution.addItems(['1280 × 720 · 30 fps','1920 × 1080 · 30 fps']); form.addRow('Picture size',self.resolution)
        self.bitrate=QSpinBox(); self.bitrate.setRange(1000,18000); self.bitrate.setValue(4500); self.bitrate.setSuffix(' kbps'); form.addRow('Video bitrate',self.bitrate)
        self.destination=QLineEdit(); self.destination.setPlaceholderText('Recording file, or rtmps://server/application'); self.destination.setAccessibleName('Recording path or streaming server'); form.addRow('Destination',self.destination)
        self.stream_key=QLineEdit(); self.stream_key.setEchoMode(QLineEdit.EchoMode.Password); self.stream_key.setPlaceholderText('Not saved in playlists or settings'); self.stream_key.setAccessibleName('Private stream key. This is not saved.'); form.addRow('Stream key',self.stream_key)
        v.addLayout(form); row=QHBoxLayout(); row.addWidget(self._button('Choose recording file',self.choose_recording)); self.start_output=self._button('Start output',self.start_broadcast,True); row.addWidget(self.start_output); self.stop_output=self._button('Stop output',self.stop_broadcast); self.stop_output.setEnabled(False); row.addWidget(self.stop_output); v.addLayout(row)
        self.broadcast_label=QLabel('Offline · Program Output is ready for OBS capture.'); self.broadcast_label.setWordWrap(True); v.addWidget(self.broadcast_label)
        self.broadcast_stats=QLabel(''); self.broadcast_stats.setWordWrap(True); v.addWidget(self.broadcast_stats)
        volume=QHBoxLayout(); volume.addWidget(QLabel('Monitor volume')); self.volume=QSlider(Qt.Orientation.Horizontal); self.volume.setRange(0,100); self.volume.setValue(70); self.volume.setAccessibleName('Local monitoring volume. Streaming audio level is unchanged.'); self.volume.valueChanged.connect(lambda n:self.engine.set_volume(n/100)); volume.addWidget(self.volume); v.addLayout(volume)
        tip=QLabel('Direct output includes only this program picture and its music—no microphone or other desktop sounds. For OBS, capture the Program Output window and this app’s audio. Prepare lyrics before starting an output.'); tip.setWordWrap(True); tip.setObjectName('muted'); v.addWidget(tip); v.addStretch(); self.tabs.addTab(page,'Broadcast')

    def _models_tab(self):
        page=QWidget(); v=QVBoxLayout(page); form=QFormLayout(); self.model_fields={}
        for key,label in [('python_path','Lyric runtime Python'),('model_root','Model folder'),('torch_home','Torch model cache'),('gpu_python_path','GPU runtime Python'),('vulkan_binary','Whisper Vulkan executable'),('vulkan_model','Whisper Vulkan model'),('gpu_lock','Shared GPU lock (optional)')]:
            entry=QLineEdit(str(getattr(self.extract_config,key))); entry.setAccessibleName(label); self.model_fields[key]=entry; form.addRow(label,entry)
        v.addLayout(form); self.gpu=QCheckBox('Use GPU when available; wait while another job uses it'); self.gpu.setChecked(self.extract_config.use_gpu); v.addWidget(self.gpu)
        v.addWidget(self._button('Save model settings',self.save_model_settings)); self.model_status=QLabel(''); self.model_status.setWordWrap(True); v.addWidget(self.model_status)
        note=QLabel('Playback does not require AI models. Extraction uses a separate local Python runtime so heavy work stays outside the player. Existing models on this Linux machine are detected automatically.'); note.setWordWrap(True); note.setObjectName('muted'); v.addWidget(note); v.addStretch(); scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(page); self.tabs.addTab(scroll,'Lyric models'); self.update_model_status()

    def _connect(self):
        self.engine.position_changed.connect(self._position)
        self.engine.duration_changed.connect(lambda _:self._position(self.engine.position))
        self.engine.playing_changed.connect(self._playing)
        self.engine.track_changed.connect(self._track_changed)
        self.engine.waveform_ready.connect(self.stage.set_waveform)
        self.engine.pcm_ready.connect(self.export.push_audio)
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
        self.engine.seek(self.seek.value()/10000*max(0,self.engine.duration)); self._seek_drag=False; self.export.clear_audio()
    def _position(self, position):
        self.stage.set_playback(position,self.engine.duration,self.engine.playing)
        self.time_label.setText(clock(position)+' / '+clock(self.engine.duration))
        if not self._seek_drag:self.seek.setValue(round(position/max(1,self.engine.duration)*10000))
    def _playing(self, playing):
        self.play_button.setText('Pause' if playing else 'Play'); self.stage.set_playback(self.engine.position,self.engine.duration,playing)
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
        self._track_generation+=1; generation=self._track_generation; self.export.clear_audio()
        if not track:
            self.stage.set_track(''); self.stage.set_lyrics(None); return
        self.stage.set_track(track.title,track.artist,track.album); self.stage.set_lyrics(None)
        self.documents.pop(str(track.path),None)
        self.now_title.setText(track.title); self.now_artist.setText(' · '.join(x for x in [track.artist,track.album] if x))
        for i in range(self.playlist.count()):
            item=self.playlist.item(i)
            if Path(item.data(Qt.ItemDataRole.UserRole))==track.path:self.playlist.setCurrentItem(item); break
        def loaded(document):
            if generation!=self._track_generation:return
            self._remember_document(track,document)
            self._display_document(track,document)
        task=self._task(lambda:self._find_document(track),loaded)
        task.failed.connect(lambda _:self._remember_document(track,None))
    def _remember_document(self,track,document):
        key=str(track.path)
        if document:
            self.documents[key]=document; self._invalid_lyrics.discard(key)
            try:self._lyric_stamps[key]=(track.path.stat().st_size,track.path.stat().st_mtime_ns)
            except OSError:self._invalid_lyrics.add(key)
        else:self.documents.pop(key,None);self._lyric_stamps.pop(key,None);self._invalid_lyrics.add(key)
    def _find_document(self,track):
        explicit=self.lyric_files.get(str(track.path))
        if explicit and Path(explicit).is_file():
            document=load_lyrics(explicit)
            if document.source_sha256 and document.source_sha256!=fingerprint(track.path):raise ValueError('The recording changed; its previous lyrics were kept off-air.')
            return document
        path=find_lyrics(track.path,Path(self.extract_config.cache_dir))
        return load_lyrics(path) if path else None
    def _display_document(self,track,document):
        if document:
            flagged=document.quality.get('status')=='review'; text='Draft · check the flagged passages before airing.' if flagged else 'Lyrics ready · synchronized to this recording.'
            self.lyric_status.setText(text+(' '+ ' '.join(document.warnings[:2]) if document.warnings else ''))
            self.lyric_text.setPlainText('\n'.join(c.text for c in document.segments))
            self.stage.set_lyrics(document if not flagged or self.allow_drafts.isChecked() else None)
        else:self.lyric_status.setText('No prepared lyrics for this recording yet.'); self.lyric_text.clear(); self.stage.set_lyrics(None)
    def _reload_current_lyrics(self):
        t=self.engine.current_track
        if not t:return
        generation=self._track_generation; self.documents.pop(str(t.path),None);self.stage.set_lyrics(None)
        def loaded(document):
            if generation!=self._track_generation:return
            self._remember_document(t,document);self._display_document(t,document)
        task=self._task(lambda:self._find_document(t),loaded);task.failed.connect(lambda _:self._remember_document(t,None))
    def _selection_changed(self,current,previous):
        if current and not self.engine.playing:
            t=next((t for t in self.all_tracks if str(t.path)==current.data(Qt.ItemDataRole.UserRole)),None)
            if t:self.lyric_status.setText(t.title+' · '+('Lyrics prepared' if self._has_lyrics(t) else 'Lyrics not prepared'))

    def _has_lyrics(self,track):
        key=str(track.path)
        if key in self._invalid_lyrics:return False
        try:stamp=(track.path.stat().st_size,track.path.stat().st_mtime_ns)
        except OSError:return False
        if key in self._lyric_stamps and self._lyric_stamps[key]!=stamp:
            self.documents.pop(key,None);self._invalid_lyrics.add(key);return False
        document=self.documents.get(key)
        if document is None and self.lyric_files.get(key):
            try: document=load_lyrics(self.lyric_files[key]); self.documents[key]=document;self._lyric_stamps[key]=stamp
            except (OSError,ValueError,TypeError): return False
        return bool(document and document.segments and document.quality.get('status')!='review')
    def _apply_queue(self):
        selected=[t for t in self.all_tracks if not self.ready_only.isChecked() or self._has_lyrics(t)]
        self.engine.set_queue(selected); self._rebuild_list(selected); self._schedule_save()
    def _rebuild_list(self,tracks):
        old=self.engine.current_track.path if self.engine.current_track else None
        self.playlist.blockSignals(True); self.playlist.clear()
        for t in tracks:
            status='LYRICS READY' if self._has_lyrics(t) else 'MUSIC ONLY'; item=QListWidgetItem(f'{t.title}\n{t.artist or "Goodneighbor Blues"}  ·  {clock(t.duration)}  ·  {status}')
            item.setData(Qt.ItemDataRole.UserRole,str(t.path)); item.setToolTip(str(t.path)); self.playlist.addItem(item)
            if old==t.path:self.playlist.setCurrentItem(item)
        if not old and self.playlist.count():self.playlist.setCurrentRow(0)
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
    def visual_settings(self):return {'brand':self.brand.text(),'tagline':self.tagline.text(),'interval_seconds':self.interval.value(),'motion':self.motion.isChecked(),'waveform':self.wave.isChecked(),'lyrics':self.lyrics_visible.isChecked(),'lyrics_ready_only':self.ready_only.isChecked()}
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
        name,_=QFileDialog.getOpenFileName(self,'Import timed lyrics','','Timed lyrics (*.json *.lrc *.srt *.vtt)')
        if not name:return
        def work():
            document=load_lyrics(name); identity=fingerprint(track.path)
            if document.source_sha256 and document.source_sha256!=identity:raise ValueError('This lyric file belongs to a different recording. Import the matching recording, or align the exact words to this one.')
            document.source_sha256=identity; document.title=track.title
            target=cached_lyrics_path(track.path,Path(self.extract_config.cache_dir)); save_lyrics(document,target); return target,document
        self._task(work,lambda result:self._lyrics_ready(track,*result))
    def _lyrics_ready(self,track,target,document):
        self.lyric_files[str(track.path)]=str(target);self._remember_document(track,document)
        if self.engine.current_track and self.engine.current_track.path==track.path:self._display_document(track,document)
        self._apply_queue(); self._schedule_save()
    def queue_extraction(self,reference=False):
        track=self._selected_track()
        if not track:self._message('Choose a recording first.'); return
        text=self.reference.toPlainText().strip() if reference else ''
        if reference and not text:self._message('Paste the exact lyric sheet before aligning it.'); return
        self._queue_extract([(track,text)])
    def queue_missing(self):self._queue_extract([(t,'') for t in self.all_tracks if not self.lyric_files.get(str(t.path)) and str(t.path) not in self.documents])
    def _queue_extract(self,jobs):
        if self.export.running:self._message('Stop the output before preparing lyrics. Playback can continue.'); return
        if not self.save_model_settings():return
        for track,text in jobs:
            if all(t.path!=track.path for t,_ in self._extract_queue):self._extract_queue.append((track,text))
        self._next_extraction()
    def _next_extraction(self):
        if self.extract_process.state()!=QProcess.ProcessState.NotRunning or not self._extract_queue:return
        track,text=self._extract_queue.pop(0); self._extract_current=track; self._process_bytes=b''; self._extract_log=''
        try:
            target=cached_lyrics_path(track.path,Path(self.extract_config.cache_dir)); args=build_command(track.path,target,self.extract_config,text)
            self.extract_progress.setValue(0); self.lyric_status.setText('Preparing lyrics · '+track.title); self.cancel_extract.setEnabled(True)
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
        except Exception as error:self._message(str(error)); self._extract_current=None
    def _extract_output(self):
        self._process_bytes+=bytes(self.extract_process.readAllStandardOutput())
        while b'\n' in self._process_bytes:
            line,self._process_bytes=self._process_bytes.split(b'\n',1); event=parse_worker_event(line)
            if not event:continue
            if event['type']=='progress':self.extract_progress.setValue(round(float(event.get('fraction',0))*100)); self.lyric_status.setText(str(event.get('message','Working…')))
            elif event['type']=='done':
                try:
                    target=Path(event['output']); document=load_lyrics(target); self._lyrics_ready(self._extract_current,target,document); self.extract_progress.setValue(100)
                except Exception as error:self._message(str(error))
            elif event['type']=='error':self._message(str(event.get('message','Lyric extraction failed.')))
    def _extract_stderr(self):self._extract_log=(self._extract_log+bytes(self.extract_process.readAllStandardError()).decode(errors='replace'))[-4000:]
    def _process_error(self,error):
        self._message(self.extract_process.errorString())
        if error==QProcess.ProcessError.FailedToStart:
            self._extract_queue.clear(); self._extract_current=None; self.cancel_extract.setEnabled(False); self.lyric_status.setText('The lyric runtime could not start. Check its Python path in Lyric models.')
    def _extract_finished(self,code,status):
        self._extract_output(); self.cancel_extract.setEnabled(False)
        if code and not self._closing:self.lyric_status.setText('Extraction stopped or failed. Existing lyrics were preserved. '+self._extract_log[-250:])
        self._extract_current=None
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
    def update_model_status(self):
        status=config_status(self.extract_config); self.model_status.setText('Local lyric models detected.' if status['ready'] else 'Playback is ready. Lyric extraction needs a configured runtime and model files. '+ '; '.join(status['missing_models'][:2]))

    def choose_recording(self):
        name,_=QFileDialog.getSaveFileName(self,'Save local broadcast recording',str(Path.home()/'broadcast.mp4'),'MP4 recording (*.mp4);;Matroska recording (*.mkv)')
        if name:self.destination.setText(name); self.output_mode.setCurrentIndex(0)
    def start_broadcast(self):
        if self.extract_process.state()!=QProcess.ProcessState.NotRunning:self._message('Finish or cancel lyric extraction before starting output.'); return
        target=self.destination.text().strip()
        if not target:self.choose_recording(); target=self.destination.text().strip()
        if not target:return
        from .broadcast import BroadcastConfig
        mode='stream' if self.output_mode.currentIndex()==1 else 'record'
        if mode=='stream' and self.stream_key.text().strip():target=target.rstrip('/')+'/'+self.stream_key.text().strip().lstrip('/')
        self._export_size=(1920,1080) if self.resolution.currentIndex()==1 else (1280,720)
        try:
            config=BroadcastConfig(mode=mode,target=target,width=self._export_size[0],height=self._export_size[1],fps=30,bitrate_kbps=self.bitrate.value())
            self.export.set_video_frame(self.stage.render_frame(*self._export_size)); self.export.start(config)
            self.preview_timer.setInterval(33); self.start_output.setEnabled(False); self.stop_output.setEnabled(True)
            for b in [self.extract_button,self.batch_button,self.align_button]:b.setEnabled(False)
            if not self.engine.playing:self.toggle_play()
        except Exception as error:self._message(str(error)); self._export_finished()
    def stop_broadcast(self):self.export.stop(); self.broadcast_label.setText('Finishing output…')
    def _export_finished(self):
        self.preview_timer.setInterval(50); self.start_output.setEnabled(True); self.stop_output.setEnabled(False)
        for b in [self.extract_button,self.batch_button,self.align_button]:b.setEnabled(True)
        if self._closing:QTimer.singleShot(0,self.close)
    def _broadcast_status(self,message):self.broadcast_label.setText(message)
    def _broadcast_statistics(self,data):
        self.broadcast_stats.setText(' · '.join(f'{key.replace("_"," ")}: {value}' for key,value in data.items() if key not in {'target','url','command'}))
    def _schedule_save(self):
        if hasattr(self,'save_timer'):self.save_timer.start(600)
    def save_session(self):
        try:
            write_profile(self.data_dir/'session.json',self.all_tracks,self.backgrounds,self.lyric_files,self.visual_settings(),self.profile_name)
            atomic_json(self.settings_file,{'extraction':self.extract_config.to_dict()})
        except Exception as error:self._message('Could not save session: '+str(error))
    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_F11:self.show_output(); self.stage.showFullScreen(); event.accept()
        elif event.key()==Qt.Key.Key_Space and not isinstance(QApplication.focusWidget(),(QLineEdit,QPlainTextEdit)):
            self.toggle_play(); event.accept()
        else:super().keyPressEvent(event)
    def closeEvent(self,event):
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
    args=parser.parse_args(argv)
    os.environ.setdefault('QT_MEDIA_BACKEND','ffmpeg')
    if getattr(sys,'frozen',False):
        bundled=Path(sys._MEIPASS)/'bin'; os.environ['PATH']=str(bundled)+os.pathsep+os.environ.get('PATH','')
        roots=[Path(sys.executable).parent,Path(sys.executable).parent.parent.parent]
    else:roots=[Path(__file__).resolve().parents[2]]
    if args.profile is None:args.profile=starter_profile(args.data_dir,roots)
    app=QApplication.instance() or QApplication(sys.argv[:1]); app.setApplicationName(APP_NAME); app.setOrganizationName('Plague Broadcast'); app.setStyleSheet(STYLE)
    window=MainWindow(args.data_dir,args.profile); window.show()
    if args.show_output:window.show_output()
    if args.autoplay:QTimer.singleShot(300,window.toggle_play)
    return app.exec()
