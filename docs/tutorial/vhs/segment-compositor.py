#!/usr/bin/env python3
"""Segment-aware video+TTS compositor.

Parses a tutorial markdown file into an ordered sequence of blocks
(VOICE, SCREEN, VOICE OVER SCREEN, OVERLAY, PAUSE), generates TTS
for VOICE blocks, records each SCREEN block as a separate clip via
expect+asciinema, then composites everything:

  - VOICE (no screen): hold last frame (or black) for voice duration
  - SCREEN: play terminal clip at 1x
  - VOICE OVER SCREEN: adjust terminal clip speed to match voice duration
  - OVERLAY: render text card via Pillow, hold for voice duration
  - PAUSE: hold last frame for N seconds

Usage:
    uv run python segment-compositor.py <section.md> --container <name>

Requires: edge-tts, asciinema, agg, ffmpeg, Pillow, Docker
"""
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from dataclasses import dataclass, field

# ── Block types ──────────────────────────────────────────────────

@dataclass
class VoiceBlock:
    text: str
    over_screen: bool = False

@dataclass
class ScreenBlock:
    commands: list[str]
    duration_hint: float = 5.0  # from [Ns] in header

@dataclass
class OverlayBlock:
    text: str

@dataclass
class PauseBlock:
    duration: float

Block = VoiceBlock | ScreenBlock | OverlayBlock | PauseBlock

# ── Parser ───────────────────────────────────────────────────────

def parse_tutorial(text: str) -> list[Block]:
    """Parse tutorial markdown into ordered block sequence."""
    blocks: list[Block] = []
    parts = re.split(r'^## ', text, flags=re.MULTILINE)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        header_line = part.split('\n', 1)[0]
        body = part.split('\n', 1)[1].strip() if '\n' in part else ''

        if header_line.startswith('VOICE'):
            over_screen = 'OVER SCREEN' in header_line
            # Split on PAUSE markers within voice body
            pause_pat = r'\[PAUSE\s+(\d+)s?\]'
            sub_parts = re.split(pause_pat, body)
            for i, chunk in enumerate(sub_parts):
                if i % 2 == 0:
                    # Text chunk
                    chunk = chunk.strip()
                    # Remove sub-headers like ### Memory
                    chunk = re.sub(r'^###.*$', '', chunk, flags=re.MULTILINE).strip()
                    if chunk:
                        # Clean markdown
                        chunk = re.sub(r'\*\*(.+?)\*\*', r'\1', chunk)
                        chunk = re.sub(r'`(.+?)`', r'\1', chunk)
                        chunk = chunk.replace('\u2014', ' \u2014 ')
                        chunk = chunk.replace('\u201c', '"').replace('\u201d', '"')
                        blocks.append(VoiceBlock(text=chunk, over_screen=over_screen))
                else:
                    # Pause duration
                    blocks.append(PauseBlock(duration=float(chunk)))

        elif header_line.startswith('SCREEN'):
            # Extract duration hint from [Ns]
            dur_match = re.search(r'\[(\d+)s\]', header_line)
            dur = float(dur_match.group(1)) if dur_match else 5.0
            # Extract commands from code block
            code_match = re.search(r'```(?:bash)?\n(.*?)```', body, re.DOTALL)
            cmds = []
            if code_match:
                for line in code_match.group(1).strip().split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#'):
                        cmds.append(line)
            if cmds:
                blocks.append(ScreenBlock(commands=cmds, duration_hint=dur))

        elif header_line.startswith('OVERLAY'):
            if body:
                # Strip blockquote markers
                clean = re.sub(r'^>\s*', '', body, flags=re.MULTILINE).strip()
                blocks.append(OverlayBlock(text=clean))

    return blocks


# ── TTS ──────────────────────────────────────────────────────────

async def generate_tts(text: str, path: Path, voice: str, rate: str) -> float:
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate)
    await comm.save(str(path))
    return get_duration(path)


def generate_silence(duration: float, path: Path) -> None:
    subprocess.run([
        'ffmpeg', '-y', '-f', 'lavfi',
        '-i', f'anullsrc=r=24000:cl=mono',
        '-t', str(duration),
        '-c:a', 'libmp3lame', '-q:a', '9',
        str(path),
    ], capture_output=True)


# ── Screen recording ─────────────────────────────────────────────

