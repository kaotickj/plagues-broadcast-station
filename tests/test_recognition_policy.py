"""Model-free regressions for actual decoder policy, evidence and singing layout."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import os
import sys
import numpy as np
import pytest

from gnb_player.extraction_backend import lyrics_pipeline as pipeline
from gnb_player.extraction_backend.lyrics_recognition import recognition_policy, cache_policy
from gnb_player.extraction_backend.lyrics_quality import assess
from gnb_player.extraction_backend import lyrics_acceleration as gpu


def test_default_policy_is_transcription_without_filename_or_previous_context(monkeypatch):
    for name in ('GNB_LYRICS_LANGUAGE', 'GNB_LYRICS_TASK', 'GNB_LYRICS_PROMPT'):
        monkeypatch.delenv(name, raising=False)
    policy = recognition_policy('large-v3')
    assert policy['language'] == 'en' and policy['task'] == 'transcribe'
    assert policy['prompt'] == '' and not policy['condition_on_previous_text']


def test_explicit_policy_is_validated_and_independent_review_ignores_prompt(monkeypatch):
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE', 'auto')
    monkeypatch.setenv('GNB_LYRICS_PROMPT', 'An explicitly supplied context')
    policy = recognition_policy('large-v3')
    assert policy['language'] == 'auto' and policy['prompt_source'] == 'explicit-setting'
    english = recognition_policy('medium.en', independent=True)
    assert english['requested_language'] == 'auto' and english['language'] == 'en'
    assert english['prompt'] == ''
    assert cache_policy(policy) != cache_policy(english)
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE', 'fr')
    with pytest.raises(ValueError, match='English-only'):
        recognition_policy('medium.en')
    monkeypatch.setenv('GNB_LYRICS_TASK', 'translate')
    with pytest.raises(ValueError, match='translation'):
        recognition_policy('large-v3')


def test_qwen_never_injects_title_and_records_explicit_settings(monkeypatch):
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE', 'auto')
    monkeypatch.delenv('GNB_LYRICS_PROMPT', raising=False)
    model = SimpleNamespace(model=object(), transcribe=Mock(return_value=[SimpleNamespace(text='Sung words', language='English')]))
    fake_torch = SimpleNamespace(bfloat16='bf16', float32='fp32')
    factory = Mock(return_value=model)
    evidence = {}
    with patch.dict(sys.modules, {'torch':fake_torch, 'qwen_asr':SimpleNamespace(Qwen3ASRModel=SimpleNamespace(from_pretrained=factory))}), patch.object(pipeline,'quantize_cpu'), patch.object(pipeline,'release_memory'):
        result = pipeline.transcribe_qwen(np.ones(16000),16000,'qwen3-asr-0.6b',lambda *a:None,title='PRIVATE FILE TITLE',evidence=evidence)
    assert model.transcribe.call_args.kwargs['context'] == ''
    assert model.transcribe.call_args.kwargs['language'] is None
    assert result[0]['language'] == 'English'
    assert evidence['chunks'][0]['text'] == 'Sung words'
    assert 'PRIVATE FILE TITLE' not in json.dumps(evidence)


def test_whisper_preserves_raw_diagnostics_and_suspect_words(monkeypatch):
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE', 'auto')
    monkeypatch.setenv('GNB_LYRICS_TASK', 'transcribe')
    monkeypatch.setenv('GNB_LYRICS_PROMPT', 'Explicit context')
    word = SimpleNamespace(word=' Thank you.', start=.2, end=1, probability=.99)
    segment = SimpleNamespace(text=' Thank you.',start=0,end=1,words=[word],avg_logprob=-1.3,compression_ratio=1,no_speech_prob=.9)
    transcribe = Mock(return_value=([segment],SimpleNamespace(duration=4,language='en',language_probability=.8)))
    model = SimpleNamespace(transcribe=transcribe)
    evidence={}
    with patch.dict(sys.modules, {'faster_whisper':SimpleNamespace(WhisperModel=lambda *a,**kw:model)}), patch.object(pipeline,'release_memory'):
        cues, warnings = pipeline.transcribe_whisper([], 'large-v3', lambda *a:None, evidence=evidence)
    assert transcribe.call_args.kwargs['task'] == 'transcribe'
    assert transcribe.call_args.kwargs['language'] is None
    assert transcribe.call_args.kwargs['initial_prompt'] == 'Explicit context'
    assert cues[0]['text'] == 'Thank you.' and cues[0]['needs_review']
    assert cues[0]['words'][0]['uncertain'] is True
    assert evidence['segments'][0]['no_speech_prob'] == .9
    assert evidence['segments'][0]['text'] == ' Thank you.'
    assert warnings


def test_sustained_words_are_grouped_without_changing_text_or_timing():
    words=[{'text':t,'start':a,'end':b,'score':.99} for t,a,b in [('I',1,1.2),('had',2.9,3.1),('a',3.2,3.3),('man',3.5,4),('who',4.2,4.5),('loved',4.6,5),('me',5.1,5.5)]]
    original=deepcopy(words)
    cues,_=pipeline.make_cues(words,10)
    assert len(cues)==1 and cues[0]['text']=='I had a man who loved me'
    assert [{k:w[k] for k in ('text','start','end','score')} for c in cues for w in c['words']]==original
    assert words==original


def test_intentional_reference_lines_and_long_pauses_stay_separate():
    words=[{'text':'One','start':0,'end':1,'line':0},{'text':'Two','start':1.2,'end':2,'line':1}]
    assert len(pipeline.make_cues(words,10,preserve_lines=True)[0])==2
    words[1]['start']=6;words[1]['end']=7
    assert len(pipeline.make_cues(words,10)[0])==2


def test_exact_reported_opening_is_flagged_without_rewriting_user_sample():
    sample=(Path(__file__).parent/'fixtures/reported_radioactive_rain.txt').read_text()
    assert sample.startswith('transcript Emily\nBeynon Rain Started Falling\n')
    lines=sample.splitlines();segments=[];cursor=0
    for line in lines:
        words=[]
        for token in line.split():
            words.append({'text':token,'start':cursor,'end':cursor+.3,'score':.99});cursor+=.4
        segments.append({'text':line,'start':words[0]['start'],'end':words[-1]['end'],'words':words})
    data={'duration':max(100,cursor+1),'segments':segments,'warnings':[], 'metrics':{'recognition_confidence':.99,'low_confidence_fraction':0}}
    before=deepcopy(data)
    result=assess(data)
    assert any(r['code']=='hallucination' and r['at']==0 for r in result['reasons'])
    assert data==before


def test_high_non_speech_evidence_flags_but_keeps_terminal_thanks():
    data={'duration':10,'segments':[{'start':1,'end':3,'text':'Thank you','words':[{'text':'Thank','start':1,'end':2},{'text':'you','start':2,'end':3}]}], 'metrics':{},
          'recognition_evidence':{'primary':{'segments':[{'text':'Thank you','start':1,'end':3,'no_speech_prob':.9,'avg_logprob':-1.3}]}}}
    before=deepcopy(data)
    assert any(r['code']=='non_speech_evidence' for r in assess(data)['reasons'])
    assert data==before


def test_vulkan_keeps_raw_words_and_literal_explicit_policy(tmp_path,monkeypatch):
    binary=tmp_path/'whisper';binary.touch();model=tmp_path/'model.bin';model.touch()
    monkeypatch.setattr(gpu,'VULKAN',binary);monkeypatch.setattr(gpu,'GGML',model)
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE','auto');monkeypatch.setenv('GNB_LYRICS_PROMPT','Literal; context $5')
    monkeypatch.setenv('GNB_LYRICS_PHASE_GPU','0')
    raw={'result':{'language':'en'},'transcription':[{'text':' Thank','offsets':{'from':0,'to':500},'tokens':[{'text':' Thank','p':.4}]}]}
    def recognize(args,**kwargs):
        Path(args[args.index('-of')+1]).with_suffix('.json').write_text(json.dumps(raw))
        return SimpleNamespace(returncode=0)
    evidence={}
    with patch.dict(sys.modules,{'soundfile':SimpleNamespace(write=lambda *a,**kw:None)}), patch.object(gpu.subprocess,'run',side_effect=recognize) as invoked:
        words=gpu.recognize_windows(np.zeros(16000),16000,[('edge',0,1)],tmp_path,lambda *a:None,evidence=evidence)
    args=invoked.call_args.args[0]
    assert args[args.index('-l')+1]=='auto'
    assert args[args.index('--prompt')+1]=='Literal; context $5'
    assert '-tr' not in args
    assert evidence['windows'][0]['raw']==raw and evidence['language']=='en'
    assert words['edge'][0]['text']=='Thank'
    assert not list(tmp_path.glob('vulkan-*.json'))


def test_worker_preserves_before_after_review_without_recursive_evidence(tmp_path,monkeypatch):
    from gnb_player import extract_worker as worker
    from gnb_player.extraction import ExtractionConfig
    source=tmp_path/'recording.mp3';source.write_bytes(b'fixture');stamp=source.stat();digest=worker.source_fingerprint(source)
    data={'duration':10,'segments':[{'text':'Original','start':1,'end':2,'words':[]}],
          'recognition_evidence':{'primary':{'raw':'original decoder output'}}}
    def review(*args,**kwargs):
        args[2]['segments'][0]['text']='Corroborated replacement'
        return args[2]
    modules={'lyrics_acceleration':SimpleNamespace(reset_gpu_timings=lambda:None,gpu_timings=lambda:[]),
             'lyrics_pipeline':SimpleNamespace(extract=lambda *a,**kw:deepcopy(data)),
             'lyrics_review':SimpleNamespace(review=review),
             'lyrics_quality':SimpleNamespace(assess=lambda data:{'status':'review'})}
    config=ExtractionConfig(cache_dir=str(tmp_path/'cache'))
    with patch.object(worker,'validate_source',return_value=(stamp.st_size,stamp.st_mtime_ns,digest,10)),patch.object(worker,'require_runtime'),patch.dict(sys.modules,modules):
        output=worker.run(source,tmp_path/'saved.json',config)
    evidence=output['recognition_evidence']
    assert evidence['before_review']['segments'][0]['text']=='Original'
    assert evidence['after_review']['segments'][0]['text']=='Corroborated replacement'
    assert evidence['primary']=={'raw':'original decoder output'}
    assert 'recognition_evidence' not in evidence['before_review']


def test_high_confidence_credit_gets_fresh_review_but_is_not_deleted_on_blank_passes():
    from gnb_player.extraction_backend import lyrics_review as review
    data={'duration':10,'segments':[{'text':'transcript Emily','start':1,'end':3,
          'words':[{'text':'transcript','start':1,'end':2,'score':.72},{'text':'Emily','start':2,'end':3,'score':.99}]}], 'warnings':[]}
    original=deepcopy(data)
    assert review.suspect_cue(data,0)
    with patch.dict(sys.modules,{'lyrics_pipeline':pipeline}):
        result=review.reconcile(data,[],{0:[]},lambda a,b:0)
    assert result['segments']==original['segments']
    assert result['machine_review']['held_passages']
    assert not result['machine_review']['repairs']
    assert data==original


def test_independent_review_raw_cache_is_policy_bound_and_preserves_diagnostics(tmp_path,monkeypatch):
    from gnb_player.extraction_backend import lyrics_review as review
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE','en');monkeypatch.setenv('GNB_LYRICS_PROMPT','Do not share this prompt with reviewer')
    word=SimpleNamespace(word='Rain',start=.1,end=.8,probability=.9)
    segment=SimpleNamespace(text='Rain',start=0,end=1,words=[word],avg_logprob=-.1,compression_ratio=1,no_speech_prob=.1)
    model=SimpleNamespace(transcribe=Mock(return_value=([segment],SimpleNamespace(language='en'))))
    paths=[];saved=[]
    cache=SimpleNamespace(evidence_path=lambda a,s,k:paths.append(k) or tmp_path/'cache.json',read_evidence=lambda *a:None,write_evidence=lambda *a:saved.append(a))
    modules={'lyrics_acceleration':SimpleNamespace(enabled=lambda:False,recognize_windows=None,GpuCapacityDeferred=ValueError),
             'lyrics_pipeline':pipeline,'lyrics_cache':cache,'faster_whisper':SimpleNamespace(WhisperModel=lambda *a,**kw:model)}
    evidence={}
    with patch.dict(sys.modules,modules),patch.object(pipeline,'release_memory'):
        result=review.recognize(np.zeros(16000),16000,[('full',0,1)],'medium.en',lambda *a:None,evidence=evidence)
    assert result['full'][0]['text']=='Rain'
    assert model.transcribe.call_args.kwargs['initial_prompt'] is None
    assert evidence['settings']['prompt']==''
    assert evidence['windows'][0]['recognition']['segments'][0]['no_speech_prob']==.1
    assert len(saved[0])==3 and saved[0][2]['segments']
    assert 'policy-v4:' in paths[0]
