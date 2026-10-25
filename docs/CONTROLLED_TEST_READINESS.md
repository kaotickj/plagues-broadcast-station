# Development candidate and controlled-test readiness

This candidate is **0.2.4rc11.dev1**, a development test build, not a stable release. Source `__version__`, project metadata, frozen `--version`, Help and the package manifest must agree. Its release ID binds the version, exact clean source commit and bundle hash. A metadata-only relabel is refused. Approval to install/activate and the affected apps' closed state must be established by the operator; command flags attest those facts and do not create authorization or stop processes.

The packaged `_internal/readiness_tools` folder contains standard-library operational tools. They are never run by normal player startup. Their default planning/verification actions do not activate code, launch the GUI, access a keyring, change station data or delete releases. Use a supported external Python on Linux. No network, package/model download or production interruption is part of readiness.

## Scoped RC10 hotfix verification

`rc10_hotfix.py BUNDLE` verifies only the pinned RC10 release, original manifest and executable hashes plus the exact approved `lyrics_review.py` replacement. The shipped policy is itself hash-pinned. Full file inventory, modes and directory layout must match; unknown content, modes, files, links or directories are refused. The generic installer's strict `verify_release` is unchanged and still refuses a manually patched original manifest.

On successful explicit audit capture, original-manifest.json is preserved byte-for-byte, patched-manifest.json records the separately derived inventory, and receipt.json is published last. None is written into the retained release. Interrupted or conflicting evidence capture does not publish a false success. The derived manifest is audit evidence, not permission to rewrite the original release manifest.

The observed real installations currently contain extra original-code hotfix backups; the radio copy also has four `.pyc` caches. The caches code-compare to pinned source and the backups match original code, but they remain outside the patch-only inventory policy. **Live verification therefore refuses them.** No exception was broadened, backup/cache moved or deleted, or successful live receipt issued. Root must disposition these preserved extras before claiming live rollback readiness. The observations and exact hashes are in the external task evidence, not embedded as a new allowance.

## Code selection, interruption and rollback

`controlled_activation.py` supports read-only `plan-activate`, `plan-rollback` and `verify`. Mutation is separated into `prepare`, `apply` and `recover`, each requiring explicit `--approved --apps-closed`. A staged candidate needs a clean development/RC identity tied to its source and content; the retained RC10 must pass the scoped verifier. Preparation performs a bounded no-GUI `--version` probe and binds its output, binary, manifests, full mode/directory identity, prior/next install metadata and hotfix evidence into the audit transaction.

Apply runs under the installer lock and requires exact prior install.json bytes. It re-verifies both code selections before atomic pointer replacement and journals before/after phases. Interrupted writes can be classified as the known prior or next selection. Recovery finalizes only matching recorded states; an unknown external selection is preserved and refused. A code-only rollback changes selection metadata while keeping each station's data-directory string unchanged. It does not restore old data or alter launchers, lyrics, models or native credentials.

This tool does not stage/copy the package or automatically snapshot userdata. The operator must complete the separately approved closed-app snapshots first. Retain RC10, its manual files/original manifest and RC9 folders; no cleanup is implied. The standard one-command rollback remains unsuitable for an unmodified original manifest with a manual patch; use only the reviewed explicit route after all real inventory differences are reconciled.

## Private station snapshots and deliberate restore

`station_snapshot.py` inventories only explicitly selected known settings/session/profile-reference files and lyric/history/reference/draft/failure trees. Two stations retain independent absolute data mappings. Opaque bytes, permissions and timestamps are recorded, with private backup files and a complete snapshot manifest published last. Explicit external linked-lyric files can be nominated; arbitrary traversal, links, nonregular files and unknown scopes are refused. Model/runtime/media folders and the native credential store are excluded; no large-model copy is performed. Persistent lock placeholders are narrowly recognized and preserved rather than treated as lyric content.

Planning is read-only but hashes the selected files. Capture requires approval and closed writers, preflights copy space and enforces a 20 GiB reserve. Supported payload limits are bounded; an oversized station selection must be reviewed rather than silently truncated. Corrupt/incomplete snapshots are not accepted.

Restore is a separate operation, not an automatic part of code rollback. It needs an explicitly reviewed exact current inventory and the snapshot manifest hash. The returned restore intent has a caller-held hash; retain that pin outside its journal. Every target is compared before replacement. Newer human lyric/approval/settings changes are refused, not overwritten. An interrupted restore retains before data and a journal. Recovery requires the original caller-held manifest/intent pins, validates the entire selected current state and journal mappings, and reverses only known committed changes. Forged journals, unknown edits/new files and changed before data are refused before further write or deletion. Do not restore an old snapshot blindly over new user corrections.

## Operational gates

1. Root receives Jay's approval naming the exact development artifact and target station(s); save pending user edits and arrange user-controlled closure. Do not stop a live stream or signal production.
2. Recheck available storage, code identity and all preserved extras. Resolve actual scoped-verification refusals without changing the generic verifier. Preserve original/derived receipts separately.
3. Privately capture the approved mutable state after closure. Keep the snapshot and restore-intent pins in the root-controlled record; no actual key values or native vault database are exported.
4. Stage and independently verify code under a new identity, prepare the exact code-selection audit, then apply only the approved target. Test dummy state before any real-state launch.
5. Launching with real station data can automatically read remembered native credentials. That remains a separate authorization/OS-permission gate. Recovery UI/software tests do not prove real ASR quality, native audio, streaming or keyring acceptance.

This implementation is qualified with disposable fixtures for both stations, interrupted activation/rollback/restore and failure paths. Fixture success does not override a real filesystem mismatch or installation approval gate.
