"""Optional selected-cue realignment, with no models, filesystem or UI effects.

The request uses an acoustic context window, not a presumed first-word onset.
New Qwen times are already on the recording timeline; merge never offsets them.
Only complete text coverage with validated positive support replaces the cue.
Untimed words remain explicit partial evidence, never invented timestamps.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

from .lyrics import LyricsDocument, validate_highlight_intervals, _review_state_digest
from .lyric_review import document_revision
from .review_core import mark_unreviewed, now
from .review_partial import partial_tokens, partial_issues
from .review_vocabulary import unresolved_suggestions

VERSION = 'selected-passage-alignment-1'
CONTEXT_SECONDS = 2.0
MAX_WINDOW_SECONDS = 180.0
SAMPLE_TOLERANCE = 1 / 16000 + 1e-9


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _interval(start, end, duration, description):
    if not (_finite(start) and _finite(end) and 0 <= start < end <= duration):
        raise ValueError(f'{description} needs a positive finite span inside this recording; no timing was guessed.')


def _cue_dict(cue):
    return {**deepcopy(cue.metadata), 'start': float(cue.start), 'end': float(cue.end),
            'text': cue.text, 'words': [word.to_dict() for word in cue.words]}


def passage_alignment_input(document, index):
    """Snapshot one edited timed cue and up to two seconds of audio context.

    Line-only timing is sufficient to select a search window. A missing, invalid
    or overlapping cue span must be corrected explicitly before this operation.
    Neighbor audio may enter the context, but replacement cannot cross neighbors.
    """
    if not isinstance(document, LyricsDocument):
        raise ValueError('Select a timed lyric document before aligning a passage.')
    if type(index) is not int or not 0 <= index < len(document.segments):
        raise ValueError('Select one existing timed phrase.')
    if not _finite(document.duration) or document.duration <= 0:
        raise ValueError('The recording duration is missing or invalid.')
    if not re.fullmatch(r'[a-f0-9]{64}', document.source_sha256):
        raise ValueError('The draft needs its exact recording fingerprint before passage alignment.')
    if document.metadata.get('instrumental') is True:
        raise ValueError('A track designated instrumental cannot acquire lyrics through passage alignment.')
    cue = document.segments[index]
    _interval(cue.start, cue.end, document.duration, 'The selected phrase')
    if not isinstance(cue.text, str) or not cue.text.strip() or len(cue.text) > 100000:
        raise ValueError('The selected phrase needs nonempty supplied text within the review limit.')
    if cue.end - cue.start > MAX_WINDOW_SECONDS:
        raise ValueError('The selected phrase exceeds the 180-second alignment limit; select a smaller timed passage.')
    left = document.segments[index - 1].end if index else 0.0
    right = document.segments[index + 1].start if index + 1 < len(document.segments) else document.duration
    if not (_finite(left) and _finite(right) and 0 <= left <= cue.start < cue.end <= right <= document.duration):
        raise ValueError('The selected phrase overlaps or has invalid neighboring timing. Correct that span first; neighbors will not be shifted.')
    context = min(CONTEXT_SECONDS, (MAX_WINDOW_SECONDS - (cue.end - cue.start)) / 2)
    start, end = max(0.0, cue.start - context), min(document.duration, cue.end + context)
    scope = {'version': VERSION, 'cue_index': index, 'cue_start': cue.start, 'cue_end': cue.end,
             'start': start, 'end': end, 'replacement_start': left, 'replacement_end': right,
             'source_sha256': document.source_sha256, 'duration': document.duration,
             'reference_sha256': hashlib.sha256(cue.text.encode()).hexdigest(),
             'document_state': _review_state_digest(document)}
    return {'reference_text': cue.text, 'reference_original': cue.text,
            'alignment_windows': [{'start': start, 'end': end, 'text': cue.text,
                'evidence': {'kind': 'operator_selected',
                    'source': 'Selected existing phrase with up to two seconds of context; not a first-word onset estimate.'}}],
            'alignment_source': 'draft_passage', 'replacement_scope': 'selected_passage',
            'replaces_revision': document_revision(document), 'passage_scope': scope}


def _validate_candidate_rows(data, duration, scope):
    """Check raw order before LyricsDocument.from_dict can sort imported spans."""
    rows = data.get('segments')
    if not isinstance(rows, list) or not rows or len(rows) > 1000:
        raise ValueError('Passage alignment returned no usable timed phrases; current text and timing were retained.')
    last = scope['start']
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Passage alignment returned an invalid phrase.')
        start, end = row.get('start'), row.get('end')
        _interval(start, end, duration, 'A new phrase')
        if start < last or start < scope['start'] or end > scope['end']:
            raise ValueError('New passage timing is out of order or outside its context window; no offsets or clipping were applied.')
        if start < scope['replacement_start']:
            raise ValueError('New passage timing crosses the previous phrase. Existing neighboring timing was not changed.')
        if end > scope['replacement_end']:
            raise ValueError('New passage timing crosses the next phrase. Existing neighboring timing was not changed.')
        text, words = row.get('text'), row.get('words')
        if not isinstance(text, str) or not text.strip() or not isinstance(words, list) or not words:
            raise ValueError('Passage alignment needs actual positive word or grouped-phrase support; no line timing was invented.')
        mapping = partial_tokens(row)
        if row.get('native_timing_issues') and mapping is None:
            raise ValueError('Passage alignment still has unresolved native timing. The edited original is unchanged; review the saved job evidence.')
        labels, previous = [], start
        for word in words:
            if not isinstance(word, dict):
                raise ValueError('Passage alignment returned an invalid word.')
            a, b = word.get('start'), word.get('end')
            _interval(a, b, duration, 'A new word or group')
            if not previous <= a < b <= end:
                raise ValueError('New word times are outside their phrase or out of order.')
            if not isinstance(word.get('text'), str) or not word['text'].strip():
                raise ValueError('A new timed word has no supplied text.')
            validate_highlight_intervals(word)
            # The backend already restored the crop offset. Verify it; never add it.
            native, offset = word.get('native_clip_span'), word.get('source_offset_seconds')
            if (not isinstance(native, dict) or not _finite(offset)
                    or not all(_finite(native.get(k)) for k in ('start', 'end'))
                    or not 0 <= native['start'] < native['end']
                    or not scope['start'] <= offset <= scope['start'] + SAMPLE_TOLERANCE
                    or abs(a - offset - native['start']) > 1e-8
                    or abs(b - offset - native['end']) > 1e-8
                    or word.get('coordinates') != 'source_audio_seconds'):
                raise ValueError('New passage source-offset evidence is missing or inconsistent; no second offset was applied.')
            labels.extend(word['text'].split())
            previous = b
        if mapping is None and labels != text.split():
            raise ValueError('A new phrase omitted or changed supplied words in its timed support.')
        last = end
    return rows


def merge_passage_alignment(original, candidate, request):
    """Return a reversible, unreviewed copy with exactly one old cue replaced.

    Accept a loaded LyricsDocument or its interchange dictionary. All new times
    must already be in original-song seconds. Missing timing is allowed only for
    exactly evidenced partial labels; missing text and independent holds fail.
    """
    if not isinstance(request, dict) or not isinstance(request.get('passage_scope'), dict):
        raise ValueError('Selected-passage request scope is missing; no whole-draft replacement is allowed.')
    index = request['passage_scope'].get('cue_index')
    expected = passage_alignment_input(original, index)
    if request.get('replaces_revision') != expected['replaces_revision']:
        raise ValueError('The draft changed after passage alignment started. Current edits and timings were retained.')
    for key, value in expected.items():
        if request.get(key) != value:
            raise ValueError('Passage text, bounds or review state changed after this job started; no result was merged.')
    if isinstance(candidate, LyricsDocument):
        data = candidate.to_dict()
    elif isinstance(candidate, dict):
        data = deepcopy(candidate)
    else:
        raise ValueError('Passage alignment did not return a lyric document.')
    if data.get('source_sha256') != original.source_sha256:
        raise ValueError('Passage alignment belongs to a different recording; existing lyrics were preserved.')
    duration = data.get('duration')
    if not _finite(duration) or abs(duration - original.duration) > SAMPLE_TOLERANCE:
        raise ValueError('Passage alignment recording duration does not match the original draft.')
    if data.get('completed') is False or data.get('error') or data.get('status') in ('failed', 'cancelled'):
        raise ValueError('Passage alignment failed or was cancelled; current lyrics were preserved.')
    provenance = data.get('provenance')
    if not isinstance(provenance, dict) or provenance.get('mode') != 'align' or provenance.get('engine') != 'qwen':
        raise ValueError('A selected passage requires the explicit Qwen supplied-text alignment result.')
    for key in ('alignment_source', 'replacement_scope', 'replaces_revision'):
        if provenance.get(key) != expected[key]:
            raise ValueError('Passage result provenance does not match this alignment job.')
    if data.get('alignment_windows', provenance.get('alignment_windows')) != expected['alignment_windows']:
        raise ValueError('Passage result used different audio sections or supplied text.')
    scope = expected['passage_scope']
    rows = _validate_candidate_rows(data, original.duration, scope)
    represented = [issue for row in rows for issue in partial_issues(row)]
    # Only the exact validated untimed labels may coexist with positive support.
    # No general missing-word/hold bypass: wholly unplaced groups still fail.
    if data.get('unplaced_groups'):
        raise ValueError('Passage alignment has words without a supported cue; current timing is unchanged.')
    for container, key in ((data, 'unplaced'), (data, 'native_timing_issues'), (provenance, 'unplaced')):
        if key in container and container[key] != represented:
            raise ValueError('Passage unplaced evidence does not exactly match its validated partial text.')
    if represented and provenance.get('unplaced') != represented:
        raise ValueError('Partial passage is missing its original unplaced-word provenance.')
    if [word for row in rows for word in row['text'].split()] != expected['reference_text'].split():
        raise ValueError('Passage alignment omitted, repeated or changed supplied text; no words were replaced.')
    new = LyricsDocument.from_dict(data)
    holds = new.metadata.get('held_passages', [])
    if (not isinstance(holds, list) or any(not isinstance(h, dict) or h.get('status') not in
            ('excluded_by_operator', 'operator_requested_alignment') for h in holds)
            or unresolved_suggestions(new)):
        raise ValueError('Passage alignment remains held for review; current text and timing were preserved.')

    result = deepcopy(original)
    before = _cue_dict(original.segments[index])
    replacements = deepcopy(new.segments)
    prior_region = original.segments[index].metadata.get('region')
    for cue in replacements:
        cue.metadata['passage_alignment'] = {'request_revision': expected['replaces_revision'],
            'original_cue_index': index, 'local_region': cue.metadata.get('region'),
            'raw_evidence': provenance.get('raw_evidence')}
        if type(prior_region) is int:
            cue.metadata['region'] = prior_region
            if cue.metadata.get('partial_timing'):
                cue.metadata['partial_timing']['region'] = prior_region
                for issue in cue.metadata.get('native_timing_issues', []):
                    issue['region'] = prior_region
            for word in cue.words:
                word.metadata['passage_local_region'] = word.metadata.get('region')
                word.metadata['region'] = prior_region
        partial_tokens(cue)  # region remapping must preserve the validated mapping
    result.segments[index:index + 1] = replacements
    # An editor can track other phrases needing alignment. Keep their identity
    # when replacing one phrase with several, and retire only this completed job.
    delta = len(replacements) - 1
    needed = result.human_review.get('alignment_needed')
    if isinstance(needed, list):
        result.human_review['alignment_needed'] = [i if i < index else i + delta for i in needed
            if type(i) is int and 0 <= i < len(original.segments) and i != index]
    event = {'version': VERSION, 'at': now(), 'action': 'align_selected_passage',
             'cue_index': index, 'replacement_count': len(replacements),
             'request': deepcopy(expected), 'before': before,
             'candidate': deepcopy(data),
             'rule': 'Existing source timestamps used once. No neighboring spans shifted; no individual timing invented.'}
    result.metadata.setdefault('passage_alignment_history', []).append(event)
    for warning in new.warnings:
        if warning not in result.warnings:
            result.warnings.append(warning)
    if 'review_windows' in result.quality:
        result.quality['review_windows'] = [w.to_dict() for c in result.segments for w in c.words if w.metadata.get('flags')]
    result = mark_unreviewed(result, 'Selected passage realigned. Neighboring lyrics and timing retained; listen before approving this revision.')
    # Explicit invariant: caller objects and all unrelated cue metadata stay exact.
    old_neighbors = [_cue_dict(c) for i, c in enumerate(original.segments) if i != index]
    new_neighbors = [_cue_dict(c) for i, c in enumerate(result.segments)
                     if not index <= i < index + len(replacements)]
    if json.dumps(old_neighbors, ensure_ascii=False, allow_nan=False) != json.dumps(new_neighbors, ensure_ascii=False, allow_nan=False):
        raise ValueError('Passage merge would change unrelated lyrics; no result returned.')
    return result
