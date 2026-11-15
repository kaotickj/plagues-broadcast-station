"""Conservative text-only degeneration hints, never an instrumental classifier.

No phrase blacklist. A held passage may be genuine singing; preserve its text and
allow an explicit reviewed override. CTC confidence is deliberately not used.
This module is also imported directly by the offline worker.
"""
import re

VERSION = 'consecutive-repetition-1'


def repetition(text):
    tokens = re.findall(r"[\w']+", text.casefold())
    if len(tokens) < 20:
        return None
    # At least four consecutive copies covering 80% of a decoder region.
    # Choruses separated by different words do not count as consecutive copies.
    best = None
    for width in range(1, min(12, len(tokens)//4) + 1):
        next_start = 0
        for start in range(len(tokens)-width*4+1):
            if start < next_start:
                continue
            pattern = tokens[start:start+width]
            end = start+width
            copies = 1
            while tokens[end:end+width] == pattern:
                copies += 1
                end += width
            fraction = (end-start)/len(tokens)
            if copies > 1:
                next_start = end-width
            if copies >= 4 and fraction >= .8:
                candidate = {'pattern': ' '.join(pattern), 'copies': copies,
                             'covered_tokens': end-start, 'token_count': len(tokens),
                             'fraction': fraction}
                if best is None or (fraction, -width) > (best['fraction'], -len(best['pattern'].split())):
                    best = candidate
    return best


def inspect_regions(regions):
    held = []
    for index, region in enumerate(regions):
        evidence = repetition(region.get('text', ''))
        if evidence:
            held.append({'region': index, 'start': float(region['start']),
                         'end': float(region['end']), 'text': region['text'],
                         'status': 'held', 'detector': VERSION, 'evidence': evidence,
                         'reason': 'Consecutive repetition; listen before using these words. This is not an instrumental diagnosis.',
                         'timing': 'Coarse review window only; no accepted word timings.'})
    return held


def held_indices(held):
    return {x['region'] for x in held if x.get('status') == 'held'}
