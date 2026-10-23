"""Offline second-model review: repair only corroborated words and timing."""
from copy import deepcopy
from difflib import SequenceMatcher
import math,re,time,os,bisect
try:
    from .lyrics_recognition import recognition_policy, cache_policy, segment_evidence
except ImportError:
    from lyrics_recognition import recognition_policy, cache_policy, segment_evidence
VERSION='automatic-review-3'

def token(text):return re.sub(r'[^\w]','',str(text).casefold())
def flatten(data):return [w for cue in data.get('segments',[]) for w in cue.get('words',[])]
def confidence(words):return sum(float(w.get('score',0)) for w in words)/len(words) if words else 0

def pairing(primary,secondary,drift=4):
    # Local time bounds prevent a repeated chorus elsewhere in the song from
    # stealing the global text match. Every secondary word is used at most once.
    matches={};centers=[(w['start']+w['end'])/2 for w in secondary];last=-1
    for i,word in enumerate(primary):
        center=(word['start']+word['end'])/2
        lo=max(last+1,bisect.bisect_left(centers,center-drift));hi=bisect.bisect_right(centers,center+drift)
        choices=[j for j in range(lo,hi) if token(word['text'])==token(secondary[j]['text'])]
        if choices:
            j=min(choices,key=lambda j:(abs(centers[j]-center),j));matches[i]=secondary[j];last=j
    return matches

def suspect_cue(data,index):
    try:
        from .lyrics_quality import artifact_suspected
    except ImportError:
        from lyrics_quality import artifact_suspected
    cue=data['segments'][index];words=cue.get('words',[])
    return bool(cue.get('needs_review') or artifact_suspected(data,index) or not valid(words)
        or (index and cue['start']<data['segments'][index-1]['end']-.1)
        or (index+1<len(data['segments']) and cue['end']>data['segments'][index+1]['start']+.1))

def within(words,start,end):return [w for w in words if start<=(w['start']+w['end'])/2<end]
def valid(words):
    previous=-1
    for word in words:
        a,b=word['start'],word['end']
        if not all(math.isfinite(v) for v in (a,b)) or a<previous-.05 or b<=a or b-a>8:return False
        previous=b
    return bool(words)

def gaps(data):
    duration=float(data['duration']);previous=0;result=[]
    for cue in data['segments']:
        if cue['start']-previous>=20:result.append((previous,float(cue['start'])))
        previous=max(previous,float(cue['end']))
    if duration-previous>=20:result.append((previous,duration))
    return result

