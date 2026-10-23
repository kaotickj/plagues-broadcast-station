"""Portable, explicit ASR settings and lossless recognition diagnostics."""
from copy import deepcopy
import hashlib
import json
import math
import os
import re

VERSION = 'recognition-policy-1'
QWEN_LANGUAGES = dict(zip(
    'zh en yue ar de fr es pt id it ko ru th vi ja tr hi ms nl sv da fi pl cs fil fa el hu mk ro'.split(),
    ['Chinese', 'English', 'Cantonese', 'Arabic', 'German', 'French', 'Spanish',
     'Portuguese', 'Indonesian', 'Italian', 'Korean', 'Russian', 'Thai', 'Vietnamese',
     'Japanese', 'Turkish', 'Hindi', 'Malay', 'Dutch', 'Swedish', 'Danish', 'Finnish',
     'Polish', 'Czech', 'Filipino', 'Persian', 'Greek', 'Hungarian', 'Macedonian', 'Romanian']))


def recognition_policy(engine, *, independent=False):
    language = os.environ.get('GNB_LYRICS_LANGUAGE', 'en').strip().lower()
    task = os.environ.get('GNB_LYRICS_TASK', 'transcribe').strip().lower()
    prompt = os.environ.get('GNB_LYRICS_PROMPT', '')
    if language != 'auto' and not re.fullmatch(r'[a-z]{2,3}', language):
        raise ValueError('Choose auto or a two/three-letter recognition language code.')
    if task != 'transcribe':
        raise ValueError('Lyric recognition supports transcription, not translation.')
    if len(prompt) > 500 or any((ord(c) < 32 and c not in '\n\t') or ord(c) == 127 for c in prompt):
        raise ValueError('An explicit recognition prompt must be at most 500 characters without control characters.')
    effective = 'en' if engine.endswith('.en') else language
    if engine.endswith('.en') and language not in {'en', 'auto'}:
        raise ValueError('The selected English-only recognizer cannot transcribe this language.')
    return {'version': VERSION, 'task': task, 'requested_language': language,
            'language': effective, 'prompt': '' if independent else prompt,
            'prompt_source': 'none' if independent or not prompt else 'explicit-setting',
            'condition_on_previous_text': False}


def cache_policy(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def qwen_language(policy):
    if policy['language'] == 'auto':
        return None
    if policy['language'] not in QWEN_LANGUAGES:
        raise ValueError('The selected recognition language is not supported by the Qwen recognizer.')
    return QWEN_LANGUAGES[policy['language']]


def finite_number(value):
    return float(value) if isinstance(value, (float, int)) and math.isfinite(value) else None


def segment_evidence(segment, *, offset=0):
    words = [{'text': w.word, 'start': offset + float(w.start), 'end': offset + float(w.end),
              'score': finite_number(getattr(w, 'probability', None))}
             for w in segment.words or []]
    result = {'text': getattr(segment, 'text', ''.join(w['text'] for w in words)),
              'start': offset + float(getattr(segment, 'start', 0)),
              'end': offset + float(getattr(segment, 'end', 0)), 'words': words}
    for name in ('avg_logprob', 'compression_ratio', 'no_speech_prob', 'temperature'):
        result[name] = finite_number(getattr(segment, name, None))
    return result


def recognition_suspect(segment):
    low = getattr(segment, 'avg_logprob', 0)
    compressed = getattr(segment, 'compression_ratio', 0)
    silence = getattr(segment, 'no_speech_prob', 0)
    return bool((isinstance(low, (int, float)) and low < -1)
                or (isinstance(compressed, (int, float)) and compressed > 2.8)
                or (isinstance(silence, (int, float)) and silence >= .6))


def transcript_snapshot(data):
    """Keep the actual draft before/after review without recursive evidence copies."""
    return deepcopy({key: data[key] for key in ('segments', 'duration', 'warnings', 'pipeline', 'metrics')
                     if key in data})
