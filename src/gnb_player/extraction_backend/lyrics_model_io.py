"""Local-only model loading; no URL loader and no unrestricted pickle fallback.

Only the app's English wav2vec2 BASE960H alignment architecture is supported.
Checkpoint fingerprints identify the bytes read; without a separately trusted
expected digest they do not establish publisher authenticity.
"""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import re
import stat


class LocalModelError(ValueError):
    """A selected local model cannot be used without changing its safety policy."""


_BASE_PARAMS = {
    'extractor_mode': 'group_norm',
    'extractor_conv_layer_config': [(512, 10, 5)] + [(512, 3, 2)] * 4 + [(512, 2, 2)] * 2,
    'extractor_conv_bias': False,
    'encoder_embed_dim': 768,
    'encoder_projection_dropout': .1,
    'encoder_pos_conv_kernel': 128,
    'encoder_pos_conv_groups': 16,
    'encoder_num_layers': 12,
    'encoder_num_heads': 12,
    'encoder_attention_dropout': .1,
    'encoder_ff_interm_features': 3072,
    'encoder_ff_interm_dropout': .0,
    'encoder_dropout': .1,
    'encoder_layer_norm_first': False,
    'encoder_layer_drop': .05,
    'aux_num_out': 29,
}
_LABELS = ('-', '|', 'E', 'T', 'A', 'O', 'N', 'I', 'H', 'S', 'R', 'D', 'L',
           'U', 'M', 'W', 'C', 'F', 'G', 'Y', 'P', 'B', 'V', 'K', "'", 'X', 'J', 'Q', 'Z')
_AUX_AXES = (1, 2, 3)
_MAX_CHECKPOINT_BYTES = 2 * 1024 ** 3


