from pathlib import Path
import os,sys,tempfile,unittest
import pytest
if os.name=="nt":pytest.skip("POSIX shared GPU leases",allow_module_level=True)
import fcntl
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/gnb_player/extraction_backend'))
import lyrics_acceleration as gpu
import lyrics_pipeline as pipeline
import lyrics_review as review

class PhaseLeaseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.lock=Path(self.tmp.name)/'gpu.lock';self.lock.touch()
  self.env=patch.dict(os.environ,{'GNB_LYRICS_GPU':'1','GNB_LYRICS_PHASE_GPU':'1','GNB_LYRICS_GPU_LOCK':str(self.lock),'GNB_LYRICS_GPU_WAIT':'1','GNB_LYRICS_GPU_WAIT_SECONDS':'0'})
  self.env.start();self.capacity=patch.object(gpu,'capacity',return_value={'available':True,'busy':False});self.capacity.start()
 def tearDown(self):self.capacity.stop();self.env.stop();self.tmp.cleanup()
 def available(self):
  with self.lock.open('r+') as f:fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 def test_gpu_phase_excludes_competing_process_and_releases_on_error(self):
  self.available()
  with self.assertRaisesRegex(RuntimeError,'fixture'):
   with gpu.gpu_phase(lambda *a:None,'aligning'):
    with self.assertRaises(BlockingIOError):self.available()
    self.assertEqual(os.environ.get('GNB_LYRICS_GPU_RESERVED'),'1')
    raise RuntimeError('fixture')
  self.available();self.assertNotIn('GNB_LYRICS_GPU_RESERVED',os.environ)
 def test_busy_gpu_is_deferred_without_cpu_fallback(self):
  with self.lock.open('r+') as other:
   fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaises(gpu.GpuCapacityDeferred):
    with gpu.gpu_phase(lambda *a:None,'transcribing'):self.fail('must not enter GPU phase')
  self.available();self.assertNotIn('GNB_LYRICS_GPU_BLOCKED',os.environ)
 def test_explicit_cpu_choice_avoids_wait(self):
  with patch.dict(os.environ,{'GNB_LYRICS_GPU_WAIT':'0'}),self.lock.open('r+') as other:
   fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
   with self.assertRaisesRegex(RuntimeError,'explicitly selected'):
    with gpu.gpu_phase(lambda *a:None,'transcribing'):self.fail('must not enter GPU phase')
 def test_wait_does_not_hold_gpu_and_can_acquire_after_other_owner_finishes(self):
  other=self.lock.open('r+');fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
  def release(_):other.close()
  with patch.dict(os.environ,{'GNB_LYRICS_GPU_WAIT_SECONDS':'10'}),patch.object(gpu.time,'sleep',side_effect=release) as sleeper:
   with gpu.gpu_phase(lambda *a:None,'transcribing'):
    with self.assertRaises(BlockingIOError):self.available()
   self.assertEqual(sleeper.call_count,1)
  self.available()
 def test_alignment_busy_propagates_without_silent_cpu_alignment(self):
  with patch.dict(sys.modules,{'soundfile':SimpleNamespace(write=lambda *a,**k:None)}),patch.object(gpu,'enabled',return_value=True),patch.object(gpu,'helper',side_effect=gpu.GpuCapacityDeferred('busy')),patch.object(pipeline,'align_reference',side_effect=AssertionError('silent CPU fallback')):
   with self.assertRaises(gpu.GpuCapacityDeferred):gpu.realign([],16000,[{'text':'Fixture'}],Path(self.tmp.name),lambda *a:None)
 def test_review_busy_propagates_without_silent_cpu_model(self):
  with patch.object(gpu,'enabled',return_value=True),patch.object(gpu,'recognize_windows',side_effect=gpu.GpuCapacityDeferred('busy')):
   with self.assertRaises(gpu.GpuCapacityDeferred):review.recognize([],16000,[('full',0,1)],'large-v3',lambda *a:None)
 def test_primary_busy_propagates_without_silent_cpu_whisper(self):
  import numpy as np
  fake_torch=SimpleNamespace(set_num_threads=lambda *a:None,set_num_interop_threads=lambda *a:None)
  fake_sf=SimpleNamespace(read=lambda *a,**k:(np.zeros(16000),16000))
  with patch.dict(sys.modules,{'torch':fake_torch,'soundfile':fake_sf}),patch.object(pipeline,'decode'),patch.object(gpu,'enabled',return_value=True),patch.object(gpu,'recognize_windows',side_effect=gpu.GpuCapacityDeferred('busy')),patch.object(pipeline,'transcribe_whisper',side_effect=AssertionError('silent CPU fallback')):
   with self.assertRaises(gpu.GpuCapacityDeferred):pipeline.extract(Path(self.tmp.name)/'song.mp3',Path(self.tmp.name),'large-v3',False,'',lambda *a:None)

if __name__=='__main__':unittest.main()
