import math
import pytest
from gnb_player.alignment_contract import alignment_regions, validate_native_words, restore_source_timeline, require_supplied_text_coverage


def window(start, end, text):
    return {'start': start, 'end': end, 'text': text,
            'evidence': {'kind': 'operator_selected', 'source': 'Listening selection, not an automatic detector'}}


def test_legacy_full_audio_unchanged():
    assert alignment_regions({'reference_text': 'Told me Murkwater'}, 30) == [
        {'start': 0, 'end': 30, 'text': 'Told me Murkwater'}]


def test_known_words_preserved_across_instrumental_gap():
    request = {'reference_text': 'Mirelurk nest Murkwater blues Murkwater blues',
               'alignment_windows': [window(12.5, 20, 'Mirelurk nest'),
                                     window(25, 30, 'Murkwater blues'),
                                     window(40, 45, 'Murkwater blues')]}
    regions = alignment_regions(request, 50)
    assert [(r['start'], r['end']) for r in regions] == [(12.5, 20), (25, 30), (40, 45)]
    regions[0]['evidence']['source'] = 'changed'
    assert request['alignment_windows'][0]['evidence']['source'] != 'changed'


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -1, True, '12.5'])
def test_invalid_bounds_rejected(bad):
    with pytest.raises(ValueError):
        alignment_regions({'reference_text': 'Told', 'alignment_windows': [window(bad, 20, 'Told')]}, 30)


@pytest.mark.parametrize('windows', [
    [window(10, 31, 'Told')], [window(10, 10, 'Told')],
    [window(10, 20, 'Told'), window(19, 25, 'me')],
    [{'start': 1, 'end': 2, 'text': 'Told'}],
])
def test_overlaps_missing_provenance_and_duration_rejected(windows):
    with pytest.raises(ValueError):
        alignment_regions({'reference_text': 'Told me', 'alignment_windows': windows}, 30)


@pytest.mark.parametrize('changed', ['Told me water', 'Told me', 'Told me Murkwater Murkwater'])
def test_no_silent_name_replacement_omission_or_repeat(changed):
    with pytest.raises(ValueError):
        alignment_regions({'reference_text': 'Told me Murkwater',
                           'alignment_windows': [window(12.5, 30, changed)]}, 30)


def test_native_times_not_shifted_or_interpolated():
    regions = [window(12.5, 30, 'Told me')]
    words = [{'text': 'Told', 'start': 14.223, 'end': 14.6, 'region': 0},
             {'text': 'me', 'start': 14.7, 'end': 14.8, 'region': 0}]
    evidence = validate_native_words(words, regions)
    assert words[0]['start'] == 14.223
    assert evidence['coverage_complete']
    assert not evidence['timing_accuracy_verified']
    assert not evidence['automatically_vocal_verified']


def test_missing_words_remain_an_explicit_coverage_issue():
    evidence = validate_native_words([{'text': 'Told', 'start': 14, 'end': 15, 'region': 0}],
                                     [window(12.5, 30, 'Told me')])
    assert not evidence['coverage_complete']
    assert evidence['coverage_issues'][0]['expected'] == ['Told', 'me']


def test_no_native_word_may_cross_instrumental_gap():
    with pytest.raises(ValueError):
        validate_native_words([{'text': 'Told', 'start': 14, 'end': 26, 'region': 0}],
                              [window(12.5, 20, 'Told'), window(25, 30, 'me')])


def test_unknown_region_and_zero_duration_words_rejected():
    for word in [{'text': 'Told', 'start': 14, 'end': 14, 'region': 0},
                 {'text': 'Told', 'start': 14, 'end': 15, 'region': 2}]:
        with pytest.raises(ValueError):
            validate_native_words([word], [window(12.5, 20, 'Told')])


def test_crop_offset_restored_once_with_gap_preserved():
    original = [{'text': 'group phrase', 'start': 1.7, 'end': 4,
                 'grouped_text': True, 'individual_word_boundaries': None,
                 'highlight_intervals': [[1.7, 2], {'start': 3, 'end': 4}]}]
    mapped = restore_source_timeline(original, clip_start=12.5, clip_end=30, source_duration=184.8)
    assert mapped[0]['start'] == 14.2
    assert mapped[0]['highlight_intervals'] == [[14.2, 14.5], {'start': 15.5, 'end': 16.5}]
    assert mapped[0]['individual_word_boundaries'] is None
    assert original[0]['start'] == 1.7
    with pytest.raises(ValueError, match='already'):
        restore_source_timeline(mapped, clip_start=12.5, clip_end=30, source_duration=184.8)


def test_offset_never_reenables_deliberately_disabled_highlight():
    mapped = restore_source_timeline([{'text': 'held', 'start': 1, 'end': 2,
                                      'highlight_allowed': False, 'highlight_intervals': []}],
                                     clip_start=12.5, clip_end=30, source_duration=184.8)
    assert mapped[0]['highlight_allowed'] is False and mapped[0]['highlight_intervals'] == []


@pytest.mark.parametrize('word', [
    {'text': 'Told', 'start': -1, 'end': 2},
    {'text': 'Told', 'start': 1, 'end': 18},
    {'text': 'Told', 'start': 1, 'end': 2, 'highlight_intervals': [[1, 3]]},
    {'text': 'Told', 'start': 1, 'end': 2, 'highlight_intervals': [[1.5, 2], [1, 1.4]]},
])
def test_crop_mapping_rejects_unsupported_spans(word):
    with pytest.raises(ValueError):
        restore_source_timeline([word], clip_start=12.5, clip_end=30, source_duration=184.8)


