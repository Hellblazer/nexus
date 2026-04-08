# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from chromadb.api import ClientAPI

# Span format: "line_start-line_end" or "chunk_idx:char_start-char_end" or
# "chash:<sha256hex>" or "".  Empty string means "the whole document".
_SPAN_PATTERN = re.compile(
    r"^$"                              # empty — whole document
    r"|^\d+-\d+$"                      # line range: "42-57"
    r"|^\d+:\d+-\d+$"                  # chunk:char range: "3:100-250"
    r"|^chash:[0-9a-f]{64}$"           # content-hash: chash:<sha256hex>
    r"|^chash:[0-9a-f]{64}:\d+-\d+$"  # content-hash + char range: chash:<sha256hex>:<start>-<end>
)

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


def _default_registry_path() -> Path:
    """Return the default path to the repo registry JSON file."""
    return Path.home() / ".config" / "nexus" / "repos.json"


def make_relative(abs_path: str | Path, repo_root: Path) -> str:
    """Return path relative to repo_root, or original if not under repo_root."""
    try:
        return str(Path(abs_path).relative_to(repo_root))
    except ValueError:
        return str(abs_path)


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

    def to_dict(self) -> dict:
        return {
            "tumbler": str(self.tumbler),
            "title": self.title,
            "author": self.author,
            "year": self.year,
            "content_type": self.content_type,
            "file_path": self.file_path,
            "corpus": self.corpus,
            "physical_collection": self.physical_collection,
            "chunk_count": self.chunk_count,
            "head_hash": self.head_hash,
            "indexed_at": self.indexed_at,
            "meta": self.meta,
        }


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

    def to_dict(self) -> dict:
        return {
            "from": str(self.from_tumbler),
            "to": str(self.to_tumbler),
            "type": self.link_type,
            "from_span": self.from_span,
            "to_span": self.to_span,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "meta": self.meta,
        }


def _run_git(
    args: list[str], cwd: Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=30)
    if check and result.returncode != 0:
        raise RuntimeError(f"git command failed: {result.stderr.strip()}")
    return result


