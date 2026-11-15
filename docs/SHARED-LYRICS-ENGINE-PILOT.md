# Shared local lyrics engine: pilot contract

This is an isolated development candidate, not an installed build or a website
deployment. No default recognizer changes are supported by the current comparison.

## Reuse the working pieces

`review_worker.py` already owns bounded, cancellable, offline execution and the
shared `/run/lock/gnb-ai-gpu.lock` protocol. `review_backend.py` contains Heart and
CTC adapters. `extraction_backend/lyrics_pipeline.py` already provides CPU vocal
separation and Qwen adapters; it is a legacy workflow with different defaults,
not an interchangeable invocation of the Heart review path. `review_capture.py`
preserves raw outputs. `review_core.py`, `lyric_review.py`, and `lyrics.py` retain
draft history, source identity, explicit review, grouped spans and highlight
intervals. Website and Studio consumers should eventually consume the same
candidate schema through thin adapters; they must keep separate publication
destinations and explicit authorization.

The new stdlib-only `alignment_contract.py` can be imported by either consumer.
It accepts supplied text and explicit vocal windows, records where those windows
came from, checks all words—including repeated choruses and fictional names—are
preserved in order, and validates returned native spans. It does not detect vocals.
The candidate worker/backend use it for supplied-text alignment. Without windows,
the existing full-audio behavior remains unchanged. The standalone-worker package
manifests include its source dependency. No UI for selecting windows has been added.

Before a supplied-text result becomes `candidate_ready`, the controller recomputes
coverage from the actual native word labels. Missing, changed or reordered words
stop candidate creation while retaining raw evidence. Explicitly held passages
retain their original region IDs and unresolved holds; only those passages may
lack aligned words. Native support intervals must remain ordered inside their
word/window. These checks preserve text and geometry; they do not prove accuracy.

## Two paths, one reviewable result

1. **Known lyrics:** keep the original supplied text and provenance. Align a sung
   copy, excluding bracketed directions. Explicit windows map phrases to vocal
   passages without forcing them through an instrumental break. Automatically
   proposed windows remain unverified. Never silently rewrite supplied words.
2. **Unknown lyrics:** produce a raw transcript, record the actual model/settings
   and input variant, and expose uncertainty. A reviewer can correct names or
   other words before alignment. Repetition warnings are not proof of an
   instrumental, and absence of a warning is not proof of correct words.

Both paths produce an unapproved candidate tied to the exact audio digest and
document revision. Model disagreement may guide listening but is not a vote that
can approve or rewrite lyrics. An explicit instrumental designation bypasses
generation. Unknown tracks need an independently evaluated vocal-presence method;
neither an empty ASR result nor a low-energy separator stem establishes that.

## Timing semantics

All window coordinates are relative to the decoded input supplied to that aligner.
For a real crop, `restore_source_timeline()` adds the crop offset exactly once,
preserving native/grouped support intervals and deliberate disabled highlights.
It records the native clip span and rejects double restoration. This is coordinate
conversion, not shifting words to a desired onset. Invalid or missing spans remain
errors or explicit coverage issues; they are not interpolated into apparent truth.

CTC frame placement is an acoustic estimate. Successful geometry/coverage validation
does not prove vocal onset or timing accuracy. The known full-intro Murkwater error
passes geometric validation, and a regression test specifically preserves that
distinction. Current Qwen forced-aligner code repairs nonmonotonic predictions,
sometimes by interpolation; the prepared adapter saves raw predictions before
that repair. All three planned alignment comparisons are now complete.

On Murkwater's original 0–30-second audio, Qwen3-ForcedAligner-0.6B placed `Told`
at 14.24 seconds using the supplied owner-corrected 20-word transcript. It received
no onset estimate, vocabulary context or vocal crop. The first raw timestamp was
14240 ms before owner review and was unchanged by the postprocessor. The owner
accepted that onset, superseding the prior 14.22 estimate for this word only.
This tests forced alignment, not recognition of unknown lyrics.

Qwen changed four later timestamp boundaries and produced a zero-duration `own`.
The complete result is therefore rejected as a usable timed candidate. Full-intro
CTC reproduced the wrong 2.2625-second opening; CTC with the previously reviewed
manual 12.5-second crop reproduced all 20 prior accepted spans. Differences from
those listening-accepted spans are not independent annotation error scores.

## Evidence so far

One owner-reviewed 17-second Murkwater passage has 26 reference words. Strict
case/punctuation-normalized WER: Heart original 8/26 errors; Heart separated 9/26;
Qwen0.6B original 8/26; Qwen with explicit Murkwater/Mirelurk context 7/26. Only
Murkwater improved exactly in the context condition. These are not full-song or
population estimates. The reference includes prior owner corrections and cannot
be presented as an automatic successful extraction.

On the owner-confirmed 30-second Saxophone instrumental, Heart emitted 51 `oh`
tokens, separated Heart 46 `na` tokens, and Qwen emitted `You.` The existing
repetition guard holds the two Heart results but misses the short Qwen result.
This does not justify a phrase blacklist or an automatic Qwen switch.

Google's [Gemma audio example](https://ai.google.dev/gemma/docs/capabilities/audio)
documents local ASR, a 30-second limit, newer Transformers and a roughly 10 GB E2B
example download. Those artifacts are not installed here, so no Google model was
run. The research handoff found no turnkey Google supplied-lyrics forced aligner;
LAST and CTC forward-loss utilities are algorithmic building blocks, not a reason
to replace the current stack with an unvalidated JAX rewrite. No paid services,
inference APIs, external audio uploads, training or model downloads are involved.

## Evidence required before rollout

Both stages are complete: six calls / 142.251 seconds for the original/separated
Heart comparison, and six calls / 48.099 seconds for Qwen ASR/context plus the
three alignments. All used CPU FP32, six threads, one job and the existing shared
lock; no GPU inference or default-engine switch occurred.

A next bounded proposal is to test an uncertain Qwen onset/window as guidance
for CTC, retaining a lead-in margin and supplied text. It has not been run, and
must not turn a model estimate or this one accepted anchor into an assumed truth
for other songs. Audition sustained syllables and gaps, not only the first word.
Evaluate an ordinary/quiet-vocal positive and a held-out instrumental
before proposing rejection thresholds. Green Glow/Nuka-Cola require trustworthy
reference excerpts; Payday needs a listening-confirmed onset. Preserve accepted
Murkwater/Mole Rats/Whiskey and distinct MacCready/Goodbye identities throughout.

The contract/unit/controller regressions use synthetic or saved data, not model
inference. The latest suite has 65 passing checks. Preview logic additionally
passes 119 saved-data JavaScript assertions. Multimedia tests stall constructing
`QAudioOutput` after `QMediaPlayer` initializes; PipeWire/PulseAudio report sandbox
permission errors. No permission bypass was attempted. Packaged multimedia and
owner listening of the later Qwen boundaries remain unverified.
Rollout needs the complete candidate test results, compatible source/publication
formats, backups, a visible build, and an explicit installation/publication scope.
