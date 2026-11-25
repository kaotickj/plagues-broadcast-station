# Local release candidate acceptance

This candidate combines the reviewed 0.2.3 workflow (`e71725a`), waveform color
change (`24cc6bf`, locally replayed as `42d140b`), and preserved phase-scoped GPU
patches. Original checkouts, their uncommitted work, installed binaries, launchers,
lyrics and media remain outside this candidate and are not modified by the work.

## 0.2.4rc5 model settings layout follow-up

The top discovery summary stays concise so the first model fields remain visible
at the minimum supported window size. Detailed resolution notes remain available
in its tooltip and the diagnostics below the fields. Model routing is unchanged
from RC4. The final native package must identify this follow-up source, rather
than reuse the earlier RC4 executable.

## 0.2.4rc4 existing model discovery follow-up

The player checks known existing shared Linux model/runtime locations for unset
or recognized legacy-default fields and keeps the portable user-local fallback.
Valid custom locations are retained; missing intentional custom locations remain
visible with diagnostics. Explicit path edits are recorded per station. Individual
component overrides can remain empty: the GUI shows their effective automatic
locations. Discovery reads file metadata, never downloads, copies or loads models.
See [model locations](MODEL_LOCATIONS.md). This package must be rebuilt from its
own source identity; the earlier RC3 artifact does not include this change.

## 0.2.4rc3 preview preference follow-up

Unapproved saved lyric preview defaults on only for a station without a saved
choice. The last explicit on/off choice survives session/playlist saves, restart,
track selection and imported/prepared results. Explicit false values are retained;
legacy playlists without the preference keep the current choice. Lyrics-ready-only
eligibility, recording checks, saved/unsaved boundaries and approval remain separate.
This follow-up is prepared locally; it does not alter an active installed window.

## 0.2.4rc2 Save follow-up

Save lyrics updates the selected recording without a path dialog; Export lyrics…
creates a separate copy. Imports retain an original provenance snapshot, edited
drafts cannot inherit old publication approval, and extraction refuses to replace
a target changed during processing. See [storage and Save](LYRICS_STORAGE_AND_SAVE.md).
New local package/test receipts identify the exact follow-up commit; historical
0.2.4rc1 results below do not establish this follow-up by themselves.

## Required acceptance

- Editable recording-bound lyrics with a visible save action and active-cell
  commit through both mouse and keyboard; export to a separate file.
- Saved text and recording references survive a fresh application launch.
- Cancel, selection changes, background results, failed writes and close prompts
  preserve unsaved work; revision/history and recording checks remain effective.
- A resizable editing workspace preserves content, focus and selection; controls
  remain reachable at the supported minimum size.
- Transcription settings avoid unintended metadata prompts, preserve raw output
  and decoder/chunk provenance, flag suspect boundary text without deleting
  genuine lyrics, and form readable lines from token output.
- Waveform color, Plague Radio profiles, GPU phase scheduling, playback/output,
  model discovery, protected profiles, installer integrity and rollback retain
  their existing behavior through relevant regression tests.
- Native package starts and its actual lyrics workflow saves/reopens using only
  disposable fixtures. Package upgrade/rollback leaves fixture data unchanged.
- Artifacts have exact source/build identity and checksums. Windows and real
  transcription accuracy are reported separately if not exercised locally.

## Confirmed pre-candidate issues

The desktop/menu shortcuts target the local 0.2.1 editor build, but XFCE panel
launcher 13 invokes `launch-plague-radio.sh`, which still selects the older
`broadcast-model-setup/dist/plague-radio-20261022` executable. Inspection of its
embedded Python archive finds no editor module or edit/save handlers, and its
transcript is read-only. The 0.2.1 editor's mouse/keyboard Save path works in an
isolated Qt fixture; this does not establish which window the user had open.

The older 0.2.3 branch already contains the guarded inline editor and managed
installer, but it lacks the later waveform/GPU changes. The candidate preserves
both development lines instead of replacing either original checkout.

## Installation boundary

No production installation or shortcut migration is performed during candidate
preparation. Approval must select the data directory and cover the known XFCE
panel shortcut as well as the desktop/menu entries. Existing station-specific
sessions must remain separate. Keep the previous executable and exact launcher
backups available; executable rollback must not revert lyric or media data.

## Source verification

The earlier 0.2.4rc1 combined source passed 312 tests and 36 subtests on the local Linux host.
Independent native Qt acceptance passed 36 workflow checks with disposable data.
A muted synthetic playback/local-recording check produced valid H.264/AAC output.
The first full run's isolated GPU-mock environment failure was corrected without
changing production admission behavior.

The reported transcript exactly matches the existing Radioactive Rain recording
and stored draft. That draft used Vulkan large-v3 and still has QC version 1.
High scores did not verify the opening words, and token grouping fragmented sung
lines. New preparation retains raw evidence and flags uncertainty. A separate
CPU medium.en pass over the opening/ending disagreed with the saved wording;
its words are review material, not verified corrections. Full-song recognition
accuracy and audible ground truth remain unverified. After the existing automated
lyrics job finished naturally under a supported pause, two bounded Vulkan checks
also completed. Their words disagreed with the independent CPU pass; every raw
result remains review evidence. The existing GPU lock was honored throughout.

The first Windows CI run exposed a real cache-freshness defect: Windows creation
time cannot substitute for content-change time. The candidate now uses native
`FILE_BASIC_INFO.ChangeTime`, with conservative content hashing when unavailable,
for both recordings and stored lyric files. The original mutation/revocation
assertion remains in the suite. A path-dependent test mock was made portable.
CI checks out the exact PR head so artifact provenance identifies that commit.
The recording integration fixture waits for the encoder's ready signal before
timing ten seconds; its duration, synchronization and real-tone assertions remain
unchanged, excluding variable input-handshake latency from the measurement.

Native package receipts, exact artifact hashes and rollback details accompany
the candidate separately. Linux-host validation does not certify Windows or
cross-distribution compatibility. No real livestream or production credentials
are used by the acceptance checks.
