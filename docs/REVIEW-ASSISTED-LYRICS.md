# Review-assisted lyrics — 0.2.4rc11.dev6

Select one recording in the Library, pause Program playback/stop output, open Lyrics → **Review-assisted lyrics…**. This is a separate private listening player; it never starts the program, recording or stream. Playback begins only when you click Play.

1. Embedded tags are checked first and retained verbatim as evidence. Generated/old tags are not treated as trustworthy. Edit supplied text and choose **Align supplied text**, or explicitly choose audio transcription instead. Bracketed directions are omitted only from alignment input.
2. **Extract Heart draft** uses installed HeartTranscriptor on CPU, then the installed wav2vec2 CTC aligner. Jobs use six CPU threads, one model process at a time, existing shared coordination, a 10 GiB worker memory cap, at least 12 GiB free at precheck, and an eight-minute total deadline. Recordings over 15 minutes are rejected. No downloads or automatic retries. Cancel/Escape/window-close terminates the worker and its child.
3. Review the audio-derived playback clock, active word, gap display and flagged-passage jump list. Seeking pauses and updates the highlight immediately. The clock is milliseconds from Qt's media playback position, not a claim of sample-accurate audio hardware timing. Flags are hints; unflagged words may still be wrong. Heart decoder regions are estimates, not automatic vocal detection.
4. Select a phrase and apply corrections. Same-count replacements retain word anchors; changed counts preserve the old supported spans as a grouped phrase. Internal new word boundaries remain unknown. Empty phrase text removes that phrase. The Timing tab edits a selected word/group or the phrase’s outer edges. Interior anchors and gaps stay intact. Use an explicit scoped offset to move the whole selected item, selected phrases, or entire draft together. Invalid overlaps and out-of-range times are rejected.
5. **Save private draft** keeps drafts separate from output. Check the listening confirmation and **Approve & use in library** only after review; this uses the app's recording/revision-bound approval and reversible lyric cache. Existing files go to history. It does not publish to a website or start playback/output.

**Instrumentals:** Check “I confirm this recording is instrumental” only when known. This bypasses all model generation and removes displayed words in that private candidate. The earlier text remains as evidence, and unchecking restores it. Empty ASR output never automatically classifies or approves a track. Heart has hallucinated repeated “oh” on known instrumentals.

Private drafts/evidence: `<app data>/lyrics-review/<audio SHA256>/`. Approved outputs use the existing `<app data>/lyrics-cache/lyrics/<audio SHA256>.json`. Both private and approved writes preserve history and reject concurrent changes. Importing an approved file still requires explicit approval of the imported copy. Original media/tags remain untouched.

This Linux build discovers the already-tested local Heart/CTC runtimes and models. Optional `<app data>/lyrics-review-runtime.json` can override `heart_python`, `align_python`, `heart_model`, `ctc_model`, `shared_lock`, and `timeout_seconds` (never above 480). Missing resources or permissions stop before inference; no permission/device changes are attempted. Existing legacy extraction settings and workflows remain available.

The first release was validated with saved evidence and silent UI fixtures. No fresh model inference was needed for integration. The previously accepted Murkwater/Whiskey recordings are references, not proof of automatic accuracy on new tracks. The Bartender 18.57-second fixture is accepted only for the reviewed 0–60-second sample.

## Manual timing controls

Select a phrase row, then use **Timing** (shown by default). Its target menu selects the whole phrase or one timed word/group. Enter Start/End and click **Apply start / end**. Phrase bounds adjust only the first/last word edges; they do not stretch interior timings. Unchanged anchors retain their original precision despite the three-decimal display.

**Start at playback** and **End at playback** use the current media clock immediately. **Earlier −0.050 s** / **Later +0.050 s**, and the numeric **Apply offset**, move the explicitly selected scope. Positive offsets mean later. Use Ctrl/Shift in the phrase table to select several phrases. **Seek to selection** auditions the vicinity without starting playback; the top Seek buttons only move the listening position.

Edits refresh the preview immediately, require review again, and remain private until Save/Approve. **Undo edit** / **Redo edit** work across timing and text corrections. **Reset timing to opened candidate** restores the initially opened timing while preserving text where the phrase/word structure still matches; if grouping changed, use Undo or Restore saved draft. Undo/reset never silently restore approval. Saved drafts keep byte-exact prior versions.

Offsets translate each selected highlight interval, retaining internal holes and disabled words. Explicit owner-confirmed instrumental exclusions remain anchored to the original audio clock. Bounds edits trim/extend only outer supported edges; they do not fill grouped internal gaps or invent constituent word boundaries. Native spans and original reference data remain historical provenance, while edited active timings are marked for review.

## Current draft, replacement and held passages (dev5)

**Realign current draft** reads the phrase table including applied text edits. The separate supplied-text box is used only by **Align supplied text**. Both replace the whole timing candidate; when timed lyrics already exist, explicitly check **Replace ALL existing timing** first. Ordinary text corrections and manual timing controls never launch realignment. Before model work the prior draft is saved to history. Results cannot replace a draft changed since job start or a saved file changed by another review. New results never inherit listening approval.

Extreme consecutive repetition (at least four copies, at least 20 tokens, covering at least 80% of one decoder region) is held before CTC alignment. This is a text diagnostic, not vocal/instrumental classification or an accuracy score. It may flag intentional singing and may miss other hallucinations. No phrase blacklist or CTC confidence cutoff is used. Raw text and tokens remain in the run evidence. Held regions have no automatic word timestamps/highlights and unresolved holds block approval.

Use the held passage jump to listen, then **I listened: exclude this text** only if it is unsupported. The exclusion remains recorded with raw text. To retain or correct it, **Copy complete text for correction**, edit the supplied text, and explicitly choose whole-draft supplied-text alignment. If the repetition is actually sung, check the intentional-repetition option for that supplied-text job; the resulting timings still require review. Empty or entirely held text is never automatically approved as instrumental. **Check saved extraction for repetition** applies the same review treatment to old candidates without another model call; it is undoable, and nothing is saved without Save/Approve.

Lines remain visible between adjacent word spans while the underline turns off in those gaps. Explicit holes inside grouped word spans, disabled words, and owner-confirmed instrumental exclusions retain their hiding semantics. The same logic drives private review and recorded/Program frames.

## Output publication gate (dev6)

The private player previews drafts; Program Output and every recorded frame use
only the current saved, locally approved library artifact for the exact audio
and lyric revision. Unresolved holds cannot be approved through either editor.
A separate private draft preserves the existing approved library copy until
explicit approval replaces it. Editing the main library revision clears its
output until approval again. Source replacement, file deletion, approval
revocation or track change clears captions. The legacy draft-preview preference
cannot bypass this gate. Local approval does not publish to the website.
