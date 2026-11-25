"""Bounded second opinions before CTC, never a saved or approved lyric result.

``refine`` is pure except for its injected ``recognize(engine, start, end)``.
Adapters return absolute ``words`` and ``segments`` with decoder confidence,
plus backend/evidence metadata. The large-v3 adapter must attest ``fresh=True``
after bypassing its recognition cache. Missing confidence stays unknown.
No text is sent to the adapter, no CTC runs here, and no file is written.
"""
from copy import deepcopy
import math
import re


VERSION = 'prealignment-recovery-1'
MAX_WINDOW = 30.0
MAX_AUDIO = 60.0
MAX_REGIONS = 4
PADDING = 2.0
_MIN_WORD_SCORE = .35
_MIN_MEAN_SCORE = .72


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _token(text):
    return re.sub(r'[\W_]+', '', text.casefold()) if isinstance(text, str) else ''


def _tokens(words):
    return [_token(word.get('text')) for word in words]


def _interval(value):
    if not isinstance(value, dict):
        return None
    start, end = value.get('start'), value.get('end')
    return (start, end) if _finite(start) and _finite(end) and 0 <= start < end else None


def _decoder_segments(raw):
    """CPU evidence already uses absolute times; Vulkan offsets are milliseconds."""
    if not isinstance(raw, dict):
        return []
    result = [deepcopy(s) for s in raw.get('segments', []) if isinstance(s, dict)] if isinstance(raw.get('segments'), list) else []
    windows = raw.get('windows', [])
    for window in windows if isinstance(windows, list) else []:
        if not isinstance(window, dict):
            continue
        recognition = window.get('recognition')
        if isinstance(recognition, dict) and isinstance(recognition.get('segments'), list):
            result.extend(deepcopy(s) for s in recognition['segments'] if isinstance(s, dict))
        payload = window.get('raw')
        if not isinstance(payload, dict) or not isinstance(payload.get('transcription'), list):
            continue
        offset = window.get('start')
        for segment in payload['transcription']:
            if not isinstance(segment, dict):
                continue
            bounds = segment.get('offsets')
            if not isinstance(bounds, dict) or not _finite(offset):
                continue
            start, end = bounds.get('from'), bounds.get('to')
            if _finite(start) and _finite(end):
                result.append({**deepcopy(segment), 'start': offset + start / 1000,
                               'end': offset + end / 1000})
    return result


def _weak_segment(segment):
    reasons = []
    for key, predicate, reason in (
            ('no_speech_prob', lambda n: not 0 <= n < .6, 'decoder-no-speech'),
            ('avg_logprob', lambda n: not -1 <= n <= 0, 'decoder-low-confidence'),
            ('compression_ratio', lambda n: not 0 <= n <= 2.8, 'decoder-repetition')):
        if key in segment and (not _finite(segment[key]) or predicate(segment[key])):
            reasons.append(reason)
    if segment.get('artifact_suspected') is True:
        reasons.append('decoder-artifact-flag')
    return reasons


def _location(cue, duration):
    bounds = _interval(cue)
    if bounds is None or bounds[1] > duration:
        return None
    words = cue.get('words', [])
    if not isinstance(words, list):
        return None
    # A zero-length word is locatable inside an otherwise valid cue. Missing,
    # reversed, nonfinite, or out-of-recording times are never guessed/clipped.
    for word in words:
        if not isinstance(word, dict):
            return None
        start, end = word.get('start'), word.get('end')
        if not (_finite(start) and _finite(end)
                and bounds[0] <= start <= end <= bounds[1] <= duration):
            return None
    return bounds


