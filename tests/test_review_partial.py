"""Partial-token data and pure painter inputs; no models or media player created."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from gnb_player.lyrics import Cue, Word, LyricsDocument, word_is_active
from gnb_player.review_partial import partial_tokens, partial_issues, preserve_native_support
from gnb_player.review_passage import passage_alignment_input, merge_passage_alignment


def raw_partial():
    labels = ['He', 'orders', 'something', 'amber']
    spans = [(2, 2.24), (2.24, 2.64), (2.64, 3.2), (3.2, 4.08)]
    offset = 27.76
    words = [{'text': text, 'start': offset + a, 'end': offset + b, 'token_index': i,
              'region': 0, 'highlight_intervals': [[offset + a, offset + b]], 'highlight_allowed': True,
              'coordinates': 'source_audio_seconds', 'source_offset_seconds': offset,
              'native_clip_span': {'start': a, 'end': b}} for i, (text, (a, b)) in enumerate(zip(labels, spans))]
    issue = {'text': 'and', 'token_index': 4, 'native_indices': [4], 'region': 0,
             'native_constituents': [{'text': 'and', 'native_index': 4,
                 'raw_timestamp_ms': [4960, 4320], 'postprocessed_timestamp_ms': [4960, 4960],
                 'repaired_boundaries': [False, True], 'native_clip_span': {'start': 4.96, 'end': 4.96},
                 'flags': ['qwen_timestamp_repaired_review'], 'issues': ['zero_or_negative_native_span']}],
             'reasons': ['zero_or_negative_native_span'], 'status': 'unresolved', 'source_window': [27.76, 33.76]}
    return {'text': 'He orders something amber and', 'start': words[0]['start'], 'end': words[-1]['end'],
            'words': words, 'region': 0, 'source_token_indices': [0, 1, 2, 3, 4],
            'native_timing_issues': [issue], 'highlight_allowed': False, 'highlight_intervals': [],
            'timing_kind': 'partial_native_support_requires_review'}


def legacy_group():
    row = raw_partial()
    group = {'text': row['text'], 'start': row['start'], 'end': row['end'],
             'grouped_text': True, 'individual_word_boundaries': None,
             'highlight_intervals': [pair for w in row['words'] for pair in w['highlight_intervals']],
             'highlight_allowed': True, 'native_constituents': deepcopy(row['words']),
             'native_timing_issues_before_automatic_grouping': deepcopy(row['native_timing_issues']),
             'flags': ['automatic_supported_group_timing']}
    row['words'] = [group]
    row['resolved_native_timing_issues'] = row.pop('native_timing_issues')
    row.pop('highlight_allowed'); row.pop('highlight_intervals')
    row['timing_kind'] = 'automatic_group_existing_support'
    return row


def document(row):
    return LyricsDocument.from_dict({'duration': 60, 'source_sha256': 'a' * 64, 'segments': [row]})


def test_native_support_stays_separate_and_untimed_and_has_no_word():
    original = raw_partial()
    row = preserve_native_support([original])[0]
    assert original == raw_partial()
    assert row['words'] == original['words']
    assert row['text'] == original['text']
    assert partial_tokens(row) == [('He', 0), ('orders', 1), ('something', 2), ('amber', 3), ('and', None)]
    assert partial_issues(row) == original['native_timing_issues']
    assert all(w['text'] != 'and' for w in row['words'])
    assert preserve_native_support([row]) == [row]


def test_legacy_recovery_restores_exact_native_words_and_retains_original_group():
    original = legacy_group()
    before = deepcopy(original)
    row = preserve_native_support([original])[0]
    assert original == before
    assert row['words'] == before['words'][0]['native_constituents']
    assert row['partial_recovery']['previous_group'] == before['words'][0]
    assert row['native_timing_issues'] == before['resolved_native_timing_issues']
    assert 'resolved_native_timing_issues' not in row
    assert partial_tokens(row)[-1] == ('and', None)
    # Merely importing an old approved/grouped artifact never unpacks it.
    loaded = document(original)
    assert len(loaded.segments[0].words) == 1 and partial_tokens(loaded.segments[0]) is None


def test_manual_group_correction_remains_unchanged():
    row = legacy_group()
    row['timing_kind'] = 'operator_grouped_existing_support'
    row['words'][0]['flags'] = ['operator_grouped_timing_review']
    assert preserve_native_support([row]) == [row]
    assert partial_tokens(row) is None


def test_later_manual_timing_of_an_automatic_group_is_not_undone():
    row = legacy_group()
    row['words'][0]['flags'].append('manual_timing_review')
    row['words'][0]['timing_status'] = 'operator_edited_needs_review'
    row['start'] -= .2
    row['words'][0]['start'] -= .2
    assert preserve_native_support([row]) == [row]
    unchanged = legacy_group()
    unchanged['words'][0]['highlight_intervals'] = [[unchanged['start'], unchanged['end']]]
    with pytest.raises(ValueError, match='support changed'):
        preserve_native_support([unchanged])


def test_partial_roundtrip_and_clock_seeks_use_only_positive_word_intervals():
    row = preserve_native_support([raw_partial()])[0]
    doc = document(row)
    restored = LyricsDocument.from_dict(doc.to_dict())
    assert restored.to_dict() == doc.to_dict()
    assert partial_tokens(restored.segments[0]) == partial_tokens(row)
    for seconds, label in [(29.8, 'He'), (30.2, 'orders'), (30.7, 'something'),
                           (31.1, 'amber'), (29.8, 'He'), (31.84 + 1e-6, None), (32.72, None), (0, None)]:
        word = restored.active_word(seconds)
        assert (word.text if word else None) == label
    # Repeating a paused playback time never advances a hidden independent timer.
    assert restored.active_word(30.2) == restored.active_word(30.2)


def test_gap_support_is_not_filled_by_partial_text_or_render_mapping():
    row = preserve_native_support([raw_partial()])[0]
    row['words'][2]['highlight_intervals'] = [[row['words'][2]['start'], 30.5], [30.8, row['words'][2]['end']]]
    doc = document(row)
    assert doc.active_word(30.6) is None and doc.active_cue(30.6) is None
    assert doc.active_word(30.85).text == 'something'
    doc.metadata['confirmed_instrumental_gaps'] = [[31.0, 31.2]]
    assert doc.active_word(31.1) is None


def test_renderer_pure_layout_preserves_full_line_and_has_no_underline_target_for_and():
    from gnb_player.stage import lyric_token_words
    row = preserve_native_support([raw_partial()])[0]
    pairs = lyric_token_words(row)
    assert [text for text, _ in pairs] == row['text'].split()
    assert pairs[-1] == ('and', None)
    for seconds, wanted in [(29.8, 'He'), (30.2, 'orders'), (31.1, 'amber'), (32.72, None)]:
        active = [text for text, word in pairs if word is not None and word_is_active(word, seconds)]
        assert active == ([] if wanted is None else [wanted])
    assert [word for _, word in lyric_token_words(document(row).segments[0])][-1] is None


@pytest.mark.parametrize('fault', ['foreign_text', 'duplicate_index', 'missing_issue', 'wrong_issue_text',
    'extra_issue', 'invalid_positive', 'positive_order', 'outside_support', 'valid_untimed', 'missing_native',
    'fake_no_acoustic', 'bad_window', 'missing_marker_index'])
def test_malformed_partial_mapping_is_rejected_without_import_bypass(fault):
    row = preserve_native_support([raw_partial()])[0]
    if fault == 'foreign_text': row['partial_timing']['tokens'][0]['text'] = 'Other'
    elif fault == 'duplicate_index': row['partial_timing']['tokens'][1]['word_index'] = 0
    elif fault == 'missing_issue': row['native_timing_issues'] = []
    elif fault == 'wrong_issue_text': row['native_timing_issues'][0]['text'] = 'other'
    elif fault == 'extra_issue': row['native_timing_issues'].append(deepcopy(row['native_timing_issues'][0]))
    elif fault == 'invalid_positive': row['words'][0]['end'] = row['words'][0]['start']
    elif fault == 'positive_order': row['words'].reverse()
    elif fault == 'outside_support': row['words'][0]['highlight_intervals'] = [[0, 40]]
    elif fault == 'valid_untimed':
        native = row['native_timing_issues'][0]['native_constituents'][0]
        native['native_clip_span'] = {'start': 4.32, 'end': 4.96}
        native['postprocessed_timestamp_ms'] = [4320, 4960]
    elif fault == 'missing_native': row['native_timing_issues'][0]['native_constituents'] = []
    elif fault == 'fake_no_acoustic':
        issue = row['native_timing_issues'][0]
        issue.update(native_constituents=[], native_indices=[], reasons=['no_acoustic_tokens'])
    elif fault == 'bad_window': row['native_timing_issues'][0]['source_window'] = [0, 1]
    else: row['partial_timing']['tokens'][4].pop('word_index')
    with pytest.raises(ValueError): partial_tokens(row)
    with pytest.raises(ValueError): document(row)


def test_legacy_group_with_mismatched_corrected_text_is_not_unpacked():
    row = legacy_group()
    row['text'] = row['words'][0]['text'] = 'He orders something different and'
    with pytest.raises(ValueError, match='current supplied'):
        preserve_native_support([row])


def test_selected_passage_accepts_only_exact_partial_provenance_and_preserves_neighbors():
    from test_review_passage import document as original_document, candidate
    original = original_document()
    original.duration = 60
    original.segments[1] = Cue('He orders something amber and', 29.76, 31.76,
        [Word('He orders something amber and', 29.76, 31.76, {'grouped_text': True})], {'region': 2})
    original.segments[2].start, original.segments[2].end = 45, 47
    request = passage_alignment_input(original, 1)
    new = candidate(original, request)
    row = preserve_native_support([raw_partial()])[0]
    new['segments'] = [row]
    new['provenance']['unplaced'] = deepcopy(row['native_timing_issues'])
    before = original.to_dict()
    merged = merge_passage_alignment(original, new, request)
    assert original.to_dict() == before
    assert merged.to_dict()['segments'][0] == before['segments'][0]
    assert merged.to_dict()['segments'][2] == before['segments'][2]
    assert [w.text for w in merged.segments[1].words] == ['He', 'orders', 'something', 'amber']
    assert partial_tokens(merged.segments[1])[-1] == ('and', None)
    assert partial_issues(merged.segments[1])[0]['region'] == 2
    assert merged.metadata['reviewed'] is False
    for bad in [[], [dict(new['provenance']['unplaced'][0], text='extra')]]:
        invalid = deepcopy(new); invalid['provenance']['unplaced'] = bad
        with pytest.raises(ValueError, match='partial'):
            merge_passage_alignment(original, invalid, request)


def test_worker_compatibility_name_preserves_four_words(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'src/gnb_player'))
    from review_qwen_worker import _automatic_groups
    result = _automatic_groups([raw_partial()])[0]
    assert len(result['words']) == 4 and partial_tokens(result)[-1] == ('and', None)


def test_frame_validator_skips_ordinary_words_and_never_copies_native_history():
    class NoCopy:
        def __deepcopy__(self, memo):
            raise AssertionError('Frame validation must not deepcopy native history')
    class OrdinaryCue:
        metadata = {}
        @property
        def words(self):
            raise AssertionError('Ordinary fast path must not access words')
    assert partial_tokens(OrdinaryCue()) is None
    assert partial_tokens({'text': 'ordinary'}) is None
    cue = document(preserve_native_support([raw_partial()])[0]).segments[0]
    cue.words[0].metadata['native_history'] = NoCopy()
    assert partial_tokens(cue)[-1] == ('and', None)


def test_saved_owner_failure_exact_native_support_and_legacy_recovery():
    folder = Path('/home/kaotickj/Documents/Codex/builds/goodneighbor-broadcast-player/user-data-plague-radio/lyrics-review/7af7d12beed49681f9d88327c25a5e3c852f3de7ea358267ec5fb47e9191bbc8/runs/4028b8689d354c70961b93540d045574')
    if not folder.is_dir(): pytest.skip('Immutable owner run is local evidence, not distributed test audio.')
    native = json.loads((folder / 'qwen-align.json').read_text())
    grouped = json.loads((folder / 'candidate.json').read_text())
    fresh = preserve_native_support(native['segments'])[0]
    recovered = preserve_native_support(grouped['segments'])[0]
    assert fresh['words'] == native['segments'][0]['words'] == recovered['words']
    assert len(fresh['words']) == 4
    assert partial_tokens(fresh) == partial_tokens(recovered)
    assert partial_issues(fresh) == native['unplaced'] == partial_issues(recovered)
    assert fresh['text'] == 'He orders something amber and'
