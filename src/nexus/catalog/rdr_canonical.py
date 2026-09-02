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
2. ADMISSION (unconditional, singleton included — the CRITICAL fix from
   the nexus-j9z30.20 fix round, code-review/critique 2026-09-01): a
   candidate survives only when it is verifiably THIS repo's own —
   EITHER its owner is the CURRENT repo owner (the owner id
   ``nx index rdr`` registers new content under today — see
   :func:`current_rdr_owner`) OR its ``source_uri`` sits under this
   repo's OWN ``docs/rdr/`` directory (see :func:`rdr_source_prefix`) —
   never owner id alone, and never granted to a candidate just because it
   is the only one found. A lone registration under another repo, or a
   same-basename document from an entirely different repo (measured
   live: ``ART`` is registered ``content_type="rdr"`` under a different
   owner in the SAME catalog), fails admission and is never a winner.
3. WINNER SELECTION among admitted candidates: the one under the current
   owner wins when unique; otherwise a single admitted candidate still
   resolves unambiguously (a legacy registration under a stale owner that
   ``source_uri`` nonetheless confirms is this repo's own file — the
   common case, ~150 RDRs in this repo alone have never been re-indexed
   under the current owner). Zero admitted candidates, or two or more
   with no unique current-owner match, is UNRESOLVABLE: no guess, no
   silent pick. :func:`resolve_canonical_tumbler` logs
   ``rdr_tumbler_unresolvable`` naming every surviving candidate and
   returns ``None`` — callers create no edge for that record.

