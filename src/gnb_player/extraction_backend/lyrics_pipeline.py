"""Offline song transcription and alignment. Heavy models are loaded one at a time."""
from pathlib import Path
import ctypes
import os
import gc
import math
import re
import subprocess
import unicodedata
from copy import deepcopy
try:
    from .lyrics_recognition import recognition_policy, qwen_language, segment_evidence, recognition_suspect
except ImportError:
    from lyrics_recognition import recognition_policy, qwen_language, segment_evidence, recognition_suspect

MODELS = Path(os.environ.get('GNB_LYRICS_MODEL_ROOT', 'models'))
ENGINES = ('large-v3', 'medium.en', 'qwen3-asr-1.7b', 'qwen3-asr-0.6b', 'small.en')
VERSION = 'singing-4'


def model_path(engine, *, review=False):
    """Route each recognizer to its own chosen cache, including large rechecks."""
    selected = os.environ.get('GNB_LYRICS_TRANSCRIPTION_ENGINE', 'large-v3')
    primary = os.environ.get('GNB_LYRICS_TRANSCRIPTION_MODEL', '')
    secondary = os.environ.get('GNB_LYRICS_REVIEW_MODEL', '')
    if review and engine == 'medium.en' and secondary:
        return Path(secondary)
    if engine == selected and primary:
        return Path(primary)
    if engine == 'medium.en' and secondary:
        return Path(secondary)
    return MODELS / engine


def separator_path():
    chosen = os.environ.get('GNB_LYRICS_SEPARATOR_MODEL', '')
    return Path(chosen) if chosen else MODELS / 'htdemucs'


def load_alignment_model(bundle, checkpoint, device):
    """Re-use an exact local file before calling TorchAudio's cache loader."""
    checkpoint = Path(checkpoint)
    if not checkpoint.is_file():
        raise ValueError('The local lyric alignment model has not been installed.')
    # load_state_dict_from_url reuses model_dir/file_name when that file exists.
    # The explicit existence guard prevents a missing selected file downloading.
    return bundle.get_model(dl_kwargs={'model_dir': str(checkpoint.parent),
                                      'file_name': checkpoint.name}).to(device)


def release_memory():
    gc.collect()
    try:
        ctypes.CDLL('libc.so.6').malloc_trim(0)
    except (OSError, AttributeError):
        pass


def quantize_cpu(model):
    """Convert one linear layer at a time; never create a second full-size model."""
    import torch
    from torch.ao.quantization import default_dynamic_qconfig
    from torch.ao.nn.quantized.dynamic import Linear
    for name, child in list(model.named_children()):
        if isinstance(child, torch.nn.Linear):
            child.float()
            child.qconfig = default_dynamic_qconfig
            setattr(model, name, Linear.from_float(child))
        else:
            quantize_cpu(child)
            # Embeddings, convolution, normalization and positional buffers use
            # FP32; int8 linear operators expect FP32 activations on this CPU.
            for name_, value in child.named_parameters(recurse=False):
                if value.is_floating_point():
                    value.data = value.data.float()
            for name_, value in child.named_buffers(recurse=False):
                if value.is_floating_point():
                    setattr(child, name_, value.float())
    return model


def decode(source, target, rate=16000, channels=1):
    # A frozen GUI cleans its native-library path before launching model Python.
    # Only the bundled FFmpeg child needs the GUI's bundled native libraries.
    environment = os.environ.copy()
    libraries = environment.get('GNB_LYRICS_MEDIA_LIBRARY_PATH', '')
    if libraries and os.name != 'nt':
        inherited = environment.get('LD_LIBRARY_PATH', '')
        environment['LD_LIBRARY_PATH'] = libraries + (os.pathsep + inherited if inherited else '')
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-protocol_whitelist', 'file,pipe', '-i', str(source), '-vn', '-sn',
                    '-t', '900', '-ar', str(rate), '-ac', str(channels), str(target)],
                   check=True, timeout=120, capture_output=True, env=environment)


def audio_chunks(audio, sr, seconds=40, search=4):
    """Cover every sample exactly once; prefer a quiet boundary near each cut."""
    import numpy as np
    start = 0
    while len(audio) - start > (seconds + search) * sr:
        left = start + (seconds - search) * sr
        right = min(len(audio), start + (seconds + search) * sr)
        # 80 ms energy windows, without a quadratic convolution on long files.
        window = max(1, int(.08 * sr))
        candidates = np.abs(audio[int(left):int(right)])
        count = len(candidates) // window
        energy = candidates[:count * window].reshape(count, window).mean(axis=1)
        cut = int(left) + int(energy.argmin()) * window + window // 2
        yield start / sr, audio[start:cut]
        start = cut
    if start < len(audio):
        yield start / sr, audio[start:]