class Catalog:
    """Xanadu-inspired catalog: owners, documents, and links over JSONL + SQLite.

    Deliberate departures from Nelson's Xanadu:
    - TTL expiry: entries with ``expires_at`` set are temporary addresses, violating
      Nelson's "ALL ADDRESSES REMAIN VALID" principle. Tumblers are never reused
      (high-water mark), but expired entries become unresolvable.
    - Chunk addressing is position-based (chunk index), not content-addressed.
      Re-indexing a document may shift which content a chunk span refers to.
    - No tumbler arithmetic (transfinitesimal ADD/SUBTRACT). Ordering and overlap
      detection use integer segment comparison, not Nelson's number space.

    Span Policy (RDR-053):
        Spans are optional on all link types. Accepted formats (validated by
        ``_SPAN_PATTERN``):

        - ``""`` — whole document (no sub-document addressing)
        - ``"N-N"`` — line range (positional, legacy)
        - ``"N:N-N"`` — chunk:char range (positional, legacy)
        - ``"chash:<sha256hex>"`` — content-addressed chunk identity (preferred)
        - ``"chash:<sha256hex>:<start>-<end>"`` — character range within a content-addressed chunk

        Content-hash spans survive re-indexing when chunk boundaries are unchanged
        (RDR-053 D5, RF-3, RF-8). Position-based spans degrade on re-index and are
        detectable via ``link_audit()``.
    """

    def __init__(self, catalog_dir: Path, db_path: Path) -> None:
        self._dir = catalog_dir
        self._db = CatalogDB(db_path)
        self._owners_path = catalog_dir / "owners.jsonl"
        self._documents_path = catalog_dir / "documents.jsonl"
        self._links_path = catalog_dir / "links.jsonl"
        self.degraded: bool = False
        # C1+C2: rebuild SQLite from JSONL on construction to ensure consistency
        if self._documents_path.exists():
            self._ensure_consistent()

    @staticmethod
    def _prefix_sql(prefix: str) -> tuple[str, list]:
        """Return (WHERE clause, params) for exact tumbler prefix matching.

        Uses segment counting to avoid lexicographic ordering bugs with
        dot-separated integers (e.g., '1.10' < '1.9' lexicographically).
        """
        depth = len(prefix.split("."))
        # Match tumblers that start with prefix. and have exactly depth+1 segments
        # e.g. prefix='1.1' (depth=2) matches '1.1.42' but not '1.10.1' or '1.1.42.7'
        like = prefix + ".%"
        # Exclude deeper segments: count dots must equal depth
        return (
            f"tumbler LIKE ? AND (length(tumbler) - length(replace(tumbler, '.', ''))) = ?",
            [like, depth],
        )

    def _ensure_consistent(self) -> None:
        """Rebuild SQLite from JSONL truth. Called when JSONL mtime changes.

        Sets ``degraded`` flag on failure so callers can surface the stale
        state rather than silently serving outdated data (nexus-f2vp).
        """
        try:
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
            links_dict = read_links(self._links_path) if self._links_path.exists() else {}
            _log.debug("catalog_consistency_rebuild")
            self._db.rebuild(owners, documents, list(links_dict.values()))
            self.degraded = False
        except Exception as exc:
            _log.warning("catalog_consistency_rebuild_failed", error=str(exc), exc_info=True)
            self.degraded = True

    def jsonl_paths(self) -> tuple[Path, ...]:
        """Public accessor for JSONL file paths (used by mtime checks)."""
        return (self._owners_path, self._documents_path, self._links_path)

    @classmethod
    def init(cls, catalog_path: Path, remote: str | None = None) -> Catalog:
        """Create catalog git repo with empty JSONL files."""
        git_dir = catalog_path / ".git"
        if remote and not git_dir.exists():
            # Clone from remote if catalog doesn't exist locally (new machine)
            import subprocess as _sp
            result = _sp.run(
                ["git", "clone", remote, str(catalog_path)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                _log.info("catalog_cloned_from_remote", remote=remote)
                db_path = catalog_path / ".catalog.db"
                return cls(catalog_path, db_path)
            else:
                raise RuntimeError(
                    f"Failed to clone catalog from {remote}: {result.stderr.strip()}"
                )
        catalog_path.mkdir(parents=True, exist_ok=True)
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

    def _should_compact(self, ratio: float = 3.0) -> bool:
        """Check if JSONL bloat ratio exceeds threshold."""
        try:
            for path in self.jsonl_paths():
                if not path.exists():
                    continue
                total_lines = sum(1 for line in path.open() if line.strip())
                if total_lines == 0:
                    continue
                live_count = self._db.execute(
                    f"SELECT count(*) FROM {path.stem}"  # owners, documents, links
                ).fetchone()[0]
                if live_count > 0 and total_lines / live_count >= ratio:
                    return True
        except Exception:
            pass
        return False

    def sync(self, message: str = "catalog update") -> None:
        """git add -A && git commit && git push (if remote configured).

        Auto-compacts JSONL files when bloat ratio exceeds 3x live records.
        """
        dir_fd = self._acquire_lock()
        try:
            if self._should_compact():
                _log.info("catalog_auto_defrag")
                self._defrag_unlocked()
            _run_git(["git", "add", "-A"], cwd=self._dir)
            status = _run_git(["git", "status", "--porcelain"], cwd=self._dir)
            if not status.stdout.strip():
                return
            _run_git(["git", "commit", "-m", message], cwd=self._dir)
            remote = _run_git(["git", "remote"], cwd=self._dir, check=False)
            if remote.stdout.strip():
                _run_git(["git", "push", "-u", "origin", "HEAD"], cwd=self._dir, check=False)
        finally:
            self._release_lock(dir_fd)

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
        self, name: str, owner_type: str, *, repo_hash: str = "", description: str = "", repo_root: str = ""
    ) -> Tumbler:
        if repo_root and not Path(repo_root).is_absolute():
            raise ValueError(f"repo_root must be an absolute path: {repo_root!r}")
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
                repo_root=repo_root,
            )
            self._append_jsonl(self._owners_path, rec.__dict__)
            # Upsert SQLite
            self._db.execute(
                "INSERT OR REPLACE INTO owners (tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (prefix, name, owner_type, repo_hash, description, repo_root),
            )
            self._db.commit()
            return Tumbler.parse(prefix)
        finally:
            self._release_lock(dir_fd)

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        row = self._db.execute(
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

            # Idempotency: check by head_hash + title within same owner
            # (content-addressed dedup for re-indexing the same document)
            if head_hash and title:
                prefix_clause, prefix_params = self._prefix_sql(str(owner))
                row = self._db.execute(
                    f"SELECT tumbler FROM documents WHERE {prefix_clause} "
                    f"AND head_hash = ? AND title = ? LIMIT 1",
                    (*prefix_params, head_hash, title),
                ).fetchone()
                if row:
                    return Tumbler.parse(row[0])

            # Permanent addressing: use owner's high-water mark from JSONL,
            # not SQLite MAX(). This prevents tumbler reuse after delete+compact.
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            owner_rec = owners.get(str(owner))
            if owner_rec and owner_rec.next_seq > 0:
                doc_num = owner_rec.next_seq
            else:
                # Fallback for pre-migration owners without next_seq
                doc_num = self._db.next_document_number(str(owner))

            tumbler = Tumbler((*owner.segments, doc_num))

            # Bump and persist the high-water mark
            new_seq = doc_num + 1
            if owner_rec:
                owner_rec.next_seq = new_seq
                self._append_jsonl(self._owners_path, owner_rec.__dict__)
            else:
                # Fallback: owner exists in SQLite but has no JSONL next_seq.
                # Persist it now so future registrations use the JSONL path.
                row = self._db.execute(
                    "SELECT name, owner_type, repo_hash, description FROM owners "
                    "WHERE tumbler_prefix = ?", (str(owner),)
                ).fetchone()
                if row:
                    fallback_rec = OwnerRecord(
                        owner=str(owner), name=row[0], owner_type=row[1],
                        repo_hash=row[2] or "", description=row[3] or "",
                        next_seq=new_seq,
                    )
                    self._append_jsonl(self._owners_path, fallback_rec.__dict__)
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
            self._db.execute(
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
            self._db.commit()
            return tumbler
        finally:
            self._release_lock(dir_fd)

    def resolve(self, tumbler: Tumbler) -> CatalogEntry | None:
        row = self._db.execute(
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

    def resolve_path(self, tumbler: Tumbler) -> Path | None:
        """Return absolute path for the document's file_path.

        Resolution order:
        1. Look up entry via self.resolve(tumbler)
        2. If entry not found: return None
        3. Find owner: tumbler.owner_address() -> str, look up in JSONL
        4. If owner not found or owner.owner_type == "curator": return None
        5. If entry.file_path is already absolute: return Path(entry.file_path)
        6. If owner.repo_root is non-empty: return Path(owner.repo_root) / entry.file_path
        7. Fallback: iterate registry to find path matching owner.repo_hash
        8. If fallback found: return Path(repo_path) / entry.file_path
        9. Otherwise: return None
        """
        import hashlib

        from nexus.registry import RepoRegistry

        entry = self.resolve(tumbler)
        if not entry:
            return None

        # Find owner via SQLite (avoids re-reading JSONL on every call)
        owner_prefix = str(tumbler.owner_address())
        row = self._db.execute(
            "SELECT owner_type, repo_root, repo_hash FROM owners WHERE tumbler_prefix = ?",
            (owner_prefix,),
        ).fetchone()
        if not row:
            return None
        owner_type, repo_root, repo_hash = row[0], row[1], row[2]

        # Curators (PDFs, standalone docs) are not resolvable
        if owner_type == "curator":
            return None

        # If file_path is already absolute, return it directly
        fp = Path(entry.file_path)
        if fp.is_absolute():
            return fp

        # Primary: use repo_root from owner
        if repo_root:
            return Path(repo_root) / entry.file_path

        # Fallback: find repo_root from registry by matching repo_hash
        if repo_hash:
            registry_path = _default_registry_path()
            if registry_path.exists():
                reg = RepoRegistry(registry_path)
                for path_str in reg.all_info():
                    path_hash = hashlib.sha256(path_str.encode()).hexdigest()[:8]
                    if path_hash == repo_hash:
                        return Path(path_str) / entry.file_path

        return None

    def descendants(self, prefix: str) -> list[dict]:
        """All documents whose tumbler starts with *prefix* (any depth).

        Unlike ``by_owner`` which returns only direct children, this returns
        the full subtree.  The prefix itself is excluded.
        """
        return self._db.descendants(prefix)

    def resolve_chunk(self, tumbler: Tumbler) -> dict | None:
        """Resolve a 4-segment chunk tumbler to its document + chunk metadata.

        Chunks are implicit addresses — the catalog tracks document-level entries
        only; chunk sub-addresses are resolved on demand from the document's
        ``chunk_count``.  Resolution parses the document prefix, verifies the
        document exists, and checks the chunk index is in range.

        Returns ``{"document_tumbler", "chunk_index", "physical_collection", ...}``
        or None if the tumbler is not a chunk address or the document/chunk is
        missing.
        """
        if tumbler.chunk is None:
            return None
        doc_tumbler = tumbler.document_address()
        entry = self.resolve(doc_tumbler)
        if entry is None:
            return None
        chunk_idx = tumbler.chunk
        # chunk_count of 0 or None means count is not yet known — skip bounds check
        if entry.chunk_count and chunk_idx >= entry.chunk_count:
            return None
        return {
            "document_tumbler": str(doc_tumbler),
            "chunk_index": chunk_idx,
            "physical_collection": entry.physical_collection,
            "title": entry.title,
            "content_type": entry.content_type,
        }

    def resolve_span(self, span: str, physical_collection: str, t3: "ClientAPI") -> dict | None:
        """Resolve a span string to its chunk content.

        Handles three span formats:
        - chash:<sha256hex>: whole chunk by content hash.
        - chash:<sha256hex>:<start>-<end>: character range within a content-addressed chunk.
        - Legacy positional (digit ranges): returns None.
        """
        if not span.startswith("chash:"):
            return None
        # Parse: chash:<hash> or chash:<hash>:<start>-<end>
        body = span[len("chash:"):]
        char_range = None
        m = re.match(r"^([0-9a-f]{64}):(\d+)-(\d+)$", body)
        if m:
            chunk_hash = m.group(1)
            char_range = (int(m.group(2)), int(m.group(3)))
        elif re.fullmatch(r"[0-9a-f]{64}", body):
            chunk_hash = body
        else:
            raise ValueError(f"malformed chash span: {span!r}")
        col = t3.get_collection(physical_collection)
        result = col.get(where={"chunk_text_hash": chunk_hash}, include=["documents", "metadatas"])
        if not result["ids"]:
            return None
        text = result["documents"][0]
        if char_range:
            text = text[char_range[0]:char_range[1]]
        out: dict = {
            "chunk_text": text,
            "metadata": result["metadatas"][0],
            "chunk_hash": chunk_hash,
        }
        if char_range:
            out["char_range"] = char_range
        return out

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
            # Merge meta dict rather than replace
            if "meta" in fields and isinstance(fields["meta"], dict):
                merged_meta = dict(rec_dict["meta"])
                merged_meta.update(fields["meta"])
                fields = dict(fields, meta=merged_meta)
            rec_dict.update(fields)
            self._append_jsonl(self._documents_path, rec_dict)
            # Upsert SQLite
            self._db.execute(
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
            self._db.commit()
        finally:
            self._release_lock(dir_fd)

    def delete_document(self, tumbler: Tumbler) -> bool:
        """Soft-delete a document: tombstone in JSONL, DELETE from SQLite.

        Links to/from this tumbler are preserved (RF-9: orphaned links intentional).
        Returns True if deleted, False if not found.
        """
        dir_fd = self._acquire_lock()
        try:
            entry = self.resolve(tumbler)
            if entry is None:
                return False
            tombstone = {
                "tumbler": str(tumbler),
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
                "_deleted": True,
            }
            self._append_jsonl(self._documents_path, tombstone)
            self._db.execute("DELETE FROM documents WHERE tumbler = ?", (str(tumbler),))
            self._db.commit()
            return True
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
                meta=json.loads(r["metadata"]) if r.get("metadata") else {},
            )
            for r in rows
        ]

    def by_file_path(self, owner: Tumbler, file_path: str) -> CatalogEntry | None:
        row = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            f"FROM documents WHERE {self._prefix_sql(str(owner))[0]} AND file_path = ?",
            (*self._prefix_sql(str(owner))[1], file_path),
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
        rows = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            f"FROM documents WHERE {self._prefix_sql(str(owner))[0]}",
            self._prefix_sql(str(owner))[1],
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

    def by_content_type(self, content_type: str) -> list[CatalogEntry]:
        """List all entries with the given content type (code, paper, rdr, knowledge)."""
        rows = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents WHERE content_type = ?",
            (content_type,),
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
            )
            for r in rows
        ]

    def by_corpus(self, corpus: str) -> list[CatalogEntry]:
        """List all entries with the given corpus tag."""
        rows = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents WHERE corpus = ?",
            (corpus,),
        ).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
            )
            for r in rows
        ]

    def doc_count(self) -> int:
        """Return the total number of documents in the catalog."""
        row = self._db.execute("SELECT COUNT(*) FROM documents").fetchone()
        return row[0] if row else 0

    def all_documents(self, limit: int = 0) -> list[CatalogEntry]:
        """Return all catalog entries. limit=0 means unlimited."""
        sql = (
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents"
        )
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = self._db.execute(sql).fetchall()
        return [
            CatalogEntry(
                tumbler=Tumbler.parse(r[0]), title=r[1], author=r[2], year=r[3],
                content_type=r[4], file_path=r[5], corpus=r[6],
                physical_collection=r[7], chunk_count=r[8], head_hash=r[9],
                indexed_at=r[10], meta=json.loads(r[11]) if r[11] else {},
            )
            for r in rows
        ]

    def by_doc_id(self, doc_id: str) -> CatalogEntry | None:
        """Look up catalog entry by T3 doc_id stored in meta.doc_id."""
        row = self._db.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata "
            "FROM documents WHERE json_extract(metadata, '$.doc_id') = ?",
            (doc_id,),
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

    # ── Links ──────────────────────────────────────────────────────────────

    def _link_unlocked(
        self,
        from_t: Tumbler,
        to_t: Tumbler,
        link_type: str,
        created_by: str,
        from_span: str,
        to_span: str,
        meta: dict,
        *,
        allow_dangling: bool = False,
    ) -> bool:
        """Core link logic — caller must hold the lock. Returns True if new, False if merged."""
        # Validate span format (Xanadu transclusion addressing)
        for span, label in [(from_span, "from_span"), (to_span, "to_span")]:
            if not _SPAN_PATTERN.match(span):
                raise ValueError(
                    f"invalid {label}: {span!r} — use 'line_start-line_end', "
                    f"'chunk_idx:char_start-char_end', 'chash:<sha256hex>', 'chash:<start>-<end>:<sha256hex>', or '' for whole document"
                )
        if not allow_dangling:
            errors = []
            from_entry = self.resolve(from_t)
            to_entry = self.resolve(to_t)
            if from_entry is None:
                errors.append(f"from_tumbler {from_t} not found")
            if to_entry is None:
                errors.append(f"to_tumbler {to_t} not found")
            if errors:
                raise ValueError(f"dangling link: {'; '.join(errors)}")
            # Validate chash: spans resolve in their document's collection
            for span, entry, label in [
                (from_span, from_entry, "from_span"),
                (to_span, to_entry, "to_span"),
            ]:
                if span.startswith("chash:") and entry and entry.physical_collection:
                    try:
                        from nexus.db import make_t3
                        t3 = make_t3()
                        result = self.resolve_span(span, entry.physical_collection, t3._client)
                        if result is None:
                            errors.append(
                                f"{label} {span!r} does not resolve in "
                                f"collection {entry.physical_collection}"
                            )
                    except Exception:
                        pass  # T3 unavailable — skip validation
            if errors:
                raise ValueError(f"unresolvable span: {'; '.join(errors)}")
        now = datetime.now(UTC).isoformat()
        row = self._db.execute(
            "SELECT id, created_by, metadata, created_at FROM links "
            "WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
            (str(from_t), str(to_t), link_type),
        ).fetchone()

        if row is not None:
            existing_meta = json.loads(row[2]) if row[2] else {}
            existing_meta.update(meta)
            co = existing_meta.get("co_discovered_by", [])
            if created_by != row[1] and created_by not in co:
                co.append(created_by)
            existing_meta["co_discovered_by"] = co
            self._db.execute(
                "UPDATE links SET from_span=?, to_span=?, metadata=? WHERE id=?",
                (from_span, to_span, json.dumps(existing_meta), row[0]),
            )
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=row[1], created_at=row[3] or now, meta=existing_meta,
            )
            self._append_jsonl(self._links_path, rec.__dict__)
            self._db.commit()
            return False
        else:
            combined_meta = dict(meta)
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=created_by, created_at=now, meta=combined_meta,
            )
            self._db.execute(
                "INSERT OR IGNORE INTO links "
                "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
                "created_by, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(from_t), str(to_t), link_type, from_span, to_span,
                 created_by, now, json.dumps(combined_meta)),
            )
            self._append_jsonl(self._links_path, rec.__dict__)
            self._db.commit()
            return True

    def link(
        self,
        from_t: Tumbler,
        to_t: Tumbler,
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        """Create or merge a link. Returns True if new, False if merged.

        Spans accept ``chash:<sha256hex>`` for content-addressed chunk identity
        (preferred) or legacy positional formats. See class docstring for full
        span policy.

        Raises ValueError if either endpoint is missing (unless allow_dangling=True)
        or if a span string does not match ``_SPAN_PATTERN``.
        """
        dir_fd = self._acquire_lock()
        try:
            return self._link_unlocked(
                from_t, to_t, link_type, created_by,
                from_span, to_span, dict(meta),
                allow_dangling=allow_dangling,
            )
        finally:
            self._release_lock(dir_fd)

    def link_if_absent(
        self,
        from_t: Tumbler,
        to_t: Tumbler,
        link_type: str,
        created_by: str,
        *,
        from_span: str = "",
        to_span: str = "",
        allow_dangling: bool = False,
        **meta: object,
    ) -> bool:
        """Create link only if it does not already exist. Returns True=created, False=existed.

        No merge, no co_discovered_by — pure insert-or-skip via UNIQUE constraint.
        No JSONL append on the 'already exists' path.
        Raises ValueError if either endpoint is missing (unless allow_dangling=True).
        """
        # Validate span format before acquiring lock
        for span, label in [(from_span, "from_span"), (to_span, "to_span")]:
            if not _SPAN_PATTERN.match(span):
                raise ValueError(
                    f"invalid {label}: {span!r} — use 'line_start-line_end', "
                    f"'chunk_idx:char_start-char_end', 'chash:<sha256hex>', 'chash:<start>-<end>:<sha256hex>', or '' for whole document"
                )
        dir_fd = self._acquire_lock()
        try:
            row = self._db.execute(
                "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
                (str(from_t), str(to_t), link_type),
            ).fetchone()
            if row is not None:
                return False
            if not allow_dangling:
                errors = []
                from_entry = self.resolve(from_t)
                to_entry = self.resolve(to_t)
                if from_entry is None:
                    errors.append(f"from_tumbler {from_t} not found")
                if to_entry is None:
                    errors.append(f"to_tumbler {to_t} not found")
                if errors:
                    raise ValueError(f"dangling link: {'; '.join(errors)}")
                for span, entry, label in [
                    (from_span, from_entry, "from_span"),
                    (to_span, to_entry, "to_span"),
                ]:
                    if span.startswith("chash:") and entry and entry.physical_collection:
                        try:
                            from nexus.db import make_t3
                            t3 = make_t3()
                            result = self.resolve_span(span, entry.physical_collection, t3._client)
                            if result is None:
                                errors.append(
                                    f"{label} {span!r} does not resolve in "
                                    f"collection {entry.physical_collection}"
                                )
                        except Exception:
                            pass
                if errors:
                    raise ValueError(f"unresolvable span: {'; '.join(errors)}")
            now = datetime.now(UTC).isoformat()
            combined_meta = dict(meta)
            rec = LinkRecord(
                from_t=str(from_t), to_t=str(to_t), link_type=link_type,
                from_span=from_span, to_span=to_span,
                created_by=created_by, created_at=now, meta=combined_meta,
            )
            self._db.execute(
                "INSERT OR IGNORE INTO links "
                "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
                "created_by, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (str(from_t), str(to_t), link_type, from_span, to_span,
                 created_by, now, json.dumps(combined_meta)),
            )
            self._append_jsonl(self._links_path, rec.__dict__)
            self._db.commit()
            return True
        finally:
            self._release_lock(dir_fd)

    def unlink(self, from_t: Tumbler, to_t: Tumbler, link_type: str = "") -> int:
        dir_fd = self._acquire_lock()
        try:
            if link_type:
                rows = self._db.execute(
                    "SELECT id, link_type, created_by FROM links "
                    "WHERE from_tumbler = ? AND to_tumbler = ? AND link_type = ?",
                    (str(from_t), str(to_t), link_type),
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT id, link_type, created_by FROM links "
                    "WHERE from_tumbler = ? AND to_tumbler = ?",
                    (str(from_t), str(to_t)),
                ).fetchall()

            for row_id, lt, original_created_by in rows:
                # Fetch full row for forensic tombstone
                full = self._db.execute(
                    "SELECT from_span, to_span, metadata FROM links WHERE id = ?",
                    (row_id,),
                ).fetchone()
                tombstone = {
                    "from_t": str(from_t),
                    "to_t": str(to_t),
                    "link_type": lt,
                    "_deleted": True,
                    "from_span": full[0] or "" if full else "",
                    "to_span": full[1] or "" if full else "",
                    "created_by": original_created_by,
                    "created_at": datetime.now(UTC).isoformat(),
                    "meta": json.loads(full[2]) if full and full[2] else {},
                }
                self._append_jsonl(self._links_path, tombstone)
                self._db.execute("DELETE FROM links WHERE id = ?", (row_id,))

            self._db.commit()
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
        return [self._row_to_link(r) for r in self._db.execute(sql, params).fetchall()]

    def links_to(self, tumbler: Tumbler, link_type: str = "") -> list[CatalogLink]:
        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links WHERE to_tumbler = ?"
        )
        params: list[str] = [str(tumbler)]
        if link_type:
            sql += " AND link_type = ?"
            params.append(link_type)
        return [self._row_to_link(r) for r in self._db.execute(sql, params).fetchall()]

    def link_query(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        direction: str = "both",
        tumbler: str = "",
        created_at_before: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[CatalogLink]:
        """Composable link filter. Returns CatalogLink list with LIMIT/OFFSET.

        limit=0 means unlimited (maps to SQLite LIMIT -1).
        """
        conditions: list[str] = []
        params: list[str | int] = []

        if tumbler:
            if direction == "out":
                conditions.append("from_tumbler = ?")
                params.append(tumbler)
            elif direction == "in":
                conditions.append("to_tumbler = ?")
                params.append(tumbler)
            else:
                conditions.append("(from_tumbler = ? OR to_tumbler = ?)")
                params.extend([tumbler, tumbler])
        if from_t:
            conditions.append("from_tumbler = ?")
            params.append(from_t)
        if to_t:
            conditions.append("to_tumbler = ?")
            params.append(to_t)
        if link_type:
            conditions.append("link_type = ?")
            params.append(link_type)
        if created_by:
            conditions.append("created_by = ?")
            params.append(created_by)
        if created_at_before:
            conditions.append("created_at != '' AND created_at < ?")
            params.append(created_at_before)

        sql = (
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata FROM links"
        )
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit if limit > 0 else -1, offset])

        return [self._row_to_link(r) for r in self._db.execute(sql, params).fetchall()]

    def bulk_unlink(
        self,
        from_t: str = "",
        to_t: str = "",
        link_type: str = "",
        created_by: str = "",
        created_at_before: str = "",
        dry_run: bool = False,
    ) -> int:
        """Delete links matching filters. Returns count removed.

        Tombstones preserve original created_by for JSONL audit trail.
        dry_run=True returns count without deleting.
        """
        has_filter = any([from_t, to_t, link_type, created_by, created_at_before])
        if not has_filter and not dry_run:
            raise ValueError("bulk_unlink requires at least one filter (or dry_run=True)")

        dir_fd = self._acquire_lock()
        try:
            matching = self.link_query(
                from_t=from_t, to_t=to_t, link_type=link_type,
                created_by=created_by, created_at_before=created_at_before,
                limit=0,
            )

            if dry_run:
                return len(matching)

            for lnk in matching:
                tombstone = {
                    "from_t": str(lnk.from_tumbler), "to_t": str(lnk.to_tumbler),
                    "link_type": lnk.link_type, "_deleted": True,
                    "from_span": lnk.from_span, "to_span": lnk.to_span,
                    "created_by": lnk.created_by,
                    "created_at": datetime.now(UTC).isoformat(),
                    "meta": lnk.meta,
                }
                self._append_jsonl(self._links_path, tombstone)
                self._db.execute(
                    "DELETE FROM links WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
                    (str(lnk.from_tumbler), str(lnk.to_tumbler), lnk.link_type),
                )
            self._db.commit()
            return len(matching)
        finally:
            self._release_lock(dir_fd)

    def validate_link(
        self, from_t: Tumbler, to_t: Tumbler, link_type: str
    ) -> list[str]:
        """Validate a proposed link. Returns list of error strings (empty = valid)."""
        errors: list[str] = []
        if self.resolve(from_t) is None:
            errors.append(f"from_tumbler {from_t} not found in documents")
        if self.resolve(to_t) is None:
            errors.append(f"to_tumbler {to_t} not found in documents")
        row = self._db.execute(
            "SELECT id FROM links WHERE from_tumbler=? AND to_tumbler=? AND link_type=?",
            (str(from_t), str(to_t), link_type),
        ).fetchone()
        if row is not None:
            errors.append(f"duplicate: link ({from_t}, {to_t}, {link_type!r}) already exists")
        return errors

    def resolve_span_text(self, tumbler: Tumbler, span: str) -> str | None:
        """Resolve a span to actual text content. Returns None if unavailable.

        Span formats:
        - "" → returns None (whole document, no sub-addressing)
        - "42-57" → lines 42-57 from the document's source file
        - "3:100-250" → characters 100-250 from chunk index 3 in T3
        - "chash:<sha256hex>" → content-addressed chunk from T3

        This is the minimal transclusion read path — given a link with a span,
        retrieve the exact passage being referenced.
        """
        if not span:
            return None
        entry = self.resolve(tumbler)
        if entry is None:
            return None

        # Content-hash span: look up by chunk_text_hash in T3
        if span.startswith("chash:") and entry.physical_collection:
            try:
                from nexus.db import make_t3
                t3 = make_t3()
                result = self.resolve_span(span, entry.physical_collection, t3._client)
                return result["chunk_text"] if result else None
            except Exception:
                _log.warning("resolve_span_text_failed", span=span,
                             collection=entry.physical_collection, exc_info=True)
                return None

        # Line-range span: read from source file
        m = re.match(r"^(\d+)-(\d+)$", span)
        if m and entry.file_path:
            start, end = int(m.group(1)), int(m.group(2))
            try:
                lines = Path(entry.file_path).read_text(encoding="utf-8").splitlines()
                return "\n".join(lines[start - 1:end])
            except Exception:
                return None

        # Chunk:char span: read from T3
        m = re.match(r"^(\d+):(\d+)-(\d+)$", span)
        if m and entry.physical_collection:
            chunk_idx, char_start, char_end = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                from nexus.db import make_t3
                t3 = make_t3()
                col = t3.get_or_create_collection(entry.physical_collection)
                # Query by chunk_index metadata for deterministic ordering
                where_filter: dict = {"chunk_index": chunk_idx}
                if entry.file_path:
                    where_filter["source_path"] = entry.file_path
                result = col.get(
                    where=where_filter if len(where_filter) == 1 else {"$and": [{k: v} for k, v in where_filter.items()]},
                    include=["documents"],
                    limit=1,
                )
                docs = result.get("documents", [])
                if docs:
                    text = docs[0]
                    return text[char_start:char_end]
            except Exception:
                return None

        return None

    def link_audit(self, *, t3: "ClientAPI | None" = None) -> dict:
        """Audit the links table. Returns stats + orphan + duplicate + chash lists.

        When ``t3`` is provided, verifies each ``chash:`` span resolves to a
        chunk in the corresponding ChromaDB collection. Unresolvable spans
        appear in ``stale_chash``.

        Args:
            t3: Raw ChromaDB client (``chromadb.ClientAPI``), not a ``T3Database``.
                Production callers pass ``t3_db._client``; tests pass an
                ``EphemeralClient`` directly. Injection keeps the method testable
                without ``make_t3()``.
        """
        total = self._db.execute("SELECT count(*) FROM links").fetchone()[0]
        by_type = dict(
            self._db.execute(
                "SELECT link_type, count(*) FROM links GROUP BY link_type"
            ).fetchall()
        )
        by_creator = dict(
            self._db.execute(
                "SELECT created_by, count(*) FROM links GROUP BY created_by"
            ).fetchall()
        )
        orphan_rows = self._db.execute(
            "SELECT from_tumbler, to_tumbler, link_type FROM links l "
            "WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.tumbler = l.from_tumbler) "
            "   OR NOT EXISTS (SELECT 1 FROM documents d WHERE d.tumbler = l.to_tumbler)"
        ).fetchall()
        orphaned = [{"from": r[0], "to": r[1], "type": r[2]} for r in orphan_rows]
        dup_rows = self._db.execute(
            "SELECT from_tumbler, to_tumbler, link_type, count(*) AS cnt "
            "FROM links GROUP BY from_tumbler, to_tumbler, link_type HAVING cnt > 1"
        ).fetchall()
        duplicates = [
            {"from": r[0], "to": r[1], "type": r[2], "count": r[3]} for r in dup_rows
        ]
        # Stale spans: positional spans pointing to documents re-indexed after link creation.
        # Content-hash spans (chash:) are excluded — they survive re-indexing by design
        # (RDR-053). Stale chash spans are detected separately via T3 verification below.
        # Checks both from_span (joined on from_tumbler) and to_span (joined on to_tumbler).
        # datetime() wraps ensure correct comparison regardless of ISO-8601 padding.
        stale_span_rows = self._db.execute(
            "SELECT l.from_tumbler, l.to_tumbler, l.link_type, l.created_at, "
            "       d.indexed_at, 'from' AS side "
            "FROM links l "
            "JOIN documents d ON d.tumbler = l.from_tumbler "
            "WHERE (l.from_span IS NOT NULL AND l.from_span != '') "
            "  AND l.from_span NOT LIKE 'chash:%' "
            "  AND datetime(l.created_at) < datetime(d.indexed_at) "
            "UNION ALL "
            "SELECT l.from_tumbler, l.to_tumbler, l.link_type, l.created_at, "
            "       d.indexed_at, 'to' AS side "
            "FROM links l "
            "JOIN documents d ON d.tumbler = l.to_tumbler "
            "WHERE (l.to_span IS NOT NULL AND l.to_span != '') "
            "  AND l.to_span NOT LIKE 'chash:%' "
            "  AND datetime(l.created_at) < datetime(d.indexed_at)"
        ).fetchall()
        stale_spans = [
            {"from": r[0], "to": r[1], "type": r[2],
             "link_created": r[3], "doc_reindexed": r[4], "side": r[5]}
            for r in stale_span_rows
        ]
        # chash verification: check each chash: span resolves in T3
        stale_chash: list[dict] = []
        if t3 is not None:
            chash_rows = self._db.execute(
                "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span "
                "FROM links WHERE from_span LIKE 'chash:%' OR to_span LIKE 'chash:%'"
            ).fetchall()
            for row in chash_rows:
                from_t, to_t, lt, from_span, to_span = row
                for span, tumbler_str in [(from_span, from_t), (to_span, to_t)]:
                    if not span.startswith("chash:"):
                        continue
                    # Extract hash from chash:<hash> or chash:<hash>:<start>-<end>
                    body = span[len("chash:"):]
                    m_range = re.match(r"^([0-9a-f]{64}):\d+-\d+$", body)
                    chunk_hash = m_range.group(1) if m_range else body
                    entry = self.resolve(Tumbler.parse(tumbler_str))
                    if entry is None:
                        stale_chash.append(
                            {"from": from_t, "to": to_t, "type": lt, "span": span,
                             "reason": "document_deleted"}
                        )
                        continue
                    try:
                        col = t3.get_collection(entry.physical_collection)
                        result = col.get(
                            where={"chunk_text_hash": chunk_hash}, include=[]
                        )
                        if not result["ids"]:
                            stale_chash.append(
                                {"from": from_t, "to": to_t, "type": lt, "span": span,
                                 "reason": "missing"}
                            )
                    except Exception as exc:
                        _log.warning(
                            "link_audit_chash_error",
                            tumbler=tumbler_str, span=span,
                            exc_info=True,
                        )
                        stale_chash.append(
                            {"from": from_t, "to": to_t, "type": lt, "span": span,
                             "reason": "error", "error": type(exc).__name__}
                        )

        return {
            "total": total,
            "by_type": by_type,
            "by_creator": by_creator,
            "orphaned": orphaned,
            "orphaned_count": len(orphaned),
            "duplicates": duplicates,
            "duplicate_count": len(duplicates),
            "stale_spans": stale_spans,
            "stale_span_count": len(stale_spans),
            "stale_chash": stale_chash,
            "stale_chash_count": len(stale_chash),
        }

    _MAX_GRAPH_DEPTH = 10
    _MAX_GRAPH_NODES = 500

    def graph(
        self,
        tumbler: Tumbler,
        depth: int = 1,
        direction: str = "both",
        link_type: str = "",
    ) -> dict:
        """BFS traversal to given depth. Returns {"nodes": [...], "edges": [...]}.

        Depth capped at _MAX_GRAPH_DEPTH. Traversal stops at _MAX_GRAPH_NODES visited.
        """
        depth = min(depth, self._MAX_GRAPH_DEPTH)
        visited: set[str] = {str(tumbler)}
        seen_edges: set[tuple[str, str, str]] = set()
        all_edges: list[CatalogLink] = []
        queue: deque[tuple[Tumbler, int]] = deque([(tumbler, 0)])

        while queue:
            if len(visited) >= self._MAX_GRAPH_NODES:
                _log.warning("graph_node_limit", tumbler=str(tumbler), visited=len(visited))
                break
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

    def _defrag_unlocked(self) -> dict[str, int]:
        """Core defrag logic — caller must hold the lock."""
        removed = {}
        for path in [self._owners_path, self._documents_path, self._links_path]:
            if not path.exists():
                continue
            original_lines = sum(1 for line in path.open() if line.strip())
            seen: dict[str, str] = {}
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "owner" in obj:
                        key = obj["owner"]
                    elif "tumbler" in obj:
                        key = obj["tumbler"]
                    elif "from_t" in obj:
                        key = f"{obj['from_t']}|{obj['to_t']}|{obj['link_type']}"
                    else:
                        continue
                    seen[key] = line
            with path.open("w") as f:
                for line in seen.values():
                    f.write(line + "\n")
            removed[path.name] = original_lines - len(seen)
            # Rebuild SQLite from defragged JSONL to stay consistent
        owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
        documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
        links_dict = read_links(self._links_path) if self._links_path.exists() else {}
        self._db.rebuild(owners, documents, list(links_dict.values()))
        return removed

    def defrag(self) -> dict[str, int]:
        """Deduplicate JSONL files — keep latest version of each live record.

        Removes duplicate overwrites but preserves tombstones (deletion markers).
        This is the safe compaction: no history is lost, deleted tumblers remain
        reserved, and the version record is intact for forensic purposes.
        Returns count of lines removed per file.
        """
        dir_fd = self._acquire_lock()
        try:
            return self._defrag_unlocked()
        finally:
            self._release_lock(dir_fd)

    def compact(self) -> dict[str, int]:
        """Full compaction: deduplicate AND remove tombstones.

        This erases deletion history — tombstoned tumblers are no longer
        visible in the JSONL (though they remain reserved via owner next_seq).
        Use defrag() for safe compaction that preserves tombstones.
        """
        dir_fd = self._acquire_lock()
        try:
            removed = {}
            for path, reader in [
                (self._owners_path, read_owners),
                (self._documents_path, read_documents),
                (self._links_path, read_links),
            ]:
                if not path.exists():
                    continue
                original_lines = sum(1 for line in path.open() if line.strip())
                records = reader(path)
                with path.open("w") as f:
                    for record in records.values():
                        f.write(json.dumps(record.__dict__, default=str) + "\n")
                new_lines = len(records)
                removed[path.name] = original_lines - new_lines
            # Rebuild SQLite from compacted JSONL
            owners = read_owners(self._owners_path) if self._owners_path.exists() else {}
            documents = read_documents(self._documents_path) if self._documents_path.exists() else {}
            links_dict = read_links(self._links_path) if self._links_path.exists() else {}
            self._db.rebuild(owners, documents, list(links_dict.values()))
            return removed
        finally:
            self._release_lock(dir_fd)
