# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only shape audit of the collection SET (nexus-ger23): ``nx collection shape``.

Distinct from :mod:`nexus.collection_audit`, which is the RDR-087 deep-dive
of ONE collection (``nx collection audit NAME``: distance histogram,
projections, orphans, hubs). This module asks a different question of the
whole tenant: does the set of collections obey docs/collections.md?

The rules live in ``docs/collections.md``; this module turns them into
checks. Every check is a pure function over gathered facts, so the whole
audit is testable without an engine, and the single I/O entry point
(:func:`audit`) does three reads and nothing else. It never writes and
never calls a model.

Two seams are deliberate:

* :func:`collection_attributes` is the ONLY place that derives a
  collection's content type, owner, model and version. Today it prefers the
  catalog row's columns and falls back to parsing the name (most rows on a
  live tenant are blank stubs, T2 ``nexus_rdr/204-research-1``). When
  RDR-204 lands, the fallback goes and this function is the one-line
  repoint.
* :data:`CHECKS_BY_RULE` maps each ``## Rule N`` heading in the doc to the
  checks that enforce it; ``tests/test_collection_shape.py`` pins the two
  to each other so a rule without a check, or a check without a rule,
  fails.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "CHECKS_BY_RULE",
    "FANOUT_FLOOR",
    "PLACEHOLDER_SUBJECTS",
    "TEST_RESIDUE_TOKENS",
    "THIN_CHUNK_FLOOR",
    "AuditReport",
    "CollectionAttributes",
    "CollectionFacts",
    "Finding",
    "audit",
    "collection_attributes",
    "gather_facts",
    "run_checks",
]

# ── pinned constants (the audit's opinions, each one line to change) ───────

#: Rule 1. Subjects that are containers, not topics. Closed list on purpose:
#: a false positive here is a one-line fix and a reader can see exactly
#: what the audit believes. ``knowledge`` and ``default`` are the two that
#: exist on a production tenant today.
PLACEHOLDER_SUBJECTS: frozenset[str] = frozenset({
    "default", "knowledge", "notes", "note", "tmp", "temp", "test", "tests",
    "scratch", "misc", "stuff", "data", "docs", "code", "rdr", "general",
    "other", "new", "my", "personal",
})

#: Rule 5. A subject containing one of these tokens is test or rehearsal
#: residue unless someone says otherwise.
TEST_RESIDUE_TOKENS: frozenset[str] = frozenset({
    "shakedown", "smoke", "test", "fixture", "probe", "rehearsal", "tmp",
    "scratch", "dummy", "sample",
})

#: Rule 4. Below this many chunks a collection is not yet earning its
#: partition (a shard per query, its own taxonomy, no cross-document
#: ranking). Advisory, not a floor the system enforces.
THIN_CHUNK_FLOOR: int = 20

#: Rule 4. The bare-corpus fan-out drops a collection below this many chunks
#: when a sibling under the same prefix is not thin (nexus-rbhci). Must
#: equal ``nexus.mcp.core._FANOUT_MIN_COLLECTION_CHUNK_COUNT``; the test
#: suite pins the two. Duplicated rather than imported because
#: ``nexus.mcp.core`` is the whole MCP server and this module must stay
#: cheap to import from the CLI.
FANOUT_FLOOR: int = 3

#: Rule 2. difflib ratio at or above which two knowledge subjects are
#: reported as likely duplicates. Name-only evidence; the finding says so.
DUP_NAME_SIMILARITY: float = 0.80

_DATE_LIKE = re.compile(r"(?:^|-)(?:20\d{2}(?:-?\d{2}){0,2}|\d{8})(?:-|$)")

#: Rule number -> check ids. The doc headings are the source of truth for
#: the numbers; the test pins this map to them.
CHECKS_BY_RULE: dict[int, tuple[str, ...]] = {
    1: ("placeholder-subject", "default-corpus"),
    2: ("duplicate-subject",),
    3: ("model-differs-from-install", "write-model-unresolvable"),
    4: ("thin-collection", "one-document", "below-fanout-floor"),
    5: ("test-residue",),
    6: ("ghost-row", "grandfathered-relic", "blank-attributes",
        "superseded-live", "unregistered-collection"),
}
_RULE_OF_CHECK: dict[str, int] = {c: r for r, cs in CHECKS_BY_RULE.items() for c in cs}


