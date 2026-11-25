"""Injected ASR fixtures only: no model, audio inference, cache or app writes."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from gnb_player.extraction_backend import lyrics_prealignment as recovery


def word(text, start, end, score=.96):
    return {'text': text, 'start': start, 'end': end, 'score': score}


def cue(text='unclear', start=10., end=12., *, weak=True):
    return {'text': text, 'start': start, 'end': end,
            'words': [word(text, start, end, .1 if weak else .96)],
            'needs_review': weak}


def hypothesis(words, start, end, **changes):
    result = {'words': deepcopy(words), 'fresh': True, 'backend': 'injected-test',
              'segments': [{'start': start, 'end': end, 'avg_logprob': -.1,
                            'no_speech_prob': .01, 'compression_ratio': 1.1}],
              'evidence': {'private_fixture': True}}
    result.update(changes)
    return result


def adapter_for(words=None):
    def run(engine, start, end):
        return hypothesis(words or [word('clear', start + .2, start + .7)], start, end)
    return Mock(side_effect=run)


def test_corroborated_positive_replaces_whole_cue_only_and_preserves_neighbors():
    original = [cue('before', 0, .5, weak=False), cue('wrong', 1, 2),
                cue('after', 3, 3.5, weak=False)]
    primary = {'engine': 'large-v3', 'segments': [{'start': 1, 'end': 2, 'no_speech_prob': .7}]}
    before = deepcopy((original, primary))
    words = [word('before', .05, .4), word('Clear', 1.1, 1.4),
             word('sky.', 1.5, 2), word('after', 3.1, 3.4)]
    adapter = adapter_for(words)
    result = recovery.refine(original, 5, adapter, primary)
    assert result['cues'][0] == original[0] and result['cues'][2] == original[2]
    replacement = result['cues'][1]
    assert replacement['text'] == 'Clear sky.' and replacement['start'] == 1.1
    assert replacement['end'] == 2 and replacement['needs_review'] is True
    assert replacement['words'] == words[1:3]
    assert result['report']['status'] == 'refined'
    assert [call.args[0] for call in adapter.call_args_list] == ['medium.en', 'large-v3']
    assert before == (original, primary)
    assert result['report']['primary_evidence'] == primary
    assert result['report']['primary_evidence'] is not primary
    result['report']['primary_evidence']['segments'].clear()
    assert primary == before[1]


def test_actual_ad_victoriam_negative_shape_empty_medium_and_high_no_speech_large():
    # Observed experiment: medium produced no words, large produced17; large's
    # no_speech=.90778 disqualifies it even with good logprob/word probabilities.
    # Word labels below are fixture labels, not a claimed reference transcript.
    original = [cue('Thank you for listening.', 0, 28)]
    words = [word('fixture' + str(n), n + .1, n + .6, .99) for n in range(17)]
    def run(engine, start, end):
        if engine == 'medium.en': return hypothesis([], start, end, segments=[])
        result = hypothesis(words, start, end)
        result['segments'][0].update(no_speech_prob=.90778, avg_logprob=-.01)
        return result
    adapter = Mock(side_effect=run)
    result = recovery.refine(original, 30, adapter)
    assert adapter.call_count == 2
    assert result['cues'] == original and not result['report']['edits']
    assert result['report']['status'] == 'held'
    region = result['report']['regions'][0]
    assert 'Empty recognition' in region['hold_reason']
    assert 'no-speech' in region['hold_reason']
    assert len(region['hypotheses']['large-v3']['words']) == 17
    assert region['hypotheses']['large-v3']['segments'][0]['no_speech_prob'] == .90778


def test_identical_hallucinations_with_high_no_speech_are_not_corroboration():
    original = [cue()]
    def run(engine, start, end):
        result = hypothesis([word('Thank', 10.1, 10.5), word('you.', 10.6, 11)], start, end)
        result['segments'][0]['no_speech_prob'] = .6
        return result
    result = recovery.refine(original, 30, run)
    assert result['cues'] == original and not result['report']['edits']
    assert 'no-speech' in result['report']['regions'][0]['hold_reason']


def test_disagreement_retains_all_original_text_and_both_hypotheses():
    original = [cue()]
    def run(engine, start, end):
        return hypothesis([word('rain' if engine == 'medium.en' else 'pain', 10, 11)], start, end)
    result = recovery.refine(original, 30, run)
    assert result['cues'] == original
    assert 'disagree' in result['report']['regions'][0]['hold_reason']
    assert set(result['report']['regions'][0]['hypotheses']) == {'medium.en', 'large-v3'}


@pytest.mark.parametrize('options,status', [({'enabled': False}, 'disabled'),
                                         ({'supplied_reference': True}, 'supplied-reference-preserved')])
def test_review_off_and_supplied_human_text_never_call_models(options, status):
    original = [cue('Human correction — 雨\nsecond line')]
    adapter = Mock(side_effect=AssertionError('Forbidden recognition'))
    result = recovery.refine(original, 30, adapter, **options)
    assert result['cues'] == original and result['cues'] is not original
    assert result['report']['status'] == status
    adapter.assert_not_called()


def test_good_primary_without_suspects_does_not_call_models():
    original = [cue('A genuine lyric', weak=False)]
    adapter = Mock()
    result = recovery.refine(original, 30, adapter)
    assert result['cues'] == original and result['report']['status'] == 'not-needed'
    adapter.assert_not_called()


def test_genuine_signoff_is_never_deleted_by_artifact_flag_or_empty_models():
    original = [cue('Thanks for listening.', weak=False)]
    def run(engine, start, end): return hypothesis([], start, end, segments=[])
    result = recovery.refine(original, 30, run)
    assert result['cues'] == original
    assert 'artifact-review-flag' in result['report']['regions'][0]['reasons']
    assert not result['report']['edits']


@pytest.mark.parametrize('field,value,reason', [('no_speech_prob', .9, 'decoder-no-speech'),
                                             ('avg_logprob', -1.3, 'decoder-low-confidence'),
                                             ('compression_ratio', 3, 'decoder-repetition')])
def test_raw_decoder_warnings_schedule_review_even_with_high_word_scores(field, value, reason):
    original = [cue('Ordinary words', weak=False)]
    raw = {'segments': [{'start': 10, 'end': 12, field: value}]}
    result = recovery.refine(original, 30, adapter_for(), raw)
    assert reason in result['report']['regions'][0]['reasons']
    assert result['report']['budget']['recognizer_calls'] == 2


def test_full_region_limits_are_enforced_without_truncation():
    original = [cue(start=10 + n * 40, end=12 + n * 40) for n in range(6)]
    adapter = adapter_for()
    result = recovery.refine(original, 250, adapter)
    budget = result['report']['budget']
    assert budget['selected_regions'] == 4 and adapter.call_count == 8
    assert budget['selected_audio_seconds'] == 24
    assert all(0 < call.args[2] - call.args[1] <= 30 for call in adapter.call_args_list)
    assert all(r['status'] == 'held' for r in result['report']['regions'][4:])
    assert result['cues'][4:] == original[4:]


def test_total_audio_limit_counts_two_passes_separately_in_documented_budget():
    original = [cue(start=2 + n * 50, end=28 + n * 50) for n in range(4)]
    adapter = adapter_for()
    result = recovery.refine(original, 200, adapter)
    assert result['report']['budget']['selected_audio_seconds'] == 60
    assert result['report']['budget']['selected_regions'] == 2
    assert adapter.call_count == 4  # Two models each inspect the same60s.
    assert result['cues'][2:] == original[2:]


def test_merged_region_over_thirty_seconds_is_held_not_split_or_clipped():
    original = [cue(start=1, end=17), cue(start=17, end=32)]
    adapter = Mock()
    result = recovery.refine(original, 60, adapter)
    adapter.assert_not_called()
    assert result['cues'] == original and len(result['report']['regions']) == 1
    assert result['report']['regions'][0]['end'] == 34
    assert 'not truncated or split' in result['report']['regions'][0]['hold_reason']


@pytest.mark.parametrize('change', [{'start': float('nan')}, {'end': 40}, {'start': 11, 'end': 10},
                                   {'start': 8}, {'end': 13}])
def test_unlocatable_primary_word_is_held_without_guessing_window(change):
    original = [cue()]
    original[0]['words'][0].update(change)
    adapter = Mock()
    result = recovery.refine(original, 30, adapter)
    adapter.assert_not_called()
    assert result['report']['unresolved'] and not result['report']['regions']
    assert result['cues'][0]['text'] == original[0]['text']


def test_zero_duration_word_inside_locatable_cue_can_receive_bounded_second_opinion():
    original = [cue()]
    original[0]['words'][0]['end'] = 10
    adapter = adapter_for([word('clear', 10.2, 11)])
    result = recovery.refine(original, 30, adapter)
    assert adapter.call_count == 2 and result['cues'][0]['text'] == 'clear'
    assert result['cues'][0]['needs_review'] is True


@pytest.mark.parametrize('error', [FileNotFoundError('missing model'), RuntimeError('GPU busy'),
                                 PermissionError('backend unavailable')])
def test_adapter_failure_holds_without_losing_evidence_or_starting_second_model(error):
    adapter = Mock(side_effect=error)
    original = [cue()]
    raw = {'segments': [{'text': 'Original evidence', 'start': 10, 'end': 12}]}
    result = recovery.refine(original, 30, adapter, raw)
    assert adapter.call_count == 1 and result['cues'] == original
    assert result['report']['primary_evidence'] == raw
    assert result['report']['regions'][0]['adapter_error']['type'] == type(error).__name__


@pytest.mark.parametrize('change', [
    {'fresh': False}, {'fresh': None}, {'fresh': 'true'},
    {'segments': []}, {'segments': [{'start': 8, 'end': 14}]},
])
def test_missing_freshness_or_decoder_confidence_cannot_corroborate(change):
    original = [cue()]
    def run(engine, start, end):
        result = hypothesis([word('clear', 10, 11)], start, end)
        if engine == 'large-v3': result.update(change)
        return result
    result = recovery.refine(original, 30, run)
    assert result['cues'] == original and not result['report']['edits']


@pytest.mark.parametrize('words', [
    [word('one', 10, 11), word('two', 10.9, 12)],
    [word('one', 7.9, 9)], [word('one', 13, 14.1)],
    [word('one', 10, 11, .2)], [word('one', 10, 11, .7)],
    [word('one', 10, 10)], [word('one', float('nan'), 11)],
])
def test_unreliable_or_overlapping_hypothesis_is_never_adopted(words):
    original = [cue()]
    result = recovery.refine(original, 30, adapter_for(words))
    assert result['cues'] == original and not result['report']['edits']


def test_words_crossing_unchanged_neighbor_are_not_clipped_or_adopted():
    original = [cue('before', 0, .8, weak=False), cue('wrong', 1, 2),
                cue('after', 2.2, 3, weak=False)]
    words = [word('crossing', .7, 1.2), word('boundary', 1.3, 2)]
    result = recovery.refine(original, 10, adapter_for(words))
    assert result['cues'] == original and not result['report']['edits']
    assert 'neighbors' in result['report']['unresolved'][0]['reason']


def test_injected_whole_edit_guard_can_reject_without_changing_text():
    original = [cue()]
    guard = Mock(return_value=False)
    result = recovery.refine(original, 30, adapter_for(), validate_edit=guard)
    assert result['cues'] == original and not result['report']['edits']
    assert guard.call_count == 1
    assert guard.call_args.args[0][0]['needs_review'] is True


def test_merged_cues_use_reviewed_predecessor_and_unchanged_successor():
    original = [cue('left wrong', 1, 2), cue('right wrong', 2.5, 3.5),
                cue('fixed', 4, 5, weak=False)]
    words = [word('left', 1.1, 2.2), word('right', 2.6, 3.4), word('fixed', 4.1, 4.8)]
    observations = []
    def guard(proposal, duration, before, after):
        observations.append(deepcopy((before, after)))
        return recovery._safe_edit(proposal, duration, before, after)
    result = recovery.refine(original, 10, adapter_for(words), validate_edit=guard)
    assert len(result['report']['regions']) == 1
    assert [c['text'] for c in result['cues']] == ['left', 'right', 'fixed']
    assert observations[0][1] == original[1]
    assert observations[1][0] == result['cues'][0]
    assert observations[1][0]['end'] == 2.2
    assert result['cues'][2] == original[2]


def test_first_edit_cannot_cross_successor_even_if_successor_also_suspect():
    original = [cue('left wrong', 1, 2), cue('right wrong', 2.5, 3.5),
                cue('fixed', 4, 5, weak=False)]
    words = [word('crossing', 1.1, 2.6), word('right', 2.7, 3.4)]
    result = recovery.refine(original, 10, adapter_for(words))
    assert result['cues'] == original
    assert not result['report']['edits']


def test_replacement_drops_old_approval_fields_and_does_not_inherit_alignment_score():
    original = [cue()]
    original[0].update(human_review={'status': 'approved'}, publication={'published': True},
                       approved=True, reviewed=True, machine_review={'old': True})
    original[0]['words'][0]['alignment_score'] = .99
    result = recovery.refine(original, 30, adapter_for())
    replacement = result['cues'][0]
    assert replacement['needs_review'] is True
    assert not {'human_review', 'publication', 'approved', 'reviewed', 'machine_review'} & replacement.keys()
    assert 'alignment_score' not in replacement['words'][0]
    assert original[0]['human_review']['status'] == 'approved'


def test_punctuation_case_normalization_can_agree_but_uses_actual_large_model_words():
    def run(engine, start, end):
        return hypothesis([word('Rain,' if engine == 'medium.en' else 'rain.', 10, 11)], start, end)
    result = recovery.refine([cue()], 30, run)
    assert result['cues'][0]['text'] == 'rain.' and result['cues'][0]['needs_review']


def test_invalid_duration_or_empty_transcript_does_not_call_models():
    adapter = Mock()
    for cues, duration in [([], 30), ([cue()], float('nan')), ([cue()], 901)]:
        result = recovery.refine(cues, duration, adapter)
        assert result['report']['status'] == 'held'
    adapter.assert_not_called()
