"""Qwen review job orchestration, under the existing controller locks and limits."""
from pathlib import Path
import json
from review_capture import atomic_json
from review_repetition import inspect_regions, held_indices
from alignment_contract import alignment_regions


def recognition_holds(regions):
    """Inspect raw pre-parser text as well as cleaned output; never erase either."""
    holds=[]
    for i,region in enumerate(regions):
        raw={**region,'text':region.get('raw_text',region['text'])}
        detected=inspect_regions([raw]) or inspect_regions([region])
        for item in detected:
            item.update(region=i,cleaned_text=region['text'],raw_text=raw['text'])
            holds.append(item)
        flags=region.get('flags',[])
        if any('limit' in str(flag).lower() or 'truncat' in str(flag).lower() for flag in flags):
            holds.append({'region':i,'start':region['start'],'end':region['end'],'text':region['text'],
                'raw_text':raw['text'],'status':'held','reason':'Generation reached its token limit; missing text is possible.',
                'timing':'Audio section only; no word timings.','detector':'decoder_termination'})
    return holds


def _model_evidence(result):
    return {k:result.get(k) for k in ('model_path','model_sha256','model_revision','qwen_asr_version','settings','device','dtype','threads','backend','hip','gpu_name','gpu_arch','torch','transformers')}


def _candidate(request,out,receipt,regions,held,result=None,evidence=None):
    result=result or {};segments=result.get('segments',[])
    aligned=bool(result.get('alignment_evidence')) or bool(evidence)
    return {'title':request['title'],'duration':request['decoded_duration'],'source_sha256':request['source_sha256'],
        'segments':segments,'quality':{'status':'review','review_windows':[w for c in segments for w in c.get('words',[]) if w.get('flags')]},
        'reviewed':False,'review':{'status':'unreviewed'},'held_passages':held,
        **({'supplied_sheet':request['supplied_sheet']} if request.get('reference_prepared') else {}),
        'warnings':['Automatic text and timing are predictions. Flagged exceptions and raw evidence are retained; nothing is published.'],
        'provenance':{'engine':'qwen','mode':request['mode'],'raw_evidence':str(out),'device':(receipt.get('alignment_model_evidence') or receipt.get('recognition_model_evidence') or {}).get('device','unverified'),'threads':'runtime-default',
            'source_offset_seconds':0,'recognition_policy':receipt['recognition_policy'],'embedded':request.get('embedded'),
            'reference_original':request.get('reference_original',''),'alignment_source':request.get('alignment_source'),
            'supplied_sheet_sha256':request.get('supplied_sheet_sha256'),'alignment_route':request.get('alignment_route'),
            'replacement_scope':request.get('replacement_scope'),'replaces_revision':request.get('replaces_revision'),
            'model_evidence':receipt.get('model_evidence'),
            'recognition_model_evidence':receipt.get('recognition_model_evidence'),
            'recognition_evidence_directory':str(out) if request['mode']=='extract' else request.get('recognition_evidence_directory'),
            'alignment_model_evidence':receipt.get('alignment_model_evidence'),
            'alignment_evidence':evidence,'unplaced':result.get('unplaced',[]),
            'timing':result.get('timing','No measured word timing; extraction checkpoint retained for alignment retry.')},
        **({'recognition_regions':request.get('recognition_regions',regions),'transcription_only':not aligned} if request['mode']=='extract' or request.get('recognition_regions') else {}),
        **({'alignment_windows':regions} if aligned else {}),
        **({'vocabulary_review':request['vocabulary_review']} if request.get('vocabulary_review') else {})}


def _automatic_groups(segments):
    """Compatibility name: retain positive native words; never collapse a cue."""
    from review_partial import preserve_native_support
    return preserve_native_support(segments)


