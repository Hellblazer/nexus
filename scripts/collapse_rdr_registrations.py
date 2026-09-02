#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Collapse duplicate RDR catalog registrations (RDR-201 Phase 3.1, nexus-j9z30.20).

Finding 4: the ~206 on-disk RDR files are registered under roughly TEN
different catalog owner ids, plus a stale legacy ``content_type="prose"``
registration beside the current ``content_type="rdr"`` one for some files.
This script lists, per RDR, every registration found and which one
:mod:`nexus.catalog.rdr_canonical`'s canonical-tumbler rule keeps — and,
with ``--apply``, sets ``alias_of`` on the losing registrations so
``resolve(follow_alias=True)`` and every alias-aware reader collapse them
to the single canonical tumbler.

``nx``-free: this is a plain Python script (no shelling to the ``nx``
binary), invoked directly (``python scripts/collapse_rdr_registrations.py``
or, once installed, ``uv run python scripts/...``).

**Repo scoping is mandatory, not optional** (code-review/critique fix
round, 2026-09-01): every document fetched here is filtered to THIS repo's
own ``docs/rdr/`` tree (:func:`nexus.catalog.rdr_canonical.rdr_source_prefix`)
before ``build_plan`` groups or resolves anything — the SAME catalog holds
other repos' RDR registrations under other owners (measured live: ``ART``
is registered ``content_type="rdr"`` in this catalog too), and an
unscoped fetch can silently resolve THIS repo's RDR to a DIFFERENT repo's
tumbler with no warning if the basenames collide. This is enforced inside
``build_plan`` itself, not left to the caller to remember.

**--dry-run is the default and does not require --apply to be absent** —
there is no way to write without explicitly passing ``--apply``. Per bead
nexus-j9z30.20, ``--apply`` MUST NOT be run against the live catalog in
this bead — that bead ships the resolution rule and this dry-run/report
tool ONLY. Whether and when to run ``--apply`` against the live catalog is
a SEPARATE decision for a follow-up bead (nexus-j9z30.20's coordinator
tracks this — see the fix-round T2 critique/code-review records), not
something this bead schedules or implies. ``--apply`` writes go through
:meth:`CatalogWriter.update` (the whitelisted RPC, ``CATALOG_WRITE_OPS``)
with an ``alias_of`` field — the same wire shape
:meth:`HttpCatalogClient.set_alias` sends, but reached through the
sanctioned writer proxy rather than a raw client (``set_alias`` itself is
not in the write whitelist).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nexus.catalog.catalog_protocol import CatalogReader, CatalogWriter
from nexus.catalog.rdr_canonical import (
    current_rdr_owner,
    group_rdr_candidates,
    rdr_source_prefix,
    resolve_all,
)
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry


@dataclass
class RdrPlanRow:
    """One RDR's collapse plan: every registration found, and the verdict."""

    rdr_key: str
    candidates: list[CatalogEntry] = field(default_factory=list)
    canonical: Tumbler | None = None

    @property
    def losers(self) -> list[Tumbler]:
        """Candidate tumblers that are NOT the canonical one.

        Empty when the row is unresolvable (``canonical is None``) — there
        is nothing to collapse onto without a winner.
        """
        if self.canonical is None:
            return []
        return [c.tumbler for c in self.candidates if c.tumbler != self.canonical]


def build_plan(
    entries: list[CatalogEntry],
    current_owner: Tumbler,
    *,
    repo_source_prefix: str,
) -> list[RdrPlanRow]:
    """Scope, group, and resolve *entries* into a per-RDR collapse plan.

    *entries* is filtered to *repo_source_prefix* FIRST (an entry whose
    ``source_uri`` does not start with it is dropped before grouping —
    this repo's plan must never even consider another repo's registration,
    not merely refuse to pick it). Resolution itself routes through
    :func:`nexus.catalog.rdr_canonical.resolve_all` — the single authority
    for the canonical-tumbler rule; this function does not re-derive it.
    Rows are sorted by ``rdr_key`` for a stable, readable report.
    """
    scoped = [e for e in entries if e.source_uri.startswith(repo_source_prefix)] if repo_source_prefix else list(entries)
    groups = group_rdr_candidates(scoped)
    resolved = resolve_all(scoped, current_owner, repo_source_prefix=repo_source_prefix)
    rows = [
        RdrPlanRow(
            rdr_key=key,
            candidates=sorted(candidates, key=lambda c: str(c.tumbler)),
            canonical=resolved[key],
        )
        for key, candidates in groups.items()
    ]
    rows.sort(key=lambda r: r.rdr_key)
    return rows