# ── facts ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CollectionAttributes:
    """What the name or the row says a collection is. See module docstring
    for why this is the RDR-204 seam."""

    content_type: str
    owner_id: str
    embedding_model: str
    model_version: str
    quarantine: bool
    #: ``"row"`` (catalog columns), ``"name"`` (parsed), ``"unknown"``.
    source: str


def collection_attributes(row: dict[str, Any]) -> CollectionAttributes:
    """Derive a collection's attributes, preferring the catalog row's columns.

    The name is parsed only when the row is blank (the stub-insert class) or
    absent. A ``quarantine-<type>`` prefix is reported as the base content
    type with ``quarantine=True``: it is a lifecycle state the orphan GC
    owns (nexus-xukbj), not a content type.
    """
    name = str(row.get("name") or "")
    ct = str(row.get("content_type") or "")
    owner = str(row.get("owner_id") or "")
    model = str(row.get("embedding_model") or "")
    ver = str(row.get("model_version") or "")
    quarantine = False
    if ct and owner:
        if ct.startswith("quarantine-"):
            ct, quarantine = ct[len("quarantine-"):], True
        return CollectionAttributes(ct, owner, model, ver, quarantine, "row")
    parts = name.split("__")
    if len(parts) < 2 or not parts[0]:
        return CollectionAttributes("", "", "", "", False, "unknown")
    p_ct = parts[0]
    if p_ct.startswith("quarantine-"):
        p_ct, quarantine = p_ct[len("quarantine-"):], True
    p_owner = parts[1]
    p_model = parts[2] if len(parts) >= 3 else ""
    p_ver = parts[3] if len(parts) >= 4 else ""
    return CollectionAttributes(p_ct, p_owner, p_model, p_ver, quarantine, "name")


@dataclass(slots=True)
class CollectionFacts:
    """Everything the checks read, joined from the catalog row, the vector
    stats row, and the catalog document count."""

    name: str
    attrs: CollectionAttributes
    catalog_row: bool
    legacy_grandfathered: bool
    superseded_by: str
    has_stats: bool
    chunk_count: int
    dim: int | None
    doc_count: int

    @property
    def live(self) -> bool:
        return self.has_stats and self.chunk_count > 0


def gather_facts(
    *,
    catalog_rows: Iterable[dict[str, Any]],
    stats_rows: Iterable[dict[str, Any]],
    doc_counts: dict[str, int],
) -> list[CollectionFacts]:
    """Join the three read surfaces by collection name. Pure.

    A collection present in the vector stats but absent from the catalog is
    kept (with ``catalog_row=False``) so it can be reported; dropping it
    would hide exactly the registration gap Rule 6 is about.
    """
    rows = {str(r.get("name") or ""): r for r in catalog_rows if r.get("name")}
    stats = {str(s.get("name") or ""): s for s in stats_rows if s.get("name")}
    facts: list[CollectionFacts] = []
    for name in sorted(set(rows) | set(stats)):
        row = rows.get(name, {"name": name})
        st = stats.get(name)
        facts.append(CollectionFacts(
            name=name,
            attrs=collection_attributes(row),
            catalog_row=name in rows,
            legacy_grandfathered=bool(row.get("legacy_grandfathered")),
            superseded_by=str(row.get("superseded_by") or ""),
            has_stats=st is not None,
            chunk_count=int(st.get("count") or 0) if st else 0,
            dim=int(st["dim"]) if st and st.get("dim") is not None else None,
            doc_count=int(doc_counts.get(name, 0) or 0),
        ))
    return facts


# ── findings ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Finding:
    collection: str
    check: str
    severity: str  # "warn" | "info"
    message: str
    action: str
    related: str = ""

    @property
    def rule(self) -> int:
        return _RULE_OF_CHECK[self.check]

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection": self.collection, "check": self.check, "rule": self.rule,
            "severity": self.severity, "message": self.message, "action": self.action,
            "related": self.related,
        }


def _subject_tokens(subject: str) -> list[str]:
    return [t for t in re.split(r"[-_]+", subject.lower()) if t]