def run_qwen(request,out,receipt,call,emit=None):
    """One lock/deadline: audio-only ASR → checks → its own text alignment."""
    from copy import deepcopy
    from qwen_review_contract import validate_review_alignment
    config=request['config'];mode=request['mode'];duration=request['decoded_duration']
    backend=Path(__file__).with_name('qwen_review_backend.py');held=[];regions=[]
    receipt['engine']='qwen'
    if request.get('recognition_model_evidence'):receipt['recognition_model_evidence']=deepcopy(request['recognition_model_evidence'])
    if mode=='extract':
        # No supplied text, metadata, vocabulary or timing is passed to ASR.
        call('qwen-asr',[config['align_python'],'-u',str(backend),'--stage','qwen-asr','--request',str(out/'request.json')],480)
        asr=json.loads((out/'qwen-asr.json').read_text());regions=asr['regions'];held=recognition_holds(regions)
        if not any(r.get('text','').strip() for r in regions):
            raise ValueError('No candidate words. Empty recognition is not an instrumental diagnosis. Raw evidence retained.')
        receipt['backend']=asr.get('backend','unverified')
        receipt['recognition_model_evidence']=_model_evidence(asr)
        receipt['model_evidence']=receipt['recognition_model_evidence']
        checkpoint=_candidate(request,out,receipt,regions,held)
        checkpoint_path=out/'recognition-candidate.json'
        atomic_json(checkpoint_path,checkpoint,exclusive=True)
        if emit:emit('progress',fraction=.55,checkpoint=str(checkpoint_path),message='Words extracted and saved. Aligning automatically…')
        # Empty sections remain in raw ASR, but cannot be forced into words.
        mapping={old:new for new,old in enumerate(i for i,r in enumerate(regions) if r.get('text','').strip())}
        windows=[]
        for old,r in enumerate(regions):
            if old not in mapping:continue
            windows.append({'start':r['start'],'end':r['end'],'text':r['text'],
                'evidence':{'kind':'detector_candidate','source':'Automatic contiguous ASR processing window, not a verified vocal region.'}})
        held=[{**h,'recognition_region':h['region'],'region':mapping[h['region']]} for h in held if h['region'] in mapping]
        alignment_request={**deepcopy(request),'reference_text':'\n'.join(r['text'] for r in windows),
            'reference_original':'','alignment_windows':windows,'alignment_source':'audio_only_asr_output',
            'held_passages':held,'recognition_regions':regions}
    else:
        windows=alignment_regions(request,duration)
        held=deepcopy(request.get('held_passages',[]))+inspect_regions(windows)
        if request.get('repeat_override'):
            for item in held:item.update(status='operator_requested_alignment',decision='Operator confirmed repeated text for alignment, not publication.')
        alignment_request={**deepcopy(request),'held_passages':held}
    windows=alignment_regions(alignment_request,duration)
    if len(windows)>6 or any(r['end']-r['start']>180 for r in windows):
        raise ValueError('Alignment requires at most six sections, each at most180 seconds. Extracted words are preserved.')
    blocked=held_indices(held)
    atomic_json(out/'alignment-request.json',alignment_request,exclusive=True)
    if len(blocked)<len(windows):
        call('qwen-align',[config['align_python'],'-u',str(backend),'--stage','qwen-align','--request',str(out/'alignment-request.json')],480)
        result=json.loads((out/'qwen-align.json').read_text())
        evidence=validate_review_alignment(result,windows,blocked)
        receipt['backend']=result.get('backend','unverified')
        receipt['alignment_model_evidence']=_model_evidence(result);receipt['model_evidence']=receipt['alignment_model_evidence']
        represented={(i.get('region'),i.get('token_index')) for c in result['segments'] for i in c.get('native_timing_issues',[])}
        for item in result.get('unplaced',[]):
            if (item.get('region'),item.get('token_index')) in represented:continue
            ri=item['region'];r=windows[ri]
            held.append({'region':ri,'start':r['start'],'end':r['end'],'text':item['text'],'status':'held',
                'reason':'No supported native interval for this word; text retained without guessed timing.',
                'detector':'unplaced_native_word','native_evidence':item})
        # Map partial text only after validation; raw native results stay intact.
        result={**result,'segments':_automatic_groups(result['segments'])}
    else:
        result={'segments':[],'timing':'Automatic checks withheld every proposed section. Raw text retained; no word times invented.'}
        evidence={'withheld_regions':sorted(blocked),'automatically_vocal_verified':False,'timing_accuracy_verified':False}
    atomic_json(out/'repetition-review.json',{'held_passages':held,'note':'Automatic diagnostics, not vocal detection.'},exclusive=True)
    final_request={**alignment_request,'mode':mode}
    candidate=_candidate(final_request,out,receipt,windows,held,result,evidence)
    if mode=='extract':candidate['recognition_regions']=regions
    candidate['processing_status']='completed_with_exceptions' if held or any(c.get('native_timing_issues') or c.get('resolved_native_timing_issues') for c in candidate['segments']) else 'completed'
    return candidate
