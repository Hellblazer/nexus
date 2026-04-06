#!/usr/bin/env python3
"""Merge TTS audio with video, using timing.json to sync.

Takes a video + TTS timing file, adjusts video speed to match narration,
and produces a final video with audio.

Usage:
    uv run python tts-composite.py <video.mp4> <tts-dir/>

Example:
    uv run python tts-composite.py poc-install.mp4 tts/02-install-nexus/

The TTS timing.json maps voice segments to the narration. This script
uses ffmpeg to merge the audio track onto the video.

For now this does a simple merge — the video and audio are combined
as-is. Future versions will use the timing data to adjust video speed
per segment.
"""
import json
import subprocess
import sys
from pathlib import Path


def get_duration(path: Path) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_format', '-print_format', 'json', str(path)],
        capture_output=True, text=True,
    )
    info = json.loads(result.stdout)
    return float(info['format']['duration'])


def main():
    if len(sys.argv) < 3:
        print(f'Usage: {sys.argv[0]} <video.mp4> <tts-dir/>')
        sys.exit(1)

    video_path = Path(sys.argv[1])
    tts_dir = Path(sys.argv[2])
    audio_path = tts_dir / 'full.mp3'
    timing_path = tts_dir / 'timing.json'
    output_path = video_path.with_stem(video_path.stem + '-with-audio')

    if not audio_path.exists():
        print(f'No full.mp3 in {tts_dir}')
        sys.exit(1)

    video_dur = get_duration(video_path)
    audio_dur = get_duration(audio_path)

    print(f'Video:    {video_path} ({video_dur:.1f}s)')
    print(f'Audio:    {audio_path} ({audio_dur:.1f}s)')
    print(f'Output:   {output_path}')
    print()

    if timing_path.exists():
        with open(timing_path) as f:
            timing = json.load(f)
        voice_segs = [s for s in timing['segments'] if s['type'] == 'voice']
        print(f'Voice segments: {len(voice_segs)}')
        for s in voice_segs:
            preview = s.get('text', '')[:50]
            print(f"  {s['start']:5.1f}s - {s['end']:5.1f}s  {preview}...")
        print()

    # Determine strategy based on duration mismatch
    ratio = audio_dur / video_dur if video_dur > 0 else 1.0
    if ratio > 1.5:
        print(f'Audio is {ratio:.1f}x longer than video — video will be slowed')
    elif ratio < 0.67:
        print(f'Video is {1/ratio:.1f}x longer than audio — video will be sped up')
    else:
        print(f'Duration ratio: {ratio:.2f} — close enough for direct merge')

    # Simple merge: use the longer duration, pad the shorter one
    # -shortest would cut to the shorter, but we want both complete
    if audio_dur > video_dur:
        # Slow video to match audio length
        speed = video_dur / audio_dur
        cmd = [
            'ffmpeg', '-y',
            '-i', str(video_path),
            '-i', str(audio_path),
            '-filter_complex',
            f'[0:v]setpts=PTS/{speed}[v]',
            '-map', '[v]',
            '-map', '1:a',
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
            '-c:a', 'aac',
            '-shortest',
            str(output_path),
        ]
    elif video_dur > audio_dur:
        # Speed up video to match audio, or just merge and let video run longer
        cmd = [
            'ffmpeg', '-y',
            '-i', str(video_path),
            '-i', str(audio_path),
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-map', '0:v',
            '-map', '1:a',
            '-shortest',
            str(output_path),
        ]
    else:
        # Direct merge
        cmd = [
            'ffmpeg', '-y',
            '-i', str(video_path),
            '-i', str(audio_path),
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-map', '0:v',
            '-map', '1:a',
            str(output_path),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'ffmpeg error: {result.stderr[-500:]}')
        sys.exit(1)

    out_dur = get_duration(output_path)
    out_size = output_path.stat().st_size
    print(f'\nDone: {output_path} ({out_dur:.1f}s, {out_size // 1024}K)')


if __name__ == '__main__':
    main()
