#!/usr/bin/env python3
"""Render fake protected-profile controls with no keyring or stream access."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND','ffmpeg')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from gnb_player.app import MainWindow, STYLE
from gnb_player.broadcast_profile import BroadcastProfileStore
from gnb_player.extraction import ExtractionConfig


class PreviewCredentials:
    def __init__(self):self.values={}
    def get_password(self,service,user):return self.values.get((service,user))
    def set_password(self,service,user,value):self.values[service,user]=value
    def delete_password(self,service,user):self.values.pop((service,user),None)


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    with tempfile.TemporaryDirectory(prefix='fake-broadcast-profile-preview-') as folder, \
         patch('gnb_player.app.discover_config',lambda:ExtractionConfig()), \
         patch('gnb_player.app.config_status',lambda config:{'ready':False,'missing_models':[]}), \
         patch.object(MainWindow,'_task',lambda self,fn,done:done(fn())):
        backend=PreviewCredentials();store=BroadcastProfileStore(Path(folder)/'data',backend_factory=lambda:backend)
        window=MainWindow(store.data_dir,credential_store=store);window.tabs.setCurrentIndex(2)
        window.output_mode.setCurrentIndex(1);window.destination.setText('rtmps://example.invalid/application')
        window.stream_key.setText('FAKE-PREVIEW-KEY');window.save_stream_profile()
        window.now_title.setText('Saved livestream · disposable preview');window.now_artist.setText('Fake credentials only · no connection or output')
        window.show();app.processEvents();checks=[]
        for width,height in ((1370,900),(1030,720)):
            window.resize(width,height);app.processEvents()
            assert (window.width(),window.height())==(width,height)
            image=args.output_dir/f'broadcast-profile-{width}x{height}.png';assert window.grab().save(str(image))
            controls={}
            for name in ('save_stream_button','restore_stream_button','forget_stream_button','clear_stream_button','stream_key','stream_profile_status'):
                widget=getattr(window,name);point=widget.mapTo(window,QPoint(0,0))
                assert widget.isVisible() and point.x()>=0 and point.y()>=0
                assert point.x()+widget.width()<=width and point.y()+widget.height()<=height
                controls[name]=[point.x(),point.y(),widget.width(),widget.height()]
            checks.append({'size':[width,height],'image':str(image),'controls':controls})
        (args.output_dir/'broadcast-profile-preview-checks.json').write_text(json.dumps(checks,indent=2)+'\n')
        window._closing=True;window.close();app.processEvents()
    print('Fake-only Broadcast profile previews saved to '+str(args.output_dir))


if __name__=='__main__':main()
