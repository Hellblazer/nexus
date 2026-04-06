#!/usr/bin/env bash
# Produce a complete tutorial section: record + TTS + retime + render + merge
#
# Usage:
#   ./produce-section.sh <section.md> <container-name>
#
# Example:
#   ./produce-section.sh ../02-install-nexus.md nexus-tutorial-ready
#
# Output:
#   <basename>-final.mp4

set -euo pipefail

SECTION="${1:?Usage: $0 <section.md> <container>}"
CONTAINER="${2:?Usage: $0 <section.md> <container>}"
BASENAME=$(basename "$SECTION" .md)
VOICE="en-US-BrianNeural"
RATE="+5%"
COLS=120
ROWS=35

echo "=== Producing: $BASENAME ==="
echo "  Section:   $SECTION"
echo "  Container: $CONTAINER"
echo ""

# Step 1: Generate TTS
echo "--- Step 1: TTS ---"
python3 tts-generate.py "$SECTION" --voice "$VOICE" --rate "$RATE"
echo ""

# Step 2: Extract SCREEN commands and generate expect script
echo "--- Step 2: Generate expect script ---"
python3 -c "
import re, sys
text = open('$SECTION').read()
parts = re.split(r'^\#\# ', text, flags=re.MULTILINE)
cmds = []
for part in parts:
    if part.startswith('SCREEN'):
        code = re.search(r'\`\`\`(?:bash)?\n(.*?)\`\`\`', part, re.DOTALL)
        if code:
            for line in code.group(1).strip().split('\n'):
                line = line.strip()
                if line and not line.startswith('#') and not line.startswith('cd '):
                    cmds.append(line)

lines = [
    '#!/usr/bin/expect -f',
    'set timeout 300',
    r'set prompt {\$ \$}',
    'log_user 0',
    f'spawn docker exec -it $CONTAINER bash',
    'expect -re \$prompt',
    'log_user 1',
    'sleep 0.5',
]
for cmd in cmds:
    # Escape special chars for expect
    cmd_escaped = cmd.replace('\"', '\\\\\"')
    lines.append(f'send \"{cmd_escaped}\\\\r\"')
    lines.append('expect -re \$prompt')
    lines.append('sleep 1')
lines.append('send \"exit\\\\r\"')
lines.append('expect eof')
print('\n'.join(lines))
" > "${BASENAME}.expect"
chmod +x "${BASENAME}.expect"
echo "  Commands:"
grep '^send ' "${BASENAME}.expect" | sed 's/send "//;s/\\r"//' | sed 's/^/    /'
echo ""

# Step 3: Record with asciinema
echo "--- Step 3: Record ---"
asciinema rec --command="./${BASENAME}.expect" --cols=$COLS --rows=$ROWS "${BASENAME}.cast"
echo ""

# Step 4: Retime cast to match TTS
echo "--- Step 4: Retime ---"
python3 cast-retimer.py "${BASENAME}.cast" "tts/${BASENAME}/timing.json" "${BASENAME}-retimed.cast"
echo ""

# Step 5: Render to GIF then MP4
echo "--- Step 5: Render ---"
agg --theme monokai --font-size 16 --idle-time-limit 1 "${BASENAME}-retimed.cast" "${BASENAME}.gif"
ffmpeg -y -i "${BASENAME}.gif" -movflags faststart -pix_fmt yuv420p \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2" "${BASENAME}.mp4"
echo ""

# Step 6: Merge video + audio
echo "--- Step 6: Merge ---"
AUDIO="tts/${BASENAME}/full.mp3"
VIDEO="${BASENAME}.mp4"
OUTPUT="${BASENAME}-final.mp4"

V_DUR=$(ffprobe -v quiet -show_format "$VIDEO" 2>&1 | grep duration | cut -d= -f2)
A_DUR=$(ffprobe -v quiet -show_format "$AUDIO" 2>&1 | grep duration | cut -d= -f2)
echo "  Video: ${V_DUR}s  Audio: ${A_DUR}s"

ffmpeg -y -i "$VIDEO" -i "$AUDIO" \
  -c:v copy -c:a aac \
  -map 0:v -map 1:a \
  -shortest \
  "$OUTPUT"

FINAL_DUR=$(ffprobe -v quiet -show_format "$OUTPUT" 2>&1 | grep duration | cut -d= -f2)
FINAL_SIZE=$(ls -lh "$OUTPUT" | awk '{print $5}')
echo ""
echo "=== Done: $OUTPUT (${FINAL_DUR}s, ${FINAL_SIZE}) ==="
