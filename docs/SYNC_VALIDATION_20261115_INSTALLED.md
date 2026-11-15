# Installed-development source sync — November 15, 2026

This checkpoint preserves the recovered 0.2.4rc11.dev13 development source,
then carries over the installed model-settings persistence, whole-studio
settings backup/import, and optional AMD ROCm FP32 review changes. Its existing
branch lineage is codex/broadcast-install-readiness. It does not replace main,
change a running installation, or qualify a stable release.

## Verified scope

- 199 source tests passed, with two skips, using synthetic temporary data and
  offscreen Qt. Coverage includes persistence/conflict handling, backup
  validation/rollback/credential exclusion, actual isolated settings UI,
  GPU selection and immutable request routing, and review contracts.
- Seven packaging tests also passed after updating the standalone-import test
  fixture for review_partial and including that helper in package data.
- New regression tests were adapted from the installed-overlay tests; only
  portable import/source locations changed. The standalone GPU selector is
  included in both PyInstaller and package-data lists.
- 44 candidate Python modules matched installed embedded code. Two older
  modules (review_backend and model_install_worker) differed; standalone-only
  modules cannot be qualified by embedded comparisons. This is source
  preservation, not a claim that every module reproduces the installed binary.
- Prior installed-overlay evidence records 28 packaged tests passing and
  bounded real CPU/GPU recognition/alignment parity on one AMD RX 7600 XT.
  On a 30-second fixture, recognition was 11.184 seconds GPU versus 15.773
  seconds CPU; short jobs were slower on GPU. Peak observed hotspot was 89 C.
  These earlier observations are not a full controller/job acceptance test.

## Remaining qualification

The broader legacy regression run had failures and stalled in a GUI test; it
was stopped without touching the user application. A first-failure rerun passed
51 tests and 28 subtests, then failed test_app's expectation that save_session
creates settings.json. The installed persistence implementation saves session
separately; other legacy assumptions and recovered variants need deliberate
reconciliation. This checkpoint does not claim a passing full suite.

A new complete installer build, Windows qualification, full-track/end-to-end
GPU review, and further real-use acceptance remain outstanding. CPU stays the
default; GPU requires the separately installed/configured runtime. No runtime,
model, driver, GPU limit, live setting or app process was changed by this sync.

Private owner lyric fixtures and three dependent local-only test files were
excluded, as were settings backups, recordings, model/runtime binaries,
production data, generated receipts and recovery Git pointers. Source, tests
and explanatory documentation only are included. The commit uses [skip ci];
no CI run or release is requested.
