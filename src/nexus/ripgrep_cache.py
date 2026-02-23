# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ripgrep line cache: flat path:line:content file with 500MB soft cap."""
import subprocess
from pathlib import Path

import structlog

_log = structlog.get_logger()

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
                # Approximate: counts UTF-8 bytes of the Python string, not actual
                # on-disk bytes (text mode may apply newline translation on Windows).
                # The 500 MB cap is intentionally approximate.
                written_bytes += len(entry.encode("utf-8"))


def search_ripgrep(
    query: str,
    cache_path: Path,
    *,
    n_results: int = 50,
    fixed_strings: bool = True,
) -> list[dict]:
    """Run ripgrep against the line cache and return parsed hits.

    Each line in the cache has the format ``/abs/path/to/file:lineno:content``.
    ripgrep is invoked without ``--line-number`` (line numbers are embedded in
    each cache entry) and with ``--no-filename`` so each output line is just the
    raw cache entry that matched.

    Returns a list of dicts with keys:
      - file_path (str)
      - line_number (int)
      - line_content (str)
      - frecency_score (float, always 0.5 — cache ordering encodes frecency)

    Returns [] if *cache_path* does not exist, ``rg`` is not installed, or the
    subprocess times out.
    """
    if not cache_path.exists():
        return []

    cmd: list[str] = ["rg"]
    if fixed_strings:
        cmd.append("--fixed-strings")
    cmd += [
        "--no-filename",
        "--no-line-number",
        "-m", str(n_results),
        query,
        str(cache_path),
    ]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        _log.warning("rg not found — skipping ripgrep hybrid search")
        return []
    except subprocess.TimeoutExpired:
        _log.warning("rg timed out after 10 s — skipping ripgrep results")
        return []

    results: list[dict] = []
    for raw_line in proc.stdout.splitlines():
        # Each matched line is a cache entry: /abs/path:lineno:content
        # Split on ":" at most twice to isolate path and lineno.
        # Paths may contain colons on exotic filesystems; the lineno field
        # is always the *second* colon-delimited token that parses as int.
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path_str, lineno_str, line_content = parts
        try:
            line_number = int(lineno_str)
        except ValueError:
            continue
        results.append({
            "file_path": file_path_str,
            "line_number": line_number,
            "line_content": line_content,
            # Hardcoded midpoint score. Ripgrep cache results rely on cache
            # ordering (not this score) for relevance. If ever used for ranking
            # against semantic search, consider computing a position-based score.
            "frecency_score": 0.5,
        })

    return results