def _is_placeholder_subject(subject: str) -> bool:
    s = subject.lower()
    if s in PLACEHOLDER_SUBJECTS:
        return True
    if _DATE_LIKE.search(s):
        return True
    return False


def _has_residue_token(subject: str) -> bool:
    return any(t in TEST_RESIDUE_TOKENS for t in _subject_tokens(subject))


def run_checks(
    facts: Iterable[CollectionFacts],
    *,
    write_model_for: Callable[[str], str | None],
) -> list[Finding]:
    """Evaluate every rule over *facts*. Pure.

    *write_model_for(content_type)* answers "which model does this install
    write new collections of this type with"; a raise or ``None`` is
    reported as ``write-model-unresolvable`` once per content type rather
    than propagated, because an audit that crashes on one install's
    credential state reports nothing about the other rules.
    """
    facts = [f for f in facts if not f.attrs.quarantine]  # GC machinery, never a target
    out: list[Finding] = []
    unresolvable: set[str] = set()

    def write_model(ct: str) -> str | None:
        if ct in unresolvable or not ct:
            return None
        try:
            return write_model_for(ct)
        except Exception as exc:  # noqa: BLE001 — reported as a finding, never propagated
            unresolvable.add(ct)
            out.append(Finding(
                collection="", check="write-model-unresolvable", severity="warn",
                message=f"could not resolve the install's write model for content type "
                        f"{ct!r}: {exc}",
                action="fix the embedding configuration (local.embed_model / voyage_api_key) "
                       "and re-run; Rule 3 was not evaluated for this content type",
            ))
            return None

    knowledge_live: list[CollectionFacts] = []

    for f in facts:
        a = f.attrs
        # Rule 6: registration and lifecycle state.
        if not f.catalog_row:
            out.append(Finding(f.name, "unregistered-collection", "warn",
                               "chunks exist in T3 but the catalog has no row for this collection",
                               "register through the catalog (nx index repo for repo collections; "
                               "re-put for knowledge) or delete the orphan chunks"))
        if f.catalog_row and not f.has_stats:
            out.append(Finding(f.name, "ghost-row", "warn",
                               "catalog row with no chunks",
                               "delete with nx collection delete (cascades), or leave for the "
                               "RDR-204 ghost sweep"))
        if f.legacy_grandfathered:
            out.append(Finding(f.name, "grandfathered-relic", "info",
                               "legacy_grandfathered row from the pre-RDR-103 naming era",
                               "delete if it has no chunks; re-index the repo if it does"))
        if f.catalog_row and f.live and a.source == "name":
            out.append(Finding(f.name, "blank-attributes", "info",
                               "catalog row has blank content_type/owner_id/embedding_model "
                               "(a stub insert); attributes shown here were parsed from the name",
                               "no action; the RDR-204 backfill fills these"))
        if f.superseded_by and f.live:
            out.append(Finding(f.name, "superseded-live", "info",
                               f"superseded by {f.superseded_by} but still holds {f.chunk_count} chunks",
                               "confirm the successor is complete, then delete this one",
                               related=f.superseded_by))
        if not f.live:
            continue  # every remaining rule is about collections that carry vectors

        # Rule 1: subjects, for knowledge; default corpus for repo types.
        if a.content_type == "knowledge" and _is_placeholder_subject(a.owner_id):
            out.append(Finding(f.name, "placeholder-subject", "warn",
                               f"subject {a.owner_id!r} is a container word or a date, not a topic",
                               "rename to a durable subject (nx collection rename) or merge "
                               "its documents into the subject collections they belong to"))
        if a.content_type in ("docs", "code", "rdr") and a.owner_id == "default":
            out.append(Finding(f.name, "default-corpus", "warn",
                               "the 'default' corpus is a placeholder owner",
                               "route these documents to a knowledge subject "
                               "(nx index md --collection <subject>) and delete this collection"))

        # Rule 3: model versus what the install writes with.
        wm = write_model(a.content_type)
        if wm and a.embedding_model and a.embedding_model != wm:
            out.append(Finding(f.name, "model-differs-from-install", "info",
                               f"embedded with {a.embedding_model}; this install now writes "
                               f"{a.content_type} with {wm}",
                               "expected after a model switch; re-index under the current "
                               "model when you want one search space, then delete this sibling"))

        # Rule 4: size.
        if f.chunk_count < FANOUT_FLOOR:
            out.append(Finding(f.name, "below-fanout-floor", "warn",
                               f"{f.chunk_count} chunk(s): dropped from bare-corpus fan-out "
                               f"whenever a sibling has {FANOUT_FLOOR} or more",
                               "add its documents to a broad subject collection"))
        elif f.chunk_count < THIN_CHUNK_FLOOR:
            out.append(Finding(f.name, "thin-collection", "warn",
                               f"{f.chunk_count} chunks; a collection is meant to hold many documents",
                               "add to it, or merge it into the broader subject"))
        if f.doc_count == 1:
            out.append(Finding(f.name, "one-document", "warn",
                               "exactly one document: a partition with no cross-document ranking",
                               "move the document into the subject collection it belongs to"))

        # Rule 5: residue.
        if a.content_type == "knowledge" and _has_residue_token(a.owner_id):
            out.append(Finding(f.name, "test-residue", "warn",
                               f"subject {a.owner_id!r} looks like test or rehearsal residue",
                               "delete it, or re-put its documents with a TTL"))

        if a.content_type == "knowledge":
            knowledge_live.append(f)

    # Rule 2: likely duplicate subjects among live knowledge collections,
    # same model only (a different model is a deliberate sibling).
    seen: set[tuple[str, str]] = set()
    for i, x in enumerate(knowledge_live):
        for y in knowledge_live[i + 1:]:
            if x.attrs.embedding_model != y.attrs.embedding_model:
                continue
            sx, sy = x.attrs.owner_id.lower(), y.attrs.owner_id.lower()
            if sx == sy:
                continue
            tx, ty = set(_subject_tokens(sx)), set(_subject_tokens(sy))
            ratio = difflib.SequenceMatcher(None, sx, sy).ratio()
            token_subset = bool(tx and ty) and (tx <= ty or ty <= tx)
            if ratio >= DUP_NAME_SIMILARITY or token_subset:
                key = (min(x.name, y.name), max(x.name, y.name))
                if key in seen:
                    continue
                seen.add(key)
                out.append(Finding(x.name, "duplicate-subject", "warn",
                                   f"subjects {sx!r} and {sy!r} look like one topic "
                                   f"(name-only evidence, similarity {ratio:.2f})",
                                   "confirm with nx collection merge-candidates (topic "
                                   "overlap), then merge the smaller into the larger with "
                                   "nx collection rename, or record why they are distinct",
                                   related=y.name))
    return out


