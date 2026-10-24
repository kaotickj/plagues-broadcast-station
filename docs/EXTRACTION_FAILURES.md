# Recognition and alignment failures

An extraction that completes its model processes can still lack usable lyric evidence. The `singing-5` pipeline checks the recognition input and CTC timing before replacing a saved draft. These checks do not certify that recognized words are correct.

If recognition audio is effectively silent, extraction stops before recognition. A separated stem with activity in less than 25% of one-second frames is compared with the original mix. If the mix exceeds −35 dBFS RMS and is at least 12 dB louder, the stem is marked suspect. **That measurement alone does not reject lyrics:** a failure requires recognition to also collapse into fewer than 0.12 words/second, at least half its word intervals exceeding eight seconds, and at least 65% of tokens belonging to the two most frequent tokens. Quiet vocals, instrumental breaks and repeated words alone do not trigger this combined check. Thresholds are conservative diagnostics requiring real-audio validation, not a vocal/instrumental classifier.

CTC realignment must preserve exact word coverage, finite in-range monotonic timing and separately measured confidence. A word longer than 12 seconds, invalid confidence, mean confidence below 0.15, or a majority of scores below 0.10 rejects the new alignment. An isolated weak score remains visible for review. Sustained words up to 12 seconds and long instrumental gaps remain possible; existing publication checks still flag words over eight seconds.

A rejected alignment stops extraction with an explanation. Ordinary optional-aligner runtime unavailability retains the original recognizer timing with a review warning. ASR confidence is never substituted for CTC confidence. Valid `alignment_score` values survive into saved word metadata and quality notes.

On failure:

- The existing saved lyric file, approval proof, recording and editor buffer remain unchanged. No replacement draft is committed.
- Raw recognition evidence, available signal measurements and the failure reason are retained separately under the selected extraction cache's `failures/` directory. Each diagnostic has a unique name, source recording hash and private file permissions. It is not a lyric export or approval.
- If diagnostics cannot be written, the original error remains actionable and existing lyrics are still preserved.
- The GUI keeps the failure message visible after redisplaying the prior draft.

Check the source recording and the separated vocal track. Where the separated track lacks usable signal, try the recording with **Separate vocals** disabled, or supply a known lyric sheet for alignment. Retrying is an explicit operation; this patch does not automatically rerun a library, select another model, download models, or modify saved text by deleting common phrases.

The regression suite uses deterministic audio arrays, saved raw-output structure, mocked model results and isolated Qt workflows. It verifies preservation and rejection behavior. It does not establish transcription accuracy or determine whether a particular recording contains lead vocals. A coordinated original-mix/stem comparison and bounded real-audio model test remain necessary before claiming the reported recognition problem corrected.
