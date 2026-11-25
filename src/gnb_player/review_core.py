"""Review state and reversible per-recording saves; no model or Qt imports."""
from __future__ import annotations
from dataclasses import dataclass, asdict, replace
from pathlib import Path
import copy, datetime, json, os, re, shutil, subprocess, uuid
from .lyrics import LyricsDocument, Cue, Word, fingerprint, load_lyrics, load_imported_lyrics, save_lyrics, lyric_target_bytes, lyric_write_lock, _save_lyrics_unlocked
from .settings import atomic_json
from .lyric_review import invalidate_review, retarget_review, document_revision, save_review_document
from .review_repetition import inspect_regions, held_indices
from .review_vocabulary import require_resolved_suggestions
from .review_acceptance import accept_current_request, validate_acceptance
from .review_partial import partial_tokens, preserve_native_support, retire_partial_mapping, recover_stale_text_edit_mapping

REVISION = '918f88917c17489c1f8dbae0165cd1019c4d5cd3'

def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

@dataclass
class ReviewConfig:
    backend: str = 'qwen'
    review_device: str = 'cpu'
    review_precision: str = 'float32'
    review_gpu_python: str = ''
    heart_python: str = '/opt/gnb-reading-voice/bin/python'
    align_python: str = ''
    heart_model: str = ''
    ctc_model: str = '/srv/ai/models/alignment/wav2vec2_fairseq_base_ls960_asr_ls960.pth'
    shared_lock: str = '/run/lock/gnb-ai-gpu.lock'
    timeout_seconds: int = 480
    qwen_asr_model: str = '/srv/ai/models/transcription/qwen3-asr-0.6b'
    qwen_aligner_model: str = '/srv/ai/models/transcription/qwen3-forced-aligner-0.6b'

    @classmethod
    def discover(cls, data_dir):
        home = Path.home()
        c = cls(align_python=str(home/'Recovery-Batch-3/opt/gnb-lyrics-quality/bin/python'),
                heart_model=str(home/'Documents/Codex/2026-10-28/task-2/lyrics-experiment-20261029/models/HeartTranscriptor-oss'/REVISION))
        registration=Path(__file__).with_name('review-gpu-runtime.json')
        if registration.is_file():
            try:c.review_gpu_python=json.loads(registration.read_text()).get('python_path','')
            except (OSError,ValueError,AttributeError):pass
        path = Path(data_dir)/'lyrics-review-runtime.json'
        if path.is_file():
            values = json.loads(path.read_text())
            c = replace(c, **{k: v for k, v in values.items() if k in cls.__dataclass_fields__})
        for name in ('review_device', 'review_gpu_python', 'backend', 'heart_python', 'align_python', 'heart_model', 'ctc_model', 'shared_lock', 'qwen_asr_model', 'qwen_aligner_model'):
            value = os.environ.get('GNB_REVIEW_'+name.upper())
            if value: setattr(c, name, value)
        c.validate_compute()
        return c

    def validate_compute(self):
        if self.review_device not in ('cpu','rocm'):raise ValueError('Choose CPU or AMD ROCm review processing.')
        if self.review_precision!='float32':raise ValueError('This installed review option is validated for FP32 only.')
        if not isinstance(self.review_gpu_python,str) or len(self.review_gpu_python)>4096 or '\x00' in self.review_gpu_python:raise ValueError('Invalid AMD review Python path.')

    def for_job(self):
        self.validate_compute()
        if self.review_device=='rocm':
            if self.backend!='qwen':raise ValueError('AMD GPU review is available for Qwen. Select CPU for Heart / CTC.')
            if not self.review_gpu_python or not Path(self.review_gpu_python).is_file() or not os.access(self.review_gpu_python,os.X_OK):raise ValueError('Choose the tested AMD review Python executable, or select CPU.')
            return replace(self,align_python=self.review_gpu_python)
        return replace(self)

    def missing(self, mode='extract'):
        if self.backend not in ('qwen','heart_ctc'): raise ValueError('Choose Qwen or the explicit Heart / CTC fallback.')
        if self.backend=='qwen':
            models=[self.qwen_asr_model,self.qwen_aligner_model] if mode=='extract' else [self.qwen_aligner_model]
            paths=[self.align_python]+[str(Path(model)/name) for model in models for name in ('model.safetensors','config.json')]
            return [str(p) for p in paths if not p or not Path(p).is_file()]
        paths = [self.align_python, self.ctc_model]
        if mode == 'extract': paths += [self.heart_python, str(Path(self.heart_model)/'model.safetensors'), str(Path(self.heart_model)/'config.json')]
        return [str(p) for p in paths if not p or not Path(p).is_file()]


