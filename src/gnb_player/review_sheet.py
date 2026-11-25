"""Recording-bound untimed lyric sheets. No models, inferred timing or publication."""
from copy import deepcopy
import hashlib
import json
import math
import re
import base64
from pathlib import Path
from .lyrics import lyric_target_bytes, lyric_write_lock
from .settings import atomic_json
from .lyric_review import document_revision

SCHEMA = 'supplied-lyric-sheet-1'
LIMIT = 100000
BRACKET = re.compile(r'\[([^\[\]]*)\]')
STAGE = re.compile(r'^(intro|outro|ending|verse(?:\s+\d+)?|chorus|pre[- ]chorus|post[- ]chorus|bridge|hook|tag|refrain|instrumental(?: break)?|(?:guitar|piano|sax(?:ophone)?|drum|bass)\s+solo|solo|spoken intro)(?:\s*[:;,—–-].*|\s+\d+)?$', re.I | re.S)
EDITORIAL = re.compile(r'^(?:style|genre|mood|tempo|key|vocals?|vocal style|vocal delivery|delivery|instruments?|instrumentation|editorial(?: note)?|note|instruction|production|arrangement|performance)\s*[:;—–-].+$', re.I | re.S)
DELIVERY = re.compile(r'^(?:spoken(?: softly)?|whisper(?:ed|ing)?|softly|shouted|raspy vocals?|soft vocals?|spoken word|falsetto|harmony|backing vocals?|fade (?:in|out)|pause|repeat(?: (?:twice|\d+ times))?|slow blues|stop[- ]time|\d+(?:\.\d+)? bpm)$', re.I)


def bracket_preview(original, choices=None):
    """Only recognizable standalone directions default to excluded; show all choices."""
    if not isinstance(original, str) or len(original) > LIMIT:
        raise ValueError('The lyric sheet must contain at most 100,000 characters.')
    choices = choices or {}
    pieces, rows, previous = [], [], 0
    for index, match in enumerate(BRACKET.finditer(original)):
        line_start = original.rfind('\n', 0, match.start()) + 1
        line_end = original.find('\n', match.end())
        if line_end < 0: line_end = len(original)
        standalone = not original[line_start:match.start()].strip() and not original[match.end():line_end].strip()
        content=match.group(1).strip()
        direction = bool(STAGE.fullmatch(content) or EDITORIAL.fullmatch(content) or DELIVERY.fullmatch(content))
        action = choices.get(index, 'exclude' if direction else 'keep')
        if action not in ('keep', 'exclude'): raise ValueError('Choose keep sung words or exclude direction.')
        replacement=match.group(1) if action=='keep' else '\n'*match.group(1).count('\n')
        if not replacement and 0<match.start() and match.end()<len(original) and original[match.start()-1].isalnum() and original[match.end()].isalnum():replacement=' '
        pieces.extend((original[previous:match.start()], replacement))
        rows.append({'start': match.start(), 'end': match.end(), 'text': match.group(0), 'action': action,
                     'suggestion': 'recognized direction / editorial note' if direction else 'possibly sung; kept by default'})
        previous = match.end()
    pieces.append(original[previous:])
    # Keep every line break, including empty direction lines. No flattening.
    return re.sub(r'[^\S\r\n]{2,}', ' ', ''.join(pieces)), rows


def make_sheet(original, sung, rows, *, source_sha256, duration, title, manual_preview=False, import_source=None):
    sheet = {'schema': SCHEMA, 'source_sha256': source_sha256, 'duration_at_save': duration,
             'title': title, 'original': original, 'sung_text': sung, 'brackets': deepcopy(rows),
             'preview_manually_edited': bool(manual_preview), 'origin': 'owner_supplied_untimed_sheet',
             'automatic_accuracy_claim': False}
    if import_source is not None:sheet['import_source']=deepcopy(import_source)
    validate_sheet(sheet, source_sha256)
    return sheet


def validate_sheet(sheet, digest):
    if not isinstance(sheet, dict) or sheet.get('schema') != SCHEMA:
        raise ValueError('Unsupported supplied lyric sheet.')
    if sheet.get('source_sha256') != digest:
        raise ValueError('This lyric sheet belongs to a different recording; original retained.')
    for key in ('original', 'sung_text'):
        if not isinstance(sheet.get(key), str) or len(sheet[key]) > LIMIT:
            raise ValueError('Invalid or oversized supplied lyric text.')
    duration = sheet.get('duration_at_save')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        raise ValueError('Recording duration is invalid. Untimed sheets can be saved when duration is unknown (zero).')
    if not isinstance(sheet.get('brackets'), list): raise ValueError('Missing bracket decisions.')
    json.dumps(sheet, allow_nan=False)
    return sheet


