"""Offline Qwen adapters with an explicit optional AMD ROCm device. The controller owns locks, cancellation and budgets.

Run only in the pinned external runtime; importing this module loads no models.
Extraction and supplied-text alignment are deliberately separate operations.
"""
from pathlib import Path
import argparse
import dataclasses
import hashlib
import importlib.metadata
import json
import math
import os
import resource
import socket
import time

try:
    from .review_capture import atomic_json, plain, save_sequences
    from .review_repetition import held_indices
    from .qwen_review_contract import (VERSION, asr_windows, supplied_windows, token_mapping,
        recognition_flags, timestamp_capture, aligned_window, review_segments, json_evidence, finite,
        validate_review_alignment)
except ImportError:
    from review_capture import atomic_json, plain, save_sequences
    from review_repetition import held_indices
    from qwen_review_contract import (VERSION, asr_windows, supplied_windows, token_mapping,
        recognition_flags, timestamp_capture, aligned_window, review_segments, json_evidence, finite,
        validate_review_alignment)


MODELS = {
    'qwen_asr_model': ('Qwen/Qwen3-ASR-0.6B', '79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea'),
    'qwen_aligner_model': ('Qwen/Qwen3-ForcedAligner-0.6B', '47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7'),
}


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _offline():
    os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',)
    def denied(*args, **kwargs):
        raise RuntimeError('Qwen review is offline; downloads and network audio are disabled.')
    socket.create_connection = denied
    socket.socket.connect = denied


def _audio(request):
    import soundfile as sf
    import numpy as np
    path = Path(request['wav'])
    if not path.is_file():
        raise ValueError('Qwen requires a local decoded WAV file.')
    info = sf.info(path)
    if info.samplerate != 16000 or info.channels != 1 or not .5 <= info.frames / 16000 <= 900:
        raise ValueError('Qwen requires mono 16 kHz audio from 0.5 to 900 seconds.')
    audio, sr = sf.read(path, dtype='float32')
    if sr != 16000 or audio.ndim != 1 or not np.isfinite(audio).all():
        raise ValueError('Qwen requires finite mono 16 kHz decoded audio.')
    offset = request.get('original_offset_seconds', 0)
    if not finite(offset) or offset < 0:
        raise ValueError('Invalid original audio offset.')
    return audio, sr, offset


def _runtime(request, key):
    version = importlib.metadata.version('qwen-asr')
    if version != '0.0.6':
        raise ValueError('This reviewed Qwen adapter requires qwen-asr 0.0.6.')
    model_path = Path(request['config'][key])
    if not model_path.is_absolute() or not model_path.is_dir():
        raise ValueError('Select an already installed absolute local Qwen model directory.')
    repository, expected = MODELS[key]
    weights = model_path / 'model.safetensors'
    actual = _sha256(weights)
    if actual != expected:
        raise ValueError('Local Qwen weights differ from the verified pilot; no model loaded.')
    import torch
    import transformers
    torch.manual_seed(202)
    from review_device import select_device
    device_options, device_metadata = select_device(torch, request['config'])
    options = dict(**device_options, attn_implementation='sdpa',
                   local_files_only=True, trust_remote_code=False)
    config_path = model_path / 'config.json'
    metadata = {'model_path': str(model_path), 'model_id': repository,
                'model_sha256': actual, 'weights_sha256': actual, 'weights_verified': True,
                'model_revision': 'sha256:' + actual,
                'qwen_asr_version': version,
                'model_config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                'torch': torch.__version__, 'transformers': transformers.__version__,
                **device_metadata, 'attention': 'sdpa',
                'threads': torch.get_num_threads(), 'interop_threads': torch.get_num_interop_threads(), 'seed': 202,
                'contract': VERSION, 'reviewed': False, 'approved': False}
    return torch, model_path, options, metadata


def capture_timestamp_parser(processor, out, grid_ms):
    """Install a recorder; write raw values BEFORE optional package repair."""
    original = processor.parse_timestamp
    captures = []
    def parse(words, timestamps):
        index = len(captures)
        prefix = Path(out) / f'timestamps-{index:03d}'
        raw = plain(timestamps)
        atomic_json(str(prefix) + '-raw.json', json_evidence({'words': words,
                    'raw_timestamp_ms': raw, 'native_grid_ms': grid_ms}), exclusive=True)
        parsed = original(words, timestamps)
        atomic_json(str(prefix) + '-postprocessed.json', json_evidence(parsed), exclusive=True)
        evidence = timestamp_capture(words, raw, parsed, grid_ms)
        atomic_json(str(prefix) + '-repair-evidence.json', json_evidence(evidence), exclusive=True)
        captures.append(evidence)
        return parsed
    processor.parse_timestamp = parse
    return captures, original