def embedded_lyrics(source):
    """Probe tags before generation. Presence is provenance, never trust/approval."""
    result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
        '-show_entries', 'format_tags:stream_tags', '-of', 'json', str(source)],
        capture_output=True, timeout=8, check=True)
    if len(result.stdout) > 1024*1024: raise ValueError('Embedded metadata exceeds the review limit.')
    data = json.loads(result.stdout); tags = dict(data.get('format', {}).get('tags', {})); sources=[]
    for location,values in [('format.tags',data.get('format',{}).get('tags',{}))]+[(f'streams[{i}].tags',s.get('tags',{})) for i,s in enumerate(data.get('streams',[]))]:
        for key,text in values.items():
            if 'lyric' in key.lower() or key.upper().startswith(('USLT','SYLT')):
                sources.append({'label':f'{location} · {key}','location':location,'tag':key,'text':str(text)})
    for stream in data.get('streams', []): tags.update(stream.get('tags', {}))
    found = {k: str(v) for k, v in tags.items() if 'lyric' in k.lower() or k.upper().startswith(('USLT', 'SYLT'))}
    if sum(map(len, found.values())) > 100000: raise ValueError('Embedded lyrics exceed the review limit.')
    return {'checked_at': now(), 'kind': 'embedded_unverified' if found else 'no_embedded_lyrics',
            'tags': found, 'sources':sources, 'text': '\n'.join(found.values()), 'trusted': False,
            'note': 'Generated tags may be wrong. Edit/confirm text before alignment; no automatic approval.'}


def sung_text(text):
    """Original text is retained elsewhere; bracketed directions are not sung words."""
    return re.sub(r'\s+', ' ', re.sub(r'\[[^\]]*\]', ' ', text, flags=re.S)).strip()


def mark_unreviewed(document, reason):
    document = invalidate_review(document, reason)
    document.metadata['review'] = {'status': 'unreviewed', 'reason': reason, 'updated_at': now()}
    document.metadata['reviewed'] = False
    document.quality = {**document.quality, 'status': 'review'}
    return document


def edit_phrase(document, index, text):
    """Correct one cue; count changes retain a group, never invented word boundaries."""
    result = copy.deepcopy(document); cue = result.segments[index]; text = text.strip()
    if text == cue.text: return result
    old = copy.deepcopy(cue)
    if not text:
        del result.segments[index]
    else:
        tokens = text.split()
        mapping = partial_tokens(cue)
        metadata = copy.deepcopy(cue.metadata)
        if (mapping is not None and len(tokens) == len(mapping)
                and all(wi is not None or t == old_text for t, (old_text, wi) in zip(tokens, mapping))):
            words = []
            for wi, w in enumerate(cue.words):
                label = ' '.join(t for t, (_, index) in zip(tokens, mapping) if index == wi)
                words.append(replace(w, text=label, metadata={**w.metadata, 'original_text':w.text,
                    'flags':list(dict.fromkeys(w.metadata.get('flags', [])+['text_corrected_review']))}))
            for t, entry in zip(tokens, metadata['partial_timing']['tokens']):entry['text']=t
        elif cue.words and len(tokens) == len(cue.words) and all(len(w.text.split()) == 1 for w in cue.words):
            metadata = retire_partial_mapping(metadata, 'Text edit replaced the partial token layout; original evidence retained.')
            words = [replace(w, text=t, metadata={**w.metadata, 'original_text': w.text,
                     'flags': list(dict.fromkeys(w.metadata.get('flags', [])+['text_corrected_review']))}) for w, t in zip(cue.words, tokens)]
        elif cue.words:
            metadata = retire_partial_mapping(metadata, 'Text edit uses existing grouped support; no individual boundaries invented.')
            intervals = [pair for w in cue.words if w.highlight_allowed for pair in
                         (w.highlight_intervals if w.highlight_intervals is not None else ((w.start, w.end),))]
            words = [Word(text, cue.start, cue.end,
                {'highlight_intervals': intervals, 'highlight_allowed': bool(intervals), 'grouped_text': True, 'individual_word_boundaries': None,
                 'native_constituents': [w.to_dict() for w in cue.words], 'flags': ['grouped_timing_review']})]
        else:
            metadata = retire_partial_mapping(metadata, 'Text edit retained existing line support.')
            words = []
        result.segments[index] = Cue(text, cue.start, cue.end, words, metadata)
    result.metadata.setdefault('edits', []).append({'at': now(), 'action': 'edit_phrase',
        'before': {'text': old.text, 'start': old.start, 'end': old.end, 'words': [w.to_dict() for w in old.words]}, 'after_text': text})
    return mark_unreviewed(result, 'Text edited; listen before approving. Existing anchors retained.')


