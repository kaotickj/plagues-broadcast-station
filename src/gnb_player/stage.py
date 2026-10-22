"""Clean broadcast compositor: one clock, measured lyrics, and actual audio samples.

The same painter serves the operator preview, presentation window and encoder.
No controls or file paths are painted into the broadcast output.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import random
import time
from typing import Any

import numpy as np
from PySide6.QtCore import Qt, QTimer, QRectF, Signal, QUrl
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QImage, QKeyEvent, QLinearGradient,
    QPainter, QPainterPath, QPen, QRadialGradient,
)
from PySide6.QtWidgets import QWidget


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}
BASE_W, BASE_H = 1280.0, 720.0


@dataclass
class _Backdrop:
    path: Path | None = None
    image: QImage | None = None
    video: bool = False


def _get(item: Any, key: str, default: Any = None) -> Any:
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (ValueError, TypeError):
        return default


def _font(size: float, *, serif: bool = False, bold: bool = False) -> QFont:
    font = QFont("DejaVu Serif" if serif else "DejaVu Sans")
    font.setPixelSize(max(1, round(size)))
    font.setBold(bold)
    return font


class BroadcastStage(QWidget):
    """A clean, independently capturable 16:9 program window.

    Visual animation stops with playback. Timed words use actual playback
    position, with only sub-frame interpolation between position notifications.
    No word timestamps are fabricated for line-only subtitle formats.
    """

    background_changed = Signal(str)
    background_error = Signal(str)
    fullscreen_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Goodneighbor Broadcast · Program Output")
        self.setObjectName("broadcastProgramOutput")
        self.setAccessibleName("Clean broadcast program output")
        self.setMinimumSize(480, 270)
        self.resize(1280, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._settings: dict[str, Any] = {
            "brand": "GOODNEIGHBOR BLUES", "tagline": "Original blues. Stay awhile.",
            "interval_seconds": 18.0, "fade_seconds": 1.8, "motion": True,
            "waveform": True, "lyrics": True, "slideshow_paused": False,
        }
        self._title, self._artist, self._album = "", "", ""
        self._position, self._duration, self._playing = 0.0, 0.0, False
        self._position_stamp = time.monotonic()
        self._offset = 0.0
        self._lyrics: Any = None
        self._backgrounds: list[Path] = []
        self._bag: list[Path] = []
        self._history: list[Path] = []
        self._backdrop = _Backdrop()
        self._outgoing = _Backdrop()
        self._fade_started: float | None = None
        self._slide_started = 0.0
        self._visual_seconds = 0.0
        self._last_tick = time.monotonic()
        self._video_player: Any = None
        self._video_sink: Any = None
        self._video_output: Any = None
        self._video_request: Path | None = None
        self._video_remember = True
        self._video_is_song = False
        self._song_frame: QImage | None = None
        self._wave_target = np.zeros(96, dtype=np.float32)
        self._wave_display = np.zeros(96, dtype=np.float32)
        self._disposed = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    @property
    def position(self) -> float:
        position = self._position
        if self._playing:
            position += max(0.0, time.monotonic() - self._position_stamp)
        return min(position, self._duration) if self._duration > 0 else position

    @property
    def lyric_position(self) -> float:
        return max(0.0, self.position + self._offset)

    @property
    def current_background(self) -> Path | None:
        return self._backdrop.path

    def set_track(self, title: str, artist: str = "", album: str = "") -> None:
        self._title, self._artist, self._album = str(title), str(artist), str(album)
        self._song_frame = None
        self._video_is_song = False
        self.update()

    def set_playback(self, position: float, duration: float, playing: bool) -> None:
        was_playing = self._playing
        self._position, self._duration = max(0.0, _finite(position)), max(0.0, _finite(duration))
        self._playing = bool(playing)
        self._position_stamp = time.monotonic()
        if self._video_player is not None and was_playing != self._playing:
            self._video_player.play() if self._playing else self._video_player.pause()
        if not self._playing:
            self._wave_target.fill(0)
        self.update()

    def set_lyrics(self, document: Any | None) -> None:
        self._lyrics = document
        self.update()

    def set_offset(self, seconds: float) -> None:
        self._offset = max(-30.0, min(30.0, _finite(seconds)))
        self.update()

    def set_waveform(self, samples: Any) -> None:
        try:
            values = np.asarray(samples, dtype=np.float32).reshape(-1)
            if not len(values):
                self._wave_target.fill(0)
                return
            values = np.nan_to_num(values[-8192:], nan=0.0, posinf=0.0, neginf=0.0)
            values = np.clip(values, -1, 1)
            chunks = np.array_split(values, len(self._wave_target))
            self._wave_target = np.array([
                math.sqrt(float(np.mean(chunk * chunk))) if len(chunk) else 0.0
                for chunk in chunks
            ], dtype=np.float32)
        except (ValueError, TypeError):
            self._wave_target.fill(0)

    def set_visual_settings(self, **kwargs: Any) -> None:
        for key in self._settings:
            if key in kwargs:
                self._settings[key] = kwargs[key]
        self._settings["brand"] = str(self._settings["brand"])[:120]
        self._settings["tagline"] = str(self._settings["tagline"])[:200]
        self._settings["interval_seconds"] = max(5.0, min(300.0, _finite(self._settings["interval_seconds"], 18)))
        self._settings["fade_seconds"] = max(0.0, min(5.0, _finite(self._settings["fade_seconds"], 1.8)))
        if self._video_player is not None:
            if self._playing and not self._settings["slideshow_paused"]:
                self._video_player.play()
            else:
                self._video_player.pause()
        self.update()

    def set_backgrounds(self, paths: list[Path]) -> None:
        if not paths:
            self._backgrounds, self._bag, self._history = [], [], []
            self._backdrop, self._outgoing = _Backdrop(), _Backdrop()
            self._fade_started = self._video_request = None
            if self._video_player is not None:
                self._video_player.stop()
            self.update()
            return
        unique: list[Path] = []
        seen: set[str] = set()
        for raw in paths:
            path = Path(raw).expanduser()
            try:
                key = str(path.resolve())
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES | VIDEO_SUFFIXES and key not in seen:
                    unique.append(path)
                    seen.add(key)
            except OSError:
                continue
        self._backgrounds = unique
        self._bag.clear()
        self._history.clear()
        # Invalid choices never replace a previously decoded, usable backdrop.
        for path in unique:
            if self._show_background(path, remember=False):
                break
        self.update()

    def _choose_next(self) -> Path | None:
        if not self._backgrounds:
            return None
        if len(self._backgrounds) == 1:
            return self._backgrounds[0]
        if not self._bag:
            self._bag = [path for path in self._backgrounds if path != self._backdrop.path]
            random.shuffle(self._bag)
        return self._bag.pop() if self._bag else None

    def next_background(self) -> None:
        for _ in range(len(self._backgrounds)):
            path = self._choose_next()
            if path is None or path == self._backdrop.path:
                return
            if self._show_background(path):
                return

    def previous_background(self) -> None:
        while self._history:
            if self._show_background(self._history.pop(), remember=False):
                return

    def _show_background(self, path: Path, *, remember: bool = True) -> bool:
        if path.suffix.lower() in VIDEO_SUFFIXES:
            return self._start_video(path, remember=remember)
        image = QImage(str(path))
        if image.isNull():
            self.background_error.emit(f"Could not decode background: {path.name}")
            return False
        image = image.scaled(2560, 1440, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation) if image.width() > 2560 or image.height() > 1440 else image
        if self._video_player is not None:
            self._video_player.stop()
        self._video_request = None
        self._set_backdrop(_Backdrop(path, image, False), remember)
        return True

    def _set_backdrop(self, backdrop: _Backdrop, remember: bool = True) -> None:
        if remember and self._backdrop.path and self._backdrop.path != backdrop.path:
            self._history.append(self._backdrop.path)
            self._history = self._history[-max(2, len(self._backgrounds) * 2):]
        self._outgoing = self._backdrop
        self._backdrop = backdrop
        self._slide_started = self._visual_seconds
        self._fade_started = self._visual_seconds if self._outgoing.image is not None else None
        self.background_changed.emit(str(backdrop.path or ""))
        self.update()

    def _start_video(self, path: Path, *, remember: bool = True) -> bool:
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QVideoSink
            if self._video_player is None:
                self._video_player = QMediaPlayer(self)
                # No audio output is attached: backdrop audio is discarded,
                # and preview-only use does not open another audio device.
                self._video_player.setAudioOutput(None)
                self._video_sink = QVideoSink(self)
                self._video_player.setVideoSink(self._video_sink)
                self._video_sink.videoFrameChanged.connect(self._backdrop_video_frame)
                self._video_player.errorOccurred.connect(self._backdrop_video_error)
                self._video_player.setLoops(QMediaPlayer.Loops.Infinite)
            self._video_request = path
            self._video_remember = remember
            self._video_player.setSource(QUrl.fromLocalFile(str(path.resolve())))
            # Decode a first frame even when the music is paused, then pause.
            self._video_player.play()
            return True
        except (ImportError, RuntimeError, OSError) as error:
            self.background_error.emit(f"Video background unavailable: {path.name} ({error})")
            return False

    def _backdrop_video_frame(self, frame: Any) -> None:
        if self._video_request is None or not frame.isValid():
            return
        image = frame.toImage()
        if image.isNull():
            return
        if self._backdrop.path != self._video_request:
            self._set_backdrop(_Backdrop(self._video_request, image, True), self._video_remember)
        else:
            self._backdrop.image = image
        if (not self._playing or self._settings["slideshow_paused"]) and self._video_player is not None:
            self._video_player.pause()
        self.update()

    def _backdrop_video_error(self, *args: Any) -> None:
        path = self._video_request
        self._video_request = None
        if path:
            self.background_error.emit(f"Could not play background: {path.name}")
        # The last successfully decoded image remains painted.

    def set_video_frame(self, image: QImage | None) -> None:
        if image is None or image.isNull():
            self._song_frame = None
            self._video_is_song = False
        else:
            self._song_frame = image.copy()
            self._video_is_song = True
        self.update()

    def _tick(self) -> None:
        if self._disposed:
            return
        now = time.monotonic()
        elapsed = min(.25, max(0.0, now - self._last_tick))
        self._last_tick = now
        if self._playing and not self._settings["slideshow_paused"]:
            self._visual_seconds += elapsed
            if self._visual_seconds - self._slide_started >= self._settings["interval_seconds"]:
                self.next_background()
                self._slide_started = self._visual_seconds
        if self._fade_started is not None and self._visual_seconds - self._fade_started >= self._settings["fade_seconds"]:
            self._fade_started = None
            self._outgoing = _Backdrop()
        # Smooth measured energy; no oscillator or fabricated rhythm.
        target = self._wave_target if self._playing else np.zeros_like(self._wave_target)
        coefficient = .30 if self._playing else .18
        self._wave_display += (target - self._wave_display) * coefficient
        self.update()

    def render_frame(self, width: int, height: int) -> QImage:
        width, height = max(1, min(7680, int(width))), max(1, min(4320, int(height)))
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(QColor("#081117"))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        scale = min(width / BASE_W, height / BASE_H)
        painter.translate((width - BASE_W * scale) / 2, (height - BASE_H * scale) / 2)
        painter.scale(scale, scale)
        self._paint_scene(painter)
        painter.end()
        return image

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.drawImage(self.rect(), self.render_frame(self.width(), self.height()))
        painter.end()

    def _draw_image(self, painter: QPainter, image: QImage, progress: float, opacity: float = 1.0) -> None:
        if image.isNull() or opacity <= 0:
            return
        target = QRectF(0, 0, BASE_W, BASE_H)
        aspect = BASE_W / BASE_H
        iw, ih = float(image.width()), float(image.height())
        source_w, source_h = (ih * aspect, ih) if iw / ih > aspect else (iw, iw / aspect)
        if self._settings["motion"] and not self._backdrop.video:
            zoom = 1 + .035 * max(0.0, min(1.0, progress))
            source_w /= zoom
            source_h /= zoom
        x = (iw - source_w) / 2
        y = (ih - source_h) / 2
        if self._settings["motion"] and not self._backdrop.video:
            x += (iw - source_w) * .12 * math.sin(progress * .8)
        painter.save()
        painter.setOpacity(max(0.0, min(1.0, opacity)))
        painter.drawImage(target, image, QRectF(x, y, source_w, source_h))
        painter.restore()

    def _paint_scene(self, p: QPainter) -> None:
        canvas = QRectF(0, 0, BASE_W, BASE_H)
        gradient = QLinearGradient(0, 0, BASE_W, BASE_H)
        gradient.setColorAt(0, QColor("#102c37"))
        gradient.setColorAt(.6, QColor("#09171f"))
        gradient.setColorAt(1, QColor("#241a19"))
        p.fillRect(canvas, gradient)
        progress = (self._visual_seconds - self._slide_started) / self._settings["interval_seconds"]
        if self._song_frame is not None:
            self._draw_image(p, self._song_frame, 0)
        else:
            if self._outgoing.image is not None:
                self._draw_image(p, self._outgoing.image, 1)
            opacity = 1.0
            if self._fade_started is not None and self._settings["fade_seconds"] > 0:
                opacity = min(1.0, (self._visual_seconds - self._fade_started) / self._settings["fade_seconds"])
            if self._backdrop.image is not None:
                self._draw_image(p, self._backdrop.image, progress, opacity)
        veil = QLinearGradient(0, 0, 0, BASE_H)
        veil.setColorAt(0, QColor(3, 10, 15, 185))
        veil.setColorAt(.35, QColor(3, 10, 15, 35))
        veil.setColorAt(.75, QColor(3, 10, 15, 85))
        veil.setColorAt(1, QColor(3, 10, 15, 232))
        p.fillRect(canvas, veil)
        self._draw_header(p)
        if self._settings["lyrics"] and self._lyrics is not None:
            self._draw_lyrics(p)
        elif not self._title:
            p.setPen(QColor("#e5c58f"))
            p.setFont(_font(43, serif=True))
            p.drawText(QRectF(135, 275, 1010, 100), Qt.AlignmentFlag.AlignCenter, str(self._settings["tagline"]))
        self._draw_footer(p)

    def _draw_header(self, p: QPainter) -> None:
        p.setPen(QColor("#e5c58f"))
        p.setFont(_font(19, bold=True))
        brand = QFontMetricsF(p.font()).elidedText(str(self._settings["brand"]), Qt.TextElideMode.ElideRight, 970)
        p.drawText(QRectF(58, 35, 970, 30), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, brand)
        p.setPen(QPen(QColor(222, 187, 125, 115), 1))
        p.drawLine(58, 81, 1222, 81)
        p.setPen(QColor("#b2b6b1"))
        p.setFont(_font(13))
        text = "NOW PLAYING" if self._playing else "STAY AWHILE"
        p.drawText(QRectF(1080, 37, 140, 28), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, text)

    def _lyric_rows(self) -> tuple[Any | None, str, str]:
        if self._lyrics is None:
            return None, "", ""
        segments = _get(self._lyrics, "segments", [])
        position = self.lyric_position
        for index, cue in enumerate(segments):
            if _finite(_get(cue, "start")) <= position < _finite(_get(cue, "end")):
                return cue, str(_get(segments[index - 1], "text", "")) if index else "", str(_get(segments[index + 1], "text", "")) if index + 1 < len(segments) else ""
        next_index = next((i for i, c in enumerate(segments) if _finite(_get(c, "start")) > position), len(segments))
        before = str(_get(segments[next_index - 1], "text", "")) if next_index > 0 else ""
        after = str(_get(segments[next_index], "text", "")) if next_index < len(segments) else ""
        return None, before, after

    def _draw_lyrics(self, p: QPainter) -> None:
        cue, previous, upcoming = self._lyric_rows()
        panel = QRectF(120, 207, 1040, 271)
        p.setPen(QPen(QColor(220, 183, 119, 115), 1))
        p.setBrush(QColor(6, 17, 23, 205))
        p.drawRoundedRect(panel, 16, 16)
        p.setFont(_font(19, serif=True))
        p.setPen(QColor("#aeb5b3"))
        previous = QFontMetricsF(p.font()).elidedText(previous.replace("\n", " "), Qt.TextElideMode.ElideRight, 920)
        upcoming = QFontMetricsF(p.font()).elidedText(upcoming.replace("\n", " "), Qt.TextElideMode.ElideRight, 920)
        p.drawText(QRectF(170, 231, 940, 35), Qt.AlignmentFlag.AlignCenter, previous)
        p.drawText(QRectF(170, 416, 940, 35), Qt.AlignmentFlag.AlignCenter, upcoming)
        if cue is None:
            p.setFont(_font(49, serif=True))
            p.setPen(QColor("#e4c385"))
            p.drawText(QRectF(180, 283, 920, 94), Qt.AlignmentFlag.AlignCenter, "♪")
            return
        text = str(_get(cue, "text", ""))
        measured = list(_get(cue, "words", []) or [])
        normalize = lambda s: " ".join(str(s).split())
        # If imported words do not cover the line, retain exact line text and
        # show line-level captions instead of misleading word highlights.
        aligned = measured if normalize(" ".join(str(_get(w, "text", "")) for w in measured)) == normalize(text) else []
        tokens = [str(_get(w, "text", "")) for w in aligned] if aligned else text.split()
        if not tokens:
            return
        box = QRectF(173, 278, 934, 126)
        lines: list[list[tuple[str, Any | None, float]]] = []
        font = _font(40, serif=True)
        for size in range(40, 19, -1):
            font = _font(size, serif=True)
            fm = QFontMetricsF(font)
            lines, row, row_width = [], [], 0.0
            for index, token in enumerate(tokens):
                width = fm.horizontalAdvance(token)
                space = fm.horizontalAdvance(" ") if row else 0
                if row and row_width + space + width > box.width():
                    lines.append(row)
                    row, row_width, space = [], 0.0, 0.0
                row.append((token, aligned[index] if aligned else None, width))
                row_width += width + space
            if row:
                lines.append(row)
            if len(lines) * fm.height() <= box.height():
                break
        p.setFont(font)
        fm = QFontMetricsF(font)
        total_height = len(lines) * fm.height()
        baseline = box.y() + (box.height() - total_height) / 2 + fm.ascent()
        position = self.lyric_position
        p.save()
        p.setClipRect(box.adjusted(-4, -8, 4, 12))
        for row in lines:
            total_width = sum(part[2] for part in row) + max(0, len(row) - 1) * fm.horizontalAdvance(" ")
            x = box.center().x() - total_width / 2
            for token, word, width in row:
                start, end = _finite(_get(word, "start")), _finite(_get(word, "end"))
                current = word is not None and start <= position < end
                sung = word is not None and position >= end
                p.setPen(QColor("#fff1cb") if current else QColor("#e9c78d") if sung else QColor("#d7d4cd"))
                p.drawText(x, baseline, token)
                if current and end > start:
                    fraction = max(0.0, min(1.0, (position - start) / (end - start)))
                    p.setPen(QPen(QColor("#f2cf89"), 2.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    p.drawLine(x, baseline + 7, x + max(2, width * fraction), baseline + 7)
                x += width + fm.horizontalAdvance(" ")
            baseline += fm.height()
        p.restore()

    def _draw_footer(self, p: QPainter) -> None:
        p.setPen(QColor("#f1ddb4"))
        p.setFont(_font(30, serif=True))
        title = self._title or str(self._settings["tagline"])
        title = QFontMetricsF(p.font()).elidedText(title, Qt.TextElideMode.ElideRight, 1120)
        p.drawText(QRectF(70, 537, 1140, 42), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, title)
        p.setPen(QColor("#b5bcba"))
        p.setFont(_font(16))
        credit = " · ".join(item for item in [self._artist, self._album] if item)
        p.drawText(QRectF(72, 582, 1120, 30), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   QFontMetricsF(p.font()).elidedText(credit, Qt.TextElideMode.ElideRight, 1120))
        if self._settings["waveform"]:
            self._draw_waveform(p, QRectF(300, 637, 680, 45))
        p.setPen(QPen(QColor(199, 168, 112, 88), 1))
        p.drawLine(70, 698, 1210, 698)
        if self._duration > 0:
            p.setPen(QPen(QColor("#d8b678"), 2))
            p.drawLine(70, 698, 70 + 1140 * min(1.0, max(0.0, self.position / self._duration)), 698)

    def _draw_waveform(self, p: QPainter, rect: QRectF) -> None:
        middle = rect.center().y()
        p.setPen(QPen(QColor(195, 162, 104, 70), 1))
        p.drawLine(rect.left(), middle, rect.right(), middle)
        p.setPen(QPen(QColor("#dfbd7f"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        count = len(self._wave_display)
        for i, raw in enumerate(self._wave_display):
            taper = .28 + .72 * math.sin(math.pi * (i + .5) / count)
            amplitude = min(rect.height() * .47, max(.35, float(raw) * rect.height() * 1.75 * taper))
            x = rect.left() + rect.width() * (i + .5) / count
            p.drawLine(x, middle - amplitude, x, middle + amplitude)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.showNormal()
            self.fullscreen_changed.emit(False)
            event.accept()
        elif event.key() in {Qt.Key.Key_F11, Qt.Key.Key_F}:
            self._toggle_fullscreen()
            event.accept()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_fullscreen()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()
        self.fullscreen_changed.emit(self.isFullScreen())

    def shutdown(self) -> None:
        """Release compositor resources when the operator application exits.

        Closing just the presentation window must not freeze a native stream
        or operator preview, both of which use this same offscreen renderer.
        """
        self._disposed = True
        self._timer.stop()
        if self._video_player is not None:
            self._video_player.stop()
            self._video_player.setSource(QUrl())

    def closeEvent(self, event: Any) -> None:
        # QWidget.close hides this independent window. Keep the compositor
        # alive for the operator preview and any native encoder session.
        super().closeEvent(event)

    def showEvent(self, event: Any) -> None:
        self._disposed = False
        self._last_tick = time.monotonic()
        if not self._timer.isActive():
            self._timer.start()
        super().showEvent(event)