def recognize(request, out):
    _offline()
    out = Path(out)
    audio, sr, offset = _audio(request)
    windows = asr_windows(len(audio), sr)
    torch, model_path, options, metadata = _runtime(request, 'qwen_asr_model')
    from qwen_asr import Qwen3ASRModel
    model = Qwen3ASRModel.from_pretrained(str(model_path), max_inference_batch_size=1,
                                         max_new_tokens=1024, **options)
    model.model.eval()
    model.model.generation_config.do_sample = False
    model.model.generation_config.return_dict_in_generate = True
    generate, infer = model.model.generate, model._infer_asr
    regions = []
    try:
        for index, window in enumerate(windows):
            directory = out / f'qwen-asr-window-{index:03d}'
            generations, raw_outputs = [], []
            def captured_generate(*args, **kwargs):
                call = len(generations)
                inputs = plain(kwargs['input_ids'])
                atomic_json(directory / f'generation-{call:03d}-input.json',
                            {'input_ids': inputs,
                             'decoded_input': model.processor.tokenizer.batch_decode(inputs, skip_special_tokens=False),
                             'context': '', 'language_selection': 'automatic'}, exclusive=True)
                answer = generate(*args, **kwargs)
                details = {'input_token_count': len(inputs[0]), 'max_new_tokens': 1024,
                           'eos_token_id': plain(model.model.generation_config.eos_token_id)}
                sequences = save_sequences(answer, directory / f'generation-{call:03d}-tokens.json', details)
                generations.append({**details, 'sequences': sequences})
                return answer
            def captured_infer(*args, **kwargs):
                answer = infer(*args, **kwargs)
                atomic_json(directory / f'preparser-{len(raw_outputs):03d}.json', answer, exclusive=True)
                raw_outputs.append(answer)
                return answer
            model.model.generate, model._infer_asr = captured_generate, captured_infer
            with torch.inference_mode():
                answer = model.transcribe(audio=(audio[window['start_sample']:window['end_sample']], sr),
                                          context='', language=None, return_time_stamps=False)
            if len(answer) != 1 or len(raw_outputs) != 1 or len(raw_outputs[0]) != 1 or len(generations) != 1:
                raise ValueError('Unexpected internal Qwen batching; raw captures retained, no ambiguous merge.')
            record = generations[0]
            if len(record['sequences']) != 1:
                raise ValueError('Unexpected Qwen generation batch size.')
            raw = raw_outputs[0][0]
            audit = recognition_flags(raw, answer[0].text,
                generated_tokens=record['sequences'][0][record['input_token_count']:],
                max_new_tokens=1024, eos_ids=record['eos_token_id'])
            region = {**window, 'start': window['start'] + offset, 'end': window['end'] + offset,
                      'text': answer[0].text, 'raw_text': raw, 'language': answer[0].language,
                      'original_offset_seconds': offset + window['start'],
                      'coordinates': 'source_audio_seconds', 'raw_capture_directory': str(directory),
                      'evidence': {'kind': 'operator_selected', 'source': 'Contiguous processing window; not detected vocals.'},
                      **audit}
            atomic_json(directory / 'region.json', region, exclusive=True)
            regions.append(region)
    finally:
        model.model.generate, model._infer_asr = generate, infer
    return {**metadata, 'text': '\n'.join(r['text'] for r in regions), 'regions': regions,
            'model_calls': len(regions),
            'duration': len(audio) / sr, 'original_offset_seconds': offset,
            'source_sha256': request.get('source_sha256'), 'decoded_audio_sha256': _sha256(request['wav']),
            'settings': {'context': '', 'language_selection': 'automatic', 'do_sample': False,
                         'max_new_tokens': 1024, 'return_time_stamps': False},
            'flags': sorted({flag for r in regions for flag in r['flags']}),
            'timing_note': 'Processing windows only; no word times or instrumental diagnosis.'}


