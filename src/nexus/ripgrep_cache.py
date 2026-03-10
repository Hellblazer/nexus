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


_DEFAULT_RIPGREP_TIMEOUT: int = 10


def search_ripgrep(
    query: str,
    cache_path: Path,
    *,
    n_results: int = 50,
    fixed_strings: bool = True,
    timeout: int = _DEFAULT_RIPGREP_TIMEOUT,
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
      - frecency_score (float, position-based: 1.0 for first result, decaying toward 0)

    Returns [] if *cache_path* does not exist, ``rg`` is not installed, or the
    subprocess times out.

    *timeout* defaults to 10 s; override via TuningConfig.ripgrep_timeout.
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
            timeout=timeout,
        )
    except FileNotFoundError:
        _log.warning("rg not found — skipping ripgrep hybrid search")
        return []
    except subprocess.TimeoutExpired:
        _log.warning("rg timed out after %d s — skipping ripgrep results", timeout)
        return []

    if proc.returncode == 2:
        _log.warning("rg exited with error", stderr=proc.stderr[:200] if proc.stderr else "")
        return []

    parsed: list[dict] = []
    for raw_line in proc.stdout.splitlines():
        # Each matched line is a cache entry: /abs/path:lineno:content
        # Split on ":" with maxsplit=2 to get exactly three parts.
        # This assumes the file path does NOT contain colons; paths with
        # colons (rare, but legal on some filesystems) will be misparsed.
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        file_path_str, lineno_str, line_content = parts
        try:
            line_number = int(lineno_str)
        except ValueError:
            continue
        parsed.append({
            "file_path": file_path_str,
            "line_number": line_number,
            "line_content": line_content,
        })

    # Position-based frecency: the cache file is ordered by descending
    # frecency, so ripgrep (which scans sequentially) returns higher-frecency
    # matches first.  Score decays from 1.0 (first hit) toward 0.0 (last).
    n = len(parsed)
    for i, hit in enumerate(parsed):
        hit["frecency_score"] = 1.0 - (i / n) if n > 1 else 1.0

    return parsed
