# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill dedupe for orphan owners (nexus-tmbh, part of nexus-b34f).

Three passes, in order:

1. **Test orphans** — names matching ``int-cce-*``, ``int-prov-*``,
   ``pdf-e2e-*`` are test fixture leakage that pre-dates the autouse
   ``_isolate_catalog`` fixture (RDR-060, 2026-04-08). No external
   references exist, so they are removed outright (documents + owner).
2. **Synthetic ``<repo>-<hashprefix>`` orphans** — curator owners whose
   name mirrors a canonical repo owner and whose 8-hex suffix matches
   the canonical's ``repo_hash`` prefix. Each orphan document is
   consolidated into its canonical equivalent (matched by ``file_path``)
   via ``Catalog.set_alias()``. Rows stay so external references
   continue to resolve through the alias chain.
3. **Uncategorised orphans** — reported but not touched. Examples:
   legitimate curator owners like ``papers``, ``knowledge``, ``test``,
   stand-alone content with no parent repo. Manual review.

The planner is dry-run first: call ``plan_dedupe(cat)`` to inspect the
classification, then ``apply_plan(cat, plan)`` to commit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from nexus.catalog.tumbler import OwnerRecord, Tumbler

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog

_log = structlog.get_logger(__name__)

# Test-orphan name patterns. Owners with these names are pure test
# fixture artifacts; no external references link to them. Unconditional
# removal is safe.
_TEST_ORPHAN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^int-cce-[0-9a-f]+$"),
    re.compile(r"^int-prov-[0-9a-f]+$"),
    re.compile(r"^int-[a-z]+-[0-9a-f]+$"),
    re.compile(r"^pdf-e2e-[A-Za-z0-9_-]+$"),
)

# Synthetic-name pattern: ``<repo_name>-<8-hex>``. Match permissively
# on the hash suffix (allow any hex length ≥4 ≤16) — many live orphans
# were created with short prefixes.
_SYNTHETIC_NAME = re.compile(r"^(?P<repo>.+)-(?P<hash>[0-9a-f]{4,16})$")


@dataclass
class OrphanPlan:
    """One orphan's disposition."""
    orphan_prefix: str
    orphan_name: str
    action: str  # "alias", "remove", "skip"
    doc_count: int
    canonical_prefix: str = ""
    canonical_name: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "orphan_prefix": self.orphan_prefix,
            "orphan_name": self.orphan_name,
            "action": self.action,
            "doc_count": self.doc_count,
            "canonical_prefix": self.canonical_prefix,
            "canonical_name": self.canonical_name,
            "reason": self.reason,
        }


@dataclass
class DedupePlan:
    """Full classification across all orphans."""
    alias: list[OrphanPlan] = field(default_factory=list)
    remove: list[OrphanPlan] = field(default_factory=list)
    skip: list[OrphanPlan] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "alias": len(self.alias),
            "remove": len(self.remove),
            "skip": len(self.skip),
            "alias_docs": sum(p.doc_count for p in self.alias),
            "remove_docs": sum(p.doc_count for p in self.remove),
            "skip_docs": sum(p.doc_count for p in self.skip),
        }


def _is_test_orphan(name: str) -> bool:
    return any(p.match(name) for p in _TEST_ORPHAN_PATTERNS)


def _doc_count(cat: "Catalog", owner_prefix: str) -> int:
    clause, params = cat._prefix_sql(owner_prefix)
    row = cat._db.execute(
        f"SELECT COUNT(*) FROM documents WHERE {clause}", params
    ).fetchone()
    return row[0] if row else 0


