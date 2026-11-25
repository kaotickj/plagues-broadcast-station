"""Restricted local loaders: stub tensors/models only, no inference or weights."""
from copy import deepcopy
import hashlib
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import Mock

import pytest

from gnb_player.extraction_backend import lyrics_model_io as model_io


class Tensor:
    def __init__(self, values, shape):
        self.values = list(values)
        self.shape = shape

    def size(self, axis):
        return self.shape[axis]

    def __getitem__(self, index):
        return self.values[index]


def state():
    return {'aux.weight': Tensor(range(32), (32, 768)),
            'aux.bias': Tensor(range(32), (32,)),
            'encoder.weight': Tensor([1], (1,))}


@pytest.fixture
def bundle():
    return SimpleNamespace(_model_type='Wav2Vec2', _normalize_waveform=False,
                           sample_rate=16000, _remove_aux_axis=(1, 2, 3),
                           _params=deepcopy(model_io._BASE_PARAMS),
                           get_labels=lambda: model_io._LABELS,
                           get_model=Mock(side_effect=AssertionError('URL loader forbidden')))


@pytest.fixture
def runtime(monkeypatch):
    loaded = state()
    model = SimpleNamespace(load_state_dict=Mock(), eval=Mock())
    model.to = Mock(return_value=model)
    stack = Mock(side_effect=lambda rows: Tensor(rows, (len(rows),)))
    torch = SimpleNamespace(Tensor=Tensor, stack=stack, load=Mock(return_value=loaded))
    package = ModuleType('torchaudio')
    models = ModuleType('torchaudio.models')
    models.wav2vec2_model = Mock(return_value=model)
    package.models = models
    monkeypatch.setitem(sys.modules, 'torch', torch)
    monkeypatch.setitem(sys.modules, 'torchaudio', package)
    monkeypatch.setitem(sys.modules, 'torchaudio.models', models)
    return SimpleNamespace(torch=torch, model=model, factory=models.wav2vec2_model, original=loaded)


@pytest.fixture
def checkpoint(tmp_path):
    path = tmp_path / 'custom-checkpoint.pth'
    path.write_bytes(b'disposable non-model fixture')
    return path


def test_restricted_readonly_descriptor_and_exact_aux_conversion(bundle, runtime, checkpoint):
    original = checkpoint.read_bytes()
    def read(stream, **kwargs):
        assert stream.read() == original and stream.mode == 'rb'
        assert kwargs == {'map_location': 'cpu', 'weights_only': True}
        return runtime.original
    runtime.torch.load.side_effect = read
    model, identity = model_io.load_local_ctc_model(bundle, checkpoint, 'cuda')
    assert model is runtime.model
    assert checkpoint.read_bytes() == original
    bundle.get_model.assert_not_called()
    runtime.factory.assert_called_once_with(**model_io._BASE_PARAMS)
    runtime.model.load_state_dict.assert_called_once()
    converted = runtime.model.load_state_dict.call_args.args[0]
    assert runtime.model.load_state_dict.call_args.kwargs == {'strict': True}
    expected = [0] + list(range(4, 32))
    assert converted['aux.weight'].values == expected
    assert converted['aux.bias'].values == expected
    assert runtime.original['aux.weight'].values == list(range(32))
    runtime.model.eval.assert_called_once_with()
    runtime.model.to.assert_called_once_with('cuda')
    assert identity['sha256'] == hashlib.sha256(original).hexdigest()
    assert identity['weights_only'] and identity['local_file_only']
    assert identity['expected_digest_matched'] is None
    assert identity['provenance'] == 'content-fingerprint-only; publisher identity not verified'


def test_explicit_trusted_hash_is_checked_without_filename_assumptions(bundle, runtime, checkpoint):
    digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    _, identity = model_io.load_local_ctc_model(bundle, checkpoint, expected_sha256=digest.upper())
    assert identity['expected_sha256'] == digest and identity['expected_digest_matched'] is True
    assert identity['provenance'] == 'matched-caller-trusted-digest'


def test_hash_mismatch_stops_before_deserialization(bundle, runtime, checkpoint):
    with pytest.raises(model_io.LocalModelError, match='trusted SHA256'):
        model_io.load_local_ctc_model(bundle, checkpoint, expected_sha256='0' * 64)
    runtime.torch.load.assert_not_called()
    runtime.factory.assert_not_called()


@pytest.mark.parametrize('digest', ['', 'xyz', 42, True, 'a' * 63])
def test_invalid_expected_hash_rejected(bundle, runtime, checkpoint, digest):
    with pytest.raises(model_io.LocalModelError, match='64 hexadecimal'):
        model_io.load_local_ctc_model(bundle, checkpoint, expected_sha256=digest)
    runtime.torch.load.assert_not_called()