def record_screen_clip(
    commands: list[str], container: str, clip_path: Path,
    cols: int = 120, rows: int = 35,
) -> float:
    """Record commands in container via expect+asciinema, render to MP4."""
    tmp = Path(tempfile.mkdtemp(prefix='seg_'))
    expect_path = tmp / 'clip.expect'
    cast_path = tmp / 'clip.cast'
    gif_path = tmp / 'clip.gif'

    # Write expect script
    expect_lines = [
        '#!/usr/bin/expect -f',
        'set timeout 120',
        r'set prompt {\$ $}',
        'log_user 0',
        f'spawn docker exec -it {container} bash',
        'expect -re $prompt',
        'log_user 1',
        'sleep 0.5',
    ]
    for cmd in commands:
        expect_lines.append(f'send "{cmd}\\r"')
        expect_lines.append('expect -re $prompt')
        expect_lines.append('sleep 1')
    expect_lines.append('send "exit\\r"')
    expect_lines.append('expect eof')
    expect_path.write_text('\n'.join(expect_lines))
    expect_path.chmod(0o755)

    # Record with asciinema
    subprocess.run([
        'asciinema', 'rec',
        f'--command={expect_path}',
        f'--cols={cols}', f'--rows={rows}',
        str(cast_path),
    ], capture_output=True)

    # Render to GIF
    subprocess.run([
        'agg', '--theme', 'monokai', '--font-size', '16',
        '--idle-time-limit', '1',
        str(cast_path), str(gif_path),
    ], capture_output=True)

    # Convert to MP4
    subprocess.run([
        'ffmpeg', '-y', '-i', str(gif_path),
        '-movflags', 'faststart', '-pix_fmt', 'yuv420p',
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
        str(clip_path),
    ], capture_output=True)

    # Cleanup
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()

    return get_duration(clip_path)


# ── Overlay rendering ────────────────────────────────────────────