def _find_synthetic_match(
    name: str, repo_index: dict[tuple[str, str], tuple[str, str]]
) -> tuple[str, str] | None:
    """Return (canonical_prefix, canonical_name) if ``name`` matches a
    synthetic ``<repo>-<hash>`` pattern keyed into an existing repo owner.

    ``repo_index`` maps (repo_name.lower(), hash_prefix) → (prefix, repo_name).
    Matching is case-insensitive on the repo name and uses the orphan's
    hex suffix as a prefix probe against canonical ``repo_hash``.
    """
    m = _SYNTHETIC_NAME.match(name)
    if not m:
        return None
    repo, hash_prefix = m.group("repo"), m.group("hash")
    # Canonical hashes stored at full length; try increasing prefix widths
    # so e.g. orphan name "nexus-571b8edd" finds canonical repo_hash
    # "571b8edd..." regardless of stored hash length.
    for width in range(len(hash_prefix), 3, -1):
        hit = repo_index.get((repo.lower(), hash_prefix[:width].lower()))
        if hit:
            return hit
    return None


def plan_dedupe(cat: "Catalog") -> DedupePlan:
    """Classify every curator owner in the catalog.

    Repo-type owners are ignored — nexus-zbne's enforcement guarantees
    they are well-formed by construction.
    """
    # Build canonical repo index: (name.lower(), first-N-hex-of-hash) → (prefix, name)
    repo_index: dict[tuple[str, str], tuple[str, str]] = {}
    for row in cat._db.execute(
        "SELECT tumbler_prefix, name, repo_hash FROM owners "
        "WHERE owner_type = 'repo' AND repo_hash != ''"
    ).fetchall():
        prefix, rname, rhash = row
        # Index every hex prefix from length 4 to full so a 6- or 10-char
        # orphan suffix still hits.
        for width in range(4, len(rhash) + 1):
            repo_index.setdefault((rname.lower(), rhash[:width].lower()), (prefix, rname))

    plan = DedupePlan()
    for row in cat._db.execute(
        "SELECT tumbler_prefix, name, owner_type FROM owners "
        "WHERE owner_type = 'curator'"
    ).fetchall():
        prefix, name, _owner_type = row
        doc_count = _doc_count(cat, prefix)

        if _is_test_orphan(name):
            plan.remove.append(OrphanPlan(
                orphan_prefix=prefix, orphan_name=name,
                action="remove", doc_count=doc_count,
                reason="matches test-orphan name pattern",
            ))
            continue

        match = _find_synthetic_match(name, repo_index)
        if match is not None:
            canonical_prefix, canonical_name = match
            plan.alias.append(OrphanPlan(
                orphan_prefix=prefix, orphan_name=name,
                action="alias", doc_count=doc_count,
                canonical_prefix=canonical_prefix,
                canonical_name=canonical_name,
                reason=f"synthetic name mirrors canonical repo '{canonical_name}'",
            ))
            continue

        # Everything else: leave alone. Reported to the user so they can
        # decide whether to clean up manually.
        plan.skip.append(OrphanPlan(
            orphan_prefix=prefix, orphan_name=name,
            action="skip", doc_count=doc_count,
            reason="no canonical match — manual review",
        ))

    return plan


def apply_alias_plan(cat: "Catalog", op: OrphanPlan) -> tuple[int, int]:
    """Alias every document under ``op.orphan_prefix`` to its canonical
    equivalent in ``op.canonical_prefix`` (matched by ``file_path``).

    Returns ``(aliased, unmatched)`` — aliased docs got a pointer;
    unmatched docs had no canonical equivalent and were left untouched.
    """
    orphan = Tumbler.parse(op.orphan_prefix)
    canonical = Tumbler.parse(op.canonical_prefix)
    orphan_docs = cat.by_owner(orphan)
    aliased = 0
    unmatched = 0
    for doc in orphan_docs:
        if not doc.file_path:
            unmatched += 1
            continue
        canonical_doc = cat.by_file_path(canonical, doc.file_path)
        if canonical_doc is None:
            unmatched += 1
            continue
        if str(doc.tumbler) == str(canonical_doc.tumbler):
            continue  # already canonical
        try:
            cat.set_alias(doc.tumbler, canonical_doc.tumbler)
            aliased += 1
        except ValueError:
            # set_alias rejects self-aliases; treat as no-op.
            continue
    _log.info(
        "catalog.dedupe.alias_plan_applied",
        orphan=op.orphan_name, canonical=op.canonical_name,
        aliased=aliased, unmatched=unmatched,
    )
    return aliased, unmatched