def test_geometric_validation_does_not_claim_vocal_onset_accuracy():
    # This resembles the known wrong full-intro CTC placement. Geometry cannot
    # decide whether the word was sung, so the evidence must not claim it can.
    evidence = validate_native_words([{'text': 'Told', 'start': 2.263, 'end': 7.583, 'region': 0}],
                                     [window(0, 30, 'Told')])
    assert evidence['coverage_complete'] and not evidence['timing_accuracy_verified']


def test_global_region_order_cannot_be_hidden_by_partitioning():
    with pytest.raises(ValueError, match='ordered'):
        validate_native_words([{'text':'me','start':4,'end':5,'region':1},
                               {'text':'Told','start':1,'end':2,'region':0}],
                              [window(0,3,'Told'),window(3,6,'me')])


@pytest.mark.parametrize('intervals', [[[0,1]], [[14.3,14.4],[14.2,14.3]], [[14.2,float('nan')]], 'bad'])
def test_native_highlight_support_must_stay_inside_word(intervals):
    with pytest.raises(ValueError):
        validate_native_words([{'text':'Told','start':14.2,'end':14.5,'region':0,
                               'highlight_intervals':intervals}],[window(12.5,30,'Told')])


def test_controller_recomputes_coverage_instead_of_trusting_adapter_claim():
    alignment={'segments':[{'text':'Told','words':[{'text':'Told','start':1,'end':2,'region':0}]}],
               'alignment_evidence':{'coverage_complete':True}}
    with pytest.raises(ValueError, match='omitted'):
        require_supplied_text_coverage(alignment,[window(0,5,'Told me')])


def test_intentionally_withheld_window_keeps_original_region_id():
    alignment={'segments':[{'text':'real words','words':[{'text':'real','start':4,'end':4.2,'region':1},
                                                       {'text':'words','start':4.3,'end':4.5,'region':1}]}]}
    evidence=require_supplied_text_coverage(alignment,[window(0,3,'oh oh'),window(3,5,'real words')],{0})
    assert evidence['alignable_regions_complete'] and not evidence['coverage_complete']
    assert evidence['withheld_regions']==[0]
    with pytest.raises(ValueError, match='omitted'):
        require_supplied_text_coverage(alignment,[window(0,3,'oh oh'),window(3,5,'real words')])


def test_controller_rejects_line_label_drift():
    alignment={'segments':[{'text':'Different words','words':[{'text':'Told','start':1,'end':2,'region':0}]}]}
    with pytest.raises(ValueError, match='line labels'):
        require_supplied_text_coverage(alignment,[window(0,5,'Told')])


@pytest.mark.parametrize('omit_word',[False,True])
def test_worker_candidate_commit_requires_complete_supplied_words(tmp_path,omit_word):
    """Fixture runtime returns JSON only; no acoustic model is loaded."""
    import json,subprocess,sys,wave,hashlib
    from pathlib import Path
    audio=tmp_path/'source.wav'
    with wave.open(str(audio),'wb') as f:
        f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\0'*16000*2*5)
    lock=tmp_path/'shared.lock';lock.touch();ctc=tmp_path/'ctc.fixture';ctc.write_bytes(b'not a model')
    fake=tmp_path/'json-only-fixture'
    fake.write_text('''#!/usr/bin/python3
import json,sys
from pathlib import Path
p=Path(sys.argv[-1]);q=json.loads(p.read_text())
assert sys.argv[sys.argv.index('--stage')+1]=='align'
assert q['alignment_windows'][1]['start']==3
words=[{'text':'Told','start':1.2,'end':1.5,'region':0}]
if not q['omit_word']:words.append({'text':'me','start':3.2,'end':3.5,'region':1})
segments=[{'text':w['text'],'start':w['start'],'end':w['end'],'words':[w]} for w in words]
result={'completed':True,'duration':5,'segments':segments,'unplaced':[],'timing':'JSON fixture, no inference','alignment_evidence':{'coverage_complete':True}}
(p.parent/'align.json').write_text(json.dumps(result))
''');fake.chmod(0o700)
    request={'source':str(audio),'source_sha256':hashlib.sha256(audio.read_bytes()).hexdigest(),
             'mode':'align','title':'Fixture','reference_text':'Told me','omit_word':omit_word,
             'alignment_windows':[window(1,2,'Told'),window(3,4,'me')],
             'local_lock':str(tmp_path/'local.fixture.lock'),
             'config':{'timeout_seconds':10,'shared_lock':str(lock),'ctc_model':str(ctc),
                       'heart_model':str(tmp_path/'unused-heart'),'heart_python':'/must-not-run',
                       'align_python':str(fake)}}
    path=tmp_path/'request.json';path.write_text(json.dumps(request))
    worker=Path(__file__).parents[1]/'src/gnb_player/review_worker.py'
    run=subprocess.run([sys.executable,str(worker),'--request',str(path)],capture_output=True,text=True,timeout=15)
    receipt=json.loads((tmp_path/'receipt.json').read_text())
    assert receipt['workers_stopped'] and receipt['locks_released']
    assert (tmp_path/'align.json').exists() # raw evidence always retained
    if omit_word:
        assert run.returncode==2 and not (tmp_path/'candidate.json').exists()
        assert 'omitted' in receipt['error']
    else:
        assert run.returncode==0,run.stdout+run.stderr
        candidate=json.loads((tmp_path/'candidate.json').read_text())
        assert not candidate['reviewed']
        assert candidate['provenance']['alignment_evidence']['coverage_complete']
        assert candidate['provenance']['alignment_windows']==request['alignment_windows']
