#!/usr/bin/env python3
"""Extract VOICE blocks from tutorial markdown and generate TTS audio.

Parses ## VOICE sections, generates MP3 per segment via edge-tts,
then concatenates into a single audio track with timing metadata.

Usage:
    uv run python tts-generate.py <section.md> [--voice VOICE] [--rate RATE]

Output:
    tts/<basename>/           directory of per-segment MP3s
    tts/<basename>/full.mp3   concatenated audio
    tts/<basename>/timing.json  start/end times per segment

Available voices (run: python3 -m edge_tts --list-voices | grep en-US):
    en-US-GuyNeural      (male, natural)
    en-US-AndrewNeural   (male, warm)
    en-US-BrianNeural    (male, clear)
    en-US-JennyNeural    (female, natural)
"""
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path


def extract_voice_blocks(text: str) -> list[dict]:
    """Extract VOICE sections and their context from tutorial markdown."""
    segments = []
    # Split on ## headers
    parts = re.split(r'^## ', text, flags=re.MULTILINE)

    for part in parts:
        if not part.startswith('VOICE'):
            continue

        # Get the voice text (strip the header line)
        lines = part.split('\n', 1)
        header = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ''

        if not body:
            continue

        # Check if it's VOICE [OVER SCREEN]
        over_screen = 'OVER SCREEN' in header

        # Process the body — handle [PAUSE Ns] markers
        # Split on PAUSE markers to create sub-segments
        pause_pattern = r'\[PAUSE\s+(\d+)s?\]'
        sub_parts = re.split(pause_pattern, body)

        i = 0
        while i < len(sub_parts):
            text_chunk = sub_parts[i].strip()
            if text_chunk and not text_chunk.startswith('#'):
                # Clean markdown formatting
                text_chunk = re.sub(r'\*\*(.+?)\*\*', r'\1', text_chunk)  # bold
                text_chunk = re.sub(r'`(.+?)`', r'\1', text_chunk)  # code
                text_chunk = text_chunk.replace('—', ' — ')
                text_chunk = text_chunk.replace('"', '"').replace('"', '"')

                segments.append({
                    'text': text_chunk,
                    'over_screen': over_screen,
                })

            # Check for pause after this chunk
            if i + 1 < len(sub_parts):
                try:
                    pause_secs = float(sub_parts[i + 1])
                    segments.append({
                        'text': None,
                        'pause': pause_secs,
                    })
                    i += 2
                    continue
                except (ValueError, IndexError):
                    pass
            i += 1

    return segments


async def generate_tts(text: str, output_path: Path, voice: str, rate: str) -> None:
    """Generate TTS audio for a text segment."""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(str(output_path))


def generate_silence(duration: float, output_path: Path) -> None:
    """Generate a silent MP3 of given duration."""
    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi',
        '-i', f'anullsrc=r=24000:cl=mono',
        '-t', str(duration),
        '-c:a', 'libmp3lame', '-q:a', '9',
        str(output_path),
    ], capture_output=True)


def get_mp3_duration(path: Path) -> float:
    """Get duration of an MP3 file."""
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_format', '-print_format', 'json', str(path)],
        capture_output=True, text=True,
    )
    info = json.loads(result.stdout)
    return float(info['format']['duration'])


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Generate TTS from tutorial markdown')
    parser.add_argument('input', help='Tutorial markdown file')
    parser.add_argument('--voice', default='en-US-AndrewNeural', help='Edge TTS voice')
    parser.add_argument('--rate', default='+0%%', help='Speech rate (e.g. +10%%, -5%%)')
    args = parser.parse_args()

    input_path = Path(args.input)
    text = input_path.read_text()
    basename = input_path.stem

    # Output directory
    out_dir = Path('tts') / basename
    out_dir.mkdir(parents=True, exist_ok=True)

    # Extract voice segments
    segments = extract_voice_blocks(text)
    if not segments:
        print(f'No VOICE blocks found in {input_path}')
        sys.exit(1)

    print(f'Input:    {input_path}')
    print(f'Voice:    {args.voice}')
    print(f'Rate:     {args.rate}')
    print(f'Segments: {len(segments)}')
    print()

    # Generate audio for each segment
    timing = []
    part_files = []
    current_time = 0.0

    for i, seg in enumerate(segments):
        part_path = out_dir / f'part_{i:03d}.mp3'

        if seg.get('pause'):
            # Silence segment
            dur = seg['pause']
            generate_silence(dur, part_path)
            print(f'  {i:3d}: [silence {dur}s]')
            timing.append({
                'index': i,
                'type': 'pause',
                'start': current_time,
                'end': current_time + dur,
                'duration': dur,
            })
            current_time += dur
        else:
            # TTS segment
            text = seg['text']
            asyncio.run(generate_tts(text, part_path, args.voice, args.rate))
            dur = get_mp3_duration(part_path)
            preview = text[:60] + ('...' if len(text) > 60 else '')
            print(f'  {i:3d}: {dur:5.1f}s  {preview}')
            timing.append({
                'index': i,
                'type': 'voice',
                'text': text,
                'over_screen': seg.get('over_screen', False),
                'start': current_time,
                'end': current_time + dur,
                'duration': dur,
            })
            current_time += dur

        part_files.append(part_path)

    # Concatenate all parts
    concat_path = out_dir / 'concat.txt'
    with open(concat_path, 'w') as f:
        for p in part_files:
            f.write(f"file '{p.name}'\n")

    full_path = out_dir / 'full.mp3'
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', str(concat_path),
        '-c:a', 'libmp3lame', '-q:a', '2',
        str(full_path),
    ], capture_output=True)
    concat_path.unlink()

    total_dur = get_mp3_duration(full_path)
    print(f'\nTotal:    {total_dur:.1f}s')
    print(f'Output:   {full_path}')

    # Write timing metadata
    timing_path = out_dir / 'timing.json'
    with open(timing_path, 'w') as f:
        json.dump({
            'source': str(input_path),
            'voice': args.voice,
            'rate': args.rate,
            'total_duration': total_dur,
            'segments': timing,
        }, f, indent=2)
    print(f'Timing:   {timing_path}')


if __name__ == '__main__':
    main()
