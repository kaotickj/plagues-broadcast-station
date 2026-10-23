# Local release candidate acceptance

This candidate combines the reviewed 0.2.3 workflow (`e71725a`), waveform color
change (`24cc6bf`, locally replayed as `42d140b`), and preserved phase-scoped GPU
patches. Original checkouts, their uncommitted work, installed binaries, launchers,
lyrics and media remain outside this candidate and are not modified by the work.

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

The combined source passed 312 tests and 36 subtests on the local Linux host.
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

Native package receipts, exact artifact hashes and rollback details accompany
the candidate separately. Linux-host validation does not certify Windows or
cross-distribution compatibility. No real livestream or production credentials
are used by the acceptance checks.
