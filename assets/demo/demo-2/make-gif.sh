#!/usr/bin/env bash
# Crop macOS window screenshots to the window itself (dropping the
# transparent drop-shadow padding) and assemble them into a looping,
# indexed GIF with a shared palette.
#
# Usage: ./make-gif.sh [output.gif]
# Edit FRAMES / DELAYS below to change slides or timing.
set -euo pipefail
cd "$(dirname "$0")"

OUT=${1:-demo.gif}
FRAMES=(ss1.png ss2.png ss3.png ss4a.png ss5a.png ss5b.png)
DELAYS=(133 133 133 300 300 300)   # centiseconds: router ticks 1.33s each, then lighting, Roku, voice 3s each (then loop)

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# Window = pixels that are (nearly) opaque; the shadow is partly
# transparent. Take the union bbox across frames in case they differ.
crop=$(magick "${FRAMES[@]}" -alpha extract -threshold 90% \
         -evaluate-sequence max -format '%@' info:)
echo "crop: $crop"

args=()
for i in "${!FRAMES[@]}"; do
  # Crop, then make alpha binary (GIF only has 1-bit transparency) so
  # the rounded corners stay transparent with no shadow fringe.
  magick "${FRAMES[$i]}" -crop "$crop" +repage \
    -channel A -threshold 50% +channel "$tmp/f$i.png"
  args+=(-delay "${DELAYS[$i]}" "$tmp/f$i.png")
done

# One shared 255-color palette (+1 for transparency) built from all
# frames, no dithering so the terminal text stays crisp.
magick "$tmp"/f*.png -background none +append \
  -dither None -colors 255 -unique-colors "$tmp/palette.png"

magick "${args[@]}" -dither None -remap "$tmp/palette.png" \
  -loop 0 -layers OptimizeTransparency "$OUT"

magick identify -format '%s: %wx%h delay=%T colors=%k\n' "$OUT"
ls -lh "$OUT"
