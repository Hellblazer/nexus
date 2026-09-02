# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Canonical-tumbler resolution for RDR documents (RDR-201 Phase 3.1, nexus-j9z30.20).

Finding 4 (T2 ``nexus_rdr/201-research-2``): the ~206 on-disk RDR files are
registered in the catalog under roughly TEN different owner ids (re-indexing
runs over time have each minted or reused a different owner), plus a stale
legacy ``content_type="prose"`` registration sitting beside the current
``content_type="rdr"`` one for some files. Nothing downstream (RDR-to-RDR
dependency edges, the RDR-201 Phase 3.2 edge seeder) can mean anything until
each on-disk RDR resolves to exactly ONE catalog tumbler.

This module owns that resolution rule and nothing else — no edge creation
(that is bead nexus-j9z30.21), no catalog writes. The rule, in order:

1. ``content_type == "rdr"`` beats the legacy ``"prose"`` registration —
   when at least one ``"rdr"`` candidate exists, only ``"rdr"`` candidates
   are considered further.
2. Among the surviving pool, the one registered under the CURRENT repo
   owner (the owner id ``nx index rdr`` registers new content under today —
   see :func:`current_rdr_owner`) wins.
3. Zero matches or more than one match at step 2 is UNRESOLVABLE: no
   guess, no silent pick. :func:`resolve_canonical_tumbler` logs
   ``rdr_tumbler_unresolvable`` naming every surviving candidate and
   returns ``None`` — callers create no edge for that record.

