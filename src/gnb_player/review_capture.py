from collections.abc import Mapping
from pathlib import Path
import json,os,tempfile

def atomic_json(path, value, exclusive=False):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if exclusive and path.exists():raise FileExistsError(path)
    payload=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')
    tmp=None
    try:
        with tempfile.NamedTemporaryFile(prefix='.'+path.name+'.',suffix='.tmp',dir=path.parent,delete=False) as f:
            tmp=Path(f.name);f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path);tmp=None
        fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if tmp is not None:tmp.unlink(missing_ok=True)

def plain(value):
    if hasattr(value,'detach'):value=value.detach().cpu()
    if hasattr(value,'tolist'):return plain(value.tolist())
    if isinstance(value,Mapping):return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [plain(v) for v in value]
    if value is None or type(value) in (str,int,float,bool):return value
    raise TypeError('Unsupported JSON value: '+type(value).__name__)

def save_sequences(generated,path,metadata):
    value=generated['sequences'] if isinstance(generated,Mapping) else generated.sequences
    rows=plain(value)
    if rows and type(rows[0]) is int:rows=[rows]
    if not isinstance(rows,list) or any(not isinstance(row,list) or any(type(t) is not int for t in row) for row in rows):
        raise TypeError('Expected a batch of one-dimensional integer token sequences')
    raw={**plain(metadata),'return_type':type(generated).__name__,'sequences':rows}
    atomic_json(path,raw,exclusive=True)
    return rows

def compact_segments(generated):
    batches=generated.get('segments') if isinstance(generated,Mapping) else getattr(generated,'segments',None)
    if batches is None:return None
    # Preserve native times/tokens, excluding potentially huge decoder caches in result.
    return [[{k:plain(seg[k]) for k in ['start','end','tokens','idxs'] if k in seg} for seg in batch] for batch in batches]
