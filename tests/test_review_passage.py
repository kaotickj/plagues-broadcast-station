"""Synthetic passage merges only: no model, audio, player or file-save actions."""
from copy import deepcopy
import json

import pytest

from gnb_player.lyrics import Cue, Word, LyricsDocument
from gnb_player.lyric_review import document_revision, is_approved
from gnb_player.review_core import edit_phrase
from gnb_player.review_passage import passage_alignment_input, merge_passage_alignment


def document():
    return LyricsDocument([
        Cue('old chorus', 3, 5, [Word('old chorus', 3, 5, {
            'highlight_intervals': [[3, 3.5], [4.5, 5]], 'grouped_text': True,
            'individual_word_boundaries': None, 'owner_listening_accepted': True})],
            {'region': 2, 'owner_note': 'Keep this accepted group exactly.'}),
        Cue('right names', 10, 12, [Word('right', 10, 10.5), Word('names', 11, 12)],
            {'region': 2, 'owner_listening_accepted': True}),
        Cue('next line', 20, 23, [Word('next', 20, 21), Word('line', 22, 23, {
            'highlight_allowed': False, 'highlight_intervals': [], 'flags': ['owner_muted']})],
            {'region': 3, 'other_metadata': {'must': ['remain', 'unchanged']}}),
    ], 30, 'Synthetic fixture', 'a' * 64,
        metadata={'confirmed_instrumental_gaps': [[6, 8], [15, 18]],
                  'provenance': {'mode': 'extract', 'raw_evidence': '/original/evidence'},
                  'alignment_windows': [{'start': 0, 'end': 15, 'text': 'old chorus right names'},
                                        {'start': 18, 'end': 30, 'text': 'next line'}]},
        human_review={'status': 'draft'})


def serialized(value):
    return json.dumps(value.to_dict() if isinstance(value, LyricsDocument) else value,
                      ensure_ascii=False, sort_keys=True, allow_nan=False)


def candidate(original, request, *, split=False, grouped=False):
    start = original.segments[request['passage_scope']['cue_index']].start
    end = original.segments[request['passage_scope']['cue_index']].end
    tokens = request['reference_text'].split()
    offset = request['alignment_windows'][0]['start']
    if grouped:
        labels = [request['reference_text']]
        spans = [(start, end)]
    else:
        labels = tokens
        unit = (end - start) / len(labels)
        spans = [(start + i * unit, start + (i + .75) * unit) for i in range(len(labels))]
    words = []
    for index, (text, (a, b)) in enumerate(zip(labels, spans)):
        intervals = [[a, b]] if not grouped else [[a, a + (b - a) / 4], [b - (b - a) / 4, b]]
        words.append({'text': text, 'start': a, 'end': b,
            'highlight_intervals': intervals, 'highlight_allowed': True,
            'coordinates': 'source_audio_seconds', 'source_offset_seconds': offset,
            'native_clip_span': {'start': a - offset, 'end': b - offset},
            'region': 0, 'token_index': index, 'flags': ['fixture_native_review'],
            **({'grouped_text': True, 'individual_word_boundaries': None,
                'native_constituents': [{'evidence': 'synthetic grouped support'}]} if grouped else {})})
    chunks = [[word] for word in words] if split else [words]
    rows = [{'text': ' '.join(w['text'] for w in group), 'start': group[0]['start'], 'end': group[-1]['end'],
             'words': group, 'region': 0, 'source_token_indices': [w['token_index'] for w in group]}
            for group in chunks]
    return {'source_sha256': original.source_sha256, 'duration': original.duration,
            'title': 'New private passage result', 'segments': rows,
            'alignment_windows': deepcopy(request['alignment_windows']),
            'held_passages': [], 'reviewed': False, 'quality': {'status': 'review'},
            'warnings': ['Synthetic new evidence'],
            'provenance': {'engine': 'qwen', 'mode': 'align', 'raw_evidence': '/new/passage/evidence',
                'alignment_source': request['alignment_source'], 'replacement_scope': request['replacement_scope'],
                'replaces_revision': request['replaces_revision'], 'unplaced': [],
                'model_evidence': {'model_sha256': 'b' * 64}}}


def test_request_selects_current_edited_text_with_context_not_onset_hint():
    original = edit_phrase(document(), 1, 'Hancock names')
    request = passage_alignment_input(original, 1)
    assert request['reference_text'] == request['reference_original'] == 'Hancock names'
    assert request['alignment_source'] == 'draft_passage'
    assert request['replacement_scope'] == 'selected_passage'
    assert request['replaces_revision'] == document_revision(original)
    assert request['alignment_windows'][0]['start'] == 8
    assert request['alignment_windows'][0]['end'] == 14
    assert request['passage_scope']['cue_index'] == 1
    assert request['passage_scope']['replacement_start'] == 5
    assert request['passage_scope']['replacement_end'] == 20
    assert 'onset' not in request and 'first_word_start' not in request


