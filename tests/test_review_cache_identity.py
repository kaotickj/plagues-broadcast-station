"""CPU independent-review cache must bind actual model/runtime identity."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import importlib

import numpy as np
import pytest

from gnb_player.extraction_backend import lyrics_review as review
from gnb_player.extraction_backend import lyrics_cache as cache


@pytest.fixture
def cpu_review(tmp_path, monkeypatch):
    counts={'models':0,'transcribes':0,'identities':0}
    state={'identity':{'model_sha256':'a'*64,'runtime_sha256':'b'*64}}
    def identify(path):
        counts['identities']+=1
        assert path==tmp_path/'existing-model'
        if state.get('error'):raise state['error']
        return deepcopy(state['identity'])
    def transcribe(audio, **kwargs):
        counts['transcribes']+=1
        assert kwargs['beam_size']==3 and kwargs['condition_on_previous_text'] is False
        word=SimpleNamespace(word=' fixture',start=.1,end=.7,probability=.9)
        segment=SimpleNamespace(start=0,end=1,text=' fixture',words=[word])
        return iter([segment]),SimpleNamespace(language='en')
    def factory(*args,**kwargs):
        counts['models']+=1
        assert kwargs['device']=='cpu' and kwargs['compute_type']=='int8'
        return SimpleNamespace(transcribe=transcribe)
    monkeypatch.setenv('GNB_LYRICS_CACHE_DIR',str(tmp_path/'cache'))
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE','en')
    monkeypatch.setenv('GNB_LYRICS_TASK','transcribe')
    monkeypatch.setenv('GNB_LYRICS_PROMPT','')
    monkeypatch.setenv('GNB_LYRICS_CPU_THREADS','2')
    monkeypatch.setitem(sys.modules,'lyrics_acceleration',SimpleNamespace(
        enabled=lambda:False,recognize_windows=Mock(side_effect=AssertionError('GPU forbidden')),GpuCapacityDeferred=RuntimeError))
    monkeypatch.setitem(sys.modules,'lyrics_pipeline',SimpleNamespace(
        model_path=lambda *a,**kw:tmp_path/'existing-model',release_memory=lambda:None))
    monkeypatch.setitem(sys.modules,'faster_whisper',SimpleNamespace(WhisperModel=factory))
    monkeypatch.setitem(sys.modules,'lyrics_cache',cache)
    identity_module=importlib.import_module('gnb_player.extraction_backend.lyrics_identity')
    monkeypatch.setattr(identity_module,'recognizer_identity',identify)
    def run(windows=None,engine='large-v3'):
        evidence={}
        result=review.recognize(np.zeros(20,dtype=np.float32),10,
            windows or [('first',0,1),('second',1,2)],engine,lambda *a:None,evidence=evidence)
        return result,evidence
    return counts,state,run,tmp_path


def test_identical_model_runtime_reuses_review_without_constructing_model(cpu_review):
    counts,state,run,_=cpu_review
    first,evidence=run()
    assert counts=={'models':1,'transcribes':2,'identities':2}
    assert evidence['cache']['status']=='identity-verified'
    second,evidence=run()
    assert second==first
    assert counts=={'models':1,'transcribes':2,'identities':3}
    assert all(item['cached'] for item in evidence['windows'])
    assert all(item['recognition']['segments'] for item in evidence['windows'])


@pytest.mark.parametrize('changed_field',['model_sha256','runtime_sha256'])
def test_same_path_model_repair_or_runtime_change_invalidates_cache(cpu_review,changed_field):
    counts,state,run,_=cpu_review
    first,_=run()
    state['identity'][changed_field]='c'*64
    second,evidence=run()
    assert second==first
    assert counts['models']==2 and counts['transcribes']==4
    assert not any(item['cached'] for item in evidence['windows'])


def test_identity_failure_disables_only_cache_without_leaking_error_contents(cpu_review,monkeypatch):
    counts,state,run,root=cpu_review
    state['error']=OSError('do-not-expose-secret-fixture')
    forbidden=Mock(side_effect=AssertionError('No unverifiable cache key permitted'))
    monkeypatch.setattr(cache,'evidence_path',forbidden)
    result,evidence=run()
    assert result['first'][0]['text']=='fixture'
    assert counts['models']==1 and counts['transcribes']==2 and counts['identities']==1
    assert evidence['cache']['status']=='disabled' and 'OSError' in evidence['cache']['reason']
    assert 'do-not-expose-secret-fixture' not in str(evidence)
    assert not list((root/'cache').glob('*'))
    forbidden.assert_not_called()


def test_same_pcm_at_different_song_offsets_keeps_absolute_word_times(cpu_review):
    counts,state,run,_=cpu_review
    first,_=run([('phrase',0,1)])
    second,evidence=run([('phrase',1,2)])
    assert first['phrase'][0]['start']==.1 and second['phrase'][0]['start']==1.1
    assert counts['transcribes']==2 and not evidence['windows'][0]['cached']


def test_recognition_language_change_invalidates_cache(cpu_review,monkeypatch):
    counts,state,run,_=cpu_review
    run()
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE','auto')
    _,evidence=run()
    assert counts['transcribes']==4 and counts['models']==2
    assert not any(item['cached'] for item in evidence['windows'])


def test_mid_recognition_model_change_writes_no_reusable_cache(cpu_review,monkeypatch):
    counts,state,run,root=cpu_review
    original=cache.evidence_path
    def mutate_after_identity(*args):
        state['identity']['model_sha256']='e'*64
        return original(*args)
    monkeypatch.setattr(cache,'evidence_path',mutate_after_identity)
    with pytest.raises(RuntimeError,match='changed during recognition'):
        run()
    assert not list((root/'cache').glob('evidence-*.json'))


def test_fresh_choice_bypasses_review_cache(cpu_review,monkeypatch):
    counts,state,run,_=cpu_review
    run()
    monkeypatch.setenv('GNB_LYRICS_FRESH_RECOGNITION','1')
    _,evidence=run()
    assert counts['models']==2 and counts['transcribes']==4
    assert not any(item['cached'] for item in evidence['windows'])
