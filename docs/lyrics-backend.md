# Local lyrics and model runtime

The player can use existing JSON, LRC, SRT and WebVTT files without AI models. Segment/word JSON retains measured word timing. LRC and ordinary subtitles retain their line timing; the player does not make up word intervals. Place sidecars beside recordings as `song.lyrics.json`, `song.lrc`, `song.srt` or `song.vtt`, or import them in the operator window.

Extraction runs in a separate process. The included routine performs CPU Demucs vocal separation, Whisper recognition, independent wav2vec2 CTC alignment, optional independent model review, and conservative quality checks. Optional Vulkan recognition and an isolated GPU alignment runtime can be configured. CPU fallback remains available. No model weights are bundled or automatically downloaded.

## Configure the model environment

Select a model Python executable in Extraction settings. It needs compatible installations of `torch`, `torchaudio`, `numpy`, `soundfile`, `faster-whisper`, `demucs`, `safetensors` and `num2words`, plus the local model files. `requirements-lyrics.txt` describes an optional CPU environment. Model versions and file formats must match the backend; installing the GUI does not install this environment.

The default user data directory is `PlaguesBroadcastStation` under Windows LocalAppData, or `plagues-broadcast-station` under Linux XDG_DATA_HOME (normally the user's `.local/share` directory). Model, Torch and extraction-cache folders default to children of that directory. These locations can be changed in settings. No machine-specific Python, GPU tool or lock location is inferred.

The same settings can be supplied before the first launch with environment variables:

| Variable | Setting |
| --- | --- |
| `GNB_PLAYER_PYTHON` | Model Python executable |
| `GNB_PLAYER_MODELS` | Local model root |
| `GNB_PLAYER_TORCH_HOME` | Torch model-cache root |
| `GNB_PLAYER_CACHE_DIR` | Writable extraction cache |
| `GNB_PLAYER_GPU_PYTHON` | Optional isolated GPU Python |
| `GNB_PLAYER_VULKAN_BINARY` | Optional Vulkan Whisper executable |
| `GNB_PLAYER_VULKAN_MODEL` | Matching local GGML Whisper model |
| `GNB_PLAYER_GPU_LOCK` | Optional existing advisory-lock file |
| `GNB_PLAYER_RECOGNITION_LANGUAGE` | Recognition language: `en` by default, `auto`, or a supported language code |
| `GNB_PLAYER_RECOGNITION_PROMPT` | Optional explicit context, empty by default, at most 500 characters |

Saved settings take precedence on later launches. Use the settings editor to change an existing configuration. Keep personal runtime paths in local settings rather than committing them to a repository.

Recognition always uses the `transcribe` task. Recording filenames are not used as
model prompts. Independent review uses no context prompt, and English-only models
remain English. A detected non-English recording is not passed through the English
CTC/reviewer as if its words had been verified. Explicit context can bias words;
leave it empty unless that context is intentional.

The alignment checkpoint belongs at `TORCH_HOME/hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth`. Faster Whisper models are loaded from `<model-root>/<engine>/model.bin`; Demucs uses `<model-root>/htdemucs/spec.json` and `model.safetensors`. A generic Demucs checkpoint is not interchangeable with this saved format. FFmpeg and ffprobe must be on PATH for source runs; native desktop packages include them.

## Processing and cancellation

Extraction accepts local recordings from 1 second to 15 minutes and up to 2 GB. Longer recordings are rejected before processing rather than silently truncated. Output is a separate JSON file keyed to the complete source SHA256. Saving is atomic; failed or cancelled jobs preserve the source and prior sidecar. Successful replacement keeps the previous JSON under `history/`.

GPU capacity is checked without loading a model. An explicitly configured advisory lock can coordinate applications that use the same lock; it does not reserve the GPU against unrelated software. If capacity is unavailable, extraction can wait or fall back to CPU. Playback continues independently. No model directories, drivers or unrelated services are modified.

On Linux, cancellation terminates the worker's isolated process group, including media and GPU child processes. On Windows the app terminates the owned worker process tree. Model-free regression tests cover POSIX process-group cancellation. GPU runtime support depends on the configured hardware, drivers and model environment; use CPU mode when a compatible accelerator is unavailable.

## Frozen packaging

The desktop package does not embed Torch or model weights. `extract_worker.py`, `extraction.py` and `extraction_backend/*.py` are bundled as ordinary data files so the external model Python can execute them. Do not select the frozen GUI executable as the model Python.

The GUI restores the native-library environment before starting external model Python. On Linux, only FFmpeg/ffprobe children receive the bundled media-library directory. Torch and GPU helpers retain their own runtime libraries. This bundle path is discovered again when the package moves.

Worker stdout is UTF-8 JSONL: `progress` (`phase`, `fraction`, `message`), `done` (`output`, `quality`, `words`, `lines`), `error` or `cancelled`. Diagnostic logs use stderr. A quality `review` result is a private draft that should be listened through before broadcast. Machine checks do not certify the words or timing.

Newly prepared JSON retains raw recognizer output, decoder settings, chunk/window
boundaries, independent-review evidence and before/after review snapshots under
`recognition_evidence`. Readable display lines may combine short fragments without
changing their words or measured word timestamps. QC version 3 flags split opening
credits and measured non-speech uncertainty; it does not silently remove a name,
an ordinary “thank you,” or an uncertain lyric. Existing cached drafts keep their
stored provenance and are not silently retranscribed or approved on upgrade.