def reconcile(original,secondary,rechecks,activity,preserve_text=False):
    """Pure review core; ASR evidence is injected so changes have testable grounds."""
    data=deepcopy(original);repairs=[];held=[];all_primary=flatten(data);matched=pairing(all_primary,secondary);cursor=0;new_cues=[]
    for index,cue in enumerate(data['segments']):
        words=cue.get('words',[]);indices=list(range(cursor,cursor+len(words)));cursor+=len(words)
        suspect=suspect_cue(data,index)
        if not suspect:new_cues.append(cue);continue
        confirmed=[matched.get(i) for i in indices]
        if words and all(w and w.get('score',0)>=.55 for w in confirmed) and confidence(confirmed)>=.72 and valid(confirmed):
            replacement=[{**old,'start':round(new['start'],3),'end':round(new['end'],3),'score':max(float(old.get('score',0)),float(new['score']))} for old,new in zip(words,confirmed)]
            new_cues.append({**cue,'start':replacement[0]['start'],'end':replacement[-1]['end'],'words':replacement,'needs_review':False})
            repairs.append({'kind':'timing-confirmed','at':cue['start'],'text':cue['text']});continue
        # Repair the uncertain words rather than demanding that two models
        # reproduce an entire nine-word line. Strong original words are kept;
        # every weak/invalid word still requires independent same-word evidence.
        candidate=[];supported=0;unresolved=False
        for old,other in zip(words,confirmed):
            weak=old.get('score',0)<.35 or old['end']<=old['start']
            strong_other=other and other.get('score',0)>=.72 and other['end']>other['start']
            if strong_other:
                candidate.append({**old,'start':round(other['start'],3),'end':round(other['end'],3),'score':max(float(old.get('score',0)),float(other['score']))});supported+=1
            else:
                candidate.append(dict(old));unresolved=unresolved or weak
        if (candidate and not unresolved and supported>=max(1,math.ceil(len(words)*.8))
            and confidence(candidate)>=.72 and valid(candidate)):
            new_cues.append({**cue,'start':candidate[0]['start'],'end':candidate[-1]['end'],'words':candidate,'needs_review':False})
            repairs.append({'kind':'wordwise-audio-confirmation','at':cue['start'],'text':cue['text'],'confirmed_words':supported,'total_words':len(words)});continue
        check=rechecks.get(index,[])
        before=data['segments'][index-1]['end'] if index else 0
        after=data['segments'][index+1]['start'] if index+1<len(data['segments']) else data['duration']
        left=max(before,float(cue['start'])-2);right=min(after,float(cue['end'])+2)
        second=within(secondary,left,right);third=within(check,left,right)
        same=bool(second and third) and [token(w['text']) for w in second]==[token(w['text']) for w in third]
        if same and confidence(second)>=.72 and confidence(third)>=.72 and all(w.get('score',0)>=.35 for w in second+third) and valid(third):
            new_text=' '.join(w['text'] for w in third)
            if not preserve_text or token(new_text)==token(cue['text']):
                replacement=[dict(w) for w in third]
                new_cues.append({'start':replacement[0]['start'],'end':replacement[-1]['end'],'text':cue['text'] if preserve_text else new_text,'words':replacement,'needs_review':False})
                repairs.append({'kind':'two-model-correction','at':cue['start'],'before':cue['text'],'after':new_text});continue
        # Both fresh recognizers must reject an uncertain phrase, and the vocal
        # stem must be quiet, before an ASR artifact is removed. Never edit a
        # person's lyric sheet or correction automatically.
        if (not preserve_text and index in rechecks and not second and not third and words
            and min(w.get('score',1) for w in words)<.15 and activity(left,right)<.2):
            repairs.append({'kind':'unsupported-phrase-removed','at':cue['start'],'text':cue['text'],'evidence':'Both fresh ASR passes found no words in a quiet vocal interval.'});continue
        new_cues.append(cue);held.append({'at':cue['start'],'reason':'The models did not agree sufficiently on this passage.'})
    # Recover a missing verse only from agreement between the full secondary
    # transcription and a fresh large-model pass of that exact audio interval.
    # No language model invents words to bridge the gap.
    from lyrics_pipeline import make_cues
    for index,(start,end) in enumerate(gaps(original)):
        second=within(secondary,start+.15,end-.15);third=within(rechecks.get('gap-'+str(index),[]),start+.15,end-.15)
        same=bool(second and third) and [token(w['text']) for w in second]==[token(w['text']) for w in third]
        if (not preserve_text and len(third)>=3 and same and valid(third) and confidence(second)>=.75 and confidence(third)>=.75
            and all(w.get('score',0)>=.35 for w in second+third)
            and third[0]['start']>=start and third[-1]['end']<=end and activity(third[0]['start'],third[-1]['end'])>=.2):
            recovered,flags=make_cues(third,data['duration'])
            if not flags:
                new_cues.extend(recovered);repairs.append({'kind':'missing-vocal-recovered','at':third[0]['start'],'text':' '.join(w['text'] for w in third),'evidence':'Two fresh audio recognition passes agree on these previously missing words.'})
    data['segments']=sorted(new_cues,key=lambda c:c['start']);new_cues=data['segments']
    verified_gaps=[]
    # A gap is only cleared when both full-song ASR passes found no words and
    # the separated vocal stem is quiet. Actual instrumental solos need no edit.
    for start,end in gaps(data):
        fresh=rechecks.get('confirm-gap-'+str(round(start,3)))
        if not within(secondary,start+1,end-1) and (activity(start+1,end-1)<.2 or (fresh is not None and not within(fresh,start+1,end-1))):
            verified_gaps.append({'start':start,'end':end,'verified':True,'method':'Two independent recognizers plus either quiet vocals or a fresh bounded large-model check found no lyric words.'})
    remaining_suspect=any(c.get('needs_review') or not valid(c.get('words',[])) for c in new_cues)
    unverified_gaps=[(a,b) for a,b in gaps(data) if a>0 and b<data['duration'] and not any(g['start']<=a+.1 and g['end']>=b-.1 for g in verified_gaps)]
    warnings=[]
    for warning in data.get('warnings',[]):
        if not remaining_suspect and ('timing review' in warning or 'uncertain recognition' in warning):continue
        if not unverified_gaps and 'long gap remains' in warning:continue
        warnings.append(warning)
    data['warnings']=warnings
    words=flatten(data);scores=[float(w['score']) for w in words if isinstance(w.get('score'),(int,float))]
    metrics=dict(data.get('metrics') or {});metrics.update(words=len(words),lines=len(new_cues),timed_seconds=round(sum(c['end']-c['start'] for c in new_cues),2))
    if scores:metrics.update(recognition_confidence=round(sum(scores)/len(scores),4),low_confidence_fraction=round(sum(s<.35 for s in scores)/len(scores),4))
    data['metrics']=metrics
    data['machine_review']={'version':VERSION,'checked_at':int(time.time()),'recognizers':list(dict.fromkeys([(original.get('pipeline') or {}).get('recognizer','large-v3'),'medium.en']+(['large-v3'] if rechecks else []))),'preserved_human_text':preserve_text,'repairs':repairs[:64],'held_passages':held[:64],'confirmed_gaps':verified_gaps,'notice':'Automated comparison, not a human review. Published corrections are never replaced.'}
    return data