def separate_vocals(source, target, work, progress):
    from lyrics_acceleration import enabled,helper,GpuCapacityDeferred
    if enabled() and os.environ.get('GNB_LYRICS_ROCM_SEPARATION')=='1':
        try:
            helper('separate',source,target,work,progress)
            return 'rocm'
        except GpuCapacityDeferred:
            raise
        except Exception as error:
            print('GPU separation fallback:',type(error).__name__,str(error)[:200],flush=True)
            target.unlink(missing_ok=True)
    import json
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.htdemucs import HTDemucs
    from demucs.apply import apply_model
    from safetensors.torch import load_file
    separator = separator_path()
    spec = json.loads((separator / 'spec.json').read_text())
    model = HTDemucs(*spec['args'], **spec['kwargs'])
    model.load_state_dict(load_file(str(separator / 'model.safetensors')))
    device='cuda' if os.environ.get('GNB_GPU_HELPER_ACTIVE') and torch.cuda.is_available() else 'cpu'
    model.eval().to(device)
    mix = work / 'mix.wav'
    decode(source, mix, 44100, 2)
    length = sf.info(mix).frames
    sr = 44100
    block = sr * 30
    vocal_index = model.sources.index('vocals')
    with sf.SoundFile(mix) as reader, sf.SoundFile(target, 'w', samplerate=sr, channels=1, subtype='FLOAT') as writer:
        for start in range(0, length, block):
            left = max(0, start-sr)
            end = min(length, start+block)
            reader.seek(left)
            samples = reader.read(min(length, end+sr)-left, dtype='float32', always_2d=True)
            wav = torch.from_numpy(samples.T.copy()).to(device)
            ref = wav.mean(0)
            mean, std = ref.mean(), ref.std().clamp_min(1e-8)
            with torch.inference_mode():
                stems = apply_model(model, ((wav-mean)/std)[None], device=device,
                                    shifts=0, split=True, overlap=.25, progress=False, num_workers=0)[0]
                vocal = (stems[vocal_index]*std+mean).mean(0).cpu().numpy()
            writer.write(np.asarray(vocal[start-left:end-left], dtype='float32'))
            progress('separating', end/length, 'Separating vocals from the band.')
            del samples, wav, ref, stems, vocal
    del model
    release_memory()


def transcribe_qwen(audio, sr, engine, progress, title='', evidence=None):
    import numpy as np
    import torch
    from qwen_asr import Qwen3ASRModel
    policy = recognition_policy(engine)
    language = qwen_language(policy)
    model = Qwen3ASRModel.from_pretrained(str(model_path(engine)), dtype=torch.bfloat16,
                                        device_map='cpu', attn_implementation='sdpa',
                                        max_inference_batch_size=1, max_new_tokens=512,
                                        local_files_only=True)
    quantize_cpu(model.model)
    model.dtype = torch.float32
    release_memory()
    chunks = []
    for offset, chunk in audio_chunks(audio, sr):
        if np.max(np.abs(chunk)) < 1e-6:
            text = ''
        else:
            result = model.transcribe(audio=(chunk, sr), language=language, context=policy['prompt'])[0]
            text = result.text.strip()
        chunks.append({'start':offset, 'end':offset+len(chunk)/sr, 'text':text,
                       'language':getattr(result, 'language', language) if text else language})
        progress('transcribing', (offset+len(chunk)/sr)/(len(audio)/sr), 'Recognizing the sung words.')
    del model
    release_memory()
    if evidence is not None:
        evidence.update(backend='qwen-asr', engine=engine, settings=policy, chunks=deepcopy(chunks))
    return chunks


def align_qwen(audio, sr, chunks, progress):
    import torch
    from qwen_asr import Qwen3ForcedAligner
    model = Qwen3ForcedAligner.from_pretrained(str(MODELS/'qwen3-forced-aligner-0.6b'),
                                            dtype=torch.bfloat16, device_map='cpu',
                                            attn_implementation='sdpa', local_files_only=True)
    quantize_cpu(model.model)
    release_memory()
    words = []
    warnings = []
    for i, chunk in enumerate(chunks):
        if chunk['text']:
            clip = audio[round(chunk['start']*sr):round(chunk['end']*sr)]
            result = model.align(audio=(clip, sr), text=chunk['text'], language=chunk.get('language') or 'English')[0]
            for item in result.items:
                start = chunk['start']+float(item.start_time)
                end = chunk['start']+float(item.end_time)
                words.append({'text':item.text, 'start':start, 'end':end})
                if end <= start:
                    warnings.append('Some words have uncertain timing. Check the highlighted lines against the recording.')
        progress('aligning', (i+1)/len(chunks), 'Placing word-by-word sing-along timestamps.')
    del model
    release_memory()
    return words, list(dict.fromkeys(warnings))


