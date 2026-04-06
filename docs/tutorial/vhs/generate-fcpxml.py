#!/usr/bin/env python3
"""Generate FCPXML project from tutorial markdown + recorded assets.

Model: the terminal is ALWAYS showing. Video clips play back-to-back,
each holding its last frame until the next clip starts. Voice audio
layers on top at the correct timestamps. No gaps, no black frames.

Usage:
    python3 generate-fcpxml.py <section.md> \
        --clips <dir-of-mp4s> \
        --tts <tts-dir/> \
        --output <section.fcpxml>
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent


def get_duration(path: Path) -> float:
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_format', '-print_format', 'json', str(path)],
        capture_output=True, text=True,
    )
    return float(json.loads(result.stdout)['format']['duration'])


def get_video_info(path: Path) -> dict:
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-show_streams', '-print_format', 'json', str(path)],
        capture_output=True, text=True,
    )
    for s in json.loads(result.stdout).get('streams', []):
        if s.get('codec_type') == 'video':
            return {'width': int(s['width']), 'height': int(s['height'])}
    return {'width': 1920, 'height': 1080}


def rational(secs: float, fps: int = 30) -> str:
    """Snap seconds to nearest frame boundary as rational."""
    frames = round(secs * fps)
    return f'{frames * 100}/{fps * 100}s'


# ── Parse tutorial into timeline events ──────────────────────────

def parse_timeline(md_text: str, voice_durations: list[float], clip_durations: list[float]) -> list[dict]:
    """Convert markdown blocks + durations into a flat timeline of events.

    Returns list of:
      {'type': 'screen', 'clip_idx': N, 'start': T, 'duration': D}
      {'type': 'audio',  'voice_idx': N, 'start': T, 'duration': D}
    """
    parts = re.split(r'^## ', md_text, flags=re.MULTILINE)

    # First pass: identify block sequence
    blocks = []
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
                        blocks.append({'type': 'voice', 'over': over})
                else:
                    blocks.append({'type': 'pause', 'duration': float(chunk)})
        elif header.startswith('SCREEN'):
            blocks.append({'type': 'screen'})
        elif header.startswith('OVERLAY'):
            blocks.append({'type': 'overlay'})

    # Second pass: build timeline
    # Strategy: walk blocks, collect pending voice-overs, attach to next screen
    events = []
    cursor = 0.0
    v_idx = 0
    c_idx = 0
    pending_over_voices = []  # voice-overs waiting for their screen

    for block in blocks:
        if block['type'] == 'voice':
            if v_idx >= len(voice_durations):
                v_idx += 1
                continue
            dur = voice_durations[v_idx]

            if block['over']:
                # VOICE OVER SCREEN: queue it, will be placed when SCREEN appears
                pending_over_voices.append({'voice_idx': v_idx, 'duration': dur})
            else:
                # Standalone voice: plays at current cursor, advances cursor
                events.append({
                    'type': 'audio',
                    'voice_idx': v_idx,
                    'start': cursor,
                    'duration': dur,
                })
                cursor += dur
            v_idx += 1

        elif block['type'] == 'screen':
            if c_idx >= len(clip_durations):
                c_idx += 1
                continue
            clip_dur = clip_durations[c_idx]
            screen_start = cursor

            # Screen duration = max(clip, sum of pending voice-overs)
            over_dur = sum(v['duration'] for v in pending_over_voices)
            actual_dur = max(clip_dur, over_dur)

            events.append({
                'type': 'screen',
                'clip_idx': c_idx,
                'start': screen_start,
                'duration': actual_dur,
            })

            # Place pending voice-overs starting at screen_start
            voice_cursor = screen_start
            for pv in pending_over_voices:
                events.append({
                    'type': 'audio',
                    'voice_idx': pv['voice_idx'],
                    'start': voice_cursor,
                    'duration': pv['duration'],
                })
                voice_cursor += pv['duration']
            pending_over_voices = []

            cursor = screen_start + actual_dur
            c_idx += 1

        elif block['type'] == 'pause':
            cursor += block['duration']

        elif block['type'] == 'overlay':
            pass

    # Flush any remaining pending voices
    for pv in pending_over_voices:
        events.append({
            'type': 'audio',
            'voice_idx': pv['voice_idx'],
            'start': cursor,
            'duration': pv['duration'],
        })
        cursor += pv['duration']

    return events


# ── FCPXML generation ────────────────────────────────────────────

def generate(
    project_name: str,
    timeline_events: list[dict],
    video_clips: list[Path],
    voice_files: list[Path],
) -> Element:
    root = Element('fcpxml', version='1.11')
    resources = SubElement(root, 'resources')

    # Format
    SubElement(resources, 'format',
               id='r1', name='FFVideoFormat1080p30',
               frameDuration='100/3000s', width='1920', height='1080')

    # Register video assets
    asset_id = 2
    v_assets = []
    for clip_path in video_clips:
        dur = get_duration(clip_path)
        aid = f'r{asset_id}'
        asset_el = SubElement(resources, 'asset',
                              id=aid, name=clip_path.stem,
                              start='0s', duration=rational(dur),
                              hasVideo='1', hasAudio='0', format='r1')
        SubElement(asset_el, 'media-rep',
                   kind='original-media',
                   src=f'file://{clip_path.resolve()}')
        v_assets.append({'id': aid, 'duration': dur})
        asset_id += 1

    # Register audio assets
    a_assets = []
    for audio_path in voice_files:
        if not audio_path.exists():
            a_assets.append(None)
            continue
        dur = get_duration(audio_path)
        aid = f'r{asset_id}'
        asset_el = SubElement(resources, 'asset',
                              id=aid, name=audio_path.stem,
                              start='0s', duration=rational(dur),
                              hasVideo='0', hasAudio='1',
                              audioSources='1', audioChannels='1', audioRate='24000')
        SubElement(asset_el, 'media-rep',
                   kind='original-media',
                   src=f'file://{audio_path.resolve()}')
        a_assets.append({'id': aid, 'duration': dur})
        asset_id += 1

    # Build timeline
    library = SubElement(root, 'library')
    event = SubElement(library, 'event', name='Tutorial')
    project = SubElement(event, 'project', name=project_name)
    sequence = SubElement(project, 'sequence',
                          format='r1', tcStart='0s', tcFormat='NDF',
                          audioRate='48k')
    spine = SubElement(sequence, 'spine')

    # Place screen clips on the main storyline (no gaps!)
    screen_events = sorted(
        [e for e in timeline_events if e['type'] == 'screen'],
        key=lambda e: e['start'],
    )

    for ev in screen_events:
        ci = ev['clip_idx']
        if ci >= len(v_assets):
            continue
        va = v_assets[ci]

        # The asset-clip on the spine
        ac = SubElement(spine, 'asset-clip',
                         ref=va['id'],
                         offset=rational(ev['start']),
                         duration=rational(ev['duration']),
                         tcFormat='NDF')

        # Attach any audio events that fall within this clip's time range
        clip_start = ev['start']
        clip_end = ev['start'] + ev['duration']
        for ae in timeline_events:
            if ae['type'] != 'audio':
                continue
            if ae['start'] >= clip_start - 0.01 and ae['start'] < clip_end:
                vi = ae['voice_idx']
                if vi < len(a_assets) and a_assets[vi]:
                    aa = a_assets[vi]
                    # Connected clip offset is relative to the parent clip's start
                    SubElement(ac, 'asset-clip',
                               ref=aa['id'],
                               lane='-1',
                               offset=rational(ae['start']),
                               duration=rational(ae['duration']))

    # Any audio events that DON'T fall within a screen clip get placed
    # as standalone connected clips on a gap
    screen_ranges = [(e['start'], e['start'] + e['duration']) for e in screen_events]
    for ae in timeline_events:
        if ae['type'] != 'audio':
            continue
        covered = any(s <= ae['start'] < e for s, e in screen_ranges)
        if not covered:
            vi = ae['voice_idx']
            if vi < len(a_assets) and a_assets[vi]:
                aa = a_assets[vi]
                # Need a gap on the spine to hold this audio
                gap = SubElement(spine, 'gap',
                                  offset=rational(ae['start']),
                                  duration=rational(ae['duration']))
                SubElement(gap, 'asset-clip',
                            ref=aa['id'],
                            lane='-1',
                            offset=rational(ae['start']),
                            duration=rational(ae['duration']))

    return root


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('input', help='Tutorial markdown file')
    parser.add_argument('--clips', required=True)
    parser.add_argument('--tts', required=True)
    parser.add_argument('--output', default=None)
    args = parser.parse_args()

    input_path = Path(args.input)
    clips_dir = Path(args.clips)
    tts_dir = Path(args.tts)
    basename = input_path.stem
    output_path = Path(args.output) if args.output else Path(f'{basename}.fcpxml')

    md_text = input_path.read_text()

    # Load timing
    with open(tts_dir / 'timing.json') as f:
        timing = json.load(f)

    # Voice durations (only voice segments, not pauses)
    voice_durations = [s['duration'] for s in timing['segments'] if s['type'] == 'voice']
    voice_files = sorted(p for p in sorted(tts_dir.glob('part_*.mp3'))
                         if not p.stem.endswith('_pause'))

    # Filter to only voice parts (skip silence parts)
    # The timing segments alternate voice and pause — match by index
    voice_part_files = []
    part_idx = 0
    for seg in timing['segments']:
        part_path = tts_dir / f'part_{part_idx:03d}.mp3'
        if seg['type'] == 'voice':
            voice_part_files.append(part_path)
        part_idx += 1

    # Video clips (sorted by name to match SCREEN block order)
    video_clips = sorted(clips_dir.glob('*.mp4'))
    clip_durations = [get_duration(p) for p in video_clips]

    print(f'Input:       {input_path}')
    print(f'Video clips: {len(video_clips)} ({sum(clip_durations):.1f}s)')
    print(f'Voice parts: {len(voice_durations)} ({sum(voice_durations):.1f}s)')
    print()

    # Build timeline
    events = parse_timeline(md_text, voice_durations, clip_durations)

    for ev in events:
        if ev['type'] == 'screen':
            print(f"  {ev['start']:5.1f}s  SCREEN clip {ev['clip_idx']} ({ev['duration']:.1f}s)")
        else:
            print(f"  {ev['start']:5.1f}s  AUDIO  voice {ev['voice_idx']} ({ev['duration']:.1f}s)")

    # Generate FCPXML
    root = generate(
        project_name=f'Nexus Tutorial - {basename}',
        timeline_events=events,
        video_clips=video_clips,
        voice_files=voice_part_files,
    )

    tree = ElementTree(root)
    indent(tree, space='  ')
    with open(output_path, 'wb') as f:
        tree.write(f, xml_declaration=True, encoding='UTF-8')

    print(f'\nOutput: {output_path}')


if __name__ == '__main__':
    main()
