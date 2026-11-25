"""Pipeline ordering and worker adapters, using disposable data and no models."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import io
import json
import os
import sys
import numpy as np
import pytest
from gnb_player import extract_worker as worker
from gnb_player.extraction_backend import lyrics_pipeline as pipeline


def cues(text='bad', score=.02):
    words=[{'text':text,'start':1,'end':1.5,'score':score}]
    return [{'text':text,'start':1,'end':1.5,'words':words,'needs_review':score<.35}]


def backend_modules(events, original):
    class Rejected(ValueError): pass
    def recognize(*args,evidence,**kwargs):
        events.append('recognize');evidence.update(backend='fixture',language='en',segments=[{'text':'bad'}])
        return {'full':deepcopy(original[0]['words'])}
    def align(audio,sr,words,*args,**kwargs):
        events.append('align')
        if words[0]['text']=='bad':raise Rejected('Wrong primary words cannot align')
        return words,'cpu'
    return {'torch':SimpleNamespace(set_num_threads=lambda _:None,set_num_interop_threads=lambda _:None),
            'soundfile':SimpleNamespace(read=lambda *a,**kw:(np.sin(np.arange(1000)/4).astype('float32')*.2,100)),
            'lyrics_cache':SimpleNamespace(restore=Mock(),store=Mock(),digest=Mock()),
            'lyrics_acceleration':SimpleNamespace(enabled=lambda:True,recognize_windows=recognize,realign=align,
                GpuCapacityDeferred=type('Deferred',(Exception,),{}),AlignmentRejected=Rejected)}


def test_second_opinion_runs_before_alignment_without_mutating_primary_evidence(tmp_path):
    events=[];original=cues();before=deepcopy(original)
    def checked(audio,sr,rows,duration,progress,primary):
        events.append('second-opinion');assert rows[0]['text']=='bad'
        rows=deepcopy(rows);rows[0].update(text='correct',needs_review=True)
        rows[0]['words']=[{'text':'correct','start':1,'end':1.5,'score':.99}]
        return {'cues':rows,'report':{'status':'refined','regions':[],'edits':[{'kind':'fixture'}],'primary_evidence':deepcopy(primary)}}
    with patch.dict(sys.modules,backend_modules(events,original)),patch.object(pipeline,'decode'):
        result=pipeline.extract(tmp_path/'audio',tmp_path,'large-v3',False,'',lambda *a:None,prealign=checked)
    assert events==['recognize','second-opinion','align']
    assert result['recognition_evidence']['pre_alignment'][0]['text']=='bad'
    assert result['recognition_evidence']['alignment_input'][0]['text']=='correct'
    assert 'primary_evidence' not in result['recognition_evidence']['pre_alignment_recovery']
    assert result['segments'][0]['text']=='correct'
    assert any('Human review is required' in warning for warning in result['warnings'])
    assert original==before


def test_unresolved_original_still_hits_existing_alignment_guard(tmp_path):
    events=[];original=cues(score=.99)
    def unresolved(*args):
        events.append('second-opinion')
        return {'cues':deepcopy(original),'report':{'regions':[{'status':'held'}]}}
    with patch.dict(sys.modules,backend_modules(events,original)),patch.object(pipeline,'decode'):
        with pytest.raises(ValueError,match='Wrong primary') as failed:
            pipeline.extract(tmp_path/'audio',tmp_path,'large-v3',False,'',lambda *a:None,prealign=unresolved)
    assert events==['recognize','second-opinion','align']
    assert failed.value.recognition_evidence['alignment']['status']=='rejected'
    assert failed.value.recognition_evidence['pre_alignment'][0]['text']=='bad'


def test_supplied_reference_never_calls_automatic_recognition_recovery(tmp_path):
    events=[];reference='User words';forbidden=Mock(side_effect=AssertionError('Never replace reference lyrics'))
    with patch.dict(sys.modules,backend_modules(events,cues())),patch.object(pipeline,'decode'),patch.object(
            pipeline,'align_reference',return_value=[{'text':'User','start':1,'end':1.5,'score':.9}]) as align:
        pipeline.extract(tmp_path/'audio',tmp_path,'large-v3',False,reference,lambda *a:None,prealign=forbidden)
    forbidden.assert_not_called();align.assert_called_once();assert not events


def test_runtime_adapter_requests_fresh_cpu_evidence_and_restores_gpu_choice(monkeypatch):
    captured=[];results=[]
    def recognize(audio,sr,windows,engine,progress,evidence):
        assert os.environ['GNB_LYRICS_GPU']=='0' and os.environ['GNB_LYRICS_FRESH_RECOGNITION']=='1'
        captured.append((engine,windows))
        evidence.update(backend='faster-whisper',windows=[{'cached':False,'recognition':{'segments':[
            {'start':0,'end':1,'no_speech_prob':.91,'avg_logprob':-.52,'compression_ratio':1}]}}])
        return {'bounded-prealignment':[]}
    def refine(rows,duration,adapter,**kwargs):
        results.append(adapter('medium.en',0,30));results.append(adapter('large-v3',0,30))
        return {'cues':rows,'report':{}}
    monkeypatch.setenv('GNB_LYRICS_GPU','1');monkeypatch.delenv('GNB_LYRICS_FRESH_RECOGNITION',raising=False)
    with patch.dict(sys.modules,{'lyrics_prealignment':SimpleNamespace(refine=refine),
            'lyrics_review':SimpleNamespace(recognize=recognize,safe_review_edit=Mock())}):
        worker.prealign_recognition([],16000,cues(),30,lambda *a:None,{})
    assert [x[0] for x in captured]==['medium.en','large-v3']
    assert all(x['fresh'] and x['segments'][0]['no_speech_prob']==.91 for x in results)
    assert os.environ['GNB_LYRICS_GPU']=='1' and 'GNB_LYRICS_FRESH_RECOGNITION' not in os.environ


@pytest.mark.parametrize('raw',[b'not valid JSON',b'{"human_review":{"status":"draft"}}',
    b'{"publication":{"method":"human"}}',b'{"pipeline":{"recognizer":"supplied-lyrics"}}',b'{"pipeline":1}'])
def test_automatic_target_protection_preserves_human_and_unreadable_documents(raw):
    assert worker.protected_human_target(raw)


def test_absent_or_machine_only_target_remains_eligible_for_explicit_extraction():
    assert not worker.protected_human_target(None)
    assert not worker.protected_human_target(b'{"pipeline":{"recognizer":"large-v3"}}')


def test_failed_retry_retains_existing_edited_untimed_draft_and_canonical_lyrics(tmp_path):
    from gnb_player.recognition_drafts import load_draft,save_user_draft
    cache=tmp_path/'cache';source_sha='a'*64
    canonical=tmp_path/'lyrics.json';canonical.write_text('Saved human lyrics — 雨\nkept')
    original=canonical.read_bytes()
    evidence={'pre_alignment':[{'text':'First automatic phrase\nsecond line','words':[]} ]}
    retained=worker.retain_failed_recognition(ValueError('Timing rejected'),evidence,cache,source_sha)
    assert retained.recognition_draft_path!=str(canonical)
    draft=load_draft(retained.recognition_draft_path,expected_source_sha256=source_sha)
    assert draft.text=='First automatic phrase\nsecond line'
    edited=save_user_draft(draft.path,'Human correction — 雨\nsecond line',
                          expected_revision=draft.revision,expected_source_sha256=source_sha)
    second=worker.retain_failed_recognition(ValueError('New retry rejected'),
        {'pre_alignment':[{'text':'Different model guess','words':[]}]},cache,source_sha)
    current=load_draft(second.recognition_draft_path,expected_source_sha256=source_sha)
    assert current.raw_payload==edited.raw_payload
    assert canonical.read_bytes()==original
    assert len(list((cache/'failures').glob('*.json')))==2
    assert current.payload['status']=='unaligned'
    assert 'segments' not in current.payload and 'human_review' not in current.payload


def test_error_protocol_emits_structured_recovery_paths_without_message_parsing(tmp_path,monkeypatch):
    error=ValueError('Timing rejected')
    error.source_sha256='b'*64
    error.diagnostic_path=str(tmp_path/'failures'/'report.json')
    error.recognition_draft_path=str(tmp_path/'recognition-drafts'/'draft.json')
    stream=io.StringIO();monkeypatch.setattr(worker,'OUTPUT',stream)
    monkeypatch.setattr(worker,'isolate_process_group',lambda:None)
    monkeypatch.setattr(worker,'run',Mock(side_effect=error))
    rc=worker.main(['--source',str(tmp_path/'source.mp3'),'--output',str(tmp_path/'lyrics.json'),'--config-json','{}'])
    event=json.loads(stream.getvalue())
    assert rc==1 and event['type']=='error'
    assert event['source_sha256']==error.source_sha256
    assert event['recognition_draft_path']==error.recognition_draft_path
    assert event['diagnostic_path']==error.diagnostic_path
