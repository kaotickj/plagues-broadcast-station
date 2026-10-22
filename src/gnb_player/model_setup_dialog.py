"""Guided, explicit setup of local lyric models and an optional Ollama service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import QProcess, QProcessEnvironment, QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QTabWidget, QVBoxLayout, QWidget,
)

from .extraction import ExtractionConfig
from .model_discovery import discover, discover_ollama, normalize_ollama_url
from .model_install import create_plan, describe_plan, installer_command, save_plan

COMPONENTS = {
    'transcription_model_path': ('transcription', 'Whisper large-v3', 'Transcribes sung words'),
    'review_model_path': ('review', 'Whisper medium.en', 'Independent second-model review'),
    'separator_model_path': ('separator', 'HTDemucs', 'Separates the vocal from the band'),
    'alignment_model_path': ('alignment', 'wav2vec2 CTC', 'Aligns words to the recording'),
}


def size_label(number):
    return f'{number / 1024**3:.1f} GiB'


class DiscoveryTask(QThread):
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self):
        try:
            self.found.emit(self.fn(self.isInterruptionRequested))
        except Exception as error:
            self.failed.emit(str(error))


class ModelSetupDialog(QDialog):
    """Local settings are returned only after a successful explicit action."""
    configured = Signal(object)

    def __init__(self, config: ExtractionConfig, data_dir: Path, parent=None):
        super().__init__(parent)
        self.config = ExtractionConfig.from_dict(config.to_dict())
        self.data_dir = Path(data_dir)
        self.extra_roots = []
        self.python_paths = []
        self.tasks = []
        self.result_config = None
        self.discovery = None
        self._busy = False
        self._cancelled = False
        self._events = None
        self._event_offset = 0
        self._event_buffer = b''
        self._result_file = None
        self.setWindowTitle('Set up lyric models · Plague’s Broadcast Station')
        self.resize(1000, 780)
        self.setMinimumSize(790, 620)
        self.process = QProcess(self)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.process.readyReadStandardOutput.connect(lambda: self.process.readAllStandardOutput())
        self.process.readyReadStandardError.connect(lambda: self.process.readAllStandardError())
        self.poll = QTimer(self)
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._read_events)
        self._build()

    def _button(self, title, slot):
        button = QPushButton(title)
        button.setAccessibleName(title)
        button.clicked.connect(slot)
        return button

    def _build(self):
        layout = QVBoxLayout(self)
        heading = QLabel('GOOD WORDS. RIGHT ON TIME.')
        heading.setObjectName('masthead')
        layout.addWidget(heading)
        intro = QLabel('Find compatible models already on this computer, or install the quality lyric bundle. '
                       'The preferred recognizer is Whisper large-v3, with vocal separation, independent review, and word alignment.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName('Model setup steps')
        layout.addWidget(self.tabs, 1)

        found = QWidget()
        body = QVBoxLayout(found)
        buttons = QHBoxLayout()
        self.scan = self._button('Find existing models', self.start_scan)
        self.add_folder = self._button('Add model folder…', self.choose_folder)
        self.add_python = self._button('Choose Python…', self.choose_python)
        for button in (self.scan, self.add_folder, self.add_python):
            buttons.addWidget(button)
        body.addLayout(buttons)
        note = QLabel('Scans common model caches and folders you select. Existing models stay where they are.')
        note.setWordWrap(True)
        body.addWidget(note)
        form = QFormLayout()
        self.models = {}
        for key, (_, title, purpose) in COMPONENTS.items():
            combo = QComboBox()
            combo.setAccessibleName(title + ' model location')
            combo.setToolTip(purpose)
            combo.addItem('Download if missing', '')
            combo.currentIndexChanged.connect(self.refresh_plan)
            label = QLabel(title + '\n' + purpose)
            label.setBuddy(combo)
            form.addRow(label, combo)
            self.models[key] = combo
        self.runtime = QComboBox()
        self.runtime.setAccessibleName('Python runtime for lyric models')
        self.runtime.addItem('Download compatible Python for this app' if os.name == 'nt' else 'Choose an installed Python 3.11–3.13', None)
        self.runtime.currentIndexChanged.connect(self.refresh_plan)
        form.addRow('Model Python', self.runtime)
        body.addLayout(form)
        self.findings = QPlainTextEdit()
        self.findings.setReadOnly(True)
        self.findings.setAccessibleName('Discovery results and compatibility explanations')
        self.findings.setPlaceholderText('Choose Find existing models to inspect common model folders and Python runtimes.')
        body.addWidget(self.findings, 1)
        self.tabs.addTab(found, '1 · Find models')

        install = QWidget()
        box = QVBoxLayout(install)
        row = QHBoxLayout()
        self.destination = QLineEdit(str((self.data_dir / 'model-setup').resolve()))
        self.destination.setAccessibleName('User-local model installation folder')
        self.destination.textChanged.connect(self.refresh_plan)
        row.addWidget(self.destination, 1)
        self.browse_destination = self._button('Choose install folder…', self.choose_destination)
        row.addWidget(self.browse_destination)
        box.addWidget(QLabel('Install missing components in this folder:'))
        box.addLayout(row)
        self.plan_text = QPlainTextEdit()
        self.plan_text.setReadOnly(True)
        self.plan_text.setAccessibleName('Model installation plan, downloads and disk requirements')
        box.addWidget(self.plan_text, 1)
        self.install = self._button('Install missing components', self.start_install)
        self.install.setObjectName('primary')
        self.reuse = self._button('Validate and use existing models', self.start_reuse)
        row = QHBoxLayout()
        row.addWidget(self.install)
        row.addWidget(self.reuse)
        box.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setAccessibleName('Model setup progress')
        box.addWidget(self.progress)
        self.status = QLabel('Ready to find models. Downloads start only when you choose Install.')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setAccessibleName('Model setup progress details')
        box.addWidget(self.log, 1)
        self.tabs.addTab(install, '2 · Review & install')

        ollama = QWidget()
        box = QVBoxLayout(ollama)
        note = QLabel('Ollama runs optional text assistants. Whisper and the alignment models handle song audio. '
                      'Setup connects to your existing local Ollama service and lists the models it already has.')
        note.setWordWrap(True)
        box.addWidget(note)
        form = QFormLayout()
        self.ollama_url = QLineEdit(getattr(self.config, 'ollama_url', 'http://127.0.0.1:11434'))
        self.ollama_url.setAccessibleName('Local Ollama service address')
        self.ollama_model = QComboBox()
        self.ollama_model.setAccessibleName('Installed Ollama text model')
        self.ollama_model.addItem('No text model selected', '')
        saved_model = getattr(self.config, 'ollama_model', '')
        if saved_model:
            self.ollama_model.addItem(saved_model, saved_model)
            self.ollama_model.setCurrentIndex(1)
        form.addRow('Local Ollama address', self.ollama_url)
        form.addRow('Installed text model', self.ollama_model)
        box.addLayout(form)
        self.check_ollama = self._button('Find Ollama models', self.scan_ollama)
        self.save_ollama = self._button('Save Ollama connection', self.apply_ollama)
        box.addWidget(self.check_ollama)
        box.addWidget(self.save_ollama)
        self.ollama_status = QLabel('Optional. Leave this step empty for audio-only lyric extraction.')
        self.ollama_status.setWordWrap(True)
        self.ollama_status.setTextFormat(Qt.TextFormat.PlainText)
        box.addWidget(self.ollama_status)
        box.addStretch()
        self.tabs.addTab(ollama, '3 · Ollama (optional)')

        row = QHBoxLayout()
        self.cancel_job = self._button('Cancel current setup', self.cancel_work)
        self.cancel_job.setEnabled(False)
        row.addWidget(self.cancel_job)
        row.addStretch()
        self.done = self._button('Close', self.close)
        row.addWidget(self.done)
        layout.addLayout(row)
        self.refresh_plan()

    def _task(self, fn, callback):
        task = DiscoveryTask(fn, self)
        self.tasks.append(task)
        task.found.connect(lambda result: callback(result) if not task.isInterruptionRequested() else None)
        task.failed.connect(self._failed)
        task.finished.connect(lambda: self._task_done(task))
        self.scan.setEnabled(False)
        self.cancel_job.setEnabled(True)
        task.start()

    def _task_done(self, task):
        if task in self.tasks:
            self.tasks.remove(task)
        task.deleteLater()
        self.scan.setEnabled(not self._busy)
        self.check_ollama.setEnabled(not self._busy)
        self.cancel_job.setEnabled(self._busy or bool(self.tasks))

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose the model/cache folder itself')
        if folder:
            self.extra_roots.append(folder)
            self.start_scan()

    def choose_python(self):
        filename, _ = QFileDialog.getOpenFileName(self, 'Choose model Python executable', '', 'Python executable (*)')
        if filename:
            self.python_paths.append(filename)
            self.start_scan()

    def choose_destination(self):
        folder = QFileDialog.getExistingDirectory(self, 'Choose user-writable installation folder')
        if folder:
            self.destination.setText(str(Path(folder) / 'PlaguesBroadcastModels'))

    def start_scan(self):
        if self._busy or self.tasks:
            return
        self.findings.setPlainText('Checking model formats, Python packages and the local Ollama service…')
        settings = self.config.to_dict()
        folders, pythons = tuple(self.extra_roots), tuple(self.python_paths)
        url = self.ollama_url.text().strip()
        self._task(lambda cancelled: discover(folders, settings=settings,
                    app_data=self.data_dir, python_paths=pythons,
                    ollama_url=url, cancelled=cancelled), self._found)

    def _found(self, result):
        if result.cancelled:
            self.findings.setPlainText('Discovery cancelled; existing selections and settings were kept.')
            return
        self.discovery = result
        descriptions = []
        for key, (kind, title, _) in COMPONENTS.items():
            combo = self.models[key]
            previous = combo.currentData() or getattr(self.config, key, '')
            combo.blockSignals(True)
            combo.clear()
            combo.addItem('Download if missing', '')
            for model in result.models:
                if model.kind == kind and model.compatible:
                    combo.addItem(model.path, model.path)
            index = combo.findData(previous) if previous else -1
            combo.setCurrentIndex(index if index >= 0 else (1 if combo.count() > 1 else 0))
            combo.blockSignals(False)
            descriptions.append(f'{title}: ' + ('found compatible model' if combo.currentData() else 'needs installation'))
        previous_runtime = (self.runtime.currentData() or {}).get('path') or self.config.python_path
        self.runtime.blockSignals(True)
        self.runtime.clear()
        self.runtime.addItem('Download compatible Python for this app' if os.name == 'nt' else 'Choose an installed Python 3.11–3.13', None)
        selected = first_compatible = first_supported = 0
        for runtime in result.runtimes:
            if runtime.version.startswith(('3.11.', '3.12.', '3.13.')):
                self.runtime.addItem(f'Python {runtime.version} · ' + ('reuse model environment' if runtime.compatible else 'create isolated environment') + ' · ' + runtime.path, runtime.to_dict())
                index = self.runtime.count() - 1
                if not first_supported:
                    first_supported = index
                if runtime.compatible and not first_compatible:
                    first_compatible = index
                if runtime.path == previous_runtime:
                    selected = index
            descriptions.append(f'Python {runtime.version or "unavailable"}: {runtime.reason} ({runtime.path})')
        self.runtime.setCurrentIndex(selected or first_compatible or first_supported)
        self.runtime.blockSignals(False)
        descriptions += [f'{model.path}: {model.reason}' for model in result.models if not model.compatible]
        descriptions += result.warnings
        if result.cancelled:
            descriptions.append('Discovery cancelled; existing settings were kept.')
        if result.truncated:
            descriptions.append('Scan limit reached. Select a more specific model folder for the remaining models.')
        self.findings.setPlainText('\n'.join(descriptions))
        self._ollama_found(result.ollama)
        self.refresh_plan()

    def _ollama_found(self, info):
        selected = self.ollama_model.currentData() or getattr(self.config, 'ollama_model', '')
        self.ollama_model.clear()
        self.ollama_model.addItem('No text model selected', '')
        for model in info.models:
            details = model.get('details', {})
            label = model['name'] + (f' · {details["parameter_size"]}' if details.get('parameter_size') else '')
            self.ollama_model.addItem(label, model['name'])
        if selected and not info.reachable and self.ollama_model.findData(selected)<0:
            self.ollama_model.addItem(selected+' · saved connection',selected)
        index = self.ollama_model.findData(selected)
        self.ollama_model.setCurrentIndex(index if index >= 0 else 0)
        self.ollama_status.setText(info.reason + ('\nExecutable: ' + info.executable if info.executable else ''))

    def scan_ollama(self):
        if self._busy or self.tasks:
            return
        self.check_ollama.setEnabled(False)
        url = self.ollama_url.text().strip()
        self._task(lambda cancelled: discover_ollama(url), self._ollama_found)

    def _ollama_values(self):
        return {'ollama_url': normalize_ollama_url(self.ollama_url.text().strip()),
                'ollama_model': self.ollama_model.currentData() or ''}

    def apply_ollama(self):
        try:
            config = ExtractionConfig.from_dict({**self.config.to_dict(), **self._ollama_values()})
            self._apply(config)
            self.ollama_status.setText('Local Ollama connection saved. Audio recognition still uses Whisper large-v3.')
        except ValueError as error:
            self._failed(str(error))

    def _plan(self):
        reuse = {key: combo.currentData() for key, combo in self.models.items() if combo.currentData()}
        runtime = self.runtime.currentData() or {}
        if runtime.get('compatible'):
            return create_plan(self.destination.text().strip(), runtime_python=runtime['path'], reuse=reuse)
        if runtime.get('path'):
            return create_plan(self.destination.text().strip(), base_python=runtime['path'], reuse=reuse)
        if os.name == 'nt':
            return create_plan(self.destination.text().strip(), bootstrap_python=True, reuse=reuse)
        raise ValueError('Choose an installed Python 3.11–3.13 executable. Setup creates its own model environment.')

    def refresh_plan(self):
        if not hasattr(self, 'plan_text'):
            return
        try:
            plan = self._plan()
            summary = describe_plan(plan)
            lines = [summary['profile'], '', 'Python: ' + summary['runtime_action']]
            for step in summary['steps']:
                lines.append(f'{step["label"]}: {step["action"]}' + (' · ' + step['path'] if step['path'] else ''))
            lines += ['', 'Estimated download: ' + size_label(summary['estimated_download_bytes']),
                      'Free disk space needed: ' + size_label(summary['required_free_bytes']),
                      'Memory: 16 GiB recommended; CPU int8 processing keeps the large recognizer.',
                      '', summary['note']]
            if not summary['enough_space']:
                lines.append('This drive needs more free space. Choose another installation folder.')
            self.plan_text.setPlainText('\n'.join(lines))
            self.install.setEnabled(not self._busy and summary['enough_space'])
            self.reuse.setEnabled(not self._busy and bool(plan.runtime_python) and len(plan.reuse) == len(COMPONENTS))
        except (ValueError, OSError) as error:
            self.plan_text.setPlainText(str(error))
            self.install.setEnabled(False)
            self.reuse.setEnabled(False)

    def start_install(self):
        self._start(True)

    def start_reuse(self):
        self._start(False)

    def _start(self, downloads):
        if self._busy or self.tasks:
            return
        try:
            plan = self._plan()
            summary = describe_plan(plan)
            self._ollama_values()
            if not downloads and (not plan.runtime_python or len(plan.reuse) != len(COMPONENTS)):
                raise ValueError('Find a complete compatible model environment to reuse without downloads.')
            if not summary['enough_space']:
                raise ValueError('There is not enough free space for this installation plan.')
            session = self.data_dir / 'model-setup-jobs' / uuid.uuid4().hex
            session.mkdir(parents=True, mode=0o700)
            plan_file = save_plan(plan, session / 'plan.json')
            self._events = session / 'events.jsonl'
            self._result_file = session / 'result.json'
            args = installer_command(plan_file, self._events, self._result_file)
            if downloads:
                args.append('--allow-downloads')
            else:
                args.append('--reuse-only')
            environment = QProcessEnvironment.systemEnvironment()
            environment.insert('PYTHONUTF8', '1')
            environment.insert('PYTHONIOENCODING', 'utf-8')
            self.process.setProcessEnvironment(environment)
            self._event_offset = 0
            self._event_buffer = b''
            self._cancelled = False
            self._busy = True
            self._controls(False)
            self.progress.setValue(0)
            self.log.clear()
            self.status.setText('Starting quality model setup…')
            self.tabs.setCurrentIndex(1)
            self.poll.start()
            self.process.start(args[0], args[1:])
        except (ValueError, OSError) as error:
            self._failed(str(error))

    def _controls(self, enabled):
        for widget in (self.scan, self.add_folder, self.add_python, self.runtime, self.destination,
                       self.browse_destination, self.check_ollama, self.save_ollama, self.ollama_url, self.ollama_model,
                       *self.models.values()):
            widget.setEnabled(enabled)
        self.cancel_job.setEnabled(not enabled)
        self.refresh_plan()

    def _read_events(self):
        if not self._events or not self._events.is_file():
            return
        try:
            with self._events.open('rb') as stream:
                stream.seek(self._event_offset)
                chunk = stream.read(256 * 1024)
                self._event_offset += len(chunk)
            self._event_buffer += chunk
            while b'\n' in self._event_buffer:
                line, self._event_buffer = self._event_buffer.split(b'\n', 1)
                try:
                    event = json.loads(line)
                    message = str(event.get('message', ''))[:2000]
                    if message:
                        self.log.appendPlainText(message)
                        self.status.setText(message)
                    value = event.get('fraction')
                    if isinstance(value, (int, float)):
                        self.progress.setValue(max(0, min(100, round(value * 100))))
                except (ValueError, TypeError, AttributeError):
                    continue
            if len(self._event_buffer) > 256 * 1024:
                self._event_buffer = b''
        except OSError:
            pass

    def _finished(self, code, status):
        self._read_events()
        self.poll.stop()
        self._busy = False
        self._controls(True)
        if self._cancelled:
            self.status.setText('Setup cancelled. Previous model settings were kept; partial downloads can be reused on retry.')
            return
        try:
            if code or not self._result_file or not self._result_file.is_file():
                raise ValueError('Setup did not finish. Check the progress details; previous settings were kept.')
            result = json.loads(self._result_file.read_text(encoding='utf-8'))
            if not isinstance(result, dict):
                raise ValueError('Setup returned invalid settings; previous settings were kept.')
            if result.get('type') == 'error' or result.get('error'):
                raise ValueError(str(result.get('message') or result.get('error') or 'Model setup failed.'))
            values = result.get('settings', result.get('config', result))
            if not isinstance(values, dict) or any(not isinstance(values.get(key), str) or not values[key] for key in ('python_path', *COMPONENTS)):
                raise ValueError('Setup returned incomplete model locations; previous settings were kept.')
            config = ExtractionConfig.from_dict({**self.config.to_dict(), **values, **self._ollama_values()})
            config.cache_dir = str(self.data_dir / 'lyrics-cache')
            self._apply(config)
            self.progress.setValue(100)
            self.status.setText('Quality lyric models are ready. Their locations have been saved in the player.')
        except (ValueError, OSError, TypeError) as error:
            self._failed(str(error))

    def _apply(self, config):
        self.config = config
        self.result_config = config
        self.configured.emit(config)

    def _process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.poll.stop()
            self._busy = False
            self._controls(True)
            self._failed('The setup worker could not start: ' + self.process.errorString())

    def _failed(self, message):
        self.status.setText(str(message))
        self.log.appendPlainText(str(message))

    def cancel_work(self):
        for task in self.tasks:
            task.requestInterruption()
        if self.process.state() != QProcess.ProcessState.NotRunning:
            self._cancelled = True
            if os.name == 'nt':
                import subprocess
                taskkill = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/taskkill.exe')
                try:
                    subprocess.run([taskkill, '/PID', str(self.process.processId()), '/T', '/F'], capture_output=True, timeout=8)
                except (OSError, subprocess.TimeoutExpired):
                    self.process.kill()
            else:
                self.process.terminate()
                pid=int(self.process.processId())
                QTimer.singleShot(3500, lambda: self._kill_owned_worker(pid))
        self.status.setText('Cancelling the current operation…')

    def _kill_owned_worker(self,pid):
        if self.process.state() != QProcess.ProcessState.NotRunning and int(self.process.processId())==pid:
            if os.name != 'nt':
                import signal
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.process.kill()

    def closeEvent(self, event):
        if self._busy or self.tasks:
            self.cancel_work()
            event.ignore()
            QTimer.singleShot(500, self.close)
            return
        event.accept()

    def reject(self):
        # Escape must use the same cancellation path as the window's Close button.
        self.close()
