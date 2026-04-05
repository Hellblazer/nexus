# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

_log = structlog.get_logger()


@dataclass(frozen=True)
class Tumbler:
    """Xanadu-inspired hierarchical address: store.owner.document[.chunk]."""

    segments: tuple[int, ...]

    @property
    def store(self) -> int:
        return self.segments[0]

    @property
    def owner(self) -> int:
        return self.segments[1]

    @property
    def document(self) -> int:
        return self.segments[2]

    @property
    def chunk(self) -> int | None:
        return self.segments[3] if len(self.segments) > 3 else None

    def is_prefix_of(self, other: Tumbler) -> bool:
        return other.segments[: len(self.segments)] == self.segments

    def document_address(self) -> Tumbler:
        return Tumbler(self.segments[:3])

    def owner_address(self) -> Tumbler:
        return Tumbler(self.segments[:2])

    def __str__(self) -> str:
        return ".".join(str(s) for s in self.segments)

    @classmethod
    def parse(cls, s: str) -> Tumbler:
        if not s:
            raise ValueError("empty tumbler string")
        return cls(tuple(int(x) for x in s.split(".")))


def _filter_fields(cls: type, obj: dict) -> dict:
    """Filter dict to only include fields declared on the dataclass."""
    import dataclasses
    valid = {f.name for f in dataclasses.fields(cls)}
    return {k: v for k, v in obj.items() if k in valid}


@dataclass
class OwnerRecord:
    owner: str
    name: str
    owner_type: str  # "repo" | "curator"
    repo_hash: str
    description: str
    next_seq: int = 1  # high-water mark — next document number to assign (never decreases)


@dataclass
class DocumentRecord:
    tumbler: str
    title: str
    author: str
    year: int
    content_type: str  # code, prose, rdr, paper, knowledge
    file_path: str
    corpus: str
    physical_collection: str
    chunk_count: int
    head_hash: str
    indexed_at: str
    meta: dict = field(default_factory=dict)
    _deleted: bool = False


@dataclass
class LinkRecord:
    from_t: str
    to_t: str
    link_type: str  # cites, supersedes, quotes, relates, comments, implements, implements-heuristic
    from_span: str
    to_span: str
    created_by: str
    created_at: str
    meta: dict = field(default_factory=dict)
    _deleted: bool = False


# -- JSONL readers (last-line-wins with tombstone support) --


def read_owners(path: Path) -> dict[str, OwnerRecord]:
    records: dict[str, OwnerRecord] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("catalog_jsonl_parse_error", path=str(path), line=line[:80])
                continue
            try:
                key = obj["owner"]
            except KeyError:
                _log.warning("catalog_jsonl_missing_key", path=str(path), line=line[:80])
                continue
            if obj.get("_deleted"):
                records.pop(key, None)
            else:
                records[key] = OwnerRecord(**_filter_fields(OwnerRecord, obj))
    return records


def read_documents(path: Path) -> dict[str, DocumentRecord]:
    records: dict[str, DocumentRecord] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("catalog_jsonl_parse_error", path=str(path), line=line[:80])
                continue
            try:
                key = obj["tumbler"]
            except KeyError:
                _log.warning("catalog_jsonl_missing_key", path=str(path), line=line[:80])
                continue
            if obj.get("_deleted"):
                records.pop(key, None)
            else:
                records[key] = DocumentRecord(**_filter_fields(DocumentRecord, obj))
    return records


def read_links(path: Path) -> dict[tuple[str, str, str], LinkRecord]:
    records: dict[tuple[str, str, str], LinkRecord] = {}
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                _log.warning("catalog_jsonl_parse_error", path=str(path), line=line[:80])
                continue
            # F2: backward compat — old JSONL uses "created", new uses "created_at"
            if "created" in obj and "created_at" not in obj:
                obj["created_at"] = obj.pop("created")
            try:
                key = (obj["from_t"], obj["to_t"], obj["link_type"])
            except KeyError:
                _log.warning("catalog_jsonl_missing_key", path=str(path), line=line[:80])
                continue
            if obj.get("_deleted"):
                records.pop(key, None)
            else:
                records[key] = LinkRecord(**_filter_fields(LinkRecord, obj))
    return records
