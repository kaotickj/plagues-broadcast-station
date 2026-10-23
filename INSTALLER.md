# Linux installer and upgrade

The local 0.2.4rc1 candidate uses the same managed installer and rollback format.
See [candidate acceptance](docs/RELEASE_CANDIDATE.md) for scope and verification
limits. Preparing this package does not upgrade an existing installation.

The Linux `.run` package installs Plague's Broadcast Station without replacing an extracted application folder. It requires Linux with Python 3.11 or newer, compatible glibc and working desktop/audio libraries. Python for ordinary playback is bundled; Python 3 runs the small installer and stable launcher. This is a **Linux installer**. Windows keeps its existing native ZIP build and CI path.

First choose a new, empty application directory and the **same data directory already passed by your existing launcher**. Data includes saved sessions, playlists, settings, artwork references, model/runtime locations, lyrics and their quality/review history. The installer does not copy, rewrite, scan or delete that data. Keep external media, artwork, model weights and runtimes in place. Do not select an old extracted application folder as the installation root.

Version 0.2.3 adds explicit native protected Broadcast profile save/restore; see `docs/BROADCAST_PROFILE.md`. The complete target and key stay outside portable/session/settings files. Upgrades preserve only the opaque profile marker in the chosen data folder; native credentials remain with the same OS user and canonical data-folder namespace. Rollback to 0.2.2 ignores that marker and returning to 0.2.3 can restore it. Native credential failure has no plaintext fallback.

Version 0.2.2 adds explicit human lyric review bound to both the recording fingerprint and exact lyric revision. Installation, upgrade and rollback preserve those approval records, drafts, original machine findings and raw prior JSON history as stored. Approval is never inferred from a machine QC pass. Human-saved revisions retain their first exact QC findings in `review_original_quality` and keep effective QC in review status; 0.2.2 recognizes a valid manual approval, while 0.2.1 keeps those revisions off-air after executable rollback. This build does not alter a currently running player; use the stable launcher when ready to open the selected installed release.

```sh
# Verify the checksum next to the package (integrity, not publisher authentication).
cd /path/to/downloads
sha256sum -c PlaguesBroadcastStation-0.2.3-linux-x86_64.run.sha256

# FIRST INSTALL: replace the example data path with the exact existing --data-dir.
sh PlaguesBroadcastStation-0.2.3-linux-x86_64.run \
  --install-dir "$HOME/.local/opt/plagues-broadcast-station" \
  --data-dir "/path/to/existing broadcast data" \
  --desktop-entry

# Launch when ready using the stable executable, or the opted-in application menu entry.
"$HOME/.local/opt/plagues-broadcast-station/bin/plagues-broadcast-station"

# UPGRADE: run the next package with the SAME install directory.
# No --data-dir is needed; the selected directory is remembered.
sh /path/to/next-linux-x86_64.run \
  --install-dir "$HOME/.local/opt/plagues-broadcast-station"

# Show installed/current/previous versions, exact source and saved data path.
"$HOME/.local/opt/plagues-broadcast-station/bin/plagues-broadcast-station" --version
"$HOME/.local/opt/plagues-broadcast-station/bin/plagues-broadcast-station" --installer-info

# Roll back the executable release. This does not roll back or change application data.
"$HOME/.local/opt/plagues-broadcast-station/bin/plagues-broadcast-station" --installer-rollback
```

All paths may contain spaces. The install location defaults to `~/.local/opt/plagues-broadcast-station`; specifying it explicitly is useful when keeping more than one installation. `--data-dir` is required on first install and must be outside the install directory, with neither directory containing the other. An upgrade refuses a different data path. The stable launcher adds the remembered `--data-dir` and forwards other arguments directly, without a shell. For a separate session, use a separate install root. The installer never changes an existing shortcut automatically and never starts, stops or relaunches a running player. A running older release keeps using its existing files; switching happens on the next launch through the stable launcher.

