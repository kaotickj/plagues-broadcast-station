"""Validated partial-word display. Untimed text is never a synthetic Word.

Pure data only; importing/loading lyrics does not recover old grouped artifacts.
Recovery is an explicit copy operation for private review or fresh worker output.
"""
from copy import deepcopy
import math
import unicodedata

VERSION = 'native-partial-tokens-1'


def retire_partial_mapping(metadata, reason):
    """Keep obsolete display maps as evidence, never active timing instructions."""
    result = deepcopy(metadata)
    if 'partial_timing' in result:
        result.setdefault('superseded_partial_mappings', []).append({
            'reason': reason, 'partial_timing': result.pop('partial_timing'),
            'native_timing_issues': deepcopy(result.get('native_timing_issues', []))})
        result['timing_kind'] = 'edited_existing_support'
    return result


def recover_stale_text_edit_mapping(data):
    """Recover only the demonstrated old editor bug, in a copy.

    Require the exact edit history, a valid prior partial map, and the editor's
    unchanged grouped intervals/constituents. Do not repair arbitrary corruption,
    change a lyric, move a boundary, or grant approval.
    """
    result = deepcopy(data)
    recovered = []
    for ci, row in enumerate(result.get('segments', [])):
        try:
            partial_tokens(row)
            continue
        except ValueError:
            pass
        if not isinstance(row, dict) or 'partial_timing' not in row:
            raise ValueError('Unrecognized invalid saved lyric row.')
        matches = []
        for edit in result.get('edits', []):
            before = edit.get('before', {})
            if (edit.get('action') == 'edit_phrase' and edit.get('after_text') == row.get('text')
                    and before.get('start') == row.get('start') and before.get('end') == row.get('end')):
                prior = {**deepcopy(row), **deepcopy(before)}
                try:
                    partial_tokens(prior)
                    words = _positive_words(row)
                    expected = []
                    for w in before['words']:
                        if w.get('highlight_allowed', True):
                            pairs = w.get('highlight_intervals')
                            expected.extend(pairs if pairs is not None else [[w['start'], w['end']]])
                    if (len(words) == 1 and words[0].get('grouped_text') is True
                            and words[0].get('individual_word_boundaries') is None
                            and words[0].get('native_constituents') == before['words']
                            and words[0]['text'] == row['text']
                            and [words[0]['start'], words[0]['end']] == [row['start'], row['end']]
                            and words[0].get('highlight_intervals') == expected
                            and words[0].get('highlight_allowed') == bool(expected)):
                        matches.append(edit)
                except (ValueError, TypeError, KeyError):
                    continue
        if not matches:
            raise ValueError('Saved partial timing is invalid and no matching safe text-edit recovery exists.')
        result['segments'][ci] = retire_partial_mapping(row,
            'Recovered obsolete partial map left by a recorded text edit; text and intervals unchanged.')
        recovered.append({'cue_index': ci, 'text': row['text'], 'edit_at': matches[-1].get('at')})
    if recovered:
        result.setdefault('saved_draft_recoveries', []).append({
            'kind': 'stale_partial_mapping_after_text_edit', 'cues': recovered,
            'text_and_timing_unchanged': True})
        result['human_review'] = {'status': 'draft', 'reason': 'Recovered saved text-edit metadata; no approval transferred.'}
    return result


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _row(cue):
    if isinstance(cue, dict):
        return cue
    def word_row(word):
        if isinstance(word, dict): return word
        return {**getattr(word, 'metadata', {}), 'text': word.text, 'start': word.start, 'end': word.end}
    return {**getattr(cue, 'metadata', {}), 'text': cue.text, 'start': cue.start,
            'end': cue.end, 'words': [word_row(word) for word in cue.words]}


