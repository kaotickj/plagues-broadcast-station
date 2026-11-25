"""Explicit whole-track acceptance; saved evidence only, no Qt/audio/models."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from gnb_player.lyrics import Cue, Word, LyricsDocument
from gnb_player.review_acceptance import (
    acceptance_requirements, acceptance_request, validate_acceptance, acceptance_message)

SNAPSHOT = Path(__file__).resolve().parents[2] / 'evidence' / 'owner-saved-draft-readonly.json'


def ordinary():
    return LyricsDocument([Cue('Good words', 1, 3,
        [Word('Good', 1, 2), Word('words', 2, 3)])], 10, 'Fixture', 'a' * 64)


def native_hold(text='lost'):
    native = {'text': text, 'native_index': 2, 'raw_timestamp_ms': [4000, 3000],
              'postprocessed_timestamp_ms': [4000, 4000], 'repaired_boundaries': [False, True],
              'native_clip_span': {'start': 4, 'end': 4}, 'flags': [],
              'issues': ['zero_or_negative_native_span']}
    evidence = {'text': text, 'token_index': 2, 'native_indices': [2], 'region': 0,
                'native_constituents': [native], 'reasons': ['zero_or_negative_native_span'],
                'status': 'unresolved', 'source_window': [0, 10]}
    return {'region': 0, 'start': 0, 'end': 10, 'text': text, 'status': 'held',
            'detector': 'unplaced_native_word', 'native_evidence': evidence}


def with_hold():
    doc = ordinary(); doc.segments[0].metadata['region'] = 0
    region = {'start': 0, 'end': 10, 'text': 'Good words lost'}
    doc.metadata.update(held_passages=[native_hold()], recognition_regions=[region],
                        alignment_windows=[deepcopy(region)], transcription_only=False)
    return doc


def pending_name(doc):
    doc.metadata['vocabulary_review'] = {'source_sha256': doc.source_sha256,
        'suggestions': [{'id': 'fixture-suggestion', 'status': 'pending', 'cue_index': 0,
            'cue_text': 'Good words', 'start_char': 0, 'end_char': 4,
            'match_text': 'Good', 'replacement': 'Better'}]}
    return doc


def blockers(doc):
    return {b['code'] for b in acceptance_requirements(doc)['blocker_details']}


def test_actual_owner_snapshot_acknowledges_only_three_omissions_without_mutation():
    original = json.loads(SNAPSHOT.read_text()); doc = LyricsDocument.from_dict(original)
    before = deepcopy(doc.to_dict()); requirements = acceptance_requirements(doc)
    assert len(doc.segments) == 62 and not requirements['blockers']
    assert [r['text'] for r in requirements['missing_native_holds']] == ['knows', 'where', 'Here.']
    assert not requirements['partial_unhighlighted_words'] and not requirements['pending_name_suggestions']
    request = acceptance_request(doc)
    assert requirements['requires_confirmation'] and validate_acceptance(doc, request)
    assert doc.to_dict() == before
    restored = LyricsDocument.from_dict(json.loads(json.dumps(before)))
    assert validate_acceptance(restored, request)
    assert "'knows'" in acceptance_message(doc) and 'omitted' in acceptance_message(doc)


def test_ordinary_advisory_flags_do_not_require_extra_confirmation():
    doc = ordinary()
    doc.segments[0].words[0].metadata['flags'] = ['long_span_review', 'qwen_timestamp_repaired_review']
    requirements = acceptance_requirements(doc)
    assert not requirements['blockers'] and not requirements['requires_confirmation']
    assert validate_acceptance(doc, acceptance_request(doc))


def test_genuine_line_only_subtitle_remains_allowed():
    doc = ordinary(); doc.segments[0].words = []
    assert not acceptance_requirements(doc)['blockers']
    assert validate_acceptance(doc, acceptance_request(doc))


def test_known_empty_instrumental_allowed_without_invented_words():
    doc = ordinary(); doc.segments = []; doc.metadata['instrumental'] = True
    assert not acceptance_requirements(doc)['blockers']


@pytest.mark.parametrize('change', ['text', 'timing', 'source', 'hold', 'vocab', 'extra', 'missing', 'wrong_type'])
def test_acceptance_cannot_transfer_or_cover_changed_evidence(change):
    doc = with_hold(); request = acceptance_request(doc)
    if change == 'text': doc.segments[0].text = 'Different text'
    elif change == 'timing': doc.segments[0].end = 3.5
    elif change == 'source': doc.source_sha256 = 'b' * 64
    elif change == 'hold': doc.metadata['held_passages'][0]['reason'] = 'Changed evidence'
    elif change == 'vocab': pending_name(doc)
    elif change == 'extra': request['missing_native_holds'].append('extra')
    elif change == 'missing': request['missing_native_holds'] = []
    else: request['pending_name_suggestions'] = False
    with pytest.raises(ValueError): validate_acceptance(doc, request)


def test_retarget_quality_and_review_bookkeeping_do_not_change_request():
    doc = with_hold(); request = acceptance_request(doc)
    doc.quality = {'status': 'review', 'reasons': [{'code': 'human_revision'}]}
    doc.human_review = {'status': 'draft', 'reason': 'retargeted'}
    doc.metadata.update(review_original_quality={'status': 'old'}, reviewed=False)
    assert validate_acceptance(doc, request)


@pytest.mark.parametrize('change', ['unknown_detector', 'repetition', 'decoder', 'status', 'missing_native',
    'text', 'window', 'native_window', 'positive_span', 'missing_measurement', 'fake_reason', 'bool_region'])
def test_only_exact_proven_native_omissions_are_eligible(change):
    doc = with_hold(); hold = doc.metadata['held_passages'][0]; evidence = hold['native_evidence']
    item = evidence['native_constituents'][0]
    if change == 'unknown_detector': hold['detector'] = 'future_detector'
    elif change == 'repetition': hold['detector'] = 'consecutive_repetition'
    elif change == 'decoder': hold['detector'] = 'decoder_termination'
    elif change == 'status': hold['status'] = 'operator_requested_alignment'
    elif change == 'missing_native': hold.pop('native_evidence')
    elif change == 'text': evidence['text'] = 'different'
    elif change == 'window': hold['end'] = 9
    elif change == 'native_window': evidence['source_window'] = [0, float('inf')]
    elif change == 'positive_span': item['native_clip_span']['end'] = 5; item['postprocessed_timestamp_ms'][1] = 5000
    elif change == 'missing_measurement': item.pop('raw_timestamp_ms')
    elif change == 'fake_reason': evidence['reasons'] = ['zero_or_negative_native_span', 'unknown']
    else: hold['region'] = True
    assert acceptance_requirements(doc)['blockers']
    with pytest.raises(ValueError): acceptance_request(doc)


@pytest.mark.parametrize('change', ['no_cues', 'wrong_region', 'missing_second_region', 'outside', 'recognition_only'])
def test_whole_missing_sections_cannot_be_accepted_as_individual_words(change):
    doc = with_hold()
    if change == 'no_cues': doc.segments = []
    elif change == 'wrong_region': doc.segments[0].metadata['region'] = 1
    elif change == 'missing_second_region':
        doc.duration = 20
        region = {'start': 10, 'end': 20, 'text': 'A whole missing section'}
        doc.metadata['recognition_regions'].append(region)
        doc.metadata['alignment_windows'].append(deepcopy(region))
    elif change == 'outside': doc.segments[0].start = -1
    else: doc.metadata['transcription_only'] = True
    assert acceptance_requirements(doc)['blockers']


def test_empty_recognition_region_remapping_is_supported():
    doc = with_hold(); doc.duration = 20
    doc.metadata['recognition_regions'].insert(0, {'start': 10, 'end': 20, 'text': ''})
    assert not acceptance_requirements(doc)['blockers']


@pytest.mark.parametrize('change', ['zero', 'nan', 'negative', 'overlap', 'outside', 'bad_interval', 'native_issue'])
def test_invalid_timing_cannot_be_acknowledged(change):
    doc = ordinary()
    if change == 'native_issue': doc.segments[0].metadata['native_timing_issues'] = [{'text': 'unknown'}]
    elif change == 'bad_interval': doc.segments[0].words[0].metadata['highlight_intervals'] = [[1, 4]]
    else:
        values = {'zero': (1, 1), 'nan': (float('nan'), 2), 'negative': (-1, 2), 'overlap': (1, 2.5), 'outside': (1, 4)}
        a, b = values[change]; doc.segments[0].words[0] = Word('Good', a, b)
    assert acceptance_requirements(doc)['blockers']
    with pytest.raises(ValueError): acceptance_request(doc)


def test_pending_names_require_explicit_keep_without_substitution():
    doc = pending_name(ordinary()); original = deepcopy(doc.to_dict())
    requirements = acceptance_requirements(doc); request = acceptance_request(doc)
    assert not requirements['blockers'] and requirements['requires_confirmation']
    assert len(requirements['pending_name_suggestions']) == 1
    assert request['name_decision'] == 'keep_current_words_unchanged'
    assert validate_acceptance(doc, request) and doc.to_dict() == original
    request['name_decision'] = 'accept_all_replacements'
    with pytest.raises(ValueError): validate_acceptance(doc, request)


@pytest.mark.parametrize('change', ['source', 'cue_text', 'duplicate', 'status'])
def test_stale_or_unknown_name_evidence_is_not_batch_acknowledged(change):
    doc = pending_name(ordinary()); state = doc.metadata['vocabulary_review']
    if change == 'source': state['source_sha256'] = 'b' * 64
    elif change == 'cue_text': state['suggestions'][0]['cue_text'] = 'Stale'
    elif change == 'duplicate': state['suggestions'].append(deepcopy(state['suggestions'][0]))
    else: state['suggestions'][0]['status'] = 'unknown'
    assert acceptance_requirements(doc)['blockers']


def test_unknown_and_malformed_holds_remain_blockers_alongside_eligible_hold():
    doc = with_hold(); doc.metadata['held_passages'].append({'status': 'held', 'detector': 'unknown'})
    requirements = acceptance_requirements(doc)
    assert len(requirements['missing_native_holds']) == 1 and requirements['blockers']
    doc.metadata['held_passages'] = 'not rows'
    assert acceptance_requirements(doc)['blockers']


def partial_document():
    from gnb_player.review_partial import preserve_native_support
    evidence = native_hold('lost')['native_evidence']
    evidence['token_index'] = 1
    row = {'start': 1, 'end': 3, 'text': 'Good lost words', 'region': 0,
           'source_token_indices': [0, 1, 2], 'native_timing_issues': [evidence],
           'words': [{'text': 'Good', 'start': 1, 'end': 2, 'region': 0, 'token_index': 0},
                     {'text': 'words', 'start': 2, 'end': 3, 'region': 0, 'token_index': 2}]}
    return LyricsDocument.from_dict({'duration': 10, 'source_sha256': 'a' * 64,
                                    'segments': preserve_native_support([row])})


def test_valid_partial_words_are_visible_acknowledgements_not_omissions():
    doc = partial_document(); before = deepcopy(doc.to_dict())
    requirements = acceptance_requirements(doc)
    assert not requirements['blockers'] and requirements['requires_confirmation']
    assert not requirements['missing_native_holds']
    assert [r['text'] for r in requirements['partial_unhighlighted_words']] == ['lost']
    assert validate_acceptance(doc, acceptance_request(doc)) and doc.to_dict() == before
    assert 'without individual highlighting' in acceptance_message(doc)


def test_partial_marker_cannot_reclassify_invalid_words_as_valid():
    doc = partial_document(); doc.segments[0].words[0] = Word('Good', 1, 1, {'region': 0, 'token_index': 0})
    assert acceptance_requirements(doc)['blockers']
    with pytest.raises(ValueError): acceptance_request(doc)


def test_actual_saved_groups_can_be_acknowledged_after_explicit_native_recovery():
    from gnb_player.review_partial import preserve_native_support
    raw = json.loads(SNAPSHOT.read_text())
    raw['segments'] = preserve_native_support(raw['segments'])
    doc = LyricsDocument.from_dict(raw); requirements = acceptance_requirements(doc)
    assert not requirements['blockers']
    assert len(requirements['missing_native_holds']) == 3
    assert requirements['partial_unhighlighted_words']
    request = acceptance_request(doc)
    assert validate_acceptance(LyricsDocument.from_dict(doc.to_dict()), request)
    request['partial_unhighlighted_words'] = []
    with pytest.raises(ValueError): validate_acceptance(doc, request)


def test_native_word_spelling_and_indices_must_match_held_evidence():
    doc = with_hold()
    doc.metadata['held_passages'][0]['native_evidence']['native_constituents'][0]['text'] = 'other'
    assert acceptance_requirements(doc)['blockers']