Repo scoping (:func:`rdr_source_prefix`) is deliberately anchored on
``docs/rdr/`` — the exact directory ``nx index rdr`` walks (mirrors
``index.py``'s ``rdr_dir = repo_root / "docs" / "rdr"``) — not the bare repo
root. A bare-root prefix would still match a STALE registration from a
nested agent worktree (``<repo_root>/.claude/worktrees/<name>/docs/rdr/…``,
a real fragmentation source found live in this repo's own catalog): that
path starts with the repo root too, but it is not this repo's own
``docs/rdr/`` and must not be treated as in-repo.
"""
from __future__ import annotations

import os
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

#: matches "RDR-102" / "rdr-102" (case-insensitive) inside a title.
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
    It does NOT, by itself, distinguish two DIFFERENT repos' RDR files
    that happen to share a basename (e.g. this repo's ``rdr-005-….md`` and
    a same-numbered RDR in another repo) — that is
    :func:`resolve_canonical_tumbler`'s job via repo scoping, not this
    function's.

    Falls back to a looser ``RDR-NNN`` search on ``title`` only when
    ``file_path`` is empty (a legacy/degenerate registration with no path
    at all) — there is no basename to anchor on in that case. Kept
    deliberately narrow rather than dropped: a handful of legacy entries
    genuinely carry no ``file_path``, and refusing to key them at all
    would silently drop them from every RDR-201 report instead of
    surfacing them (as a same-basename group, or a harmless singleton) —
    see ``TestRdrKeyOf.test_extracts_from_title_when_no_file_path``.
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
    whose :func:`rdr_key_of` resolves are included. No repo scoping here —
    a caller may deliberately group across repos to inspect a cross-repo
    basename collision (see :func:`resolve_canonical_tumbler`'s docstring);
    scoping is applied at resolution time, not grouping time.
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


def _is_in_repo(
    entry: CatalogEntry, current_owner: Tumbler, repo_source_prefix: str,
) -> bool:
    """True when *entry* is trustworthy as THIS repo's own registration.

    Either signal suffices: the CURRENT owner (today's registrations, by
    definition trustworthy — see :func:`current_rdr_owner`) or a
    ``source_uri`` under this repo's own ``docs/rdr/`` (covers legacy
    registrations under a stale/legacy owner that are still genuinely
    this repo's files — the common case: ~150 RDRs in this repo's own
    catalog have never been re-indexed under the current owner and are
    still legitimately resolvable via path alone).
    """
    if entry.tumbler.owner_address() == current_owner:
        return True
    return bool(repo_source_prefix) and entry.source_uri.startswith(repo_source_prefix)


def resolve_canonical_tumbler(
    candidates: Sequence[CatalogEntry],
    current_owner: Tumbler,
    *,
    repo_source_prefix: str,
    rdr_key: str = "",
) -> Tumbler | None:
    """Resolve one RDR's canonical catalog tumbler from its candidates.

    See the module docstring for the rule. Returns ``None`` (after logging
    :data:`UNRESOLVABLE_EVENT`) when the candidates cannot be narrowed to
    exactly one in-repo tumbler (:func:`_is_in_repo`) — UNCONDITIONALLY,
    including the single-candidate case: a lone registration that is
    neither under *current_owner* nor under *repo_source_prefix* is
    unresolvable, not accepted by default. *repo_source_prefix* is
    required (no default) so this check can never be silently skipped by
    omission — pass :func:`rdr_source_prefix`'s output, or an explicit
    empty string if a caller genuinely has no repo root to scope against
    (that degrades to owner-only matching, never to accept-everything).
    *rdr_key* is optional context for the log event (falls back to
    :func:`rdr_key_of` on the first candidate).
    """
    if not candidates:
        return None
    rdr_only = [c for c in candidates if c.content_type == "rdr"]
    pool = rdr_only or list(candidates)

    # Stage 1 -- admission: drop anything that cannot be shown to belong to
    # THIS repo at all (unconditionally, singleton included -- the CRITICAL
    # fix). A candidate that fails both checks never becomes a winner no
    # matter how few other candidates exist.
    plausible = [c for c in pool if _is_in_repo(c, current_owner, repo_source_prefix)]

    # Stage 2 -- winner selection among the admitted candidates: the
    # CURRENT owner's registration wins whenever it is unique (rule step
    # 2's literal "the one under the current owner wins"); otherwise a
    # single surviving admitted candidate (a legacy registration under a
    # stale owner that source_uri nonetheless confirms is this repo's own
    # file -- the common case, ~150 RDRs in this repo alone) still
    # resolves unambiguously. Two or more admitted candidates with no
    # unique current-owner match is the genuine Finding-4 ambiguity.
    owner_matches = [c for c in plausible if c.tumbler.owner_address() == current_owner]
    if len(owner_matches) == 1:
        return owner_matches[0].tumbler
    if len(plausible) == 1:
        return plausible[0].tumbler
    _log.warning(
        UNRESOLVABLE_EVENT,
        rdr_key=rdr_key or (rdr_key_of(candidates[0]) or "?"),
        candidates=[str(c.tumbler) for c in pool],
        candidate_content_types=[c.content_type for c in pool],
        candidate_source_uris=[c.source_uri for c in pool],
        current_owner=str(current_owner),
        repo_source_prefix=repo_source_prefix,
        plausible_count=len(plausible),
        owner_match_count=len(owner_matches),
    )
    return None


def resolve_all(
    entries: Iterable[CatalogEntry],
    current_owner: Tumbler,
    *,
    repo_source_prefix: str,
) -> dict[str, Tumbler | None]:
    """Resolve every RDR key found in *entries* to its canonical tumbler.

    The single authority for this: :func:`group_rdr_candidates` then
    :func:`resolve_canonical_tumbler` per group. A ``None`` value means
    that RDR is unresolvable (already logged). Every other caller in this
    package that needs a full-catalog resolution (e.g.
    ``scripts/collapse_rdr_registrations.py``'s ``build_plan``) MUST route
    through this function rather than re-deriving the loop, so there is
    exactly one place the resolution rule is executed.
    """
    groups = group_rdr_candidates(entries)
    return {
        key: resolve_canonical_tumbler(
            candidates, current_owner, repo_source_prefix=repo_source_prefix, rdr_key=key,
        )
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


def rdr_source_prefix(repo_root: Path | str) -> str:
    """The ``file://`` prefix identifying *repo_root*'s OWN ``docs/rdr/`` tree.

    Anchored on ``docs/rdr`` (the exact directory ``nx index rdr`` walks —
    ``index.py``'s ``rdr_dir = repo_root / "docs" / "rdr"``), not the bare
    repo root: a nested agent worktree checkout
    (``<repo_root>/.claude/worktrees/<name>/docs/rdr/…``) sits INSIDE the
    repo root's own path but is a DIFFERENT ``docs/rdr/`` directory and
    must not be accepted as this repo's own registration — a bare-root
    prefix would wrongly match it; this anchor does not. Mirrors the
    ``file://`` + ``os.path.normpath`` shape
    :func:`nexus.catalog.types._normalize_source_uri` stamps onto
    ``source_uri`` at register time, plus a trailing separator so a
    differently-named sibling directory sharing this one as a string
    prefix (``…/nexus`` vs ``…/nexus2``) cannot false-match.
    """
    rdr_dir = os.path.normpath(str(Path(repo_root).resolve() / "docs" / "rdr"))
    return f"file://{rdr_dir}{os.sep}"
