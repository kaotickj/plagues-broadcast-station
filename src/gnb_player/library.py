"""Local media cataloguing. Importing a recording never moves or edits it."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Callable, Iterable


AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".aiff", ".aif", ".wma"})
VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".mpeg", ".mpg"})
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

# These editorial spellings also appear in the Goodneighbor site catalog.
_ALIASES = {
    "aintnoeasyroadhome": "Ain't No Easy Road Home",
    "aintnogoodthing": "Ain't No Good Thing",
    "aintthatahardroad": "Ain't That a Hard Road",
    "backdoorkey": "Back Door Key",
    "baptizedintheglowingsea": "Baptized in the Glowing Sea",
    "bootsinblack": "Boots in Black",
    "deadmanwalkingthecharles": "Dead Man Walking the Charles",
    "deathclawgotacrushonme": "Deathclaw Got a Crush on Me",
    "greenglowinthesky": "Green Glow in the Sky",
    "greenlightonthehighway": "Green Light on the Highway",
    "igotmoleratsinmybasement": "I Got Mole Rats in My Basement",
    "livinaloneinthecommonwealth": "Livin' Alone in the Commonwealth",
    "moonoverboston": "Moon Over Boston",
    "myneighborsasupermutant": "My Neighbor's a Super Mutant",
    "neontearsatclosingtime": "Neon Tears at Closing Time",
    "onemoreshotmaccreadysstory": "One More Shot — MacCready's Story",
    "onemoreshotofgoodbye": "One More Shot of Goodbye",
    "redrocketdowntheroad": "Red Rocket Down the Road",
    "redrocketwhiskey": "Red Rocket Whiskey",
    "smokeaftermidnight": "Smoke After Midnight",
    "smokybluesclub": "Smoky Blues Club",
    "thebrahminatemygarden": "The Brahmin Ate My Garden",
    "thecommonwealthtookitsdue": "The Commonwealth Took Its Due",
    "themanileftbehindhancocksstory": "The Man I Left Behind — Hancock's Story",
    "thewastelandblues": "The Wasteland Blues",
    "tincanwhiskey": "Tin Can Whiskey",
    "whiskeyblues": "Whiskey Blues",
    "whiskeyinmybones": "Whiskey in My Bones",
    "whiskeywastelandandbaddecisions": "Whiskey Wasteland and Bad Decisions",
    "yourcupiscold": "Your Cup Is Cold",
}
_MINOR = {"a", "an", "the", "and", "as", "but", "for", "if", "nor", "or", "so", "yet", "at", "by", "in", "of", "off", "on", "per", "to", "up", "via"}
_PROPER = {"maccready": "MacCready", "maccready's": "MacCready's", "mccready": "McCready", "mccready's": "McCready's", "dj": "DJ", "ai": "AI", "cd": "CD", "tv": "TV", "nasa": "NASA", "usa": "USA", "uk": "UK", "ii": "II", "iii": "III", "iv": "IV"}
_PARTICLES = {"give up", "get up", "stand up", "wake up", "grow up", "break up", "look up", "turn up", "turn off", "take off", "hold on", "carry on", "come on", "move on", "go on"}
_WORDS = re.compile(r"[^\W_]+(?:['’][^\W_]+)*(?:-[^\W_]+(?:['’][^\W_]+)*)*", re.UNICODE)


def normalize_title(value: str) -> str:
    """APA-style display case with known compact Goodneighbor spellings.

    Unknown concatenated names stay intact: guessing their word boundaries can
    damage proper names. This function is never used to rename source files.
    """
    value = str(value).strip()
    compact = re.sub(r"[^a-z0-9]", "", value.casefold())
    if compact in _ALIASES:
        return _ALIASES[compact]
    value = re.sub(r"^\d{2,3}[\s._-]+", "", value)
    value = re.sub(r"_+", " ", value)
    value = re.sub(r"\s+", " ", value)
    matches = list(_WORDS.finditer(value))
    if not matches:
        return value or "Untitled Recording"
    parts, end, previous = [], 0, ""
    for index, match in enumerate(matches):
        gap = value[end:match.start()]
        word = match.group()
        lower = word.casefold().replace("’", "'")
        force = index in (0, len(matches) - 1) or bool(re.search(r"[:—–.!?]\s*[\"'“‘(]*$", gap))
        if lower in _PROPER:
            formatted = _PROPER[lower]
        elif previous + " " + lower in _PARTICLES:
            formatted = word[:1].upper() + word[1:].lower()
        else:
            components = word.split("-")
            formatted = "-".join(
                piece.lower() if piece.casefold() in _MINOR and not (force and i in (0, len(components)-1))
                else piece[:1].upper() + piece[1:].lower()
                for i, piece in enumerate(components)
            )
        parts.extend((gap, formatted))
        end, previous = match.end(), lower
    parts.append(value[end:])
    return "".join(parts)


@dataclass
class Track:
    path: Path
    title: str
    artist: str = ""
    album: str = ""
    duration: float = 0.0

    def __post_init__(self):
        self.path = Path(self.path).expanduser()
        self.duration = float(self.duration) if isinstance(self.duration, (int, float)) and math.isfinite(self.duration) else 0.0

    @property
    def is_video(self) -> bool:
        return self.path.suffix.lower() in VIDEO_EXTENSIONS

    def to_dict(self) -> dict:
        return {**asdict(self), "path": str(self.path)}

    @classmethod
    def from_dict(cls, data: dict) -> "Track":
        return cls(Path(data["path"]), str(data.get("title") or normalize_title(Path(data["path"]).stem)),
                   str(data.get("artist") or ""), str(data.get("album") or ""), data.get("duration", 0.0))


def probe_track(path: str | Path, *, ffprobe: str | None = None, timeout: float = 8.0) -> Track:
    """Read bounded local metadata. An unavailable probe still permits playback."""
    path = Path(path).expanduser().resolve()
    title, artist, album, duration = normalize_title(path.stem), "", "", 0.0
    executable = ffprobe or shutil.which("ffprobe")
    if executable:
        try:
            result = subprocess.run(
                [executable, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                 "format=duration:format_tags=title,artist,album:stream_tags=title,artist,album", "-of", "json", str(path)],
                capture_output=True, timeout=timeout, check=True,
            )
            if len(result.stdout) > 1024 * 1024:
                raise ValueError("Media metadata is too large")
            data = json.loads(result.stdout)
            info = data.get("format") or {}
            tags = {str(k).casefold(): v for stream in data.get("streams", []) for k, v in (stream.get("tags") or {}).items()}
            tags.update({str(k).casefold(): v for k, v in (info.get("tags") or {}).items()})
            title = normalize_title(str(tags.get("title") or path.stem)[:600])
            artist, album = str(tags.get("artist") or "")[:500], str(tags.get("album") or "")[:500]
            measured = float(info.get("duration") or 0)
            duration = measured if math.isfinite(measured) and 0 <= measured <= 7 * 86400 else 0.0
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
            pass
    return Track(path, title, artist, album, duration)


def scan_media(paths: Iterable[str | Path], recursive: bool = True,
               progress: Callable[[int, str], None] | None = None,
               *, max_files: int = 10000, cancelled: Callable[[], bool] | None = None) -> list[Track]:
    """Find recordings, deduplicate canonical paths, and probe without a shell.

    Run this in a worker thread for large folders. Only media extensions are
    inspected; documents and other files are ignored.
    """
    found: dict[Path, None] = {}
    for original in paths:
        if cancelled and cancelled():
            break
        path = Path(original).expanduser()
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
        else:
            iterator = (path,)
        try:
            for item in iterator:
                if cancelled and cancelled():
                    break
                if len(found) >= max_files:
                    break
                try:
                    if item.suffix.lower() in MEDIA_EXTENSIONS and item.is_file():
                        found[item.resolve()] = None
                except OSError:
                    continue
        except (OSError, PermissionError):
            continue
    tracks = []
    for index, path in enumerate(sorted(found, key=lambda p: str(p).casefold())):
        if cancelled and cancelled():
            break
        if progress:
            progress(index, str(path))
        tracks.append(probe_track(path))
    if progress:
        progress(len(tracks), "Library scan complete")
    return tracks
