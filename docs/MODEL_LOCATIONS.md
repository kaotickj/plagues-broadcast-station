# Existing lyric models and runtime paths

Open **Lyric models** to see checked runtime/model locations. Discovery checks
known local paths without loading model weights, copying files or downloading.
Models are optional for ordinary media playback.

Valid custom paths stay selected. A missing custom path stays visible with its
missing-file diagnostic; automatic discovery does not silently replace it. Old,
unmarked portable default paths are recognized as defaults. A path entered
explicitly in the GUI is remembered as a deliberate choice, even if it equals a
default location. Clear an override and save to return it to automatic selection.
Settings and lyric caches remain in each station's own data directory.

On Linux, unset/default fields can reuse these locations when the required files
exist and are accessible:

| Field | Existing shared candidate |
| --- | --- |
| Lyric runtime Python | `/opt/gnb-lyrics-quality/bin/python` |
| Model folder | `/var/lib/gnb-lyrics/models` |
| Torch model cache | `/var/lib/gnb-lyrics/torch` |
| GPU runtime Python | `/opt/gnb-lyrics-gpu/bin/python` |
| Whisper Vulkan executable | `/opt/gnb-whisper-vulkan-src/build/bin/whisper-cli` |
| Whisper Vulkan model | `/var/lib/gnb-lyrics/models/whisper-vulkan/ggml-large-v3-q5_0.bin` |
| Shared GPU lock | `/run/lock/gnb-ai-gpu.lock` |

These are candidates, not an assumption that every machine has them. Other
installations keep their existing portable/user-local locations. GPU discovery
does not turn on a saved off choice for **Use GPU when available**.

The four component fields are optional overrides. When empty, they show
**Automatic: …** with the effective location. Typical shared-layout values are:

| Component | Effective location |
| --- | --- |
| Whisper large-v3 | `/var/lib/gnb-lyrics/models/large-v3` |
| Independent review | `/var/lib/gnb-lyrics/models/medium.en` |
| Vocal separator | `/var/lib/gnb-lyrics/models/htdemucs` |
| Word alignment | `/var/lib/gnb-lyrics/torch/hub/checkpoints/wav2vec2_fairseq_base_ls960_asr_ls960.pth` |

The selected recognizer determines the primary component; a different selection
can use `medium.en` or `small.en`. The GUI lists actual missing files rather than
claiming that an empty override is itself a missing model. File checks establish
presence/access only, not successful inference or model quality.

**Ollama text model** is optional and expects an installed model tag, not a path.
The setup dialog can list the existing loopback Ollama service's tags. This
version saves that connection/selection but does not use it for audio recognition
or independent lyric review. Leave it empty when not using the optional connector.
