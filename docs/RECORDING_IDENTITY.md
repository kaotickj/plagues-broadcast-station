# Recording identity cache

Lyric review and playback eligibility remain bound to the saved recording SHA256.
A recording title, file size or modification time cannot establish that identity.

POSIX filesystems can coalesce multiple content changes into one ctime clock tick.
The player hashes recording bytes while ctime is less than two seconds old, in the
future, or invalid. Once timestamps settle it can reuse the recording hash until
the change stamp differs. A settling boundary crossed during hashing is not itself
a content change; unchanged metadata still requires another hash before caching.

This policy assumes the supported POSIX filesystem refreshes ctime on content
changes with timestamp granularity no coarser than two seconds. It is not an inode
version counter or a guarantee against concurrent writers or manipulated clocks.
Windows retains its separate native ChangeTime query and content-hash fallback;
the POSIX regression does not establish native Windows behavior.

Saved lyric files are much smaller than recordings. Their bytes are hashed on each
identity check with the existing 8 MiB read limit, including readable subtitle
links. A missing baseline, changed file, oversized replacement or read failure
holds its loaded lyrics off output. Native Save and extraction compare-and-swap
checks continue to protect revisions independently of this read-side cache.

Regression coverage includes frozen same-tick metadata, actual rapid recording
rewrites, unchanged files crossing the settling boundary, stable-cache reuse,
changed lyric bytes, oversized files and readable subtitle links. Tests do not
relax the strict same-size/restored-mtime recording rejection.