def _positive_words(row):
    start, end, words = row.get('start'), row.get('end'), row.get('words')
    if not (_finite(start) and _finite(end) and 0 <= start < end):
        raise ValueError('Partial cue has invalid outer timing.')
    if not isinstance(words, list) or not words:
        raise ValueError('Partial cue needs actual positive word support; no line span is invented.')
    previous = start
    for word in words:
        if not isinstance(word, dict):
            raise ValueError('Partial positive support must contain word dictionaries.')
        a, b = word.get('start'), word.get('end')
        if not (_finite(a) and _finite(b) and previous <= a < b <= end):
            raise ValueError('Partial cue contains invalid or reordered positive word geometry.')
        if not isinstance(word.get('text'), str) or not word['text'].split():
            raise ValueError('Partial word has no text.')
        if 'highlight_allowed' in word and type(word['highlight_allowed']) is not bool:
            raise ValueError('Invalid partial-word highlight permission.')
        pairs = word.get('highlight_intervals', [[a, b]])
        if not isinstance(pairs, (list, tuple)):
            raise ValueError('Partial-word highlight intervals must be explicit.')
        prior = a
        for pair in pairs:
            if isinstance(pair, dict) and set(pair) == {'start', 'end'}:
                left, right = pair['start'], pair['end']
            elif isinstance(pair, (list, tuple)) and len(pair) == 2:
                left, right = pair
            else:
                raise ValueError('Malformed partial-word highlight interval.')
            if not (_finite(left) and _finite(right) and prior <= left < right <= b):
                raise ValueError('Partial highlight interval crosses its supported word span.')
            prior = right
        previous = b
    return words


def _validate_issue(issue, text, index, region, row, words):
    if (not isinstance(issue, dict) or issue.get('text') != text
            or issue.get('token_index') != index or issue.get('region') != region
            or issue.get('status') != 'unresolved'):
        raise ValueError('Untimed token does not match its preserved native evidence.')
    reasons, native, indices = issue.get('reasons'), issue.get('native_constituents'), issue.get('native_indices')
    allowed = {'zero_or_negative_native_span', 'native_span_outside_window',
               'nonfinite_native_span', 'native_span_order_conflict', 'no_acoustic_tokens'}
    if (not isinstance(reasons, list) or not reasons or any(not isinstance(r, str) for r in reasons) or not set(reasons) <= allowed
            or not isinstance(native, list) or not isinstance(indices, list)):
        raise ValueError('Untimed token has no recognized native timing failure evidence.')
    bounds = issue.get('source_window')
    if (not isinstance(bounds, list) or len(bounds) != 2 or not all(_finite(t) for t in bounds)
            or not 0 <= bounds[0] <= row['start'] < row['end'] <= bounds[1]):
        raise ValueError('Untimed token source window does not contain its existing cue.')
    if not native:
        if (indices or reasons != ['no_acoustic_tokens'] or any(
                ch == "'" or unicodedata.category(ch)[0] in 'LN' for ch in text)):
            raise ValueError('Missing native constituents are not evidence of a timing failure.')
        return
    if any(not isinstance(n, dict) for n in native) or [n.get('native_index') for n in native] != indices or any(type(i) is not int for i in indices):
        raise ValueError('Untimed native constituent identities changed.')
    normalized = ''.join(ch for ch in text if ch == "'" or unicodedata.category(ch)[0] in 'LN')
    if ''.join(str(n.get('text', '')) for n in native) != normalized:
        raise ValueError('Untimed native constituents do not match the supplied token.')
    demonstrated = set()
    preceding = [w['end'] - bounds[0] for w in words if w.get('token_index', index) < index]
    for constituent in native:
        span = constituent.get('native_clip_span', {})
        if not isinstance(span, dict):
            raise ValueError('Untimed token has malformed native span evidence.')
        a, b = span.get('start'), span.get('end')
        raw, post = constituent.get('raw_timestamp_ms'), constituent.get('postprocessed_timestamp_ms')
        if not isinstance(raw, list) or len(raw) != 2 or not isinstance(post, list) or len(post) != 2:
            raise ValueError('Untimed token lost pre/postprocessor boundary evidence.')
        if not (_finite(a) and _finite(b)):
            def nonfinite(value):
                return (isinstance(value, float) and not math.isfinite(value)) or (
                    isinstance(value, dict) and set(value) == {'nonfinite_native_value'}
                    and value['nonfinite_native_value'] in ('nan', 'inf', '-inf'))
            if not all(_finite(t) or nonfinite(t) for t in (a, b)):
                raise ValueError('Missing native bounds are not evidence of a nonfinite prediction.')
            demonstrated.add('nonfinite_native_span')
            continue
        if not (all(_finite(t) for t in post) and abs(a - post[0] / 1000) < 1e-8 and abs(b - post[1] / 1000) < 1e-8):
            raise ValueError('Untimed token clip span disagrees with its postprocessor evidence.')
        if b <= a:
            demonstrated.add('zero_or_negative_native_span')
        if a < 0 or b > bounds[1] - bounds[0]:
            demonstrated.add('native_span_outside_window')
        if preceding and a < max(preceding):
            demonstrated.add('native_span_order_conflict')
    if not set(reasons) <= demonstrated:
        raise ValueError('A valid native span cannot be hidden as an untimed token.')