def reference_lines(text):
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or re.fullmatch(r'\[[^\]]{1,80}\]', line):
            continue
        lines.append(line)
    if not lines or len(lines)>1000 or len(text)>20000:
        raise ValueError('Supply the lyrics as sung, with one line per phrase (up to 20,000 characters).')
    return lines


def number_words(match):
    from num2words import num2words
    return num2words(match.group(), lang='en').replace('-', ' ').replace(',', '')


def ctc_letters(text):
    text = re.sub(r'\d+(?:\.\d+)?', number_words, text)
    text = unicodedata.normalize('NFKD', text.replace('’', "'")).encode('ascii', 'ignore').decode().upper()
    return re.sub(r"[^A-Z']", '', text)


def align_reference(audio, sr, lines, progress):
    """Global CTC alignment supports long songs and preserves all supplied words."""
    import torch
    import torchaudio
    import torchaudio.functional as F
    bundle = torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H
    # Preload at installation. Runtime has no network access.
    selected = os.environ.get('GNB_LYRICS_ALIGNMENT_MODEL', '')
    checkpoint = Path(selected) if selected else Path(torch.hub.get_dir())/'checkpoints'/bundle._path
    if not checkpoint.is_file():
        raise ValueError('The local lyric alignment model has not been installed.')
    device='cuda' if os.environ.get('GNB_GPU_HELPER_ACTIVE') and torch.cuda.is_available() else 'cpu'
    model = load_alignment_model(bundle, checkpoint, device)
    labels = {label:i for i,label in enumerate(bundle.get_labels())}
    emissions = []
    times = []
    block = 20*sr
    for start in range(0, len(audio), block):
        left = max(0, start-sr)
        end = min(len(audio), start+block)
        right = min(len(audio), end+sr)
        with torch.inference_mode():
            output, _ = model(torch.from_numpy(audio[left:right].copy())[None].to(device))
            output = output[0].log_softmax(-1).cpu()
        centers = (torch.arange(len(output))*320+200+left)/sr
        selected = (centers>=start/sr)&(centers<end/sr)
        emissions.append(output[selected])
        times.extend(centers[selected].tolist())
        progress('aligning', end/len(audio)*.8, 'Matching your complete lyrics to the vocals.')
    del model
    release_memory()
    emission = torch.cat(emissions)[None]
    original = [(line_index, word) for line_index,line in enumerate(lines) for word in line.split()]
    tokens = []
    owners = []
    for word_index, (_, word) in enumerate(original):
        letters = ctc_letters(word)
        if not letters:
            raise ValueError('Use words rather than standalone symbols in the supplied lyrics.')
        if tokens:
            tokens.append(labels['|']);owners.append(None)
        tokens.extend(labels[letter] for letter in letters)
        owners.extend([word_index]*len(letters))
    if len(tokens)>15000:
        raise ValueError('The lyric sheet is too long to align safely.')
    aligned, scores = F.forced_align(emission, torch.tensor([tokens],dtype=torch.int64), blank=0)
    spans = F.merge_tokens(aligned[0], scores[0].exp(), blank=0)
    if len(spans)!=len(tokens):
        raise ValueError('The supplied lyric sheet could not be aligned. Check that it matches this recording.')
    grouped = [[] for _ in original]
    for span, owner in zip(spans, owners):
        if owner is not None:
            grouped[owner].append(span)
    words = []
    for (line_index, text), spans in zip(original, grouped):
        first, last = spans[0], spans[-1]
        words.append({'text':text, 'start':max(0,times[first.start]-.01),
                      'end':min(len(audio)/sr,times[last.end-1]+.01),
                      'score':sum(float(s.score) for s in spans)/len(spans), 'line':line_index})
    progress('aligning', 1, 'Complete lyric sheet aligned. Preparing the review draft.')
    return words


