# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for JSONL reader resilience — malformed lines skipped with warning."""

import json
from pathlib import Path

from nexus.catalog.tumbler import read_documents, read_links, read_owners


def _write(path: Path, lines: list[str]) -> Path:
    path.write_text("\n".join(lines) + "\n")
    return path


def test_read_links_skips_malformed_line(tmp_path: Path) -> None:
    good = json.dumps({
        "from_t": "1.1.1", "to_t": "1.1.2", "link_type": "cites",
        "from_span": "", "to_span": "", "created_by": "test", "created": "2026-01-01",
    })
    _write(tmp_path / "links.jsonl", [good, "NOT-JSON{{{", ""])
    result = read_links(tmp_path / "links.jsonl")
    assert len(result) == 1
    assert ("1.1.1", "1.1.2", "cites") in result


def test_read_documents_skips_malformed_line(tmp_path: Path) -> None:
    good = json.dumps({
        "tumbler": "1.1.1", "title": "doc", "author": "a", "year": 2026,
        "content_type": "code", "file_path": "f.py", "corpus": "c",
        "physical_collection": "pc", "chunk_count": 1, "head_hash": "h",
        "indexed_at": "2026-01-01",
    })
    _write(tmp_path / "docs.jsonl", [good, "{bad json"])
    result = read_documents(tmp_path / "docs.jsonl")
    assert len(result) == 1
    assert "1.1.1" in result


def test_read_owners_skips_malformed_line(tmp_path: Path) -> None:
    good = json.dumps({
        "owner": "1.1", "name": "test-repo", "owner_type": "repo",
        "repo_hash": "abc", "description": "desc",
    })
    _write(tmp_path / "owners.jsonl", [good, "CORRUPT"])
    result = read_owners(tmp_path / "owners.jsonl")
    assert len(result) == 1
    assert "1.1" in result


def test_read_links_empty_file(tmp_path: Path) -> None:
    (tmp_path / "links.jsonl").write_text("")
    result = read_links(tmp_path / "links.jsonl")
    assert result == {}


def test_read_links_all_malformed(tmp_path: Path) -> None:
    _write(tmp_path / "links.jsonl", ["BAD1", "BAD2", "BAD3"])
    result = read_links(tmp_path / "links.jsonl")
    assert result == {}


def test_read_owners_skips_missing_key(tmp_path: Path) -> None:
    """Valid JSON but missing 'owner' key should be skipped."""
    bad = json.dumps({"name": "test", "owner_type": "repo"})
    _write(tmp_path / "owners.jsonl", [bad])
    result = read_owners(tmp_path / "owners.jsonl")
    assert result == {}


def test_read_documents_skips_missing_key(tmp_path: Path) -> None:
    """Valid JSON but missing 'tumbler' key should be skipped."""
    bad = json.dumps({"title": "doc", "author": "a"})
    _write(tmp_path / "docs.jsonl", [bad])
    result = read_documents(tmp_path / "docs.jsonl")
    assert result == {}


def test_read_links_skips_missing_key(tmp_path: Path) -> None:
    """Valid JSON but missing 'from_t' key should be skipped."""
    bad = json.dumps({"to_t": "1.1.2", "link_type": "cites"})
    _write(tmp_path / "links.jsonl", [bad])
    result = read_links(tmp_path / "links.jsonl")
    assert result == {}


def test_read_links_old_format_created_field(tmp_path: Path) -> None:
    """Old JSONL uses 'created' key; should be remapped to 'created_at'."""
    old_format = json.dumps({
        "from_t": "1.1.1", "to_t": "1.1.2", "link_type": "cites",
        "from_span": "", "to_span": "", "created_by": "test",
        "created": "2026-01-01T00:00:00Z",
    })
    _write(tmp_path / "links.jsonl", [old_format])
    result = read_links(tmp_path / "links.jsonl")
    assert len(result) == 1
    rec = result[("1.1.1", "1.1.2", "cites")]
    assert rec.created_at == "2026-01-01T00:00:00Z"


def test_read_links_new_format_created_at(tmp_path: Path) -> None:
    """New JSONL uses 'created_at' key directly."""
    new_format = json.dumps({
        "from_t": "1.1.1", "to_t": "1.1.2", "link_type": "cites",
        "from_span": "", "to_span": "", "created_by": "test",
        "created_at": "2026-02-01T00:00:00Z",
    })
    _write(tmp_path / "links.jsonl", [new_format])
    result = read_links(tmp_path / "links.jsonl")
    rec = result[("1.1.1", "1.1.2", "cites")]
    assert rec.created_at == "2026-02-01T00:00:00Z"