def set_phrase_bounds(document, index, start, end):
    """Compatibility entry point for explicit phrase outer-bound edits."""
    from .review_timing import edit_bounds
    return edit_bounds(document,index,None,start,end)


def alignment_input(document, supplied_text, *, source='draft', replace_timing=False):
    """Snapshot the chosen source. A stale supplied-text editor cannot override draft edits."""
    if source not in ('draft', 'supplied'): raise ValueError('Choose current draft or supplied text explicitly.')
    require_resolved_suggestions(document)
    if document.segments and not replace_timing:
        raise ValueError('Realignment replaces ALL current timings, including manual edits. Confirm that scope first; ordinary edits preserve timing.')
    if source=='draft' and any(x.get('status')=='held' for x in document.metadata.get('held_passages',[])):
        raise ValueError('Held words are not in the timed draft. Review/exclude them, or supply corrected complete lyrics explicitly.')
    text='\n'.join(c.text for c in document.segments) if source=='draft' else supplied_text
    text=text.strip()
    if not sung_text(text):raise ValueError('Supply sung words before alignment.')
    if len(text)>100000:raise ValueError('Lyric text exceeds 100,000 characters.')
    return {'reference_text':text,'reference_original':text,'alignment_source':source,
            'replacement_scope':'whole_draft','replaces_revision':document_revision(document),
            **({'vocabulary_review':copy.deepcopy(document.metadata['vocabulary_review'])}
               if source=='draft' and document.metadata.get('vocabulary_review') else {})}


def verify_job_revision(document, expected_revision):
    if document_revision(document)!=expected_revision:
        raise ValueError('Draft changed after this job started. Result remains in raw evidence; current edits were not replaced.')


def hold_saved_repetition(document):
    """Explicit, undoable review action on a saved extraction, with native spans retained as evidence."""
    if document.metadata.get('provenance',{}).get('mode')!='extract':
        raise ValueError('This check is for saved extraction candidates. Supplied/edited lyrics are reviewed explicitly.')
    grouped={}
    for cue in document.segments:
        for word in cue.words:
            index=word.metadata.get('region',cue.metadata.get('region',0))
            grouped.setdefault(index,[]).append(word)
    keys=sorted(grouped)
    regions=[{'start':min(w.start for w in grouped[k]),'end':max(w.end for w in grouped[k]),
              'text':' '.join(w.text for w in grouped[k])} for k in keys]
    held=inspect_regions(regions)
    if not held:return copy.deepcopy(document)
    for item in held:
        key=keys[item['region']];item['region']=key
        item['native_words_before_hold']=[w.to_dict() for w in grouped[key]]
    blocked=held_indices(held);result=copy.deepcopy(document);result.segments=[]
    for cue in document.segments:
        words=[copy.deepcopy(w) for w in cue.words if w.metadata.get('region',cue.metadata.get('region',0)) not in blocked]
        if not cue.words:result.segments.append(copy.deepcopy(cue))
        elif words:result.segments.append(Cue(' '.join(w.text for w in words),words[0].start,words[-1].end,words,copy.deepcopy(cue.metadata)))
    result.metadata.setdefault('held_passages',[]).extend(held)
    return mark_unreviewed(result,'Repetitive passages held, not deleted. Review raw evidence and unresolved coverage before approval.')


def exclude_held_passage(document, index):
    result=copy.deepcopy(document);held=result.metadata.get('held_passages',[])
    if not 0<=index<len(held) or held[index].get('status')!='held':raise ValueError('Select an unresolved held passage.')
    held[index].update(status='excluded_by_operator',reviewed_at=now(),decision='Operator listened and rejected this proposed text; this is not automatic instrumental detection.')
    if not result.segments and result.metadata.get('recognition_regions'):
        region=held[index].get('recognition_region',held[index].get('region'))
        if type(region) is int and 0<=region<len(result.metadata['recognition_regions']):
            section=result.metadata['recognition_regions'][region]
            section['text']='';section['text_review_status']='excluded_by_operator'
    return mark_unreviewed(result,'Held text excluded by operator; review the remaining coverage before approval.')


