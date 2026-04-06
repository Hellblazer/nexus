#!/usr/bin/env python3
"""Retime an asciinema .cast file to sync with TTS audio.

Instead of cutting video into segments, this modifies the .cast file
timestamps directly — inserting pauses where the voice needs time,
compressing boring stretches, and leaving interesting parts at 1x.

The result is a single continuous recording that plays in sync with
the TTS audio track.

Usage:
    uv run python cast-retimer.py <input.cast> <timing.json> <output.cast>

Then render + merge:
    agg output.cast output.gif
    ffmpeg -i output.gif -i tts/full.mp3 -c:v libx264 -c:a aac final.mp4
"""
import json
import sys
from pathlib import Path


def load_cast(path: Path) -> tuple[dict, list]:
    with open(path) as f:
        header = json.loads(f.readline())
        events = [json.loads(line) for line in f]
    return header, events


def save_cast(path: Path, header: dict, events: list) -> None:
    with open(path, 'w') as f:
        f.write(json.dumps(header) + '\n')
        for e in events:
            f.write(json.dumps(e) + '\n')


def to_absolute(events: list) -> list:
    """Convert relative timestamps to absolute."""
    t = 0.0
    result = []
    for e in events:
        t += e[0]
        result.append([t, e[1], e[2]])
    return result


def to_relative(events: list) -> list:
    """Convert absolute timestamps back to relative."""
    result = []
    prev = 0.0
    for e in events:
        delta = max(0, e[0] - prev)
        result.append([delta, e[1], e[2]])
        prev = e[0]
    return result


def find_event_at(events: list, pattern: str) -> float | None:
    """Find the absolute timestamp of the first event matching pattern."""
    for e in events:
        if pattern in e[2]:
            return e[0]
    return None


def retime_cast(
    events: list,
    voice_segments: list[dict],
    screen_landmarks: list[dict],
) -> list:
    """Retime events to sync with voice segments.

    Strategy:
    - Each screen_landmark maps a command pattern to a voice segment
    - Between landmarks, adjust time scale so video matches voice timing
    - Idle stretches (like package install) get compressed
    - After last command, hold at current frame (just extend the timestamp gap)
    """
    abs_events = to_absolute(events)
    if not abs_events:
        return events

    total_cast_time = abs_events[-1][0]

    # Build remap: list of (cast_time, target_time) anchor points
    anchors = [(0.0, 0.0)]  # start at 0

    for lm in screen_landmarks:
        cast_t = find_event_at(abs_events, lm['pattern'])
        if cast_t is not None:
            anchors.append((cast_t, lm['audio_time']))

    # Sort by cast time
    anchors.sort(key=lambda x: x[0])

    # Add end anchor: map cast end to audio end
    audio_end = max(s['end'] for s in voice_segments)
    anchors.append((total_cast_time, audio_end))

    # Remap each event's timestamp via linear interpolation between anchors
    retimed = []
    for e in abs_events:
        cast_t = e[0]
        # Find surrounding anchors
        for j in range(len(anchors) - 1):
            if anchors[j][0] <= cast_t <= anchors[j + 1][0]:
                cast_span = anchors[j + 1][0] - anchors[j][0]
                target_span = anchors[j + 1][1] - anchors[j][1]
                if cast_span > 0:
                    frac = (cast_t - anchors[j][0]) / cast_span
                else:
                    frac = 0
                new_t = anchors[j][1] + frac * target_span
                retimed.append([new_t, e[1], e[2]])
                break
        else:
            # Past last anchor — hold
            retimed.append([audio_end, e[1], e[2]])

    return to_relative(retimed)


def main():
    if len(sys.argv) < 4:
        print(f'Usage: {sys.argv[0]} <input.cast> <timing.json> <output.cast>')
        print()
        print('Also needs a landmarks file (auto-generated if missing).')
        sys.exit(1)

    cast_path = Path(sys.argv[1])
    timing_path = Path(sys.argv[2])
    output_path = Path(sys.argv[3])

    header, events = load_cast(cast_path)
    with open(timing_path) as f:
        timing = json.load(f)

    voice_segments = timing['segments']
    abs_events = to_absolute(events)

    # Auto-detect landmarks from cast content
    # Find key commands and map them to voice segment times
    landmarks = []

    # Print cast content summary for manual landmark creation
    print('Cast events with visible output:')
    for e in abs_events:
        clean = e[2].replace('\x1b[?2004h', '').replace('\x1b[?2004l', '')
        clean = clean.replace('\r\n', ' ').replace('\r', '').strip()
        if len(clean) > 3 and e[0] > 0:
            print(f'  {e[0]:7.1f}s  {clean[:70]}')

    print()
    print('Voice segments:')
    for s in voice_segments:
        if s['type'] == 'voice':
            print(f"  {s['start']:5.1f}s - {s['end']:5.1f}s  {s['text'][:55]}...")
        else:
            print(f"  {s['start']:5.1f}s - {s['end']:5.1f}s  [pause]")

    # Auto-generate landmarks by matching command patterns to voice timing
    # This is a heuristic — for production, use a manual landmarks file
    landmarks_path = cast_path.with_suffix('.landmarks.json')
    if landmarks_path.exists():
        with open(landmarks_path) as f:
            landmarks = json.load(f)
        print(f'\nLoaded {len(landmarks)} landmarks from {landmarks_path}')
    else:
        print(f'\nNo landmarks file found at {landmarks_path}')
        print('Create one with entries like:')
        print('  [{"pattern": "uv tool install", "audio_time": 6.6},')
        print('   {"pattern": "nx --version", "audio_time": 15.8}]')
        print()
        print('For now, using linear time mapping.')
        # Simple linear remap
        cast_dur = abs_events[-1][0] if abs_events else 1
        audio_dur = timing['total_duration']
        landmarks = [
            {'pattern': abs_events[0][2][:10], 'audio_time': 0.0},
        ]

    # Retime
    retimed = retime_cast(events, voice_segments, landmarks)

    # Save
    save_cast(output_path, header, retimed)
    print(f'\nOutput: {output_path}')


if __name__ == '__main__':
    main()
