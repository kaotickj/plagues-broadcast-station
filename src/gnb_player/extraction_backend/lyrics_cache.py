"""Bounded private derived-audio/evidence cache; never stores or deletes masters."""
from pathlib import Path
import os,json,hashlib,time,re,uuid,math
LIMIT=2*1024**3
NAME=re.compile(r'^(?:vocals|evidence)-[a-f0-9]{64}\.(?:flac|json)$')
def directory():
    value=os.environ.get('GNB_LYRICS_CACHE_DIR')
    if not value:return None
    p=Path(value)
    if p.is_symlink():return None
    p.mkdir(parents=True,exist_ok=True,mode=0o700)
    return p
def digest(p):
    with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def atomic(path,data):
    tmp=path.with_name('.'+uuid.uuid4().hex)
    try:tmp.write_text(json.dumps(data,allow_nan=False));tmp.chmod(0o600);tmp.replace(path)
    finally:tmp.unlink(missing_ok=True)
def prune(folder,limit=LIMIT):
    files=[p for p in folder.iterdir() if not p.is_symlink() and p.is_file() and NAME.fullmatch(p.name)]
    total=sum(p.stat().st_size for p in files)
    for p in sorted(files,key=lambda p:p.stat().st_mtime):
        if total<=limit and p.stat().st_mtime>time.time()-30*86400:continue
        total-=p.stat().st_size;p.unlink(missing_ok=True)
def vocal_paths(source):
    folder=directory()
    if folder is None:return None
    key=hashlib.sha256(('htdemucs-vocals-16k-pcm16-v1:'+digest(source)).encode()).hexdigest()
    return folder/f'vocals-{key}.flac',folder/f'vocals-{key}.json'
def restore(source,target):
    try:
        paths=vocal_paths(source)
        if not paths:return False
        audio,meta=paths
        if audio.is_symlink() or meta.is_symlink() or not audio.is_file() or not meta.is_file():return False
        d=json.loads(meta.read_text())
        if not isinstance(d,dict) or digest(audio)!=d.get('sha256'):return False
        import soundfile as sf
        samples,sr=sf.read(audio,dtype='float32')
        if sr!=16000 or samples.ndim!=1 or not 1<=len(samples)/sr<=900:return False
        sf.write(target,samples,sr,subtype='PCM_16');audio.touch();meta.touch();return True
    except (OSError,ValueError,KeyError,TypeError,RuntimeError):return False
def store(source,wav):
    try:
        paths=vocal_paths(source)
        if not paths:return
        audio,meta=paths
        if audio.is_symlink() or meta.is_symlink():return
        import soundfile as sf
        samples,sr=sf.read(wav,dtype='float32')
        if sr!=16000 or samples.ndim!=1 or not 1<=len(samples)/sr<=900:return
        tmp=audio.with_name('.'+uuid.uuid4().hex+'.flac')
        try:
            sf.write(tmp,samples,sr,subtype='PCM_16');tmp.chmod(0o600);tmp.replace(audio)
        finally:tmp.unlink(missing_ok=True)
        atomic(meta,{'sha256':digest(audio),'version':'htdemucs-vocals-16k-pcm16-v1'});prune(audio.parent)
    except (OSError,ValueError,RuntimeError):return
def evidence_path(audio,sr,config):
    folder=directory()
    if folder is None:return None
    h=hashlib.sha256((str(sr)+':'+config).encode());h.update(memoryview(audio).cast('B'))
    return folder/f'evidence-{h.hexdigest()}.json'
def read_evidence(path,duration):
    if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size>2*1024*1024:return None
    try:
        data=json.loads(path.read_text());words=data['words'];previous=-1
        if not isinstance(words,list) or len(words)>10000:return None
        for w in words:
            if not isinstance(w,dict):return None
            a,b,p=w['start'],w['end'],w['score']
            if not isinstance(w['text'],str) or not all(isinstance(x,(int,float)) and math.isfinite(x) for x in [a,b,p]) or a<previous-.1 or b<a or a<0 or b>duration+.1 or not 0<=p<=1:return None
            previous=a
        path.touch();return words
    except (OSError,ValueError,KeyError,TypeError):return None
def read_evidence_metadata(path):
    if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size>2*1024*1024:return None
    try:
        raw=json.loads(path.read_text()).get('recognition')
        return raw if isinstance(raw,dict) else None
    except (OSError,ValueError,TypeError):return None
def write_evidence(path,words,recognition=None):
    if path is None or path.is_symlink():return
    try:atomic(path,{'words':words,**({'recognition':recognition} if recognition is not None else {})});prune(path.parent)
    except (OSError,ValueError,TypeError):return


PRIMARY_SCHEMA = 'raw-primary-recognition-2'
PRIMARY_LIMIT = 2 * 1024 * 1024


def primary_path(pcm_sha, identity, policy, start, end):
    folder = directory()
    if folder is None: return None
    value = {'schema': PRIMARY_SCHEMA, 'pcm_sha256': pcm_sha, 'identity': identity,
             'policy': policy, 'start': start, 'end': end}
    key = hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return folder / ('evidence-' + key + '.json')


def _primary_valid(raw, identity):
    # Decoder evidence, not validated lyrics: retain measured zero-duration or
    # stretched words so retrying CTC/review need not rerun recognition. All
    # downstream timing/quality/save guards still run identically on cache hits.
    if not isinstance(identity, dict) or not isinstance(raw, dict) or set(raw) - {'systeminfo', 'model', 'params', 'result', 'transcription'}:
        return False
    rows = raw.get('transcription')
    duration = identity.get('duration')
    if (not isinstance(duration, (int, float)) or isinstance(duration, bool) or
            not math.isfinite(duration) or duration <= 0 or not isinstance(rows, list) or not 0 < len(rows) <= 10000):
        return False
    previous = 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str): return False
        bounds = row.get('offsets')
        if not isinstance(bounds, dict): return False
        a, b = bounds.get('from'), bounds.get('to')
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (a, b)): return False
        # Whisper decodes a final padded30s block. Preserve that raw evidence;
        # it must not become a claim that the tail is valid recording timing.
        if not 0 <= a <= b <= (duration + 30) * 1000 + .001: return False
        if a < previous: return False
        previous = a
        tokens = row.get('tokens')
        if not isinstance(tokens, list) or not tokens: return False
        for token in tokens:
            if not isinstance(token, dict) or not isinstance(token.get('text'), str): return False
            score = token.get('p')
            if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1: return False
    return True


def read_primary(path, identity):
    try:
        if path is None or path.is_symlink() or not path.is_file() or path.stat().st_size > PRIMARY_LIMIT: return None
        data = json.loads(path.read_text())
        raw = data.get('raw')
        if data.get('version') != PRIMARY_SCHEMA or data.get('identity') != identity or not _primary_valid(raw, identity): return None
        payload = json.dumps(raw, sort_keys=True, allow_nan=False).encode()
        if hashlib.sha256(payload).hexdigest() != data.get('raw_sha256'): return None
        return raw
    except (OSError, ValueError, TypeError, AttributeError): return None


def write_primary(path, raw, identity):
    try:
        if path is None or path.is_symlink() or not _primary_valid(raw, identity): return False
        data = {'version': PRIMARY_SCHEMA, 'identity': identity, 'raw': raw,
                'raw_sha256': hashlib.sha256(json.dumps(raw, sort_keys=True, allow_nan=False).encode()).hexdigest()}
        if len(json.dumps(data, allow_nan=False).encode()) > PRIMARY_LIMIT: return False
        atomic(path, data)
        prune(path.parent)
        return True
    except (OSError, ValueError, TypeError): return False
