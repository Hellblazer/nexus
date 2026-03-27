#!/usr/bin/env python3
"""Post-process recordings with variable speed and fade transitions.

Takes a video and a speed map (time ranges with multipliers + optional
fade transitions), then produces output where boring parts zip by,
interesting parts play at normal speed, and steps are separated by fades.

Usage:
    uv run python poc-speedmap.py <input.mp4> <speedmap.toml>

Speed map format (TOML):
    fade_duration = 0.5   # seconds, default 0.0 (no fades)

    [[segment]]
    start = 0.0
    end = 4.0
    speed = 1.0
    label = "install uv"

    [[segment]]
    start = 4.0
    end = 8.0
    speed = 3.0
    fade = true           # fade out at end, fade in at start of next
    label = "uv install output (fast)"

Segments must be contiguous and cover the full duration. Gaps are
filled at 1x speed automatically.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]


def get_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_format", "-print_format", "json", str(path)],
        capture_output=True, text=True,
    )
    import json
    info = json.loads(result.stdout)
    return float(info["format"]["duration"])


def build_speed_segments(
    segments: list[dict], duration: float
) -> list[tuple[float, float, float, str, bool]]:
    """Fill gaps and return complete list of (start, end, speed, label, fade)."""
    # Resolve negative offsets (from end) to absolute times
    resolved = []
    for seg in segments:
        start = float(seg["start"])
        end = float(seg["end"])
        if start < 0:
            start = max(0, duration + start)
        if end < 0:
            end = max(0, duration + end)
        end = min(end, duration)
        resolved.append({**seg, "start": start, "end": end})

    resolved = sorted(resolved, key=lambda s: s["start"])

    result: list[tuple[float, float, float, str, bool]] = []
    cursor = 0.0

    for seg in resolved:
        start = float(seg["start"])
        end = float(seg["end"])
        speed = float(seg["speed"])
        label = seg.get("label", "")
        fade = bool(seg.get("fade", False))

        if start > cursor + 0.1:
            result.append((cursor, start, 1.0, "", False))

        if end > start:
            result.append((start, end, speed, label, fade))
            cursor = end

    if cursor < duration - 0.1:
        result.append((cursor, duration, 1.0, "", False))

    return result


def process_video(input_path: Path, speedmap_path: Path) -> Path:
    output_path = input_path.with_stem(input_path.stem + "-speedmapped")
    duration = get_duration(input_path)

    with open(speedmap_path, "rb") as f:
        config = tomllib.load(f)

    fade_dur = float(config.get("fade_duration", 0.0))
    segments = build_speed_segments(config.get("segment", []), duration)

    print(f"Input:    {input_path} ({duration:.1f}s)")
    print(f"Segments: {len(segments)}")
    if fade_dur > 0:
        print(f"Fade:     {fade_dur}s")
    print()

    # Calculate expected output duration
    total_out = 0.0
    for start, end, speed, label, fade in segments:
        seg_dur = (end - start) / speed
        total_out += seg_dur
        speed_str = f"{speed:.0f}x" if speed >= 2 else f"{speed}x"
        tag = f"  ({label})" if label else ""
        fade_tag = " [fade]" if fade else ""
        print(f"  {start:6.1f}s - {end:6.1f}s  @ {speed_str:>4s} → {seg_dur:5.1f}s{tag}{fade_tag}")

    print(f"\nExpected output: ~{total_out:.1f}s")

    # Strategy: extract each segment, apply speed + fade, concatenate
    tmp_dir = Path(tempfile.mkdtemp(prefix="speedmap_"))
    part_files = []

    for i, (start, end, speed, _label, fade) in enumerate(segments):
        part_path = tmp_dir / f"part_{i:03d}.mp4"
        part_files.append(part_path)

        seg_dur = (end - start) / speed
        pts_filter = f"setpts=(PTS-STARTPTS)/{speed}"

        # Build filter chain
        vf_parts = [pts_filter]
        if fade and fade_dur > 0:
            # Fade out at end of this segment
            fade_start = max(0, seg_dur - fade_dur)
            vf_parts.append(f"fade=t=out:st={fade_start}:d={fade_dur}")
        # Fade in at start of segment that follows a fade
        if i > 0 and segments[i - 1][4] and fade_dur > 0:
            vf_parts.append(f"fade=t=in:st=0:d={fade_dur}")

        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start),
            "-to", str(end),
            "-i", str(input_path),
            "-vf", ",".join(vf_parts),
            "-an",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            str(part_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  ERROR on segment {i}: {result.stderr[-300:]}")
            sys.exit(1)

    # Build concat file
    concat_path = tmp_dir / "concat.txt"
    with open(concat_path, "w") as f:
        for part in part_files:
            f.write(f"file '{part}'\n")

    # Concatenate all parts
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_path),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Concat ERROR: {result.stderr[-300:]}")
        sys.exit(1)

    # Clean up
    for p in part_files:
        p.unlink()
    concat_path.unlink()
    tmp_dir.rmdir()

    out_size = output_path.stat().st_size
    out_dur = get_duration(output_path)
    print(f"\nOutput: {output_path} ({out_dur:.1f}s, {out_size // 1024}K)")
    return output_path


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <input.mp4> <speedmap.toml>")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    speedmap_path = Path(sys.argv[2])
    process_video(input_path, speedmap_path)


if __name__ == "__main__":
    main()