def partial_tokens(cue):
    """Return (display token, positive Word index or None), or None if ordinary.

    Malformed partial markers fail closed. Geometry is checked independently of
    labels; this contract never converts a zero/invalid Word into a valid one.
    """
    # Ordinary cues are the common compositor path. Do not even materialize a
    # shallow row or traverse word history unless a partial marker is present.
    metadata = cue if isinstance(cue, dict) else getattr(cue, 'metadata', {})
    if 'partial_timing' not in metadata:
        return None
    row = _row(cue)
    marker = row['partial_timing']
    if not isinstance(marker, dict) or marker.get('version') != VERSION:
        raise ValueError('Unknown partial timing contract.')
    text = row.get('text')
    if not isinstance(text, str) or not text.split():
        raise ValueError('Partial cue has no text.')
    words = _positive_words(row)
    region = marker.get('region')
    tokens = marker.get('tokens')
    source = row.get('source_token_indices')
    if (type(region) is not int or region < 0 or row.get('region') != region
            or not isinstance(tokens, list) or len(tokens) != len(text.split())
            or not isinstance(source, list) or len(source) != len(tokens)
            or any(type(i) is not int or i < 0 for i in source)
            or source != list(range(source[0], source[0] + len(source)))):
        raise ValueError('Partial token source identity is invalid.')
    issues = row.get('native_timing_issues')
    if not isinstance(issues, list) or not issues:
        raise ValueError('A partial cue needs preserved evidence for its untimed words.')
    issue_map = {}
    for issue in issues:
        if not isinstance(issue, dict) or type(issue.get('token_index')) is not int or issue['token_index'] in issue_map:
            raise ValueError('Duplicate or malformed untimed evidence.')
        issue_map[issue['token_index']] = issue
    mapping, used, missing = [], {}, []
    for label, index, entry in zip(text.split(), source, tokens):
        if not isinstance(entry, dict) or 'word_index' not in entry or entry.get('text') != label or entry.get('token_index') != index:
            raise ValueError('Partial display changed, omitted or reordered the full cue text.')
        wi = entry.get('word_index')
        if wi is None:
            _validate_issue(issue_map.get(index), label, index, region, row, words)
            missing.append(index)
        else:
            if type(wi) is not int or not 0 <= wi < len(words):
                raise ValueError('Partial display refers to a nonexistent positive word.')
            word = words[wi]
            if word.get('region') != region or type(word.get('token_index')) is not int:
                raise ValueError('Positive word lost its native source token identity.')
            used.setdefault(wi, []).append((index, label))
        mapping.append((label, wi))
    if missing != list(issue_map) or set(used) != set(range(len(words))):
        raise ValueError('Partial evidence does not exactly cover missing and positive words.')
    last = -1
    for wi, word in enumerate(words):
        labels = word['text'].split()
        expected = list(zip(range(word['token_index'], word['token_index'] + len(labels)), labels))
        if used[wi] != expected or expected[0][0] <= last:
            raise ValueError('Partial token mapping changed a supported word or group.')
        last = expected[-1][0]
    return mapping


def partial_issues(cue):
    """Only validated untimed-token evidence is eligible for explicit review."""
    if partial_tokens(cue) is None:
        return []
    return deepcopy(_row(cue)['native_timing_issues'])


