"""Timed lyric interchange. Imports retain measured words; plain subtitles stay line-timed."""
from __future__ import annotations

from bisect import bisect_right
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import file_digest, sha256
from pathlib import Path
from typing import Any
import json
import math
import os
import re
import tempfile
try:
    from .review_partial import partial_tokens
except ImportError:  # extract_worker imports this module from its standalone directory
    from review_partial import partial_tokens

MAX_LYRIC_BYTES = 8 * 1024 * 1024
MAX_IMPORT_PROVENANCE_BYTES = 256 * 1024
IMPORT_PROVENANCE_SCHEMA = 'broadcast-import-provenance-1'


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float
    metadata: dict[str, Any] = field(default_factory=dict)


    @property
    def highlight_intervals(self):
        value = self.metadata.get('highlight_intervals')
        return tuple(_highlight_pair(pair) for pair in value) if value is not None else None

    @property
    def highlight_allowed(self):
        return self.metadata.get('highlight_allowed', True) is not False

    def to_dict(self):
        return {**deepcopy(self.metadata), 'text': self.text, 'start': self.start, 'end': self.end}


def _highlight_pair(pair):
    return (pair["start"], pair["end"]) if isinstance(pair, dict) else tuple(pair)


def word_is_active(word, seconds):
    """One interval contract shared by private review and Program Output."""
    data = word if isinstance(word, dict) else ({**getattr(word, 'metadata', {}), 'start': word.start, 'end': word.end} if word is not None else None)
    if not data or data.get('highlight_allowed', True) is False:
        return False
    if not data['start'] <= seconds < data['end']:
        return False
    intervals = data.get('highlight_intervals')
    return intervals is None or any(a <= seconds < b for a, b in map(_highlight_pair, intervals))


def cue_is_active(cue, seconds, document=None):
    """Keep a line visible between words; underline support is a separate clock.

    Explicit holes *inside* a word/group and disabled words retain their existing
    hiding meaning. Owner-confirmed instrumental exclusions also take precedence.
    """
    try: partial_tokens(cue)
    except (ValueError, TypeError, AttributeError): return False
    start, end, words = (cue['start'], cue['end'], cue.get('words', [])) if isinstance(cue, dict) else (cue.start, cue.end, cue.words)
    if not start <= seconds < end:
        return False
    metadata=document if isinstance(document,dict) else getattr(document,'metadata',{})
    original=metadata.get('reference_provenance',{}).get('original_review_data',{})
    for gap in metadata.get('confirmed_instrumental_gaps',original.get('confirmed_instrumental_gaps',[])):
        a,b=_highlight_pair(gap)
        if a<=seconds<b:return False
    explicit = any(('highlight_intervals' in w or 'highlight_allowed' in w) if isinstance(w, dict)
                   else ('highlight_intervals' in getattr(w, 'metadata', {}) or 'highlight_allowed' in getattr(w, 'metadata', {})) for w in words)
    if not explicit or any(word_is_active(word, seconds) for word in words):return True
    data=[w if isinstance(w,dict) else w.to_dict() for w in words]
    if any(w['start']<=seconds<w['end'] for w in data):return False
    return any(a['end']<=seconds<b['start'] and a.get('highlight_allowed',True) is not False
               and b.get('highlight_allowed',True) is not False
               for a,b in zip(data,data[1:]))


def validate_highlight_intervals(word):
    """Reject malformed explicit intervals; never silently enable a hidden span."""
    if 'highlight_allowed' in word and type(word['highlight_allowed']) is not bool:
        raise ValueError('Highlight permission must be true or false.')
    if 'highlight_intervals' not in word:
        return
    intervals = word['highlight_intervals']
    if not isinstance(intervals, (list, tuple)):
        raise ValueError('Highlight intervals must be an explicit list.')
    previous = float(word['start'])
    for pair in intervals:
        if not isinstance(pair, (dict, list, tuple)) or len(pair) != 2:
            raise ValueError('Invalid highlight interval.')
        a, b = _interval(*_highlight_pair(pair))
        if a < previous - 1e-6 or b > float(word['end']) + 1e-6:
            raise ValueError('Highlight intervals must be ordered inside their word/group.')
        previous = b