`--desktop-entry` explicitly opts in to a stable application-menu entry in `$XDG_DATA_HOME/applications` (default `~/.local/share/applications`). `--desktop-file "/chosen/path/Station.desktop"` chooses another entry location, including a desktop shortcut if desired. Existing unrelated entries or entries owned by another install are refused before activation. An owned entry always points to the stable launcher. New entries use the bundled app icon copied to the stable `INSTALL_DIR/app-icon.png`. Omit desktop options to leave shortcuts untouched. Running `--help`, `--status` or `--rollback` on the `.run` package is also supported. The installed manager is available through `python3 INSTALL_DIR/installer.py --install-dir INSTALL_DIR --status` or `--rollback`.

### One-time migration of an existing portable launcher

If the standard menu entry already launches the portable app, explicitly adopt it instead of creating a confusing duplicate:

```sh
sh PlaguesBroadcastStation-0.2.3-linux-x86_64.run \
  --install-dir "$HOME/.local/opt/plagues-broadcast-station" \
  --data-dir "/the same data directory used by the portable launcher" \
  --adopt-desktop-entry
```

This selects the existing standard menu file automatically; add `--desktop-file` only if it is elsewhere. Adoption requires `Type=Application`, the exact name `Plague's Broadcast Station`, and a direct absolute `PlaguesBroadcastStation` or `run-linux.sh` Exec with only `--data-dir` and the **same** selected session path. The old command is parsed without executing it. Shell wrappers, other apps, mismatched sessions and entries belonging to another managed install are refused. Before replacement, the exact original is saved beside the entry as `.desktop.pbs-before-managed-HASH`; that backup is never overwritten and its suffix does not create a second menu application. Existing Icon, Path, Categories and startup fields are retained. Later upgrades keep the same stable command and need no further adoption.

XFCE panel launchers can contain private copies of menu entries. Updating the menu does not replace those copies. When first moving from a portable build, remove the old pinned launcher and pin the managed application-menu entry once, or explicitly repair that known panel entry. The installer does not scan desktop/panel configuration or change additional shortcuts. This one-time migration is separate from normal upgrades through the stable launcher.

Optional desktop integration happens after the package is fully installed. If its final write fails, the package remains valid, the old menu file remains available, and the error says to rerun the installer to retry integration. This does not require reinstalling or moving data.

Each release lives under `INSTALL_DIR/releases/RELEASE_ID`. The ID includes the app version, source commit and bundle hash. `install.json` atomically selects `current` and `previous`; the stable executable lives at `INSTALL_DIR/bin/plagues-broadcast-station`. The launcher reports the package version and source commit. Extraction happens in a new staging folder. Payload checksum, every file/link, manifest identity and a native `--version` startup check must pass before activation. Unsafe paths, escaping links and special archive files are refused. Installer operations are locked; repeated installation of the same package is safe and preserves the previous rollback target. A failed activation leaves the previous selection in place, and rerunning the same package can complete an interrupted installation. A process killed before cleanup can leave its uniquely named staging directory; it is not treated as an installed release or automatically purged.

Previous releases are retained without automatic pruning. Rollback verifies the target before atomically swapping `current` and `previous`. It preserves the selected data path and desktop entry. No unknown application folder, cache, runtime, model or data directory is removed. Rollback only changes executables: compatibility with data written by a future app version must be considered separately. This installer performs no schema migration.

The payload checksum and per-file manifest detect accidental damage. They are not a signature or proof of publisher authenticity: a maliciously replaced installer can replace its checksums too. Obtain packages through a trusted source. The optional menu entry and installation require no administrator privileges when their selected locations are user-writable.

## Build and provenance

The normal `./build-linux.sh` and Linux GitHub Actions job run `scripts/build_linux_installer.py --record-source` immediately before the clean PyInstaller build. Packaging requires the same exact source identity afterward and requires native `--version` to agree with `pyproject.toml`. The manifest records the Git commit, a SHA-256 of source filenames/content, whether the checkout was dirty, build Python, build host, architecture, bundle identity and individual file checksums/link targets. Linux packaging also emits a versioned `-portable.tar.gz` with the same manifest and preserved native executable bits/symlinks, plus its checksum. Extract that archive into a separate empty folder and run its `PlaguesBroadcastStation` binary or `run-linux.sh`; it does not perform a managed upgrade. Existing ZIPs and the Windows build remain available. No network updater, system package installation or model download is part of this installer.

