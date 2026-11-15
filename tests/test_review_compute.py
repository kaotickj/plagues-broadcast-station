import tempfile,json,unittest,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from gnb_player.review_core import ReviewConfig
from gnb_player.review_dialog import LyricsReviewDialog
from gnb_player import studio_settings as s
class ComputeTests(unittest.TestCase):
 def test_routes(self):
  c=ReviewConfig(align_python='/cpu',review_gpu_python=sys.executable)
  self.assertEqual(c.for_job().align_python,'/cpu')
  c.review_device='rocm';self.assertEqual(c.for_job().align_python,sys.executable);self.assertEqual(c.align_python,'/cpu')
  c.backend='heart_ctc'
  with self.assertRaises(ValueError):c.for_job()
 def test_invalid(self):
  for data in ({'review_device':'cuda'},{'review_precision':'float16'},{'review_device':'rocm','review_gpu_python':'/nonexistent'}):
   with self.assertRaises(ValueError):ReviewConfig(**data).for_job()
 def test_ui_save_reload_failure_and_backup(self):
  app=QApplication.instance() or QApplication([])
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);data=root/'data';data.mkdir();cache=root/'cache';cache.mkdir();wav=root/'song.wav';wav.write_bytes(b'fixture')
   p=data/'lyrics-review-runtime.json';p.write_text(json.dumps({'align_python':'/cpu','unrelated':'retain'}))
   track=SimpleNamespace(path=wav,title='Test',duration=12)
   with patch('gnb_player.review_dialog.embedded_lyrics',return_value={'text':'','kind':'no_embedded_lyrics'}):
    w=LyricsReviewDialog(track,data,cache)
    try:
     self.assertEqual(w.review_device_select.currentData(),'cpu');w.review_gpu_python.setText(sys.executable);w.review_device_select.setCurrentIndex(1)
     w.save_review_compute();saved=json.loads(p.read_text());self.assertEqual(saved['unrelated'],'retain');self.assertEqual(saved['align_python'],'/cpu');self.assertEqual(ReviewConfig.discover(data).review_device,'rocm')
     with patch.object(w.store,'verify'),patch.object(w,'save_draft',return_value=True),patch.object(ReviewConfig,'missing',return_value=[]),patch.object(w.process,'start') as start:
      w.start_job('extract');self.assertTrue(start.called);self.assertEqual(start.call_args.args[0],sys.executable);self.assertEqual(w._job_request['config']['review_device'],'rocm');self.assertEqual(w._job_request['config']['align_python'],sys.executable)
     w.set_busy(False)
     w.set_busy(True);self.assertFalse(w.review_device_select.isEnabled());self.assertFalse(w.save_review_compute_button.isEnabled());w.set_busy(False)
     before=p.read_bytes();w.review_gpu_python.setText('/not/a/runtime');w.save_review_compute();self.assertEqual(p.read_bytes(),before)
     backup=s.make_backup(data);dest=root/'backup.json';s.export_studio(dest,backup,data);target=root/'target';target.mkdir();files,_,_=s.prepare_import(dest,target);s.import_studio(target,files)
     restored=ReviewConfig.discover(target);self.assertEqual(restored.review_device,'rocm');self.assertEqual(restored.review_gpu_python,sys.executable)
     w.review_device_select.setCurrentIndex(0);w.save_review_compute();self.assertEqual(ReviewConfig.discover(data).for_job().align_python,'/cpu')
    finally:w.dirty=False;w.close()
