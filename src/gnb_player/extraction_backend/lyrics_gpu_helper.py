#!/usr/bin/env python3
"""Read-only model inference in a separate, existing-library ROCm runtime."""
from pathlib import Path
import json,sys,os
# Keep the sibling backend importable even with a safe-path Python runtime.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import torch
try:
    import resource
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
except ImportError:
    pass
os.environ['GNB_GPU_HELPER_ACTIVE']='1'
torch.set_num_threads(2)
# Prefer the portable math attention path in this isolated helper process.
torch.backends.cuda.enable_flash_sdp(False)
torch.backends.cuda.enable_mem_efficient_sdp(False)
torch.backends.cuda.enable_math_sdp(True)
torch.backends.cudnn.enabled=False
def progress(phase,fraction,message):print(json.dumps(dict(phase=phase,fraction=fraction,message=message)),flush=True)
if __name__=='__main__':
    operation,source,target,work=sys.argv[1:];source=Path(source);target=Path(target);work=Path(work)
    if not torch.cuda.is_available():raise RuntimeError('ROCm device unavailable')
    if operation=='separate':
        from lyrics_pipeline import separate_vocals
        separate_vocals(source,target,work,progress)
    elif operation=='align':
        import soundfile as sf
        from lyrics_pipeline import align_reference
        request=json.loads((work/'align-request.json').read_text())
        audio,sr=sf.read(source,dtype='float32')
        result=align_reference(audio,sr,request['lines'],progress)
        target.write_text(json.dumps(result));target.chmod(0o600)
    else:raise ValueError('Unknown inference operation')
