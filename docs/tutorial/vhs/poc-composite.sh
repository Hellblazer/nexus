#!/usr/bin/env bash
# POC: Composite a VHS recording with text overlays via FFmpeg
#
# Proves: VHS → MP4 → FFmpeg overlay → final video
# TTS audio would be added the same way (ffmpeg -i video -i audio -c:v copy)
#
# Usage:
#   ./poc-composite.sh poc-test.mp4
#
# Produces: poc-test-composited.mp4

set -euo pipefail

INPUT="${1:?Usage: $0 <input.mp4>}"
OUTPUT="${INPUT%.mp4}-composited.mp4"

if ! command -v ffmpeg &>/dev/null; then
    echo "ffmpeg not found. Install with: brew install ffmpeg"
    exit 1
fi

echo "=== POC Composite ==="
echo "Input:  $INPUT"
echo "Output: $OUTPUT"
echo ""

# Add a title overlay for the first 5 seconds
# and a "cheatsheet URL" overlay for the last 5 seconds
ffmpeg -y -i "$INPUT" \
    -vf "
        drawtext=text='Nexus Tutorial — POC Test':
            fontsize=28:
            fontcolor=white:
            x=(w-text_w)/2:
            y=30:
            enable='between(t,0,5)':
            box=1:boxcolor=black@0.6:boxborderw=10,
        drawtext=text='github.com/Hellblazer/nexus/docs/tutorial/companion-cheatsheet.md':
            fontsize=16:
            fontcolor=white:
            x=(w-text_w)/2:
            y=h-50:
            enable='between(t,15,25)':
            box=1:boxcolor=black@0.6:boxborderw=5
    " \
    -codec:a copy \
    "$OUTPUT"

echo ""
echo "✓ Composited: $OUTPUT"
echo ""
echo "To add TTS audio later:"
echo "  ffmpeg -i $OUTPUT -i voice.mp3 -c:v copy -c:a aac -map 0:v -map 1:a final.mp4"
