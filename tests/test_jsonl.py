# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json

import pytest

from nexus.catalog.tumbler import (
    DocumentRecord,
    LinkRecord,
    OwnerRecord,
    read_documents,
    read_links,
    read_owners,
)


def _write_jsonl(path, records: list[dict]):
    with path.open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_owner(*, owner: str = "1.1", name: str = "test-repo", **overrides) -> dict:
    base = {
        "owner": owner,
        "name": name,
        "owner_type": "repo",
        "repo_hash": "abcd1234",
        "description": "test repo",
    }
    base.update(overrides)
    return base


def _make_doc(*, tumbler: str = "1.1.1", title: str = "test.py", **overrides) -> dict:
    base = {
        "tumbler": tumbler,
        "title": title,
        "author": "",
        "year": 0,
        "content_type": "code",
        "file_path": "src/test.py",
        "corpus": "",
        "physical_collection": "code__test",
        "chunk_count": 5,
        "head_hash": "abc123",
        "indexed_at": "2026-01-01T00:00:00Z",
        "meta": {},
    }
    base.update(overrides)
    return base


def _make_link(
    *,
    from_t: str = "1.1.1",
    to_t: str = "1.1.2",
    link_type: str = "cites",
    **overrides,
) -> dict:
    base = {
        "from_t": from_t,
        "to_t": to_t,
        "link_type": link_type,
        "from_span": "",
        "to_span": "",
        "created_by": "user",
        "created": "2026-01-01T00:00:00Z",
        "meta": {},
    }
    base.update(overrides)
    return base


class TestReadDocuments:
    def test_single_record(self, tmp_path):
        path = tmp_path / "documents.jsonl"
        _write_jsonl(path, [_make_doc()])
        docs = read_documents(path)
        assert len(docs) == 1
        assert "1.1.1" in docs
        assert docs["1.1.1"].title == "test.py"

    def test_last_line_wins(self, tmp_path):
        path = tmp_path / "documents.jsonl"
        _write_jsonl(
            path,
            [
                _make_doc(tumbler="1.1.1", title="old.py"),
                _make_doc(tumbler="1.1.1", title="new.py"),
            ],
        )
        docs = read_documents(path)
        assert len(docs) == 1
        assert docs["1.1.1"].title == "new.py"

    def test_tombstone_deletes(self, tmp_path):
        path = tmp_path / "documents.jsonl"
        _write_jsonl(
            path,
            [
                _make_doc(tumbler="1.1.1"),
                {**_make_doc(tumbler="1.1.1"), "_deleted": True},
            ],
        )
        docs = read_documents(path)
        assert len(docs) == 0

    def test_tombstone_then_readd(self, tmp_path):
        path = tmp_path / "documents.jsonl"
        _write_jsonl(
            path,
            [
                _make_doc(tumbler="1.1.1", title="first"),
                {**_make_doc(tumbler="1.1.1"), "_deleted": True},
                _make_doc(tumbler="1.1.1", title="revived"),
            ],
        )
        docs = read_documents(path)
        assert len(docs) == 1
        assert docs["1.1.1"].title == "revived"

    def test_multiple_tumblers(self, tmp_path):
        path = tmp_path / "documents.jsonl"
        _write_jsonl(
            path,
            [
                _make_doc(tumbler="1.1.1", title="a.py"),
                _make_doc(tumbler="1.1.2", title="b.py"),
            ],
        )
        docs = read_documents(path)
        assert len(docs) == 2

    def test_empty_file(self, tmp_path):
        path = tmp_path / "documents.jsonl"
        path.write_text("")
        docs = read_documents(path)
        assert len(docs) == 0

    def test_blank_lines_skipped(self, tmp_path):
        path = tmp_path / "documents.jsonl"
        path.write_text(
            json.dumps(_make_doc()) + "\n\n\n" + json.dumps(_make_doc(tumbler="1.1.2")) + "\n"
        )
        docs = read_documents(path)
        assert len(docs) == 2


class TestReadOwners:
    def test_single_owner(self, tmp_path):
        path = tmp_path / "owners.jsonl"
        _write_jsonl(path, [_make_owner()])
        owners = read_owners(path)
        assert len(owners) == 1
        assert owners["1.1"].name == "test-repo"

    def test_last_line_wins(self, tmp_path):
        path = tmp_path / "owners.jsonl"
        _write_jsonl(
            path,
            [
                _make_owner(owner="1.1", name="old"),
                _make_owner(owner="1.1", name="new"),
            ],
        )
        owners = read_owners(path)
        assert owners["1.1"].name == "new"


class TestReadLinks:
    def test_single_link(self, tmp_path):
        path = tmp_path / "links.jsonl"
        _write_jsonl(path, [_make_link()])
        links = read_links(path)
        assert len(links) == 1

    def test_tombstone_deletes_link(self, tmp_path):
        path = tmp_path / "links.jsonl"
        _write_jsonl(
            path,
            [
                _make_link(from_t="1.1.1", to_t="1.1.2", link_type="cites"),
                {
                    **_make_link(from_t="1.1.1", to_t="1.1.2", link_type="cites"),
                    "_deleted": True,
                },
            ],
        )
        links = read_links(path)
        assert len(links) == 0

    def test_different_link_types_distinct(self, tmp_path):
        path = tmp_path / "links.jsonl"
        _write_jsonl(
            path,
            [
                _make_link(from_t="1.1.1", to_t="1.1.2", link_type="cites"),
                _make_link(from_t="1.1.1", to_t="1.1.2", link_type="supersedes"),
            ],
        )
        links = read_links(path)
        assert len(links) == 2