For broad Linux compatibility, build on the oldest supported distribution. A local Debian 13/glibc 2.41 build is for a compatible x86_64 host; it is not evidence that Ubuntu 22.04 or every Linux distribution can run that artifact. The Linux CI job continues to build on Ubuntu 22.04. Windows packaging has not been validated by this local Linux test run.

Focused verification:

```sh
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg \
  python -m pytest -q tests/test_linux_installer.py tests/test_packaging.py
```

The lifecycle/security tests use disposable session files containing lyrics/review history, playlists, settings, artwork and model placeholders. They compare content and modification times after upgrade and rollback; no production session or real shortcut is touched. Coverage includes repeated install, exact launcher arguments and version/status, failed/abrupt activation and retry, failed rollback, broken native startup, damaged payloads, unsafe archives, marker/lock ownership, modified rollback targets, conflicting data directories and optional desktop entry ownership/validation. Legacy migration tests verify explicit consent, exact app/session checks, no-clobber backup, preserved icon/working directory, repeat install and recovery after an optional desktop write fails. Archive tests include a nonzero payload offset, as used by the executable installer header. Where installed, `desktop-file-validate` and an actual `gio launch` exercise a disposable fixture through the desktop parser, including spaces, quotes, backslashes, percent signs, dollar signs and backticks. Desktop quoting follows the [Desktop Entry specification](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html).

The 0.2.2 preservation fixture additionally creates a valid recording/revision-bound approved lyric document, a separately edited draft and exact prior-JSON history using the application's review APIs. Installer checks preserve every file's bytes and modification time and verify approval still belongs to the same recording/revision afterward. `scripts/check_linux_packages.py --previous-installer /path/to/0.2.1.run` checks an actual native upgrade and rollback; it also extracts/verifies the portable archive and starts only its own temporary offscreen GUI processes. Cooperating 0.2.2 import, manual-save and extraction writers share a per-file lock and reject stale base content or approval state. Unrelated/legacy writers do not honor that lock; their subsequent payload changes cannot inherit a valid revision-bound approval.

## 0.2.3 build and verification, 22 October 2026

The final native package was built from clean commit **`31e6dd15dcb85ab249c3d461e77e95cb2f9e0f3e`**, source-tree SHA-256 `65865aa89c4fb1d66cec9e06b34b67a2b65149cea1cff82ffa63ce0db84bea0e`, on `codex/lyrics-review-workflow`. Origin remains `https://github.com/kaotickj/plagues-broadcast-station.git`. The source receipt and manifest both record `source_dirty=false`. This receipt was added afterward as documentation only; the source archive below contains the exact native build source. The final release is `0.2.3-31e6dd15dcb8-b4d48ca565bf`, with **873 file/link entries** in each native format.

Version 0.2.3 adds explicit **Save/update**, **Restore saved**, **Forget saved** and **Clear fields** controls for direct livestreams. The entire destination and masked key, including any inline URL credentials, are stored together only in native protected credentials. The local reference contains schema, slot and a non-secret random revision token. Native services are scoped to the same OS user, canonical actual data folder and slot. The revision binding rejects stale native fallback copies. Two slots and a per-folder lock preserve the previously selected profile when saving fails. Forget can clean interrupted-save orphans and damaged references in this version’s format. Native storage failure remains session-only with honest feedback and no plaintext fallback; restore never starts output. See `docs/BROADCAST_PROFILE.md`.

Built and tested on **Debian 13.7 x86_64, glibc 2.41**, with Python 3.13.5 / PyInstaller 6.22.3 / PySide6 6.12.0. A workspace-only `.venv` references the pre-existing runtime’s site-packages read-only; keyring 25.7.0, SecretStorage 3.5.0, jeepney 0.9.0 and their dependencies were added only in that workspace. Exact versions are in `artifacts/0.2.3-evidence/build-runtime.json`. Native build/archive work used niceness 10 and numerical thread caps of two. No system packages, models, GPU work or inference were involved. Other Linux distributions and a native Windows runtime were not locally validated. The Windows ZIP/CI path remains compatible; its actual WinVault class was tested against fake Win32 credential APIs with explicit local-machine persistence. Real OS credential services were deliberately not probed or modified during verification; Linux use requires an available, unlocked desktop Secret Service.

