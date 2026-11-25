"""Portable, non-secret studio snapshots and recoverable multi-file imports."""
from contextlib import contextmanager
from copy import deepcopy
import base64
import json
import math
import os
from pathlib import Path, PureWindowsPath
import re
import threading
from urllib.parse import urlsplit
from .settings import atomic_json
from .extraction import ExtractionConfig, MODEL_PATH_FIELDS
from .broadcast_profile import StreamProfile, public_destination

FILES=('settings.json','session.json','broadcast-settings.json')
OPTIONAL=('lyrics-review-runtime.json',)
KIND='plagues-broadcast-station-studio-settings'
LIMIT=16*1024*1024
EXCLUSIONS=['OS-keyring credentials and credential references','passwords, tokens, keys and authenticated/private streaming URLs','media/model binaries, lyric contents and caches','logs, diagnostics and current playback/output activity']
_local=threading.local()

@contextmanager
def studio_lock(directory):
    from PySide6.QtCore import QLockFile
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    key=str(directory.resolve());held=getattr(_local,'held',set())
    if key in held:
        yield;return
    lock=QLockFile(str(directory/'.studio-settings.lock'))
    if not lock.tryLock(1000):raise OSError('Studio settings are busy in another operation. Try again.')
    _local.held=held|{key}
    try:yield
    finally:_local.held=held;lock.unlock()