@dataclass
class Cue:
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LyricsDocument:
    segments: list[Cue]
    duration: float
    title: str = ""
    source_sha256: str = ""
    warnings: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    human_review: dict[str, Any] = field(default_factory=dict)

    @property
    def untimed_text(self):
        return self.metadata.get('untimed_lyrics','') if self.metadata.get('timing_mode')=='untimed' and not self.segments else ''

    def active_cue(self, seconds: float) -> Cue | None:
        if not math.isfinite(seconds) or seconds < 0 or not self.segments:
            return None
        index = bisect_right([cue.start for cue in self.segments], seconds) - 1
        if index < 0:
            return None
        cue = self.segments[index]
        return cue if cue_is_active(cue, seconds, self) else None

    def active_word(self, seconds: float) -> Word | None:
        cue = self.active_cue(seconds)
        if cue is None:
            return None
        return next((word for word in cue.words if word_is_active(word, seconds)), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            **deepcopy(self.metadata),
            "version": self.metadata.get("version", "broadcast-player-lyrics-1"),
            "title": self.title,
            "duration": float(self.duration),
            "source_sha256": self.source_sha256,
            "warnings": deepcopy(self.warnings),
            "quality": deepcopy(self.quality),
            "human_review": deepcopy(self.human_review),
            "segments": [{**deepcopy(c.metadata), "start": float(c.start), "end": float(c.end), "text": c.text,
                          "words": [{**deepcopy(w.metadata), "text": w.text, "start": float(w.start), "end": float(w.end)}
                                    for w in c.words]} for c in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], duration: float = 0) -> "LyricsDocument":
        if not isinstance(data, dict):
            raise ValueError("The lyric file must contain a JSON object.")
        raw = data.get("segments", [])
        if not isinstance(raw, list) or len(raw) > 10000:
            raise ValueError("The lyric file has too many lines or an invalid line list.")
        if data.get('timing_mode')=='untimed':
            text=data.get('untimed_lyrics')
            if raw or not isinstance(text,str) or not text.strip() or len(text)>100000:
                raise ValueError('Untimed lyric sheets need text and must not contain timestamped cues.')
            sheet=data.get('supplied_sheet',{})
            if not isinstance(sheet,dict) or sheet.get('source_sha256')!=data.get('source_sha256') or sheet.get('sung_text')!=text:
                raise ValueError('Untimed text must match its recording-bound saved sheet.')
        warnings = [str(value) for value in data.get("warnings", [])] if isinstance(data.get("warnings"), list) else []
        cues: list[Cue] = []
        invalid_words = False
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("A lyric line is invalid.")
            # Partial labels cannot conceal invalid words that the legacy
            # importer would otherwise remove/sort into native-issue evidence.
            partial_tokens(item)
            start, end = _interval(item.get("start"), item.get("end"))
            text = str(item.get("text", ""))[:20000]
            words: list[Word] = []
            native_issues = []
            for word_index, word in enumerate(item.get("words", []) if isinstance(item.get("words"), list) else []):
                if isinstance(word, dict) and ("highlight_intervals" in word or "highlight_allowed" in word):
                    validate_highlight_intervals(word)
                try:
                    a, b = _interval(word.get("start"), word.get("end"))
                    if a < start - .1 or b > end + .1:
                        raise ValueError("Word outside its line")
                    extras = {key: deepcopy(value) for key, value in word.items() if key not in {"text", "start", "end"}}
                    words.append(Word(str(word.get("text", ""))[:1000], a, b, extras))
                except (AttributeError, ValueError, TypeError) as error:
                    invalid_words = True
                    # Failed native spans are evidence, not usable word timing.
                    # Never silently convert an all-invalid list to line timing.
                    native_issues.append({'word_index': word_index,
                        'raw_word': _native_evidence(word), 'reason': str(error),
                        'status': 'unresolved', 'replacement_interval': None})
            if text.strip():
                extras = {key: deepcopy(value) for key, value in item.items() if key not in {"text", "start", "end", "words"}}
                if native_issues:
                    previous = extras.get('native_timing_issues', [])
                    if not isinstance(previous, list):
                        raise ValueError('Invalid native timing issue evidence.')
                    extras['native_timing_issues'] = previous + native_issues
                cue = Cue(text, start, end, sorted(words, key=lambda value: value.start), extras)
                partial_tokens(cue)
                cues.append(cue)
        cues.sort(key=lambda cue: cue.start)
        if invalid_words:
            warnings.append("Some imported words had invalid timing; original spans are retained as unresolved evidence. No guessed timing or automatic line-timing approval.")
        supplied_duration = _finite(data.get("duration", duration or (cues[-1].end if cues else 0)))
        if supplied_duration < 0:
            raise ValueError("Lyric duration must be positive.")
        fingerprint = str(data.get("source_sha256", ""))
        if fingerprint and not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
            raise ValueError("The lyric file contains an invalid source fingerprint.")
        quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
        extras = {key: deepcopy(value) for key, value in data.items()
                  if key not in {"title", "duration", "source_sha256", "warnings", "quality", "segments", "human_review"}
                  and not (key == "version" and value == "broadcast-player-lyrics-1")}
        review = deepcopy(data.get("human_review")) if isinstance(data.get("human_review"), dict) else {}
        result = cls(cues, supplied_duration, str(data.get("title", ""))[:1000], fingerprint.lower(), warnings, deepcopy(quality), extras, review)
        result._review_base_state = _review_state_digest(result)
        return result