def make_cues(words, duration, preserve_lines=False):
    """No guessed timing for unaligned words; retain text and flag it for review."""
    cues = []
    pending = []
    warnings = []
    for item in words:
        text = str(item.get('text','')).strip()
        start, end = float(item['start']), float(item['end'])
        if not text or not math.isfinite(start) or not math.isfinite(end):
            raise ValueError('The model returned invalid lyric timing. The previous draft was kept.')
        start = max(0,min(duration,start));end = max(start,min(duration,end))
        if pending:
            split = item.get('line')!=pending[-1].get('line') if preserve_lines else (
                start-pending[-1]['end']>3 or (start-pending[-1]['end']>1.4 and len(pending)>=3) or len(pending)>=9 or
                (re.search(r'[.!?]$',pending[-1]['text']) and len(pending)>=3))
            if split:
                cues.append(cue(pending));pending=[]
        pending.append({**item,'text':text,'start':round(start,3),'end':round(end,3)})
        if end<=start or item.get('score',1)<.15:
            warnings.append('Some words need timing review; they were kept rather than silently discarded.')
    if pending:
        cues.append(cue(pending))
    if not cues:
        raise ValueError('No lyric words were found. Try vocal separation or supply the original lyrics.')
    if len(cues)>1000:
        raise ValueError('The result contains too many lyric lines. The previous draft was kept.')
    return cues, list(dict.fromkeys(warnings))


def cue(words):
    return {'start':words[0]['start'], 'end':max(w['end'] for w in words),
            'text':' '.join(w['text'] for w in words),
            'words':[{k:w[k] for k in ('text','start','end','score','uncertain') if k in w} for w in words],
            'needs_review':any(w['end']<=w['start'] or w.get('score',1)<.15 or w.get('uncertain',False) for w in words)}


def alignment_incomplete(words, text):
    normalized = lambda value: re.sub(r'[^\w]+','',value,flags=re.UNICODE).casefold()
    if normalized(' '.join(w['text'] for w in words)) != normalized(text):
        return True
    previous = 0
    for word in words:
        if word['end']<=word['start'] or word['start']<previous-.05:
            return True
        previous = word['end']
    return False


def transcribe_whisper(audio, engine, progress, title='', evidence=None):
    from faster_whisper import WhisperModel
    model = WhisperModel(str(model_path(engine)),device='cpu',compute_type='int8',cpu_threads=int(os.environ.get('GNB_LYRICS_CPU_THREADS','2')),num_workers=1,local_files_only=True)
    policy = recognition_policy(engine)
    segments, info = model.transcribe(audio, language=None if policy['language']=='auto' else policy['language'], task=policy['task'], word_timestamps=True,
                                     condition_on_previous_text=False, beam_size=1,
                                     vad_filter=False, no_speech_threshold=.8,
                                     temperature=(0,.2),
                                     compression_ratio_threshold=2.8, log_prob_threshold=-1.5,
                                     initial_prompt=policy['prompt'] or None)
    all_words = []
    flags = 0
    raw_segments = []
    for segment in segments:
        raw_segments.append(segment_evidence(segment))
        words = [{'text':w.word.strip(),'start':w.start,'end':w.end,'score':getattr(w,'probability',1)} for w in segment.words or [] if w.word.strip()]
        if words:
            suspect = recognition_suspect(segment)
            flags += int(suspect)
            # Confidence flags are for review, never a reason to delete sung lines.
            for word in words:
                word['uncertain'] = suspect
                if all_words and re.fullmatch(r'[^\w]+',word['text'],flags=re.UNICODE):
                    all_words[-1]['text'] += word['text']
                else:
                    all_words.append(word)
        progress('transcribing',min(1,segment.end/info.duration),'Recognizing the sung words and timestamps.')
    del model
    release_memory()
    if evidence is not None:
        evidence.update(backend='faster-whisper', engine=engine, settings=policy,
                        language=getattr(info, 'language', policy['language']),
                        language_probability=getattr(info, 'language_probability', None), segments=raw_segments)
    if not all_words:
        raise ValueError('No lyric words were found. Try vocal separation or supply the original lyrics.')
    cues, timing_warnings = make_cues(all_words,info.duration)
    warnings = [f'{flags} passages have uncertain recognition. Listen before publishing.'] if flags else []
    return cues, warnings+timing_warnings


