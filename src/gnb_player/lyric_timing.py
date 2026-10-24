"""Conservative reconciliation of millisecond and centisecond line boundaries."""
from copy import deepcopy
import math


def validate_line_interval(start, end, duration, previous, number):
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start or end > duration:
        raise ValueError(f"Line {number}: {start:.3f}–{end:.3f}s must be a positive interval within the recording ({duration:.3f}s).")
    if start < previous:
        raise ValueError(f"Line {number} starts at {start:.3f}s before line {number - 1} ends at {previous:.3f}s "
                         f"({previous - start:.3f}s overlap). Adjust these line times; nothing was saved.")


def normalize_rounded_boundaries(segments):
    """Copy, never sort/drop lines or words; reconcile only a rounded start.

    Independent review emits centiseconds while CTC retains milliseconds. A
    centisecond start that equals the preceding millisecond end rounded down
    can overlap by at most 5ms. Snap only that signature, never general overlaps.
    Raw recognition/review evidence is intentionally outside this operation.
    """
    result, adjustments = deepcopy(segments), []
    for index in range(1, len(result)):
        previous, cue = result[index - 1], result[index]
        start, end, boundary = cue.get('start'), cue.get('end'), previous.get('end')
        if not all(type(value) in (int, float) and math.isfinite(value) for value in (start, end, boundary)):
            continue
        overlap = boundary - start
        if not (0 < overlap <= .005 + 1e-9 and boundary < end and
                abs(boundary - round(boundary, 3)) < 1e-9 and
                abs(start - round(boundary, 2)) < 1e-9):
            continue
        words = cue.get('words', [])
        # Only the first word sharing the old line start may move. Never
        # shorten a word to zero or mask unrelated invalid word intervals.
        if words:
            first = words[0]
            a, b = first.get('start'), first.get('end')
            if not all(type(v) in (int, float) and math.isfinite(v) for v in (a, b)):
                continue
            if a < boundary:
                if a != start or b <= boundary:
                    continue
                first['start'] = boundary
        cue['start'] = boundary
        adjustments.append({'line': index + 1, 'previous_line': index,
                            'old_start': start, 'start': boundary,
                            'reason': 'centisecond-start/millisecond-end rounding'})
    return result, adjustments


def normalize_generated_timing(data):
    """Normalize final generated lines and reject genuine unsaveable overlaps."""
    segments, adjustments = normalize_rounded_boundaries(data.get('segments', []))
    previous = 0.0
    duration = float(data.get('duration', 0))
    for number, cue in enumerate(segments, 1):
        validate_line_interval(float(cue['start']), float(cue['end']), duration, previous, number)
        previous = cue['end']
    data['segments'] = segments
    if adjustments:
        data.setdefault('timing_boundary_adjustments', []).extend(adjustments)
    return adjustments