# ── report ─────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class AuditReport:
    findings: list[Finding]
    collections_examined: int
    collections_skipped_quarantine: int = 0
    by_check: Counter = field(default_factory=Counter)

    @classmethod
    def build(
        cls,
        facts: list[CollectionFacts],
        *,
        write_model_for: Callable[[str], str | None],
    ) -> AuditReport:
        findings = run_checks(facts, write_model_for=write_model_for)
        quarantine = sum(1 for f in facts if f.attrs.quarantine)
        return cls(
            findings=findings,
            collections_examined=len(facts) - quarantine,
            collections_skipped_quarantine=quarantine,
            by_check=Counter(f.check for f in findings),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "collections_examined": self.collections_examined,
            "collections_skipped_quarantine": self.collections_skipped_quarantine,
            "finding_count": len(self.findings),
            "by_check": dict(self.by_check),
            "findings": [f.to_dict() for f in self.findings],
        }


def audit(
    *,
    catalog: Any,
    t3: Any,
    write_model_for: Callable[[str], str | None],
) -> AuditReport:
    """The one I/O entry point: three reads, then the pure pipeline.

    Any read failure propagates: an audit that cannot see the tenant must
    not print an empty report that reads as clean (the nexus-moht0
    vacuous-gate doctrine).
    """
    rows = catalog.list_collections()
    stats = t3.collection_stats()
    docs = catalog.collection_doc_counts()
    _log.info("collection_audit_read", catalog_rows=len(rows), stats_rows=len(stats),
              doc_counted=len(docs))
    facts = gather_facts(catalog_rows=rows, stats_rows=stats, doc_counts=docs)
    return AuditReport.build(facts, write_model_for=write_model_for)