def apply_remove_plan(cat: "Catalog", op: OrphanPlan) -> tuple[int, int]:
    """Hard-delete every document under ``op.orphan_prefix`` plus the
    orphan owner itself. Links that reference any deleted tumbler are
    also dropped. Writes JSONL tombstones so the deletion survives a
    future rebuild.

    Returns ``(deleted_docs, deleted_links)``.
    """
    orphan_prefix = op.orphan_prefix
    clause, params = cat._prefix_sql(orphan_prefix)

    # 1. Collect docs to delete (need the tombstone payload before SQL DELETE).
    rows = cat._db.execute(
        f"SELECT tumbler, title, author, year, content_type, file_path, "
        f"corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        f"metadata, source_mtime, alias_of FROM documents WHERE {clause}",
        params,
    ).fetchall()
    doc_tumblers = [r[0] for r in rows]

    # 2. Collect links to delete.
    placeholders = ",".join("?" * len(doc_tumblers)) if doc_tumblers else "''"
    link_rows = cat._db.execute(
        f"SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
        f"created_by, created_at, metadata FROM links "
        f"WHERE from_tumbler IN ({placeholders}) OR to_tumbler IN ({placeholders})",
        (*doc_tumblers, *doc_tumblers) if doc_tumblers else (),
    ).fetchall() if doc_tumblers else []

    # 3. JSONL tombstones — documents, links, owner.
    import json as _json
    for r in rows:
        cat._append_jsonl(cat._documents_path, {
            "tumbler": r[0], "title": r[1], "author": r[2], "year": r[3],
            "content_type": r[4], "file_path": r[5], "corpus": r[6],
            "physical_collection": r[7], "chunk_count": r[8],
            "head_hash": r[9], "indexed_at": r[10],
            "meta": _json.loads(r[11]) if r[11] else {},
            "source_mtime": r[12] or 0.0,
            "alias_of": r[13] or "",
            "_deleted": True,
        })
    for r in link_rows:
        cat._append_jsonl(cat._links_path, {
            "from_t": r[0], "to_t": r[1], "link_type": r[2],
            "from_span": r[3] or "", "to_span": r[4] or "",
            "created_by": r[5], "created_at": r[6] or "",
            "meta": _json.loads(r[7]) if r[7] else {},
            "_deleted": True,
        })
    cat._append_jsonl(cat._owners_path, {"owner": orphan_prefix, "_deleted": True})

    # 4. SQL deletion.
    if doc_tumblers:
        cat._db.execute(
            f"DELETE FROM links WHERE from_tumbler IN ({placeholders}) "
            f"OR to_tumbler IN ({placeholders})",
            (*doc_tumblers, *doc_tumblers),
        )
    cat._db.execute(f"DELETE FROM documents WHERE {clause}", params)
    cat._db.execute(
        "DELETE FROM owners WHERE tumbler_prefix = ?", (orphan_prefix,),
    )
    cat._db.commit()

    _log.info(
        "catalog.dedupe.remove_plan_applied",
        orphan=op.orphan_name, docs=len(rows), links=len(link_rows),
    )
    return len(rows), len(link_rows)


def apply_plan(
    cat: "Catalog", plan: DedupePlan,
) -> dict[str, int]:
    """Apply all alias + remove plans. Skipped orphans are untouched.

    Returns aggregate counts.
    """
    totals = {"aliased_docs": 0, "unmatched_docs": 0,
              "removed_docs": 0, "removed_links": 0,
              "orphans_aliased": 0, "orphans_removed": 0}
    for op in plan.alias:
        aliased, unmatched = apply_alias_plan(cat, op)
        totals["aliased_docs"] += aliased
        totals["unmatched_docs"] += unmatched
        totals["orphans_aliased"] += 1
    for op in plan.remove:
        docs, links = apply_remove_plan(cat, op)
        totals["removed_docs"] += docs
        totals["removed_links"] += links
        totals["orphans_removed"] += 1
    return totals
