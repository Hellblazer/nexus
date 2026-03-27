#!/usr/bin/env python3
"""Generate all raw assets for a tutorial section.

Produces a directory of named clips and audio files ready to drop into
Final Cut Pro (or any NLE). Also writes a manifest showing the intended
sequence.

Usage:
    python3 generate-assets.py <section.md> --container <name>

Output:
    assets/<basename>/
        video/
            01-uv-tool-install-conexus.mp4
            02-nx-version.mp4
            ...
        audio/
            01-intro-voice.mp3
            02-install-voiceover.mp3
            ...
        manifest.txt   (human-readable assembly guide)
"""
import asyncio
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def get_duration(path: Path) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_format', '-print_format', 'json', str(path)],
        capture_output=True, text=True,
    )
    return float(json.loads(result.stdout)['format']['duration'])


async def generate_tts(text: str, path: Path, voice: str, rate: str) -> float:
    import edge_tts
    comm = edge_tts.Communicate(text, voice, rate=rate)
    await comm.save(str(path))
    return get_duration(path)


def record_clip(commands: list[str], container: str, output: Path) -> float:
    """Record commands in container → MP4."""
    tmp = Path(tempfile.mkdtemp(prefix='rec_'))
    expect_path = tmp / 'clip.expect'
    cast_path = tmp / 'clip.cast'
    gif_path = tmp / 'clip.gif'

    lines = [
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
        cmd_esc = cmd.replace('"', '\\"')
        lines.append(f'send "{cmd_esc}\\r"')
        lines.append('expect -re $prompt')
        lines.append('sleep 1')
    lines.append('send "exit\\r"')
    lines.append('expect eof')
    expect_path.write_text('\n'.join(lines))
    expect_path.chmod(0o755)

    subprocess.run([
        'asciinema', 'rec', f'--command={expect_path}',
        '--cols=120', '--rows=35', '--overwrite',
        str(cast_path),
    ], capture_output=True)

    subprocess.run([
        'agg', '--theme', 'monokai', '--font-size', '16',
        '--idle-time-limit', '1',
        str(cast_path), str(gif_path),
    ], capture_output=True)

    subprocess.run([
        'ffmpeg', '-y', '-i', str(gif_path),
        '-movflags', 'faststart', '-pix_fmt', 'yuv420p',
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
        str(output),
    ], capture_output=True)

    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()

    return get_duration(output)


def slugify(text: str, max_len: int = 40) -> str:
    """Convert text to a filename-safe slug."""
    s = text.lower()
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s]+', '-', s).strip('-')
    return s[:max_len]


def parse_blocks(text: str) -> list[dict]:
    """Parse markdown into ordered blocks."""
    blocks = []
    parts = re.split(r'^## ', text, flags=re.MULTILINE)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        header = part.split('\n')[0]
        body = part.split('\n', 1)[1].strip() if '\n' in part else ''

        if header.startswith('VOICE'):
            over = 'OVER SCREEN' in header
            pause_pat = r'\[PAUSE\s+(\d+)s?\]'
            sub_parts = re.split(pause_pat, body)
            for i, chunk in enumerate(sub_parts):
                if i % 2 == 0:
                    chunk = re.sub(r'^###.*$', '', chunk, flags=re.MULTILINE).strip()
                    if chunk:
                        clean = re.sub(r'\*\*(.+?)\*\*', r'\1', chunk)
                        clean = re.sub(r'`(.+?)`', r'\1', clean)
                        clean = clean.replace('\u2014', ' \u2014 ')
                        clean = clean.replace('\u201c', '"').replace('\u201d', '"')
                        blocks.append({
                            'type': 'voice',
                            'over': over,
                            'text': clean,
                        })
                else:
                    blocks.append({'type': 'pause', 'duration': float(chunk)})

        elif header.startswith('SCREEN'):
            code = re.search(r'```(?:bash)?\n(.*?)```', body, re.DOTALL)
            cmds = []
            if code:
                for line in code.group(1).strip().split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#') and not line.startswith('cd '):
                        cmds.append(line)
            if cmds:
                blocks.append({'type': 'screen', 'commands': cmds})

        elif header.startswith('OVERLAY'):
            if body:
                clean = re.sub(r'^>\s*', '', body, flags=re.MULTILINE).strip()
                blocks.append({'type': 'overlay', 'text': clean})
    return blocks


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='Tutorial markdown file')
    parser.add_argument('--container', required=True)
    parser.add_argument('--voice', default='en-US-BrianNeural')
    parser.add_argument('--rate', default='+5%')
    args = parser.parse_args()

    input_path = Path(args.input)
    basename = input_path.stem
    blocks = parse_blocks(input_path.read_text())

    out_dir = Path('assets') / basename
    vid_dir = out_dir / 'video'
    aud_dir = out_dir / 'audio'
    vid_dir.mkdir(parents=True, exist_ok=True)
    aud_dir.mkdir(parents=True, exist_ok=True)

    print(f'Section:   {basename}')
    print(f'Container: {args.container}')
    print(f'Output:    {out_dir}/')
    print()

    manifest_lines = [f'# {basename} — Assembly Guide', '']
    seq = 0
    v_seq = 0
    s_seq = 0

    for block in blocks:
        seq += 1

        if block['type'] == 'voice':
            v_seq += 1
            tag = 'voiceover' if block['over'] else 'voice'
            slug = slugify(block['text'][:30])
            filename = f'{v_seq:02d}-{tag}-{slug}.mp3'
            filepath = aud_dir / filename

            dur = asyncio.run(generate_tts(block['text'], filepath, args.voice, args.rate))
            print(f'  {seq:2d}. VOICE{"[OVER]" if block["over"] else "":7s} {dur:5.1f}s  {filename}')
            manifest_lines.append(
                f'{seq:2d}. {"VOICE OVER" if block["over"] else "VOICE":12s}  '
                f'{dur:5.1f}s  audio/{filename}'
            )
            manifest_lines.append(f'    "{block["text"][:80]}..."')

        elif block['type'] == 'screen':
            s_seq += 1
            cmd_str = ' && '.join(block['commands'])
            slug = slugify(cmd_str[:30])
            filename = f'{s_seq:02d}-{slug}.mp4'
            filepath = vid_dir / filename

            dur = record_clip(block['commands'], args.container, filepath)
            print(f'  {seq:2d}. SCREEN        {dur:5.1f}s  {filename}')
            manifest_lines.append(
                f'{seq:2d}. SCREEN        {dur:5.1f}s  video/{filename}'
            )
            manifest_lines.append(f'    Commands: {cmd_str}')

        elif block['type'] == 'overlay':
            print(f'  {seq:2d}. OVERLAY       (add in FCP)')
            manifest_lines.append(f'{seq:2d}. OVERLAY       (add title in FCP)')
            manifest_lines.append(f'    "{block["text"][:80]}..."')

        elif block['type'] == 'pause':
            print(f'  {seq:2d}. PAUSE         {block["duration"]}s')
            manifest_lines.append(f'{seq:2d}. PAUSE         {block["duration"]}s')

        manifest_lines.append('')

    # Write manifest
    manifest_path = out_dir / 'manifest.txt'
    manifest_path.write_text('\n'.join(manifest_lines))

    print(f'\n  Manifest: {manifest_path}')
    print(f'  Videos:   {vid_dir}/ ({s_seq} clips)')
    print(f'  Audio:    {aud_dir}/ ({v_seq} files)')
    print(f'\nDrop {out_dir}/ into FCP and arrange per manifest.')


if __name__ == '__main__':
    main()
