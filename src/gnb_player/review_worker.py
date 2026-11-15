"""Bounded, cancellable review-only model controller. Never approves or publishes."""
from pathlib import Path
import argparse, contextlib, fcntl, hashlib, json, os, signal, subprocess, sys, time, wave
from review_capture import atomic_json
from review_repetition import inspect_regions, held_indices
from alignment_contract import alignment_regions, require_supplied_text_coverage
from recognition_policy import audio_only_policy
import re

def sung_text(text):
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", " ", text, flags=re.S)).strip()

class Cancelled(Exception): pass

def emit(kind, **fields):print(json.dumps({'type':kind,**fields}),flush=True)

def stop_child(child):
    if child is None or child.poll() is not None:return
    if os.name!='nt':
        with contextlib.suppress(ProcessLookupError):os.killpg(child.pid,signal.SIGTERM)
    else:child.terminate()
    try:child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name!='nt':
            with contextlib.suppress(ProcessLookupError):os.killpg(child.pid,signal.SIGKILL)
        else:child.kill()
        child.wait(timeout=3)

def run(request_path):
    path=Path(request_path);request=json.loads(path.read_text());out=path.parent;source=Path(request['source']);config=request['config']
    started=time.monotonic();deadline=started+min(480,int(config.get('timeout_seconds',480)));child=None;locks=[]
    receipt={'status':'precheck','calls':[],'threads':'runtime-default','backend':('AMD ROCm requested' if config.get('review_device')=='rocm' else 'CPU FP32'),'source':str(source),'source_sha256':request['source_sha256'],'output_approved':False}
    def digest(p):
        with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
    def interrupted(sig,frame):raise Cancelled('Cancelled by operator; drafts and approved lyrics retained.')
    signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
    if os.name!='nt':
        with contextlib.suppress(PermissionError):os.setsid()
    def call(stage,command,timeout,model=True):
        nonlocal child
        cap=min(timeout,deadline-time.monotonic())
        if cap<=0:raise TimeoutError('Eight-minute review job limit reached.')
        record={'stage':stage,'model_call':model,'limit_seconds':cap};receipt['calls'].append(record);atomic_json(out/'receipt.json',receipt)
        label={'decode':'Reading audio','heart':'Extracting lyrics','qwen-asr':'Extracting lyrics','qwen-align':'Aligning lyrics automatically','align':'Aligning lyrics automatically'}.get(stage,stage)
        emit('progress',fraction=.1 if stage=='decode' else .2 if stage in ('heart','qwen-asr') else .65,message=label+' · '+('AMD ROCm requested' if config.get('review_device')=='rocm' else 'CPU FP32')+'; private result')
        begin=time.monotonic()
        with (out/(stage+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,start_new_session=True,
                env={**os.environ,'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','PYTHONDONTWRITEBYTECODE':'1',**({'LD_LIBRARY_PATH':request['media_library_path']} if stage=='decode' and request.get('media_library_path') else {})})
            record['pid']=child.pid;peak=0
            try:
                while child.poll() is None:
                    if time.monotonic()-begin>=cap:raise TimeoutError(stage+' reached its limit; no automatic retry.')
                    if Path('/proc').is_dir():
                        with contextlib.suppress(FileNotFoundError):
                            for line in Path(f'/proc/{child.pid}/status').read_text().splitlines():
                                if line.startswith('VmHWM:'):peak=max(peak,int(line.split()[1]))
                    time.sleep(.1)
                if child.returncode:raise RuntimeError(stage+' failed. Raw evidence/log retained in '+str(out))
            finally:
                stop_child(child);record.update(elapsed_seconds=time.monotonic()-begin,exit_code=child.returncode,peak_rss_kib=peak,stopped=True);child=None;atomic_json(out/'receipt.json',receipt)
    try:
        if config.get('review_device')=='rocm' and config.get('backend')!='qwen':raise ValueError('Optional GPU review supports Qwen only; select CPU for Heart/CTC.')
        receipt['recognition_policy']=audio_only_policy(request)
        if os.geteuid()==0:raise ValueError('Model review must run as an ordinary user.')
        if request.get('instrumental'):raise ValueError('Known instrumentals bypass model generation; save the designation in review.')
        if request['mode'] not in ('extract','align'):raise ValueError('Unknown review mode.')
        if digest(source)!=request['source_sha256']:raise ValueError('Recording changed before review.')
        if source.stat().st_size>2*1024**3:raise ValueError('Recording exceeds2GiB limit.')
        for lockpath,flags in [(Path(request['local_lock']),os.O_RDWR|os.O_CREAT)]:
            fd=os.open(lockpath,flags|os.O_NOFOLLOW,0o600);locks.append(fd)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise RuntimeError('Another model job holds the coordination lock. Try after it finishes.')
        call('decode',['ffmpeg','-v','error','-nostdin','-threads',str(os.cpu_count() or 1),'-protocol_whitelist','file,pipe','-i',str(source),'-t','901','-vn','-sn','-ar','16000','-ac','1',str(out/'audio.wav')],30,False)
        with wave.open(str(out/'audio.wav')) as w:duration=w.getnframes()/w.getframerate()
        if not 1<=duration<=900:raise ValueError('Review one recording of at most15minutes; audio was not silently truncated.')
        request['wav']=str(out/'audio.wav');request['decoded_duration']=duration
        if request['mode']=='align':
            if request.get('reference_prepared'):
                from supplied_text_contract import prepared_text
                request['reference_text']=prepared_text(request)
            else:request['reference_text']=sung_text(request.get('reference_text',''))
            if not request['reference_text']:raise ValueError('Supply sung words before alignment.')
        atomic_json(path,request)
        if config.get('backend')=='qwen':
            from review_qwen_worker import run_qwen
            candidate=run_qwen(request,out,receipt,call,emit)
            if digest(source)!=request['source_sha256']:raise ValueError('Source changed during review; candidate not committed.')
            atomic_json(out/'candidate.json',candidate,exclusive=True);receipt['status']='candidate_ready'
            emit('done',output=str(out/'candidate.json'))
            return 0
        cache=out/'torch-cache/hub/checkpoints';cache.mkdir(parents=True)
        ctc=Path(config['ctc_model']);(cache/'wav2vec2_fairseq_base_ls960_asr_ls960.pth').symlink_to(ctc.resolve())
        receipt['ctc_sha256']=digest(ctc);receipt['heart_model']=config['heart_model'];receipt['heart_revision']=Path(config['heart_model']).name
        held=[]
        if request['mode']=='extract':
            receipt['heart_weights_sha256']=digest(Path(config['heart_model'])/'model.safetensors')
            call('heart',[config['heart_python'],'-u',str(Path(__file__).with_name('review_backend.py')),'--stage','heart','--request',str(path)],max(1,deadline-time.monotonic()-45))
            h=json.loads((out/'heart.json').read_text())
            if not h['text'].strip():raise ValueError('No candidate text. Empty output does not establish an instrumental.')
            regions=h['regions'];held=inspect_regions(regions)
        else:
            regions=alignment_regions(request,duration)
            held=inspect_regions(regions)
            if request.get('repeat_override'):
                for item in held:item.update(status='operator_requested_alignment',decision='Operator explicitly confirmed repeated supplied text for alignment; candidate still requires listening approval.')
        atomic_json(out/'repetition-review.json',{'held_passages':held,'note':'Text-only diagnostic, not singing detection.'},exclusive=True)
        blocked=held_indices(held)
        request['held_passages']=held;atomic_json(path,request)
        if any(r['text'].strip() and i not in blocked for i,r in enumerate(regions)):
            call('align',[config['align_python'],'-u',str(Path(__file__).with_name('review_backend.py')),'--stage','align','--request',str(path)],min(120,max(1,deadline-time.monotonic())))
            a=json.loads((out/'align.json').read_text());assert a['completed']
        else:
            a={'segments':[],'duration':duration,'unplaced':[],'timing':'No alignment: all proposed text held for listening review.'}
            emit('progress',message='Repetitive text held without alignment. Listen and review the raw passage; this does not identify an instrumental.')
        if request['mode']=='align':
            a['alignment_evidence']=require_supplied_text_coverage(a,regions,blocked)
        if digest(source)!=request['source_sha256']:raise ValueError('Source changed during review; candidate not committed.')
        candidate={'title':request['title'],'duration':a['duration'],'source_sha256':request['source_sha256'],'segments':a['segments'],
            'quality':{'status':'review','review_windows':[w for c in a['segments'] for w in c['words'] if w.get('flags')]},
            'reviewed':False,'review':{'status':'unreviewed'},'warnings':['Text and timing need listening review; instrumental hallucinations remain possible.'],
            'held_passages':held,
            **({'supplied_sheet':request['supplied_sheet']} if request.get('reference_prepared') else {}),
            **({'vocabulary_review':request['vocabulary_review']}
               if request['mode']=='align' and request.get('alignment_source')=='draft' and request.get('vocabulary_review') else {}),
            'provenance':{'mode':request['mode'],'raw_evidence':str(out),'source_offset_seconds':0,'model_revision':receipt['heart_revision'],
                          'ctc_sha256':receipt['ctc_sha256'],'threads':'runtime-default','device':'CPU FP32','embedded':request.get('embedded'),
                          'recognition_policy':receipt['recognition_policy'],
                          'reference_original':request.get('reference_original',''),'alignment_source':request.get('alignment_source'),
                          'supplied_sheet_sha256':request.get('supplied_sheet_sha256'),'alignment_route':request.get('alignment_route'),
                          'replacement_scope':request.get('replacement_scope'),'replaces_revision':request.get('replaces_revision'),
                          'alignment_windows':request.get('alignment_windows'),
                          'alignment_evidence':a.get('alignment_evidence'),
                          'unplaced':a['unplaced'],'timing':a['timing']}}
        atomic_json(out/'candidate.json',candidate,exclusive=True);receipt['status']='candidate_ready'
        emit('done',output=str(out/'candidate.json'))
    except Cancelled as e:receipt['status']='cancelled';emit('cancelled',message=str(e))
    except BaseException as e:receipt.update(status='failed',error=str(e));emit('error',message=str(e))
    finally:
        stop_child(child)
        for fd in locks:os.close(fd)
        receipt.update(elapsed_seconds=time.monotonic()-started,workers_stopped=True,locks_released=True)
        atomic_json(out/'receipt.json',receipt)
    return 0 if receipt['status']=='candidate_ready' else 2

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--request',required=True);a=p.parse_args();raise SystemExit(run(a.request))