def _stamp(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


@contextmanager
def _local_file(path, *, label, maximum):
    """Hold one regular read-only descriptor; reject replacement or in-place edits."""
    path = Path(path).expanduser().absolute()
    descriptor = None
    try:
        initial = path.lstat()
        if not stat.S_ISREG(initial.st_mode):
            raise LocalModelError(f'The local {label} must be a regular file, not a symbolic link or device: {path}')
        if not 0 < initial.st_size <= maximum:
            raise LocalModelError(f'The local {label} is empty or exceeds the supported file size: {path}')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or _stamp(opened) != _stamp(initial):
                raise LocalModelError(f'The local {label} changed while it was opened. Retry after its update finishes.')
            yield path, stream, opened
            if _stamp(os.fstat(stream.fileno())) != _stamp(opened) or _stamp(path.lstat()) != _stamp(opened):
                raise LocalModelError(f'The local {label} changed while it was being read. No model was accepted.')
    except LocalModelError:
        raise
    except OSError as error:
        raise LocalModelError(f'The local {label} is missing, unreadable, or changed. Select its existing regular file; nothing was downloaded.') from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def require_local_tokenizer(model_path):
    """Fail before WhisperModel's remote tokenizer fallback for absent assets.

    This preflight does not replace process-level network isolation: another
    writer deleting an asset after it returns is still an external race.
    """
    path = Path(model_path).expanduser() / 'tokenizer.json'
    with _local_file(path, label='Whisper tokenizer.json', maximum=8 * 1024 ** 2) as (path, _, _):
        return path


def _bundle_parameters(bundle):
    """Do not permit checkpoint metadata to select executable/model factories."""
    try:
        params = bundle._params
        compatible = (bundle._model_type == 'Wav2Vec2'
                      and bundle._normalize_waveform is False
                      and bundle.sample_rate == 16000
                      and tuple(bundle._remove_aux_axis) == _AUX_AXES
                      and tuple(bundle.get_labels()) == _LABELS
                      and isinstance(params, dict) and params == _BASE_PARAMS)
    except (AttributeError, TypeError):
        compatible = False
    if not compatible:
        raise LocalModelError('The selected TorchAudio bundle is not the supported English WAV2VEC2_ASR_BASE_960H alignment architecture.')
    return deepcopy(_BASE_PARAMS)


def _tensor_state(state, torch):
    if (not isinstance(state, dict) or not 0 < len(state) <= 4096
            or any(not isinstance(key, str) or not key or len(key) > 256
                   or not isinstance(value, torch.Tensor) for key, value in state.items())):
        raise LocalModelError('The local CTC checkpoint must contain only a nonempty tensor state dictionary; training objects and nested checkpoints are unsupported.')
    state = dict(state)
    try:
        weight, bias = state['aux.weight'], state['aux.bias']
        if tuple(weight.shape) != (32, 768) or tuple(bias.shape) != (32,):
            raise LocalModelError('The CTC checkpoint has incompatible output axes. Use a BASE960H checkpoint with the original 32-class fairseq output; already-pruned or other architectures are unsupported.')
        # Exactly TorchAudio's Wav2Vec2ASRBundle conversion: retain rows0,4..31.
        for key in ('aux.weight', 'aux.bias'):
            mat = state[key]
            state[key] = torch.stack([mat[index] for index in range(mat.size(0)) if index not in _AUX_AXES])
    except LocalModelError:
        raise
    except (KeyError, AttributeError, TypeError, RuntimeError, IndexError) as error:
        raise LocalModelError('The local CTC checkpoint lacks compatible tensor output weights. Select the matching BASE960H alignment checkpoint.') from error
    return state


def load_local_ctc_model(bundle, checkpoint, device='cpu', *, expected_sha256=None):
    """Return ``(eval_model, content_identity)`` using only one opened local file.

    ``expected_sha256`` is optional caller-supplied trusted provenance, not a
    hardcoded filename assumption. Compatible intentional custom checkpoints
    are accepted without it and explicitly reported as fingerprinted only.
    No exception triggers a URL load or an unrestricted deserialization retry.
    """
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or re.fullmatch(r'[a-fA-F0-9]{64}', expected_sha256) is None):
        raise LocalModelError('A trusted expected checkpoint SHA256 must contain exactly 64 hexadecimal characters.')
    params = _bundle_parameters(bundle)
    with _local_file(checkpoint, label='CTC checkpoint', maximum=_MAX_CHECKPOINT_BYTES) as (path, stream, opened):
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if expected_sha256 is not None and digest != expected_sha256.lower():
            raise LocalModelError('The local CTC checkpoint does not match its explicitly configured trusted SHA256. Keep the file for diagnosis and select the intended checkpoint; nothing was downloaded or replaced.')
        stream.seek(0)
        import torch
        try:
            state = torch.load(stream, map_location='cpu', weights_only=True)
        except Exception as error:
            raise LocalModelError('The local CTC checkpoint could not be loaded with the restricted tensor loader. Select a compatible tensor-only checkpoint; unrestricted pickle and downloads were not attempted.') from error
    state = _tensor_state(state, torch)
    try:
        from torchaudio.models import wav2vec2_model
        model = wav2vec2_model(**params)
        model.load_state_dict(state, strict=True)
        model.eval()
        model = model.to(device)
    except Exception as error:
        raise LocalModelError('The local CTC tensors are incompatible with the supported BASE960H alignment runtime or selected device. Check the matching checkpoint and Torch/TorchAudio runtime; nothing was downloaded.') from error
    identity = {
        'schema': 'local-ctc-checkpoint-1', 'path': str(path),
        'bytes': opened.st_size, 'sha256': digest,
        'weights_only': True, 'local_file_only': True,
        'expected_sha256': expected_sha256.lower() if expected_sha256 is not None else None,
        'expected_digest_matched': True if expected_sha256 is not None else None,
        'provenance': 'matched-caller-trusted-digest' if expected_sha256 is not None else 'content-fingerprint-only; publisher identity not verified',
        'architecture': 'WAV2VEC2_ASR_BASE_960H', 'removed_aux_axes': list(_AUX_AXES),
    }
    return model, identity