def _plan(cues, duration, raw, report):
    try:
        from .lyrics_quality import artifact_suspected
    except ImportError:
        from lyrics_quality import artifact_suspected
    segments = _decoder_segments(raw)
    seeds = []
    for index, cue in enumerate(cues):
        reasons = []
        location = _location(cue, duration)
        if location is None:
            report['unresolved'].append({'cue': index, 'reason': 'Primary timing is unlocatable; no region was guessed.'})
            continue
        words = cue.get('words', [])
        if not words or any(not _token(w.get('text')) for w in words):
            reasons.append('missing-word-coverage')
        if cue.get('needs_review') or cue.get('artifact_suspected'):
            reasons.append('primary-review-flag')
        # Existing artifact patterns select a review window only. They never
        # remove, replace, or blacklist a phrase, including genuine sign-offs.
        if artifact_suspected({'segments': cues}, index):
            reasons.append('artifact-review-flag')
        scores = [w.get('score') for w in words]
        if scores and (any(not _finite(s) or not 0 <= s <= 1 or s < _MIN_WORD_SCORE for s in scores)
                       or all(_finite(s) for s in scores) and sum(scores) / len(scores) < _MIN_MEAN_SCORE):
            reasons.append('primary-weak-words')
        previous = location[0]
        for word in words:
            a, b = word['start'], word['end']
            if a < previous or b <= a or a < location[0] or b > location[1] or b - a > 8:
                reasons.append('primary-invalid-or-stretched-timing')
            if word.get('uncertain') or ('alignment_score' in word and
                    (not _finite(word['alignment_score']) or word['alignment_score'] < .15)):
                reasons.append('primary-uncertain-word')
            previous = b
        for segment in segments:
            interval = _interval(segment)
            if interval and interval[0] < location[1] and interval[1] > location[0]:
                reasons.extend(_weak_segment(segment))
        if reasons:
            seeds.append({'start': max(0., location[0] - PADDING),
                          'end': min(duration, location[1] + PADDING),
                          'cues': [index], 'reasons': sorted(set(reasons))})
    merged = []
    for seed in sorted(seeds, key=lambda r: (r['start'], r['end'])):
        if merged and seed['start'] <= merged[-1]['end']:
            region = merged[-1]
            region['end'] = max(region['end'], seed['end'])
            region['cues'] = sorted(set(region['cues'] + seed['cues']))
            region['reasons'] = sorted(set(region['reasons'] + seed['reasons']))
        else:
            merged.append(deepcopy(seed))
    audio = 0.
    selected = 0
    for region in merged:
        seconds = region['end'] - region['start']
        region.update(status='held', hypotheses={}, edits=[])
        if seconds > MAX_WINDOW:
            region['hold_reason'] = 'Merged suspect region exceeds30seconds; it was not truncated or split by guessed boundaries.'
        elif selected >= MAX_REGIONS or audio + seconds > MAX_AUDIO:
            region['hold_reason'] = 'Bounded recovery audio/region budget exhausted.'
        else:
            selected += 1
            audio += seconds
            region['status'] = 'planned'
        report['regions'].append(region)
    report['budget'].update(selected_regions=selected, selected_audio_seconds=audio)


def _hypothesis(result, engine, start, end):
    if not isinstance(result, dict):
        return None, 'Recognizer returned no structured word/segment evidence.'
    if engine == 'large-v3' and result.get('fresh') is not True:
        return None, 'Fresh large-v3 recognition was not established.'
    words, segments = result.get('words'), result.get('segments')
    if not isinstance(words, list) or not words:
        return None, 'Empty recognition is unresolved, not evidence of silence or permission to delete text.'
    if len(words) > 512 or not isinstance(segments, list) or not 0 < len(segments) <= 256:
        return None, 'Recognizer word/segment evidence is absent or exceeds the bounded result size.'
    scores = []
    previous = start
    for word in words:
        bounds = _interval(word)
        score = word.get('score') if isinstance(word, dict) else None
        if (not bounds or not _token(word.get('text')) or len(word['text']) > 1000
                or bounds[0] < previous or bounds[0] < start or bounds[1] > end
                or bounds[1] - bounds[0] > 8):
            return None, 'Recognizer words overlap, lack labels, or extend outside the bounded window.'
        if not _finite(score) or not _MIN_WORD_SCORE <= score <= 1 or word.get('uncertain'):
            return None, 'Recognizer word confidence is weak or unknown.'
        previous = bounds[1]
        scores.append(score)
    if sum(scores) / len(scores) < _MIN_MEAN_SCORE:
        return None, 'Recognizer mean word confidence is too low.'
    for segment in segments:
        bounds = _interval(segment)
        if not bounds or bounds[0] < start or bounds[1] > end:
            return None, 'Decoder segment location is invalid or outside the bounded window.'
        # High no-speech is disqualifying even with excellent word probability
        # and average log-probability. Missing values are never synthesized.
        if (not _finite(segment.get('no_speech_prob'))
                or not 0 <= segment['no_speech_prob'] < .6):
            return None, 'Decoder no-speech probability is high, invalid, or unknown.'
        if not _finite(segment.get('avg_logprob')) or not -1 <= segment['avg_logprob'] <= 0:
            return None, 'Decoder segment confidence is low or unknown.'
        if (not _finite(segment.get('compression_ratio'))
                or not 0 <= segment['compression_ratio'] <= 2.8):
            return None, 'Decoder segment repetition evidence is unreliable or unknown.'
    for word in words:
        if not any(s['start'] <= word['start'] and word['end'] <= s['end'] for s in segments):
            return None, 'Word timing is not covered by its decoder segment evidence.'
    return words, None


def _boundary(cue, side, default):
    if cue is None:
        return default
    if not isinstance(cue, dict) or not isinstance(cue.get('words', []), list):
        return None
    values = [cue.get(side)] + [w.get(side) if isinstance(w, dict) else None for w in cue.get('words', [])]
    if not all(_finite(value) for value in values):
        return None
    return max(values) if side == 'end' else min(values)


def _safe_edit(proposal, duration, before, after):
    """Equivalent whole-proposal containment, without importing review runtime."""
    left, right = _boundary(before, 'end', 0), _boundary(after, 'start', duration)
    if left is None or right is None:
        return False
    for cue in proposal:
        bounds = _interval(cue)
        words = cue.get('words', [])
        if (not bounds or bounds[0] < left or bounds[1] > right or bounds[1] > duration
                or not words or _token(cue.get('text')) != _token(' '.join(w['text'] for w in words))):
            return False
        previous = bounds[0]
        for word in words:
            interval = _interval(word)
            if not interval or interval[0] < previous or interval[1] > bounds[1] or interval[1] - interval[0] > 8:
                return False
            previous = interval[1]
        left = bounds[1]
    return bool(proposal)


