import sys,unittest,tempfile
from pathlib import Path
from types import ModuleType,SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src/gnb_player"))
import qwen_review_backend as backend
from review_qwen_worker import _model_evidence
class AdapterTests(unittest.TestCase):
 def test_legacy_and_rocm_runtime_options_and_provenance(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d);(p/'config.json').write_text('{}')
   torch=ModuleType('torch');torch.float32='fp32';torch.float16='fp16';torch.bfloat16='bf16';torch.__version__='test+rocm';torch.version=SimpleNamespace(hip='test-hip')
   torch.manual_seed=Mock();torch.get_num_threads=lambda:8;torch.get_num_interop_threads=lambda:8
   torch.cuda=SimpleNamespace(is_available=Mock(return_value=True),get_device_properties=lambda _:SimpleNamespace(name='RX 7600 XT',gcnArchName='gfx1102',total_memory=16*1024**3))
   transformers=ModuleType('transformers');transformers.__version__='4.57.6'
   with patch.dict(sys.modules,{'torch':torch,'transformers':transformers}),patch.object(backend.importlib.metadata,'version',return_value='0.0.6'),patch.object(backend,'_sha256',return_value=backend.MODELS['qwen_asr_model'][1]):
    _,_,options,meta=backend._runtime({'config':{'qwen_asr_model':d}},'qwen_asr_model')
    self.assertEqual(options['device_map'],'cpu');torch.cuda.is_available.assert_not_called();self.assertEqual(meta['dtype'],'float32')
    _,_,options,meta=backend._runtime({'config':{'qwen_asr_model':d,'review_device':'rocm'}},'qwen_asr_model')
    self.assertEqual(options['device_map'],'cuda:0');self.assertEqual(meta['gpu_arch'],'gfx1102');self.assertEqual(_model_evidence(meta)['hip'],'test-hip')
if __name__=='__main__':unittest.main()