def recognize(audio,sr,windows,engine,progress,evidence=None):
    from lyrics_acceleration import enabled,recognize_windows,GpuCapacityDeferred
    if engine=='large-v3' and enabled():
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix='gnb-review-vulkan-') as folder:
            try:return recognize_windows(audio,sr,windows,Path(folder),progress,evidence=evidence,independent=True)
            except GpuCapacityDeferred:
                raise
            except Exception as error:print('GPU review fallback:',type(error).__name__,str(error)[:200],flush=True)
    from faster_whisper import WhisperModel
    from lyrics_pipeline import model_path,release_memory
    model=None
    selected_model_path=str(model_path(engine,review=engine=='medium.en'))
    # Load the CPU model lazily; a cached full-song evidence pass needs none.
    # The timestamp/probability evidence remains private and source-specific.
    from lyrics_cache import evidence_path,read_evidence,write_evidence
    import lyrics_cache
    policy=recognition_policy(engine,independent=True)
    if evidence is not None:evidence.update(backend='faster-whisper',engine=engine,settings=policy,windows=[])
    model_factory=lambda:WhisperModel(selected_model_path,device='cpu',compute_type='int8',cpu_threads=int(os.environ.get('GNB_LYRICS_CPU_THREADS','2')),num_workers=1,local_files_only=True)
    result={}
    for n,(key,start,end) in enumerate(windows):
        clip=audio[round(start*sr):round(end*sr)]
        cache=evidence_path(clip,sr,engine+'-beam3-policy-v4:'+cache_policy(policy)+':'+str(start))
        cached=read_evidence(cache,end)
        if cached is not None:
            if evidence is not None:
                reader=getattr(lyrics_cache,'read_evidence_metadata',lambda p:None)
                evidence['windows'].append({'key':key,'start':start,'end':end,'cached':True,
                                            'recognition':reader(cache),'words':deepcopy(cached)})
            result[key]=cached;progress('reviewing',(n+1)/len(windows),'Reusing cached independent recognition evidence.');continue
        if model is None:model=model_factory()
        segments,info=model.transcribe(clip,language=None if policy['language']=='auto' else policy['language'],task=policy['task'],word_timestamps=True,condition_on_previous_text=False,beam_size=3,vad_filter=False,initial_prompt=None,no_speech_threshold=.6,temperature=(0,.2))
        words=[]
        raw={'segments':[],'language':getattr(info,'language',policy['language'])}
        for cue in segments:
            raw['segments'].append(segment_evidence(cue,offset=start))
            for w in cue.words or []:
                if w.word.strip():words.append({'text':w.word.strip(),'start':round(start+w.start,3),'end':round(start+w.end,3),'score':float(w.probability)})
        write_evidence(cache,words,raw)
        if evidence is not None:evidence['windows'].append({'key':key,'start':start,'end':end,'cached':False,'recognition':raw})
        result[key]=words;progress('reviewing',(n+1)/len(windows),f'Checking the vocal with {engine}; no title prompt is used.')
    del model;release_memory();return result

