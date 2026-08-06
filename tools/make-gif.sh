#!/usr/bin/env bash
# Render the transfer animation and assemble it into a GIF.
#   tools/make-gif.sh [output.gif]
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$here/docs/screenshots/nearshare-transfer.gif}"
frames="$(mktemp -d)"
trap 'rm -rf "$frames"' EXIT

"$here/.venv/bin/python" "$here/tools/screencast.py" "$frames"

# Two passes: a per-frame palette keeps the orange accents from banding.
ffmpeg -y -loglevel error -framerate 5 -i "$frames/frame-%03d.png" \
    -vf "fps=10,scale=470:-1:flags=lanczos,palettegen=stats_mode=diff" \
    "$frames/palette.png"
ffmpeg -y -loglevel error -framerate 5 -i "$frames/frame-%03d.png" \
    -i "$frames/palette.png" \
    -lavfi "fps=10,scale=470:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=3" \
    -loop 0 "$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"