def extract(source, work, engine, separate, reference, progress, title=''):
    import soundfile as sf
    import torch
    torch.set_num_threads(int(os.environ.get('GNB_LYRICS_CPU_THREADS','2')))
    try:torch.set_num_interop_threads(1)
    except RuntimeError:pass
    warnings = []
    recognition_evidence = {'version':'raw-recognition-1', 'primary':{}}
    gpu_used=False;alignment_compute=None;separation_compute=None
    from lyrics_cache import restore,store
    processing = source;wav = work/'recognition.wav'
    cached=bool(separate and restore(source,wav))
    if cached:
        separation_compute='cached';progress('separating',1,'Reusing the verified private vocal cache.')
    else:
        if separate:
            processing = work/'vocals.wav'
            separation_compute=separate_vocals(source, processing, work, progress) or 'cpu'
        decode(processing, wav)
        if separate:store(source,wav)
    audio, sr = sf.read(wav,dtype='float32')
    duration = len(audio)/sr
    if reference:
        words = align_reference(audio,sr,reference_lines(reference),progress)
        cues, flags = make_cues(words,duration,preserve_lines=True)
        warnings.extend(flags)
        method = 'wav2vec2-ctc-reference'
    elif engine.startswith('qwen3-asr-'):
        chunks = transcribe_qwen(audio,sr,engine,progress,title,evidence=recognition_evidence['primary'])
        complete_text = '\n'.join(chunk['text'] for chunk in chunks if chunk['text'])
        words, flags = align_qwen(audio,sr,chunks,progress)
        if alignment_incomplete(words, complete_text):
            words = align_reference(audio,sr,reference_lines(complete_text),progress)
            method = 'wav2vec2-ctc-fallback'
            warnings.append('Alternative word alignment was used for sustained or difficult vocals. Check the timing before publishing.')
        else:
            method = 'qwen3-forced-aligner-0.6b'
            warnings.extend(flags)
        cues, flags = make_cues(words,duration)
        warnings.extend(flags)
    else:
        from lyrics_acceleration import enabled,recognize_windows,realign,GpuCapacityDeferred
        if engine=='large-v3' and enabled():
            try:
                words=recognize_windows(audio,sr,[('full',0,duration)],work,progress,evidence=recognition_evidence['primary'])['full']
                cues,flags=make_cues(words,duration);gpu_used=True
            except GpuCapacityDeferred:
                raise
            except Exception as error:
                print('GPU recognition fallback:',type(error).__name__,str(error)[:200],flush=True)
        if not gpu_used:cues, flags = transcribe_whisper(audio,engine,progress,title,evidence=recognition_evidence['primary'])
        method='whisper-vulkan-dtw' if gpu_used else 'whisper-word-timestamps'
        recognition_evidence['pre_alignment'] = deepcopy(cues)
        try:
            detected=recognition_evidence['primary'].get('language','en')
            if detected not in {'en', 'English', 'english'}:
                raise ValueError('English-only CTC alignment was skipped; recognizer word timing was retained.')
            words=[w for c in cues for w in c['words']]
            words,alignment_compute=realign(audio,sr,words,work,progress,return_backend=True)
            cues,flags=make_cues(words,duration);method='wav2vec2-ctc-recognized-text'
        except GpuCapacityDeferred:
            raise
        except Exception as error:
            warnings.append('Recognition word timing was retained because audio realignment could not complete.')
            print('Audio alignment fallback:',type(error).__name__,str(error)[-500:],flush=True)
        warnings.extend(flags)
    for before, after in zip(cues,cues[1:]):
        if after['start']-before['end']>=20:
            warnings.append('A long gap remains between lyric lines. Check whether it is an instrumental passage or missing vocals.')
            break
    scores=[w['score'] for c in cues for w in c.get('words',[]) if isinstance(w.get('score'),(int,float))]
    return {'segments':cues,'duration':duration,'warnings':list(dict.fromkeys(warnings)),
            'recognition_evidence':recognition_evidence,
            'pipeline':{'version':VERSION,'recognizer':engine if not reference else 'supplied-lyrics',
                        'aligner':method,'vocals_separated':bool(separate),'compute':'radeon-vulkan' if gpu_used else ('cpu-fp32' if reference else 'cpu-int8'),'alignment_compute':alignment_compute,'separation_compute':separation_compute},
            'metrics':{'words':sum(len(c.get('words',[])) for c in cues),'lines':len(cues),
                       'timed_seconds':round(sum(max(0,c['end']-c['start']) for c in cues),2),
                       'recognition_confidence':round(sum(scores)/len(scores),4) if scores else None,
                       'low_confidence_fraction':round(sum(s<.35 for s in scores)/len(scores),4) if scores else None}}