def align(request, out):
    _offline()
    out = Path(out)
    audio, sr, offset = _audio(request)
    duration = len(audio) / sr
    regions = supplied_windows(request, duration)
    blocked = held_indices(request.get('held_passages', []))
    if any(type(i) is not int or not 0 <= i < len(regions) for i in blocked):
        raise ValueError('Invalid held Qwen alignment region ID.')
    if request.get('alignment_language', 'English') != 'English':
        raise ValueError('This supplied-text mapping is validated for English; other tokenizers need separate review.')
    if len(blocked) < len(regions):
        torch, model_path, options, metadata = _runtime(request, 'qwen_aligner_model')
        from qwen_asr import Qwen3ForcedAligner
        model = Qwen3ForcedAligner.from_pretrained(str(model_path), **options)
        model.model.eval()
    else:
        metadata = {'contract': VERSION, 'reviewed': False, 'approved': False,
                    'model_loaded': False, 'reason': 'Every region is explicitly held.'}
    words, unplaced, issues, audits = [], [], [], []
    for index, region in enumerate(regions):
        if index in blocked:
            continue
        directory = out / f'qwen-align-window-{index:03d}'
        _, expected = token_mapping(region['text'])
        actual, _ = model.aligner_processor.encode_timestamp(region['text'], 'English')
        if actual != expected:
            raise ValueError('Installed Qwen normalizer differs from the reviewed mapping; no alignment attempted.')
        # Sample-rounded crop boundaries, recorded explicitly and used once.
        first, last = math.ceil(region['start'] * sr), math.floor(region['end'] * sr)
        left, right = first / sr, last / sr
        if last <= first:
            raise ValueError('Alignment section is shorter than one decoded audio sample.')
        captures, original = capture_timestamp_parser(model.aligner_processor, directory, model.timestamp_segment_time)
        try:
            with torch.inference_mode():
                answers = model.align(audio=(audio[first:last], sr), text=region['text'], language='English')
        finally:
            model.aligner_processor.parse_timestamp = original
        if len(answers) != 1 or len(captures) != 1:
            raise ValueError('Unexpected Qwen aligner batching; raw evidence retained.')
        returned = [dataclasses.asdict(item) for item in answers[0].items]
        atomic_json(directory / 'native-result.json', json_evidence(returned), exclusive=True)
        result = aligned_window(region['text'], returned, captures[0], region=index,
                                start=left, end=right, source_offset=offset)
        words.extend(result['words'])
        unplaced.extend(result['unplaced'])
        issues.extend(result['native_timing_issues'])
        audits.append({'region': index, 'start_sample': first, 'end_sample': last,
                       'source_offset_seconds': offset + left, 'capture_directory': str(directory),
                       'changed_boundary_count': captures[0]['changed_boundary_count'],
                       'coverage_complete': result['coverage_complete']})
    source_regions = [{**r, 'start': r['start'] + offset, 'end': r['end'] + offset} for r in regions]
    segments, unplaced_groups = review_segments(words, unplaced, source_regions, blocked)
    evidence = {'version': VERSION, 'coordinates': 'source_audio_seconds',
                'normalized_coverage_complete': True, 'coverage_complete': not unplaced,
                'coverage_issues': deepcopy_issues(issues), 'window_count': len(regions),
                'source_offset_applied_once': True, 'automatically_vocal_verified': False,
                'timing_accuracy_verified': False, 'windows': audits}
    result = {**metadata, 'text': request['reference_text'], 'reference_text': request['reference_text'],
            'model_calls': len(audits),
            'reference_sha256': hashlib.sha256(request['reference_text'].encode()).hexdigest(),
            'source_sha256': request.get('source_sha256'), 'decoded_audio_sha256': _sha256(request['wav']),
            'segments': segments, 'duration': duration, 'original_offset_seconds': offset,
            'unplaced': unplaced, 'native_timing_issues': issues,
            'unplaced_groups': unplaced_groups, 'regions': source_regions,
            'alignment_evidence': evidence,
            'timing': 'Qwen native 80-ms classification with package monotonic repair; raw and post-repair evidence retained. Invalid spans remain unresolved; no interpolation by Studio.'}
    result['alignment_evidence'].update(validate_review_alignment(result, source_regions, blocked))
    return result


def deepcopy_issues(issues):
    return json.loads(json.dumps(issues, ensure_ascii=False, allow_nan=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', choices=['qwen-asr', 'qwen-align'], required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    path = Path(args.request)
    request = json.loads(path.read_text())
    started = time.monotonic()
    result = recognize(request, path.parent) if args.stage == 'qwen-asr' else align(request, path.parent)
    result.update(completed=True, elapsed_seconds=time.monotonic() - started,
                  peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    atomic_json(path.parent / (args.stage + '.json'), result, exclusive=True)


if __name__ == '__main__':
    main()
