import unittest
from types import SimpleNamespace
from unittest.mock import Mock
import sys
from pathlib import Path
from gnb_player.review_device import select_device
class DeviceTests(unittest.TestCase):
 def fake(self,hip='10.0',available=True):
  cuda=SimpleNamespace(is_available=Mock(return_value=available),get_device_properties=Mock(return_value=SimpleNamespace(name='RX 7600 XT',gcnArchName='gfx1102',total_memory=16*1024**3)))
  return SimpleNamespace(float32='fp32',float16='fp16',bfloat16='bf16',version=SimpleNamespace(hip=hip),cuda=cuda)
 def test_default_cpu_never_initializes_gpu(self):
  t=self.fake();o,m=select_device(t,{});self.assertEqual(o,{'dtype':'fp32','device_map':'cpu'});t.cuda.is_available.assert_not_called();self.assertEqual(m['device'],'cpu')
 def test_gpu_uses_rocm_cuda_alias_and_real_metadata(self):
  o,m=select_device(self.fake(),{'review_device':'rocm'});self.assertEqual(o,{'dtype':'fp32','device_map':'cuda:0'});self.assertEqual(m['gpu_arch'],'gfx1102');self.assertEqual(m['backend'],'AMD ROCm')
 def test_no_silent_fallback_cpu_wheel(self):
  with self.assertRaisesRegex(RuntimeError,'ROCm PyTorch'):select_device(self.fake(hip=None),{'review_device':'rocm'})
 def test_no_silent_fallback_unavailable(self):
  with self.assertRaisesRegex(RuntimeError,'No silent CPU fallback'):select_device(self.fake(available=False),{'review_device':'rocm'})
 def test_optional_precision_explicit(self):
  o,_=select_device(self.fake(),{'review_device':'rocm','review_precision':'float16'});self.assertEqual(o['dtype'],'fp16')
 def test_bad_policy_rejected(self):
  for config in [{'review_device':'cuda'},{'review_precision':'int8'},{'review_precision':'float16'}]:
   with self.assertRaises(ValueError):select_device(self.fake(),config)
if __name__=='__main__':unittest.main()