def review_markers(document):
    markers = [(0.0, 'Beginning — verify the first vocal onset')]; previous = 0
    for h in document.metadata.get('held_passages',[]):
        kind = 'untimed word' if h.get('detector') == 'unplaced_native_word' else 'incomplete extraction' if h.get('detector') == 'decoder_termination' else 'repetition warning'
        markers.append((max(0,h['start']-.5),f"{kind}: {h['text']} · {h['status']} · processing window {h['start']:.2f}–{h['end']:.2f}s (not a word timestamp)"))
    for cue in document.segments:
        if cue.metadata.get('native_timing_issues'):
            try: partial = partial_tokens(cue)
            except ValueError: partial = None
            markers.append((max(0,cue.start-1),'Some words have no individual highlight — existing word timings retained' if partial else 'Unresolved native timing — raw evidence retained'))
        if cue.start-previous >= 1: markers.append((max(0,previous-.5), f'Gap {previous:.2f}–{cue.start:.2f}: instruments or missed words?'))
        for w in cue.words:
            flags = w.metadata.get('flags', [])
            if flags: markers.append((max(0,w.start-1), f'{w.start:.2f}s · {w.text} · '+', '.join(flags)))
        previous = max(previous,cue.end)
    return sorted(markers)


def review_provenance_label(document):
    """Describe the current text source without turning provenance into approval."""
    if document.metadata.get('instrumental') is True and not document.segments:
        return 'Operator instrumental designation. Prior lyric evidence remains recoverable; no model-generated words are used.'
    provenance=document.metadata.get('provenance',{})
    mode=provenance.get('mode') if isinstance(provenance,dict) else None
    if mode=='supplied_untimed':return 'Words: supplied lyric sheet. Untimed — no alignment, synchronized highlighting or automatic accuracy claim.'
    if mode=='align':
        source=provenance.get('alignment_source')
        detail={'draft':'current draft','supplied':'supplied text','saved_extraction_sections':'saved audio transcription','draft_passage':'selected corrected passage'}.get(source,'supplied words')
        words=f'Words: {detail} used for alignment; this job did not recognize the words.'
    elif mode=='extract':
        words='Words: model transcription; corrections may have been applied afterward.'
        if document.metadata.get('transcription_only'):words+=' Words checkpointed: Retry alignment continues automatically. Processing sections are not detected vocal regions.'
    else:
        words='Words: imported or unrecorded origin; do not assume an audio transcription.'
    unresolved=sum(len(value) if isinstance(value,list) else int(bool(value))
                   for value in (c.metadata.get('native_timing_issues',[]) for c in document.segments))
    blocked=False
    for cue in document.segments:
        if cue.metadata.get('native_timing_issues'):
            try: blocked=blocked or partial_tokens(cue) is None
            except ValueError: blocked=True
    if blocked:
        return words+f' Timing: {unresolved} unresolved native spans; a supported result is needed before approval.'
    return words+' '+(f'Timing: {unresolved} words lack individual timing. Valid word highlights are retained; the approval summary explains limitations.' if unresolved else 'Timing validity and listening approval are separate from text origin.')


def recover_word_highlights(document):
    """Undoable private recovery using stored native spans, never new inference."""
    before = document.to_dict()
    rows = preserve_native_support(before['segments'])
    if rows == before['segments']:
        return copy.deepcopy(document)
    result = copy.deepcopy(document)
    result.segments = LyricsDocument.from_dict({**before, 'segments': rows}).segments
    result.metadata.setdefault('timing_recovery_history', []).append({
        'at': now(), 'action': 'recover_supported_word_highlights',
        'before_segments': before['segments'],
        'rule': 'Only stored positive native intervals recovered; unresolved words retain no individual time.'})
    return mark_unreviewed(result, 'Individual measured word highlights recovered. Untimed words and raw evidence remain; no model run or guessed timing.')


