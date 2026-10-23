"""Optional local Radeon acceleration; CPU recognizers remain installed."""
from pathlib import Path
import json,os,subprocess,math,re
import threading,time
from copy import deepcopy
try:
    from .lyrics_recognition import recognition_policy
except ImportError:
    from lyrics_recognition import recognition_policy
try:
    import fcntl
except ImportError:
    fcntl=None
from contextlib import contextmanager

def configured_path(name, default=""):
    value=os.environ.get(name,default)
    return Path(value) if value else None

GPU_PYTHON=configured_path('GNB_LYRICS_GPU_PYTHON')
VULKAN=configured_path('GNB_LYRICS_VULKAN_BINARY')
GGML=configured_path('GNB_LYRICS_VULKAN_MODEL')

def capacity():
    """Read-only sysfs check: never initialize a GPU merely to check its load."""
    for load in sorted(Path('/sys/class/drm').glob('card*/device/gpu_busy_percent')):
        try:
            device=load.parent
            if (device/'vendor').read_text().strip()!='0x1002':continue
            total=int((device/'mem_info_vram_total').read_text());used=int((device/'mem_info_vram_used').read_text());busy=int(load.read_text())
            return capacity_values(busy,total,used)
        except (OSError,ValueError):continue
    return {'available':False,'busy':False,'message':'GPU capacity is unavailable; CPU processing remains available.'}

def capacity_values(percent,total,used):
    free=max(0,total-used)
    busy=percent>=40 or free<5*1024**3
    return {'available':True,'busy':busy,'busy_percent':percent,'free_bytes':free,'message':'Waiting for GPU capacity; other rendering jobs have priority.' if busy else 'GPU capacity available.'}

def enabled():
    if os.environ.get('GNB_LYRICS_GPU')!='1' or os.environ.get('GNB_GPU_HELPER_ACTIVE') or os.environ.get('GNB_LYRICS_GPU_BLOCKED'):return False
    state=capacity()
    return bool(state['available'] and (os.environ.get('GNB_LYRICS_PHASE_GPU')=='1' or not state['busy'] or os.environ.get('GNB_LYRICS_GPU_RESERVED')=='1') and VULKAN and VULKAN.is_file() and GGML and GGML.is_file())

@contextmanager
def reservation():
    """Use an explicitly configured advisory lock without changing ownership."""
    old={key:os.environ.get(key) for key in ('GNB_LYRICS_GPU_RESERVED','GNB_LYRICS_GPU_BLOCKED')}
    handle=None
    state=capacity() if os.environ.get('GNB_LYRICS_GPU')=='1' else {'available':False,'busy':False}
    try:
        lock=os.environ.get('GNB_LYRICS_GPU_LOCK')
        if lock and state['available'] and not state['busy']:
            try:
                if fcntl is None:raise OSError('GPU lock is unavailable on this platform; use CPU processing.')
                handle=os.open(lock,os.O_RDWR|getattr(os,'O_NOFOLLOW',0))
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                os.environ['GNB_LYRICS_GPU_RESERVED']='1'
            except OSError:
                state={**state,'busy':True,'message':'Waiting for the shared GPU; other rendering jobs have priority.'}
        if lock and os.environ.get('GNB_LYRICS_GPU_RESERVED')!='1':os.environ['GNB_LYRICS_GPU_BLOCKED']='1'
        yield state
    finally:
        if handle is not None:os.close(handle)
        for key,value in old.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value

_phase_timings=[]

def reset_gpu_timings():
    _phase_timings.clear()

def gpu_timings():
    return [dict(row) for row in _phase_timings]


class GpuCapacityDeferred(ValueError):
    """A requested GPU phase is waiting; do not silently start CPU recognition."""


@contextmanager
def gpu_phase(progress, phase):
    """Own the exclusive GPU lease only while a GPU subprocess is alive."""
    if os.environ.get('GNB_LYRICS_PHASE_GPU')!='1':
        yield
        return
    try: limit=max(0,min(3600,float(os.environ.get('GNB_LYRICS_GPU_WAIT_SECONDS','900'))))
    except ValueError: limit=900
    began=time.monotonic()
    while True:
        with reservation() as state:
            if not state.get('available'):
                raise RuntimeError('GPU capacity is unavailable; using CPU processing.')
            if not state.get('busy'):
                progress(phase,0,'Using the shared GPU for this phase; CPU stages do not reserve it.')
                acquired=time.monotonic()
                try:
                    yield
                finally:
                    _phase_timings.append({'phase':phase,'wait_seconds':round(acquired-began,3),'lease_seconds':round(time.monotonic()-acquired,3)})
                    del _phase_timings[:-128]
                return
            if os.environ.get('GNB_LYRICS_GPU_WAIT','1')!='1':
                progress('preparing',0,'GPU is busy; CPU fallback was explicitly selected.')
                raise RuntimeError('GPU busy; CPU fallback explicitly selected.')
            if time.monotonic()-began>=limit:
                raise GpuCapacityDeferred('Waiting for the shared GPU. Existing lyrics are unchanged; the job can retry.')
            progress('waiting',0,state.get('message','Waiting for the shared GPU.'))
        time.sleep(min(2,max(.01,limit-(time.monotonic()-began))))


def helper(operation,source,target,work,progress):
    with gpu_phase(progress,'aligning' if operation=='align' else 'separating'):
        return _helper_unlocked(operation,source,target,work,progress)


