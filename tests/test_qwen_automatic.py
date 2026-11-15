"""Automatic ASR→alignment controller with saved-style results; no model imports."""
import copy,json,hashlib
from pathlib import Path
import pytest

@pytest.fixture
def worker(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]/'src/gnb_player'))
    import review_qwen_worker
    return review_qwen_worker


def request(tmp_path):
    r={'source_sha256':'fixture','source':'fixture.wav','title':'Must not become prompt','mode':'extract',
       'decoded_duration':60,'config':{'align_python':'fixture-python'},'reference_text':'','reference_original':''}
    (tmp_path/'request.json').write_text(json.dumps(r));return r


def asr(regions=None):
    return {'regions':regions or [{'start':0,'end':60,'text':'Good words','raw_text':'Good words','flags':[]}],
            'model_revision':'ASR-revision','settings':{'context':''}}


def aligned(text='Good words',region=0):
    words=[{'text':t,'start':10+i*.5,'end':10.4+i*.5,'region':region,'token_index':i} for i,t in enumerate(text.split())]
    return {'segments':[{'text':text,'start':words[0]['start'],'end':words[-1]['end'],'words':words,'region':region}],
            'unplaced':[],'native_timing_issues':[],'alignment_evidence':{},'timing':'fixture native times',
            'model_revision':'ALIGN-revision'}


def complete_result(result,inputs):
    result=copy.deepcopy(result);text=inputs['reference_text']
    result.update(text=text,reference_text=text,reference_sha256=hashlib.sha256(text.encode()).hexdigest())
    for cue in result['segments']:
        cue['source_token_indices']=[w['token_index'] for w in cue['words']]
        for word in cue['words']:
            offset=inputs['alignment_windows'][word['region']]['start']
            word.update(source_offset_seconds=offset,native_clip_span={'start':word['start']-offset,'end':word['end']-offset},
                coordinates='source_audio_seconds',highlight_intervals=[[word['start'],word['end']]])
    return result


def run(worker,tmp_path,raw=None,result=None,fail=None):
    r=request(tmp_path);receipt={'recognition_policy':{'automatic_vocabulary_injection':False}};calls=[];events=[]
    def call(stage,command,cap):
        inputs=json.loads(Path(command[-1]).read_text());calls.append((stage,inputs,cap))
        if stage=='qwen-asr':
            assert not inputs['reference_text'] and not inputs.get('context')
            (tmp_path/'qwen-asr.json').write_text(json.dumps(raw or asr()))
        elif fail:raise fail
        else:(tmp_path/'qwen-align.json').write_text(json.dumps(complete_result(result or aligned(),inputs)))
    answer=worker.run_qwen(r,tmp_path,receipt,call,lambda kind,**kw:events.append((kind,kw)))
    return answer,receipt,calls,events


def test_one_job_generates_timed_lyrics_without_supplied_text(worker,tmp_path):
    candidate,receipt,calls,events=run(worker,tmp_path)
    assert [s for s,_,_ in calls]==['qwen-asr','qwen-align']
    assert calls[1][1]['reference_text']=='Good words'
    assert calls[1][1]['alignment_source']=='audio_only_asr_output'
    assert candidate['segments'][0]['start']==10
    assert candidate['recognition_regions'][0]['text']=='Good words'
    assert candidate['provenance']['recognition_model_evidence']['model_revision']=='ASR-revision'
    assert candidate['provenance']['alignment_model_evidence']['model_revision']=='ALIGN-revision'
    assert candidate['reviewed'] is False and candidate['review']['status']=='unreviewed'
    assert len(events)==1 and Path(events[0][1]['checkpoint']).is_file()
    assert json.loads((tmp_path/'request.json').read_text())['reference_text']==''


def test_alignment_failure_preserves_recognition_checkpoint(worker,tmp_path):
    with pytest.raises(TimeoutError):run(worker,tmp_path,fail=TimeoutError('fixture budget'))
    saved=json.loads((tmp_path/'recognition-candidate.json').read_text())
    assert saved['transcription_only'] and not saved['segments'] and not saved['reviewed']
    assert saved['recognition_regions'][0]['text']=='Good words'
    assert (tmp_path/'qwen-asr.json').is_file()


