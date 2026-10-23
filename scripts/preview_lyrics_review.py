#!/usr/bin/env python3
"""Render disposable review-workflow fixtures. Never opens the operator's data."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
os.environ.setdefault('QT_MEDIA_BACKEND','ffmpeg')
os.environ.setdefault('LIBGL_ALWAYS_SOFTWARE','1')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication
from gnb_player.app import MainWindow, STYLE
from gnb_player.library import Track
from gnb_player.lyrics import Cue, LyricsDocument, Word, fingerprint, load_lyrics, save_lyrics
from gnb_player.lyric_review import document_revision, save_review_document


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,required=True)
    args=parser.parse_args();args.output_dir.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([]);app.setStyleSheet(STYLE)
    with tempfile.TemporaryDirectory(prefix='broadcast-review-preview-') as folder:
        root=Path(folder);source=root/'review-workflow-demo.wav';source.write_bytes(b'disposable screenshot fixture; never played')
        track=Track(source,'Evening Road · review demo','Disposable fixture',duration=42)
        document=LyricsDocument([
            Cue('A quiet evening carries me',2,6,[Word('A',2,2.4),Word('quiet',2.4,3),Word('evening',3,4),Word('carries',4,5),Word('me',5,6)]),
            Cue('The old road brings me home',9,14,[Word('The',9,9.5),Word('old',9.5,10),Word('road',10,11),Word('brings',11,12),Word('me',12,13),Word('home',13,14)]),
            Cue('Thank you for listening.',28,31,[Word('Thank',28,28.5),Word('you',28.5,29),Word('for',29,30),Word('listening.',30,31)]),
        ],42,track.title,fingerprint(source),['Independent review left a summary warning.'],
            {'status':'review','reasons':[
                {'code':'uncertain','message':'Check this phrase against the recording.','at':2.4},
                {'code':'hallucination','message':'A passage resembles a transcription sign-off.','at':28},
                {'code':'model_warning','message':'Review the stored model note.'},
            ]})
        target=root/'fixture-lyrics.json';save_lyrics(document,target)
        window=MainWindow(root/'state');window.all_tracks=[track];window._apply_queue()
        window._lyrics_ready(track,target,load_lyrics(target));window.tabs.setCurrentIndex(1)
        window.now_title.setText('Review workflow · disposable preview')
        window.now_artist.setText('Saved draft, editable lines, and explicit approval')
        window.show();app.processEvents()
        checks=[]
        for width,height in [(1370,900),(1030,720)]:
            window.resize(width,height);app.processEvents()
            image=args.output_dir/f'lyrics-review-{width}x{height}.png'
            assert window.grab().save(str(image))
            buttons={}
            for name in ('save_lyrics_button','approve_lyrics_button','reload_lyrics_button','review_confirm'):
                widget=getattr(window,name);point=widget.mapTo(window,QPoint(0,0))
                rect=[point.x(),point.y(),widget.width(),widget.height()]
                assert widget.isVisible() and point.x()>=0 and point.y()>=0
                assert point.x()+widget.width()<=window.width() and point.y()+widget.height()<=window.height()
                buttons[name]={'rect':rect,'enabled':widget.isEnabled()}
            assert window.width()==width and window.height()==height
            checks.append({'size':[width,height],'image':str(image),'controls':buttons})
        # Approval uses the same guarded persistence API as the UI, on this fixture only.
        current=load_lyrics(target)
        approved=save_review_document(current,target,source,document_revision(current),approve=True)
        window._lyrics_ready(track,target,approved);window.resize(1370,900);app.processEvents()
        assert window.grab().save(str(args.output_dir/'lyrics-reviewed-ready-1370x900.png'))
        (args.output_dir/'preview-checks.json').write_text(json.dumps(checks,indent=2)+'\n')
        window._closing=True;window.close();app.processEvents()
    print('Disposable preview and control-bound checks saved to '+str(args.output_dir))


if __name__=='__main__':main()