class ReviewStore:
    def __init__(self, data_dir, source):
        self.source = Path(source).resolve(); self.digest = fingerprint(self.source)
        self.root = Path(data_dir)/'lyrics-review'/self.digest
        self.root.mkdir(parents=True, exist_ok=True)
        self.draft_path = self.root/'draft.json'
        self.draft_base = lyric_target_bytes(self.draft_path)
        self.load_failed = False
        self.output_base = None
        self.output_revision = None
        self.output_target = None

    def bind_output(self, cache_dir):
        self.output_target = Path(cache_dir)/'lyrics'/(self.digest+'.json')
        self.output_base = load_lyrics(self.output_target) if self.output_target.is_file() else None
        self.output_revision = document_revision(self.output_base) if self.output_base else None


    def verify(self):
        if fingerprint(self.source) != self.digest: raise ValueError('Recording changed. Reopen its review; old lyrics were preserved.')

    def history(self, path):
        path = Path(path)
        if path.is_symlink(): raise ValueError('Review saves cannot replace a symbolic link.')
        if path.is_file():
            folder=self.root/'history'; folder.mkdir(exist_ok=True)
            target=folder/(datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S')+'-'+uuid.uuid4().hex+'-'+path.name)
            shutil.copy2(path,target)
            if target.read_bytes()!=path.read_bytes(): raise OSError('Backup verification failed.')
            return target

    def save_draft(self, document):
        if self.load_failed:raise ValueError('The existing saved draft could not be read. It has been preserved; restore or import a valid draft before replacing it.')
        self.verify()
        if document.source_sha256 and document.source_sha256 != self.digest: raise ValueError('Lyrics belong to another recording.')
        document=copy.deepcopy(document); document.source_sha256=self.digest
        serialized=json.loads(json.dumps(document.to_dict(),ensure_ascii=False,allow_nan=False))
        restored=LyricsDocument.from_dict(serialized)
        if restored.to_dict()!=serialized:raise ValueError('Draft cannot be reloaded without changing its content. Previous save preserved.')
        with lyric_write_lock(self.draft_path):
            if lyric_target_bytes(self.draft_path) != self.draft_base:
                raise ValueError('The private draft changed in another review. Restore the latest saved draft before saving.')
            self.history(self.draft_path)
            _save_lyrics_unlocked(document,self.draft_path)
            self.draft_base = lyric_target_bytes(self.draft_path)
        return document

    def load(self):
        self.verify()
        if not self.draft_path.is_file(): return None
        self.draft_base=lyric_target_bytes(self.draft_path)
        self.load_failed=True
        raw=json.loads(self.draft_base.decode("utf-8-sig"))
        try:doc=LyricsDocument.from_dict(raw)
        except ValueError:doc=LyricsDocument.from_dict(recover_stale_text_edit_mapping(raw))
        if doc.source_sha256!=self.digest: raise ValueError('Saved draft source does not match.')
        self.load_failed=False
        return doc

    def import_document(self, path):
        document=load_imported_lyrics(path); self.verify()
        if document.source_sha256 and document.source_sha256!=self.digest: raise ValueError('Import belongs to another recording.')
        document.metadata['import_evidence']={'path':str(path),'source_hash_present':bool(document.source_sha256),'previous_review':document.metadata.get('review'), 'at':now()}
        old_failed=self.load_failed;self.load_failed=False
        try:return self.save_draft(mark_unreviewed(document,'Imported copy; explicit review approval required.'))
        except Exception:
            self.load_failed=old_failed;raise

    def approve(self, document, cache_dir, *, confirmed=False, acceptance=None):
        if not confirmed: raise ValueError('Explicit listening confirmation is required.')
        if self.load_failed:raise ValueError('The existing saved draft could not be read. Original files preserved; restore or import a valid draft first.')
        if acceptance is None:
            acceptance = accept_current_request(document)
        validate_acceptance(document, acceptance)
        self.verify()
        if document.source_sha256!=self.digest: raise ValueError('Candidate source mismatch.')
        output=Path(cache_dir)/'lyrics'/(self.digest+'.json')
        if output != self.output_target:
            raise ValueError('Output target was not captured when review opened. Reopen review.')
        candidate=retarget_review(document,self.output_base)
        approved=save_review_document(candidate, output, self.source, self.output_revision, approve=True, acceptance=acceptance)
        # The editor may change its working copy after approval. Keep the saved
        # concurrency baseline separate so those edits cannot mutate it.
        self.output_base=copy.deepcopy(approved)
        self.output_revision=document_revision(approved)
        return output,approved
