"""Own-process native Qt smoke: muted monitoring, clean local output, no network ingest."""
import sys,os,json,uuid,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ.setdefault('QT_QPA_PLATFORM','offscreen');os.environ.setdefault('QT_MEDIA_BACKEND','ffmpeg')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from gnb_player.app import MainWindow,STYLE
root=Path(__file__).resolve().parents[1];out=root/'artifacts';out.mkdir(exist_ok=True)
app=QApplication([]);app.setStyleSheet(STYLE)
w=MainWindow(out/'qa-state',root/'media-packs/goodneighbor/profile.json');w.engine.set_volume(0);w.volume.setValue(0);w.show()
record=out/('operator-smoke-'+uuid.uuid4().hex[:8]+'.mp4');errors=[];w.export.error.connect(errors.append)
def play():
 assert len(w.all_tracks)==110;assert len(w.lyric_files)==32
 w.shuffle.setChecked(False);w.playlist.setCurrentRow(0);w.toggle_play()
def seek():w.engine.seek(100);w.export.clear_audio()
def start():w.destination.setText(str(record));w.start_broadcast()
def proof():
 w.grab().save(str(out/'operator-desk.png'));w.stage.render_frame(1920,1080).save(str(out/'program-output.png'))
 print(json.dumps({'tracks':len(w.all_tracks),'lyrics':len(w.lyric_files),'playing':w.engine.playing,'position':w.engine.position,'recording':w.export.running,'current':w.engine.current_track.title,'errors':errors}),flush=True)
def stop():w.export.stop()
def finish():
 print(json.dumps({'recording_file':str(record),'bytes':record.stat().st_size if record.exists() else 0,'errors':errors}),flush=True)
 w._closing=True;w.close();app.quit()
QTimer.singleShot(300,play);QTimer.singleShot(1800,seek);QTimer.singleShot(3000,start);QTimer.singleShot(7000,proof);QTimer.singleShot(14000,stop);QTimer.singleShot(17000,finish);QTimer.singleShot(25000,app.quit)
raise SystemExit(app.exec())