def _within_envelope(words, left, right):
    if left is None or right is None or left >= right:
        return None
    # A word crossing a neighbor boundary cannot be clipped or reassigned.
    if any(w['start'] < right and w['end'] > left and
           (w['start'] < left or w['end'] > right) for w in words):
        return None
    return [word for word in words if left <= word['start'] and word['end'] <= right]


def refine(cues, duration, recognize, raw_primary=None, enabled=True,
           supplied_reference=False, *, validate_edit=None):
    """Return independent candidates/report; caller retains all CTC/save gates.

    Limits bound distinct audio regions; two recognizers inspect each selected
    region, so maximum aggregate recognizer audio is120seconds. Adapter timeout,
    model lifecycle and exclusive GPU coordination remain the caller's job.
    """
    candidate = deepcopy(cues)
    report = {'version': VERSION, 'status': 'held', 'regions': [], 'edits': [],
              'unresolved': [], 'primary_evidence': deepcopy(raw_primary),
              'budget': {'max_window_seconds': MAX_WINDOW, 'max_audio_seconds': MAX_AUDIO,
                         'max_regions': MAX_REGIONS, 'recognizer_calls': 0,
                         'selected_regions': 0, 'selected_audio_seconds': 0.},
              'notice': 'Independent ASR comparison only; every candidate still needs review, CTC and existing save/approval checks.'}
    result = {'cues': candidate, 'report': report}
    if not enabled or supplied_reference:
        report['status'] = 'disabled' if not enabled else 'supplied-reference-preserved'
        return result
    if not isinstance(cues, list) or not cues or not _finite(duration) or not 0 < duration <= 900:
        report['unresolved'].append({'reason': 'Primary transcript or recording duration is invalid/unlocatable; no recovery call made.'})
        return result
    _plan(cues, duration, raw_primary, report)
    validator = validate_edit or _safe_edit
    for region in report['regions']:
        if region['status'] != 'planned':
            continue
        available = {}
        for engine in ('medium.en', 'large-v3'):
            report['budget']['recognizer_calls'] += 1
            try:
                hypothesis = recognize(engine, region['start'], region['end'])
            except Exception as error:
                region.update(status='held', hold_reason='Independent recognizer unavailable; original passage retained.',
                              adapter_error={'engine': engine, 'type': type(error).__name__})
                break
            region['hypotheses'][engine] = deepcopy(hypothesis)
            available[engine] = _hypothesis(hypothesis, engine, region['start'], region['end'])
        if region['status'] == 'held':
            continue
        reasons = [engine + ': ' + reason for engine, (_, reason) in available.items() if reason]
        if reasons:
            region.update(status='held', hold_reason=' '.join(reasons))
            continue
        secondary, fresh = available['medium.en'][0], available['large-v3'][0]
        if _tokens(secondary) != _tokens(fresh):
            region.update(status='held', hold_reason='Independent models disagree; all original words retained.')
            continue
        region['status'] = 'corroborated'
        for index in region['cues']:
            before = candidate[index - 1] if index else None
            after = cues[index + 1] if index + 1 < len(cues) else None
            left, right = _boundary(before, 'end', 0), _boundary(after, 'start', duration)
            second_words = _within_envelope(secondary, left, right)
            third_words = _within_envelope(fresh, left, right)
            if not second_words or not third_words or _tokens(second_words) != _tokens(third_words):
                report['unresolved'].append({'cue': index, 'reason': 'Corroborated words do not fit unchanged/reviewed neighbors; original passage retained.'})
                continue
            # Only measured decoder words/times enter a proposal. No inferred
            # timing or carried-over approval/alignment score is introduced.
            words = [{key: deepcopy(w[key]) for key in ('text', 'start', 'end', 'score')} for w in third_words]
            proposal = {**deepcopy(cues[index]), 'text': ' '.join(w['text'] for w in words),
                        'start': words[0]['start'], 'end': words[-1]['end'],
                        'words': words, 'needs_review': True}
            for key in ('human_review', 'publication', 'approved', 'reviewed', 'machine_review'):
                proposal.pop(key, None)
            try:
                safe = validator([proposal], duration, before, after)
            except Exception as error:
                safe = False
            if not safe:
                report['unresolved'].append({'cue': index, 'reason': 'Whole-cue timing/coverage/neighbor guard rejected the proposal; original passage retained.'})
                continue
            candidate[index] = proposal
            edit = {'cue': index, 'before': deepcopy(cues[index]), 'candidate': deepcopy(proposal),
                    'region': [region['start'], region['end']], 'method': 'bounded-medium-and-fresh-large-agreement'}
            region['edits'].append(index)
            report['edits'].append(edit)
        if not region['edits']:
            region.update(status='held', hold_reason='No whole-cue proposal passed neighbor checks.')
    report['status'] = 'refined' if report['edits'] else 'held' if report['regions'] or report['unresolved'] else 'not-needed'
    return result
