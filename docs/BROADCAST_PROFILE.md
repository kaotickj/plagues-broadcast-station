# Save a direct livestream profile

Choose **Direct livestream**, enter the server URL and masked Stream key separately, then choose picture size and video bitrate. **Save profile** (Ctrl+S on Broadcast) saves reusable settings for this station data folder. Read the persistent saved/unsaved status after saving. **Save playlist** keeps songs and appearance separately; it does not save a streaming key.

Ordinary server, picture and bitrate settings restore on the next launch without requiring a keyring. Keys remain session-only unless **Remember stream key in OS keyring** is checked before saving. Saving or reloading never starts output. Press **Start livestream** explicitly when ready. Local recording has a separate session filename; switching output type preserves the current filename and stream server independently. Recording filenames are not persisted in the stream profile.

**Reload saved** asks before replacing unsaved stream fields. **Clear fields** clears only this session. **Forget saved** asks before deleting the ordinary settings and protected copies for this data folder; cancellation keeps the current fields and saved selection. A failure message means the operation needs attention. Unchecking Remember and saving stops future automatic key restoration; older protected copies remain until explicit Forget saved.

## Ordinary settings and protected targets

`broadcast-settings.json` stores the picture size, bitrate, explicit remember choice, and a conservative public server URL. Ordinary URL paths are limited to empty, `/live`, `/live2`, `/app`, or `/application` (and trailing slash variants). An arbitrary custom application path could itself be a private key. Custom paths, inline keys, userinfo, query parameters and fragments require protected storage or a corrected server/key split. A path matching the entered key is never written as an ordinary application name.

When Remember is selected, the complete destination and key are kept together in protected native credentials. A private destination is omitted from the ordinary file. `broadcast-profile.json` contains only a schema number, active native slot and random non-secret revision token. No key enters ordinary settings, playlists, session JSON, portable exports or diagnostics.

If protected storage fails for a normal public server URL, ordinary settings still save and the status explains that the key was not remembered. If the complete target is private, it stays session-only when protected saving fails. A nonsecret `broadcast-profile-attempt.json` records that a protected write was attempted, so Forget can also clean up an interrupted first save after restarting. It contains only a schema marker, never a target or key. It is removed only after verified native cleanup. A partial save is reported explicitly; retry after correcting the reported issue. The app never silently falls back to plaintext secret storage.

## Native storage and multiple windows

Windows uses the explicit Windows Credential Manager backend; Linux uses the explicit Secret Service backend from the maintained [keyring library](https://keyring.readthedocs.io/en/stable/). Linux needs a running desktop Secret Service and an unlocked collection. No alternate backend or automatic backend-selection fallback is used. Native backend exception text is not shown. A fresh startup or ordinary-only profile does not contact a keyring. Profile operations run outside the GUI thread and are disabled during output.

Protected credentials belong to the same OS user and canonical data-folder path. Two native slots allow writing and verifying a replacement before atomically selecting it. A random revision token refuses stale fallback copies; an unavailable active credential never silently falls back to an older inactive slot. Windows persistence is local-machine. A failed update keeps the prior active slot; an inactive protected copy can remain until a later save or Forget. Per-data-folder locks serialize cooperating windows' complete save/reload/forget operations and their individual storage writes. Busy operations report a retry instead of interleaving.

Existing native keyring access controls and the OS account define the protection boundary. This feature does not configure the user's keyring, migrate actual credentials or change the OS security setup. Existing protected-only profiles can still restore; subsequent Save profile adds ordinary settings only after an explicit save. Older app versions do not understand ordinary-only profiles. Do not use different app versions concurrently against one data folder.

## Bitrate and help

Video bitrate accepts **1,000–100,000 kbps**. Type `28500` for **28.5 Mbps**. This is an available value, not a universal recommendation. The presets use YouTube's H.264 guidance for the app's 30 fps picture sizes: 8,000 kbps at 720p and 14,000 kbps at 1080p, checked 25 October 2026. Match codec, resolution, frame rate and upload capacity to your service. See [official YouTube encoder guidance](https://support.google.com/youtube/answer/2853702?hl=en).

Broadcast help and **Help** (F1) contain bundled setup, model, saving and troubleshooting topics with local search. Only the explicit YouTube help button opens a browser. Help does not start streams, install models or publish content. **Show preview and playback** restores the operator monitor above Broadcast controls; Program Output is independent.
