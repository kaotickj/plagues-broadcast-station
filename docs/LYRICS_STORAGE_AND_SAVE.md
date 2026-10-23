# Save lyrics and recording identity

**Save lyrics** commits text and line times to the selected recording's native
lyric storage. It does not ask for a filename. The separate **Export lyrics…**
action creates a copy in a location you choose; save pending edits first.

Double-click a lyric cell or press F2, make the correction, and choose **Save
lyrics** or Ctrl+S. The active cell commits as part of Save. Lyric lines and
Unicode text survive reopening. Until a save succeeds, changes stay in the edit
buffer. If saving the session link fails after the lyric file commits, the status
explains that partial result and Save retries the link without rewriting lyrics.

## Where the selected song's lyrics go

The session maps each recording to its lyric file. Existing native JSON stays at
that location; imported or subtitle revisions use a managed JSON file under:

```
<station data directory>/lyrics-cache/lyrics/<audio SHA256>.json
```

Identity follows the exact recording bytes, not its title. Two recordings with
the same title do not share the cache entry unless their audio bytes are the
same. The session link is durable before Save reports full success. Original
subtitle files are retained in their own formats.

Manual revisions and original machine findings remain recoverable in history.
Imports retain available source IDs and original provenance separately from the
current recording/revision. A document without an audio fingerprint is explicitly
bound to the selected recording by your import action; that does not prove it
originated from the same recording elsewhere. Check imported words and timing.

Changing text creates a current draft. Old review or publication metadata is
retained as evidence and cannot approve the new text. **Approve for airing**
still requires separate review of the saved lyrics against that recording.
Neither Save nor local airing approval publishes to a website.

Cooperating saves use a write lock and expected revision checks. Extraction
compares the target revision captured before processing before replacing it; a
newer human correction causes a conflict rather than an automatic overwrite.
If a conflict or disk error occurs, preserve the editor and inspect the status
before reloading or retrying. History is not permission to overwrite a conflict.

## Website and desktop relationship

Goodneighbor and Metalfix share the website CMS lyric store. Broadcast Station
has its own local sidecars, cache and station session. These are independent
stores connected by explicit snapshot/import/export workflows, not automatic
live synchronization. The packet-verified Goodneighbor media-pack seeder retains
source mapping when it creates a local copy for differently encoded audio.

Website public lyric JSON can omit audio fingerprints and review provenance.
A title or website song ID alone cannot establish which local audio it matches.
Keep available origin metadata, verify the recording, and review each destination
draft. This version adds no implicit website writes and never silently resolves
divergent desktop and website copies.

## Getting the tested version

Check the version in the Operator Desk title. A managed installation retains a
previous executable for rollback and preserves station data. Desktop, menu and
panel shortcuts must point to that managed launcher; an already running older
window keeps its original code until you choose to close it and launch again.
