"""Deterministic audio fixtures and failed-worker preservation; no model loading."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
import sys

import numpy as np
import pytest

from gnb_player import extract_worker as worker
from gnb_player.extraction import ExtractionConfig
from gnb_player.extraction_backend import lyrics_pipeline as pipeline
from gnb_player.extraction_backend import lyrics_signal as signal
from gnb_player.extraction_backend.lyrics_quality import assess


def audio_fixture(*, active=1, level=.1, seconds=100, sr=100):
    values = np.zeros(seconds * sr, dtype=np.float32)
    count = round(seconds * active) * sr
    values[:count] = level * np.sin(np.arange(count) * .31)
    return values


def collapsed_cues():
    return [{'text':'Thank you ' * 5,
             'words':[{'text': t, 'start': n * 10, 'end': (n + 1) * 10, 'score':.99}
                      for n,t in enumerate(['Thank','you'] * 5)]}]


@pytest.mark.parametrize('level', [.003, .1])
def test_good_vocals_and_quiet_continuous_vocals_are_not_rejected(level):
    source = signal.measure(audio_fixture(), 100)
    report = signal.inspect_input(audio_fixture(level=level), 100, separated=True, source=source)
    assert not report['suspect_stem'] and not report['effectively_empty']
    signal.require_usable_recognition(collapsed_cues(), 100, report)


def test_long_instrumental_sections_with_short_genuine_lyrics_are_kept():
    report = signal.inspect_input(audio_fixture(active=.15, level=.003), 100,
                                  separated=True, source=signal.measure(audio_fixture(), 100))
    assert report['suspect_stem']
    cues = [{'words':[{'text':'again','start':i*.5,'end':i*.5+.4} for i in range(10)]}]
    signal.require_usable_recognition(cues, 100, report)
    assert report['recognition']['two_token_fraction'] == 1  # Repetition alone is legitimate.


def test_near_silent_stem_plus_collapsed_timing_fails_without_mutating_words():
    report = signal.inspect_input(audio_fixture(active=.15, level=.003), 100,
                                  separated=True, source=signal.measure(audio_fixture(), 100))
    cues = collapsed_cues(); before = deepcopy(cues)
    with pytest.raises(ValueError, match='Separate vocals disabled'):
        signal.require_usable_recognition(cues, 100, report)
    assert cues == before


def test_short_loud_bursts_cannot_mask_long_missing_stem_activity():
    # A globally audible RMS, like Whiskey and Silence, must not bypass activity measurement.
    report = signal.inspect_input(audio_fixture(active=.19, level=.04), 100,
                                  separated=True, source=signal.measure(audio_fixture(), 100))
    assert report['recognition_input']['rms_dbfs'] > -42
    assert report['suspect_stem']


def test_silent_and_invalid_inputs_have_no_guessed_transcript():
    assert signal.inspect_input(np.zeros(100),100)['effectively_empty']
    assert not signal.inspect_input(audio_fixture(active=.5),100)['effectively_empty']
    for values in ([], [float('nan')], [float('inf')]):
        with pytest.raises(ValueError): signal.inspect_input(values,100)


def test_actual_pipeline_stops_before_alignment_and_exposes_raw_failure_evidence(tmp_path):
    stem = audio_fixture(active=.15, level=.003)
    original = audio_fixture()
    words = collapsed_cues()[0]['words']
    raw = {'text':'transcript Emily · Thank you.♪♪'}
    def recognize(*args, evidence, **kwargs):
        evidence.update(backend='whisper.cpp', windows=[{'raw':deepcopy(raw)}])
        return {'full':deepcopy(words)}
    align = Mock(side_effect=AssertionError('An unusable stem must never be forced into alignment'))
    modules = {'torch':SimpleNamespace(set_num_threads=lambda n:None,set_num_interop_threads=lambda n:None),
               'soundfile':SimpleNamespace(read=lambda p,**kw:(original if Path(p).name=='original-signal-check.wav' else stem,100)),
               'lyrics_cache':SimpleNamespace(restore=lambda *a:False,store=lambda *a:None),
               'lyrics_acceleration':SimpleNamespace(enabled=lambda:True,recognize_windows=recognize,
                                                      realign=align,GpuCapacityDeferred=type('Deferred',(Exception,),{}),
                                                      AlignmentRejected=type('Rejected',(ValueError,),{}))}
    with patch.dict(sys.modules,modules), patch.object(pipeline,'separate_vocals',return_value='cpu'), patch.object(pipeline,'decode'):
        with pytest.raises(ValueError,match='No replacement lyrics') as failed:
            pipeline.extract(tmp_path/'source.mp3',tmp_path,'large-v3',True,'',lambda *a:None)
    assert failed.value.recognition_evidence['primary']['windows'][0]['raw'] == raw
    assert failed.value.recognition_evidence['pre_alignment']
    assert failed.value.recognition_evidence['signal']['suspect_stem']
    align.assert_not_called()


@pytest.mark.parametrize('edited_during_job', [False, True])
@pytest.mark.parametrize('diagnostics_writable', [False, True])
def test_worker_failure_keeps_saved_or_newer_manual_lyrics_and_raw_evidence(tmp_path, monkeypatch,
                                                                         edited_during_job, diagnostics_writable):
    source=tmp_path/'recording.mp3'; source.write_bytes(b'original recording')
    target=tmp_path/'lyrics.json'; target.write_text('Saved human lyrics — 雨\nsecond line',encoding='utf-8')
    stamp=source.stat(); fingerprint=worker.source_fingerprint(source)
    raw={'primary':{'backend':'fixture','segments':[{'text':'Thank you.♪♪\ntranscript Emily'}]}}
    def fail(*args, **kwargs):
        if edited_during_job: target.write_text('Newer manual edit — 雨\nkept',encoding='utf-8')
        error=ValueError('Unusable vocal input; try Separate vocals disabled.')
        error.recognition_evidence=deepcopy(raw)
        raise error
    modules={'lyrics_pipeline':SimpleNamespace(extract=fail),
             'lyrics_acceleration':SimpleNamespace(reset_gpu_timings=lambda:None,gpu_timings=lambda:[]),
             'lyrics_review':SimpleNamespace(review=Mock()),'lyrics_quality':SimpleNamespace(assess=Mock())}
    config=ExtractionConfig(cache_dir=str(tmp_path/'cache'))
    if not diagnostics_writable: monkeypatch.setattr(worker,'save_failure_evidence',Mock(side_effect=PermissionError('fixture')))
    with patch.dict(sys.modules,modules),patch.object(worker,'validate_source',return_value=(stamp.st_size,stamp.st_mtime_ns,fingerprint,100)),patch.object(worker,'require_runtime'):
        with pytest.raises(ValueError,match='Separate vocals disabled'):
            worker.run(source,target,config)
    expected='Newer manual edit — 雨\nkept' if edited_during_job else 'Saved human lyrics — 雨\nsecond line'
    assert target.read_text(encoding='utf-8') == expected
    assert source.read_bytes() == b'original recording'
    assert not (tmp_path/'history').exists()
    files=list((tmp_path/'cache/failures').glob('*.json'))
    assert len(files)==int(diagnostics_writable)
    if files:
        data=json.loads(files[0].read_text()); assert data['recognition_evidence']==raw
        assert data['status']=='failed' and data['source_sha256']==fingerprint
        assert files[0].stat().st_mode & 0o777 == 0o600


def test_alignment_confidence_and_independent_no_speech_are_reviewed_without_deleting_text():
    word={'text':'rain','start':1,'end':2,'score':.99,'alignment_score':.05}
    cue=pipeline.cue([word]); assert cue['words'][0]['alignment_score']==.05 and cue['needs_review']
    data={'duration':10,'segments':[cue], 'recognition_evidence':{
        'primary':{'windows':[{'raw':{'transcription':[{'text':'rain'}]}}]},
        'independent_review':{'secondary':{'windows':[{'recognition':{'segments':[
            {'text':'rain','start':1,'no_speech_prob':.77,'avg_logprob':-.90}]}}]}}}}
    before=deepcopy(data); codes={r['code'] for r in assess(data)['reasons']}
    assert {'alignment_confidence','non_speech_evidence'} <= codes and data==before


def test_missing_vulkan_no_speech_values_stay_unknown():
    data={'duration':10,'segments':[], 'recognition_evidence':{'primary':{
        'windows':[{'raw':{'transcription':[{'text':'rain','tokens':[{'p':.001}]}]}}]}}}
    assert 'non_speech_evidence' not in {r['code'] for r in assess(data)['reasons']}


@pytest.mark.parametrize('fatal', [False,True])
def test_pipeline_distinguishes_rejected_alignment_from_optional_runtime_failure(tmp_path,fatal):
    class Rejected(ValueError): pass
    error = Rejected('Implausible CTC timing; no replacement saved') if fatal else RuntimeError('Optional aligner unavailable')
    words=[{'text':'雨','start':1,'end':1.4,'score':.9}]
    raw={'text':'雨'}
    def recognize(*args,evidence,**kwargs):
        evidence.update(language='en',windows=[{'raw':deepcopy(raw)}])
        return {'full':deepcopy(words)}
    modules={'torch':SimpleNamespace(set_num_threads=lambda n:None,set_num_interop_threads=lambda n:None),
             'soundfile':SimpleNamespace(read=lambda *a,**kw:(audio_fixture(),100)),
             'lyrics_cache':SimpleNamespace(restore=lambda *a:False,store=lambda *a:None),
             'lyrics_acceleration':SimpleNamespace(enabled=lambda:True,recognize_windows=recognize,
                realign=Mock(side_effect=error),GpuCapacityDeferred=type('Deferred',(Exception,),{}),AlignmentRejected=Rejected)}
    with patch.dict(sys.modules,modules),patch.object(pipeline,'decode'):
        if fatal:
            with pytest.raises(Rejected) as failed:
                pipeline.extract(tmp_path/'source',tmp_path,'large-v3',False,'',lambda *a:None)
            evidence=failed.value.recognition_evidence
            assert evidence['alignment']['status']=='rejected'
        else:
            result=pipeline.extract(tmp_path/'source',tmp_path,'large-v3',False,'',lambda *a:None)
            assert 'Optional aligner unavailable' in ' '.join(result['warnings'])
            assert result['segments'][0]['words']==words
            evidence=result['recognition_evidence']
    assert evidence['primary']['windows'][0]['raw']==raw
    assert evidence['pre_alignment'][0]['words']==words


def test_malformed_optional_backend_metadata_does_not_break_quality_assessment():
    for evidence in ('unknown', {'signal':'unknown','primary':'unknown'}, {'primary':{'windows':[None,{'raw':{'transcription':[None,{'text':'rain','offsets':'unknown'}]}}]}}):
        result=assess({'duration':10,'segments':[],'recognition_evidence':evidence})
        assert result['status']=='review'