def read_json(path,default=None,limit=LIMIT):
    try:
        with Path(path).open('rb') as f:raw=f.read(limit+1)
    except FileNotFoundError:
        if default is not None:return deepcopy(default)
        raise
    if len(raw)>limit:raise ValueError('Settings file is too large.')
    def duplicate(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate settings field.')
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=duplicate,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Non-finite number.')))

def _sensitive(key):
    key=re.sub('[^a-z0-9]','',key.lower())
    return any(word in key for word in ('password','passwd','passphrase','secret','credential','token','authorization','cookie','apikey','streamkey','privatekey','accesskey')) or key in {'key','auth'}

def _private_value(value,secrets):
    if any(secret and secret in value for secret in secrets):return True
    if '-----BEGIN ' in value and 'PRIVATE KEY-----' in value:return True
    # Do not carry URL credentials, query tokens or private stream targets.
    for url in re.findall(r'[a-zA-Z][a-zA-Z0-9+.-]*://[^\s]+',value):
        try:
            p=urlsplit(url)
            if p.username or p.password or p.query or p.fragment:return True
            if p.scheme in {'rtmp','rtmps'} and not public_destination(url):return True
        except ValueError:return True
    return False

def sanitize(value,secrets=(),trail='',excluded=None):
    excluded=excluded if excluded is not None else []
    if isinstance(value,dict):
        result={}
        for key,item in value.items():
            location=trail+'.'+key
            if _sensitive(key) or (isinstance(item,str) and _private_value(item,secrets)):
                excluded.append(location);continue
            result[key]=sanitize(item,secrets,location,excluded)
        return result
    if isinstance(value,list):
        return [sanitize(item,secrets,trail+'[]',excluded) if not isinstance(item,str) or not _private_value(item,secrets) else '' for item in value]
    return value

def merge(existing,incoming):
    result=deepcopy(existing)
    for key,value in incoming.items():
        result[key]=merge(result[key],value) if isinstance(value,dict) and isinstance(result.get(key),dict) else deepcopy(value)
    return result

def _string(value,label,maxlen=8192):
    if not isinstance(value,str) or len(value)>maxlen or '\0' in value:raise ValueError('Invalid '+label)

def _path(value):
    _string(value,'file path')
    if '://' in value:raise ValueError('Use local file paths for studio media and runtimes.')

def validate_files(files):
    if not isinstance(files,dict) or not set(FILES).issubset(files) or not set(files).issubset(set(FILES)|set(OPTIONAL)):raise ValueError('Backup must contain all three studio settings sections.')
    for name,value in files.items():
        if not isinstance(value,dict):raise ValueError('Invalid settings section: '+name)
    config=files['settings.json']
    if not isinstance(config.get('extraction'),dict):raise ValueError('Missing lyric model settings.')
    ExtractionConfig.from_dict(config['extraction'])
    ui=config.get('studio_ui',{})
    if not isinstance(ui,dict):raise ValueError('Invalid studio controls.')
    for key in ('shuffle','repeat','broadcast_preview'):
        if key in ui and type(ui[key]) is not bool:raise ValueError('Invalid studio switch: '+key)
    for key,low,high in [('volume',0,100),('output_mode',0,1)]:
        if key in ui and (type(ui[key]) is not int or not low<=ui[key]<=high):raise ValueError('Invalid studio control: '+key)
    if 'offset' in ui and (type(ui['offset']) not in (int,float) or not math.isfinite(ui['offset']) or not -15<=ui['offset']<=15):raise ValueError('Invalid lyric preview offset.')
    if 'recording_destination' in ui:_path(ui['recording_destination'])
    session=files['session.json']
    if type(session.get('version')) is not int or session['version']!=1:raise ValueError('Unsupported playlist version.')
    _string(session.get('name'),'playlist name')
    tracks=session.get('tracks'); backgrounds=session.get('backgrounds');lyrics=session.get('lyrics');visual=session.get('settings')
    if not isinstance(tracks,list) or len(tracks)>20000 or not isinstance(backgrounds,list) or len(backgrounds)>20000 or not isinstance(lyrics,dict) or len(lyrics)>20000 or not isinstance(visual,dict):raise ValueError('Invalid playlist or appearance settings.')
    for row in tracks:
        if not isinstance(row,dict):raise ValueError('Invalid track.')
        _path(row.get('path'))
        for key in ('title','artist','album'):_string(row.get(key,''),'track '+key)
        duration=row.get('duration',0)
        if type(duration) not in (int,float) or not math.isfinite(duration) or duration<0:raise ValueError('Invalid track duration.')
    for value in backgrounds:_path(value)
    for key,value in lyrics.items():_path(key);_path(value)
    for key in ('brand','tagline'):
        if key in visual:_string(visual[key],key)
    for key in ('motion','waveform','lyrics','lyrics_ready_only','preview_unapproved_lyrics'):
        if key in visual and type(visual[key]) is not bool:raise ValueError('Invalid appearance switch.')
    if 'interval_seconds' in visual and (type(visual['interval_seconds']) is not int or not 5<=visual['interval_seconds']<=300):raise ValueError('Invalid artwork interval.')
    if 'waveform_color' in visual and (not isinstance(visual['waveform_color'],str) or not re.fullmatch('#[0-9a-fA-F]{6}',visual['waveform_color'])):raise ValueError('Invalid waveform color.')
    stream=files['broadcast-settings.json']
    if type(stream.get('schema')) is not int or stream['schema']!=1 or type(stream.get('remember')) is not bool:raise ValueError('Invalid broadcast schema.')
    target=stream.get('destination');_string(target,'broadcast server')
    if target and not public_destination(target):raise ValueError('Backup contains a private streaming target.')
    StreamProfile(target or 'rtmps://validation.invalid/app','',stream.get('picture_index'),stream.get('bitrate_kbps')).validate()
    review=files.get('lyrics-review-runtime.json',{})
    if 'review_device' in review and review['review_device'] not in {'cpu','rocm'}:raise ValueError('Invalid review processing device.')
    if 'review_precision' in review and review['review_precision']!='float32':raise ValueError('Review precision must be the validated FP32 option.')
    if 'backend' in review and review['backend'] not in {'qwen','heart_ctc'}:raise ValueError('Invalid review backend.')
    for key in ('heart_python','align_python','heart_model','ctc_model','shared_lock','qwen_asr_model','qwen_aligner_model','review_gpu_python'):
        if key in review:_path(review[key])
    if 'timeout_seconds' in review and (type(review['timeout_seconds']) is not int or review['timeout_seconds']<1):raise ValueError('Invalid review timeout.')
    # All extension values must also be ordinary finite JSON, bounded by LIMIT.
    if len(json.dumps(files,allow_nan=False).encode())>LIMIT:raise ValueError('Studio settings are too large.')
    return files

def _absolute(value,base):
    if not value:return value
    if PureWindowsPath(value).is_absolute() or Path(value).is_absolute():return value
    return str((Path(base)/value).resolve())

def _transform_paths(files,transform):
    model=files['settings.json']['extraction']
    for key in set(MODEL_PATH_FIELDS)|{'cache_dir','media_library_path'}:
        if model.get(key):model[key]=transform(model[key])
    for key in ('heart_python','align_python','heart_model','ctc_model','shared_lock','qwen_asr_model','qwen_aligner_model','review_gpu_python'):
        review=files.get('lyrics-review-runtime.json',{})
        if review.get(key):review[key]=transform(review[key])
    ui=files['settings.json'].get('studio_ui',{})
    if ui.get('recording_destination'):ui['recording_destination']=transform(ui['recording_destination'])
    s=files['session.json']
    for row in s['tracks']:row['path']=transform(row['path'])
    s['backgrounds']=[transform(p) for p in s['backgrounds']]
    s['lyrics']={transform(k):transform(v) for k,v in s['lyrics'].items()}

def snapshot_session(directory,tracks,backgrounds,lyrics,visual,name):
    directory=Path(directory)
    old=read_json(directory/'session.json',{})
    rows={_absolute(r['path'],directory):r for r in old.get('tracks',[]) if isinstance(r,dict) and isinstance(r.get('path'),str)}
    return {**old,'version':1,'name':name,
            'tracks':[{**rows.get(str(t.path),{}),'path':str(t.path),'title':t.title,'artist':t.artist,'album':t.album,'duration':t.duration} for t in tracks],
            'backgrounds':list(map(str,backgrounds)),'lyrics':dict(lyrics),'settings':{**old.get('settings',{}),**visual}}

def make_backup(directory,overrides=None,secrets=()):
    directory=Path(directory)
    with studio_lock(directory):
        files={name:read_json(directory/name,{}) for name in FILES}
        files.update({name:read_json(directory/name) for name in OPTIONAL if (directory/name).exists()})
        files['settings.json'].setdefault('extraction',ExtractionConfig().to_dict())
        files['session.json']=merge({'version':1,'name':'My station','tracks':[],'backgrounds':[],'lyrics':{},'settings':{}},files['session.json'])
        files['broadcast-settings.json']=merge({'schema':1,'destination':'','picture_index':0,'bitrate_kbps':4500,'remember':False},files['broadcast-settings.json'])
        for name,value in (overrides or {}).items():files[name]=merge(files[name],value)
    stream=files['broadcast-settings.json'];stream['remember']=False
    if not public_destination(stream.get('destination','')) or _private_value(stream.get('destination',''),secrets):stream['destination']=''
    excluded=[];files=sanitize(files,secrets,excluded=excluded)
    validate_files(files);_transform_paths(files,lambda p:_absolute(p,directory))
    return {'kind':KIND,'version':1,'source':{'home':str(Path.home()),'data_dir':str(directory.resolve()),'platform':os.name},'exclusions':EXCLUSIONS,'redacted_fields':excluded,'files':files}

def export_studio(path,backup,directory):
    destination=Path(path).resolve()
    if destination.parent==Path(directory).resolve():raise ValueError('Save the backup outside the live studio data directory.')
    validate_backup(backup);atomic_json(destination,backup)

def validate_backup(data):
    if not isinstance(data,dict) or set(data)!={'kind','version','source','exclusions','redacted_fields','files'} or data.get('kind')!=KIND or type(data.get('version')) is not int or data['version']!=1:raise ValueError('Choose a version 1 studio settings backup.')
    source=data['source']
    if not isinstance(source,dict) or set(source)!={'home','data_dir','platform'}:raise ValueError('Missing backup path information.')
    for value in source.values():_string(value,'backup source')
    for name in ('exclusions','redacted_fields'):
        if not isinstance(data[name],list) or any(not isinstance(v,str) for v in data[name]):raise ValueError('Invalid backup exclusions.')
    if sanitize(data['files'])!=data['files']:raise ValueError('Backup contains credential fields or authenticated URLs; use a non-secret export.')
    validate_files(data['files'])
    if data['files']['broadcast-settings.json']['remember']:raise ValueError('Backups cannot enable protected credentials.')
    return data

def prepare_import(path,directory,home_root=None):
    data=validate_backup(read_json(path));files=deepcopy(data['files']);source=data['source'];directory=Path(directory)
    mappings=[(source['data_dir'],str(directory.resolve()))]
    if home_root:mappings.append((source['home'],str(Path(home_root).resolve())))
    def relocate(value):
        normalized=value.replace('\\','/')
        for old,new in mappings:
            old=old.replace('\\','/').rstrip('/')
            # Component boundary prevents remapping /home/jay-other.
            if normalized==old or normalized.startswith(old+'/'):return str(Path(new)/normalized[len(old):].lstrip('/'))
        return value
    _transform_paths(files,relocate)
    # Cache/runtime bundle locations belong to this installation, never another machine.
    files['settings.json']['extraction']['cache_dir']=str(directory/'lyrics-cache')
    files['settings.json']['extraction']['media_library_path']=''
    missing=[]
    _transform_paths(deepcopy(files),lambda p:(missing.append(p) or p) if p and not Path(p).exists() else p)
    validate_files(files)
    return files,sorted(set(missing)),data['source']

def _restore_bytes(directory,before):
    for name,encoded in before.items():
        path=directory/name
        if encoded is None:path.unlink(missing_ok=True)
        else:
            import tempfile
            fd,temp=tempfile.mkstemp(prefix='.studio-rollback-',dir=directory)
            try:
                with os.fdopen(fd,'wb') as f:f.write(base64.b64decode(encoded,validate=True));f.flush();os.fsync(f.fileno())
                os.chmod(temp,0o600);os.replace(temp,path)
            finally:
                if os.path.exists(temp):os.unlink(temp)

def recover_import(directory):
    directory=Path(directory)
    with studio_lock(directory):
        journal=directory/'.studio-import-journal.json'
        if journal.exists():
            data=read_json(journal,limit=LIMIT*8)
            if not set(FILES).issubset(data) or not set(data).issubset(set(FILES)|set(OPTIONAL)):raise ValueError('Invalid studio recovery journal; preserve files for repair.')
            # Decode all rollback data before touching anything.
            for value in data.values():
                if value is not None:base64.b64decode(value,validate=True)
            _restore_bytes(directory,data);journal.unlink()

def import_studio(directory,files):
    directory=Path(directory);validate_files(files)
    with studio_lock(directory):
        recover_import(directory)
        before={name:base64.b64encode((directory/name).read_bytes()).decode() if (directory/name).exists() else None for name in files}
        # Merge extensions and excluded credential fields already on this computer.
        existing={name:read_json(directory/name,{}) for name in files}
        merged={name:merge(existing[name],value) for name,value in files.items()}
        old_rows={_absolute(row['path'],directory):row for row in existing['session.json'].get('tracks',[]) if isinstance(row,dict) and isinstance(row.get('path'),str)}
        merged['session.json']['tracks']=[merge(old_rows.get(_absolute(row['path'],directory),{}),row) for row in files['session.json']['tracks']]
        validate_files(merged)
        journal=directory/'.studio-import-journal.json'
        atomic_json(journal,before)
        try:
            for name,value in merged.items():atomic_json(directory/name,value)
            journal.unlink()
        except BaseException:
            _restore_bytes(directory,before);journal.unlink();raise
        return merged