def preserve_native_support(rows):
    """Copy raw partial rows, or explicitly recover exact legacy automatic groups.

    Manual grouped phrase corrections and ordinary rows remain byte-for-byte
    unchanged. No installed/saved artifact is changed by loading this module.
    """
    result = deepcopy(rows)
    for row in result:
        if not isinstance(row, dict):
            raise ValueError('Native support rows must be lyric dictionaries.')
        if 'partial_timing' in row:
            partial_tokens(row)
            continue
        words = row.get('words', [])
        legacy = (row.get('timing_kind') == 'automatic_group_existing_support'
                  and len(words) == 1 and 'automatic_supported_group_timing' in words[0].get('flags', []))
        if legacy:
            group = words[0]
            flags = group.get('flags', [])
            manually_edited = (group.get('highlight_allowed') is False
                or any(str(flag).startswith(('manual_', 'operator_')) for flag in flags)
                or str(group.get('timing_status', '')).startswith('operator_')
                or str(row.get('timing_status', '')).startswith('operator_'))
            if manually_edited:
                continue  # explicit recovery must not undo later manual timing
        if legacy:
            group = deepcopy(words[0])
            native = group.get('native_constituents')
            issues = group.get('native_timing_issues_before_automatic_grouping')
            if (not isinstance(native, list) or not native or not isinstance(issues, list) or not issues
                    or group.get('text') != row.get('text') or group.get('start') != row.get('start')
                    or group.get('end') != row.get('end') or not group.get('grouped_text')):
                raise ValueError('Automatic group lacks exact original native support; no recovery attempted.')
            if row.get('resolved_native_timing_issues', issues) != issues:
                raise ValueError('Automatic group timing evidence is inconsistent.')
            supported = []
            for word in native:
                if not isinstance(word, dict):
                    raise ValueError('Automatic group native support is malformed.')
                if word.get('highlight_allowed', True):
                    supported.extend(word.get('highlight_intervals', [[word.get('start'), word.get('end')]]))
            if (group.get('start') != native[0].get('start') or group.get('end') != native[-1].get('end')
                    or group.get('highlight_intervals') != supported):
                raise ValueError('Automatic group support changed after grouping; no recovery or timing rewrite attempted.')
            row['words'] = deepcopy(native)
            row['native_timing_issues'] = deepcopy(issues)
            row['partial_recovery'] = {'kind': 'automatic_group_unpacked', 'previous_group': group,
                'previous_timing_kind': row.get('timing_kind'),
                'previous_resolved_native_timing_issues': row.pop('resolved_native_timing_issues', None)}
        elif not row.get('native_timing_issues'):
            continue
        positive = _positive_words(row)
        region, source = row.get('region'), row.get('source_token_indices')
        if not isinstance(source, list) or len(source) != len(row.get('text', '').split()):
            raise ValueError('Partial recovery needs the exact original source token indices.')
        by_token = {}
        for wi, word in enumerate(positive):
            index = word.get('token_index')
            if type(index) is not int:
                raise ValueError('Native positive support has no original token index.')
            for i, label in enumerate(word['text'].split(), index):
                if i in by_token:
                    raise ValueError('Native positive support has duplicate token identities.')
                by_token[i] = (label, wi)
        entries = []
        for label, index in zip(row['text'].split(), source):
            matched = by_token.get(index)
            if matched and matched[0] != label:
                raise ValueError('Native support does not match the current supplied words.')
            entries.append({'text': label, 'token_index': index, 'word_index': matched[1] if matched else None})
        row['partial_timing'] = {'version': VERSION, 'region': region, 'tokens': entries}
        row['timing_kind'] = 'partial_native_word_support'
        # These were the old all-or-nothing cue placeholders, not word support.
        if row.get('highlight_allowed') is False and row.get('highlight_intervals') == []:
            row['partial_original_highlight_policy'] = {'highlight_allowed': False, 'highlight_intervals': []}
            row.pop('highlight_allowed'); row.pop('highlight_intervals')
        partial_tokens(row)
    return result
