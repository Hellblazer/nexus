"""The garbage sweep: one place that finds and removes what nothing else reaps.

Sam, 2026-09-05, after a by-hand census found 706 stale mint-lock files,
38 MB of rotated crash logs, 508 MB of pre-PG SQLite relics, a 37 GB
pre-migration archive, 61 orphaned catalog links and 880 tombstoned
documents with 1,503 stranded chunks, none of it reported anywhere: "come
up with a small tight plan to stop doing that". This module is that plan's
third item. Every class of litter this repo has produced gets a row here,
``nx doctor`` reports the counts on every run, and ``nx doctor --fix`` is
the one command that reclaims them. A new litter class is a new row in
:func:`sweep_local_garbage` or :func:`catalog_garbage`, never a new
command and never a note in a handoff.

Two halves, split by whether the reclaim touches the engine:

* **Local files** (:func:`sweep_local_garbage`): pure ``os`` work under the
  nexus config dir. Reaped by ``nx doctor`` itself on every run, the way
  the T1 lease and handoff-marker reapers already behave, because a stale
  lock file or a two-week-old rotated log has no restore path worth
  protecting.
* **Catalog** (:func:`catalog_garbage` / :func:`reclaim_catalog_garbage`):
  orphaned links (an endpoint that resolves to no live document) and
  tombstoned documents past the purge window. Counted on every doctor run,
  reclaimed only under ``--fix`` because each reclaim is an engine write.

Ages are day-granular and deliberately short. A rotated log is kept 14
days, an operator dispatch log 7, a mint lock 1: all of them exist for
post-mortem reading within hours of the event, never weeks.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import structlog

_log = structlog.get_logger(__name__)

#: A rotated log (``name.log.N``) older than this is deleted.
ROTATED_LOG_MAX_AGE_DAYS: int = 14
#: An operator dispatch dump (``operator-timeout-*.log``,
#: ``operator-budget-*.log``) older than this is deleted.
OPERATOR_LOG_MAX_AGE_DAYS: int = 7
#: A ``t1_mint_<session>.lock`` file older than this with no live lease
#: for its session is deleted. The lease is the liveness signal; the lock
#: file is only the flock's inode.
MINT_LOCK_MAX_AGE_DAYS: int = 1
#: Tombstoned catalog documents older than this are purged under --fix.
#: One day, not thirty: the reversible period for a delete, matching
#: ``nx catalog purge-trash``'s default since 7.32.0.
TRASH_MAX_AGE_DAYS: int = 1

_ROTATED_LOG_RE = re.compile(r"\.log\.\d+$")
_OPERATOR_LOG_RE = re.compile(r"^operator-(timeout|budget)-.*\.log$")
_MINT_LOCK_PREFIX = "t1_mint_"
_MINT_LOCK_SUFFIX = ".lock"
_LEASE_PREFIX = "t1_session_lease."


@dataclass
class SweepReport:
    """What one local sweep found and removed, per litter class."""

    removed: dict[str, list[str]] = field(default_factory=dict)
    failed: dict[str, list[str]] = field(default_factory=dict)

    @property
    def removed_count(self) -> int:
        return sum(len(v) for v in self.removed.values())

    @property
    def failed_count(self) -> int:
        return sum(len(v) for v in self.failed.values())

    def add(self, kind: str, path: Path, *, ok: bool) -> None:
        bucket = self.removed if ok else self.failed
        bucket.setdefault(kind, []).append(path.name)


def _older_than(path: Path, days: int, *, now: float) -> bool:
    try:
        return (now - path.stat().st_mtime) > days * 86_400
    except OSError:
        return False


def _unlink(path: Path, kind: str, report: SweepReport) -> None:
    try:
        path.unlink()
        report.add(kind, path, ok=True)
    except OSError as exc:
        _log.warning("garbage_unlink_failed", kind=kind, path=str(path), error=str(exc))
        report.add(kind, path, ok=False)


def sweep_local_garbage(config_dir: Path, *, now: float | None = None) -> SweepReport:
    """Delete stale local litter under *config_dir* and report what went.

    Classes, each keyed by name in the report:

    * ``rotated_log``: ``logs/*.log.N`` older than :data:`ROTATED_LOG_MAX_AGE_DAYS`.
    * ``operator_log``: ``logs/operator-{timeout,budget}-*.log`` older than
      :data:`OPERATOR_LOG_MAX_AGE_DAYS`.
    * ``mint_lock``: ``t1_mint_<session>.lock`` older than
      :data:`MINT_LOCK_MAX_AGE_DAYS` whose session has no lease file. A
      lock with a live lease is never touched, whatever its age.

    Never raises on a single file: a failed unlink lands in
    ``report.failed`` and the sweep continues.
    """
    now = time.time() if now is None else now
    report = SweepReport()
    if not config_dir.is_dir():
        return report

    logs_dir = config_dir / "logs"
    if logs_dir.is_dir():
        for path in sorted(logs_dir.iterdir()):
            if not path.is_file():
                continue
            if _ROTATED_LOG_RE.search(path.name):
                if _older_than(path, ROTATED_LOG_MAX_AGE_DAYS, now=now):
                    _unlink(path, "rotated_log", report)
            elif _OPERATOR_LOG_RE.match(path.name):
                if _older_than(path, OPERATOR_LOG_MAX_AGE_DAYS, now=now):
                    _unlink(path, "operator_log", report)

    live_sessions = {
        p.name[len(_LEASE_PREFIX):]
        for p in config_dir.glob(f"{_LEASE_PREFIX}*")
    }
    for path in sorted(config_dir.glob(f"{_MINT_LOCK_PREFIX}*{_MINT_LOCK_SUFFIX}")):
        session_id = path.name[len(_MINT_LOCK_PREFIX):-len(_MINT_LOCK_SUFFIX)]
        if session_id in live_sessions:
            continue
        if _older_than(path, MINT_LOCK_MAX_AGE_DAYS, now=now):
            _unlink(path, "mint_lock", report)

    if report.removed_count or report.failed_count:
        _log.info(
            "garbage_local_sweep",
            removed={k: len(v) for k, v in report.removed.items()},
            failed={k: len(v) for k, v in report.failed.items()},
        )
    return report


class CatalogGarbageClient(Protocol):
    """The two catalog surfaces the sweep needs; ``HttpCatalogClient`` satisfies it."""

    def orphaned_links(self) -> list[dict]: ...
    def unlink(self, from_t: Any, to_t: Any, link_type: str) -> int: ...
    def purge_trash(self, older_than_days: int = ..., *, dry_run: bool = ...) -> dict: ...


@dataclass
class CatalogGarbage:
    """Counts of catalog litter, read-only."""

    orphaned_links: int
    trash_documents: int
    stranded_chunks: int
    error: str | None = None

    @property
    def total(self) -> int:
        return self.orphaned_links + self.trash_documents + self.stranded_chunks


def _stranded_total(result: dict) -> int:
    return sum(int(v) for k, v in result.items() if k.startswith("chunks_") and k.endswith("_stranded"))


def catalog_garbage(client: CatalogGarbageClient) -> CatalogGarbage:
    """Count orphaned links and past-window trash. Never raises; an
    unreachable engine is reported in ``error`` with zero counts, and the
    caller must not read zero-with-error as clean."""
    try:
        links = client.orphaned_links()
        trash = client.purge_trash(older_than_days=TRASH_MAX_AGE_DAYS, dry_run=True)
    except Exception as exc:  # noqa: BLE001 - a doctor check reports, never crashes the run
        return CatalogGarbage(0, 0, 0, error=str(exc))
    return CatalogGarbage(
        orphaned_links=len(links),
        trash_documents=int(trash.get("documents_purged", 0)),
        stranded_chunks=_stranded_total(trash),
    )


@dataclass
class ReclaimReport:
    links_deleted: int
    trash_documents: int
    stranded_chunks: int


def reclaim_catalog_garbage(client: CatalogGarbageClient) -> ReclaimReport:
    """Delete every orphaned link, then purge trash past the window.

    Links first: a tombstoned document's links are what stranded the 61
    found on 2026-09-05, and the purge does not touch links. Raises on an
    engine error; ``--fix`` is an explicit act and must fail loud.
    """
    deleted = 0
    for link in client.orphaned_links():
        deleted += client.unlink(link["from_tumbler"], link["to_tumbler"], link["link_type"])
    result = client.purge_trash(older_than_days=TRASH_MAX_AGE_DAYS, dry_run=False)
    report = ReclaimReport(
        links_deleted=deleted,
        trash_documents=int(result.get("documents_purged", 0)),
        stranded_chunks=_stranded_total(result),
    )
    _log.info(
        "garbage_catalog_reclaim",
        links_deleted=report.links_deleted,
        trash_documents=report.trash_documents,
        stranded_chunks=report.stranded_chunks,
    )
    return report