Artifacts in `artifacts/`, with adjacent checksum files (integrity, not publisher authenticity):

```text
PlaguesBroadcastStation-0.2.3-linux-x86_64.run  (189485387 bytes)
3693fa4effcf7c35ac1aea819fcd339c0b8f37889b33480c7409fab2dbd3f077

PlaguesBroadcastStation-0.2.3-linux-x86_64-portable.tar.gz  (189461610 bytes)
5acdc1c22c6fe1fe47fea451a04be74d0a484ef3e122a19aacf67aed346c9bee

PlaguesBroadcastStation-0.2.3-source-31e6dd1.tar.gz  (3103946 bytes)
1052d036a724964cd9f3b9a975100edcaef8731dd8cf08f15fe75d77051eb64e
```

The exact-source model-free suite passed **258 tests and 36 subtests**, with **2 real recording/loopback-stream integration cases deliberately deselected**. Profile coverage uses fake credentials and temporary data: restart, explicit overwrite, absent/locked/error backends, readback/atomic-marker failure, interrupted save, safe partial Forget, corruption recovery, future-format/symlink refusal, short and encoded key redaction, current/prior target fragments, export/settings/lyric preservation, cross-process busy locking and literal-tilde directory isolation. Actual keyring WinVault primary/compound fallback semantics were exercised through fake native APIs; stale fallback credentials fail the revision check. Independent real Qt Task/eventloop checks confirmed responsive unlock waits, disabled mutation/Start controls, asynchronous restart restore without output, failure feedback and recovery. Those checks used an inert playback engine to isolate audio; the regular offscreen suite required approved execution outside the restricted Qt-audio sandbox.

Both final archives were read directly into disposable extraction paths, and their embedded executable SHA-256 is `bd81c401a4dd8c0edcc8f567591a93a8a3a5dfa13a8aa7a377167c2fb1930acb`. A recursive bytecode/constants/names/closure/exception-table comparison proves the packaged version, profile store, GUI and broadcaster modules match the reviewed source. It imports or executes no application/keyring code. The native PYZ and extension check confirms SecretService, SecretStorage, jeepney, cryptography and cffi are present and alternate/plaintext/chainer/null backends are absent. The superseded intermediate candidate and its receipts are separately labelled under `artifacts/0.2.3-evidence/superseded-7c7ac64`; they are not the final artifacts.

The actual completed **0.2.2 installer → final 0.2.3 → rollback to real 0.2.2 → return to 0.2.3** passed in disposable application/data folders with spaces. Every persisted file retained identical bytes and modification time: sessions, playlists, settings, model placeholders, artwork, manually approved lyrics, drafts, original QC, raw prior-JSON history and the opaque Broadcast profile marker. No real credentials were read. Fresh/repeated installation, stable version/status, explicit legacy desktop-fixture adoption with backup, damaged/truncated installer refusal, portable modes/symlinks/full manifest and its launcher passed. Installed operator, installed model-setup and portable operator windows each remained running offscreen for three seconds; only those own fixture processes were terminated. Bundled FFmpeg/ffprobe passed version checks, the copied extraction worker imported without Qt/models, and the native model installer refused execution without explicit installation authorization.

Fake-only Broadcast screenshots and control bounds at **1370×900** and **1030×720** are under `artifacts/0.2.3-evidence/`. Profile controls and save status fit both sizes; the remaining Broadcast output controls are available by scrolling at the smaller size. All prior artifact-root files, including the completed 0.2.2 installer, portable/source archives and receipts, retain their exact original hashes, sizes and modification times. The baseline is recorded in `prior-artifacts-preservation.json`.

Reproduction commands from the clean source:

