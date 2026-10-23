# Review, edit, save and approve lyrics

Open **Lyrics** and select a recording. The recording name above the editor
identifies the lyrics you are changing, even while another recording plays.
The operator preview becomes smaller on this tab so the notes and editor fit;
the separate Program Output keeps its normal size.

1. Read **Review notes**. Every stored reason and model warning is available
   in this scrollable panel. A timestamp and current line excerpt appear only
   when the saved reason contains that timestamp. Summary-only results say
   that no passage timestamps were stored. Original automatic findings remain
   available after corrections or human approval.
2. Double-click a line's text, start or end cell, or select it and press **F2**.
   **Tab** moves between cells. Times accept seconds or `m:ss`. Use **Add line**
   or **Remove line** for omissions or recognition artifacts.
3. Choose **Save draft / Save changes**, or press **Ctrl+S**. Wait for **Saved**.
   **Not saved** means the prior file remains and your edits are still in the
   editor. Changing recordings keeps unsaved buffers separate; closing with
   unsaved changes offers Save, Discard or Cancel. Saved drafts restore from
   the recording's lyric file and saved session.
4. Listen to the recording and check its saved words and line times. Check
   **I checked these lyrics against this recording**, then choose
   **Approve for airing**. A successful approval says **Saved · Reviewed and
   approved** and changes the row to **LYRICS READY**.

Saving and approving are separate actions. Extraction, alignment, import and
passing automatic checks create unapproved drafts. Existing files without a
valid human approval also show **LYRICS NEED REVIEW**; the app does not assume
that earlier automatic results were reviewed. Approval belongs to the exact
recording fingerprint and document revision. Editing, replacing the recording,
importing, or preparing another revision cannot carry that approval forward.
Automatic findings are evidence to review, not something that Save erases.

An edit keeps a line's measured start/end times. Unchanged lines keep their
word times. Changing a line's text or times removes that line's old word
highlights, so they cannot be attached to different words. To retain a line
with questionable word timing, select it and choose **Line timing**, then
save. Line-timed lyrics can be approved without inventing word timestamps.
Out-of-order, overlapping or out-of-range line times must be corrected before
saving. Approval also checks that any retained word times match the line and
stay within it.

**Align text** is optional when saved lines have no word highlights. It
supplies the edited text to the existing full-recording alignment routine;
you do not need to paste the song again. It requires configured local models
and produces another unapproved draft. The editor does not run models merely
because you change or save text. **More lyric options** contains import,
extraction, a supplied-sheet field and preview controls for existing lyrics.
For a recording without lyrics, preparation buttons appear immediately.

Unapproved lyrics stay off Program Output and out of **Lyrics-ready tracks
only**. The optional **Preview unapproved saved lyrics on Program Output**
control explicitly allows a saved draft onto that output; it starts unchecked
and resets for another recording or newly prepared/imported revision. It does
not approve or save anything. An intentionally empty saved draft remains
unapproved and cannot be made Lyrics Ready; clearing false text does not
claim automatic instrumental detection.

Successful replacements keep the complete prior JSON in a sibling `history/`
folder. Saved metadata includes `human_review`, bound to the recording SHA256
and canonical document revision. The first original QC object is retained
under `review_original_quality`, and older application versions see an
effective QC status of `review`, so rollback cannot silently air a manual
revision. Unknown model metadata and original findings are preserved. Shared
write locks and revision checks reject conflicting saves; reload the saved
revision before editing again when another writer changed it.

For a disposable UI preview with no model or audio playback, run:

```sh
QT_QPA_PLATFORM=offscreen python scripts/preview_lyrics_review.py --output-dir /tmp/lyrics-review-preview
```

This creates its own temporary recording/session and renders normal and
minimum-size windows. It never reads or modifies an operator's live data.