def render_overlay(text: str, path: Path, width: int = 960, height: int = 540) -> None:
    """Render text overlay as a PNG with semi-transparent background."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.truetype('/System/Library/Fonts/Menlo.ttc', 18)
    except OSError:
        font = ImageFont.load_default()

    img = Image.new('RGBA', (width, height), (30, 30, 30, 240))
    draw = ImageDraw.Draw(img)

    # Word-wrap and draw
    margin = 40
    y = margin
    for line in text.split('\n'):
        draw.text((margin, y), line, font=font, fill=(255, 255, 255, 255))
        y += 28
    img.save(path)


def overlay_to_video(png_path: Path, duration: float, mp4_path: Path) -> None:
    """Convert a static PNG to an MP4 of given duration."""
    subprocess.run([
        'ffmpeg', '-y',
        '-loop', '1', '-i', str(png_path),
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
        '-pix_fmt', 'yuv420p',
        str(mp4_path),
    ], capture_output=True)


# ── Freeze frame ─────────────────────────────────────────────────

def freeze_last_frame(video_path: Path, duration: float, out_path: Path) -> None:
    """Extract last frame of video and hold it for duration."""
    tmp_frame = Path(tempfile.mktemp(suffix='.png'))
    # Extract last frame — try sseof first, fall back to seeking near end
    vid_dur = get_duration(video_path)
    seek = max(0, vid_dur - 0.1)
    subprocess.run([
        'ffmpeg', '-y', '-ss', str(seek), '-i', str(video_path),
        '-frames:v', '1', str(tmp_frame),
    ], capture_output=True)

    if not tmp_frame.exists():
        # Fallback: just grab first frame
        subprocess.run([
            'ffmpeg', '-y', '-i', str(video_path),
            '-frames:v', '1', str(tmp_frame),
        ], capture_output=True)

    if tmp_frame.exists():
        subprocess.run([
            'ffmpeg', '-y',
            '-loop', '1', '-i', str(tmp_frame),
            '-t', str(duration),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
            '-pix_fmt', 'yuv420p',
            str(out_path),
        ], capture_output=True)
        tmp_frame.unlink()
    else:
        # Last resort: stretch the video
        adjust_video_speed(video_path, duration, out_path)


# ── Utilities ────────────────────────────────────────────────────

def get_duration(path: Path) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_format', '-print_format', 'json', str(path)],
        capture_output=True, text=True,
    )
    info = json.loads(result.stdout)
    return float(info['format']['duration'])


def adjust_video_speed(input_path: Path, target_dur: float, output_path: Path) -> None:
    """Adjust video speed to match target duration."""
    actual = get_duration(input_path)
    speed = actual / target_dur
    subprocess.run([
        'ffmpeg', '-y', '-i', str(input_path),
        '-vf', f'setpts=PTS/{speed}',
        '-an', '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
        str(output_path),
    ], capture_output=True)


# ── Main compositor ──────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='Tutorial markdown file')
    parser.add_argument('--container', required=True, help='Docker container name')
    parser.add_argument('--voice', default='en-US-BrianNeural')
    parser.add_argument('--rate', default='+5%')
    args = parser.parse_args()

    input_path = Path(args.input)
    basename = input_path.stem
    blocks = parse_tutorial(input_path.read_text())

    print(f'Input:     {input_path}')
    print(f'Container: {args.container}')
    print(f'Voice:     {args.voice}')
    print(f'Blocks:    {len(blocks)}')
    print()

    work_dir = Path('segments') / basename
    work_dir.mkdir(parents=True, exist_ok=True)

    video_parts = []
    audio_parts = []
    last_video_path = None

    for i, block in enumerate(blocks):
        v_path = work_dir / f'v_{i:03d}.mp4'
        a_path = work_dir / f'a_{i:03d}.mp3'

        if isinstance(block, VoiceBlock):
            # Generate TTS
            dur = asyncio.run(generate_tts(block.text, a_path, args.voice, args.rate))
            preview = block.text[:50]
            tag = ' [OVER]' if block.over_screen else ''
            print(f'  {i:3d} VOICE{tag}  {dur:5.1f}s  {preview}...')

            if block.over_screen and last_video_path:
                # Adjust previous screen clip to match this voice duration
                adjust_video_speed(last_video_path, dur, v_path)
            elif last_video_path:
                # Freeze last frame for voice duration
                freeze_last_frame(last_video_path, dur, v_path)
            else:
                # No previous video — black frame
                overlay_to_video(
                    _black_frame(work_dir), dur, v_path)

            video_parts.append(v_path)
            audio_parts.append(a_path)

        elif isinstance(block, ScreenBlock):
            # Record screen clip
            print(f'  {i:3d} SCREEN  recording: {block.commands}')
            dur = record_screen_clip(block.commands, args.container, v_path)
            print(f'           {dur:5.1f}s recorded')
            last_video_path = v_path
            # No audio for screen-only blocks (paired with VOICE OVER SCREEN)

        elif isinstance(block, OverlayBlock):
            # Render overlay — shown during the NEXT voice segment
            png = work_dir / f'overlay_{i:03d}.png'
            render_overlay(block.text, png)
            print(f'  {i:3d} OVERLAY rendered')
            # Overlays are handled as static — skip for now
            # TODO: composite overlay PNG onto video

        elif isinstance(block, PauseBlock):
            # Silence + freeze frame
            generate_silence(block.duration, a_path)
            if last_video_path:
                freeze_last_frame(last_video_path, block.duration, v_path)
            else:
                overlay_to_video(_black_frame(work_dir), block.duration, v_path)
            print(f'  {i:3d} PAUSE   {block.duration}s')
            video_parts.append(v_path)
            audio_parts.append(a_path)

    # Concatenate all video parts
    print(f'\nSplicing {len(video_parts)} video segments...')
    final_video = work_dir / 'final_video.mp4'
    _concat_videos(video_parts, final_video)

    # Concatenate all audio parts
    final_audio = work_dir / 'final_audio.mp3'
    _concat_audios(audio_parts, final_audio)

    # Merge
    output = Path(f'{basename}-final.mp4')
    subprocess.run([
        'ffmpeg', '-y',
        '-i', str(final_video),
        '-i', str(final_audio),
        '-c:v', 'copy', '-c:a', 'aac',
        '-map', '0:v', '-map', '1:a',
        '-shortest',
        str(output),
    ], capture_output=True)

    dur = get_duration(output)
    size = output.stat().st_size
    print(f'\nDone: {output} ({dur:.1f}s, {size // 1024}K)')


def _black_frame(work_dir: Path) -> Path:
    p = work_dir / 'black.png'
    if not p.exists():
        from PIL import Image
        img = Image.new('RGB', (960, 540), (30, 30, 30))
        img.save(p)
    return p


def _concat_videos(parts: list[Path], output: Path) -> None:
    concat = output.with_suffix('.txt')
    with open(concat, 'w') as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', str(concat),
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
        str(output),
    ], capture_output=True)
    concat.unlink()


def _concat_audios(parts: list[Path], output: Path) -> None:
    concat = output.with_suffix('.txt')
    with open(concat, 'w') as f:
        for p in parts:
            f.write(f"file '{p.resolve()}'\n")
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', str(concat),
        '-c:a', 'libmp3lame', '-q:a', '2',
        str(output),
    ], capture_output=True)
    concat.unlink()


if __name__ == '__main__':
    main()
