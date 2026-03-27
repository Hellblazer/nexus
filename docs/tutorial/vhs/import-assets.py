#!/usr/bin/env python3
"""Generate FCPXML that imports all assets into FCP's media browser.

No timeline — just organizes clips into keyword collections so they
appear in FCP's browser ready to drag onto a timeline.

Usage:
    python3 import-assets.py assets/02-install-nexus assets/03-first-use-cli
"""
import json
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


def rational(secs: float, fps: int = 30) -> str:
    frames = round(secs * fps)
    return f'{frames * 100}/{fps * 100}s'


def main():
    if len(sys.argv) < 2:
        print(f'Usage: {sys.argv[0]} <assets-dir> [<assets-dir> ...]')
        sys.exit(1)

    asset_dirs = [Path(d) for d in sys.argv[1:]]

    root = Element('fcpxml', version='1.11')
    resources = SubElement(root, 'resources')

    # Video format
    SubElement(resources, 'format',
               id='r1', name='FFVideoFormat1080p30',
               frameDuration='100/3000s', width='1920', height='1080')

    library = SubElement(root, 'library')
    asset_id = 2

    for asset_dir in asset_dirs:
        section_name = asset_dir.name
        event = SubElement(library, 'event', name=section_name)

        # Import videos
        vid_dir = asset_dir / 'video'
        if vid_dir.exists():
            for mp4 in sorted(vid_dir.glob('*.mp4')):
                dur = get_duration(mp4)
                aid = f'r{asset_id}'
                asset_el = SubElement(resources, 'asset',
                                      id=aid, name=mp4.stem,
                                      start='0s', duration=rational(dur),
                                      hasVideo='1', hasAudio='0', format='r1')
                SubElement(asset_el, 'media-rep',
                           kind='original-media',
                           src=f'file://{mp4.resolve()}')

                # Add as clip in event
                SubElement(event, 'asset-clip',
                           ref=aid, name=mp4.stem,
                           start='0s', duration=rational(dur),
                           format='r1')

                print(f'  VIDEO  {dur:5.1f}s  {section_name}/video/{mp4.name}')
                asset_id += 1

        # Import audio
        aud_dir = asset_dir / 'audio'
        if aud_dir.exists():
            for mp3 in sorted(aud_dir.glob('*.mp3')):
                dur = get_duration(mp3)
                aid = f'r{asset_id}'
                asset_el = SubElement(resources, 'asset',
                                      id=aid, name=mp3.stem,
                                      start='0s', duration=rational(dur),
                                      hasVideo='0', hasAudio='1',
                                      audioSources='1', audioChannels='1',
                                      audioRate='24000')
                SubElement(asset_el, 'media-rep',
                           kind='original-media',
                           src=f'file://{mp3.resolve()}')

                SubElement(event, 'asset-clip',
                           ref=aid, name=mp3.stem,
                           start='0s', duration=rational(dur))

                print(f'  AUDIO  {dur:5.1f}s  {section_name}/audio/{mp3.name}')
                asset_id += 1

        print()

    output = Path('nexus-tutorial-assets.fcpxml')
    tree = ElementTree(root)
    indent(tree, space='  ')
    with open(output, 'wb') as f:
        tree.write(f, xml_declaration=True, encoding='UTF-8')

    print(f'Output: {output}')
    print(f'Open in FCP → all clips appear in the browser, organized by section.')


if __name__ == '__main__':
    main()
