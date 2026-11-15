"""Exact, explicit acceptance of displayed lyrics with retained untimed evidence.

This module never edits lyrics, resolves model evidence, or grants approval. Its
request is an acknowledgement for the separate source/revision-bound approval.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

from .lyrics import validate_highlight_intervals

SCHEMA = 'displayed-lyrics-acceptance-1'
AS_IS_SCHEMA = 'displayed-lyrics-as-is-1'


def accept_current_request(document):
    """Bind the checked Approve action to this exact displayed candidate.

    Quality evidence stays unresolved and unchanged. This records the owner's
    decision, not a model-quality pass or newly measured word boundaries.
    """
    return {'schema': AS_IS_SCHEMA, 'source_sha256': document.source_sha256,
            'content_sha256': _hash(_payload(document)),
            'decision': 'accept_current_displayed_lyrics_and_timing_as_is',
            'confirmation': 'checked_approve_action',
            'name_decision': 'keep_current_words_unchanged'}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _index(value):
    return type(value) is int and value >= 0


def _window(value, duration):
    return (isinstance(value, (list, tuple)) and len(value) == 2 and
            all(_finite(v) for v in value) and 0 <= value[0] < value[1] <= duration)


def _payload(document):
    # Private, read-only serialization view: no references escape this module.
    # Match LyricsDocument.to_dict numeric normalization without copying large
    # raw provenance/history that the acceptance hash does not include.
    payload = {key: document.metadata.get(key) for key in (
        'held_passages', 'vocabulary_review', 'recognition_regions',
        'alignment_windows', 'transcription_only', 'instrumental',
        'confirmed_instrumental_gaps', 'reference_provenance')}
    payload.update(duration=float(document.duration), source_sha256=document.source_sha256,
        segments=[{**cue.metadata, 'start': float(cue.start), 'end': float(cue.end),
            'text': cue.text,
            'words': [{**word.metadata, 'text': word.text,
                       'start': float(word.start), 'end': float(word.end)}
                      for word in cue.words]} for cue in document.segments])
    return payload


def _native_hold(hold, duration):
    """Return true only for a specifically evidenced omitted native token.

    Order conflicts/nonfinite/tokenizer failures cannot be independently proved
    from this row alone and remain blockers. No invalid span enters output.
    """
    if (hold.get('detector') != 'unplaced_native_word' or
            not isinstance(hold.get('text'), str) or not hold['text'].strip() or
            not _index(hold.get('region'))):
        return False
    evidence = hold.get('native_evidence')
    bounds = [hold.get('start'), hold.get('end')]
    if (not isinstance(evidence, dict) or not _window(bounds, duration) or
            evidence.get('source_window') != bounds or evidence.get('text') != hold['text'] or
            not _index(evidence.get('region')) or evidence.get('region') != hold['region'] or not _index(evidence.get('token_index')) or
            evidence.get('status') != 'unresolved'):
        return False
    native = evidence.get('native_constituents')
    indices = evidence.get('native_indices')
    reasons = evidence.get('reasons')
    allowed = {'zero_or_negative_native_span', 'native_span_outside_window'}
    if (not isinstance(native, list) or not native or not isinstance(indices, list) or
            len(native) != len(indices) or not all(_index(i) for i in indices) or
            len(set(indices)) != len(indices) or indices != sorted(indices) or
            not isinstance(reasons, list) or not reasons or not set(reasons) <= allowed):
        return False
    from .qwen_review_contract import normalized_token
    if [item.get('text') if isinstance(item, dict) else None for item in native] != normalized_token(hold['text']):
        return False
    found = set()
    for item, index in zip(native, indices):
        if not isinstance(item, dict) or item.get('native_index') != index:
            return False
        span = item.get('native_clip_span')
        if not isinstance(span, dict) or not all(_finite(span.get(k)) for k in ('start', 'end')):
            return False
        a, b = span['start'], span['end']
        raw, post = item.get('raw_timestamp_ms'), item.get('postprocessed_timestamp_ms')
        if any(not isinstance(pair, list) or len(pair) != 2 or not all(_finite(v) for v in pair)
               for pair in (raw, post)):
            return False
        if abs(post[0] / 1000 - a) > 1e-8 or abs(post[1] / 1000 - b) > 1e-8:
            return False
        actual = ('native_span_outside_window' if a < 0 or b > bounds[1] - bounds[0]
                  else 'zero_or_negative_native_span' if b <= a else None)
        issues = item.get('issues')
        if (not isinstance(issues, list) or not set(issues) <= allowed or
                (actual is None and issues) or (actual is not None and issues != [actual])):
            return False
        if actual:
            found.add(actual)
    return bool(found) and found == set(reasons)


def acceptance_requirements(document, *, _validate_payload=True):
    """Describe exact acknowledgements and blockers, without changing evidence."""
    result = {'missing_native_holds': [], 'partial_unhighlighted_words': [],
              'pending_name_suggestions': [], 'blockers': [], 'blocker_details': [], 'requires_confirmation': False}

    def block(code, message, **location):
        result['blockers'].append(message)
        result['blocker_details'].append({'code': code, 'message': message, **location})

    try:
        duration = document.duration
        if not _finite(duration) or not 0 < duration <= 86400:
            block('duration', 'The recording duration is invalid.')
            return result
        if not isinstance(document.source_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', document.source_sha256):
            block('source', 'The exact recording fingerprint is required.')
        if not isinstance(document.metadata, dict) or not isinstance(document.segments, list):
            block('structure', 'The lyric document structure is invalid.')
            return result
        if document.metadata.get('transcription_only') is True:
            block('recognition_only', 'Extracted words have not been aligned.')
        if not document.segments and document.metadata.get('instrumental') is not True:
            block('no_timed_output', 'There are no timed lyrics to approve.')
        if document.segments and document.metadata.get('instrumental') is True:
            block('instrumental_conflict', 'An instrumental designation cannot contain timed lyrics.')
        previous = 0.0
        for ci, cue in enumerate(document.segments):
            if (not all(_finite(v) for v in (cue.start, cue.end)) or
                    not 0 <= cue.start < cue.end <= duration or cue.start < previous):
                block('cue_timing', 'Lyric line timing is invalid or overlaps another line.', cue_index=ci)
            previous = cue.end
            if not isinstance(cue.text, str) or not cue.text.strip() or not isinstance(cue.metadata, dict):
                block('cue_text', 'A lyric line needs valid text and metadata.', cue_index=ci)
                continue
            validate_highlight_intervals({**cue.metadata, 'start': cue.start, 'end': cue.end})
            partial = None
            if 'partial_timing' in cue.metadata:
                from .review_partial import partial_tokens, partial_issues
                partial = partial_tokens(cue)
                if partial is None:
                    block('partial_invalid', 'Partial word evidence was not validated.', cue_index=ci)
                else:
                    evidence = partial_issues(cue)
                    for ti, (text, wi) in enumerate(partial):
                        if wi is None:
                            row = {'cue_index': ci, 'display_token_index': ti, 'text': text,
                                   'evidence': deepcopy(evidence)}
                            result['partial_unhighlighted_words'].append({'identity': _hash(row), **row})
            elif cue.metadata.get('native_timing_issues'):
                block('unresolved_native_timing', 'Native timing is unresolved within a displayed line.', cue_index=ci)
            if not isinstance(cue.words, list):
                block('word_structure', 'Word timing must be a list.', cue_index=ci)
                continue
            end = cue.start
            for wi, word in enumerate(cue.words):
                if (not all(_finite(v) for v in (word.start, word.end)) or
                        not end <= word.start < word.end <= cue.end or
                        not isinstance(word.text, str) or not word.text.strip()):
                    block('word_timing', 'A word has invalid, zero, overlapping or out-of-line timing.',
                          cue_index=ci, word_index=wi)
                validate_highlight_intervals(word.to_dict())
                end = word.end
            normalize = lambda text: re.sub(r'\W+', '', text.casefold())
            if partial is None and cue.words and normalize(' '.join(w.text for w in cue.words)) != normalize(cue.text):
                block('word_text', 'Timed words do not match the displayed lyric line.', cue_index=ci)

        held = document.metadata.get('held_passages', [])
        if not isinstance(held, list):
            block('holds_structure', 'Held evidence must be a list.')
        else:
            for hi, hold in enumerate(held):
                if not isinstance(hold, dict):
                    block('hold_structure', 'A held-evidence row is malformed.', hold_index=hi)
                elif hold.get('status') == 'excluded_by_operator':
                    continue
                elif hold.get('status') == 'held' and _native_hold(hold, duration):
                    row = {'hold_index': hi, 'text': hold['text'], 'region': hold['region'],
                           'source_window': [hold['start'], hold['end']], 'evidence': deepcopy(hold)}
                    result['missing_native_holds'].append({'identity': _hash(row), **row})
                else:
                    block('held_blocker', 'Held recognition or malformed native evidence still requires review.', hold_index=hi)

        # Recognition region indices can be remapped after empty sections. Match
        # their exact source windows to the alignment window before checking cues.
        regions = document.metadata.get('recognition_regions', [])
        windows = document.metadata.get('alignment_windows', regions)
        if not isinstance(regions, list) or not isinstance(windows, list):
            block('regions_structure', 'Recognition/alignment sections are malformed.')
        else:
            for ri, region in enumerate(regions):
                if not isinstance(region, dict) or not isinstance(region.get('text'), str) or not _window([region.get('start'), region.get('end')], duration):
                    block('region_invalid', 'A recognition section is malformed.', region=ri)
                    continue
                if not region['text'].strip():
                    continue
                matches = [i for i, w in enumerate(windows) if isinstance(w, dict) and
                           [w.get('start'), w.get('end')] == [region['start'], region['end']]]
                if len(matches) != 1:
                    block('region_mapping', 'A recognized section has no unique alignment window.', region=ri)
                    continue
                cues = [c for c in document.segments if type(c.metadata.get('region')) is int and c.metadata['region'] == matches[0]]
                if not cues or any(c.start < region['start'] or c.end > region['end'] for c in cues):
                    block('whole_section_missing', 'A recognized section has no valid timed lyric coverage.', region=ri)
            for hi, hold in enumerate(held if isinstance(held, list) else []):
                if isinstance(hold, dict) and hold.get('status') == 'held' and hold.get('detector') == 'unplaced_native_word':
                    ri = hold.get('region')
                    if (not _index(ri) or ri >= len(windows) or not isinstance(windows[ri], dict) or
                            [windows[ri].get('start'), windows[ri].get('end')] != [hold.get('start'), hold.get('end')] or
                            not any(c.metadata.get('region') == ri for c in document.segments)):
                        block('hold_region', 'An omitted token has no matching timed source section.', hold_index=hi)

        vocabulary = document.metadata.get('vocabulary_review', {})
        if not isinstance(vocabulary, dict) or not isinstance(vocabulary.get('suggestions', []), list):
            block('names_structure', 'Name suggestions are malformed.')
        else:
            seen = set()
            for ni, item in enumerate(vocabulary.get('suggestions', [])):
                if not isinstance(item, dict) or item.get('status') not in ('pending', 'accepted', 'rejected'):
                    block('name_status', 'A name suggestion has an unknown status.', suggestion_index=ni)
                    continue
                if item['status'] != 'pending':
                    continue
                ci = item.get('cue_index')
                identity = item.get('id')
                if (vocabulary.get('source_sha256') != document.source_sha256 or
                        not isinstance(identity, str) or not identity or identity in seen or
                        not _index(ci) or ci >= len(document.segments) or
                        item.get('cue_text') != document.segments[ci].text or
                        not _index(item.get('start_char')) or not _index(item.get('end_char')) or
                        not item['start_char'] < item['end_char'] <= len(document.segments[ci].text) or
                        not isinstance(item.get('match_text'), str) or not item['match_text'] or
                        document.segments[ci].text[item['start_char']:item['end_char']] != item['match_text'] or
                        not isinstance(item.get('replacement'), str) or not item['replacement'].strip()):
                    block('name_stale', 'A pending name suggestion no longer matches this recording and text.', suggestion_index=ni)
                    continue
                seen.add(identity)
                row = {'suggestion_index': ni, 'suggestion_id': identity, 'cue_index': ci,
                       'text': item['match_text'], 'suggested_text': item['replacement'],
                       'evidence': deepcopy(item)}
                result['pending_name_suggestions'].append({'identity': _hash(row), **row})
        if _validate_payload:
            _hash(_payload(document))  # Standalone requirements still fail closed.
    except (TypeError, ValueError, AttributeError, KeyError, IndexError) as error:
        block('malformed_evidence', 'Invalid lyric or review evidence: ' + str(error))
    result['requires_confirmation'] = any(result[k] for k in (
        'missing_native_holds', 'partial_unhighlighted_words', 'pending_name_suggestions'))
    return result


def acceptance_request(document):
    """Create the exact request shown for a single explicit confirmation."""
    requirements = acceptance_requirements(document, _validate_payload=False)
    if requirements['blockers']:
        raise ValueError('Approval blocked: ' + '; '.join(requirements['blockers']))
    return {'schema': SCHEMA, 'source_sha256': document.source_sha256,
            'content_sha256': _hash(_payload(document)),
            'decision': 'accept_displayed_lyrics_with_listed_exceptions',
            'missing_native_holds': [r['identity'] for r in requirements['missing_native_holds']],
            'partial_unhighlighted_words': [r['identity'] for r in requirements['partial_unhighlighted_words']],
            'pending_name_suggestions': [r['identity'] for r in requirements['pending_name_suggestions']],
            'name_decision': 'keep_current_words_unchanged'}


def validate_acceptance(document, request):
    """An extra/missing/stale/foreign acknowledgement is never transferable."""
    expected = (accept_current_request(document) if isinstance(request, dict) and
                request.get('schema') == AS_IS_SCHEMA else acceptance_request(document))
    if not isinstance(request, dict) or _json(request) != _json(expected):
        raise ValueError('Listening acceptance does not match this exact lyric content and evidence.')
    return True


def acceptance_message(document):
    requirements = acceptance_requirements(document)
    lines = [f'Approve the {len(document.segments)} displayed timed lyric lines after listening.']
    missing = requirements['missing_native_holds']
    if missing:
        lines.append('Untimed words omitted from the display: ' + ', '.join(repr(r['text']) for r in missing) + '.')
    partial = requirements['partial_unhighlighted_words']
    if partial:
        lines.append('Visible words without individual highlighting: ' + ', '.join(repr(r['text']) for r in partial) + '.')
    names = requirements['pending_name_suggestions']
    if names:
        lines.append(f'Keep the current wording for all {len(names)} pending name suggestions; make no substitutions.')
    lines.append('Grouped timing and advisory flags remain as evidence. This approves the displayed result, not model accuracy or completeness.')
    if requirements['blockers']:
        lines.append('Approval blocked: ' + '; '.join(requirements['blockers']))
    return '\n'.join(lines)
