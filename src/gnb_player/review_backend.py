"""Pinned local Heart/CTC adapters, executed only in installed model runtimes."""
from pathlib import Path
import argparse, json, os, time, resource, sys
from review_capture import atomic_json, save_sequences, compact_segments
from review_repetition import held_indices
from alignment_contract import alignment_regions, validate_native_words
from recognition_policy import audio_only_policy

def heart(request, out):
    policy=audio_only_policy(request)  # before importing/loading model resources
    import numpy as np, torch, transformers, wave
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    torch.set_num_threads(6); torch.set_num_interop_threads(1)
    model_path=Path(request['config']['heart_model'])
    model=WhisperForConditionalGeneration.from_pretrained(model_path,local_files_only=True,
        use_safetensors=True,trust_remote_code=False,dtype=torch.float32,attn_implementation='eager').to('cpu').eval()
    processor=WhisperProcessor.from_pretrained(model_path,local_files_only=True,trust_remote_code=False)
    with wave.open(request['wav'],'rb') as f:
        assert f.getframerate()==16000 and f.getnchannels()==1 and f.getsampwidth()==2
        audio=np.frombuffer(f.readframes(f.getnframes()),dtype='<i2').astype(np.float32)/32768
    inputs=processor(audio,sampling_rate=16000,return_tensors='pt',return_attention_mask=True,
                     truncation=False,padding='longest' if len(audio)>480000 else 'max_length')
    settings=dict(max_new_tokens=256,num_beams=2,language='en',task='transcribe',
        condition_on_prev_tokens=False,temperature=0.0,return_timestamps=True,
        compression_ratio_threshold=1.8,logprob_threshold=-1.0,no_speech_threshold=.4,use_cache=True)
    with torch.inference_mode():
        generated=model.generate(input_features=inputs.input_features,attention_mask=inputs.attention_mask,
                                 return_dict_in_generate=True,**settings)
    rows=save_sequences(generated,out/'raw-heart-tokens.json',{'model_path':str(model_path),'source_sha256':request['source_sha256']})
    native=compact_segments(generated)
    if native is not None:atomic_json(out/'raw-heart-segments.json',native,exclusive=True)
    text=processor.tokenizer.decode(rows[0],skip_special_tokens=True)
    regions=[{**r,'text':processor.tokenizer.decode(r['tokens'],skip_special_tokens=True)} for r in native[0]] if native else [{'start':0,'end':len(audio)/16000,'text':text}]
    return {'text':text,'regions':regions,'settings':settings,'torch':torch.__version__,'recognition_policy':policy,
            'transformers':transformers.__version__,'threads':torch.get_num_threads(),
            'device':'cpu','dtype':'float32','timing_note':'Decoder regions are coarse estimates, not detected singing.'}

