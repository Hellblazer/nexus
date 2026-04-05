# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.tumbler import (
    DocumentRecord,
    LinkRecord,
    OwnerRecord,
    Tumbler,
    read_documents,
    read_links,
    read_owners,
)

_log = structlog.get_logger()


@dataclass
class CatalogEntry:
    tumbler: Tumbler
    title: str
    author: str
    year: int
    content_type: str
    file_path: str
    corpus: str
    physical_collection: str
    chunk_count: int
    head_hash: str
    indexed_at: str
    meta: dict = field(default_factory=dict)


@dataclass
class CatalogLink:
    from_tumbler: Tumbler
    to_tumbler: Tumbler
    link_type: str
    from_span: str
    to_span: str
    created_by: str
    created_at: str
    meta: dict = field(default_factory=dict)


def _run_git(
    args: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    if check and result.returncode != 0:
        raise RuntimeError(f"git command failed: {result.stderr.strip()}")
    return result


class Catalog:
    """Xanadu-inspired catalog: owners, documents, and links over JSONL + SQLite."""

    def __init__(self, catalog_dir: Path, db_path: Path) -> None:
        self._dir = catalog_dir
        self._db = CatalogDB(db_path)
        self._owners_path = catalog_dir / "owners.jsonl"
        self._documents_path = catalog_dir / "documents.jsonl"
        self._links_path = catalog_dir / "links.jsonl"

    @classmethod
    def init(cls, catalog_path: Path, remote: str | None = None) -> Catalog:
        """Create catalog git repo with empty JSONL files."""
        catalog_path.mkdir(parents=True, exist_ok=True)
        git_dir = catalog_path / ".git"
        if not git_dir.exists():
            _run_git(["git", "init"], cwd=catalog_path)
        # Create empty JSONL files if missing
        for name in ("documents.jsonl", "owners.jsonl", "links.jsonl"):
            p = catalog_path / name
            if not p.exists():
                p.touch()
        # Create .gitignore
        gitignore = catalog_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(".catalog.db\n")
        # Initial commit if no commits yet
        result = _run_git(["git", "rev-parse", "HEAD"], cwd=catalog_path, check=False)
        if result.returncode != 0:
            _run_git(["git", "add", "-A"], cwd=catalog_path)
            _run_git(["git", "commit", "-m", "Init catalog"], cwd=catalog_path)
        if remote:
            # Only add remote if not already set
            r = _run_git(["git", "remote"], cwd=catalog_path, check=False)
            if "origin" not in r.stdout:
                _run_git(["git", "remote", "add", "origin", remote], cwd=catalog_path)
        db_path = catalog_path / ".catalog.db"
        return cls(catalog_path, db_path)

    @staticmethod
    def is_initialized(catalog_path: Path) -> bool:
        """Return True if catalog git repo exists at path."""
        return (
            (catalog_path / ".git").exists()
            and (catalog_path / "documents.jsonl").exists()
        )

    def sync(self, message: str = "catalog update") -> None:
        """git add -A && git commit && git push (if remote configured)."""
        _run_git(["git", "add", "-A"], cwd=self._dir)
        # Check if there's anything to commit
        status = _run_git(["git", "status", "--porcelain"], cwd=self._dir)
        if not status.stdout.strip():
            return
        _run_git(["git", "commit", "-m", message], cwd=self._dir)
        # Push only if remote exists
        remote = _run_git(["git", "remote"], cwd=self._dir, check=False)
        if remote.stdout.strip():
            _run_git(["git", "push", "-u", "origin", "HEAD"], cwd=self._dir, check=False)

    def pull(self) -> None:
        """git pull && rebuild SQLite from JSONL."""
        remote = _run_git(["git", "remote"], cwd=self._dir, check=False)
        if remote.stdout.strip():
            _run_git(["git", "pull"], cwd=self._dir, check=False)
        self.rebuild()

    # ── Locking ────────────────────────────────────────────────────────────

    def _acquire_lock(self) -> int:
        dir_fd = os.open(str(self._dir), os.O_RDONLY)
        fcntl.flock(dir_fd, fcntl.LOCK_EX)
        return dir_fd

    def _release_lock(self, dir_fd: int) -> None:
        fcntl.flock(dir_fd, fcntl.LOCK_UN)
        os.close(dir_fd)

    # ── JSONL append helpers ───────────────────────────────────────────────

    def _append_jsonl(self, path: Path, record: dict) -> None:
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    # ── Owners ─────────────────────────────────────────────────────────────

    def register_owner(
        self, name: str, owner_type: str, *, repo_hash: str = "", description: str = ""
    ) -> Tumbler:
        dir_fd = self._acquire_lock()
        try:
            # Read existing owners to find next number
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            next_num = max(
                (Tumbler.parse(k).owner for k in owners), default=0
            ) + 1
            prefix = f"1.{next_num}"
            rec = OwnerRecord(
                owner=prefix,
                name=name,
                owner_type=owner_type,
                repo_hash=repo_hash,
                description=description,
            )
            self._append_jsonl(self._owners_path, rec.__dict__)
            # Upsert SQLite
            self._db._conn.execute(
                "INSERT OR REPLACE INTO owners (tumbler_prefix, name, owner_type, repo_hash, description) "
                "VALUES (?, ?, ?, ?, ?)",
                (prefix, name, owner_type, repo_hash, description),
            )
            self._db._conn.commit()
            return Tumbler.parse(prefix)
        finally:
            self._release_lock(dir_fd)

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        row = self._db._conn.execute(
            "SELECT tumbler_prefix FROM owners WHERE repo_hash = ?", (repo_hash,)
        ).fetchone()
        return Tumbler.parse(row[0]) if row else None

    # ── Documents ──────────────────────────────────────────────────────────

    def register(
        self,
        owner: Tumbler,
        title: str,
        *,
        content_type: str = "",
        file_path: str = "",
        corpus: str = "",
        physical_collection: str = "",
        chunk_count: int = 0,
        head_hash: str = "",
        author: str = "",
        year: int = 0,
        meta: dict | None = None,
    ) -> Tumbler:
        dir_fd = self._acquire_lock()
        try:
            # Idempotency: check by file_path if non-empty
            if file_path:
                existing = self.by_file_path(owner, file_path)
                if existing is not None:
                    return existing.tumbler

            doc_num = self._db.next_document_number(str(owner))
            tumbler = Tumbler((*owner.segments, doc_num))
            now = datetime.now(UTC).isoformat()
            rec = DocumentRecord(
                tumbler=str(tumbler),
                title=title,
                author=author,
                year=year,
                content_type=content_type,
                file_path=file_path,
                corpus=corpus,
                physical_collection=physical_collection,
                chunk_count=chunk_count,
                head_hash=head_hash,
                indexed_at=now,
                meta=meta or {},
            )
            self._append_jsonl(self._documents_path, rec.__dict__)
            self._db._conn.execute(
                "INSERT INTO documents "
                "(tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(tumbler), title, author, year, content_type, file_path,
                    corpus, physical_collection, chunk_count, head_hash, now,
                    json.dumps(meta or {}),
                ),
            )
            self._db._conn.commit()
            return tumbler
        finally:
            self._release_lock(dir_fd)

    def resolve(self, tumbler: Tumbler) -> CatalogEntry | None:
        row = self._db._conn.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents WHERE tumbler = ?",
            (str(tumbler),),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
        )

    def update(self, tumbler: Tumbler, **fields: object) -> None:
        dir_fd = self._acquire_lock()
        try:
            entry = self.resolve(tumbler)
            if entry is None:
                raise KeyError(f"no document with tumbler {tumbler}")
            # Build updated record
            rec_dict = {
                "tumbler": str(entry.tumbler),
                "title": entry.title,
                "author": entry.author,
                "year": entry.year,
                "content_type": entry.content_type,
                "file_path": entry.file_path,
                "corpus": entry.corpus,
                "physical_collection": entry.physical_collection,
                "chunk_count": entry.chunk_count,
                "head_hash": entry.head_hash,
                "indexed_at": entry.indexed_at,
                "meta": entry.meta,
            }
            rec_dict.update(fields)
            self._append_jsonl(self._documents_path, rec_dict)
            # Upsert SQLite
            self._db._conn.execute(
                "INSERT OR REPLACE INTO documents "
                "(tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec_dict["tumbler"], rec_dict["title"], rec_dict["author"],
                    rec_dict["year"], rec_dict["content_type"], rec_dict["file_path"],
                    rec_dict["corpus"], rec_dict["physical_collection"],
                    rec_dict["chunk_count"], rec_dict["head_hash"],
                    rec_dict["indexed_at"], json.dumps(rec_dict["meta"]),
                ),
            )
            self._db._conn.commit()
        finally:
            self._release_lock(dir_fd)

    def find(self, query: str, *, content_type: str | None = None) -> list[CatalogEntry]:
        rows = self._db.search(query, content_type=content_type)
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r["tumbler"]),
                title=r["title"],
                author=r["author"],
                year=r["year"],
                content_type=r["content_type"],
                file_path=r["file_path"],
                corpus=r["corpus"],
                physical_collection=r["physical_collection"],
                chunk_count=r["chunk_count"],
                head_hash=r["head_hash"] or "",
                indexed_at=r["indexed_at"] or "",
            )
            for r in rows
        ]

    def by_file_path(self, owner: Tumbler, file_path: str) -> CatalogEntry | None:
        row = self._db._conn.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents WHERE tumbler LIKE ? AND file_path = ?",
            (str(owner) + ".%", file_path),
        ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            tumbler=Tumbler.parse(row[0]),
            title=row[1],
            author=row[2],
            year=row[3],
            content_type=row[4],
            file_path=row[5],
            corpus=row[6],
            physical_collection=row[7],
            chunk_count=row[8],
            head_hash=row[9],
            indexed_at=row[10],
            meta=json.loads(row[11]) if row[11] else {},
        )

    def by_owner(self, owner: Tumbler) -> list[CatalogEntry]:
        rows = self._db._conn.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents WHERE tumbler LIKE ?",
            (str(owner) + ".%",),
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]),
                title=r[1],
                author=r[2],
                year=r[3],
                content_type=r[4],
                file_path=r[5],
                corpus=r[6],
                physical_collection=r[7],
                chunk_count=r[8],
                head_hash=r[9],
                indexed_at=r[10],
                meta=json.loads(r[11]) if r[11] else {},
            )
            for r in rows
        ]

    # ── Links ───────────────────────────────────────────────────��──────────

    def link(
        self,
        from_t: Tumbler,
        to_t: Tumbler,
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        **meta: object,
    ) -> None:
        dir_fd = self._acquire_lock()
        try:
            now = datetime.now(UTC).isoformat()
            rec = LinkRecord(
                from_t=str(from_t),
                to_t=str(to_t),
                link_type=link_type,
                from_span=from_span,
                to_span=to_span,
                created_by=created_by,
                created=now,
                meta=dict(meta),
            )
            self._append_jsonl(self._links_path, rec.__dict__)
            self._db._conn.execute(
                "INSERT INTO links "
                "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
                "created_by, created_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(from_t), str(to_t), link_type, from_span, to_span,
                    created_by, now, json.dumps(dict(meta)),
                ),
            )
            self._db._conn.commit()
        finally:
            self._release_lock(dir_fd)

    def unlink(self, from_t: Tumbler, to_t: Tumbler, link_type: str = "") -> int:
        dir_fd = self._acquire_lock()
        try:
            if link_type:
                rows = self._db._conn.execute(
                    "SELECT id, link_type FROM links WHERE from_tumbler = ? AND to_tumbler = ? AND link_type = ?",
                    (str(from_t), str(to_t), link_type),
                ).fetchall()
            else:
                rows = self._db._conn.execute(
                    "SELECT id, link_type FROM links WHERE from_tumbler = ? AND to_tumbler = ?",
                    (str(from_t), str(to_t)),
                ).fetchall()

            for row_id, lt in rows:
                tombstone = {
                    "from_t": str(from_t),
                    "to_t": str(to_t),
                    "link_type": lt,
                    "_deleted": True,
                    "from_span": "",
                    "to_span": "",
                    "created_by": "",
                    "created": datetime.now(UTC).isoformat(),
                    "meta": {},
                }
                self._append_jsonl(self._links_path, tombstone)
                self._db._conn.execute("DELETE FROM links WHERE id = ?", (row_id,))

            self._db._conn.commit()
            return len(rows)
        finally:
            self._release_lock(dir_fd)

    def _row_to_link(self, row: tuple) -> CatalogLink:
        return CatalogLink(
            from_tumbler=Tumbler.parse(row[0]),
            to_tumbler=Tumbler.parse(row[1]),
            link_type=row[2],
            from_span=row[3] or "",
            to_span=row[4] or "",
            created_by=row[5],
            created_at=row[6] or "",
            meta=json.loads(row[7]) if row[7] else {},
        )

    def links_from(self, tumbler: Tumbler, link_type: str = "") -> list[CatalogLink]:
        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links WHERE from_tumbler = ?"
        )
        params: list[str] = [str(tumbler)]
        if link_type:
            sql += " AND link_type = ?"
            params.append(link_type)
        return [self._row_to_link(r) for r in self._db._conn.execute(sql, params).fetchall()]

    def links_to(self, tumbler: Tumbler, link_type: str = "") -> list[CatalogLink]:
        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links WHERE to_tumbler = ?"
        )
        params: list[str] = [str(tumbler)]
        if link_type:
            sql += " AND link_type = ?"
            params.append(link_type)
        return [self._row_to_link(r) for r in self._db._conn.execute(sql, params).fetchall()]

    def graph(
        self,
        tumbler: Tumbler,
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
    ) -> dict:
        """BFS traversal to given depth. Returns {"nodes": [...], "edges": [...]}."""
        visited: set[str] = {str(tumbler)}
        seen_edges: set[tuple[str, str, str]] = set()
        all_edges: list[CatalogLink] = []
        queue: deque[tuple[Tumbler, int]] = deque([(tumbler, 0)])

        while queue:
            current, d = queue.popleft()
            if d >= depth:
                continue

            neighbors: list[CatalogLink] = []
            if direction in ("out", "both"):
                neighbors.extend(self.links_from(current, link_type=link_type))
            if direction in ("in", "both"):
                neighbors.extend(self.links_to(current, link_type=link_type))

            for edge in neighbors:
                edge_key = (str(edge.from_tumbler), str(edge.to_tumbler), edge.link_type)
                if edge_key not in seen_edges:
                    seen_edges.add(edge_key)
                    all_edges.append(edge)
                # Determine the "other" end
                other = edge.to_tumbler if edge.from_tumbler == current else edge.from_tumbler
                if str(other) not in visited:
                    visited.add(str(other))
                    queue.append((other, d + 1))

        # Build node list (exclude the starting node)
        visited.discard(str(tumbler))
        nodes = [self.resolve(Tumbler.parse(t)) for t in visited]
        nodes = [n for n in nodes if n is not None]
        return {"nodes": nodes, "edges": all_edges}

    # ── Rebuild ───────────���──────────────────────────��─────────────────────

    def rebuild(self) -> None:
        """Rebuild SQLite from JSONL. Called at startup and after git pull."""
        dir_fd = self._acquire_lock()
        try:
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
            links_dict = read_links(self._links_path) if self._links_path.exists() else {}
            self._db.rebuild(owners, documents, list(links_dict.values()))
        finally:
            self._release_lock(dir_fd)
