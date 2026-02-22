"""AC7: Ripgrep line cache — path:line:content format and 500MB cap."""
from pathlib import Path

import pytest

from nexus.ripgrep_cache import build_cache, MAX_CACHE_SIZE


def test_cache_format_path_line_content(tmp_path: Path) -> None:
    """Each line is formatted as 'abspath:lineno:content'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    src = repo / "hello.py"
    src.write_text("line one\nline two\nline three\n")

    cache_path = tmp_path / "cache.txt"
    build_cache(repo, cache_path, [(1.0, src)])

    lines = cache_path.read_text().splitlines()
    assert len(lines) == 3
    assert lines[0] == f"{src}:1:line one"
    assert lines[1] == f"{src}:2:line two"
    assert lines[2] == f"{src}:3:line three"


def test_cache_multiple_files_high_frecency_first(tmp_path: Path) -> None:
    """Files are written in descending frecency order."""
    repo = tmp_path / "repo"
    repo.mkdir()
    high = repo / "high.py"
    high.write_text("high content\n")
    low = repo / "low.py"
    low.write_text("low content\n")

    cache_path = tmp_path / "cache.txt"
    build_cache(repo, cache_path, [(2.0, high), (0.5, low)])

    text = cache_path.read_text()
    assert text.index(str(high)) < text.index(str(low))


def test_cache_empty_file_list(tmp_path: Path) -> None:
    """Empty file list → empty cache file created."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cache_path = tmp_path / "cache.txt"

    build_cache(repo, cache_path, [])

    assert cache_path.exists()
    assert cache_path.read_text() == ""


def test_cache_cap_omits_low_frecency_files(tmp_path: Path, monkeypatch) -> None:
    """Files that would push cache past MAX_CACHE_SIZE are omitted."""
    repo = tmp_path / "repo"
    repo.mkdir()
    first = repo / "first.py"
    first.write_text("aaa\n")
    second = repo / "second.py"
    second.write_text("bbb\n")

    # Set cap so that after writing `first`, `second` is skipped
    monkeypatch.setattr("nexus.ripgrep_cache.MAX_CACHE_SIZE", 1)

    cache_path = tmp_path / "cache.txt"
    build_cache(repo, cache_path, [(2.0, first), (0.5, second)])

    content = cache_path.read_text()
    assert str(first) in content
    assert str(second) not in content


def test_cache_skips_binary_like_files(tmp_path: Path) -> None:
    """Files that can't be decoded as UTF-8 are skipped gracefully."""
    repo = tmp_path / "repo"
    repo.mkdir()
    binary_file = repo / "data.bin"
    binary_file.write_bytes(b"\x00\x01\x02\xff\xfe")
    good_file = repo / "good.py"
    good_file.write_text("hello\n")

    cache_path = tmp_path / "cache.txt"
    build_cache(repo, cache_path, [(1.0, binary_file), (0.5, good_file)])

    content = cache_path.read_text()
    assert str(good_file) in content
