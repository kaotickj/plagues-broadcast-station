"""Pure contracts for the pinned Qwen adapter; geometry is not accuracy.

Never repair timestamps here. Preserve the package's raw and repaired predictions
and the supplied spelling independently. Invalid words remain explicit unplaced
evidence, so a partial timing result cannot pass for complete alignment.
"""
from copy import deepcopy
import hashlib
import math
import re
import unicodedata

try:
    from .alignment_contract import alignment_regions, finite
    from .review_repetition import repetition
except ImportError:  # standalone model-runtime script
    from alignment_contract import alignment_regions, finite
    from review_repetition import repetition

VERSION = 'qwen-review-contract-1'
SAMPLE_RATE = 16000
MAX_DURATION = 900
MAX_WINDOWS = 6
ASR_WINDOW_SECONDS = 150
ALIGN_WINDOW_SECONDS = 180


def asr_windows(sample_count, sample_rate=SAMPLE_RATE):
    """Contiguous, sample-exact windows; balancing avoids a sub-0.5s tail."""
    if type(sample_count) is not int or sample_count <= 0 or sample_rate != SAMPLE_RATE:
        raise ValueError('Qwen needs nonempty mono 16 kHz audio.')
    duration = sample_count / sample_rate
    if not .5 <= duration <= MAX_DURATION:
        raise ValueError('Qwen review supports recordings from 0.5 to 900 seconds.')
    count = math.ceil(sample_count / (sample_rate * ASR_WINDOW_SECONDS))
    if count > MAX_WINDOWS:
        raise ValueError('Qwen review is limited to six windows.')
    return [{'start_sample': i * sample_count // count,
             'end_sample': (i + 1) * sample_count // count,
             'start': (i * sample_count // count) / sample_rate,
             'end': ((i + 1) * sample_count // count) / sample_rate}
            for i in range(count)]


def supplied_windows(request, duration):
    if not finite(duration) or not .5 <= duration <= MAX_DURATION:
        raise ValueError('Qwen alignment supports recordings from 0.5 to 900 seconds.')
    if request.get('alignment_windows') is None and duration > ALIGN_WINDOW_SECONDS:
        raise ValueError('Supply explicit lyric sections of at most 180 seconds for this recording; text is not split proportionally.')
    regions = alignment_regions(request, duration)
    if len(regions) > MAX_WINDOWS or any(r['end'] - r['start'] > ALIGN_WINDOW_SECONDS for r in regions):
        raise ValueError('Qwen alignment accepts at most six explicit sections, each at most 180 seconds.')
    for region in regions:
        token_mapping(region['text'])  # fail before loading a model
    return regions


def _cjk(ch):
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in ((0x4E00, 0x9FFF), (0x3400, 0x4DBF),
        (0x20000, 0x2A6DF), (0x2A700, 0x2B73F), (0x2B740, 0x2B81F),
        (0x2B820, 0x2CEAF), (0xF900, 0xFAFF)))


def normalized_token(token):
    """Mirror qwen-asr 0.0.6 space-language tokenization, not phonetic matching."""
    clean = ''.join(ch for ch in token if ch == "'" or unicodedata.category(ch)[0] in 'LN')
    parts, buffer = [], ''
    for ch in clean:
        if _cjk(ch):
            if buffer:
                parts.append(buffer)
                buffer = ''
            parts.append(ch)
        else:
            buffer += ch
    if buffer:
        parts.append(buffer)
    return parts


def token_mapping(text):
    if not isinstance(text, str) or not text.split():
        raise ValueError('Supply sung text before Qwen alignment.')
    if re.search(r'\[[^\]]+\]', text):
        raise ValueError('Separate bracketed directions from the sung text before alignment; the original request is retained.')
    mapping, normalized = [], []
    for index, original in enumerate(text.split()):
        parts = normalized_token(original)
        mapping.append({'text': original, 'token_index': index,
                        'native_indices': list(range(len(normalized), len(normalized) + len(parts)))})
        normalized.extend(parts)
    if not normalized:
        raise ValueError('The supplied text contains no alignable language tokens.')
    return mapping, normalized


def raw_text_payload(raw):
    """Strip only the model's metadata delimiter; never deduplicate raw lyrics."""
    if not isinstance(raw, str):
        raise ValueError('Qwen must return a decoded string.')
    return raw.split('<asr_text>', 1)[-1].strip()


def recognition_flags(raw, cleaned, *, generated_tokens, max_new_tokens, eos_ids):
    payload = raw_text_payload(raw)
    evidence = repetition(payload)
    flags = []
    if evidence:
        flags.append('raw_consecutive_repetition')
    if payload != cleaned.strip():
        flags.append('parser_modified_text')
    eos_ids = [eos_ids] if isinstance(eos_ids, int) else (eos_ids or [])
    stopped_eos = bool(generated_tokens and generated_tokens[-1] in eos_ids)
    limited = len(generated_tokens) >= max_new_tokens and not stopped_eos
    if limited:
        flags.append('token_limit_incomplete')
    if not cleaned.strip():
        flags.append('empty_transcript_not_instrumental_diagnosis')
    return {'flags': flags, 'raw_repetition': evidence,
            'generated_token_count': len(generated_tokens),
            'termination': 'token_limit' if limited else ('eos' if stopped_eos else 'unknown'),
            'automatically_vocal_verified': False}


def json_evidence(value):
    """Keep invalid native evidence JSON-safe instead of emitting NaN JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return {'nonfinite_native_value': repr(value)}
    if isinstance(value, dict):
        return {str(k): json_evidence(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_evidence(v) for v in value]
    return value


def timestamp_capture(words, raw_ms, parsed, grid_ms):
    """Compare package repair with saved predictions, without doing any repair."""
    if len(raw_ms) != 2 * len(words) or len(parsed) != len(words):
        raise ValueError('Qwen timestamp count does not cover every normalized supplied token.')
    if [p.get('text') for p in parsed] != words:
        raise ValueError('Qwen timestamp parsing changed supplied token order or identity.')
    items = []
    for index, row in enumerate(parsed):
        original = list(raw_ms[2 * index:2 * index + 2])
        repaired = [row['start_time'], row['end_time']]
        items.append({'text': words[index], 'native_index': index,
                      'raw_timestamp_ms': original, 'postprocessed_timestamp_ms': repaired,
                      'repaired_boundaries': [a != b for a, b in zip(original, repaired)]})
    return {'native_grid_ms': grid_ms, 'items': items,
            'changed_boundary_count': sum(sum(i['repaired_boundaries']) for i in items)}


def aligned_window(text, returned, capture, *, region, start, end, source_offset=0):
    """Map native positive support to original tokens; keep invalid tokens unresolved.

    Grouping only joins a tokenizer's constituents of the SAME original token.
    It never borrows adjacent words' timing to repair a zero-duration word.
    """
    mapping, expected = token_mapping(text)
    if not (all(finite(x) for x in (start, end, source_offset)) and 0 <= start < end and source_offset >= 0):
        raise ValueError('Invalid Qwen source window or offset.')
    if [w.get('text') for w in returned] != expected or [w.get('text') for w in capture['items']] != expected:
        raise ValueError('Qwen alignment omitted, reordered or changed supplied normalized words.')
    if not finite(capture['native_grid_ms']) or capture['native_grid_ms'] <= 0:
        raise ValueError('Invalid native timestamp resolution.')
    constituents, last = [], 0.0
    for row, prediction in zip(returned, capture['items']):
        left, right = row.get('start_time'), row.get('end_time')
        parsed_start, parsed_end = prediction['postprocessed_timestamp_ms']
        if (all(finite(x) for x in (left, right, parsed_start, parsed_end))
                and (abs(left - parsed_start / 1000) > 1e-8 or abs(right - parsed_end / 1000) > 1e-8)):
            raise ValueError('Returned Qwen timing differs from the captured postprocessor result.')
        flags, reasons = [], []
        if any(prediction['repaired_boundaries']):
            flags.append('qwen_timestamp_repaired_review')
        if not (finite(left) and finite(right)):
            reasons.append('nonfinite_native_span')
        elif left < 0 or right > end - start:
            reasons.append('native_span_outside_window')
        elif right <= left:
            reasons.append('zero_or_negative_native_span')
        elif left < last:
            reasons.append('native_span_order_conflict')
        else:
            last = right
            if right - left <= capture['native_grid_ms'] / 1000 + 1e-9:
                flags.append('qwen_resolution_limited_span_review')
            if right - left > 2:
                flags.append('long_span_review')
        constituents.append({**deepcopy(prediction), 'native_clip_span': {'start': left, 'end': right},
                             'flags': flags, 'issues': reasons})
    words, unplaced, issues = [], [], []
    for entry in mapping:
        native = [constituents[i] for i in entry['native_indices']]
        reasons = sorted({reason for c in native for reason in c['issues']})
        if not native:
            reasons.append('no_acoustic_tokens')
        common = {**entry, 'region': region, 'native_constituents': json_evidence(native)}
        if reasons:
            issue = {**common, 'reasons': reasons, 'status': 'unresolved',
                     'source_window': [source_offset + start, source_offset + end]}
            unplaced.append(issue)
            issues.append(deepcopy(issue))
            continue
        offset = source_offset + start
        intervals = [[c['native_clip_span']['start'] + offset, c['native_clip_span']['end'] + offset] for c in native]
        word = {**common, 'start': intervals[0][0], 'end': intervals[-1][1],
                'highlight_intervals': intervals, 'highlight_allowed': True,
                'flags': sorted({flag for c in native for flag in c['flags']}),
                'coordinates': 'source_audio_seconds', 'source_offset_seconds': offset,
                'native_clip_span': {'start': native[0]['native_clip_span']['start'],
                                     'end': native[-1]['native_clip_span']['end']}}
        if len(native) > 1:
            word.update(grouped_text=True, individual_word_boundaries=None)
        words.append(word)
    return {'words': words, 'unplaced': unplaced, 'native_timing_issues': issues,
            'normalized_coverage_complete': True, 'coverage_complete': not unplaced,
            'source_offset_applied_once': True}


def review_segments(words, unplaced, regions, withheld_regions=()):
    """Small cues preserve labels; their bounds come only from positive support."""
    segments, groups = [], []
    for ri, region in enumerate(regions):
        if ri in withheld_regions:
            continue
        labels = region['text'].split()
        valid = {w['token_index']: w for w in words if w['region'] == ri}
        invalid = {w['token_index']: w for w in unplaced if w['region'] == ri}
        batches, batch, last_support = [], [], None
        for index in range(len(labels)):
            word = valid.get(index)
            if batch and (len(batch) >= 9 or (word and last_support is not None and word['start'] - last_support > 1.2)):
                batches.append(batch)
                batch = []
            batch.append(index)
            if word:
                last_support = word['end']
        if batch:
            batches.append(batch)
        for indices in batches:
            native = [deepcopy(valid[i]) for i in indices if i in valid]
            issues = [deepcopy(invalid[i]) for i in indices if i in invalid]
            common = {'text': ' '.join(labels[i] for i in indices), 'region': ri,
                      'source_token_indices': indices, 'native_timing_issues': issues}
            if not native:
                groups.append({**common, 'status': 'unresolved', 'reason': 'No supported positive native span; no cue boundary invented.'})
                continue
            cue = {**common, 'start': native[0]['start'], 'end': native[-1]['end'], 'words': native}
            if issues:
                cue.update(highlight_allowed=False, highlight_intervals=[],
                           timing_kind='partial_native_support_requires_review')
            segments.append(cue)
    return segments, groups


def validate_review_alignment(result, regions, withheld_regions=()):
    """Recompute text accounting; incomplete timing is allowed only as a held draft."""
    expected = [r['text'].split() for r in regions]
    blocked = set(withheld_regions)
    if any(type(i) is not int or not 0 <= i < len(regions) for i in blocked):
        raise ValueError('Invalid withheld Qwen region IDs.')
    reference = result.get('reference_text')
    if (not isinstance(reference, str) or result.get('text') != reference
            or reference.split() != [t for row in expected for t in row]
            or result.get('reference_sha256') != hashlib.sha256(reference.encode()).hexdigest()):
        raise ValueError('Qwen result changed the supplied reference text or its hash.')
    accounted, visible, unresolved = {}, {}, []
    segments, unplaced = result.get('segments'), result.get('unplaced')
    if not isinstance(segments, list) or not isinstance(unplaced, list):
        raise ValueError('Qwen review needs explicit cue and unplaced evidence lists.')

    def identity(item):
        ri, ti = item.get('region'), item.get('token_index')
        if (type(ri) is not int or type(ti) is not int or not 0 <= ri < len(expected)
                or ri in blocked or not 0 <= ti < len(expected[ri]) or item.get('text') != expected[ri][ti]):
            raise ValueError('Qwen review contains foreign, withheld or relabeled supplied tokens.')
        key = (ri, ti)
        if key in accounted:
            raise ValueError('Qwen review duplicates a supplied token.')
        accounted[key] = item
        return key

    last_cue_end, last_visible = -1.0, (-1, -1)
    for cue in segments:
        ri, indices, words = cue.get('region'), cue.get('source_token_indices'), cue.get('words')
        if (type(ri) is not int or not 0 <= ri < len(expected) or ri in blocked or not isinstance(indices, list)
                or not indices or any(type(i) is not int or not 0 <= i < len(expected[ri]) for i in indices)
                or indices != list(range(indices[0], indices[-1] + 1)) or not isinstance(words, list) or not words):
            raise ValueError('Malformed Qwen review cue or source token indices.')
        if cue.get('text', '').split() != [expected[ri][i] for i in indices]:
            raise ValueError('Qwen cue text silently omitted or changed a supplied word.')
        for index in indices:
            if (ri, index) in visible or (ri, index) <= last_visible:
                raise ValueError('Qwen review cue order duplicates or reorders source words.')
            visible[(ri, index)] = True
            last_visible = (ri, index)
        if cue.get('start') != words[0].get('start') or cue.get('end') != words[-1].get('end'):
            raise ValueError('Qwen review cue bounds must come from native positive support.')
        if not finite(cue['start']) or not finite(cue['end']) or cue['start'] < last_cue_end:
            raise ValueError('Qwen review cue times are nonfinite or out of order.')
        last_cue_end = cue['end']
        previous, previous_index = cue['start'], -1
        for word in words:
            key = identity(word)
            start, end = word.get('start'), word.get('end')
            native, offset = word.get('native_clip_span', {}), word.get('source_offset_seconds')
            if (key[0] != ri or key[1] not in indices or key[1] <= previous_index
                    or not all(finite(x) for x in (start, end, offset, native.get('start'), native.get('end')))
                    or not regions[ri]['start'] <= start < end <= regions[ri]['end']
                    or start < previous or not 0 <= native['start'] < native['end']
                    or not regions[ri]['start'] <= offset <= regions[ri]['start'] + 1 / SAMPLE_RATE + 1e-9
                    or abs(start - offset - native['start']) > 1e-8 or abs(end - offset - native['end']) > 1e-8
                    or word.get('coordinates') != 'source_audio_seconds'):
                raise ValueError('Qwen positive timing or source offset is invalid.')
            intervals = word.get('highlight_intervals')
            if not isinstance(intervals, list) or not intervals:
                raise ValueError('Qwen positive word needs explicit supported intervals.')
            left_previous = start
            for interval in intervals:
                if (not isinstance(interval, list) or len(interval) != 2
                        or not all(finite(t) for t in interval)
                        or not left_previous <= interval[0] < interval[1] <= end):
                    raise ValueError('Qwen highlight interval crosses unsupported timing.')
                left_previous = interval[1]
            previous, previous_index = end, key[1]
        missing_here = set(indices) - {w['token_index'] for w in words}
        cue_issues = cue.get('native_timing_issues', [])
        issue_indices = {w.get('token_index') for w in cue_issues if w.get('region') == ri}
        if missing_here != issue_indices:
            raise ValueError('Qwen cue did not retain its unresolved timing evidence.')
        if missing_here and (cue.get('highlight_allowed') is not False or cue.get('highlight_intervals') != []):
            raise ValueError('Unresolved Qwen timing cannot enable cue highlighting.')
    last_unplaced = (-1, -1)
    for item in unplaced:
        key = identity(item)
        if item.get('status') != 'unresolved' or not item.get('reasons') or key <= last_unplaced:
            raise ValueError('Unplaced Qwen words need an explicit unresolved reason.')
        unresolved.append(key)
        last_unplaced = key
    for container in segments + result.get('unplaced_groups', []):
        for issue in container.get('native_timing_issues', []):
            key = (issue.get('region'), issue.get('token_index'))
            if key not in unresolved or issue != accounted.get(key):
                raise ValueError('Qwen displayed timing hold differs from the original unresolved evidence.')
    for group in result.get('unplaced_groups', []):
        ri, indices = group.get('region'), group.get('source_token_indices', [])
        if type(ri) is not int or not 0 <= ri < len(expected) or ri in blocked or not indices:
            raise ValueError('Invalid wholly untimed text group.')
        if any(type(i) is not int or not 0 <= i < len(expected[ri]) for i in indices):
            raise ValueError('Invalid wholly untimed token index.')
        if group.get('text', '').split() != [expected[ri][i] for i in indices]:
            raise ValueError('Wholly untimed text was changed.')
        for index in indices:
            key = (ri, index)
            if key in visible or key not in unresolved:
                raise ValueError('Wholly untimed group duplicates or fabricates text.')
            visible[key] = True
    all_keys = {(ri, ti) for ri, row in enumerate(expected) if ri not in blocked for ti in range(len(row))}
    if set(accounted) != all_keys or set(visible) != all_keys:
        raise ValueError('Qwen review omitted supplied words from timing evidence or displayed text.')
    issues = result.get('native_timing_issues', [])
    if {(i.get('region'), i.get('token_index')) for i in issues} != set(unresolved):
        raise ValueError('Qwen document timing holds do not match unresolved words.')
    return {'version': VERSION, 'supplied_text_preserved': True,
            'coverage_complete': not unresolved and not blocked, 'review_text_complete': True,
            'unresolved_token_count': len(unresolved), 'withheld_regions': sorted(blocked),
            'source_offset_applied_once': True, 'automatically_vocal_verified': False,
            'timing_accuracy_verified': False}
