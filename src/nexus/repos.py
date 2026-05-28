# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 2a: catalog-backed repo reader with DEBUG dual-read shim.

This module is the catalog-side replacement for the ``RepoRegistry.get``
read surface. Consumers swap from
``RepoRegistry(...).get(repo)`` to ``nexus.repos.read_dual(repo, cat=...)``
in Phase 3 (one consumer per bead: ``nexus-tts0d.6`` to ``.13``);
``read_dual`` returns the catalog answer when present, falls back to
``RepoRegistry`` otherwise, and emits a structured DEBUG log line on
fallback or disagreement so cutover-progress is observable in real
time.

The catalog-only path (``from_catalog``) is what Phase 4 will switch
all consumers to once the auto-migration (Phase 1.5a, nexus-tts0d.1)
has run on enough installs that the fallback rate is zero. At that
point the shim downgrades to silent and Phase 5 deletes the registry.

Per-decision references:

- **A5 verdict** (gate critique 2026-05-28): DEBUG-then-WARN log
  strategy modelled on ``catalog/catalog_docs.py:429-484
  resolve_path``. Phase 1.5 backfill incompleteness produces
  legitimate fallback fires during cutover; promotion to WARN
  happens in Phase 2b (``nexus-tts0d.5``) once the threshold is hit.
- **OQ-5 lock**: when the catalog has a ``knowledge__*`` collection
  registered to a repo's owner, that is the canonical
  ``docs_collection`` — the user's ``--corpus knowledge`` opt-in
  is the most recent intent and the reader preserves it without
  any schema change.

Return shape mirrors ``RepoRegistry.get`` so consumer code can shift
the import one PR at a time without changing field names.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover — type-hint-only import
    from nexus.catalog.catalog import Catalog

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RepoRecord:
    """The shape ``RepoRegistry.get`` returns, frozen and typed.

    All fields default to safe values so partial data (catalog-only,
    no docs collection registered yet) still constructs a valid record.

    ``collection`` is a back-compat alias for ``code_collection``;
    every legacy caller that grabs ``rec.collection`` continues to
    work.
    """

    name: str = ""
    collection: str = ""
    code_collection: str = ""
    docs_collection: str = ""
    rdr_collection: str = ""
    head_hash: str = ""
    status: str = "registered"


def from_catalog(repo: Path, *, cat: Catalog) -> RepoRecord | None:
    """Read a repo's collection set from the catalog.

    Returns ``None`` when the catalog has no owner registered for
    ``repo``. Returns a partial record (only fields the catalog can
    answer for) otherwise.

    OQ-5 lock: if multiple ``docs``-content-type collections are
    registered to the owner, prefer a ``knowledge__*`` name over a
    ``docs__*`` name — the user's ``--corpus knowledge`` opt-in is
    the canonical intent.
    """
    from nexus.registry import _repo_identity_with_main  # noqa: PLC0415

    name, repo_hash, _main_repo = _repo_identity_with_main(repo)
    owner = cat.owner_for_repo(repo_hash)
    if owner is None:
        return None

    owner_id = str(owner).replace(".", "-")
    rows = cat._db.execute(
        "SELECT name, content_type FROM collections WHERE owner_id = ?",
        (owner_id,),
    ).fetchall()

    code = ""
    docs = ""
    rdr = ""
    knowledge = ""
    for col_name, content_type in rows:
        if content_type == "code" and not code:
            code = col_name
        elif content_type == "rdr" and not rdr:
            rdr = col_name
        elif content_type == "docs" and not docs:
            docs = col_name
        elif content_type == "knowledge" and not knowledge:
            knowledge = col_name

    # OQ-5: knowledge wins over docs for the docs_collection slot.
    docs_canonical = knowledge or docs

    # Fetch head_hash from owners (RDR-137 Phase 1.5b column).
    head_row = cat._db.execute(
        "SELECT head_hash FROM owners WHERE tumbler_prefix = ?",
        (str(owner),),
    ).fetchone()
    head_hash = (head_row[0] or "") if head_row else ""

    return RepoRecord(
        name=name,
        collection=code,
        code_collection=code,
        docs_collection=docs_canonical,
        rdr_collection=rdr,
        head_hash=head_hash,
        status="registered",
    )