def test_cancellation_is_not_retried_or_claimed_complete(worker,tmp_path):
    class Cancelled(Exception):pass
    with pytest.raises(Cancelled):run(worker,tmp_path,fail=Cancelled('operator'))
    assert (tmp_path/'recognition-candidate.json').is_file()
    assert not (tmp_path/'candidate.json').exists()


def test_all_suspect_text_finishes_flagged_without_alignment(worker,tmp_path):
    raw=asr([{'start':0,'end':60,'text':'Thank you','raw_text':'Thank you '*20,'flags':[]}])
    candidate,receipt,calls,_=run(worker,tmp_path,raw=raw)
    assert [s for s,_,_ in calls]==['qwen-asr']
    assert not candidate['segments'] and candidate['held_passages']
    assert candidate['processing_status']=='completed_with_exceptions' and not candidate['reviewed']


def test_partial_hold_carries_raw_tokenlimit_and_correct_region(worker,tmp_path):
    raw=asr([{'start':0,'end':30,'text':'Unfinished','raw_text':'Unfinished','flags':['token_limit_incomplete']},
             {'start':30,'end':60,'text':'Good words','raw_text':'Good words','flags':[]}])
    result=aligned(region=1)
    for word in result['segments'][0]['words']:word['start']+=30;word['end']+=30
    result['segments'][0]['start']+=30;result['segments'][0]['end']+=30
    candidate,receipt,calls,_=run(worker,tmp_path,raw=raw,result=result)
    assert calls[1][1]['held_passages'][0]['region']==0
    assert candidate['held_passages'][0]['detector']=='decoder_termination'
    assert candidate['segments'][0]['region']==1


def test_empty_regions_keep_original_evidence_and_remap_holds(worker,tmp_path):
    raw=asr([{'start':0,'end':20,'text':'','raw_text':'','flags':[]},
             {'start':20,'end':40,'text':'bad','raw_text':'bad','flags':['token_limit']},
             {'start':40,'end':60,'text':'Good words','raw_text':'Good words','flags':[]}])
    result=aligned(region=1)
    for word in result['segments'][0]['words']:word['start']+=35;word['end']+=35
    result['segments'][0]['start']+=35;result['segments'][0]['end']+=35
    candidate,_,calls,_=run(worker,tmp_path,raw=raw,result=result)
    assert len(candidate['recognition_regions'])==3
    hold=calls[1][1]['held_passages'][0]
    assert hold['region']==0 and hold['recognition_region']==1


def test_missing_native_evidence_cannot_create_grouped_timing(worker):
    segments=[{'text':'one missing three','start':1,'end':4,'region':0,
               'words':[{'text':'one','start':1,'end':2},{'text':'three','start':3,'end':4}],
               'native_timing_issues':[{'text':'missing','region':0,'token_index':1}],
               'highlight_allowed':False,'highlight_intervals':[]}]
    original=copy.deepcopy(segments)
    with pytest.raises(ValueError,match='source token indices'):
        worker._automatic_groups(segments)
    assert segments==original


def test_retry_alignment_uses_saved_text_without_new_asr(worker,tmp_path):
    r=request(tmp_path);r.update(mode='align',reference_text='Good words',alignment_windows=[{'start':0,'end':60,'text':'Good words','evidence':{'kind':'operator_selected','source':'saved'}}])
    (tmp_path/'request.json').write_text(json.dumps(r));calls=[]
    def call(stage,command,cap):
        calls.append(stage);(tmp_path/'qwen-align.json').write_text(json.dumps(complete_result(aligned(),json.loads(Path(command[-1]).read_text()))))
    candidate=worker.run_qwen(r,tmp_path,{'recognition_policy':{}},call)
    assert calls==['qwen-align'] and candidate['segments'] and not candidate['reviewed']