@pytest.mark.parametrize('index', [0, 2])
def test_first_and_last_passages_clip_context_to_recording(index):
    original = document()
    if index == 0:
        original.segments[0] = Cue('opening', .25, 2)
    else:
        original.segments[2] = Cue('ending', 28, 29.75)
    request = passage_alignment_input(original, index)
    assert request['alignment_windows'][0]['start' if index == 0 else 'end'] == (0 if index == 0 else 30)
    merged = merge_passage_alignment(original, candidate(original, request), request)
    assert merged.segments[index].text == original.segments[index].text


def test_180_second_limit_reduces_context_instead_of_splitting_words():
    original = LyricsDocument([Cue('supplied words', 5, 184)], 200, source_sha256='a' * 64)
    request = passage_alignment_input(original, 0)
    region = request['alignment_windows'][0]
    assert (region['start'], region['end']) == (4.5, 184.5)
    original.segments[0].end = 186
    with pytest.raises(ValueError, match='180-second'):
        passage_alignment_input(original, 0)


@pytest.mark.parametrize('index', [True, -1, 3, None])
def test_invalid_selection_cannot_trigger_whole_track_alignment(index):
    with pytest.raises(ValueError):
        passage_alignment_input(document(), index)


@pytest.mark.parametrize('span', [(10, 10), (12, 10), (-1, 12), (10, 31), (float('nan'), 12)])
def test_invalid_old_cue_has_no_guessed_window(span):
    original = document()
    original.segments[1].start, original.segments[1].end = span
    with pytest.raises(ValueError, match='span'):
        passage_alignment_input(original, 1)


def test_line_only_old_cue_still_has_explicit_search_span():
    original = document()
    original.segments[1].words = []
    assert passage_alignment_input(original, 1)['alignment_windows'][0]['start'] == 8
    original.segments[1].start = 4
    with pytest.raises(ValueError, match='neighbor'):
        passage_alignment_input(original, 1)


def test_merge_is_source_timed_and_keeps_all_neighbors_and_caller_objects_exact():
    original = document()
    request = passage_alignment_input(original, 1)
    new = candidate(original, request, split=True)
    before, raw_before, request_before = serialized(original), serialized(new), serialized(request)
    merged = merge_passage_alignment(original, new, request)
    assert serialized(original) == before and serialized(new) == raw_before and serialized(request) == request_before
    assert len(merged.segments) == 4
    assert merged.to_dict()['segments'][0] == original.to_dict()['segments'][0]
    assert merged.to_dict()['segments'][3] == original.to_dict()['segments'][2]
    assert merged.segments[1].start == new['segments'][0]['start'] == 10  # not 10 + window offset 8
    assert merged.metadata['confirmed_instrumental_gaps'] == original.metadata['confirmed_instrumental_gaps']
    assert merged.metadata['provenance'] == original.metadata['provenance']
    assert all(c.metadata['region'] == 2 for c in merged.segments[1:3])
    assert all(w.metadata['region'] == 2 and w.metadata['passage_local_region'] == 0 for c in merged.segments[1:3] for w in c.words)
    event = merged.metadata['passage_alignment_history'][-1]
    assert event['candidate'] == new and event['before']['start'] == 10
    assert event['before']['owner_listening_accepted'] is True
    assert not is_approved(merged) and merged.metadata['reviewed'] is False


def test_grouped_candidate_keeps_native_intervals_and_unknown_internal_boundaries():
    original = document()
    request = passage_alignment_input(original, 1)
    new = candidate(original, request, grouped=True)
    merged = merge_passage_alignment(original, LyricsDocument.from_dict(new), request)
    word = merged.segments[1].words[0]
    assert word.metadata['grouped_text'] and word.metadata['individual_word_boundaries'] is None
    assert word.metadata['highlight_intervals'] == [[10, 10.5], [11.5, 12]]
    assert word.metadata['native_constituents'] == [{'evidence': 'synthetic grouped support'}]


@pytest.mark.parametrize('neighbor', ['previous', 'next'])
def test_new_timing_crossing_neighbor_is_rejected_without_clipping_or_shifting(neighbor):
    original = document()
    if neighbor == 'previous':
        original.segments[0].end = 9.75
    else:
        original.segments[2].start = 12.1
    request = passage_alignment_input(original, 1)
    new = candidate(original, request, grouped=True)
    word = new['segments'][0]['words'][0]
    if neighbor == 'previous':
        new['segments'][0]['start'] = word['start'] = 9.5
        word['native_clip_span']['start'] = 1.5
        word['highlight_intervals'][0][0] = 9.5
    else:
        new['segments'][0]['end'] = word['end'] = 12.25
        word['native_clip_span']['end'] = 4.25
        word['highlight_intervals'][-1][1] = 12.25
    before = serialized(original)
    with pytest.raises(ValueError, match=neighbor):
        merge_passage_alignment(original, new, request)
    assert serialized(original) == before


