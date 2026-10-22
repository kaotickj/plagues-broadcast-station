# Put the program picture on your livestream

## Picture

Open the app's separate **Program Output** window. In OBS, create a scene and add a **Window Capture** source selecting that program window. Capture the clean program window rather than the operator controls. Set the scene/base canvas to a 16:9 size such as 1920×1080 and fit the source to that canvas. The same program painter is used for the app's preview and encoder output.

Keep Program Output open while broadcasting. Fullscreen is available when using a dedicated display; window capture is convenient when the operator and program windows share one screen. If your desktop compositor stops updating minimized windows, leave the program window restored on its own display or workspace.

## Sound

The app sends its music to the selected operating-system playback device. A picture-only Window Capture does not necessarily capture that audio.

On supported Windows versions, OBS provides application audio capture, and recent OBS versions can include audio in a Window Capture source. Select Plague's Broadcast Station's audio, then check the OBS audio meter while a song is playing. Use one capture path for the music; disable a second desktop-audio source carrying the same output to prevent doubled sound. See the official [OBS application-audio guide](https://obsproject.com/kb/application-audio-capture-guide).

On Linux, choose the monitor/loopback source for the output device carrying the app, using the audio sources available in your OBS/PulseAudio/PipeWire setup. Per-application capture availability depends on that desktop's OBS/audio integration. If using whole-desktop audio, other desktop sounds on that output are also captured; route the app to a dedicated sink if you need isolation. This application does not change the desktop's audio routing automatically.

No microphone is needed for the music waveform or lyric extraction. Add a microphone in OBS only when you want spoken hosting.

## A short check before going live

1. Play a song with known lyrics and record 30 seconds locally in OBS.
2. Confirm the picture fills the scene, the music meter moves, and playback has one clean soundtrack.
3. Listen through a few lyric lines. Use the player's lyric offset for a consistent delay; text needing correction should be reviewed rather than disguised with a large timing adjustment.
4. Check a song transition and a slideshow/video transition.
5. Leave a little audio headroom and choose a comfortable visualizer level. Motion can be reduced or disabled without stopping the music.

## In-app recording and direct streaming

These are alternatives to using OBS. Choose a local recording file or enter the platform's RTMP/RTMPS destination and stream key in the app, then explicitly start the output. The standalone native package includes FFmpeg; a source checkout needs FFmpeg on PATH. Keys remain in the current app session and are not part of saved profiles; don't put them into filenames or screenshots.

Make a local recording first. Direct encoder performance depends on resolution, frame rate, codec and available CPU/GPU resources. The player and lyric extraction share a machine, so run queued extraction in advance when preparing a show. Public service limits and destination settings are controlled by the streaming platform.