A group with exactly one candidate after step 1 resolves directly; there is
nothing left to disambiguate, so step 2's owner check never runs (and the
lone candidate's content_type may be "prose" — a record that has never been
re-indexed under the current content_type is still unambiguous, just old).
"""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

import structlog

from nexus.catalog.catalog_protocol import CatalogReader
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry

_log = structlog.get_logger()

#: content types this rule considers when grouping/resolving RDR registrations.
#: Anything else (code, paper, knowledge, ...) is outside this rule's domain.
RDR_CONTENT_TYPES: frozenset[str] = frozenset({"rdr", "prose"})

#: matches "RDR-102" / "rdr-102" (case-insensitive) inside a file_path or title.
_RDR_ID_RE = re.compile(r"rdr-(\d{3,})", re.IGNORECASE)

#: structlog event name for an unresolvable RDR record (RDR-201 § Failure Modes).
UNRESOLVABLE_EVENT = "rdr_tumbler_unresolvable"


def rdr_key_of(entry: CatalogEntry) -> str | None:
    """Extract the RDR identity key *entry* belongs to.

    ``file_path`` is checked first, and — when present — is the ONLY
    signal used: the key is the lower-cased basename (extension stripped)
    of a file directly inside a ``rdr/`` directory (``docs/rdr/rdr-201-….md``,
    absolute or relative). This is deliberately narrower than "contains an
    RDR-NNN pattern": ``docs/rdr/`` also holds files that legitimately share
    an RDR's numeric prefix but are NOT the RDR document itself —
    ``docs/rdr/post-mortem/rdr-191-….md`` (a different subdirectory) and
    sibling artifacts like ``rdr-200-phase1-prereg.md`` /
    ``rdr-200-phase1-gate-result.md`` (a different basename, same directory).
    A loose ``rdr-(\\d+)`` substring match collapses those into the SAME key
    as the real ``rdr-200-….md`` document, which is a false ambiguity, not
    the genuine owner/content-type fragmentation this rule exists to
    resolve (measured against the live catalog while building this module:
    6 of 15 apparent "unresolvable" RDRs before this fix were exactly this
    — a post-mortem companion or a phase-artifact sibling, not a duplicate
    registration of the same file).

    Two on-disk RDR files that are genuinely THE SAME on-disk file,
    registered multiple times (Finding 4's fragmentation), always share
    the identical basename — so basename identity still groups true
    duplicates correctly while leaving distinct sibling documents alone.

    Falls back to a looser ``RDR-NNN`` search on ``title`` only when
    ``file_path`` is empty (a legacy/degenerate registration with no path
    at all) — there is no basename to anchor on in that case.
    """
    fp = entry.file_path
    if fp:
        path = PurePosixPath(fp)
        name = path.name
        if (
            path.parent.name.lower() == "rdr"
            and name.lower().startswith("rdr-")
            and name.lower().endswith(".md")
        ):
            return name[: -len(".md")].lower()
        return None
    title = entry.title
    if title:
        m = _RDR_ID_RE.search(title)
        if m:
            return f"rdr-{m.group(1)}"
    return None


def group_rdr_candidates(
    entries: Iterable[CatalogEntry],
) -> dict[str, list[CatalogEntry]]:
    """Group catalog entries into per-RDR candidate-registration lists.

    Only entries whose ``content_type`` is in :data:`RDR_CONTENT_TYPES` and
    whose :func:`rdr_key_of` resolves are included.
    """
    groups: dict[str, list[CatalogEntry]] = defaultdict(list)
    for entry in entries:
        if entry.content_type not in RDR_CONTENT_TYPES:
            continue
        key = rdr_key_of(entry)
        if key is None:
            continue
        groups[key].append(entry)
    return dict(groups)


def resolve_canonical_tumbler(
    candidates: Sequence[CatalogEntry],
    current_owner: Tumbler,
    *,
    rdr_key: str = "",
) -> Tumbler | None:
    """Resolve one RDR's canonical catalog tumbler from its candidates.

    See the module docstring for the rule. Returns ``None`` (after logging
    :data:`UNRESOLVABLE_EVENT`) when the candidates cannot be narrowed to
    exactly one tumbler. *rdr_key* is optional context for the log event
    (falls back to :func:`rdr_key_of` on the first candidate).
    """
    if not candidates:
        return None
    rdr_only = [c for c in candidates if c.content_type == "rdr"]
    pool = rdr_only or list(candidates)
    if len(pool) == 1:
        return pool[0].tumbler
    owner_matches = [c for c in pool if c.tumbler.owner_address() == current_owner]
    if len(owner_matches) == 1:
        return owner_matches[0].tumbler
    _log.warning(
        UNRESOLVABLE_EVENT,
        rdr_key=rdr_key or (rdr_key_of(candidates[0]) or "?"),
        candidates=[str(c.tumbler) for c in pool],
        candidate_content_types=[c.content_type for c in pool],
        current_owner=str(current_owner),
        owner_match_count=len(owner_matches),
    )
    return None


def resolve_all(
    entries: Iterable[CatalogEntry],
    current_owner: Tumbler,
) -> dict[str, Tumbler | None]:
    """Resolve every RDR key found in *entries* to its canonical tumbler.

    Convenience wrapper: :func:`group_rdr_candidates` then
    :func:`resolve_canonical_tumbler` per group. A ``None`` value means that
    RDR is unresolvable (already logged).
    """
    groups = group_rdr_candidates(entries)
    return {
        key: resolve_canonical_tumbler(candidates, current_owner, rdr_key=key)
        for key, candidates in groups.items()
    }


def current_rdr_owner(cat: CatalogReader, repo_root: Path | str) -> Tumbler | None:
    """Resolve the catalog owner tumbler the RDR indexer registers under today.

    Mirrors the identity ``nx index rdr`` itself derives at registration
    time (``_repo_identity`` → ``repo_hash`` → ``owner_for_repo``; the same
    lookup :meth:`HttpCatalogClient.collection_for_repo` performs and the
    idempotent fast path :meth:`HttpCatalogClient.ensure_owner_for_repo`
    checks before minting). Returns ``None`` when no owner has been
    registered for this repo yet — a fresh catalog, or a repo that has
    never been indexed.
    """
    from nexus.repo_identity import _repo_identity  # noqa: PLC0415 — circular-dep avoidance, matches http_catalog_client.py's own pattern

    _, repo_hash = _repo_identity(Path(repo_root))
    if not repo_hash:
        return None
    return cat.owner_for_repo(repo_hash)
