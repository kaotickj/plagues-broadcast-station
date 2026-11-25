# Review a failed recognition draft

When automatic extraction reports failure, **Lyrics → Review recognition draft** can open a separate window for that recording's retained text. The action also reopens a previously saved recognition draft after restarting the same station. An error message alone is never treated as a file path; the app uses structured worker metadata or the recording's exact managed draft location.

The window labels the text **untimed and review required**. Edit the draft, choose **Save draft text** or press **Ctrl+S**, and read the saved/unsaved status. No filename picker is needed. **Copy draft text** copies the current text, including unsaved corrections. The original recognition and raw failure details remain read-only in their own tabs; later failed attempts do not replace the original evidence or an existing human draft.

**Reload saved** asks before discarding unsaved text. **Close** and Escape offer Save, Discard or Cancel for a dirty draft. Cancel keeps the window and buffer. A failed save or conflicting revision keeps the current buffer; copy it if needed, then deliberately reload the latest saved revision before resolving the conflict. Closing the child leaves the station and playback state open. Closing the station also respects a recovery window's Cancel decision.

The recording's bytes must still match the SHA-256 shown in the window, and the original recording must be selected before saving or reloading. A selection change or changed file blocks stale operations. A late background result cannot replace another recording's editor. Keep the buffer and select the matching recording again rather than guessing from titles.

## Separate from timed lyrics

Save draft text writes only `lyrics-cache/recognition-drafts/<recording SHA256>.json`, using the revision opened by the editor. This schema is `recognition-draft-1`, `status=unaligned`, `needs_review=true`. It has no timed segments, approval or publication. The saved draft does not enter the timed editor, saved lyric revision history, lyrics-ready filtering, portable timed-lyric export or Program Output. It does not align the words or prove that recognition improved. This window has no automatic alignment or approval action.

Existing timed lyrics, human corrections, approval state, model choices and source recordings remain unchanged. A retry can retain a new immutable raw failure report while preserving previously saved human recovery text. Any eventual alignment must be an explicit separate workflow with normal timing, recording-identity and human-review checks.

## Platform and evidence limits

Editable recovery storage currently supports Linux's protected directory-relative file operations. On an unsupported platform, or when no editable draft was retained, structured failure diagnostics can open read-only with Save disabled. An unreadable or invalid draft is preserved. The original raw report is bound to the recording and its stored digest; a changed/missing report is reported rather than silently substituted.

This UI does not install models, change GPU/separation preferences, start recognition, download anything, stream, access credential storage or publish lyrics. Actual sung-passage ASR accuracy must be evaluated independently; wiring the recovery editor does not establish it.
