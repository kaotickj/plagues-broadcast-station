"""Disposable content-identified ASR cache fixtures; no model inference."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from contextlib import nullcontext
import sys
import wave
from types import SimpleNamespace

import pytest

from gnb_player.extraction_backend import lyrics_cache as cache


def raw_recognition():
    return {
        'systeminfo': 'fixture, no recognizer process',
        'params': {'language': 'en', 'translate': False},
        'result': {'language': 'en'},
        'transcription': [
            {'text': ' Rain', 'offsets': {'from': 100, 'to': 800},
             'tokens': [{'text': ' Rain', 'p': .91}]},
            {'text': ' falls', 'offsets': {'from': 1000, 'to': 1700},
             'tokens': [{'text': ' falls', 'p': .94}]},
        ],
    }


@pytest.fixture
def request_identity(tmp_path, monkeypatch):
    monkeypatch.setenv('GNB_LYRICS_CACHE_DIR', str(tmp_path / 'derived'))
    return {'model_sha256': 'a' * 64, 'runtime_sha256': 'b' * 64,
            'duration': 4.0, 'start': 0.0, 'end': 4.0}


def policy():
    return {'language': 'en', 'task': 'transcribe', 'prompt': '',
            'condition_on_previous_text': False,
            'decoder': {'beam_size': 3, 'best_of': 3, 'dtw': 'large.v3'}}


def cache_path(identity):
    return cache.primary_path('c' * 64, identity, policy(), 0, 4)


def test_complete_raw_roundtrip_is_lossless_private_and_read_copy(request_identity):
    path = cache_path(request_identity)
    raw = raw_recognition(); original = deepcopy(raw)
    assert cache.write_primary(path, raw, request_identity)
    assert raw == original
    assert cache.read_primary(path, request_identity) == original
    assert path.stat().st_mode & 0o777 == 0o600
    first = cache.read_primary(path, request_identity)
    first['transcription'][0]['text'] = 'edited caller copy'
    assert cache.read_primary(path, request_identity) == original
    assert not list(path.parent.glob('.*'))


def test_pcm_model_runtime_policy_and_offsets_are_distinct_keys(request_identity):
    identity = request_identity
    base = cache_path(identity)
    assert cache.primary_path('d' * 64, identity, policy(), 0, 4) != base
    for key in ('model_sha256', 'runtime_sha256'):
        changed = {**identity, key: 'e' * 64}
        assert cache_path(changed) != base
    variants = []
    for key, value in [('language', 'fr'), ('prompt', 'Explicit context')]:
        changed = policy(); changed[key] = value; variants.append(changed)
    changed = policy(); changed['decoder']['beam_size'] = 1; variants.append(changed)
    changed = policy(); changed['decoder']['dtw'] = None; variants.append(changed)
    for changed in variants:
        assert cache.primary_path('c' * 64, identity, changed, 0, 4) != base
    assert cache.primary_path('c' * 64, identity, policy(), 10, 14) != base


def test_same_path_read_rejects_wrong_identity(request_identity):
    path = cache_path(request_identity)
    assert cache.write_primary(path, raw_recognition(), request_identity)
    assert cache.read_primary(path, {**request_identity, 'model_sha256': 'f' * 64}) is None


@pytest.mark.parametrize('payload', [b'{', b'[]', b'null', b'{"raw":{}}', b' ' * (2 * 1024 * 1024 + 1)])
def test_malformed_or_oversized_cache_is_a_miss(request_identity, payload):
    path = cache_path(request_identity)
    path.write_bytes(payload)
    assert cache.read_primary(path, request_identity) is None


def test_valid_json_but_tampered_raw_integrity_is_a_miss(request_identity):
    path = cache_path(request_identity)
    assert cache.write_primary(path, raw_recognition(), request_identity)
    before = path.read_text()
    assert 'Rain' in before
    path.write_text(before.replace('Rain', 'Fake'))
    assert cache.read_primary(path, request_identity) is None


def test_symlink_and_disabled_cache_never_change_link_target(request_identity, tmp_path, monkeypatch):
    target = tmp_path / 'protected.json'; target.write_text('keep this file')
    path = cache_path(request_identity); path.symlink_to(target)
    assert cache.read_primary(path, request_identity) is None
    assert not cache.write_primary(path, raw_recognition(), request_identity)
    assert target.read_text() == 'keep this file'
    assert path.is_symlink()
    assert cache.read_primary(None, request_identity) is None
    assert not cache.write_primary(None, raw_recognition(), request_identity)
    monkeypatch.delenv('GNB_LYRICS_CACHE_DIR')
    assert cache_path(request_identity) is None


@pytest.mark.parametrize('bounds', [(-1, 800), (800, 100), (100, 35000), (False, 800), (float('nan'), 800), (100, float('inf'))])
def test_malformed_or_beyond_padding_raw_times_are_not_cached(request_identity, bounds):
    path = cache_path(request_identity); raw = raw_recognition()
    raw['transcription'][0]['offsets'] = dict(zip(('from', 'to'), bounds))
    assert not cache.write_primary(path, raw, request_identity)
    assert cache.read_primary(path, request_identity) is None


def raw_with_actual_failure_timing_shapes():
    """Compact Nobody Knows shape: blank row, long Thank, lexical zero, padded tail."""
    raw = raw_recognition()
    raw['transcription'] = [
        {'text': '', 'offsets': {'from': 0, 'to': 0},
         'tokens': [{'text': '[_BEG_]', 'p': 1.0}]},
        {'text': ' Thank', 'offsets': {'from': 225100, 'to': 244210},
         'tokens': [{'text': ' Thank', 'p': .209701}]},
        {'text': ' i', 'offsets': {'from': 290060, 'to': 290060},
         'tokens': [{'text': ' i', 'p': .4}]},
        {'text': ' cooled', 'offsets': {'from': 479000, 'to': 503000},
         'tokens': [{'text': ' cooled', 'p': .3}]},
    ]
    return raw


def test_raw_zero_duration_and_padded_tail_roundtrip_without_fabricating_timings(request_identity):
    from gnb_player.extraction_backend.lyrics_acceleration import parse_words
    identity = {**request_identity, 'duration': 479.921625, 'end': 479.921625}
    path = cache.primary_path('c' * 64, identity, policy(), 0, identity['duration'])
    raw = raw_with_actual_failure_timing_shapes(); before = deepcopy(raw)
    assert cache.PRIMARY_SCHEMA == 'raw-primary-recognition-2'
    assert cache.write_primary(path, raw, identity)
    restored = cache.read_primary(path, identity)
    assert restored == before and raw == before
    assert parse_words(restored) == parse_words(before)
    words = parse_words(restored)
    assert words[1]['text'] == 'i' and words[1]['start'] == words[1]['end'] == 290.06
    assert words[2]['end'] == 503 and words[2]['end'] > identity['duration']
    assert not any(key in restored for key in ('human_review', 'publication', 'reviewed'))


def test_cached_long_zero_evidence_still_fails_original_alignment_guard(request_identity):
    from gnb_player.extraction_backend.lyrics_acceleration import parse_words, validate_alignment, AlignmentRejected
    identity = {**request_identity, 'duration': 479.921625, 'end': 479.921625}
    path = cache.primary_path('c' * 64, identity, policy(), 0, identity['duration'])
    raw = raw_with_actual_failure_timing_shapes()
    assert cache.write_primary(path, raw, identity)
    uncached = parse_words(raw)
    cached = parse_words(cache.read_primary(path, identity))
    assert cached == uncached
    messages = []
    for words in (uncached, cached):
        aligned = [{'text': word['text'], 'start': word['start'], 'end': word['end'], 'score': .8}
                   for word in words]
        with pytest.raises(AlignmentRejected, match='more than 12 seconds') as error:
            validate_alignment(words, aligned, identity['duration'])
        messages.append(str(error.value))
    assert messages[0] == messages[1]
    assert 'No replacement lyrics were saved' in messages[1]


def test_cache_write_failure_is_optional_and_keeps_other_files(request_identity, monkeypatch, tmp_path):
    path = cache_path(request_identity)
    protected = tmp_path / 'operator-lyrics.json'; protected.write_text('雨 — retain my work')
    def fail(*args, **kwargs): raise OSError('fixture: disk full or read-only cache')
    monkeypatch.setattr(cache, 'atomic', fail)
    assert not cache.write_primary(path, raw_recognition(), request_identity)
    assert protected.read_text() == '雨 — retain my work'


def _identity_module():
    import importlib
    return importlib.import_module('gnb_player.extraction_backend.lyrics_identity')


def _replace_preserving_mtime(path, content):
    stamp = path.stat()
    assert len(content) == stamp.st_size
    path.write_bytes(content)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))


def test_model_same_path_size_and_mtime_change_invalidates_memoized_identity(tmp_path, monkeypatch):
    identity = _identity_module()
    model = tmp_path / 'model.bin'; model.write_bytes(b'model-v1')
    binary = tmp_path / 'whisper-cli'; binary.write_bytes(b'binary-v1')
    monkeypatch.setattr(identity, 'runtime_dependencies', lambda selected: [])
    first = identity.recognizer_identity(model, binary)
    assert identity.recognizer_identity(model, binary) == first
    _replace_preserving_mtime(model, b'model-v2')
    second = identity.recognizer_identity(model, binary)
    assert first != second
    assert hashlib.sha256(b'model-v2').hexdigest() in json.dumps(second)


def test_atomic_same_size_model_replacement_invalidates_identity(tmp_path, monkeypatch):
    identity = _identity_module()
    model = tmp_path / 'model.bin'; model.write_bytes(b'model-v1')
    binary = tmp_path / 'whisper-cli'; binary.write_bytes(b'binary-v1')
    monkeypatch.setattr(identity, 'runtime_dependencies', lambda selected: [])
    before = identity.recognizer_identity(model, binary); stamp = model.stat()
    replacement = tmp_path / 'new-model'; replacement.write_bytes(b'model-v2')
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    replacement.replace(model)
    assert identity.recognizer_identity(model, binary) != before


def test_same_path_binary_and_backend_library_changes_invalidate_identity(tmp_path, monkeypatch):
    identity = _identity_module()
    model = tmp_path / 'model.bin'; model.write_bytes(b'model-v1')
    binary = tmp_path / 'whisper-cli'; binary.write_bytes(b'binary-v1'); binary.chmod(0o755)
    library = tmp_path / 'libwhisper.so'; library.write_bytes(b'library-v1')
    monkeypatch.setattr(identity, 'runtime_dependencies', lambda selected: [library])
    first = identity.recognizer_identity(model, binary)
    assert identity.recognizer_identity(model, binary) == first
    _replace_preserving_mtime(binary, b'binary-v2')
    second = identity.recognizer_identity(model, binary)
    assert second != first
    _replace_preserving_mtime(library, b'library-v2')
    third = identity.recognizer_identity(model, binary)
    assert third != second
    assert hashlib.sha256(b'library-v2').hexdigest() in json.dumps(third)


@pytest.mark.parametrize('field', ['human_review', 'publication', 'reviewed'])
def test_raw_cache_cannot_carry_approval_or_publication(request_identity, field):
    raw = raw_recognition(); raw[field] = {'status': 'approved'} if field != 'reviewed' else True
    path = cache_path(request_identity)
    assert not cache.write_primary(path, raw, request_identity)
    assert cache.read_primary(path, request_identity) is None


@pytest.fixture
def mocked_recognizer(tmp_path, monkeypatch):
    import numpy as np
    from gnb_player.extraction_backend import lyrics_acceleration as gpu
    identity = _identity_module()
    model = tmp_path / 'model.bin'; model.write_bytes(b'model-v1')
    binary = tmp_path / 'whisper-cli'; binary.write_bytes(b'binary-v1'); binary.chmod(0o755)
    monkeypatch.setattr(gpu, 'GGML', model)
    monkeypatch.setattr(gpu, 'VULKAN', binary)
    monkeypatch.setattr(identity, 'runtime_dependencies', lambda selected: [])
    # Backend modules are also imported as standalone files by the model worker.
    monkeypatch.setitem(sys.modules, 'lyrics_cache', cache)
    monkeypatch.setitem(sys.modules, 'lyrics_identity', identity)
    monkeypatch.setenv('GNB_LYRICS_CACHE_DIR', str(tmp_path / 'derived'))
    monkeypatch.setenv('GNB_LYRICS_LANGUAGE', 'en')
    monkeypatch.setenv('GNB_LYRICS_TASK', 'transcribe')
    monkeypatch.delenv('GNB_LYRICS_PROMPT', raising=False)
    monkeypatch.delenv('GNB_LYRICS_FRESH_RECOGNITION', raising=False)
    def write_wave(path, values, sample_rate, subtype):
        assert subtype == 'PCM_16'
        with wave.open(str(path), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(sample_rate)
            stream.writeframes((np.clip(values, -1, 1) * 32767).astype('<i2').tobytes())
    monkeypatch.setitem(sys.modules, 'soundfile', SimpleNamespace(write=write_wave))
    leases = []
    monkeypatch.setattr(gpu, 'gpu_phase', lambda *a: leases.append(a) or nullcontext())
    calls = []
    def recognize(args, **kwargs):
        calls.append(args)
        output = Path(args[args.index('-of') + 1]).with_suffix('.json')
        output.write_text(json.dumps(raw_recognition()))
        return SimpleNamespace(returncode=0, stderr=b'')
    monkeypatch.setattr(gpu.subprocess, 'run', recognize)
    work = tmp_path / 'work'; work.mkdir()
    return SimpleNamespace(gpu=gpu, identity=identity, model=model, binary=binary,
                           calls=calls, leases=leases, work=work,
                           audio=np.full(4 * 16000, .1, dtype=np.float32))


def test_warm_hit_skips_recognizer_and_lease_but_preserves_evidence_and_offset(mocked_recognizer, monkeypatch):
    f = mocked_recognizer
    first_evidence = {}; second_evidence = {}
    first = f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None, evidence=first_evidence)
    assert len(f.calls) == len(f.leases) == 1
    second = f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None, evidence=second_evidence)
    assert first == second
    assert len(f.calls) == len(f.leases) == 1
    assert second_evidence['windows'][0]['cached'] is True
    assert second_evidence['windows'][0]['raw'] == raw_recognition()
    monkeypatch.setenv('GNB_LYRICS_FRESH_RECOGNITION', '1')
    fresh = f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None)
    assert fresh == first and len(f.calls) == len(f.leases) == 2


def test_warm_bad_raw_hit_skips_asr_but_repeats_same_alignment_rejection(mocked_recognizer, monkeypatch):
    import numpy as np
    f = mocked_recognizer; audio = np.tile(f.audio, 8)  # 32-second disposable signal.
    raw = raw_with_actual_failure_timing_shapes()
    for row, bounds in zip(raw['transcription'], [(0, 0), (100, 19210), (20060, 20060), (22000, 30000)]):
        row['offsets'] = dict(zip(('from', 'to'), bounds))
    def recognize(args, **kwargs):
        f.calls.append(args)
        Path(args[args.index('-of') + 1]).with_suffix('.json').write_text(json.dumps(raw))
        return SimpleNamespace(returncode=0, stderr=b'')
    monkeypatch.setattr(f.gpu.subprocess, 'run', recognize)
    first = f.gpu.recognize_windows(audio, 16000, [('full', 0, 32)], f.work, lambda *a: None)
    evidence = {}
    second = f.gpu.recognize_windows(audio, 16000, [('full', 0, 32)], f.work, lambda *a: None, evidence=evidence)
    assert len(f.calls) == len(f.leases) == 1
    assert evidence['windows'][0]['cached'] is True and evidence['windows'][0]['raw'] == raw
    assert first == second
    messages = []
    for result in (first, second):
        words = result['full']
        aligned = [{**word, 'score': .8} for word in words]
        with pytest.raises(f.gpu.AlignmentRejected, match='more than 12 seconds') as error:
            f.gpu.validate_alignment(words, aligned, 32)
        messages.append(str(error.value))
    assert messages[0] == messages[1]


def test_model_change_after_first_pass_forces_actual_recognition(mocked_recognizer):
    f = mocked_recognizer
    f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None)
    _replace_preserving_mtime(f.model, b'model-v2')
    f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None)
    assert len(f.calls) == 2


def test_nonzero_recognizer_exit_never_creates_reusable_entry(mocked_recognizer, monkeypatch):
    f = mocked_recognizer
    monkeypatch.setattr(f.gpu.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1, stderr=b'fixture failure'))
    with pytest.raises(RuntimeError, match='recognition failed'):
        f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None)
    assert not list((f.work.parent / 'derived').glob('*.json'))


def test_identity_failure_disables_cache_without_blocking_recognition(mocked_recognizer, monkeypatch):
    f = mocked_recognizer
    def unavailable(*args): raise RuntimeError('fixture runtime dependency cannot be identified')
    monkeypatch.setattr(f.identity, 'recognizer_identity', unavailable)
    first_evidence = {}; second_evidence = {}
    first = f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None, evidence=first_evidence)
    second = f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None, evidence=second_evidence)
    assert first == second and len(f.calls) == 2
    assert first_evidence['cache_disabled_reason'] and second_evidence['cache_disabled_reason']
    assert not list((f.work.parent / 'derived').glob('*.json'))


def test_dependency_changed_during_recognition_is_not_cached(mocked_recognizer, monkeypatch):
    f = mocked_recognizer; original = f.gpu.subprocess.run
    def change_during_call(*args, **kwargs):
        result = original(*args, **kwargs)
        _replace_preserving_mtime(f.model, b'model-v2')
        return result
    monkeypatch.setattr(f.gpu.subprocess, 'run', change_during_call)
    with pytest.raises(RuntimeError, match='changed during recognition'):
        f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None)
    assert not list((f.work.parent / 'derived').glob('*.json'))
    assert not list(f.work.iterdir())


def test_cancelled_recognition_cleans_owned_work_and_creates_no_cache(mocked_recognizer, monkeypatch):
    f = mocked_recognizer
    def cancel(*args, **kwargs): raise KeyboardInterrupt('fixture cancellation')
    monkeypatch.setattr(f.gpu.subprocess, 'run', cancel)
    with pytest.raises(KeyboardInterrupt):
        f.gpu.recognize_windows(f.audio, 16000, [('full', 0, 4)], f.work, lambda *a: None)
    assert not list((f.work.parent / 'derived').glob('*.json'))
    assert not list(f.work.iterdir())


def test_identical_repeated_audio_at_different_positions_uses_correct_absolute_times(mocked_recognizer):
    import numpy as np
    f = mocked_recognizer; audio = np.tile(f.audio, 4)
    first = f.gpu.recognize_windows(audio, 16000, [('first', 0, 4)], f.work, lambda *a: None)
    repeat = f.gpu.recognize_windows(audio, 16000, [('chorus', 10, 14)], f.work, lambda *a: None)
    assert len(f.calls) == 2
    assert first['first'][0]['start'] == .1
    assert repeat['chorus'][0]['start'] == 10.1
    assert repeat['chorus'][1]['end'] == 11.7


@pytest.mark.parametrize('tokens', [None, 'invalid list', [None], [{'text': 'Rain', 'p': True}],
                                  [{'text': 'Rain', 'p': float('nan')}], [{'text': 'Rain', 'p': -1}],
                                  [{'text': 'Rain', 'p': 1.1}]])
def test_malformed_token_evidence_is_not_reusable(request_identity, tokens):
    raw = raw_recognition(); raw['transcription'][0]['tokens'] = tokens
    path = cache_path(request_identity)
    assert not cache.write_primary(path, raw, request_identity)
    assert cache.read_primary(path, request_identity) is None


def test_out_of_order_raw_rows_are_not_cached(request_identity):
    raw = raw_recognition()
    raw['transcription'][1]['offsets'] = {'from': 0, 'to': 70}
    path = cache_path(request_identity)
    assert not cache.write_primary(path, raw, request_identity)
    assert cache.read_primary(path, request_identity) is None
