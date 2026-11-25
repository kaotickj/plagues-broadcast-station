"""Sequential private playlist preparation using the existing recording review worker."""
from pathlib import Path
import uuid
from PySide6.QtCore import QTimer, QProcess, Qt, QUrl
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QListWidget,
                               QPushButton, QProgressBar, QCheckBox)
from .batch_review import batch_plan
from .review_dialog import LyricsReviewDialog
from .lyric_review import is_approved
from .settings import atomic_json


class BatchReviewDialog(QDialog):
    def __init__(self, window, tracks, *, missing_only=False):
        super().__init__(window)
        self.window=window
        self.tracks=list({str(t.path):t for t in tracks}.values())
        self.missing_only=missing_only
        self.rows=[{'path':str(t.path),'title':t.title,'status':'pending','detail':''} for t in self.tracks]
        self.index=0
        self.current=None
        self.started=False
        self.paused=False
        self.cancelled=False
        self.finished=False
        self.report=Path(window.data_dir)/'lyrics-review'/'batch-reports'/(uuid.uuid4().hex+'.json')
        self.setWindowTitle('Playlist lyric preparation / audit')
        self.resize(900,620)
        layout=QVBoxLayout(self)
        info=QLabel('Private preparation only. Previous vocal approvals are not accuracy evidence. Existing published files are retained; new candidates need fresh listening and approval. Accuracy target: at least 95% against independently verified words, with no detected hallucinations. This batch does not measure accuracy.')
        info.setWordWrap(True);layout.addWidget(info)
        self.long_ctc=QCheckBox('Allow CPU CTC alignment for embedded lyrics longer than 3 minutes (no transcription)')
        layout.addWidget(self.long_ctc)
        self.progress=QProgressBar();self.progress.setRange(0,max(1,len(self.tracks)));layout.addWidget(self.progress)
        self.status=QLabel('Ready. One recording at a time.');self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.status)
        self.list=QListWidget();layout.addWidget(self.list,1)
        for row in self.rows:self.list.addItem(row['title']+' · pending')
        self.report_label=QLabel('Report: '+str(self.report));self.report_label.setWordWrap(True)
        self.report_label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(self.report_label)
        controls=QHBoxLayout();layout.addLayout(controls)
        self.start_button=QPushButton('Start audit' if not missing_only else 'Prepare missing')
        self.pause_button=QPushButton('Pause after current')
        self.cancel_button=QPushButton('Cancel')
        self.review_button=QPushButton('Review selected recording')
        self.close_button=QPushButton('Close')
        for button,slot in ((self.start_button,self.start),(self.pause_button,self.toggle_pause),
                (self.cancel_button,self.cancel),(self.review_button,self.review_selected),(self.close_button,self.reject)):
            controls.addWidget(button);button.clicked.connect(slot)
        self.list.currentRowChanged.connect(self._controls)
        self._controls()

    def _controls(self,*unused):
        active=self.started and not self.finished
        self.start_button.setEnabled(not self.started and bool(self.tracks))
        self.long_ctc.setEnabled(not self.started)
        self.pause_button.setEnabled(active and not self.cancelled)
        self.cancel_button.setEnabled(active and not self.cancelled)
        self.review_button.setEnabled(self.finished and 0<=self.list.currentRow()<len(self.tracks))
        self.close_button.setEnabled(not active)

    def _report(self):
        try:
            self.report.parent.mkdir(parents=True,exist_ok=True)
            atomic_json(self.report,{'schema':1,'scope':'missing' if self.missing_only else 'audit',
                'finished':self.finished,'cancelled':self.cancelled,'previous_approvals_trusted':False,
                'accuracy_target':0.95,'hallucination_target':0,'accuracy_measured':False,'records':self.rows})
        except Exception as error:
            self.report_label.setText('Report could not be saved: '+str(error)+'; results remain visible here.')

    def start(self):
        if self.started:return
        self.started=True;self._controls();self._report();QTimer.singleShot(0,self._next)

    def toggle_pause(self):
        if not self.started or self.finished or self.cancelled:return
        self.paused=not self.paused
        self.pause_button.setText('Resume' if self.paused else 'Pause after current')
        if self.paused:self.status.setText('Pausing after the current recording.')
        elif self.current is None:QTimer.singleShot(0,self._next)

    def cancel(self):
        if self.finished or self.cancelled:return
        self.cancelled=True;self.paused=False;self._controls()
        if self.current is not None and self.current.process.state()!=QProcess.ProcessState.NotRunning:
            self.current.cancel_job();self.status.setText('Cancelling current worker; remaining tracks will not start.')
        elif self.current is None:self._finish()

    def _row(self,status,detail):
        row=self.rows[self.index];row.update(status=status,detail=detail)
        self.list.item(self.index).setText(row['title']+' · '+status+' · '+detail)
        self._report()

    def _next(self):
        if self.finished or self.current is not None:return
        if self.cancelled or self.index>=len(self.tracks):self._finish();return
        if self.paused:self.status.setText('Paused between recordings.');return
        track=self.tracks[self.index]
        self.status.setText(f'{self.index+1}/{len(self.tracks)} · {track.title}')
        try:
            existing=self.window._find_document(track)
            self.rows[self.index]['previous_approval']=bool(existing and is_approved(existing))
            review=LyricsReviewDialog(track,self.window.data_dir,self.window.extract_config.cache_dir,existing,self)
            self.current=review
            if review.store.load_failed:raise ValueError('Existing private draft is unreadable; preserved for manual recovery.')
            self.rows[self.index]['source_sha256']=review.store.digest
            self.rows[self.index]['private_draft']=str(review.store.draft_path)
            route,detail,sheet=batch_plan(review.document,review.embedded,existing=existing,
                missing_only=self.missing_only,allow_long_ctc=self.long_ctc.isChecked(),backend=review.config.backend)
            self.rows[self.index]['route']=route
            if route not in ('align','extract'):
                self._complete(route,detail);return
            self._row('processing',detail)
            # Explicit batch Start authorizes a new private candidate, not publication.
            review.replace_timing.setChecked(True)
            review.process.finished.connect(lambda *args,r=review:self._worker_done(r))
            review.process.errorOccurred.connect(lambda error,r=review:self._worker_error(r,error))
            review.process.readyReadStandardOutput.connect(lambda r=review:self._worker_progress(r))
            if route=='align':
                if review.document.duration>180 and review.config.backend=='qwen':
                    review.review_device_select.setCurrentIndex(0)
                review.start_job('align',source='sheet',supplied_sheet=sheet)
            else:
                review.ignore_tags.setChecked(True)
                review.start_job('extract')
            if self.current is review and not review.busy:
                self._complete('failed',review.status.text())
        except Exception as error:
            self._complete('failed',str(error))

    def _worker_progress(self,review):
        if self.current is review and not self.paused and not self.cancelled:
            self.status.setText(f'{self.index+1}/{len(self.tracks)} · {self.tracks[self.index].title} · '+review.status.text())

    def _worker_error(self,review,error):
        if error==QProcess.ProcessError.FailedToStart:
            QTimer.singleShot(0,lambda:self._worker_done(review))

    def _worker_done(self,review):
        if self.current is not review:return
        # The review dialog's own finished handler saves candidate/checkpoint
        # first. Never turn an empty or failed result into an instrumental.
        if self.cancelled:status='cancelled'
        elif review._seen_result and review.document.segments:status='candidate'
        elif review._seen_result or review._recoverable_candidate:status='review'
        else:status='failed'
        self._complete(status,review.status.text())

    def _complete(self,status,detail):
        self._row(status,detail)
        review=self.current;self.current=None
        if review is not None:
            review.timer.stop();review.player.stop();review.player.setSource(QUrl())
            review.deleteLater()
        self.index+=1;self.progress.setValue(self.index)
        if self.cancelled or self.index>=len(self.tracks):self._finish()
        else:QTimer.singleShot(0,self._next)

    def _finish(self):
        if self.finished:return
        self.finished=True
        if self.cancelled:
            for i in range(self.index,len(self.rows)):
                if self.rows[i]['status']=='pending':
                    self.rows[i].update(status='cancelled',detail='Not started.')
                    self.list.item(i).setText(self.rows[i]['title']+' · cancelled · Not started.')
        counts={}
        for row in self.rows:counts[row['status']]=counts.get(row['status'],0)+1
        self.status.setText(('Cancelled. ' if self.cancelled else 'Batch complete. ')+
            ', '.join(f'{k}: {v}' for k,v in sorted(counts.items()))+
            '. New candidates are private and unverified.')
        self._controls();self._report()

    def review_selected(self):
        i=self.list.currentRow()
        if not self.finished or not 0<=i<len(self.tracks):return
        track=self.tracks[i]
        try:
            review=LyricsReviewDialog(track,self.window.data_dir,self.window.extract_config.cache_dir,
                                      self.window._find_document(track),self)
            review.approved.connect(lambda path,doc:self.window._lyrics_ready(track,path,doc))
            review.exec()
            review.deleteLater()
        except Exception as error:self.status.setText(str(error))

    def reject(self):
        if self.started and not self.finished:self.cancel();return
        super().reject()

    def closeEvent(self,event):
        if self.started and not self.finished:self.cancel();event.ignore();return
        super().closeEvent(event)
