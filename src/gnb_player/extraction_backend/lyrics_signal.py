"""Measured ASR input checks, not a classifier for vocals or instrumental music."""
import math
import re
from collections import Counter


VERSION = 'signal-checks-1'


def measure(audio, sr):
    import numpy as np
    values = np.asarray(audio)
    if not isinstance(sr, (int, float)) or not math.isfinite(sr) or sr <= 0:
        raise ValueError('The recognition audio has an invalid sample rate.')
    if values.ndim != 1 or not values.size or not np.isfinite(values).all():
        raise ValueError('The recognition audio is empty or contains invalid samples.')
    frame = max(1, int(sr))
    energies = []
    total = 0.0
    peak = 0.0
    for start in range(0, len(values), frame):
        block = values[start:start + frame].astype('float64')
        energy = float(np.dot(block, block))
        total += energy
        energies.append(energy / len(block))
        peak = max(peak, float(np.max(np.abs(block))))
    db = lambda power: round(10 * math.log10(max(power, 1e-12)), 3)
    return {'version': VERSION, 'duration': len(values) / sr,
            'rms_dbfs': db(total / len(values)), 'peak_dbfs': db(peak * peak),
            'active_fraction': sum(e > 1e-6 for e in energies) / len(energies),
            'activity_threshold_dbfs': -60}


def limited_signal(metrics):
    return metrics['active_fraction'] < .25


def inspect_input(audio, sr, *, separated=False, source=None):
    metrics = measure(audio, sr)
    report = {'version': VERSION, 'recognition_input': metrics, 'separated': separated}
    if source is not None:
        report['original_mix'] = source
        report['level_drop_db'] = round(source['rms_dbfs'] - metrics['rms_dbfs'], 3)
        report['suspect_stem'] = bool(separated and limited_signal(metrics)
                                      and source['rms_dbfs'] > -35
                                      and report['level_drop_db'] >= 12)
    else:
        report['suspect_stem'] = False
    # A short silence or a quiet song is not an extraction failure. This only
    # rejects an effectively empty whole input, before asking models for words.
    report['effectively_empty'] = metrics['rms_dbfs'] <= -80 and metrics['peak_dbfs'] <= -60
    return report


def recognition_diagnostics(cues, duration):
    words = [w for cue in cues for w in cue.get('words', [])]
    tokens = [re.sub(r'[^\w]+', '', str(w.get('text', ''))).casefold() for w in words]
    counts = Counter(t for t in tokens if t)
    stretched = sum(float(w['end']) - float(w['start']) > 8 for w in words)
    return {'word_count': len(words), 'words_per_second': len(words) / max(duration, 1),
            'stretched_fraction': stretched / max(len(words), 1),
            'two_token_fraction': sum(n for _, n in counts.most_common(2)) / max(len(tokens), 1)}


def require_usable_recognition(cues, duration, report):
    diagnostics = recognition_diagnostics(cues, duration)
    report['recognition'] = diagnostics
    # Combine independent signal and output measurements. Neither quiet musical
    # breaks nor genuine repeated lyrics alone is grounds for rejecting a draft.
    collapsed = (diagnostics['words_per_second'] < .12
                 and diagnostics['stretched_fraction'] >= .5
                 and diagnostics['two_token_fraction'] >= .65)
    if report.get('suspect_stem') and collapsed:
        raise ValueError('The separated vocal track has very little usable signal and recognition '
                         'collapsed into sparse repeated words with unreliable timing. '
                         'Try this recording with Separate vocals disabled, or align a known lyric sheet. '
                         'No replacement lyrics were saved.')
