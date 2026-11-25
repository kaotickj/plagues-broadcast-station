"""Saved/synthetic JSON only: no imports of torch, Qwen or acoustic models."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path
from contextlib import nullcontext
from dataclasses import make_dataclass
import sys

import pytest

from gnb_player.qwen_review_contract import (
    asr_windows, supplied_windows, token_mapping, recognition_flags,
    timestamp_capture, aligned_window, review_segments, validate_review_alignment,
)
from gnb_player.qwen_review_backend import capture_timestamp_parser


def window(start, end, text):
    return {'start': start, 'end': end, 'text': text,
            'evidence': {'kind': 'operator_selected', 'source': 'Test listening selection'}}


def native(text, spans, *, raw=None, start=0, end=30, region=0, offset=0):
    _, labels = token_mapping(text)
    parsed = [{'text': label, 'start_time': left, 'end_time': right}
              for label, (left, right) in zip(labels, spans)]
    raw = raw if raw is not None else [v for pair in spans for v in pair]
    capture = timestamp_capture(labels, raw, parsed, 80)
    seconds = [{**p, 'start_time': p['start_time'] / 1000, 'end_time': p['end_time'] / 1000} for p in parsed]
    return aligned_window(text, seconds, capture, region=region, start=start, end=end, source_offset=offset)


def result_for(text, spans, *, raw=None, start=0, end=30, offset=0):
    mapped = native(text, spans, raw=raw, start=start, end=end, offset=offset)
    regions = [window(start + offset, end + offset, text)]
    segments, groups = review_segments(mapped['words'], mapped['unplaced'], regions)
    result = {'text': text, 'reference_text': text,
              'reference_sha256': hashlib.sha256(text.encode()).hexdigest(),
              'segments': segments, 'unplaced': mapped['unplaced'],
              'native_timing_issues': mapped['native_timing_issues'], 'unplaced_groups': groups}
    return result, regions


@pytest.mark.parametrize('seconds, count', [(0.5, 1), (60, 1), (150, 1), (150.1, 2), (900, 6)])
def test_asr_windows_are_contiguous_sample_exact_and_bounded(seconds, count):
    samples = round(seconds * 16000)
    rows = asr_windows(samples)
    assert len(rows) == count and rows[0]['start_sample'] == 0 and rows[-1]['end_sample'] == samples
    assert all(.5 <= r['end'] - r['start'] <= 150 for r in rows)
    assert all(a['end_sample'] == b['start_sample'] for a, b in zip(rows, rows[1:]))
    assert sum(r['end_sample'] - r['start_sample'] for r in rows) == samples


@pytest.mark.parametrize('samples', [0, True, 1, 900 * 16000 + 1])
def test_invalid_asr_duration_fails_before_models(samples):
    with pytest.raises(ValueError):
        asr_windows(samples)


def test_alignment_long_text_needs_explicit_sections_without_proportional_split():
    with pytest.raises(ValueError, match='explicit'):
        supplied_windows({'reference_text': 'one two'}, 181)
    regions = [window(0, 180, 'one'), window(180, 360, 'two')]
    assert supplied_windows({'reference_text': 'one two', 'alignment_windows': regions}, 360) == regions
    with pytest.raises(ValueError):
        supplied_windows({'reference_text': 'one two', 'alignment_windows': [window(0, 181, 'one two')]}, 360)
    with pytest.raises(ValueError, match='six'):
        supplied_windows({'reference_text': ' '.join(['one'] * 7),
                          'alignment_windows': [window(i, i + 1, 'one') for i in range(7)]}, 7)


def test_supplied_spelling_apostrophes_hyphens_punctuation_and_repeats_survive():
    text = "Here’s to Murk-water, Murk-water!"
    mapping, normalized = token_mapping(text)
    assert normalized == ['Heres', 'to', 'Murkwater', 'Murkwater']
    assert [m['text'] for m in mapping] == text.split()
    result, regions = result_for(text, [(1000, 1200), (1200, 1400), (1400, 2000), (2100, 2800)])
    assert [w['text'] for c in result['segments'] for w in c['words']] == text.split()
    assert validate_review_alignment(result, regions)['coverage_complete']
    with pytest.raises(ValueError, match='bracketed'):
        token_mapping('[Instrumental] Here we go')


def test_one_original_token_with_multiple_native_constituents_keeps_support_gaps():
    result, regions = result_for('A中B', [(1000, 1200), (1400, 1600), (1800, 2000)])
    word = result['segments'][0]['words'][0]
    assert word['text'] == 'A中B' and word['individual_word_boundaries'] is None
    assert word['highlight_intervals'] == [[1, 1.2], [1.4, 1.6], [1.8, 2]]
    assert validate_review_alignment(result, regions)['coverage_complete']


def test_raw_repetition_detected_even_if_package_collapses_it():
    raw = 'language English<asr_text>' + 'Thank you. ' * 20
    audit = recognition_flags(raw, 'Thank you.', generated_tokens=[1, 2, 99], max_new_tokens=1024, eos_ids=[99])
    assert {'raw_consecutive_repetition', 'parser_modified_text'} <= set(audit['flags'])
    assert audit['raw_repetition']['copies'] == 20 and audit['termination'] == 'eos'
    assert not audit['automatically_vocal_verified']


def test_token_limit_and_empty_output_are_not_success_or_instrumental_detection():
    audit = recognition_flags('', '', generated_tokens=[1] * 1024, max_new_tokens=1024, eos_ids=[99])
    assert audit['termination'] == 'token_limit'
    assert 'token_limit_incomplete' in audit['flags']
    assert 'empty_transcript_not_instrumental_diagnosis' in audit['flags']


def test_offset_once_native_gaps_and_zero_words_remain_reviewable():
    result, regions = result_for('my own two', [(1000, 2000), (2000, 2000), (2500, 3000)], start=12.5, end=20)
    cue = result['segments'][0]
    assert cue['text'] == 'my own two' and [w['text'] for w in cue['words']] == ['my', 'two']
    assert (cue['start'], cue['end']) == (13.5, 15.5)
    assert cue['highlight_allowed'] is False and cue['highlight_intervals'] == []
    assert cue['native_timing_issues'][0]['text'] == 'own'
    audit = validate_review_alignment(result, regions)
    assert audit['review_text_complete'] and not audit['coverage_complete']
    assert audit['unresolved_token_count'] == 1 and not audit['timing_accuracy_verified']
    bad = deepcopy(result)
    bad['segments'][0]['words'][0]['source_offset_seconds'] += 12.5
    with pytest.raises(ValueError, match='offset'):
        validate_review_alignment(bad, regions)


def test_external_clip_offset_is_restored_once():
    result, regions = result_for('Told', [(1700, 2000)], start=0, end=17.5, offset=12.5)
    word = result['segments'][0]['words'][0]
    assert word['start'] == 14.2 and word['native_clip_span']['start'] == 1.7
    assert validate_review_alignment(result, regions)['source_offset_applied_once']


def test_repaired_and_resolution_limited_positive_spans_are_flagged_not_stretched():
    result, regions = result_for('me', [(14480, 14560)], raw=[13680, 14560])
    word = result['segments'][0]['words'][0]
    assert word['start'] == 14.48 and word['end'] == 14.56
    assert set(word['flags']) == {'qwen_timestamp_repaired_review', 'qwen_resolution_limited_span_review'}
    assert word['native_constituents'][0]['raw_timestamp_ms'] == [13680, 14560]
    assert validate_review_alignment(result, regions)['coverage_complete']


@pytest.mark.parametrize('span', [(1000, 1000), (2000, 1000), (-100, 500), (1000, 31000), (float('nan'), 1000)])
def test_invalid_native_spans_not_dropped_or_fabricated(span):
    result, regions = result_for('own', [span])
    assert result['segments'] == [] and result['unplaced'][0]['text'] == 'own'
    assert result['unplaced_groups'][0]['text'] == 'own'
    assert not validate_review_alignment(result, regions)['coverage_complete']
    json.dumps(result, allow_nan=False)


def test_order_conflict_is_unresolved_instead_of_sorted_into_apparent_accuracy():
    result, regions = result_for('one two', [(2000, 3000), (1000, 2000)])
    assert result['unplaced'][0]['text'] == 'two'
    assert 'native_span_order_conflict' in result['unplaced'][0]['reasons']
    assert validate_review_alignment(result, regions)['unresolved_token_count'] == 1


@pytest.mark.parametrize('mutation', ['drop_unplaced', 'wrong_text', 'hide_word', 'wrong_hash', 'bad_interval', 'duplicate'])
def test_review_validator_does_not_trust_adapter_claims(mutation):
    result, regions = result_for('my own two', [(1000, 2000), (2000, 2000), (2500, 3000)])
    result['alignment_evidence'] = {'coverage_complete': True}
    if mutation == 'drop_unplaced':
        result['unplaced'] = []
    elif mutation == 'wrong_text':
        result['segments'][0]['words'][0]['text'] = 'other'
    elif mutation == 'hide_word':
        result['segments'][0]['text'] = 'my two'
    elif mutation == 'wrong_hash':
        result['reference_sha256'] = 'bad'
    elif mutation == 'bad_interval':
        result['segments'][0]['words'][0]['highlight_intervals'] = [[0, 4]]
    else:
        result['unplaced'].append(deepcopy(result['unplaced'][0]))
    with pytest.raises(ValueError):
        validate_review_alignment(result, regions)


def test_small_partial_cues_and_all_invalid_groups_retain_every_original_token():
    text = ' '.join(f'word{i}' for i in range(20))
    spans = [(i * 100, i * 100 if i < 9 else i * 100 + 80) for i in range(20)]
    result, regions = result_for(text, spans)
    assert len(result['unplaced_groups']) == 1
    assert result['unplaced_groups'][0]['source_token_indices'] == list(range(9))
    assert all(len(c['source_token_indices']) <= 9 for c in result['segments'])
    assert validate_review_alignment(result, regions)['unresolved_token_count'] == 9


def test_withheld_region_has_no_timing_and_keeps_original_ids():
    result, _ = result_for('real words', [(1000, 1200), (1300, 1500)], start=3, end=6)
    for cue in result['segments']:
        cue['region'] = 1
        for word in cue['words']:
            word['region'] = 1
    reference = 'oh oh real words'
    result.update(text=reference, reference_text=reference,
                  reference_sha256=hashlib.sha256(reference.encode()).hexdigest())
    regions = [window(0, 3, 'oh oh'), window(3, 6, 'real words')]
    evidence = validate_review_alignment(result, regions, {0})
    assert evidence['withheld_regions'] == [0] and not evidence['coverage_complete']
    with pytest.raises(ValueError, match='omitted'):
        validate_review_alignment(result, regions)


def test_capture_saves_pre_and_post_repair_under_unique_names(tmp_path):
    processor = SimpleNamespace(parse_timestamp=lambda words, timestamps:
        [{'text': words[0], 'start_time': 160, 'end_time': 240}])
    captures, original = capture_timestamp_parser(processor, tmp_path, 80)
    for _ in range(2):
        processor.parse_timestamp(['me'], [80, 240])
    assert captures[0]['changed_boundary_count'] == 1
    assert json.loads((tmp_path / 'timestamps-000-raw.json').read_text())['raw_timestamp_ms'] == [80, 240]
    assert json.loads((tmp_path / 'timestamps-001-postprocessed.json').read_text())[0]['start_time'] == 160
    assert callable(original)


def test_raw_timestamp_survives_postprocessor_failure(tmp_path):
    def failed(*args):
        raise ValueError('fixture parsing failure')
    processor = SimpleNamespace(parse_timestamp=failed)
    capture_timestamp_parser(processor, tmp_path, 80)
    with pytest.raises(ValueError, match='fixture'):
        processor.parse_timestamp(['me'], [80, 240])
    assert (tmp_path / 'timestamps-000-raw.json').exists()
    assert not (tmp_path / 'timestamps-000-postprocessed.json').exists()


def test_native_count_or_normalized_text_drift_is_rejected():
    with pytest.raises(ValueError, match='count'):
        timestamp_capture(['one'], [80], [], 80)
    with pytest.raises(ValueError, match='order'):
        timestamp_capture(['one'], [80, 160], [{'text': 'other', 'start_time': 80, 'end_time': 160}], 80)


class FakeAudio:
    def __init__(self, samples):
        self.samples = samples

    def __len__(self):
        return self.samples

    def __getitem__(self, item):
        return FakeAudio(item.stop - item.start)


def fake_runtime(monkeypatch, samples):
    from gnb_player import qwen_review_backend as backend
    monkeypatch.setattr(backend, '_offline', lambda: None)
    monkeypatch.setattr(backend, '_audio', lambda request: (FakeAudio(samples), 16000, 0))
    monkeypatch.setattr(backend, '_runtime', lambda request, key:
        (SimpleNamespace(inference_mode=nullcontext), Path('/fake-local-model'), {}, {'reviewed': False}))
    monkeypatch.setattr(backend, '_sha256', lambda path: 'fixture-only')
    return backend


def test_fake_asr_wiring_loads_once_and_captures_six_windows_separately(tmp_path, monkeypatch):
    backend = fake_runtime(monkeypatch, 900 * 16000)
    loads, calls = [], []

    class FakeASR:
        def __init__(self):
            self.processor = SimpleNamespace(tokenizer=SimpleNamespace(batch_decode=lambda rows, **kwargs: ['fake prompt']))
            self.model = SimpleNamespace(eval=lambda: None,
                generation_config=SimpleNamespace(eos_token_id=[99], do_sample=False),
                generate=lambda **kwargs: SimpleNamespace(sequences=[[10, 11, 20, 99]]))

        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            loads.append(kwargs)
            return cls()

        def _infer_asr(self, *args, **kwargs):
            self.model.generate(input_ids=[[10, 11]])
            return ['language English<asr_text>word']

        def transcribe(self, **kwargs):
            assert kwargs['context'] == '' and kwargs['language'] is None and kwargs['return_time_stamps'] is False
            calls.append(len(kwargs['audio'][0]))
            self._infer_asr()
            return [SimpleNamespace(text='word', language='English')]

    monkeypatch.setitem(sys.modules, 'qwen_asr', SimpleNamespace(Qwen3ASRModel=FakeASR))
    result = backend.recognize({'wav': '/fake.wav'}, tmp_path)
    assert len(loads) == 1 and len(calls) == result['model_calls'] == 6
    assert loads[0]['max_new_tokens'] == 1024
    assert result['text'] == '\n'.join(['word'] * 6) and not result['reviewed']
    for i, region in enumerate(result['regions']):
        directory = tmp_path / f'qwen-asr-window-{i:03d}'
        assert (directory / 'generation-000-input.json').exists()
        assert (directory / 'generation-000-tokens.json').exists()
        assert (directory / 'preparser-000.json').exists()
        assert region['start'] == 150 * i and region['termination'] == 'eos'


def test_fake_alignment_wiring_skips_held_region_and_restores_offset(tmp_path, monkeypatch):
    backend = fake_runtime(monkeypatch, 60 * 16000)
    NativeWord = make_dataclass('NativeWord', [('text', str), ('start_time', float), ('end_time', float)])
    loads, calls = [], []

    class FakeAligner:
        timestamp_segment_time = 80

        def __init__(self):
            self.model = SimpleNamespace(eval=lambda: None)
            self.aligner_processor = SimpleNamespace(
                encode_timestamp=lambda text, language: (token_mapping(text)[1], 'not-used'),
                parse_timestamp=lambda words, times: [dict(text=w, start_time=times[i * 2], end_time=times[i * 2 + 1]) for i, w in enumerate(words)])

        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            loads.append(1)
            return cls()

        def align(self, *, audio, text, language):
            calls.append(text)
            labels = token_mapping(text)[1]
            parsed = self.aligner_processor.parse_timestamp(labels, [80, 160, 240, 320])
            return [SimpleNamespace(items=[NativeWord(p['text'], p['start_time'] / 1000, p['end_time'] / 1000) for p in parsed])]

    monkeypatch.setitem(sys.modules, 'qwen_asr', SimpleNamespace(Qwen3ForcedAligner=FakeAligner))
    regions = [window(0, 20, 'oh oh'), window(20, 40, 'real words')]
    request = {'wav': '/fake.wav', 'reference_text': 'oh oh real words', 'alignment_windows': regions,
               'held_passages': [{'region': 0, 'status': 'held'}]}
    result = backend.align(request, tmp_path)
    assert len(loads) == 1 and calls == ['real words'] and result['model_calls'] == 1
    assert result['segments'][0]['words'][0]['start'] == 20.08
    assert result['segments'][0]['region'] == 1
    assert result['alignment_evidence']['withheld_regions'] == [0]
    assert not (tmp_path / 'qwen-align-window-000').exists()
    assert (tmp_path / 'qwen-align-window-001/timestamps-000-raw.json').exists()


@pytest.mark.parametrize('variant', ['A-known-lyrics-alignment', 'B-self-transcript-alignment'])
def test_saved_stage_e_partial_timing_reaches_review_controller_without_word_loss(tmp_path, monkeypatch, variant):
    """Replay saved predictions through contract/controller, never run a model."""
    saved = Path(__file__).parents[2] / 'stage-e-seven-hours' / variant
    if not (saved / 'qwen.json').exists():
        pytest.skip('Local saved Stage E evidence not distributed with source checkout.')
    old = json.loads((saved / 'qwen.json').read_text())
    raw = json.loads((saved / 'raw-qwen-timestamps.json').read_text())
    parsed = json.loads((saved / 'postprocessed-qwen-timestamps.json').read_text())
    reference = old['request']['reference_text']
    capture = timestamp_capture(raw['words'], raw['raw_timestamp_ms'], parsed, raw['native_grid_ms'])
    mapped = aligned_window(reference, old['words'], capture, region=0, start=0, end=60)
    regions = [window(0, 60, reference)]
    segments, groups = review_segments(mapped['words'], mapped['unplaced'], regions)
    result = {'completed': True, 'text': reference, 'reference_text': reference,
              'reference_sha256': hashlib.sha256(reference.encode()).hexdigest(),
              'segments': segments, 'unplaced': mapped['unplaced'], 'duration': 60,
              'native_timing_issues': mapped['native_timing_issues'], 'unplaced_groups': groups}
    evidence = validate_review_alignment(result, regions)
    assert evidence['review_text_complete'] and not evidence['coverage_complete']
    assert mapped['unplaced']
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'src/gnb_player'))
    from review_qwen_worker import run_qwen
    request = {'mode': 'align', 'decoded_duration': 60, 'reference_text': reference,
               'alignment_windows': regions, 'title': 'Saved Stage E JSON fixture',
               'source_sha256': old['request']['source_sha256'], 'config': {'align_python': '/must-not-execute'}}
    calls = []
    def fake_call(stage, arguments, timeout):
        assert stage == 'qwen-align' and '/must-not-execute' in arguments
        calls.append(stage)
        (tmp_path / 'qwen-align.json').write_text(json.dumps(result, allow_nan=False))
    candidate = run_qwen(request, tmp_path, {'recognition_policy': {}}, fake_call)
    assert calls == ['qwen-align'] and not candidate['reviewed']
    assert candidate['provenance']['alignment_evidence']['review_text_complete']
    assert candidate['provenance']['unplaced'] == mapped['unplaced']
    # The controller upgrades legacy partial cues to explicit native-token
    # support. Independently construct the expected display map; every other
    # field (including original measurements, words and text) must stay exact.
    from copy import deepcopy
    expected = deepcopy(segments)
    partial_count = 0
    for row in expected:
        if row.get('timing_kind') != 'partial_native_support_requires_review':
            continue
        partial_count += 1
        assert row.pop('highlight_allowed') is False
        assert row.pop('highlight_intervals') == []
        row['partial_original_highlight_policy'] = {'highlight_allowed': False, 'highlight_intervals': []}
        indices = row['source_token_indices']
        labels = row['text'].split()
        assert len(indices) == len(labels)
        word_indices = {word['token_index']: i for i, word in enumerate(row['words'])}
        assert len(word_indices) == len(row['words'])
        tokens = [{'text': label, 'token_index': index, 'word_index': word_indices.get(index)}
                  for label, index in zip(labels, indices)]
        assert any(token['word_index'] is None for token in tokens)
        missing = {token['token_index'] for token in tokens if token['word_index'] is None}
        assert missing == {issue['token_index'] for issue in row['native_timing_issues']}
        row['timing_kind'] = 'partial_native_word_support'
        row['partial_timing'] = {'version': 'native-partial-tokens-1', 'region': row['region'], 'tokens': tokens}
    assert partial_count > 0
    assert candidate['segments'] == expected
    print(variant, 'tokens', len(reference.split()), 'positive', len(mapped['words']),
          'unresolved', len(mapped['unplaced']), 'cues', len(segments), 'controller candidate unreviewed')
