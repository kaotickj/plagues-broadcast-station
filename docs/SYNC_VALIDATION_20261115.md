# Development source synchronization — 2026-11-15

Validated source: `cf513300e25b0619cbe0533e98c63c9593be06a2` on
`codex/broadcast-stable-rc`. This records development-source verification,
not a release, installation, merge into main, or live broadcast qualification.

The eight outgoing commits preserve explicit lyric saves and human revisions,
recording identity, draft preview preferences, shared-model discovery, useful
failed-extraction drafts, output-start feedback, and redacted diagnostics.

An isolated archive of the committed source passed **458 tests and 64 subtests**
with Python 3.13, offscreen Qt and the project's existing PySide6 test runtime.
The old runtime lacked the declared `keyring` dependency; version 25.7.0 and its
dependencies were installed only in a disposable test directory. All tests then
passed. Settings/data/cache and pytest temporary files were isolated; a null
keyring backend and nonexistent PulseAudio server avoided personal credentials
and live audio output. Sandbox-denied PulseAudio wakeups required the isolated
suite to run outside that socket restriction. No live application was restarted.

No model downloads, GPU inference, paid provider requests, packaged rebuilds,
real streaming destinations, or live audio quality tests were performed.
Credential-pattern scanning and outgoing-path review found no private keys,
API tokens, generated media, or private profiles in the outgoing source changes.
Other development worktrees have differing source and remain separate.

The synchronization commit uses `[skip ci]`. The repository workflow uses push
and pull-request events, for which GitHub documents commit-message skipping:
https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs
Required skipped checks may remain pending; this is not permission to merge or
release without the appropriate later validation.
