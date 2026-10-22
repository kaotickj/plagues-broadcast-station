"""Timed lyric interchange. Imports retain measured words; plain subtitles stay line-timed."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from hashlib import file_digest
from pathlib import Path
from typing import Any
import json
import math
import os
import re
import tempfile

MAX_LYRIC_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass
class Cue:
    text: str
    start: float
    end: float
    words: list[Word] = field(default_factory=list)


@dataclass
class LyricsDocument:
    segments: list[Cue]
    duration: float
    title: str = ""
    source_sha256: str = ""
    warnings: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)

    def active_cue(self, seconds: float) -> Cue | None:
        if not math.isfinite(seconds) or seconds < 0 or not self.segments:
            return None
        index = bisect_right([cue.start for cue in self.segments], seconds) - 1
        if index < 0:
            return None
        cue = self.segments[index]
        return cue if cue.start <= seconds < cue.end else None

    def active_word(self, seconds: float) -> Word | None:
        cue = self.active_cue(seconds)
        if cue is None:
            return None
        return next((word for word in cue.words if word.start <= seconds < word.end), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": "broadcast-player-lyrics-1",
            "title": self.title,
            "duration": self.duration,
            "source_sha256": self.source_sha256,
            "warnings": self.warnings,
            "quality": self.quality,
            "segments": [{"start": c.start, "end": c.end, "text": c.text,
                          "words": [{"text": w.text, "start": w.start, "end": w.end}
                                    for w in c.words]} for c in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], duration: float = 0) -> "LyricsDocument":
        if not isinstance(data, dict):
            raise ValueError("The lyric file must contain a JSON object.")
        raw = data.get("segments", [])
        if not isinstance(raw, list) or len(raw) > 10000:
            raise ValueError("The lyric file has too many lines or an invalid line list.")
        warnings = [str(value)[:1000] for value in data.get("warnings", [])[:100]] if isinstance(data.get("warnings"), list) else []
        cues: list[Cue] = []
        invalid_words = False
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("A lyric line is invalid.")
            start, end = _interval(item.get("start"), item.get("end"))
            text = str(item.get("text", ""))[:20000]
            words: list[Word] = []
            for word in item.get("words", []) if isinstance(item.get("words"), list) else []:
                try:
                    a, b = _interval(word.get("start"), word.get("end"))
                    if a < start - .1 or b > end + .1:
                        raise ValueError("Word outside its line")
                    words.append(Word(str(word.get("text", ""))[:1000], a, b))
                except (AttributeError, ValueError, TypeError):
                    invalid_words = True
            if text.strip():
                cues.append(Cue(text, start, end, sorted(words, key=lambda value: value.start)))
        cues.sort(key=lambda cue: cue.start)
        if invalid_words:
            warnings.append("Some imported words had invalid timing; their line text is retained without guessed word timing.")
        supplied_duration = _finite(data.get("duration", duration or (cues[-1].end if cues else 0)))
        if supplied_duration < 0:
            raise ValueError("Lyric duration must be positive.")
        fingerprint = str(data.get("source_sha256", ""))
        if fingerprint and not re.fullmatch(r"[a-fA-F0-9]{64}", fingerprint):
            raise ValueError("The lyric file contains an invalid source fingerprint.")
        quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
        return cls(cues, supplied_duration, str(data.get("title", ""))[:1000], fingerprint.lower(), warnings, quality)


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


def load_lyrics(path: str | Path, duration: float = 0) -> LyricsDocument:
    file = Path(path)
    if not file.is_file() or file.stat().st_size > MAX_LYRIC_BYTES:
        raise ValueError("Choose a lyric JSON, LRC, SRT or VTT file smaller than 8 MB.")
    text = file.read_text(encoding="utf-8-sig")
    suffix = file.suffix.lower()
    if suffix == ".lrc":
        return parse_lrc(text, duration, file.stem)
    if suffix in {".srt", ".vtt"}:
        return parse_subtitles(text, duration, file.stem)
    return LyricsDocument.from_dict(json.loads(text), duration)


def save_lyrics(document: LyricsDocument, path: str | Path) -> None:
    """Atomic local save. Failed imports/extraction cannot leave a partial sidecar."""
    target = Path(path)
    if target.is_symlink():
        raise ValueError("Choose a regular lyric file, not a symbolic link.")
    payload = json.dumps(document.to_dict(), ensure_ascii=False, allow_nan=False, indent=2)
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