```sh
pbs_python="$PWD/.venv/bin/python"
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_QPA_PLATFORM=offscreen \
  QT_MEDIA_BACKEND=ffmpeg LIBGL_ALWAYS_SOFTWARE=1 "$pbs_python" -m pytest -q \
  -k 'not real_ten_second_recording and not local_stream_refusal'
PYTHONDONTWRITEBYTECODE=1 "$pbs_python" scripts/build_linux_installer.py --record-source
PYTHONDONTWRITEBYTECODE=1 PYINSTALLER_CONFIG_DIR="$PWD/build/0.2.3-final-pyinstaller-cache" \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 nice -n 10 "$pbs_python" -m PyInstaller \
  --clean --noconfirm --distpath dist/0.2.3-final --workpath build/0.2.3-final-native PlaguesBroadcastStation.spec
cp run-linux.sh README.md INSTALLER.md THIRD_PARTY_NOTICES.md requirements-lyrics.txt dist/0.2.3-final/PlaguesBroadcastStation/
cp -R docs dist/0.2.3-final/PlaguesBroadcastStation/
chmod +x dist/0.2.3-final/PlaguesBroadcastStation/run-linux.sh
"$pbs_python" scripts/check_credential_bundle.py dist/0.2.3-final/PlaguesBroadcastStation/PlaguesBroadcastStation
PYTHONDONTWRITEBYTECODE=1 nice -n 10 "$pbs_python" scripts/build_linux_installer.py --bundle dist/0.2.3-final/PlaguesBroadcastStation
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg \
  LIBGL_ALWAYS_SOFTWARE=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 nice -n 10 "$pbs_python" \
  scripts/check_linux_packages.py --previous-installer "$PWD/artifacts/PlaguesBroadcastStation-0.2.2-linux-x86_64.run" \
  --report-dir "$PWD/artifacts/0.2.3-evidence"
```

Complete command details, final source/runtime receipts, tests/build/package logs, recursive code comparison reports/helper, independent reviewer scripts/output, native lifecycle report, hashes and source patches are retained under `artifacts/0.2.3-evidence/`. `source-changes-since-0.2.2.patch` describes the frozen native-source changes; `source-changes-from-upstream.patch` includes the full isolated update from upstream snapshot `7aeee7e`. **Creating 0.2.3 did not install it into the live environment, start a real stream, alter/relaunch the currently running app, modify real settings/keyring/shortcuts/cache, or push/publish source.**

## 0.2.2 build and verification, 22 October 2026

The final candidate was built from clean commit **`fcf40d1bd07ba3202feea47288eea67edc8099a1`** on `codex/lyrics-review-workflow`, originating from the completed 0.2.1 source `a9a5960476f55ef0138d7ec485bfb2d868c9d6fc`. Origin remains `https://github.com/kaotickj/plagues-broadcast-station.git`. The source receipt and native manifest agree on `source_dirty=false` and source-tree SHA-256 `71541e8d1949758c694100231af32ce1210dc74e87e162d35c49fa1f72c0d041`; this was independently recomputed from the committed files. This receipt was added afterward as documentation only. The exact build source is retained in Git and the source archive below.

Built/tested on **Debian 13.7 x86_64, glibc 2.41**, using the existing read-only Python 3.13.5 / PyInstaller 6.22.3 / PySide6 6.12.0 runtime. Native build and archive creation used CPU with niceness 10 and numerical-library thread caps of two. No dependencies, models or system packages were installed, and no inference or GPU work was performed. Other Linux distributions and Windows were not locally validated; the existing Windows ZIP/CI path remains available.

Artifacts and SHA-256 values:

```text
PlaguesBroadcastStation-0.2.2-linux-x86_64.run  (183149585 bytes)
8b288cc0dd0c95c557565a9ef27cdae799e0d38fd85dcbccd70a2169860ab74a

PlaguesBroadcastStation-0.2.2-linux-x86_64-portable.tar.gz  (183122904 bytes)
c21fcc762e23980ac86cf48a06da781f7fa1204e18b6dcc4db24ed43afc79709

PlaguesBroadcastStation-0.2.2-source-fcf40d1.tar.gz
bb077da0ee6d65cc8cc700330c23ec78917af9917c6746d2cc6ab487296911b4
```