def from_registry(
    repo: Path, *, registry_path: Path,
) -> RepoRecord | None:
    """Read a repo's collection set from the legacy ``repos.json``.

    Returns ``None`` when the registry file does not exist or has no
    entry for ``repo``. The output shape is identical to
    :func:`from_catalog` so the dual-read shim can compare them
    field-for-field.
    """
    from nexus.registry import RepoRegistry  # noqa: PLC0415

    if not registry_path.exists():
        return None
    reg = RepoRegistry(registry_path)
    entry = reg.get(repo)
    if entry is None:
        return None
    return RepoRecord(
        name=entry.get("name", ""),
        collection=entry.get("collection", ""),
        code_collection=entry.get("code_collection", entry.get("collection", "")),
        docs_collection=entry.get("docs_collection", ""),
        rdr_collection=entry.get("rdr_collection", ""),
        head_hash=entry.get("head_hash", ""),
        status=entry.get("status", "registered"),
    )


def read_dual(
    repo: Path, *, cat: Catalog, registry_path: Path,
) -> RepoRecord | None:
    """Dual-read shim: prefer catalog, fall back to registry, log both.

    Resolution order:
      1. Try ``from_catalog``. On hit, also probe the registry; if it
         disagrees on any field, emit a DEBUG ``repos_read_dual_disagreement``
         event naming the divergent fields. Return the catalog answer
         regardless — catalog is authoritative.
      2. On miss (catalog returns ``None``), call ``from_registry``.
         If it returns something, emit a DEBUG ``repos_read_dual_fallback``
         event. Return the registry answer.
      3. On both-miss, return ``None``.

    DEBUG level (per A5): Phase 1.5 backfill incompleteness produces
    legitimate fallback fires during cutover; routine WARN would be
    noise. Phase 2b (``nexus-tts0d.5``) promotes to WARN once
    fallback rates settle.
    """
    cat_rec = from_catalog(repo, cat=cat)
    reg_rec = from_registry(repo, registry_path=registry_path)

    if cat_rec is not None:
        if reg_rec is not None:
            disagreements = _diff_fields(cat_rec, reg_rec)
            if disagreements:
                _log.debug(
                    "repos_read_dual_disagreement",
                    repo=str(repo),
                    disagreements=disagreements,
                )
        return cat_rec

    if reg_rec is not None:
        _log.debug(
            "repos_read_dual_fallback",
            repo=str(repo),
            fallback_branch="registry",
            reason="catalog_owner_not_registered",
        )
        return reg_rec

    return None


def _diff_fields(a: RepoRecord, b: RepoRecord) -> dict[str, dict[str, str]]:
    """Return field-by-field disagreements between two RepoRecords.

    Empty fields on either side are NOT a disagreement (catalog may
    have not registered the docs_collection yet for a code-only
    indexed repo; that's a partial-record case, not a divergence).
    """
    fields = (
        "name", "collection", "code_collection", "docs_collection",
        "rdr_collection", "head_hash",
    )
    diffs: dict[str, dict[str, str]] = {}
    for f in fields:
        av = getattr(a, f)
        bv = getattr(b, f)
        if av and bv and av != bv:
            diffs[f] = {"catalog": av, "registry": bv}
    return diffs


def list_repos_dual(
    *, cat: Catalog, registry_path: Path,
) -> list[str]:
    """Enumerate every known repo path (catalog ∪ registry), sorted.

    Catalog source: ``owners WHERE owner_type='repo'`` projected via
    ``repo_root``.  Registry source: ``RepoRegistry.all()``.  Union is
    a set merge (deterministic via ``sorted``); paths the catalog
    knows about that the registry does NOT (or vice versa) are
    surfaced via a single DEBUG event so cutover-progress is observable
    without one source overriding the other.

    Used by ``health.py``'s git-hook check and by any other consumer
    that needs to iterate every registered repo.  When ``registry_path``
    does not exist (post-Phase-5 install), returns the catalog set
    alone; the DEBUG event is suppressed so the steady state is silent.
    """
    cat_paths: set[str] = set()
    rows = cat._db.execute(
        "SELECT repo_root FROM owners "
        "WHERE owner_type = 'repo' AND repo_root != ''"
    ).fetchall()
    for (rr,) in rows:
        if rr:
            cat_paths.add(rr)

    reg_paths: set[str] = set()
    if registry_path.exists():
        from nexus.registry import RepoRegistry  # noqa: PLC0415
        reg = RepoRegistry(registry_path)
        reg_paths = set(reg.all())

    only_cat = cat_paths - reg_paths
    only_reg = reg_paths - cat_paths
    if registry_path.exists() and (only_cat or only_reg):
        _log.debug(
            "repos_list_dual_disagreement",
            only_catalog=sorted(only_cat),
            only_registry=sorted(only_reg),
            both_count=len(cat_paths & reg_paths),
        )

    return sorted(cat_paths | reg_paths)


__all__ = (
    "RepoRecord",
    "from_catalog",
    "from_registry",
    "list_repos_dual",
    "read_dual",
)
