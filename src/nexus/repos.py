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

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover — type-hint-only import
    from nexus.catalog.catalog import Catalog

_log = structlog.get_logger(__name__)


def _shim_log_level() -> str:
    """Return the log-level token for the dual-read shim events.

    RDR-137 Phase 2b (nexus-tts0d.5, OQ-10): DEBUG→WARN graduation
    mechanism. Defaults to ``"debug"`` (Phase 1.5 backfill
    incompleteness produces legitimate fallback fires during cutover).

    Operators flip to WARN by setting ``NEXUS_REPOS_SHIM_WARN=1``
    after observing 24 consecutive hours of zero fallback fires in
    ``~/.config/nexus/logs/mcp.log`` (the documented threshold).
    Promotion is a config flip, not a code edit — once the env var
    is set, the next process restart picks it up.
    """
    # RDR-137 followup SIG-11 (nexus-43qgm.11): case-insensitive so
    # `True` / `YES` / `On` (common operator habits, shell-script
    # idioms, k8s ConfigMap booleans) all flip the gate.
    if os.environ.get("NEXUS_REPOS_SHIM_WARN", "").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        return "warning"
    return "debug"


def _emit_shim_event(event: str, **fields: object) -> None:
    """Emit a shim observability event at the currently configured level."""
    level = _shim_log_level()
    getattr(_log, level)(event, **fields)


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
    from nexus.repo_identity import _repo_identity_with_main  # noqa: PLC0415

    name, repo_hash, _main_repo = _repo_identity_with_main(repo)
    owner = cat.owner_for_repo(repo_hash)
    if owner is None:
        return None

    owner_id = str(owner).replace(".", "-")
    # RDR-137 followup SIG-6 (nexus-43qgm.6): ORDER BY name DESC so
    # the OQ-5 first-wins selection is deterministic. For conformant
    # names the trailing ``__v<n>`` segment makes lex-latest the
    # highest model version (e.g. ``knowledge__owner__voyage-context-3__v2``
    # wins over ``__v1``). Without the ORDER BY, SQLite's
    # implementation-defined row order made the docs_collection slot
    # non-deterministic for owners with multiple collections of the
    # same content_type (post-model-upgrade state).
    rows = cat._db.execute(
        "SELECT name, content_type FROM collections WHERE owner_id = ? "
        "ORDER BY name DESC",
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


def _read_repos_json(registry_path: Path) -> dict[str, dict]:
    """Parse the legacy ``repos.json`` file shape with stdlib json.

    RDR-137 Phase 5.3 (nexus-tts0d.20): replaces the old
    ``RepoRegistry(path).all_info()`` round-trip. Returns the inner
    ``{"<path>": {...entry}}`` mapping; empty dict on missing file.

    **RDR-137 followup CRITICAL-4 (nexus-43qgm.4):** malformed
    ``repos.json`` is NOT silently treated as empty — the function
    emits ``repos_json_malformed`` at WARNING (so the cause is
    observable) AND still returns ``{}`` to keep the read-only
    callers crash-free. Callers that drive destructive operations
    (the migration verb) MUST pre-validate the file separately, or
    the malformed-but-recoverable file gets deleted on the false
    parity. See ``commands/upgrade._migrate_repos_json_to_catalog``
    for the pre-validation pattern.

    The migration verb + dual-read shim both need to read the legacy
    file shape during the deprecation window; everything else has
    already cut over to the catalog. Keeping this as a stdlib helper
    means deleting :class:`RepoRegistry` does not break the migration.
    """
    import json

    if not registry_path.exists():
        return {}
    try:
        data = json.loads(registry_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning(
            "repos_json_malformed",
            path=str(registry_path),
            error=str(exc),
            hint="file present but unparseable; callers that delete on parity "
                 "must pre-validate before consuming the empty-dict return",
        )
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    return repos if isinstance(repos, dict) else {}


def _repos_json_is_parseable(registry_path: Path) -> bool:
    """Return True iff *registry_path* is absent OR parseable as the
    legacy ``repos.json`` shape. Used by the migration verb to refuse
    deletion on parse failure.

    Absent file → True (idempotent migration no-op).
    Parseable file → True (proceed to the parity check).
    Malformed file → False (refuse to delete; surface to operator).
    """
    import json
    if not registry_path.exists():
        return True
    try:
        json.loads(registry_path.read_text())
        return True
    except (OSError, json.JSONDecodeError):
        return False


def from_registry(
    repo: Path, *, registry_path: Path,
) -> RepoRecord | None:
    """Read a repo's collection set from the legacy ``repos.json``.

    Returns ``None`` when the registry file does not exist or has no
    entry for ``repo``. The output shape is identical to
    :func:`from_catalog` so the dual-read shim can compare them
    field-for-field.
    """
    entry = _read_repos_json(registry_path).get(str(repo))
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
                _emit_shim_event(
                    "repos_read_dual_disagreement",
                    repo=str(repo),
                    disagreements=disagreements,
                )
            # RDR-137 followup SIG-8 (nexus-43qgm.8): also surface the
            # asymmetric case where the catalog has the owner but a
            # field is empty AND the registry has a value. _diff_fields
            # intentionally suppresses empty-vs-non-empty as a partial-
            # record case; we still want operators to see when catalog
            # is silently stale relative to registry during cutover.
            catalog_missing = _catalog_missing_fields(cat_rec, reg_rec)
            if catalog_missing:
                _emit_shim_event(
                    "repos_read_dual_catalog_missing",
                    repo=str(repo),
                    catalog_missing=catalog_missing,
                )
        return cat_rec

    if reg_rec is not None:
        _emit_shim_event(
            "repos_read_dual_fallback",
            repo=str(repo),
            fallback_branch="registry",
            reason="catalog_owner_not_registered",
        )
        return reg_rec

    return None


def _catalog_missing_fields(
    cat_rec: RepoRecord, reg_rec: RepoRecord,
) -> dict[str, str]:
    """RDR-137 followup SIG-8: surface fields the catalog is silently
    missing but the registry has populated.

    Distinct from :func:`_diff_fields` (mutual disagreement). This
    captures the asymmetric case that arises during Phase 3 cutover
    when the catalog has the owner but not all per-content-type
    collections registered yet, while the legacy registry still has
    them.
    """
    fields = (
        "name", "collection", "code_collection", "docs_collection",
        "rdr_collection", "head_hash",
    )
    missing: dict[str, str] = {}
    for f in fields:
        if not getattr(cat_rec, f) and getattr(reg_rec, f):
            missing[f] = getattr(reg_rec, f)
    return missing


def _diff_fields(a: RepoRecord, b: RepoRecord) -> dict[str, dict[str, str]]:
    """Return field-by-field disagreements between two RepoRecords.

    Empty fields on either side are NOT a disagreement (catalog may
    have not registered the docs_collection yet for a code-only
    indexed repo; that's a partial-record case, not a divergence).
    The asymmetric catalog-empty/registry-has-value case is surfaced
    separately via :func:`_catalog_missing_fields` so cutover
    observability is preserved (RDR-137 followup SIG-8).
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
    # RDR-146 P1.2: ``cat`` may be None when the catalog is uninitialised
    # (the reader factory returns None). Fall back to the registry-only set
    # rather than dereferencing a missing handle.
    cat_paths: set[str] = set()
    if cat is not None:
        rows = cat._db.execute(
            "SELECT repo_root FROM owners "
            "WHERE owner_type = 'repo' AND repo_root != ''"
        ).fetchall()
        for (rr,) in rows:
            if rr:
                cat_paths.add(rr)

    reg_paths: set[str] = set(_read_repos_json(registry_path).keys())

    only_cat = cat_paths - reg_paths
    only_reg = reg_paths - cat_paths
    if registry_path.exists() and (only_cat or only_reg):
        _emit_shim_event(
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
