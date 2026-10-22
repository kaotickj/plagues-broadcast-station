"""Native FFmpeg output from clean frames and decoded program audio.

Only temporary 127.0.0.1 sockets are opened. There is no microphone capture,
window capture, automatic external connection, or shell command construction.
Call start() with an explicit destination to record or stream.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit
import math
import os
import re
import shutil
import socket
import subprocess
import time
from typing import Any

from PySide6.QtCore import QObject, Signal, Qt
from PySide6.QtGui import QImage

AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_SAMPLE_BYTES = 4
AUDIO_FRAME_BYTES = AUDIO_CHANNELS * AUDIO_SAMPLE_BYTES
AUDIO_CHUNK_FRAMES = 960  # 20ms, paced against the same clock as video.
MAX_AUDIO_BYTES = AUDIO_RATE * AUDIO_FRAME_BYTES * 2


@dataclass(frozen=True)
class BroadcastConfig:
    mode: str = "record"
    target: str = ""
    width: int = 1280
    height: int = 720
    fps: int = 30
    bitrate_kbps: int = 4500
    ffmpeg_path: str = "ffmpeg"
    reconnect_attempts: int = 3

    def validate(self) -> None:
        if self.mode not in {"record", "stream"}:
            raise ValueError("Choose recording or direct streaming.")
        if not isinstance(self.width, int) or not isinstance(self.height, int) or self.width % 2 or self.height % 2 or not (320 <= self.width <= 3840 and 180 <= self.height <= 2160):
            raise ValueError("Choose even video dimensions between 320×180 and 3840×2160.")
        if not isinstance(self.fps, int) or not 10 <= self.fps <= 60:
            raise ValueError("Choose a video frame rate from 10 to 60 fps.")
        if not isinstance(self.bitrate_kbps, int) or not 300 <= self.bitrate_kbps <= 50000:
            raise ValueError("Choose a video bitrate from 300 to 50000 kbps.")
        if not isinstance(self.target, str) or not self.target.strip() or any(ord(c) < 32 for c in self.target):
            raise ValueError("Choose a valid recording filename or streaming destination.")
        if self.mode == "record":
            target = Path(self.target).expanduser()
            if target.suffix.lower() not in {".mp4", ".mkv"}:
                raise ValueError("Recordings must use an .mp4 or .mkv filename.")
            if target.exists() or target.is_symlink():
                raise ValueError("That recording already exists. Choose a new filename.")
            if not target.parent.is_dir():
                raise ValueError("Choose an existing folder for the recording.")
        else:
            try:
                target = urlsplit(self.target)
                port = target.port
            except ValueError:
                raise ValueError("Enter a valid RTMP or RTMPS destination.") from None
            if target.scheme.lower() not in {"rtmp", "rtmps"} or not target.hostname or target.fragment or any(c.isspace() for c in self.target):
                raise ValueError("Direct streaming requires an RTMP or RTMPS server URL.")
            if port is not None and not 1 <= port <= 65535:
                raise ValueError("The streaming server port is invalid.")
        if not isinstance(self.reconnect_attempts, int) or not 0 <= self.reconnect_attempts <= 5:
            raise ValueError("Reconnect attempts must be between 0 and 5.")
        if not shutil.which(self.ffmpeg_path) and not Path(self.ffmpeg_path).is_file():
            raise ValueError("FFmpeg was not found. Select its executable in settings.")


def redact(message: str, target: str = "") -> str:
    """Never expose an ingest key or full network destination in status/logs."""
    message = str(message)
    if target:
        message = message.replace(target, "[broadcast destination]")
        try:
            parsed = urlsplit(target)
            # FFmpeg sometimes logs only the final path/key rather than URL.
            if parsed.scheme.lower() in {"rtmp", "rtmps"}:
                for secret in [parsed.path.lstrip("/"), parsed.path.rsplit("/", 1)[-1], parsed.query, parsed.password]:
                    if secret and len(secret) >= 4:
                        message = message.replace(secret, "[redacted]")
        except ValueError:
            pass
    message = re.sub(r"(?i)\b(?:rtmps?|https?|tcp)://[^\s\]\[<>\"']+", "[network destination]", message)
    return message[:1200]


def build_command(config: BroadcastConfig, video_port: int, audio_port: int) -> list[str]:
    """All arguments are passed directly; target text is never shell code."""
    destination = str(Path(config.target).expanduser().resolve()) if config.mode == "record" else config.target
    command = [
        config.ffmpeg_path, "-hide_banner", "-loglevel", "warning", "-nostdin", "-n",
        "-thread_queue_size", "8", "-f", "rawvideo", "-pixel_format", "rgb24",
        "-video_size", f"{config.width}x{config.height}", "-framerate", str(config.fps),
        "-analyzeduration", "0", "-probesize", "32", "-i", f"tcp://127.0.0.1:{video_port}?timeout=5000000",
        "-thread_queue_size", "64", "-f", "f32le", "-ar", str(AUDIO_RATE), "-ac", str(AUDIO_CHANNELS),
        "-analyzeduration", "0", "-probesize", "32", "-i", f"tcp://127.0.0.1:{audio_port}?timeout=5000000",
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
        "-tune", "zerolatency", "-threads", "2", "-b:v", f"{config.bitrate_kbps}k",
        "-maxrate", f"{config.bitrate_kbps}k", "-bufsize", f"{config.bitrate_kbps * 2}k",
        "-pix_fmt", "yuv420p", "-g", str(config.fps * 2), "-keyint_min", str(config.fps * 2),
        "-sc_threshold", "0", "-c:a", "aac", "-b:a", "192k", "-ar", str(AUDIO_RATE), "-ac", "2",
        # -fps_mode was added after the FFmpeg shipped by Ubuntu 22.04.
        # The equivalent global CFR option is supported by both that 4.4
        # release and current FFmpeg (the encoder has one video output).
        "-vsync", "cfr", "-shortest",
    ]
    if config.mode == "stream":
        command += ["-f", "flv", "-flvflags", "no_duration_filesize"]
    elif Path(config.target).suffix.lower() == ".mp4":
        command += ["-movflags", "+faststart"]
    command.append(destination)
    return command


class BroadcastSession(QObject):
    status = Signal(str)
    error = Signal(str)
    statistics = Signal(dict)
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = Lock()
        self._stop = Event()
        self._running = False
        self._manager: Thread | None = None
        self._config: BroadcastConfig | None = None
        self._image: QImage | None = None
        self._frame: bytes = b""
        self._frame_version = 0
        self._audio = bytearray()
        self._audio_dropped_frames = 0
        self._video_written = 0
        self._video_duplicated = 0
        self._video_skipped_input = 0
        self._audio_written_frames = 0
        self._audio_silence_frames = 0
        self._delivery_lag_seconds = 0.0
        self._started_at = 0.0
        self._reconnects = 0
        self._transport: Event | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self, config: BroadcastConfig) -> None:
        config.validate()
        with self._lock:
            if self._running:
                raise RuntimeError("A recording or direct stream is already running.")
            self._config = config
            self._running = True
            self._audio.clear()
            self._audio_dropped_frames = self._video_written = self._video_duplicated = self._video_skipped_input = 0
            self._audio_written_frames = self._audio_silence_frames = self._reconnects = 0
            self._delivery_lag_seconds = 0.0
            self._stop.clear()
            self._frame = bytes(config.width * config.height * 3)
            if self._image is not None:
                self._frame = self._image_bytes(self._image, config)
            self._started_at = time.monotonic()
        self.status.emit("Starting local recording…" if config.mode == "record" else "Connecting to the streaming server…")
        self._manager = Thread(target=self._run, name="broadcast-encoder", daemon=True)
        self._manager.start()

    def stop(self) -> None:
        """Asynchronous: finished emits after FFmpeg flushes the file trailer."""
        if not self.running:
            return
        self._stop.set()
        self.status.emit("Finishing the recording…" if self._config and self._config.mode == "record" else "Stopping the stream…")

    def wait(self, timeout: float | None = None) -> bool:
        """Shutdown/testing helper. GUI code should use the finished signal."""
        manager = self._manager
        if manager is not None:
            manager.join(timeout)
            return not manager.is_alive()
        return True

    @staticmethod
    def _image_bytes(image: QImage, config: BroadcastConfig) -> bytes:
        if image.width() != config.width or image.height() != config.height:
            image = image.scaled(config.width, config.height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        image = image.convertToFormat(QImage.Format.Format_RGB888)
        raw = bytes(image.constBits())
        stride, row = image.bytesPerLine(), config.width * 3
        if stride == row:
            return raw[:row * config.height]
        return b"".join(raw[index * stride:index * stride + row] for index in range(config.height))

    def set_video_frame(self, image: QImage) -> None:
        if image is None or image.isNull():
            return
        # QImage is implicitly shared; keep an owned snapshot across threads.
        image = image.copy()
        with self._lock:
            config = self._config
            running = self._running
        frame = self._image_bytes(image, config) if config is not None and running else b""
        with self._lock:
            self._image = image
            if frame:
                self._frame = frame
                self._frame_version += 1

    def push_audio(self, audio: bytes) -> None:
        if not isinstance(audio, (bytes, bytearray, memoryview)) or not audio:
            return
        raw = bytes(audio)
        raw = raw[:len(raw) - len(raw) % AUDIO_FRAME_BYTES]
        with self._lock:
            if not self._running:
                return
            self._audio.extend(raw)
            overflow = len(self._audio) - MAX_AUDIO_BYTES
            if overflow > 0:
                overflow += (-overflow) % AUDIO_FRAME_BYTES
                del self._audio[:overflow]
                self._audio_dropped_frames += overflow // AUDIO_FRAME_BYTES

    def clear_audio(self) -> None:
        with self._lock:
            self._audio.clear()

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self._running, "elapsed_seconds": round(time.monotonic() - self._started_at, 2),
                "video_frames": self._video_written, "duplicated_video_frames": self._video_duplicated,
                "replaced_video_inputs": self._video_skipped_input, "audio_frames": self._audio_written_frames,
                "audio_dropped_frames": self._audio_dropped_frames, "silence_frames": self._audio_silence_frames,
                "audio_buffer_seconds": round(len(self._audio) / (AUDIO_RATE * AUDIO_FRAME_BYTES), 3),
                "delivery_lag_seconds": round(self._delivery_lag_seconds, 3), "reconnects": self._reconnects,
            }

    def _listener(self) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(.2)
        return listener

    def _run(self) -> None:
        config = self._config
        assert config is not None
        try:
            attempts = config.reconnect_attempts if config.mode == "stream" else 0
            for number in range(attempts + 1):
                if self._stop.is_set():
                    break
                problem = self._run_once(config)
                if self._stop.is_set():
                    if problem and problem.startswith("Encoder shutdown timed out"):
                        self.error.emit(problem)
                    break
                if problem is None:
                    problem = "The encoder ended before the broadcast was stopped."
                if number >= attempts:
                    self.error.emit(redact(problem, config.target))
                    break
                with self._lock:
                    self._reconnects += 1
                    self._audio.clear()
                self.status.emit(f"Connection lost. Reconnecting ({number + 1}/{attempts})…")
                self.statistics.emit(self._snapshot())
                if self._stop.wait(min(10.0, 2.0 ** number)):
                    break
        except Exception as exception:
            self.error.emit(redact(f"The broadcast encoder could not start: {exception}", config.target))
        finally:
            with self._lock:
                self._running = False
            self.statistics.emit(self._snapshot())
            self.status.emit("Recording finished." if config.mode == "record" else "Stream stopped.")
            self.finished.emit()

    def _run_once(self, config: BroadcastConfig) -> str | None:
        transport_stop = Event()
        ready = Event()
        connected = {"video": False, "audio": False}
        clock = {"base": 0.0}
        errors: deque[str] = deque(maxlen=12)
        stderr_lines: deque[str] = deque(maxlen=24)
        listeners: list[socket.socket] = []
        clients: list[socket.socket] = []
        workers: list[Thread] = []
        process: subprocess.Popen[bytes] | None = None
        self._transport = transport_stop

        def stopped() -> bool:
            return self._stop.is_set() or transport_stop.is_set()

        def pace(deadline: float) -> bool:
            while not stopped():
                delay = deadline - time.monotonic()
                if delay <= 0:
                    return True
                transport_stop.wait(min(.02, delay))
            return False

        def connect(listener: socket.socket) -> socket.socket | None:
            began = time.monotonic()
            while not stopped() and time.monotonic() - began < 12:
                try:
                    client, address = listener.accept()
                    if address[0] != "127.0.0.1":
                        client.close()
                        continue
                    client.settimeout(2.0)
                    clients.append(client)
                    return client
                except socket.timeout:
                    continue
                except OSError:
                    return None
            if not stopped():
                errors.append("The encoder did not accept its local audio/video connection.")
                transport_stop.set()
            return None

        def stream_video(listener: socket.socket) -> None:
            client = connect(listener)
            if client is None:
                return
            try:
                with self._lock:
                    initial, previous_version = self._frame, self._frame_version
                # FFmpeg needs one video frame before it opens the audio input.
                client.sendall(initial)
                with self._lock:
                    self._video_written += 1
                    connected["video"] = True
                    if connected["audio"]:
                        clock["base"] = time.monotonic()
                        ready.set()
                while not stopped() and not ready.wait(.02):
                    pass
                sequence = 1
                while not stopped():
                    if not pace(clock["base"] + sequence / config.fps):
                        break
                    lag = max(0.0, time.monotonic() - clock["base"] - sequence / config.fps)
                    with self._lock:
                        self._delivery_lag_seconds = max(self._delivery_lag_seconds, lag)
                        frame, version = self._frame, self._frame_version
                        if version == previous_version:
                            self._video_duplicated += 1
                        elif version > previous_version + 1:
                            self._video_skipped_input += version - previous_version - 1
                    if lag > 2:
                        raise TimeoutError("The encoder cannot keep up. Lower the video resolution or frame rate.")
                    client.sendall(frame)
                    with self._lock:
                        self._video_written += 1
                    previous_version = version
                    sequence += 1
            except (OSError, TimeoutError) as error:
                if not self._stop.is_set():
                    errors.append(redact(str(error), config.target))
                    transport_stop.set()
            finally:
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                client.close()

        def stream_audio(listener: socket.socket) -> None:
            client = connect(listener)
            if client is None:
                return
            try:
                with self._lock:
                    connected["audio"] = True
                    if connected["video"]:
                        clock["base"] = time.monotonic()
                        ready.set()
                while not stopped() and not ready.wait(.02):
                    pass
                sequence = 0
                size = AUDIO_CHUNK_FRAMES * AUDIO_FRAME_BYTES
                while not stopped():
                    if not pace(clock["base"] + sequence * AUDIO_CHUNK_FRAMES / AUDIO_RATE):
                        break
                    with self._lock:
                        take = min(size, len(self._audio))
                        chunk = bytes(self._audio[:take])
                        del self._audio[:take]
                        missing = size - take
                        self._audio_silence_frames += missing // AUDIO_FRAME_BYTES
                    if missing:
                        chunk += bytes(missing)
                    client.sendall(chunk)
                    with self._lock:
                        self._audio_written_frames += AUDIO_CHUNK_FRAMES
                    sequence += 1
            except OSError as error:
                if not self._stop.is_set():
                    errors.append(redact(str(error), config.target))
                    transport_stop.set()
            finally:
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                client.close()

        try:
            video = self._listener()
            listeners.append(video)
            audio = self._listener()
            listeners.append(audio)
            options: dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "stdin": subprocess.DEVNULL}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW
            process = subprocess.Popen(build_command(config, video.getsockname()[1], audio.getsockname()[1]), **options)

            def read_errors() -> None:
                assert process is not None and process.stderr is not None
                for line in iter(process.stderr.readline, b""):
                    stderr_lines.append(redact(line.decode(errors="replace").strip(), config.target))

            for name, target, listener in [("video", stream_video, video), ("audio", stream_audio, audio)]:
                worker = Thread(target=target, args=(listener,), name="broadcast-" + name, daemon=True)
                workers.append(worker)
                worker.start()
            reader = Thread(target=read_errors, name="broadcast-encoder-log", daemon=True)
            reader.start()
            announced = False
            reported = 0.0
            while process.poll() is None and not stopped():
                if ready.is_set() and not announced:
                    self.status.emit("Recording clean program output." if config.mode == "record" else "Sending the clean program stream.")
                    announced = True
                if time.monotonic() - reported >= 1:
                    self.statistics.emit(self._snapshot())
                    reported = time.monotonic()
                self._stop.wait(.1)
            transport_stop.set()
            for worker in workers:
                worker.join(2.5)
            for client in clients:
                try:
                    client.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
            try:
                returncode = process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    returncode = process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    returncode = process.wait(timeout=2)
                if self._stop.is_set():
                    return "Encoder shutdown timed out; the recording may be incomplete."
            reader.join(1)
            if returncode:
                detail = " ".join(stderr_lines)[-900:]
                return f"Encoder stopped with an error. {detail}" if detail else (errors[-1] if errors else "The encoder could not produce the broadcast output.")
            if errors and not self._stop.is_set():
                return errors[-1]
            return None
        finally:
            transport_stop.set()
            for listener in listeners:
                listener.close()
            for client in clients:
                try:
                    client.close()
                except OSError:
                    pass
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            self._transport = None
