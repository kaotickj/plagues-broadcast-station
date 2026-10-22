# Find and install lyric models

The player can play music, show imported lyrics, record and broadcast without AI models. Model setup adds the separate local tools used to extract and align new lyrics. You can reuse compatible models already installed on the computer, install missing components, or keep using existing timed lyric files.

## Start the wizard

Open model setup from the operator's **Lyric models** tab. You can also open it directly:

```powershell
.\PlaguesBroadcastStation.exe --setup-models
```

From Python source, run `python launch.py --setup-models`. On Linux, run `./PlaguesBroadcastStation --setup-models`.

1. **Scan your computer.** Setup checks known model/cache locations, saved settings and folders you select. Add the parent folder of a collection if your models are on another drive. Scanning is bounded; it does not search the contents of personal documents or deserialize model checkpoints.
2. **Choose existing models.** Each candidate shows its role, format, location and compatibility result. Choose the compatible transcription, review, vocal separator and alignment models you want to reuse. A format check is followed by runtime validation before settings are applied.
3. **Choose a runtime and destination.** Reuse a compatible model Python, create a separate environment from an installed Python, or let Windows setup download its private supported Python. Choose a user-writable destination with enough free space.
4. **Review the plan.** The summary distinguishes reused files from downloads and shows estimated space. Scanning and reviewing do not download models.
5. **Install or reuse.** Choose **Install missing components** to download and prepare missing tools. If you have selected a compatible runtime and all four models, **Validate and use existing models** validates them offline without package or model downloads. Successful validation automatically saves the verified runtime and model locations to the player. If a step fails or you cancel it, earlier working settings and existing models remain available.

## Preferred quality pack

| Role | Model | Fresh weight download |
| --- | --- | --- |
| Sung-word recognition | Whisper **large-v3**, CTranslate2 format | About 3.09 GB |
| Second recognition pass | Whisper **medium.en**, CTranslate2 format | About 1.53 GB |
| Vocal separation | **HTDemucs** | About 84 MB |
| Word timing | **wav2vec2 base ASR 960h** | About 378 MB |

The weight download totals about **5.1 GB**. A fresh runtime, temporary files and model preparation need additional space; allow roughly **12–14 GB free** and use the wizard's plan-specific estimate. Existing compatible models reduce the download. Large-v3 remains the preferred transcription model; setup does not silently replace it with a tiny, distilled or turbo model to save space.

The published [large-v3 conversion](https://huggingface.co/Systran/faster-whisper-large-v3) and [medium.en conversion](https://huggingface.co/Systran/faster-whisper-medium.en) supply the CTranslate2 model files. The installer uses fixed publisher revisions and checks downloaded file integrity before use. The separator comes from the official [Demucs model registry](https://github.com/facebookresearch/demucs/blob/main/demucs/remote/files.txt), and the aligner uses the official [TorchAudio bundle](https://docs.pytorch.org/audio/2.8.0/generated/torchaudio.pipelines.WAV2VEC2_ASR_BASE_960H.html).

## What can be reused?

- A compatible CTranslate2 Whisper directory contains model weights, configuration, tokenizer and vocabulary files. Setup recognizes the model configuration, not just a directory named `large-v3` or a file named `model.bin`.
- Supported vocal-separator folders contain `spec.json` and `model.safetensors`. The installer can prepare its pinned official HTDemucs checkpoint into this format. It does not automatically execute arbitrary local PyTorch checkpoint packages.
- The supported wav2vec2 alignment checkpoint can be reused from a local Torch cache or a location selected in setup.
- A compatible existing model runtime must provide the required packages and alignment API. A Python executable alone is not proof that its model environment is ready.

An OpenAI Whisper `.pt` file, a whisper.cpp GGML file, an Ollama/GGUF model and a CTranslate2 Whisper directory are different formats. An incompatible candidate is explained in the scan results rather than relabelled as usable or deleted. You can keep it for its original application.

## Python and acceleration

The optional installer creates a CPU model environment with matching **Torch 2.8.0 and TorchAudio 2.8.0**. These versions retain the forced-alignment API used by the lyric routine; [TorchAudio documents its removal in 2.9](https://docs.pytorch.org/audio/master/tutorials/ctc_forced_alignment_api_tutorial.html). The GUI's bundled Python and libraries remain separate.

On Windows x86_64, setup can install a private [Python 3.13.17](https://www.python.org/downloads/release/python-31317/) when an appropriate Python is unavailable. It verifies the official installer before execution and uses per-user installation options. A normal setup does not require administrator access or replace the computer's default Python.

If Windows reports a missing native library or DLL during validation, the error includes repair guidance. Some systems need Microsoft's [supported x64 Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist). Install that prerequisite separately if needed, then retry setup; the wizard does not silently install system-wide prerequisites.

CPU mode is the standard installer path. Detecting a GPU or an Ollama GPU backend does not by itself establish GPU support for Whisper, Torch or word alignment. Existing explicitly configured GPU tools remain an advanced option; the installer does not change graphics drivers or promise acceleration for an unsupported card.

## Connect existing Ollama models

Use the Ollama connection area to check a local endpoint and choose an installed model tag. The default endpoint is `http://127.0.0.1:11434`. Discovery uses Ollama's [list-models API](https://docs.ollama.com/api/tags), so it does not need to move, rename or interpret its model blobs.

Ollama is a separate text-model connection. Its tags do not satisfy the player's Whisper, vocal-separation or word-alignment requirements. Saving the connection records the local endpoint and selected tag; it does not make a text model transcribe the song, install Ollama, or pull another model automatically.

If Ollama is absent, install it using the [official Windows instructions](https://docs.ollama.com/windows), start it and scan again. If it uses a custom `OLLAMA_MODELS` directory, keep that setting with Ollama. Setup preserves the existing model store and other applications' configuration.

## Check the result

After successful setup automatically applies settings, the Lyric models tab should show a ready model runtime. Test extraction on one recording, listen to the result and inspect its quality notes before preparing a whole playlist. Large models improve the tools available to the pipeline; they do not guarantee correct sung words or timings.

Setup runs in a separate cancellable worker, with progress and errors saved as UTF-8 events so the packaged Windows app does not rely on a console window. Installer tests and native build checks use temporary plans and never download model weights. Model files, personal paths and local settings are not embedded in the application package or committed with its source.
