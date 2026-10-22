# Plague's Broadcast Station

A Python desktop player for music livestreams: a rotating image/video backdrop, synchronized sing-along lyrics, and a calm waveform driven by the music actually playing. An operator window manages the show; a separate clean 16:9 Program Output window is ready for OBS capture. Local video recording and optional direct streaming use FFmpeg.

The music, lyrics, waveform and broadcast view follow the same player clock. The player supports shuffle/repeat, seeking, playback controls and adjustable lyric offset. Background motion, lyric display and the visualizer can be adjusted separately. Lyric extraction runs outside the GUI process and can wait for available GPU capacity without stopping the music.

## Run a packaged build

Extract the entire platform ZIP and keep `PlaguesBroadcastStation` beside its `_internal` folder. On Linux run `PlaguesBroadcastStation` or `run-linux.sh`; on Windows run `PlaguesBroadcastStation.exe` or `run-windows.bat`. Python is bundled for ordinary playback; the optional AI model runtime is separate.

On Linux the desktop needs working Qt platform/audio libraries. If an X11 startup reports a missing `libxcb-cursor.so.0`, install your distribution's `libxcb-cursor0` package. Builds should be produced on an older supported Linux distribution when broad glibc compatibility is required; a build made on this newer Debian host is intended for a compatible host, not every Linux distribution.

A media pack is separate from the application. Extract a pack and open its `profile.json` in the player. A detected starter pack can load on first launch; later launches restore your saved session and settings. Profiles use relative media paths, so a whole media pack can be moved between Windows and Linux. Source recordings and model weights are not stored in Git or embedded in CI artifacts.

## First show

1. Add local audio or video recordings to the playlist, or open a saved profile/M3U.
2. Add your images and silent video backdrops. Choose the station name and visual settings.
3. Import timed lyrics, or extract a local lyric draft for a selected recording. Listen through draft lyrics before using them on-air.
4. Open Program Output and start playback. Capture that window and the application's sound in OBS, or configure the app's local recording/direct-stream output.
5. Make a short local test recording and check the picture, lyrics and sound before starting a public stream.

An on-screen waveform is a visual response to the decoded audio, not microphone input. Background videos are silent to keep the song as the soundtrack. The operator controls and local file paths are kept out of the clean program picture.

See [the OBS guide](docs/OBS.md) for scene and audio setup. No livestream is started merely by opening the app. Direct streaming requires an explicit start action and a destination/key supplied by the operator; stream keys are not saved in profiles.

## Lyrics

Supported imports are segment/word JSON, LRC, SRT and WebVTT. JSON may contain measured word times for karaoke highlighting. Ordinary LRC/subtitle files remain line-timed; the app does not invent word timing from the text.

The included extraction routine separates the vocal, recognizes it with Whisper, aligns the words independently, and can perform a second model review. Choose a separate model Python environment and local model folders in Extraction settings. CPU mode works without a supported GPU; optional GPU tools and an advisory lock are configured explicitly. The app never downloads model weights on startup.

Default model/cache folders live under the application's user data directory. A model Python executable must be selected explicitly. The player itself does not need Torch. [Model setup, cancellation and output details](docs/lyrics-backend.md) explain the optional backend and environment settings. `requirements-lyrics.txt` is for a separate CPU model environment; its Torch/TorchAudio versions are pinned below 2.9 to retain the forced-alignment API.

In a source checkout, FFmpeg and ffprobe must be on PATH for extraction and metadata probing. Native packages include FFmpeg and ffprobe executables with their dependencies for recording/direct streaming. Source runs require those tools on PATH. Existing timed lyric imports work without the model environment.

## Run from Python source

Use Python 3.11 or newer; Python 3.12 is the CI build version.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install '.[build,test]'
./run-linux.sh
```

On Windows:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install '.[build,test]'
.\run-windows.bat
```

To select a particular profile:

```sh
.venv/bin/python launch.py --profile /path/to/profile.json
```

## Build native packages

Run `./build-linux.sh` on Linux or `./build-windows.ps1` in Windows PowerShell. These install build dependencies into `.venv`, run tests, then create a one-folder native package with PyInstaller. Keep the resulting folder intact. ZIPs are written under `artifacts/`.

The GitHub Actions workflow builds Linux and Windows separately on standard hosted runners. It uses no GPU runners, model downloads, private media or livestream credentials. A Windows executable must be built on Windows; a successful Linux build is not evidence that a Windows build passed. Native build artifacts and test results should be checked before calling a release tested on that platform.

The spec bundles FFmpeg/ffprobe and their native dependencies, retains Qt Multimedia and its playback backend dependencies, and excludes WebEngine, PDF and the heavyweight AI libraries from the GUI bundle. Builds fail clearly if the FFmpeg tools are absent. External-worker Python sources are bundled as data so the selected model Python can execute them. The optional models remain outside the package.

```sh
QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg .venv/bin/python -m pytest -q
```

No public broadcast is used by the tests. Source media is read-only during playback and extraction. Extraction saves new local JSON atomically; failed/cancelled jobs preserve earlier lyric files, and successful replacement keeps their history.

Third-party license texts, native-build version information and notices are included with the package. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), including corresponding-source requirements when redistributing GPL-configured FFmpeg builds.