def format_plan(rows: list[RdrPlanRow], *, current_owner: Tumbler) -> str:
    """Render *rows* as a human-readable report (one line per registration)."""
    lines = [f"RDR canonical-tumbler collapse plan — current owner {current_owner}"]
    resolved = sum(1 for r in rows if r.canonical is not None)
    unresolvable = len(rows) - resolved
    lines.append(f"{len(rows)} RDR(s) found: {resolved} resolved, {unresolvable} unresolvable")
    lines.append("")
    for row in rows:
        if row.canonical is None:
            lines.append(f"{row.rdr_key}: UNRESOLVABLE ({len(row.candidates)} candidates, no single in-repo match)")
        else:
            lines.append(f"{row.rdr_key}: canonical={row.canonical}")
        for c in row.candidates:
            marker = "KEEP" if c.tumbler == row.canonical else "collapse ->"
            target = f" {row.canonical}" if marker == "collapse ->" and row.canonical else ""
            lines.append(f"    {marker:<12}{c.tumbler}  content_type={c.content_type}{target}")
    return "\n".join(lines)


def apply_plan(writer: CatalogWriter, rows: list[RdrPlanRow]) -> int:
    """Set ``alias_of`` on every losing registration. Returns the write count.

    Only rows with a resolved canonical tumbler are touched — an
    unresolvable row has no winner to alias its candidates onto, so it is
    left completely untouched (no guess, per the canonical-tumbler rule).
    """
    n = 0
    for row in rows:
        if row.canonical is None:
            continue
        for loser in row.losers:
            writer.update(tumbler=str(loser), alias_of=str(row.canonical))
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--repo-root", type=Path, default=Path.cwd(),
        help="Repo root to derive the current owner AND the repo-scoping source_uri prefix from (default: cwd).",
    )
    parser.add_argument(
        "--owner", type=str, default="",
        help="Override the current-owner tumbler prefix (e.g. '1.1') instead of auto-resolving it from repo git identity.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually collapse duplicates by setting alias_of on losing registrations. "
             "DO NOT pass this against the live catalog for bead nexus-j9z30.20 -- whether "
             "and when to run --apply live is a separate follow-up decision, not part of "
             "this bead. Dry-run (the default, no flag needed) only prints the plan and "
             "writes nothing.",
    )
    args = parser.parse_args(argv)

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred: avoid import cost/side effects for --help and tests that inject a reader

    cat: CatalogReader = make_catalog_reader()

    if args.owner:
        current_owner = Tumbler.parse(args.owner)
    else:
        current_owner = current_rdr_owner(cat, args.repo_root)
        if current_owner is None:
            print(
                f"No catalog owner registered for repo {args.repo_root} yet; "
                "pass --owner explicitly.",
                file=sys.stderr,
            )
            return 1

    prefix = rdr_source_prefix(args.repo_root)
    entries = [
        *cat.all_documents(content_type="rdr"),
        *cat.all_documents(content_type="prose"),
    ]
    rows = build_plan(entries, current_owner, repo_source_prefix=prefix)
    print(format_plan(rows, current_owner=current_owner))

    if args.apply:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred: only constructed when --apply is actually passed

        writer = make_catalog_writer()
        n = apply_plan(writer, rows)
        print(f"\nApplied {n} alias_of update(s).")
    else:
        print("\nDry run only — no writes. Pass --apply to collapse the plan above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