@pytest.mark.parametrize('kind', ['missing', 'empty', 'directory', 'symlink', 'fifo'])
def test_nonregular_missing_empty_checkpoint_never_loaded(bundle, runtime, tmp_path, kind):
    path = tmp_path / 'checkpoint'
    if kind == 'empty': path.touch()
    elif kind == 'directory': path.mkdir()
    elif kind == 'symlink':
        target = tmp_path / 'target'; target.write_bytes(b'unchanged'); path.symlink_to(target)
    elif kind == 'fifo':
        import os
        if not hasattr(os, 'mkfifo'): pytest.skip('POSIX fifo unavailable')
        os.mkfifo(path)
    with pytest.raises(model_io.LocalModelError):
        model_io.load_local_ctc_model(bundle, path)
    runtime.torch.load.assert_not_called()
    runtime.factory.assert_not_called()


@pytest.mark.parametrize('change', ['replace', 'modify', 'unlink'])
def test_changed_file_during_load_is_not_accepted(bundle, runtime, checkpoint, change):
    def load(stream, **kwargs):
        assert stream.read() == b'disposable non-model fixture'
        if change == 'replace':
            replacement = checkpoint.with_name('replacement')
            replacement.write_bytes(b'different bytes'); replacement.replace(checkpoint)
        elif change == 'modify': checkpoint.write_bytes(b'changed')
        else: checkpoint.unlink()
        return runtime.original
    runtime.torch.load.side_effect = load
    with pytest.raises(model_io.LocalModelError, match='changed'):
        model_io.load_local_ctc_model(bundle, checkpoint)
    runtime.factory.assert_not_called()


def test_restricted_load_error_never_retries_unsafe_or_url(bundle, runtime, checkpoint):
    runtime.torch.load.side_effect = RuntimeError('pickle GLOBAL is not allowed')
    with pytest.raises(model_io.LocalModelError, match='restricted tensor loader'):
        model_io.load_local_ctc_model(bundle, checkpoint)
    assert runtime.torch.load.call_count == 1
    assert runtime.torch.load.call_args.kwargs['weights_only'] is True
    runtime.factory.assert_not_called()
    bundle.get_model.assert_not_called()


@pytest.mark.parametrize('loaded', [None, [], {}, {'extra': 'not tensor'}, {'state_dict': state()}])
def test_nontensor_training_or_nested_objects_are_rejected(bundle, runtime, checkpoint, loaded):
    runtime.torch.load.return_value = loaded
    with pytest.raises(model_io.LocalModelError, match='tensor state dictionary'):
        model_io.load_local_ctc_model(bundle, checkpoint)
    runtime.factory.assert_not_called()


@pytest.mark.parametrize('change', ['missing', 'pruned', 'width'])
def test_bad_aux_shapes_rejected(bundle, runtime, checkpoint, change):
    if change == 'missing': del runtime.original['aux.weight']
    elif change == 'pruned': runtime.original['aux.weight'] = Tensor(range(29), (29, 768))
    else: runtime.original['aux.weight'] = Tensor(range(32), (32, 1024))
    with pytest.raises(model_io.LocalModelError, match='output|axes'):
        model_io.load_local_ctc_model(bundle, checkpoint)
    runtime.factory.assert_not_called()


@pytest.mark.parametrize('field,value', [('_model_type', 'WavLM'), ('sample_rate', 8000),
                                      ('_normalize_waveform', True), ('_remove_aux_axis', (1, 2)),
                                      ('_params', {})])
def test_unsupported_bundle_fails_before_file_load(bundle, runtime, checkpoint, field, value):
    setattr(bundle, field, value)
    with pytest.raises(model_io.LocalModelError, match='BASE_960H'):
        model_io.load_local_ctc_model(bundle, checkpoint)
    runtime.torch.load.assert_not_called()


def test_strict_model_shape_failure_is_actionable(bundle, runtime, checkpoint):
    runtime.model.load_state_dict.side_effect = RuntimeError('missing encoder key')
    with pytest.raises(model_io.LocalModelError, match='incompatible'):
        model_io.load_local_ctc_model(bundle, checkpoint)
    runtime.model.to.assert_not_called()
    assert runtime.torch.load.call_count == 1


def test_local_tokenizer_precheck_accepts_existing_regular_file(tmp_path):
    path = tmp_path / 'tokenizer.json'; path.write_bytes(b'{"model":{}}')
    assert model_io.require_local_tokenizer(tmp_path) == path
    assert path.read_bytes() == b'{"model":{}}'


@pytest.mark.parametrize('kind', ['missing', 'empty', 'symlink', 'directory'])
def test_missing_or_nonregular_tokenizer_fails_without_fallback(tmp_path, kind):
    path = tmp_path / 'tokenizer.json'
    if kind == 'empty': path.touch()
    elif kind == 'directory': path.mkdir()
    elif kind == 'symlink':
        real = tmp_path / 'actual.json'; real.write_bytes(b'{}'); path.symlink_to(real)
    with pytest.raises(model_io.LocalModelError, match='tokenizer'):
        model_io.require_local_tokenizer(tmp_path)
