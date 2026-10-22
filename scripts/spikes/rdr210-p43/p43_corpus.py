# SPDX-License-Identifier: AGPL-3.0-or-later
"""Corpus selection and building for the P4.3 harness.

Two corpora, both chosen DETERMINISTICALLY (path order by sha1 of the repo-
relative path, so adding a file elsewhere does not reshuffle the set) and then
PINNED in ``corpus-files.json`` so the file list is a recorded fact:

* ``prose``: 46 Markdown files, ``docs/*.md`` first (up to 23) and
  ``docs/rdr/rdr-*.md`` for the rest.
* ``code``:  40 Python files from ``src/nexus``.

Each corpus is chunked by :mod:`p43_chunker` and addressed by chunk hash
(``sha256`` of the chunk text, 64 hex), the identity the engine stores. The
corpus hash covers every file path and chunk hash, so a changed file means new
collection names and no stale reuse.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from p43_chunker import DEFAULT_MAX_CHARS, chunk_text

PROSE_FILES = 46
PROSE_DOCS_FIRST = 23
CODE_FILES = 40
MIN_BYTES = 3000
PROSE_MAX_BYTES = 60_000
CODE_MAX_BYTES = 40_000
PROSE_EXCLUDE = frozenset({"README.md", "AGENTS.md"})

#: kind -> (catalog content_type, embedding family tokens as the harness names them)
KINDS = ("prose", "code")
CONTENT_TYPE = {"prose": "knowledge", "code": "code"}


def _order_key(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()  # noqa: S324 - ordering, not security


def select_files(repo_root: Path, kind: str) -> list[str]:
    """The deterministic file list for *kind* (repo-relative POSIX paths)."""
    if kind == "prose":
        docs = _candidates(repo_root, "docs", "*.md", PROSE_MAX_BYTES, exclude=PROSE_EXCLUDE)
        rdrs = _candidates(repo_root, "docs/rdr", "rdr-*.md", PROSE_MAX_BYTES)
        first = sorted(docs, key=_order_key)[:PROSE_DOCS_FIRST]
        rest = sorted(rdrs, key=_order_key)[: PROSE_FILES - len(first)]
        return sorted(first + rest)
    if kind == "code":
        cands = _candidates(repo_root, "src/nexus", "*.py", CODE_MAX_BYTES, recursive=True,
                            exclude=frozenset({"__init__.py"}))
        return sorted(sorted(cands, key=_order_key)[:CODE_FILES])
    raise ValueError(f"unknown corpus kind {kind!r}")


def _candidates(
    repo_root: Path, sub: str, pattern: str, max_bytes: int, *,
    recursive: bool = False, exclude: frozenset[str] = frozenset(),
) -> list[str]:
    base = repo_root / sub
    paths = base.rglob(pattern) if recursive else base.glob(pattern)
    out = []
    for p in paths:
        if not p.is_file() or p.name in exclude or "__pycache__" in p.parts:
            continue
        if MIN_BYTES <= p.stat().st_size <= max_bytes:
            out.append(p.relative_to(repo_root).as_posix())
    return out


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_pinned_list(repo_root: Path, out: Path) -> dict:
    """Select both corpora and write the pinned list (path + sha256 each)."""
    doc: dict = {"schema": 1, "chunk_max_chars": DEFAULT_MAX_CHARS}
    for kind in KINDS:
        doc[kind] = [
            {"path": rel, "sha256": file_sha256(repo_root / rel)}
            for rel in select_files(repo_root, kind)
        ]
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return doc


@dataclass(frozen=True)
class Chunk:
    file: str
    position: int
    text: str
    chash: str


@dataclass
class Corpus:
    kind: str
    files: list[str]
    chunks_by_file: dict[str, list[Chunk]]
    drifted: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE[self.kind]

    @property
    def n_chunks(self) -> int:
        return sum(len(v) for v in self.chunks_by_file.values())

    @property
    def n_chars(self) -> int:
        return sum(len(c.text) for v in self.chunks_by_file.values() for c in v)

    @property
    def corpus_hash(self) -> str:
        h = hashlib.sha256()
        h.update(self.kind.encode())
        for f in sorted(self.files):
            h.update(b"\0" + f.encode())
            for c in self.chunks_by_file[f]:
                h.update(b"\1" + c.chash.encode())
        return h.hexdigest()

    @property
    def short_hash(self) -> str:
        return self.corpus_hash[:8]

    def chash_files(self) -> dict[str, set[str]]:
        """chash -> the files whose chunks carry that text (identical text in
        two files is one chash, relevant to both)."""
        out: dict[str, set[str]] = {}
        for f, chunks in self.chunks_by_file.items():
            for c in chunks:
                out.setdefault(c.chash, set()).add(f)
        return out

    def chashes_of(self, file: str) -> set[str]:
        return {c.chash for c in self.chunks_by_file[file]}

    def all_chunks(self) -> list[Chunk]:
        return [c for f in sorted(self.files) for c in self.chunks_by_file[f]]


def chash_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_file(rel: str, text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    return [Chunk(rel, i, t, chash_of(t)) for i, t in enumerate(chunk_text(text, max_chars))]


def build_corpus(repo_root: Path, kind: str, pinned: dict) -> Corpus:
    """Read the pinned files and chunk them. A file whose sha256 no longer
    matches the pin is chunked as it is now and listed in ``drifted``."""
    entries = pinned[kind]
    max_chars = int(pinned.get("chunk_max_chars", DEFAULT_MAX_CHARS))
    files: list[str] = []
    chunks_by_file: dict[str, list[Chunk]] = {}
    drifted: list[str] = []
    for e in entries:
        rel = e["path"]
        path = repo_root / rel
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != e["sha256"]:
            drifted.append(rel)
        files.append(rel)
        chunks_by_file[rel] = chunk_file(rel, data.decode("utf-8"), max_chars)
    return Corpus(kind=kind, files=files, chunks_by_file=chunks_by_file, drifted=drifted)


def load_pinned(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