def align(request,out):
    import torch, torchaudio, soundfile as sf
    import torchaudio.functional as F
    from extraction_backend.lyrics_pipeline import ctc_letters
    torch.set_num_threads(6);torch.set_num_interop_threads(1)
    audio,sr=sf.read(request['wav'],dtype='float32');assert sr==16000 and audio.ndim==1
    duration=len(audio)/sr
    regions=json.loads((out/'heart.json').read_text())['regions'] if request['mode']=='extract' else alignment_regions(request,duration)
    bundle=torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H;model=bundle.get_model().to('cpu').eval();labels={x:i for i,x in enumerate(bundle.get_labels())}
    emissions=[];times=[];block=20*sr
    for first in range(0,len(audio),block):
        left=max(0,first-sr);end=min(len(audio),first+block);right=min(len(audio),end+sr)
        with torch.inference_mode():logits,_=model(torch.from_numpy(audio[left:right].copy())[None]);prob=logits[0].log_softmax(-1).cpu()
        centers=(torch.arange(len(prob))*320+200+left)/sr;chosen=(centers>=first/sr)&(centers<end/sr)
        emissions.append(prob[chosen]);times.extend(centers[chosen].tolist())
    del model
    emission=torch.cat(emissions);centers=torch.tensor(times);words=[];unplaced=[];traces=[];last=0
    blocked=held_indices(request.get('held_passages',[]))
    for ri,region in enumerate(regions):
        if ri in blocked:continue
        left=max(0,float(region['start']));right=min(duration,float(region['end']))
        if not region['text'].strip():continue
        if left<last-.021 or right<=left:raise ValueError('Overlapping/invalid decoder regions need manual review; no guessed alignment.')
        last=right;selected=(centers>=left)&(centers<right);region_times=centers[selected].tolist();e=emission[selected][None]
        tokens=[];owners=[];original=[]
        for word in region['text'].split():
            letters=ctc_letters(word)
            if not letters:unplaced.append({'region':ri,'text':word,'reason':'No CTC letters'});continue
            wi=len(original);original.append(word)
            if tokens:tokens.append(labels['|']);owners.append(None)
            tokens.extend(labels[c] for c in letters);owners.extend([wi]*len(letters))
        if not tokens:continue
        aligned,scores=F.forced_align(e,torch.tensor([tokens],dtype=torch.int64),blank=0)
        spans=F.merge_tokens(aligned[0],scores[0].exp(),blank=0)
        if len(spans)!=len(tokens):raise ValueError('Native CTC span mismatch; no interpolation permitted.')
        grouped=[[] for _ in original]
        for span,owner in zip(spans,owners):
            if owner is not None:grouped[owner].append(span)
        for wi,(word,chars) in enumerate(zip(original,grouped)):
            st=max(left,region_times[chars[0].start]-.01);en=min(right,region_times[chars[-1].end-1]+.01)
            score=sum(float(x.score) for x in chars)/len(chars);flags=[]
            if score<.2:flags.append('low_ctc_score')
            if en-st>2:flags.append('long_span_review')
            if wi==0 or wi==len(original)-1:flags.append('decoder_boundary_review')
            if '?' in word:flags.append('fragment_review')
            words.append({'text':word,'start':st,'end':en,'score':score,'flags':flags,'region':ri,
                          'highlight_intervals':[[st,en]],'highlight_allowed':True})
        traces.append({'region':ri,'frame_centers':region_times,'target_tokens':tokens,'owners':owners,
                       'character_spans':[{'token':int(s.token),'start':int(s.start),'end':int(s.end),'score':float(s.score)} for s in spans]})
    atomic_json(out/'ctc-native-traces.json',traces,exclusive=True)
    if not words:raise ValueError('No words could be aligned. Raw text retained for review.')
    cues=[]
    for w in words:
        if not cues or len(cues[-1]['words'])>=9 or w['region']!=cues[-1]['region'] or w['start']-cues[-1]['end']>1.2:
            cues.append({'start':w['start'],'end':w['end'],'text':'','words':[],'region':w['region']})
        cues[-1]['words'].append(w);cues[-1]['end']=w['end'];cues[-1]['text']=' '.join(x['text'] for x in cues[-1]['words'])
    evidence=validate_native_words(words,regions) if request['mode']=='align' else None
    return {'alignment_evidence':evidence,'segments':cues,'duration':duration,'unplaced':unplaced,'regions':regions,
            'torch':torch.__version__,'torchaudio':torchaudio.__version__,'threads':torch.get_num_threads(),
            'device':'cpu','dtype':'float32','timing':'Native CTC frame spans; original offset zero; no interpolation.'}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--stage',choices=['heart','align'],required=True);p.add_argument('--request',required=True);a=p.parse_args()
    request=json.loads(Path(a.request).read_text());out=Path(a.request).parent
    os.environ.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',OMP_NUM_THREADS='6',MKL_NUM_THREADS='6',OPENBLAS_NUM_THREADS='6',TORCH_HOME=str(out/'torch-cache'))
    import socket
    def offline(*args,**kwargs):raise RuntimeError('Review models are offline; downloads are disabled.')
    socket.create_connection=offline;socket.socket.connect=offline
    started=time.monotonic();result=heart(request,out) if a.stage=='heart' else align(request,out)
    result.update(completed=True,elapsed_seconds=time.monotonic()-started,peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    atomic_json(out/(a.stage+'.json'),result,exclusive=True)