def embedded_sources(embedded):
    sources=embedded.get('sources')
    if isinstance(sources,list):return deepcopy(sources)
    tags=embedded.get('tags',{})
    if tags:return [{'label':key,'text':str(text),'location':'embedded tag','tag':key} for key,text in tags.items()]
    return [{'label':'Embedded lyrics','text':embedded['text'],'location':'embedded text'}] if embedded.get('text') else []


def import_text_file(path):
    path=Path(path)
    with path.open('rb') as f:raw=f.read(512*1024+1)
    if len(raw)>512*1024:raise ValueError('Lyric text file is larger than 512 KiB; choose a single-song sheet.')
    encoding='utf-16' if raw.startswith((b'\xff\xfe',b'\xfe\xff')) else 'utf-8-sig'
    try:text=raw.decode(encoding)
    except UnicodeError as error:raise ValueError('Save this lyric sheet as UTF-8 or UTF-16 text before importing.') from error
    if '\0' in text or len(text)>LIMIT:raise ValueError('Not a supported single-song plain-text sheet.')
    return text,{'kind':'text_file','path':str(path.resolve()),'name':path.name,'encoding':encoding,
                 'source_sha256':hashlib.sha256(raw).hexdigest(),'original_bytes_base64':base64.b64encode(raw).decode('ascii'),
                 'original_text':text,'trusted':False}


def untimed_document(sheet, current):
    """Text only: zero cues/word spans. Old document belongs in store history."""
    from .lyrics import LyricsDocument
    validate_sheet(sheet,current.source_sha256)
    if not sheet['sung_text'].strip():raise ValueError('Add sung words before using an untimed sheet.')
    return LyricsDocument([],current.duration,current.title,current.source_sha256,
        quality={'status':'review'},metadata={'timing_mode':'untimed','untimed_lyrics':sheet['sung_text'],
            'supplied_sheet':deepcopy(sheet),'review':{'status':'unreviewed'},'reviewed':False,
            'provenance':{'mode':'supplied_untimed','reference_original':sheet['original'],
                          'import_source':deepcopy(sheet.get('import_source')),
                          'timing':'No timestamps, alignment or recognition. Text sheet only.'}})


class SheetStore:
    """Separate sheet save never changes current timed draft or approved output."""
    def __init__(self, review_store):
        self.review = review_store
        self.path = review_store.root / 'supplied-sheet.json'
        self.base = lyric_target_bytes(self.path)

    def load(self):
        self.review.verify()
        if self.base is None: return None
        return validate_sheet(json.loads(self.base.decode('utf-8')), self.review.digest)

    def save(self, sheet):
        self.review.verify()
        validate_sheet(sheet, self.review.digest)
        value = json.loads(json.dumps(sheet, ensure_ascii=False, allow_nan=False))
        with lyric_write_lock(self.path):
            if lyric_target_bytes(self.path) != self.base:
                raise ValueError('This sheet changed in another window. Reopen Paste lyrics before saving.')
            self.review.history(self.path)
            atomic_json(self.path, value)
            self.base = lyric_target_bytes(self.path)
            if json.loads(self.base.decode('utf-8')) != value:
                raise OSError('Saved sheet verification failed; do not assume it was saved.')
        return deepcopy(value)


def alignment_plan(sheet, document, preferred_backend):
    validate_sheet(sheet, document.source_sha256)
    duration = document.duration
    if not math.isfinite(duration) or not 1 <= duration <= 900:
        raise ValueError('Align one recording of at most 15 minutes.')
    if abs(sheet['duration_at_save'] - duration) > 1:
        raise ValueError('Recording duration changed; reopen the sheet before aligning.')
    if not sheet['sung_text'].strip(): raise ValueError('The sung-text preview is empty. Add the words to align.')
    if preferred_backend not in ('qwen', 'heart_ctc'): raise ValueError('Unknown alignment engine.')
    # Splitting text proportionally across time would manufacture audio anchors.
    # The installed whole-recording acoustic CTC path is explicit for long sheets.
    backend = 'heart_ctc' if duration > 180 else preferred_backend
    reason = ('Whole-song CTC alignment; no transcription. Qwen needs measured sections for recordings over 180 seconds.'
              if duration > 180 else ('Qwen supplied-word alignment; no transcription.' if backend == 'qwen' else 'CTC supplied-word alignment; no transcription.'))
    request = {'reference_text': sheet['sung_text'], 'reference_original': sheet['original'],
               'alignment_source': 'supplied_sheet', 'supplied_sheet': deepcopy(sheet),
               'supplied_sheet_sha256': hashlib.sha256(json.dumps(sheet, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
               'reference_prepared': True, 'replacement_scope': 'whole_draft',
               'replaces_revision': document_revision(document), 'repeat_override': True,
               'alignment_route': reason}
    return backend, reason, request
