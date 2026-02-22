# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ripgrep line cache: flat path:line:content file with 500MB soft cap."""
from pathlib import Path

MAX_CACHE_SIZE: int = 500 * 1024 * 1024  # 500 MB


def build_cache(
    repo: Path,
    cache_path: Path,
    files: list[tuple[float, Path]],
) -> None:
    """Write a ripgrep-compatible line cache for *repo*.

    *files* is a list of (frecency_score, path) sorted by descending frecency.
    Files are written in that order until the cumulative byte count after
    completing a file exceeds MAX_CACHE_SIZE; no mid-file truncation occurs.

    Each line is formatted as ``/abs/path/to/file:lineno:content``.
    Binary files (non-UTF-8) are skipped silently.
    """
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    written_bytes = 0

    with cache_path.open("w", encoding="utf-8") as fh:
        for _score, file in files:
            if written_bytes >= MAX_CACHE_SIZE:
                break  # Soft cap reached — omit remaining files

            try:
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # Skip binary or unreadable files

            for lineno, line in enumerate(text.splitlines(), start=1):
                entry = f"{file}:{lineno}:{line}\n"
                fh.write(entry)
                written_bytes += len(entry.encode("utf-8"))
