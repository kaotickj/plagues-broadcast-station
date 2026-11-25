# Recording-specific name review

Development candidate only; not installed or published. The owner accepted the
Stage D recommendation to keep automatic vocabulary hints off. The six-call
screen improved Murkwater and Hancock but inserted irrelevant names and changed
Bartender to Bartend in its ordinary-song control.

Recognition remains audio-only. A shared policy rejects nonempty vocabulary,
context and prompt options before locks, audio decoding or model loading. The
existing Studio adapter is still Heart plus CTC: this change does not install or
silently substitute the experimental Qwen engine.

The private review dialog has a **Names** tab. Use **Load track vocabulary
suggestions** with a profile containing entries, canonical names, observed
confusions and spelling-source provenance. Only accepted-lyric spellings whose
`source.audio_sha256` matches the exact recording are considered. Catalog titles
alone do not qualify. Matching uses exact whole words/phrases, not fuzzy matching
or a library-wide replacement rule. Loading a profile changes no lyric text.

Each proposal can be accepted or rejected. Acceptance changes only that occurrence
and retains existing native anchors and highlight intervals. A change in word
count retains grouped timing, with no invented individual word boundaries.
Rejection leaves all words and timing unchanged. Original segments, recognition
provenance, profile digest and each decision remain separate in the draft.
Undo/redo and versioned private saves use the existing review history. Accepted
corrections do not approve lyrics or start alignment. All pending proposals must
be decided before alignment/approval. **Realign current draft** snapshots the
corrected words and their decision provenance; **Align supplied text** remains an
explicitly separate source. Existing output publication requires separate approval.

Verified: 75 software tests, including real Qt Names controls with fake media,
accept/reject/undo/save/reload, same-source checks, unchanged non-Fallout text,
honest grouped gaps, stale-draft refusal and hint rejection before model calls.
No inference was run for these tests. A wider selection inadvertently included a
real multimedia test and stalled at the known audio-device restriction; it was
interrupted. It is not counted as passed. No device access workaround was used.

Next gate: normal-desktop review of the Names tab and packaged worker-policy
availability, then a tested build/rollout decision. Real audio playback and
packaged app acceptance remain unverified. Installed dev6, live websites and
published lyrics are unchanged.
