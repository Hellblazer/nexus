#!/usr/bin/env python3
"""POC: Composite a VHS MP4 with text overlays using Pillow + ffmpeg.

Generates transparent PNG overlays from text, then uses ffmpeg's overlay
filter to burn them onto the video at specified time ranges.

Usage:
    uv run python poc-composite.py poc-test.mp4

Produces: poc-test-composited.mp4
"""
import subprocess
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

VIDEO_WIDTH = 1200
VIDEO_HEIGHT = 600

# Overlay definitions: (text, start_sec, end_sec, position, font_size)
# position: "top-center", "bottom-center"
OVERLAYS = [
    ("Nexus Tutorial — POC Test", 0, 5, "top-center", 28),
    ("cheatsheet: github.com/Hellblazer/nexus", 15, 25, "bottom-center", 18),
]


def make_overlay_png(text: str, font_size: int, output_path: Path) -> None:
    """Generate a transparent PNG with white text on semi-transparent black."""
    # Use default font (always available)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", font_size)
    except OSError:
        font = ImageFont.load_default()

    # Measure text
    dummy = Image.new("RGBA", (1, 1))
    draw = ImageDraw.Draw(dummy)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    # Create image with padding
    pad = 20
    img = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Semi-transparent black background
    draw.rounded_rectangle(
        [(0, 0), (text_w + pad * 2, text_h + pad * 2)],
        radius=8,
        fill=(0, 0, 0, 160),
    )
    # White text
    draw.text((pad, pad), text, font=font, fill=(255, 255, 255, 255))
    img.save(output_path)


def position_xy(pos: str, overlay_w: int, overlay_h: int) -> tuple[str, str]:
    """Return ffmpeg overlay x,y expressions."""
    if pos == "top-center":
        return f"(main_w-overlay_w)/2", "30"
    elif pos == "bottom-center":
        return f"(main_w-overlay_w)/2", f"main_h-overlay_h-30"
    return "10", "10"


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input.mp4>")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_path = input_path.with_stem(input_path.stem + "-composited")

    print(f"=== POC Composite (Pillow + ffmpeg) ===")
    print(f"Input:  {input_path}")
    print(f"Output: {output_path}")
    print()

    # Generate overlay PNGs
    overlay_files = []
    for i, (text, start, end, pos, size) in enumerate(OVERLAYS):
        png_path = Path(f"/tmp/overlay_{i}.png")
        make_overlay_png(text, size, png_path)
        overlay_files.append((png_path, start, end, pos))
        print(f"  Generated: {png_path} ({text[:40]}...)")

    # Build ffmpeg command
    # Each overlay is a separate input with enable time range
    inputs = ["-i", str(input_path)]
    for png_path, _, _, _ in overlay_files:
        inputs.extend(["-i", str(png_path)])

    # Build filter chain
    # Start with [0:v] (the video), overlay each PNG in sequence
    filters = []
    prev = "[0:v]"
    for i, (_, start, end, pos) in enumerate(overlay_files):
        x, y = position_xy(pos, 0, 0)
        enable = f"between(t\\,{start}\\,{end})"
        if i < len(overlay_files) - 1:
            tag = f"[v{i}]"
            filters.append(f"{prev}[{i+1}:v]overlay={x}:{y}:enable='{enable}'{tag}")
            prev = tag
        else:
            # Last overlay — no output tag (we append [vout] in the command)
            filters.append(f"{prev}[{i+1}:v]overlay={x}:{y}:enable='{enable}'")

    filter_str = ";".join(filters)

    # The last filter output tag becomes the video stream
    # Map it explicitly with [tag] syntax
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_str + "[vout]",
        "-map", "[vout]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-c:a", "copy",
        str(output_path),
    ]

    print()
    print(f"  Running ffmpeg...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ffmpeg FAILED:\n{result.stderr[-500:]}")
        sys.exit(1)

    size = output_path.stat().st_size
    print(f"  ✓ Output: {output_path} ({size // 1024}K)")
    print()
    print("To add TTS audio:")
    print(f"  ffmpeg -i {output_path} -i voice.mp3 -c:v copy -c:a aac -map 0:v -map 1:a final.mp4")


if __name__ == "__main__":
    main()