def _native_evidence(value):
    """Keep malformed numeric evidence serializable without converting it to time."""
    if isinstance(value, float) and not math.isfinite(value):
        return {'invalid_number_repr': repr(value)}
    if isinstance(value, dict):
        return {key: _native_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_native_evidence(item) for item in value]
    return deepcopy(value)


def _review_state_digest(document: LyricsDocument) -> str:
    """Ephemeral concurrency token including approval/revocation audit fields."""
    payload = json.dumps(document.to_dict(), sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return sha256(payload.encode("utf-8")).hexdigest()


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("Invalid lyric timestamp.")
    try:
        number = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError("Invalid lyric timestamp.") from error
    if not math.isfinite(number):
        raise ValueError("Invalid lyric timestamp.")
    return number


def _interval(start: Any, end: Any) -> tuple[float, float]:
    a, b = _finite(start), _finite(end)
    if a < 0 or b <= a or b > 86400:
        raise ValueError("Lyric timestamps must be ordered, positive intervals within one day.")
    return a, b


def _clock(text: str) -> float:
    fields = text.strip().replace(",", ".").split(":")
    if len(fields) == 2:
        return int(fields[0]) * 60 + float(fields[1])
    if len(fields) == 3:
        return int(fields[0]) * 3600 + int(fields[1]) * 60 + float(fields[2])
    raise ValueError("Unsupported subtitle timestamp.")


def parse_lrc(text: str, duration: float = 0, title: str = "") -> LyricsDocument:
    offset = re.search(r"\[offset:([+-]?\d+)\]", text, re.I)
    adjustment = int(offset.group(1)) / 1000 if offset else 0
    title_match = re.search(r"\[ti:([^\]]*)\]", text, re.I)
    rows: list[tuple[float, str]] = []
    for line in text.splitlines():
        stamps = re.findall(r"\[(\d+:\d+(?:\.\d+)?)\]", line)
        if not stamps:
            continue
        lyric = re.sub(r"\[[^\]]*\]", "", line).strip()
        if not lyric:
            continue
        rows.extend((max(0, _clock(stamp) + adjustment), lyric) for stamp in stamps)
    rows.sort(key=lambda row: row[0])
    cues = []
    for index, (start, lyric) in enumerate(rows):
        end = rows[index + 1][0] if index + 1 < len(rows) else (duration if duration > start else start + 8)
        if end > start:
            cues.append(Cue(lyric, start, end))
    return LyricsDocument(cues, duration or (cues[-1].end if cues else 0), title or (title_match.group(1) if title_match else ""))


def parse_subtitles(text: str, duration: float = 0, title: str = "") -> LyricsDocument:
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        timing = lines[timing_index].split("-->", 1)
        start = _clock(timing[0].strip())
        end = _clock(timing[1].strip().split()[0])
        _interval(start, end)
        lyric = re.sub(r"<[^>]*>", "", "\n".join(lines[timing_index + 1:])).strip()
        if lyric:
            cues.append(Cue(lyric, start, end))
    cues.sort(key=lambda cue: cue.start)
    return LyricsDocument(cues, duration or (cues[-1].end if cues else 0), title)


def _read_lyric_bytes(file: Path) -> bytes:
    if not file.is_file():
        raise ValueError("Choose a lyric JSON, LRC, SRT or VTT file smaller than 8 MB.")
    with file.open('rb') as handle:
        payload = handle.read(MAX_LYRIC_BYTES + 1)
    if len(payload) > MAX_LYRIC_BYTES:
        raise ValueError("Choose a lyric JSON, LRC, SRT or VTT file smaller than 8 MB.")
    return payload


def _parse_lyric_bytes(file: Path, payload: bytes, duration: float) -> LyricsDocument:
    text = payload.decode('utf-8-sig')
    suffix = file.suffix.lower()
    if suffix == ".lrc":
        return parse_lrc(text, duration, file.stem)
    if suffix in {".srt", ".vtt"}:
        return parse_subtitles(text, duration, file.stem)
    return LyricsDocument.from_dict(json.loads(text), duration)


def load_lyrics(path: str | Path, duration: float = 0) -> LyricsDocument:
    file = Path(path)
    return _parse_lyric_bytes(file, _read_lyric_bytes(file), duration)


def lyric_content_revision(path: str | Path) -> str:
    """Hash the bounded saved lyric bytes, including readable subtitle links."""
    return sha256(_read_lyric_bytes(Path(path))).hexdigest()


def _json_bytes(data) -> bytes:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def preserve_import_provenance(document: LyricsDocument, original: dict | None = None,
                               *, payload: bytes | None = None, source_format='json',
                               source_name='', kind='legacy-provenance-before-local-review') -> None:
    """Preserve inert origin claims once; never convert imported claims to approval.

    Metadata values stay in the normal document unchanged except for effective
    publication flags handled by the review layer. The bounded snapshot keeps
    important origin fields first; oversized/excess fields have explicit hashes
    and remain available in the ordinary preserved metadata. No nested snapshots
    are created on a subsequent import/export cycle.
    """
    existing = document.metadata.get('import_provenance')
    if (isinstance(existing, dict) and existing.get('schema') == IMPORT_PROVENANCE_SCHEMA
            and existing.get('claims_are_unverified') is True
            and re.fullmatch(r'[a-f0-9]{64}', str(existing.get('payload_sha256', '')))
            and len(_json_bytes(existing)) <= MAX_IMPORT_PROVENANCE_BYTES):
        return
    original = deepcopy(original if original is not None else document.to_dict())
    metadata = {key: value for key, value in original.items() if key != 'segments'}
    preferred = ['song_id', 'recording_id', 'origin_id', 'source_sha256', 'source_size',
                 'source_mtime', 'source', 'origin', 'provenance', 'publication',
                 'reviewed', 'human_review', 'machine_review', 'quality', 'title', 'duration', 'version']
    keys = [key for key in preferred if key in metadata] + [key for key in metadata if key not in preferred]
    fields, omitted, used = {}, [], 0
    omitted_count = 0
    for index, key in enumerate(keys):
        encoded = _json_bytes({key: metadata[key]})
        if index < 256 and len(encoded) <= 64 * 1024 and used + len(encoded) <= 192 * 1024:
            fields[key] = deepcopy(metadata[key]); used += len(encoded)
        else:
            omitted_count += 1
            if len(omitted) < 128:
                omitted.append({'field': str(key)[:256], 'sha256': sha256(encoded).hexdigest(), 'bytes': len(encoded)})
    snapshot = {'schema': IMPORT_PROVENANCE_SCHEMA, 'kind': kind,
                'claims_are_unverified': True, 'source_format': str(source_format)[:16],
                'source_name': str(source_name)[:256],
                'payload_sha256': sha256(payload if payload is not None else _json_bytes(original)).hexdigest(),
                'original_metadata_sha256': sha256(_json_bytes(metadata)).hexdigest(),
                'original_fields': fields, 'omitted_fields': omitted, 'omitted_field_count': omitted_count}
    if len(_json_bytes(snapshot)) > MAX_IMPORT_PROVENANCE_BYTES:
        raise ValueError('The imported provenance snapshot exceeds the supported size.')
    document.metadata['import_provenance'] = snapshot


def load_imported_lyrics(path: str | Path, duration: float = 0) -> LyricsDocument:
    """Explicit operator import: capture origin before any local audio/title binding."""
    file = Path(path)
    payload = _read_lyric_bytes(file)
    document = _parse_lyric_bytes(file, payload, duration)
    original = json.loads(payload.decode('utf-8-sig')) if file.suffix.lower() not in {'.lrc', '.srt', '.vtt'} else document.to_dict()
    preserve_import_provenance(document, original, payload=payload,
                               source_format=file.suffix.lower().lstrip('.') or 'json',
                               source_name=file.name, kind='explicit-file-import')
    if len(json.dumps(document.to_dict(), ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')) > MAX_LYRIC_BYTES:
        raise ValueError('The imported lyrics and provenance exceed the supported 8 MB saved-file size.')
    return document


def lyric_target_bytes(target: str | Path) -> bytes | None:
    """Bounded raw target snapshot for compare-and-swap writers; None means absent."""
    target = Path(target)
    if target.is_symlink():
        raise ValueError('Save lyrics to a regular JSON file, not a symbolic link.')
    if not target.exists():
        return None
    return _read_lyric_bytes(target)


def lyric_target_revision(target: str | Path) -> str | None:
    payload = lyric_target_bytes(target)
    return sha256(payload).hexdigest() if payload is not None else None


@contextmanager
def lyric_write_lock(target: str | Path):
    """Serialize cooperating manual/import/extraction writers, without Qt."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name("." + target.name + ".review.lock")
    if lock.is_symlink():
        raise ValueError("Refusing a symbolic-link lyric write lock.")
    with lock.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another lyric write is in progress. Retry after it finishes.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def save_lyrics(document: LyricsDocument, path: str | Path) -> None:
    """Atomic local save. Failed imports/extraction cannot leave a partial sidecar."""
    target = Path(path)
    with lyric_write_lock(target):
        _save_lyrics_unlocked(document, target)


def _save_lyrics_unlocked(document: LyricsDocument, target: Path) -> None:
    if target.is_symlink():
        raise ValueError("Choose a regular lyric file, not a symbolic link.")
    payload = json.dumps(document.to_dict(), ensure_ascii=False, allow_nan=False, indent=2)
    if len(payload.encode('utf-8')) > MAX_LYRIC_BYTES:
        raise ValueError('The lyric file exceeds the supported 8 MB saved-file size.')
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lyrics-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fingerprint(source: str | Path) -> str:
    with Path(source).open("rb") as handle:
        return file_digest(handle, "sha256").hexdigest()


def cached_lyrics_path(source: str | Path, cache_dir: str | Path) -> Path:
    return Path(cache_dir) / "lyrics" / (fingerprint(source) + ".json")


def find_lyrics(source: str | Path, cache_dir: str | Path,
                published_dir: str | Path | None = None) -> Path | None:
    """Use sidecars first. Matching stored fingerprints never trusts a title alone."""
    audio = Path(source)
    for extension in (".lyrics.json", ".lrc", ".srt", ".vtt", ".json"):
        candidate = audio.with_suffix(extension)
        if candidate.is_file() and candidate.stat().st_size <= MAX_LYRIC_BYTES:
            try:
                document = load_lyrics(candidate)
                if not document.source_sha256 or document.source_sha256 == fingerprint(audio):
                    return candidate
            except (OSError, ValueError):
                continue
    digest = fingerprint(audio)
    cached = Path(cache_dir) / "lyrics" / (digest + ".json")
    if cached.is_file():
        try:
            if load_lyrics(cached).source_sha256 == digest:
                return cached
        except (OSError, ValueError):
            pass
    if published_dir is not None:
        folder = Path(published_dir)
        try:
            paths = list(folder.glob("*.json"))[:10000]
        except OSError:
            return None
        for candidate in paths:
            try:
                if candidate.is_symlink() or candidate.stat().st_size > MAX_LYRIC_BYTES:
                    continue
                data = json.loads(candidate.read_text())
                if data.get("source_sha256") != digest:
                    continue
                automatic = (data.get("publication") or {}).get("method") == "automatic" and (data.get("quality") or {}).get("status") == "pass"
                if data.get("reviewed") or automatic:
                    load_lyrics(candidate)
                    return candidate
            except (OSError, ValueError, TypeError, AttributeError):
                continue
    return None
