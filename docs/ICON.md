# Broadcast Station icon

The broadcast tower, signal arcs and waveform belong to the same weathered
chrome and electric-lime family as Plague's Music Studio. All icon files retain
real transparency. The original generated master is preserved alongside PNG
exports at 16, 24, 32, 48, 64, 128, 256 and 512 pixels. The Windows ICO contains
16–256-pixel frames.

Assets live in `src/gnb_player/assets/`, are included in wheel and standalone
packages, and provide the shared Qt application/window icon. PyInstaller embeds
the ICO into the Windows executable. Linux desktop launchers use the
`plagues-broadcast-station` icon name and matching desktop-file identity.

Created with the built-in image generation tool using the existing Music Studio
master as a style reference. ImageMagick performed only deterministic size and
format exports, preserving the original alpha.

Generation prompt:

```text
+Use case: logo-brand
Asset type: desktop application icon for Plague's Broadcast Station, Windows and Linux.
Input image: the provided Plague's Music Studio master icon is a STYLE REFERENCE ONLY. Create a new sibling emblem in the same brand family, rather than duplicating the microphone.
Primary request: one bold professional broadcast emblem: a short, sturdy black-chrome radio transmission tower centered inside two broad, symmetric broadcast signal arcs; the tower and arcs form a compact striking silhouette. Integrate one simple vivid acid-lime audio waveform across the tower's middle. This should feel like a sibling of the reference icon.
Style/materials: sculpted beveled weathered gunmetal and chrome, controlled acid-lime green luminous edges, subtle rugged industrial metal texture. Nearly frontal view, crisp realistic 3D emblem.
Composition: square 1:1, emblem centered with generous 8% transparent safety margin on every edge; strong chunky shapes, readable at 32 pixels. The transmission arcs must remain thick enough to survive reduction. Keep details subordinate to the silhouette.
Background: genuinely transparent alpha, no solid or checkerboard background. All space outside the emblem transparent.
Constraints: no text, no words, no letters, no numbers, no watermark, no rectangular app tile, no extra objects, no microphone. Preserve the black/chrome and electric lime palette of the reference. Polished finished icon.
```
