# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 6 (nexus-u5m5g.7): the client half of a catalog reconcile run.

``indexer._catalog_hook`` walks the files, stats and hashes them and builds one landing row per file (the work only the
client can do); this module stages those rows on the engine, commits them as one job, and rebuilds from the commit's
outcome every structure the hook's callers read, with no further request (RDR-193 Part A2). The diff against the
catalog, the upsert, the miss-count state machine, rename detection and the explicit deletions are the engine's.

There is no fallback: a commit that fails raises :class:`~nexus.catalog.http_catalog_client.CatalogReconcileError` and the
hook treats the run as unregistered, so no chunk of the run is written under a document the catalog never committed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger(__name__)

#: Rows per stage call: the engine's cap (``CatalogHandler.MAX_BATCH_DOC_IDS``).
STAGE_PAGE = 1000


def stage_and_commit(writer: Any, owner: Any, rows: list[dict], *, walk: str, page: int = STAGE_PAGE) -> dict:
    """Stage *rows* (pages of at most *page*), commit the run once and return the outcome.

    ``walk`` is ``complete`` (the walk saw every file of the repository, so a missing one is a candidate for the
    miss-count sweep and for rename detection) or ``partial`` (a delta walk: nothing is swept, renamed or counted
    missing, and only the rows named are touched). An empty run still sends one empty page, which is what creates the
    run's header for the commit.
    """
    run_id = f"idx-{uuid.uuid4()}"
    total = len(rows)
    for start in range(0, max(total, 1), page):
        writer.reconcile_stage(str(owner), run_id, walk, total, rows[start:start + page])
    return writer.reconcile_commit(str(owner), run_id)


@dataclass
class Applied:
    """What the hook's callers read, rebuilt from one commit outcome."""

    file_to_doc_id: dict[Path, str] = field(default_factory=dict)
    new_tumblers: list[Tumbler] = field(default_factory=list)
    new_content_types: set[str] = field(default_factory=set)
    relink_rdr_tumblers: list[Tumbler] = field(default_factory=list)
    needs_fence: dict[str, tuple[str, str]] = field(default_factory=dict)
    stale_fence_doc_ids: set[str] = field(default_factory=set)
    complete_doc_hashes: dict[str, str] = field(default_factory=dict)
    sensitive_confirmed: set[Path] = field(default_factory=set)
    #: tumbler -> the files of this run a ``--sensitive-glob`` matched that the commit left unflagged
    sensitivity_pending: dict[str, list[Path]] = field(default_factory=dict)
    reconciled: list[tuple[Path, str]] = field(default_factory=list)
    refused: dict[Path, str] = field(default_factory=dict)
    updated: int = 0


def apply_outcome(outcome: dict, *, rel_to_abs: dict[str, Path], staged: dict[str, dict]) -> Applied:
    """Rebuild the hook's out-structures from the outcome's ``rows`` (RDR-193 Part A2, lines 949-965).

    *rel_to_abs* maps the staged ``file_path`` of each present row to the file; *staged* holds the row as staged. An
    outcome row for a path outside *rel_to_abs* (a ``keep`` or ``deleted`` row) belongs to no file of this run.
    """
    out = Applied()
    flagged_wanted: dict[str, list[Path]] = {}  # tumbler -> files of this run a glob matched
    for r in outcome.get("rows", []):
        rel = r.get("file_path", "")
        abs_path = rel_to_abs.get(rel)
        if abs_path is None:
            continue
        action = r.get("action")
        tumbler = r.get("tumbler")
        if action == "refused" or not tumbler:
            out.refused[abs_path] = str(r.get("reason") or action or "")
            continue
        tumbler = str(tumbler)
        row = staged[rel]
        out.file_to_doc_id[abs_path] = tumbler
        if row.get("sensitive"):
            flagged_wanted.setdefault(tumbler, []).append(abs_path)
        fence = (row.get("content_hash", ""), row.get("physical_collection", ""))
        if action == "created":
            out.new_tumblers.append(Tumbler.parse(tumbler))
            out.new_content_types.add(row.get("content_type", ""))
            out.needs_fence[tumbler] = fence
        elif action in ("updated", "unchanged"):
            if action == "updated":
                out.updated += 1
            if r.get("content_hash_changed"):
                out.needs_fence[tumbler] = fence
                if row.get("content_type") == "rdr":
                    out.relink_rdr_tumblers.append(Tumbler.parse(tumbler))
            state = r.get("index_state")
            if state in ("indexing", "failed"):
                out.stale_fence_doc_ids.add(tumbler)
            elif state == "complete" and r.get("index_content_hash"):
                out.complete_doc_hashes[tumbler] = str(r["index_content_hash"])
        elif action in ("same_owner_by_uri", "foreign_owner"):
            out.reconciled.append((abs_path, tumbler))
        if row.get("sensitive") and r.get("sensitive"):
            out.sensitive_confirmed.add(abs_path)
    for tumbler in outcome.get("sensitivity_pending", []):
        out.sensitivity_pending[str(tumbler)] = flagged_wanted.get(str(tumbler), [])
    return out


def raise_pending_sensitivity(writer: Any, applied: Applied, *, repo_name: str, page: int = STAGE_PAGE) -> None:
    """Raise the sensitive flag on the existing documents a ``--sensitive-glob`` matched that the commit did not flag.

    The commit never flags an existing document (``sensitive`` is not an updatable column); it names them in
    ``sensitivity_pending`` and this raises them through the audited ``set_sensitivity`` route, also when nothing else
    about the file changed. A page that fails leaves its files unconfirmed, and the caller then does not embed them.
    """
    pending = list(applied.sensitivity_pending.items())
    if not pending:
        return
    from nexus.sensitive_write import SENSITIVE_REASON  # noqa: PLC0415 - circular-dep avoidance (nexus.sensitive_write)

    set_sens = getattr(writer, "set_sensitivity", None)
    for start in range(0, len(pending), page):
        chunk = pending[start:start + page]
        tumblers = [t for t, _ in chunk]
        try:
            if not callable(set_sens):
                raise RuntimeError("the catalog writer has no set_sensitivity")
            set_sens(tumblers, True, reason=f"{SENSITIVE_REASON}: nx index repo --sensitive-glob")
        except Exception as exc:  # noqa: BLE001 - boundary: any failure of the audited route leaves these files unflagged, and the run does not embed them
            _log.warning(
                "sensitive_glob_flag_not_set", repo=repo_name, files=len(tumblers),
                error_type=type(exc).__name__, error=str(exc),
            )
            continue
        for _t, paths in chunk:
            applied.sensitive_confirmed.update(paths)
        # An existing document indexed before the glob matched may sit in topics labelled from its text
        # (RDR-243 Phase 3 fix r3, nexus-rfvyy.8, N1): relabel them, best effort and logged, as raise_flag does.
        from nexus import sensitive_topics  # noqa: PLC0415 - deferred: only a late flag needs it

        sensitive_topics.withhold_after_flag_best_effort(writer, tumblers, source="index repo --sensitive-glob")