All three artifacts and adjacent checksum files are under `artifacts/`. The installed release identity is `0.2.2-fcf40d1bd07b-e8ab62fb82d7`. Both native formats contain the same manifest and verify all **836 file/link entries**. `source-changes-since-0.2.1.patch` records this update; `source-changes.patch` includes the full installer/review change from upstream snapshot `7aeee7e`.

The frozen full suite passed: **222 tests and 36 subtests**. This includes the latest empty-draft, uncertain-word-evidence, strict approval, source/revision conflict, raw history, metadata, UI and installer-security tests. The actual completed 0.2.1 `.run` was installed into a disposable root, upgraded to this actual 0.2.2 `.run`, rolled back to the real 0.2.1 native release and returned to 0.2.2. Saved approvals remained valid for their exact recording/revision; approved lyrics, edited drafts, raw prior JSON history, sessions, settings, model placeholders, artwork and playlists retained identical content and modification times throughout.

Fresh/repeat installation, stable version/status, exact backed-up legacy-entry adoption, and rejection of a truncated copy of the actual installer passed. The portable archive retained executable modes, symlinks and the full manifest; its executable and launcher passed startup checks. Installed operator, installed model setup and portable operator windows each stayed running offscreen for three seconds. Only those own disposable GUI processes were terminated. Bundled FFmpeg/ffprobe passed; the copied standalone extraction worker imported without Qt/model initialization, and the native model installer refused execution without an explicit install action.

The candidate fixture screenshots in `artifacts/screenshots/` show review notes, editable lines and save/approval controls at **1370×900** and **1030×720**. They were visually inspected; `preview-checks.json` confirms the save, approval, reload and confirmation controls remain within both window sizes. The fixture screenshots and all tests used temporary data, with no production recording opened or played.

Commands used at the frozen source (the runtime was never changed):

```sh
pbs_python=/home/kaotickj/Documents/Codex/builds/goodneighbor-broadcast-player/.venv/bin/python
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 QT_QPA_PLATFORM=offscreen \
  QT_MEDIA_BACKEND=ffmpeg LIBGL_ALWAYS_SOFTWARE=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  nice -n 10 "$pbs_python" -m pytest -q
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg \
  LIBGL_ALWAYS_SOFTWARE=1 "$pbs_python" scripts/preview_lyrics_review.py --output-dir "$PWD/artifacts/screenshots"
PYTHONDONTWRITEBYTECODE=1 "$pbs_python" scripts/build_linux_installer.py --record-source
PYTHONDONTWRITEBYTECODE=1 PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache" \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 nice -n 10 "$pbs_python" -m PyInstaller --clean --noconfirm PlaguesBroadcastStation.spec
cp run-linux.sh README.md INSTALLER.md THIRD_PARTY_NOTICES.md requirements-lyrics.txt dist/PlaguesBroadcastStation/
cp -R docs dist/PlaguesBroadcastStation/
chmod +x dist/PlaguesBroadcastStation/run-linux.sh
PYTHONDONTWRITEBYTECODE=1 nice -n 10 "$pbs_python" scripts/build_linux_installer.py
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg LIBGL_ALWAYS_SOFTWARE=1 \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 nice -n 10 "$pbs_python" scripts/check_linux_packages.py \
  --previous-installer '../broadcast-installer/artifacts/PlaguesBroadcastStation-0.2.1-linux-x86_64.run'
```

Full logs, native checks, source receipt and preview checks are retained under `artifacts/`. Final source/artifact review found no blocking issue. The completed 0.2.1 workspace/artifacts are preserved. **Creating 0.2.2 did not install it, change or relaunch the currently running app, modify real shortcuts/data/cache, or push/publish any source.**

## Prior 0.2.1 validation (preserved separately)

The historical record below describes the completed 0.2.1 checkout/artifacts in `../broadcast-installer`. They remain unchanged. The 0.2.2 candidate is built and verified separately after its lyric-review workflow is frozen and reviewed.

