"""Shared supplied-text alignment contract, independent of models and UI.

Windows are explicit evidence, not inferred vocal detection. Source coordinates
remain unchanged and the complete supplied transcript must be accounted for.
"""
from copy import deepcopy
import math

VERSION = 'supplied-alignment-windows-1'


def tokens(text):
    if not isinstance(text, str):
        raise ValueError('Alignment text must be a string.')
    return text.split()


def finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def alignment_regions(request, duration):
    """Validate explicit windows; the legacy full-audio mode stays unchanged."""
    if not finite(duration) or duration <= 0:
        raise ValueError('Alignment needs a finite positive recording duration.')
    reference = request.get('reference_text', '')
    if not tokens(reference):
        raise ValueError('Supply sung words before alignment.')
    supplied = request.get('alignment_windows')
    if supplied is None:
        return [{'start': 0, 'end': duration, 'text': reference}]
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= 1000:
        raise ValueError('Supply between one and 1000 alignment windows.')
    result, previous = [], 0
    for item in supplied:
        if not isinstance(item, dict):
            raise ValueError('Every alignment window needs explicit evidence.')
        start, end = item.get('start'), item.get('end')
        if not (finite(start) and finite(end) and 0 <= previous <= start < end <= duration):
            raise ValueError('Alignment windows must be ordered, disjoint and inside the recording.')
        if not tokens(item.get('text', '')):
            raise ValueError('An alignment window must contain supplied sung words.')
        evidence = item.get('evidence')
        if (not isinstance(evidence, dict) or
                evidence.get('kind') not in ('owner_reviewed', 'operator_selected', 'detector_candidate') or
                not isinstance(evidence.get('source'), str) or not evidence['source'].strip()):
            raise ValueError('Record where each vocal-window boundary came from.')
        result.append(deepcopy(item))
        previous = end
    if [word for item in result for word in tokens(item['text'])] != tokens(reference):
        raise ValueError('Window text must preserve every supplied word in order, including repeats and names.')
    return result


def validate_native_words(words, regions):
    """Reject malformed spans; report omitted words without inventing boundaries."""
    result, missing = [], []
    previous_region = -1
    for word in words:
        index = word.get('region')
        if type(index) is not int or not previous_region <= index < len(regions):
            raise ValueError('Native words must retain ordered, valid alignment window IDs.')
        previous_region = index
    for ri, region in enumerate(regions):
        actual = [w for w in words if w.get('region') == ri]
        previous = region['start']
        for word in actual:
            start, end = word.get('start'), word.get('end')
            if not (finite(start) and finite(end)):
                raise ValueError('Native word span has nonfinite or nonnumeric bounds.')
            if end <= start:
                raise ValueError(f"Native word {word.get('text', '')!r} has zero or negative duration; no replacement boundary was invented.")
            if not previous <= start < end <= region['end']:
                raise ValueError('Native word span crosses a vocal window or has invalid/order-conflicting bounds.')
            if not isinstance(word.get('text'), str) or not word['text'].strip():
                raise ValueError('Native word span has no text.')
            intervals = word.get('highlight_intervals')
            if intervals is not None:
                if not isinstance(intervals, (list, tuple)):
                    raise ValueError('Native highlight intervals must be a sequence.')
                previous_interval = start
                for interval in intervals:
                    if isinstance(interval, dict):
                        left, right = interval.get('start'), interval.get('end')
                    elif isinstance(interval, (list, tuple)) and len(interval) == 2:
                        left, right = interval
                    else:
                        raise ValueError('Malformed native highlight interval.')
                    if not (finite(left) and finite(right) and previous_interval <= left < right <= end):
                        raise ValueError('Native highlight intervals must be ordered inside their word and window.')
                    previous_interval = right
            previous = end
        expected = tokens(region['text'])
        labels = [w['text'] for w in actual]
        if labels != expected:
            missing.append({'region': ri, 'expected': expected, 'returned': labels,
                            'reason': 'Supplied-text coverage mismatch; no replacement or interpolation applied.'})
        result.extend(deepcopy(actual))
    if len(result) != len(words):
        raise ValueError('Native words refer to an unknown alignment window.')
    return {'version': VERSION, 'coordinates': 'decoded_audio_seconds',
            'window_count': len(regions), 'coverage_complete': not missing,
            'coverage_issues': missing, 'automatically_vocal_verified': False,
            'timing_accuracy_verified': False}


