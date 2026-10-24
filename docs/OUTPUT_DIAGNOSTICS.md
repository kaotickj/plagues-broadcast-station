# Output diagnostics

In **Broadcast**, use **Copy diagnostics** after a failed start, interruption or reconnect. It copies version/build identity, UTC event times, safe output dimensions/rate, each attempt's failure reason, encoder exit code, redacted stderr excerpts and delivery counters. It does not start or stop output. Streaming destinations, keys, passwords, tokens, process environments and full commands are excluded.

The latest history is kept privately in `output-diagnostics.json` under that station's managed data folder: at most64 events and64KiB, replaced atomically with0600 permissions. If the file cannot be saved, the current in-memory history can still be copied. A new explicit start clears the visible old failure; final encoder status does not erase a failure.

The diagnostics candidate preserves RC5's encoder settings, two-second delivery/send limits and retry/backoff policy. It does not claim that instrumentation fixes an interrupted livestream. Local playback continuing after stream failure does not imply the stream is still sending.

For comparison, the reported screenshot showed delivery lag2.277s, two reconnects and stopped output at1920×1080/30fps and14000kbps. That lag crosses the existing two-second delivery guard. Slow local encoding/scheduling and downstream backpressure remain distinct possible causes; use the recorded attempt/feed error to distinguish them.

This output-only candidate is based on RC5. It excludes the separately prepared, paused lyric-extraction safeguard changes.