Built and tested on **Debian GNU/Linux 13.7, x86_64, glibc 2.41**. Build Python was 3.13.5, PyInstaller 6.22.3 and PySide6 6.12.0. An already-installed, read-only build runtime was reused; no dependencies, models or system packages were downloaded/installed. The clean native build used CPU only, niceness 10, two-thread numerical-library caps and a cache inside this isolated checkout.

Exact build source: `b91408438f45dd47078e3730c4f26fb40ea3cda9`, on top of confirmed upstream snapshot `7aeee7e79e049db6017a6306f2e6882b6d1c8fb5`, with origin `https://github.com/kaotickj/plagues-broadcast-station.git`. The build receipt and package manifest both record `source_dirty=false` and source-tree SHA-256 `dbfc869f26ac3b90f27c378a51248d8147427e56850aa303ef28d737e0f7e57b`. This verification section was added afterward as documentation; the exact build snapshot is retained in Git and `artifacts/PlaguesBroadcastStation-0.2.1-source-b914084.tar.gz`.

Artifact: `artifacts/PlaguesBroadcastStation-0.2.1-linux-x86_64.run`, **183,098,012 bytes**. SHA-256:

```text
7b1b266877844c39935c2ce434d669f38c6cc884e369d7bfc543ed9fbbad3551
```

The selected release identity is `0.2.1-b91408438f45-6ce69fc886f7`. Its manifest verifies 834 file/link entries. The adjacent `.manifest.json` and `.run.sha256` preserve source identity and package integrity details. Source changes can be reviewed with `git diff 7aeee7e b914084`; `artifacts/source-changes.patch` also includes this evidence document. Changes cover Linux installer/build scripts, focused tests, Linux CI packaging, minimal `0.2.1`/`--version` identity and documentation. Windows retains its ZIP flow; its only build-script addition copies this documentation file. No Windows installer or locally tested Windows build is claimed.

Commands used from this isolated checkout (the runtime below was read, never changed):

```sh
pbs_python=/home/kaotickj/Documents/Codex/builds/goodneighbor-broadcast-player/.venv/bin/python
PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen QT_MEDIA_BACKEND=ffmpeg \
  "$pbs_python" -m pytest -q tests/test_linux_installer.py tests/test_packaging.py
PYTHONDONTWRITEBYTECODE=1 "$pbs_python" scripts/build_linux_installer.py --record-source
PYTHONDONTWRITEBYTECODE=1 PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache" \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 nice -n 10 \
  "$pbs_python" -m PyInstaller --clean --noconfirm PlaguesBroadcastStation.spec
cp run-linux.sh README.md INSTALLER.md THIRD_PARTY_NOTICES.md requirements-lyrics.txt dist/PlaguesBroadcastStation/
cp -R docs dist/PlaguesBroadcastStation/
chmod +x dist/PlaguesBroadcastStation/run-linux.sh
PYTHONDONTWRITEBYTECODE=1 nice -n 10 "$pbs_python" scripts/build_linux_installer.py
PYTHONDONTWRITEBYTECODE=1 nice -n 10 "$pbs_python" artifacts/check_native_installer.py
```

Results: **26 focused tests passed, plus 9 subtests**. The actual executable `.run` passed fresh installation, repeat installation, exact stable version/status, legacy-entry adoption with backup and preserved icon/working directory, upgrade over a deliberately identified previous-executable fixture, rollback and return to the native package. The fixture is not represented as a historical released binary. Representative saved session, playlist, settings, artwork, model placeholders, lyrics and review-history files kept identical content and modification times. A truncated copy of the actual `.run` was rejected before activation and kept the selected release/data. Direct `.run --help`, desktop-file validation, all installed native-file checksums and bundled FFmpeg/ffprobe startup passed. The packaged operator and model-setup windows each remained running offscreen for three seconds; only those own disposable processes were terminated. No model-install action was invoked.

Evidence is retained under `artifacts/`: `installer-tests.log`, `native-build.log`, `linux-build-source.json`, `native-installer-checks.json`, `native-installer-checks.log` and the repeatable `check_native_installer.py`. No live installation, real shortcut, existing extracted build or production data directory was modified by this work. Nothing was pushed, published or installed for the user.