def require_supplied_text_coverage(alignment, regions, withheld_regions=()):
    """Controller boundary: incomplete supplied text cannot become candidate_ready.

Intentionally held windows keep their original IDs and remain unresolved holds;
only those windows may lack timed words. Always recompute, rather than trusting
an adapter's self-reported coverage flag.
"""
    blocked = set(withheld_regions)
    if any(type(i) is not int or not 0 <= i < len(regions) for i in blocked):
        raise ValueError('Invalid withheld alignment window ID.')
    segments = alignment.get('segments')
    if not isinstance(segments, list):
        raise ValueError('Alignment must return an explicit segment list.')
    words = []
    for cue in segments:
        native = cue.get('words')
        if not isinstance(native, list):
            raise ValueError('Supplied-text alignment requires native words, not fabricated line timing.')
        if tokens(cue.get('text', '')) != tokens(' '.join(w.get('text', '') for w in native)):
            raise ValueError('Alignment line labels do not match their native words.')
        words.extend(native)
    if any(w.get('region') in blocked for w in words):
        raise ValueError('A withheld passage cannot also acquire candidate word timings.')
    evidence = validate_native_words(words, regions)
    unexpected = [issue for issue in evidence['coverage_issues'] if issue['region'] not in blocked]
    if unexpected:
        raise ValueError('Supplied-text alignment omitted, reordered or changed words; raw evidence retained, no candidate committed.')
    evidence.update(withheld_regions=sorted(blocked), alignable_regions_complete=True)
    return evidence


def restore_source_timeline(words, *, clip_start, clip_end, source_duration):
    """Restore a real crop offset once, preserving native gaps and grouped spans."""
    if not (all(finite(x) for x in (clip_start, clip_end, source_duration))
            and 0 <= clip_start < clip_end <= source_duration):
        raise ValueError('Crop bounds must lie inside the original recording.')
    duration = clip_end - clip_start
    result = []
    for original in words:
        word = deepcopy(original)
        if word.get('coordinates') == 'source_audio_seconds' or 'native_clip_span' in word:
            raise ValueError('Crop offset was already restored; never apply it twice.')
        start, end = word.get('start'), word.get('end')
        if not (finite(start) and finite(end) and 0 <= start < end <= duration):
            raise ValueError('Native span must lie inside the decoded crop.')
        intervals = word.get('highlight_intervals')
        if intervals is not None:
            if not isinstance(intervals, list):
                raise ValueError('Highlight intervals must be an explicit list.')
            translated = []
            previous = start
            for interval in intervals:
                if isinstance(interval, dict):
                    left, right = interval.get('start'), interval.get('end')
                elif isinstance(interval, (list, tuple)) and len(interval) == 2:
                    left, right = interval
                else:
                    raise ValueError('Malformed highlight interval.')
                if not (finite(left) and finite(right) and previous <= left < right <= end):
                    raise ValueError('Highlight intervals must stay ordered inside their native word/group.')
                translated.append({**interval, 'start': left + clip_start, 'end': right + clip_start}
                                  if isinstance(interval, dict) else [left + clip_start, right + clip_start])
                previous = right
            word['highlight_intervals'] = translated
        word.update(start=start + clip_start, end=end + clip_start,
                    native_clip_span={'start': start, 'end': end},
                    source_offset_seconds=clip_start, coordinates='source_audio_seconds')
        result.append(word)
    return result
