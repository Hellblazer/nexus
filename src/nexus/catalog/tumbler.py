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

    @property
    def depth(self) -> int:
        """Number of segments (dot count + 1)."""
        return len(self.segments)

    def ancestors(self) -> list[Tumbler]:
        """All tumbler prefixes including self: 1.1.42 → [1, 1.1, 1.1.42]."""
        return [Tumbler(self.segments[: i + 1]) for i in range(len(self.segments))]

    @staticmethod
    def lca(a: Tumbler, b: Tumbler) -> Tumbler | None:
        """Least common ancestor. Returns None if no segments match."""
        common: list[int] = []
        for sa, sb in zip(a.segments, b.segments):
            if sa != sb:
                break
            common.append(sa)
        return Tumbler(tuple(common)) if common else None

    def is_prefix_of(self, other: Tumbler) -> bool:
        return other.segments[: len(self.segments)] == self.segments

    def document_address(self) -> Tumbler:
        return Tumbler(self.segments[:3])

    def owner_address(self) -> Tumbler:
        return Tumbler(self.segments[:2])

    def __str__(self) -> str:
        return ".".join(str(s) for s in self.segments)

    def __lt__(self, other: object) -> bool:
        """Segment-by-segment integer comparison with -1 sentinel padding.

        Shorter tumblers sort before longer ones with identical prefixes
        (parent < child). This is simplified lexicographic ordering over
        integer segments — not Nelson's transfinitesimal arithmetic — per
        RDR-053 deviation D6.
        """
        if not isinstance(other, Tumbler):
            return NotImplemented
        max_len = max(len(self.segments), len(other.segments))
        a = self.segments + (-1,) * (max_len - len(self.segments))
        b = other.segments + (-1,) * (max_len - len(other.segments))
        return a < b

    def __le__(self, other: object) -> bool:
        if not isinstance(other, Tumbler):
            return NotImplemented
        return self == other or self < other

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, Tumbler):
            return NotImplemented
        return not self <= other

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, Tumbler):
            return NotImplemented
        return not self < other

    @staticmethod
    def spans_overlap(
        a_start: Tumbler, a_end: Tumbler,
        b_start: Tumbler, b_end: Tumbler,
    ) -> bool:
        """True if positional span [a_start, a_end] overlaps [b_start, b_end].

        Standard interval overlap: a_start <= b_end and b_start <= a_end.
        Inclusive bounds (endpoints touching = overlapping).

        Callers must ensure a_start <= a_end and b_start <= b_end (ordered
        bounds). Reversed spans produce silently wrong results.

        Applies only to positional (index-based) spans. Content-hash spans
        (chash: format) carry no ordering — overlap is undefined for them and
        this method must not be called with chash: span arguments.
        """
        return a_start <= b_end and b_start <= a_end

    @classmethod
    def parse(cls, s: str) -> Tumbler:
        if not s:
            raise ValueError("empty tumbler string")
        segs = tuple(int(x) for x in s.split("."))
        if any(x < 0 for x in segs):
            raise ValueError(f"tumbler segments must be non-negative: {s!r}")
        return cls(segs)


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
    repo_root: str = ""  # absolute path to repo working tree (RDR-060)
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
    # nexus-8luh: POSIX mtime (seconds since epoch) of ``file_path`` at
    # index time. 0.0 means "not captured" (pre-migration rows, manual
    # registrations, synthesized docs). Used by stale-source detection
    # to flag documents whose source file has changed since last index.
    source_mtime: float = 0.0
    # nexus-s8yz: alias pointer to a canonical tumbler. '' means this
    # document is canonical. When dedupe-owners (nexus-tmbh) consolidates
    # documents, the surplus copies are rewritten with alias_of=<canonical>
    # so external references continue to resolve. Resolution is transitive
    # with cycle protection — see Catalog.resolve_alias().
    alias_of: str = ""
    # RDR-096 P2.1: persistent URI identity. '' on pre-migration rows;
    # populated at register time for new entries. Phase 3.1 (P3.1)
    # will derive URIs at the catalog register boundary.
    source_uri: str = ""
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
