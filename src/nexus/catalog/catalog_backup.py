# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backup-before-delete safety net for the catalog (4.29.1 / RDR-106
Option A).

Every destructive catalog verb (``delete``, ``gc``, ``prune-stale``,
``link-bulk-delete``, ``t3 prune-stale``) snapshots the rows
about to be deleted to JSONL under
``$NEXUS_CONFIG_DIR/catalog/.deleted-backups/`` BEFORE the actual
delete. ``nx catalog undelete <backup-file>`` re-registers them;
``nx catalog vacuum-backups`` drops files older than the retention
window (default 30 days).

Pure recovery layer — no schema change, no projector change, no
read-path filter. The backup files are out-of-tree (gitignored
by the catalog dir's ``.gitignore``) and per-machine.

Storage layout::

    $NEXUS_CONFIG_DIR/catalog/.deleted-backups/
        catalog-delete-2026-05-08T20-15-00-<short>.jsonl
        catalog-gc-2026-05-08T21-30-00-<short>.jsonl
        catalog-prune-stale-2026-05-08T22-00-00-<short>.jsonl
        catalog-link-bulk-delete-2026-05-08T22-15-00-<short>.jsonl
        t3-prune-stale-2026-05-08T22-30-00-<short>.jsonl

Each file is JSONL with a header record (``kind="header"``) carrying
the verb, timestamp, reason, and operator-supplied filter args, plus
one record per deleted entity. The header is the first line so a
``head -1`` quickly summarises what a backup contains.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import structlog

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog

_log = structlog.get_logger(__name__)


_BACKUP_DIRNAME: str = ".deleted-backups"
_DEFAULT_RETENTION_DAYS: int = 30


def _backup_dir(catalog: "Catalog") -> Path:
    """Return the backup dir path; create it on demand."""
    d = catalog._dir / _BACKUP_DIRNAME
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _timestamp() -> str:
    """ISO 8601 timestamp with hyphens (filesystem-safe)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")


def _short_id() -> str:
    """4-byte hex tag; collision-resistant within one timestamp second."""
    return secrets.token_hex(4)


@dataclass(frozen=True)
class BackupRecord:
    """Metadata for a single backup file (the header line)."""

    verb: str
    timestamp: str
    reason: str
    args: dict
    rows_count: int
    path: Path

    @classmethod
    def from_file(cls, path: Path) -> "BackupRecord":
        """Read the header line + count rows from a backup file."""
        with path.open("r") as f:
            header_line = f.readline()
        header = json.loads(header_line)
        rows = sum(1 for _ in path.open()) - 1  # subtract header
        return cls(
            verb=header.get("verb", ""),
            timestamp=header.get("timestamp", ""),
            reason=header.get("reason", ""),
            args=header.get("args", {}),
            rows_count=rows,
            path=path,
        )


def snapshot_documents(
    catalog: "Catalog",
    tumblers: Iterable[str],
    *,
    verb: str,
    reason: str = "",
    args: dict | None = None,
) -> Path | None:
    """Write a JSONL snapshot of the documents about to be deleted.

    Returns the backup file path, or ``None`` if no rows were
    snapshotted (caller should still proceed with the delete; the
    no-rows case is benign — there's nothing to back up).

    Captures the full document row, plus its inbound and outbound
    links, so an ``undelete`` can fully reconstruct the document
    AND its position in the link graph.
    """
    tumbler_list = list(tumblers)
    if not tumbler_list:
        return None

    backup_dir = _backup_dir(catalog)
    fname = f"catalog-{verb}-{_timestamp()}-{_short_id()}.jsonl"
    path = backup_dir / fname

    placeholders = ",".join("?" * len(tumbler_list))
    rows = catalog._db.execute(
        f"SELECT tumbler, title, author, year, content_type, file_path, "
        f"corpus, physical_collection, chunk_count, head_hash, "
        f"indexed_at, metadata, source_mtime, alias_of, source_uri "
        f"FROM documents WHERE tumbler IN ({placeholders})",
        tumbler_list,
    ).fetchall()
    if not rows:
        return None

    # Inbound + outbound links per tumbler so undelete can recreate
    # the link graph.
    links_by_tumbler: dict[str, list[dict]] = {}
    for t in tumbler_list:
        from_rows = catalog._db.execute(
            "SELECT from_tumbler, to_tumbler, link_type, from_span, "
            "to_span, created_by, created_at, metadata FROM links "
            "WHERE from_tumbler = ? OR to_tumbler = ?",
            (t, t),
        ).fetchall()
        links_by_tumbler[t] = [
            {
                "from": fr[0], "to": fr[1], "link_type": fr[2],
                "from_span": fr[3] or "", "to_span": fr[4] or "",
                "created_by": fr[5], "created_at": fr[6] or "",
                "meta": json.loads(fr[7]) if fr[7] else {},
            }
            for fr in from_rows
        ]

    with path.open("w") as f:
        # Header.
        header = {
            "kind": "header",
            "verb": verb,
            "timestamp": datetime.now(UTC).isoformat(),
            "reason": reason,
            "args": args or {},
            "rows_count": len(rows),
        }
        f.write(json.dumps(header) + "\n")
        # Rows.
        for row in rows:
            (tumbler, title, author, year, content_type, file_path,
             corpus, physical_collection, chunk_count, head_hash,
             indexed_at, metadata_json, source_mtime, alias_of,
             source_uri) = row
            rec = {
                "kind": "document",
                "tumbler": tumbler,
                "title": title or "",
                "author": author or "",
                "year": year or 0,
                "content_type": content_type or "",
                "file_path": file_path or "",
                "corpus": corpus or "",
                "physical_collection": physical_collection or "",
                "chunk_count": chunk_count or 0,
                "head_hash": head_hash or "",
                "indexed_at": indexed_at or "",
                "metadata": (
                    json.loads(metadata_json) if metadata_json else {}
                ),
                "source_mtime": source_mtime or 0.0,
                "alias_of": alias_of or "",
                "source_uri": source_uri or "",
                "links": links_by_tumbler.get(tumbler, []),
            }
            f.write(json.dumps(rec) + "\n")

    # Permissions: backup carries deleted content, owner-only read.
    os.chmod(path, 0o600)
    _log.info(
        "catalog_backup_written",
        verb=verb,
        path=str(path),
        rows=len(rows),
    )
    return path


def snapshot_links(
    catalog: "Catalog",
    links: Iterable[dict],
    *,
    verb: str,
    reason: str = "",
    args: dict | None = None,
) -> Path | None:
    """JSONL snapshot of links about to be deleted.

    Each link dict carries from_tumbler / to_tumbler / link_type /
    spans / metadata so undelete can re-emit ``LinkCreated`` events.
    """
    link_list = list(links)
    if not link_list:
        return None

    backup_dir = _backup_dir(catalog)
    fname = f"catalog-{verb}-{_timestamp()}-{_short_id()}.jsonl"
    path = backup_dir / fname

    with path.open("w") as f:
        header = {
            "kind": "header",
            "verb": verb,
            "timestamp": datetime.now(UTC).isoformat(),
            "reason": reason,
            "args": args or {},
            "rows_count": len(link_list),
        }
        f.write(json.dumps(header) + "\n")
        for link in link_list:
            f.write(json.dumps({"kind": "link", **link}) + "\n")

    os.chmod(path, 0o600)
    _log.info(
        "catalog_backup_written",
        verb=verb,
        path=str(path),
        rows=len(link_list),
    )
    return path


def list_backups(catalog: "Catalog") -> list[BackupRecord]:
    """All backups, newest first."""
    d = catalog._dir / _BACKUP_DIRNAME
    if not d.exists():
        return []
    files = [
        p for p in d.glob("*.jsonl")
        if p.is_file() and p.stat().st_size > 0
    ]
    records = []
    for p in sorted(files, key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            records.append(BackupRecord.from_file(p))
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning(
                "catalog_backup_unreadable",
                path=str(p), error=str(exc),
            )
    return records


def restore_documents(
    catalog: "Catalog", backup_path: Path,
) -> tuple[int, int]:
    """Re-register all documents from a backup file.

    Returns ``(restored_documents, restored_links)``.

    Documents are re-registered via ``Catalog.register`` (event-sourced;
    DocumentRegistered event lands in events.jsonl). Links are
    re-emitted via ``Catalog.link`` (LinkCreated event).

    Idempotent: re-registering an already-existing tumbler is a
    DocumentRegistered-on-existing, which the projector handles via
    INSERT OR REPLACE. Re-emitting an already-existing link merges
    into the existing row's metadata.
    """
    from nexus.catalog.tumbler import Tumbler

    if not backup_path.exists():
        raise FileNotFoundError(f"backup not found: {backup_path}")

    restored_docs = 0
    restored_links = 0
    pending_links: list[dict] = []

    with backup_path.open("r") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as exc:
                _log.warning(
                    "catalog_backup_skip_malformed",
                    path=str(backup_path), lineno=lineno, error=str(exc),
                )
                continue
            kind = rec.get("kind", "")
            if kind == "header":
                continue
            if kind == "document":
                # Re-register via catalog.register. Owner is derived
                # from tumbler prefix: "1.5.233" → owner "1.5".
                t_str = rec["tumbler"]
                parts = t_str.split(".")
                if len(parts) < 3:
                    _log.warning(
                        "catalog_backup_skip_no_owner",
                        tumbler=t_str,
                    )
                    continue
                owner = Tumbler.parse(".".join(parts[:2]))
                # Best-effort restore: register with the original
                # title, content_type, etc. Catalog.register normally
                # mints a fresh tumbler; here we want the ORIGINAL
                # tumbler back, which means using the lower-level
                # event-emit path. Use Catalog._write_to_event_log
                # directly with a DocumentRegistered payload.
                from nexus.catalog.events import (
                    DocumentRegisteredPayload as _DocPayload,
                )
                from nexus.catalog.catalog import _make_event
                payload = _DocPayload(
                    doc_id=t_str,
                    owner_id=str(owner),
                    content_type=rec.get("content_type", ""),
                    source_uri=rec.get("source_uri", ""),
                    coll_id=rec.get("physical_collection", ""),
                    title=rec.get("title", ""),
                    source_mtime=float(rec.get("source_mtime", 0.0)),
                    indexed_at_doc=rec.get("indexed_at", ""),
                    tumbler=t_str,
                    author=rec.get("author", ""),
                    year=int(rec.get("year", 0)),
                    file_path=rec.get("file_path", ""),
                    corpus=rec.get("corpus", ""),
                    physical_collection=rec.get(
                        "physical_collection", "",
                    ),
                    chunk_count=int(rec.get("chunk_count", 0)),
                    head_hash=rec.get("head_hash", ""),
                    indexed_at=rec.get("indexed_at", ""),
                    alias_of=rec.get("alias_of", ""),
                    meta=dict(rec.get("metadata", {})),
                )
                event = _make_event(payload, v=0)
                dir_fd = catalog._acquire_lock()
                try:
                    catalog._write_to_event_log(event)
                    catalog._projector.apply(event)
                    catalog._db.commit()
                finally:
                    catalog._release_lock(dir_fd)
                restored_docs += 1
                # Defer link restoration until all docs are back so
                # the link's endpoints exist.
                for link in rec.get("links", []):
                    pending_links.append(link)
            elif kind == "link":
                pending_links.append(rec)

    # Re-emit links via catalog.link_if_absent (idempotent).
    for link in pending_links:
        try:
            from_t = Tumbler.parse(link["from"])
            to_t = Tumbler.parse(link["to"])
            catalog.link_if_absent(
                from_t, to_t, link["link_type"],
                link.get("created_by", "undelete"),
                from_span=link.get("from_span", ""),
                to_span=link.get("to_span", ""),
                allow_dangling=True,
                **link.get("meta", {}),
            )
            restored_links += 1
        except Exception as exc:
            _log.warning(
                "catalog_backup_link_restore_failed",
                from_t=link.get("from", ""),
                to_t=link.get("to", ""),
                error=str(exc),
            )

    _log.info(
        "catalog_backup_restored",
        path=str(backup_path),
        documents=restored_docs,
        links=restored_links,
    )
    return restored_docs, restored_links


def vacuum_old_backups(
    catalog: "Catalog",
    *,
    older_than_days: int = _DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Drop backup files older than the retention window.

    Returns ``(removed_count, kept_count)``. With ``dry_run=True``
    nothing is removed; the counts are what WOULD happen.
    """
    d = catalog._dir / _BACKUP_DIRNAME
    if not d.exists():
        return (0, 0)
    cutoff = time.time() - (older_than_days * 86400)
    removed = 0
    kept = 0
    for p in d.glob("*.jsonl"):
        if p.stat().st_mtime < cutoff:
            if not dry_run:
                p.unlink()
                _log.info("catalog_backup_vacuumed", path=str(p))
            removed += 1
        else:
            kept += 1
    return (removed, kept)
