"""AC7: Ripgrep line cache — path:line:content format and 500MB cap."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.ripgrep_cache import build_cache, MAX_CACHE_SIZE, search_ripgrep


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


# ── search_ripgrep tests ──────────────────────────────────────────────────────


def test_search_ripgrep_returns_empty_when_cache_missing(tmp_path: Path) -> None:
    """search_ripgrep returns [] when the cache file does not exist."""
    missing = tmp_path / "nonexistent.cache"
    results = search_ripgrep("hello", missing)
    assert results == []


def test_search_ripgrep_returns_empty_when_rg_not_installed(tmp_path: Path) -> None:
    """search_ripgrep returns [] when rg binary is not found."""
    cache_path = tmp_path / "cache.txt"
    cache_path.write_text("/some/file.py:1:hello world\n")

    with patch("subprocess.run", side_effect=FileNotFoundError("rg not found")):
        results = search_ripgrep("hello", cache_path)

    assert results == []


def test_search_ripgrep_finds_matching_lines(tmp_path: Path) -> None:
    """search_ripgrep returns parsed hits from lines matching the query."""
    cache_path = tmp_path / "cache.txt"
    cache_path.write_text(
        "/repo/auth.py:42:def authenticate(user, token):\n"
        "/repo/main.py:10:import os\n"
        "/repo/auth.py:43:    return validate(token)\n"
    )

    results = search_ripgrep("authenticate", cache_path)

    assert len(results) == 1
    assert results[0]["file_path"] == "/repo/auth.py"
    assert results[0]["line_number"] == 42
    assert "authenticate" in results[0]["line_content"]
    assert results[0]["frecency_score"] == pytest.approx(0.5)


def test_search_ripgrep_multiple_matches(tmp_path: Path) -> None:
    """search_ripgrep returns all matching lines across different files."""
    cache_path = tmp_path / "cache.txt"
    cache_path.write_text(
        "/repo/a.py:1:token validation here\n"
        "/repo/b.py:5:token refresh logic\n"
        "/repo/c.py:9:unrelated line\n"
    )

    results = search_ripgrep("token", cache_path)

    assert len(results) == 2
    file_paths = {r["file_path"] for r in results}
    assert "/repo/a.py" in file_paths
    assert "/repo/b.py" in file_paths


def test_search_ripgrep_respects_n_results(tmp_path: Path) -> None:
    """search_ripgrep returns at most n_results hits."""
    lines = "\n".join(f"/repo/f.py:{i}:match line {i}" for i in range(1, 20))
    cache_path = tmp_path / "cache.txt"
    cache_path.write_text(lines + "\n")

    results = search_ripgrep("match", cache_path, n_results=5)

    assert len(results) <= 5


def test_search_ripgrep_returns_empty_on_no_match(tmp_path: Path) -> None:
    """search_ripgrep returns [] when no lines match the query."""
    cache_path = tmp_path / "cache.txt"
    cache_path.write_text("/repo/foo.py:1:completely unrelated\n")

    results = search_ripgrep("xyzzy_no_match_123", cache_path)

    assert results == []


def test_search_ripgrep_fixed_strings_by_default(tmp_path: Path) -> None:
    """search_ripgrep uses fixed-string matching by default (no regex interpretation)."""
    cache_path = tmp_path / "cache.txt"
    # dot in fixed-strings should NOT match any character
    cache_path.write_text(
        "/repo/a.py:1:foo.bar baz\n"
        "/repo/b.py:2:fooXbar baz\n"
    )

    # With fixed_strings=True, "foo.bar" should only match literal "foo.bar"
    results = search_ripgrep("foo.bar", cache_path, fixed_strings=True)

    assert len(results) == 1
    assert results[0]["file_path"] == "/repo/a.py"
