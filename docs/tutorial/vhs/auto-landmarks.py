#!/usr/bin/env python3
"""Auto-generate landmarks by matching SCREEN commands to cast events and voice timing.

Parses the tutorial markdown to get the block sequence, finds each SCREEN
command's timestamp in the cast file, and maps it to the corresponding
audio timestamp based on where it falls in the voice sequence.

Usage:
    python3 auto-landmarks.py <section.md> <cast-file> <timing.json>

Output:
    <cast-file>.landmarks.json
"""
import json
import re
import sys
from pathlib import Path


def parse_block_sequence(text: str) -> list[dict]:
    """Parse tutorial into ordered blocks with types."""
    blocks = []
    parts = re.split(r'^## ', text, flags=re.MULTILINE)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        header = part.split('\n', 1)[0]
        body = part.split('\n', 1)[1].strip() if '\n' in part else ''

        if header.startswith('VOICE'):
            over = 'OVER SCREEN' in header
            # Extract pause durations
            for chunk in re.split(r'\[PAUSE\s+(\d+)s?\]', body):
                clean = chunk.strip()
                if clean and not clean.isdigit() and not clean.startswith('#'):
                    blocks.append({'type': 'voice', 'over_screen': over, 'text': clean[:60]})
                elif clean.isdigit():
                    blocks.append({'type': 'pause', 'duration': float(clean)})

        elif header.startswith('SCREEN'):
            code = re.search(r'```(?:bash)?\n(.*?)```', body, re.DOTALL)
            if code:
                for line in code.group(1).strip().split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#') and not line.startswith('cd '):
                        blocks.append({'type': 'screen', 'command': line})

        elif header.startswith('OVERLAY'):
            blocks.append({'type': 'overlay'})

    return blocks


def find_in_cast(abs_events: list, pattern: str) -> float | None:
    """Find first event timestamp matching pattern."""
    for e in abs_events:
        if pattern in e[2]:
            return e[0]
    return None


def main():
    md_path = Path(sys.argv[1])
    cast_path = Path(sys.argv[2])
    timing_path = Path(sys.argv[3])

    text = md_path.read_text()
    blocks = parse_block_sequence(text)

    with open(timing_path) as f:
        timing = json.load(f)

    with open(cast_path) as f:
        header = json.loads(f.readline())
        events = [json.loads(l) for l in f]

    # Convert to absolute timestamps
    t = 0.0
    abs_events = []
    for e in events:
        t += e[0]
        abs_events.append([t, e[1], e[2]])

    # Walk through blocks, tracking audio position from voice segments
    voice_idx = 0
    voice_segments = [s for s in timing['segments']]
    audio_cursor = 0.0

    landmarks = []

    for block in blocks:
        if block['type'] == 'voice':
            # Advance audio cursor past this voice segment
            if voice_idx < len(voice_segments):
                seg = voice_segments[voice_idx]
                audio_cursor = seg['end']
                voice_idx += 1

        elif block['type'] == 'pause':
            if voice_idx < len(voice_segments):
                seg = voice_segments[voice_idx]
                audio_cursor = seg['end']
                voice_idx += 1

        elif block['type'] == 'screen':
            cmd = block['command']
            # Find this command in the cast
            # Use first distinctive word(s) as pattern
            pattern = cmd.split()[0] if ' ' in cmd else cmd
            # For commands like 'nx memory put "..."', use first 2 words
            words = cmd.split()
            if len(words) >= 2:
                pattern = ' '.join(words[:2])
            if len(words) >= 3 and words[0] in ('nx', 'uv'):
                pattern = ' '.join(words[:3])

            cast_time = find_in_cast(abs_events, pattern)
            if cast_time is not None:
                landmarks.append({
                    'pattern': pattern,
                    'audio_time': round(audio_cursor, 1),
                    'cast_time': round(cast_time, 1),
                    'command': cmd,
                })
                print(f'  {cast_time:7.1f}s → {audio_cursor:5.1f}s  {cmd[:60]}')
            else:
                print(f'  ???     → {audio_cursor:5.1f}s  {cmd[:60]}  [NOT FOUND IN CAST]')

    output_path = cast_path.with_suffix('.landmarks.json')
    with open(output_path, 'w') as f:
        json.dump(landmarks, f, indent=2)
    print(f'\nWrote {len(landmarks)} landmarks to {output_path}')


if __name__ == '__main__':
    main()