def review(source,work,data,progress,preserve_text=False):
    import numpy as np
    import soundfile as sf
    from lyrics_pipeline import decode,separate_vocals
    from lyrics_quality import assess
    if assess(data)['status']=='pass':return data
    from lyrics_cache import restore,store
    wav=work/'recognition.wav'
    if not wav.is_file() or not (data.get('pipeline') or {}).get('vocals_separated'):
        if restore(source,wav):progress('separating',1,'Reusing the verified private vocal cache.')
        else:
            vocals=work/'review-vocals.wav';separate_vocals(source,vocals,work,progress);wav=work/'review-recognition.wav';decode(vocals,wav);store(source,wav)
    audio,sr=sf.read(wav,dtype='float32');duration=len(audio)/sr
    window=max(1,int(sr*.25));padded=np.pad(audio,(0,(-len(audio))%window));energy=np.sqrt(np.mean(padded.reshape(-1,window)**2,axis=1));reference=max(float(np.quantile(energy,.95)),1e-7)
    def activity(a,b):
        part=energy[max(0,int(a*4)):min(len(energy),max(1,int(b*4)))]
        return float(np.mean(part>reference*.12)) if len(part) else 0
    primary=(data.get('recognition_evidence') or {}).get('primary') or {}
    language=primary.get('language',os.environ.get('GNB_LYRICS_LANGUAGE','en'))
    if language not in {'en','English','english'}:
        data.setdefault('warnings',[]).append('English-only independent review was skipped for this detected language; review the saved words manually.')
        return data
    review_evidence={'secondary':{},'rechecks':{}}
    data.setdefault('recognition_evidence',{})['independent_review']=review_evidence
    secondary=recognize(audio,sr,[('full',0,duration)],'medium.en',progress,evidence=review_evidence['secondary'])['full']
    matches=pairing(flatten(data),secondary);windows=[];cursor=0
    for i,cue in enumerate(data['segments']):
        words=cue.get('words',[]);indices=range(cursor,cursor+len(words));cursor+=len(words)
        if suspect_cue(data,i) and not all(j in matches and matches[j].get('score',0)>=.55 for j in indices):
            windows.append((i,max(0,cue['start']-5),min(duration,cue['end']+5)))
    # A long empty interval with confident secondary vocals gets its own fresh
    # check. Gaps are disjoint; at most one extra song-length audio pass overall.
    gap_windows=[]
    if not preserve_text:
        for index,(start,end) in enumerate(gaps(data)):
            missing=within(secondary,start+.15,end-.15)
            if len(missing)>=3 and confidence(missing)>=.75:
                gap_windows.append(('gap-'+str(index),max(0,start-1),min(duration,end+1)))
    # Bounded per-recording second pass. Difficult cases remain saved, not lost.
    confirmations=[('confirm-gap-'+str(round(start,3)),start,end) for start,end in gaps(data)
        if not within(secondary,start+1,end-1) and activity(start+1,end-1)>=.2]
    check_windows=windows[:24]+gap_windows[:8]+confirmations[:8]
    rechecks=recognize(audio,sr,check_windows,'large-v3',progress,evidence=review_evidence['rechecks']) if check_windows else {}
    reviewed=reconcile(data,secondary,rechecks,activity,preserve_text)
    reviewed.setdefault('recognition_evidence',{})['independent_review']=review_evidence
    reviewed.setdefault('pipeline',{})['reviewer']=VERSION
    return reviewed