@pytest.mark.parametrize('change', ['source', 'duration', 'revision', 'scope', 'window', 'engine', 'offset'])
def test_wrong_job_or_track_is_rejected(change):
    original = document()
    request = passage_alignment_input(original, 1)
    new = candidate(original, request)
    if change == 'source':
        new['source_sha256'] = 'c' * 64
    elif change == 'duration':
        new['duration'] += 1
    elif change == 'revision':
        new['provenance']['replaces_revision'] = 'stale'
    elif change == 'scope':
        new['provenance']['replacement_scope'] = 'whole_draft'
    elif change == 'window':
        new['alignment_windows'][0]['start'] += 1
    elif change == 'engine':
        new['provenance']['engine'] = 'other'
    else:
        new['segments'][0]['words'][0]['source_offset_seconds'] += 8
    before = serialized(original)
    with pytest.raises(ValueError):
        merge_passage_alignment(original, new, request)
    assert serialized(original) == before


def test_unsaved_edit_or_review_change_during_job_is_not_overwritten():
    original = document()
    request = passage_alignment_input(original, 1)
    new = candidate(original, request)
    edited = edit_phrase(original, 2, 'new neighbor text')
    with pytest.raises(ValueError, match='draft changed'):
        merge_passage_alignment(edited, new, request)
    changed_review = deepcopy(original)
    changed_review.human_review['reason'] = 'Approval state changed during the job'
    assert document_revision(changed_review) == document_revision(original)
    with pytest.raises(ValueError, match='review state'):
        merge_passage_alignment(changed_review, new, request)


@pytest.mark.parametrize('fault', ['hold', 'unplaced', 'native_issue', 'failed', 'no_words', 'missing_text', 'wrong_word', 'zero_word', 'word_order'])
def test_held_failed_or_incomplete_results_do_not_clobber_current_draft(fault):
    original = document()
    request = passage_alignment_input(original, 1)
    new = candidate(original, request)
    if fault == 'hold':
        new['held_passages'] = [{'region': 0, 'start': 8, 'end': 14, 'text': 'uncertain', 'status': 'held'}]
    elif fault == 'unplaced':
        new['provenance']['unplaced'] = [{'text': 'names', 'status': 'unresolved'}]
    elif fault == 'native_issue':
        new['segments'][0]['native_timing_issues'] = [{'text': 'names'}]
    elif fault == 'failed':
        new['completed'] = False
    elif fault == 'no_words':
        new['segments'][0]['words'] = []
    elif fault == 'missing_text':
        new['segments'][0]['text'] = 'right'
        new['segments'][0]['words'] = new['segments'][0]['words'][:1]
    elif fault == 'wrong_word':
        new['segments'][0]['words'][0]['text'] = 'wrong'
    elif fault == 'zero_word':
        new['segments'][0]['words'][0]['end'] = new['segments'][0]['words'][0]['start']
    else:
        new['segments'][0]['words'].reverse()
    before = serialized(original)
    with pytest.raises(ValueError):
        merge_passage_alignment(original, new, request)
    assert serialized(original) == before


def test_edited_text_and_undo_snapshot_survive_with_other_alignment_needs_reindexed():
    base = document()
    edited = edit_phrase(base, 1, 'Hancock names')
    edited.human_review['alignment_needed'] = [1, 2]
    undo_snapshot = deepcopy(edited)
    request = passage_alignment_input(edited, 1)
    merged = merge_passage_alignment(edited, candidate(edited, request, split=True), request)
    assert [c.text for c in merged.segments] == ['old chorus', 'Hancock', 'names', 'next line']
    assert merged.human_review['alignment_needed'] == [3]
    assert merged.metadata['edits'] == edited.metadata['edits']
    assert serialized(edited) == serialized(undo_snapshot)
    assert merged.metadata['passage_alignment_history'][-1]['before']['text'] == 'Hancock names'
    # Existing UI history can restore this deep snapshot; helper performs no saves.
    assert undo_snapshot.segments[1].start == 10 and undo_snapshot.segments[1].end == 12


def test_tampered_request_bounds_and_whole_track_scope_are_not_accepted():
    original = document()
    request = passage_alignment_input(original, 1)
    new = candidate(original, request)
    request['replacement_scope'] = 'whole_draft'
    with pytest.raises(ValueError, match='bounds or review state'):
        merge_passage_alignment(original, new, request)