def _helper_unlocked(operation,source,target,work,progress):
    script=Path(__file__).with_name('lyrics_gpu_helper.py')
    if not GPU_PYTHON or not GPU_PYTHON.is_file() or not script.is_file():raise RuntimeError('Configure the GPU helper Python before using GPU alignment.')
    env=os.environ.copy();env['GNB_GPU_HELPER_ACTIVE']='1'
    log=work/('gpu-'+operation+'.log')
    with log.open('w') as errors:
        process=subprocess.Popen([str(GPU_PYTHON),str(script),operation,str(source),str(target),str(work)],stdout=subprocess.PIPE,stderr=errors,text=True,env=env,start_new_session=(os.name!='nt'))
        watchdog=threading.Timer(600,process.kill);watchdog.daemon=True;watchdog.start()
        try:
            for line in process.stdout:
                try:
                    d=json.loads(line);progress(d['phase'],d['fraction'],d['message'])
                except (KeyError,TypeError,ValueError):continue
            code=process.wait(timeout=15)
        except BaseException:process.kill();process.wait();raise
        finally:watchdog.cancel()
    if code or not target.is_file():raise RuntimeError('GPU helper failed: '+log.read_text()[-500:])

def parse_words(data,offset=0):
    """Use the recognizer's measured word intervals; never interpolate zero durations."""
    words=[]
    for cue in data.get('transcription',[]):
        text=str(cue.get('text','')).strip()
        if not text or text.startswith('[_') or re.fullmatch(r'\[[^\]]*\]',text):continue
        if re.fullmatch(r'[^\w]+',text):
            if words:words[-1]['text']+=text
            continue
        bounds=cue.get('offsets',{});a=offset+float(bounds.get('from',0))/1000;b=offset+float(bounds.get('to',0))/1000
        scores=[float(t['p']) for t in cue.get('tokens',[]) if not str(t.get('text','')).startswith('[_') and isinstance(t.get('p'),(int,float))]
        if not scores or not math.isfinite(a+b):continue
        words.append({'text':text,'start':round(a,3),'end':round(b,3),'score':sum(scores)/len(scores)})
    return words

def realign(audio,sr,words,work,progress,return_backend=False):
    import soundfile as sf
    source=work/'align-audio.wav';target=work/'aligned-words.json'
    sf.write(source,audio,sr,subtype='FLOAT')
    (work/'align-request.json').write_text(json.dumps({'lines':[' '.join(w['text'] for w in words)]}))
    aligned=None;backend='cpu'
    if enabled():
        try:
            helper('align',source,target,work,progress);aligned=json.loads(target.read_text());backend='radeon-rocm'
        except GpuCapacityDeferred:
            raise
        except Exception as error:print('GPU alignment fallback:',type(error).__name__,str(error)[-500:],flush=True)
    if aligned is None:
        from lyrics_pipeline import align_reference
        aligned=align_reference(audio,sr,[' '.join(w['text'] for w in words)],progress)
    if len(aligned)!=len(words):raise ValueError('Alignment text coverage changed')
    result=[]
    for old,new in zip(words,aligned):
        if old['text']!=new['text']:raise ValueError('Alignment text changed')
        result.append({**old,'start':new['start'],'end':new['end'],'alignment_score':new.get('score',0)})
    return (result,backend) if return_backend else result

def recognize_windows(audio,sr,windows,work,progress,evidence=None,independent=False):
    import soundfile as sf
    if not VULKAN or not VULKAN.is_file() or not GGML or not GGML.is_file():raise RuntimeError('Configure the Vulkan recognizer and model paths before using GPU recognition.')
    result={}
    policy=recognition_policy('large-v3',independent=independent)
    if evidence is not None:evidence.update(backend='whisper.cpp',engine='large-v3',settings=policy,windows=[])
    for index,(key,start,end) in enumerate(windows):
        clip=work/f'vulkan-{index}.wav';output=work/f'vulkan-{index}'
        sf.write(clip,audio[round(start*sr):round(end*sr)],sr,subtype='PCM_16')
        args=[str(VULKAN),'-m',str(GGML),'-f',str(clip),'-t','4','-bs','3','-bo','3','-l',policy['language'],'-mc','0','-ojf','-of',str(output),'-ml','1','-sow','-dtw','large.v3','-nfa']
        if policy['prompt']:args+=['--prompt',policy['prompt']]
        try:
            with gpu_phase(progress,'reviewing' if len(windows)>1 else 'transcribing'):
                p=subprocess.run(args,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=max(120,min(900,int((end-start)*2+60))))
            if p.returncode:raise RuntimeError('Vulkan recognition failed: '+p.stderr.decode(errors='replace')[-350:])
            raw=json.loads(output.with_suffix('.json').read_text())
            result[key]=parse_words(raw,start)
            if evidence is not None:
                evidence['windows'].append({'key':key,'start':start,'end':end,'raw':deepcopy(raw)})
                evidence['language']=(raw.get('result') or {}).get('language',policy['language'])
        finally:
            clip.unlink(missing_ok=True);output.with_suffix('.json').unlink(missing_ok=True)
        progress('reviewing' if len(windows)>1 else 'transcribing',(index+1)/len(windows),'Recognizing sung words with Radeon-accelerated Whisper large-v3.')
    return result
