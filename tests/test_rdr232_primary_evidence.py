# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.4): the bounded, project-aware named-source resolver and
section-diverse primary evidence.

Tenant-free on purpose. The catalog is a strict fake that answers the four calls the resolver is
allowed (``find``, ``get_owner_by_prefix``, ``resolve``, ``get_manifests``) and fails the test on
any other, so "no unbounded listing" is a property of the run, not a comment. Manifest rows are
the real ``ManifestRow`` built through the client's own wire decoder, so the reply shapes the
engine can send (a 0.1.164 reply with no label keys; a develop reply with ``labels_written`` true
or false) are exercised as the client decodes them. The real-catalog claims (the missing-owner
probe, resolution over two registered owners, hydration through the T3 service) ride the existing
tenant tests: ``test_rdr232_reserved_bindings.py`` and ``test_rdr232_golden_fixture.py``.

Test Plan scenarios of the bead (nexus-jypmj.1.4): 1 = two projects, one RDR number; 2 = a
document over the evidence budget; 3 = chunks without headings; 4 = several sources and shared
chashes; 5 = the same text in two documents; 6 = ``labels_written``.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from nexus.catalog.http_catalog_client import _manifest_row_from_dict
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, ManifestRow
from nexus.plans.evidence import (
    EvidenceRequest,
    OwnerResolution,
    SourceRef,
    SourceRefError,
    build_evidence_request,
    parse_sources,
)
from nexus.plans.primary_evidence import (
    DEFAULT_CAPS,
    PrimaryEvidenceCaps,
    derive_content_labels,
    gather_primary_evidence,
    resolve_named_sources,
    select_primary_evidence,
    t3_chunk_fetcher,
)

ART_OWNER, NEXUS_OWNER = "1.2", "1.1"
ART_DOC, NEXUS_DOC = "1.2.4686", "1.1.3996"
ART_COL, NEXUS_COL = "rdr__1-2__bge-base-en-v15-768__v1", "rdr__1-1__bge-base-en-v15-768__v1"
ART_PATH = "docs/rdr/RDR-084-list-parse-canonical-consolidation.md"
NEXUS_PATH = "docs/rdr/rdr-084-plan-library-growth.md"


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry(
    tumbler: str, file_path: str, *, ctype: str = "rdr", collection: str = ART_COL,
    chunk_count: int = 0, title: str | None = None, source_uri: str = "",
) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=title or file_path.rsplit("/", 1)[-1][:-3],
        author="", year=0, content_type=ctype, file_path=file_path, corpus="",
        physical_collection=collection, chunk_count=chunk_count, head_hash="",
        indexed_at="", source_uri=source_uri,
    )


class FakeCatalog:
    """The four calls the resolver and the hydrator may make; anything else fails the test."""

    def __init__(
        self, entries: Sequence[CatalogEntry] = (), manifests: dict[str, list[ManifestRow]] | None = None,
        owners: Sequence[str] = (ART_OWNER, NEXUS_OWNER),
    ) -> None:
        self.entries = list(entries)
        self.manifests = manifests or {}
        self.owners = set(owners)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_find = False

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the resolver made an unbounded or unexpected catalog call: {name}")

    def find(self, query: str, *, content_type: str | None = None, limit: int = 0) -> list[CatalogEntry]:
        self.calls.append(("find", {"query": query, "content_type": content_type, "limit": limit}))
        if self.fail_find:
            raise RuntimeError("engine unreachable")
        needle = query.lower()
        hits = [
            e for e in self.entries
            if needle in e.file_path.lower() or needle in e.title.lower()
            if content_type is None or e.content_type == content_type
        ]
        return hits[: limit if limit > 0 else 50]  # the engine's default cap is 50

    def get_owner_by_prefix(self, prefix: str) -> dict | None:
        self.calls.append(("get_owner_by_prefix", {"prefix": prefix}))
        return {"tumbler_prefix": prefix} if prefix in self.owners else None

    def resolve(self, tumbler: Any, *, follow_alias: bool = True) -> CatalogEntry | None:
        self.calls.append(("resolve", {"tumbler": str(tumbler)}))
        return next((e for e in self.entries if str(e.tumbler) == str(tumbler)), None)

    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[ManifestRow]]:
        self.calls.append(("get_manifests", {"doc_ids": list(doc_ids)}))
        return {d: self.manifests[d] for d in doc_ids if d in self.manifests}

    def count(self, name: str) -> int:
        return sum(1 for n, _ in self.calls if n == name)


class FakeT3:
    """chash -> text per collection, and a ledger of every fetch."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.fetches: list[tuple[str, int]] = []

    def put(self, collection: str, text: str) -> str:
        chash = _hash(text)
        self.store[(collection, chash)] = text
        return chash

    def fetch(self, collection: str, chashes: Sequence[str]) -> dict[str, str]:
        self.fetches.append((collection, len(chashes)))
        return {c: self.store[(collection, c)] for c in chashes if (collection, c) in self.store}


def _doc(
    cat: FakeCatalog, t3: FakeT3, tumbler: str, path: str, texts: Sequence[str], *,
    collection: str = ART_COL, labels: Sequence[tuple[bool, str | None]] | None = None,
    ctype: str = "rdr", declared_count: int | None = None,
) -> CatalogEntry:
    """Register a document, its manifest and its chunk texts. *labels* is one
    ``(labels_written, section_title)`` per chunk; ``None`` builds the 0.1.164 reply (no label keys)."""
    entry = _entry(
        tumbler, path, ctype=ctype, collection=collection,
        chunk_count=len(texts) if declared_count is None else declared_count,
    )
    cat.entries.append(entry)
    rows = []
    for i, text in enumerate(texts):
        wire: dict[str, Any] = {
            "position": i, "chash": t3.put(collection, text), "chunk_index": i, "collection": collection,
        }
        if labels is not None:
            written, title = labels[i]
            wire.update(labels_written=written, section_title=title, section_type=None, page_number=None)
        rows.append(_manifest_row_from_dict(wire))
    cat.manifests[tumbler] = rows
    return entry


def _two_projects(cat: FakeCatalog, t3: FakeT3) -> None:
    _doc(cat, t3, ART_DOC, ART_PATH, ["# ART RDR-084\nbody"], collection=ART_COL)
    _doc(cat, t3, NEXUS_DOC, NEXUS_PATH, ["# Nexus RDR-084\nbody"], collection=NEXUS_COL)


def _resolved(tumbler: str | None, *contribs: tuple[str, str]) -> OwnerResolution:
    if tumbler is None:
        return OwnerResolution("none")
    return OwnerResolution("resolved", tumbler, tuple(contribs) or (("owner", tumbler),))


RDR084 = SourceRef.parse("RDR-084")


# ── Scenario 1: two projects, one RDR number ──────────────────────────────────────────────


def test_two_projects_one_rdr_number_without_a_qualifier_is_candidates_not_a_pick() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    res = resolve_named_sources([RDR084], _resolved(None), cat=cat)
    (src,) = res.sources
    assert src.status == "candidates"
    assert src.tumbler is None, "no qualifier and two projects: nothing may be picked"
    assert sorted(c.tumbler for c in src.candidates) == [NEXUS_DOC, ART_DOC]
    assert not res.resolved()
    assert any("RDR-084" in line and ART_DOC in line and NEXUS_DOC in line for line in res.disclosures())


@pytest.mark.parametrize(("owner", "want", "other"), [(ART_OWNER, ART_DOC, NEXUS_DOC), (NEXUS_OWNER, NEXUS_DOC, ART_DOC)])
def test_an_owner_qualifier_picks_that_projects_document_and_never_the_other(
    owner: str, want: str, other: str,
) -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    (src,) = resolve_named_sources([RDR084], _resolved(owner), cat=cat).sources
    assert (src.status, src.tumbler, src.qualified) == ("resolved", want, True)
    assert other not in {c.tumbler for c in src.candidates}, "a foreign project's document is not a candidate"


def test_a_lone_unqualified_candidate_resolves_and_says_it_was_unqualified() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, ["x"])
    (src,) = resolve_named_sources([RDR084], _resolved(None), cat=cat).sources
    assert (src.status, src.tumbler, src.qualified) == ("resolved", ART_DOC, False)
    assert src.to_wire()["qualified"] is False


def test_a_qualifier_that_names_no_owner_in_the_catalog_is_disclosed_and_nothing_is_searched() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    # 9.9 is tumbler-shaped, so evidence.py accepts it without a catalog check; the resolver checks.
    (src,) = resolve_named_sources([RDR084], _resolved("9.9"), cat=cat).sources
    assert src.status == "unresolved" and src.tumbler is None
    assert "9.9" in src.detail and "not in the catalog" in src.detail
    assert cat.count("find") == 0, "an owner that does not exist must not fall back to a global search"


@pytest.mark.parametrize("status", ["conflict", "unresolved"])
def test_a_qualifier_that_could_not_be_honoured_resolves_nothing_and_searches_nothing(status: str) -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    owner = OwnerResolution(status, None, (), "owner 1.2 and scope 1.1 name different subtrees")
    (src,) = resolve_named_sources([RDR084], owner, cat=cat).sources
    assert src.status == "unresolved" and "different subtrees" in src.detail
    assert cat.calls == [], "no catalog call when the explicit qualifier already failed"


def test_a_document_depth_qualifier_is_a_document_and_must_agree_with_the_source() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    # scope=1.2.4686 with owner=1.2 resolves to the longer: a document, not an owner.
    (ok,) = resolve_named_sources([RDR084], _resolved(ART_DOC, ("owner", "1.2"), ("scope", ART_DOC)), cat=cat).sources
    assert (ok.status, ok.tumbler) == ("resolved", ART_DOC)
    (bad,) = resolve_named_sources([RDR084], _resolved("1.2.9999"), cat=cat).sources
    assert bad.status == "unresolved" and "1.2.9999" in bad.detail
    assert bad.tumbler is None, "a document-depth qualifier that is not the RDR must not be replaced by another"
    assert cat.count("get_owner_by_prefix") == 2
    assert {c["prefix"] for n, c in cat.calls if n == "get_owner_by_prefix"} == {"1.2"}, (
        "the owner probe asks for the owner (two segments), never the document tumbler")


def test_sibling_artifacts_sharing_the_number_are_candidates_and_a_longer_number_is_not_one() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", "docs/rdr/rdr-084-list-parse.md", ["x"])
    _doc(cat, t3, "1.2.11", "docs/rdr/rdr-084-gate-result.md", ["x"])
    _doc(cat, t3, "1.2.12", "docs/rdr/rdr-0840-other.md", ["x"])
    _doc(cat, t3, "1.2.13", "docs/rdr/post-mortem/rdr-084-list-parse.md", ["x"])
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert src.status == "candidates" and src.tumbler is None
    assert sorted(c.tumbler for c in src.candidates) == ["1.2.10", "1.2.11"]
    assert "by its tumbler" in src.detail, "the remedy must be named"


def test_one_basename_in_two_projects_is_ambiguous_without_a_qualifier_and_resolved_with_one() -> None:
    """The cross-repo collision rdr_canonical documents: the same file name under two owners."""
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, ["x"], collection=ART_COL)
    _doc(cat, t3, NEXUS_DOC, ART_PATH, ["x"], collection=NEXUS_COL)
    (src,) = resolve_named_sources([RDR084], _resolved(None), cat=cat).sources
    assert src.status == "candidates" and src.tumbler is None
    assert "2 projects" in src.detail and "owner or project" in src.detail
    (art,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert (art.status, art.tumbler) == ("resolved", ART_DOC)


def test_the_rdr_registration_beats_a_legacy_prose_one_and_duplicates_are_not_guessed() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", ART_PATH, ["x"], ctype="rdr")
    _doc(cat, t3, "1.2.11", ART_PATH, ["x"], ctype="prose")
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert (src.status, src.tumbler) == ("resolved", "1.2.10")

    cat2, t32 = FakeCatalog(), FakeT3()
    _doc(cat2, t32, "1.2.10", ART_PATH, ["x"], ctype="rdr")
    _doc(cat2, t32, "1.2.11", ART_PATH, ["x"], ctype="rdr")
    (dup,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat2).sources
    assert dup.status == "candidates" and dup.tumbler is None


def test_a_legacy_prose_only_registration_still_resolves() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", ART_PATH, ["x"], ctype="prose")
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert (src.status, src.tumbler) == ("resolved", "1.2.10")
    assert [c["content_type"] for n, c in cat.calls if n == "find"] == ["rdr", "prose"]


def test_a_document_tumbler_source_must_exist_and_sit_inside_the_qualifier() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    ok = SourceRef.parse(ART_DOC)
    (found,) = resolve_named_sources([ok], _resolved(None), cat=cat).sources
    assert (found.status, found.tumbler) == ("resolved", ART_DOC)
    (missing,) = resolve_named_sources([SourceRef.parse("1.2.7777")], _resolved(None), cat=cat).sources
    assert missing.status == "unresolved" and "1.2.7777" in missing.detail
    (outside,) = resolve_named_sources([ok], _resolved(NEXUS_OWNER), cat=cat).sources
    assert outside.status == "unresolved" and "1.1" in outside.detail
    assert cat.count("find") == 0, "a tumbler names its document; there is nothing to search for"


# ── Bounds ────────────────────────────────────────────────────────────────────────────────


def test_every_lookup_is_capped_and_a_candidate_set_at_its_cap_is_not_resolved() -> None:
    caps = replace(DEFAULT_CAPS, max_candidates_per_lookup=3)
    cat, t3 = FakeCatalog(), FakeT3()
    for i in range(3):  # three RDR-084 registrations exactly fill the cap
        _doc(cat, t3, f"1.2.{10 + i}", f"docs/rdr/rdr-084-variant-{i}.md", ["x"])
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat, caps=caps).sources
    assert src.candidates_truncated is True
    assert src.status == "unresolved" and src.tumbler is None, (
        "a candidate lookup that hit its cap may be hiding a second candidate: no pick")
    assert "cap" in src.detail
    limits = {c["limit"] for n, c in cat.calls if n == "find"}
    assert limits == {3}, "every find carries the stated cap and none is unbounded"


def test_the_whole_resolution_makes_a_bounded_number_of_catalog_calls() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    sources = [SourceRef.parse(f"RDR-{n:03d}") for n in range(60, 80)]
    for ref in sources:
        _doc(cat, t3, f"1.2.{ref.number}", f"docs/rdr/rdr-{ref.number:03d}-x.md", ["x"])
    res = resolve_named_sources(sources, _resolved(ART_OWNER), cat=cat)
    assert len(res.sources) == DEFAULT_CAPS.max_named_sources
    assert res.dropped == tuple(s.value for s in sources[DEFAULT_CAPS.max_named_sources:])
    assert all(s.status == "resolved" for s in res.sources)
    # one owner probe, then at most two finds per source
    assert cat.count("get_owner_by_prefix") == 1
    assert cat.count("find") <= 2 * DEFAULT_CAPS.max_named_sources
    assert res.catalog_calls == len(cat.calls)
    assert any("dropped" in line for line in res.disclosures())


def test_a_failing_catalog_is_unresolved_and_never_raises() -> None:
    cat = FakeCatalog()
    cat.fail_find = True
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert src.status == "unresolved" and "RuntimeError" in src.detail
    (none,) = resolve_named_sources([RDR084], _resolved(None), cat=None).sources
    assert none.status == "unresolved" and "catalog" in none.detail


def test_caps_are_one_calibration_object_and_leave_the_operator_cap_room() -> None:
    from nexus.plans.runner import _OPERATOR_MAX_INPUTS

    assert isinstance(DEFAULT_CAPS, PrimaryEvidenceCaps)
    assert 0 < DEFAULT_CAPS.max_selected_items < _OPERATOR_MAX_INPUTS, (
        "the reserved share must stay below the operator input cap so plan evidence keeps room")
    wire = DEFAULT_CAPS.to_wire()
    assert {"max_named_sources", "max_candidates_per_lookup", "max_manifest_rows", "max_fetched_chars",
            "max_selected_items", "max_selected_chars"} <= set(wire)


# ── Content-derived labels ────────────────────────────────────────────────────────────────


def test_a_heading_starts_a_section_the_next_chunks_inherit_and_early_content_is_unlabelled() -> None:
    labels = derive_content_labels([
        "preamble text, no heading yet",
        "# Title\nintro",
        "continuation of the intro",
        "## Decision\nwe decide\n## Consequences\nthese follow",
        "tail of consequences",
    ])
    assert [la.label for la in labels] == [None, "Title", "Title", "Decision", "Consequences"]
    assert labels[3].headings == ("Decision", "Consequences"), "every heading in the chunk is kept for matching"
    assert labels[3].carry == "Consequences", "the section the chunk ends in is what the next chunk inherits"


def test_a_comment_inside_a_code_fence_is_not_a_heading_even_across_a_chunk_boundary() -> None:
    labels = derive_content_labels([
        "## Run\n```bash\n# install the thing\nmake",
        "# still inside the fence\n```\nafter the fence",
    ])
    assert [la.label for la in labels] == ["Run", "Run"]
    assert all(la.headings == (("Run",) if i == 0 else ()) for i, la in enumerate(labels))


# ── Selection helpers ─────────────────────────────────────────────────────────────────────


def _select(cat: FakeCatalog, t3: FakeT3, sources: list[SourceRef], owner: str | None = ART_OWNER,
            caps: PrimaryEvidenceCaps = DEFAULT_CAPS):
    res = resolve_named_sources(sources, _resolved(owner), cat=cat, caps=caps)
    return res, select_primary_evidence(res, cat=cat, fetch_chunks=t3.fetch, caps=caps)


def _sectioned(n: int, names: dict[int, str] | None = None, tag: str = "") -> list[str]:
    names = names or {}
    return [f"## {names.get(i, f'Section {i}')}\nbody of section {i} {tag} " + "x" * 40 for i in range(n)]


# ── Scenario 2: a document over the evidence budget ───────────────────────────────────────


def test_a_document_over_the_budget_keeps_one_chunk_per_kind_of_section_spread_not_a_prefix() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    names = {3: "Proposed Solution", 11: "Research Findings", 24: "Implementation Status", 36: "Open Questions"}
    _doc(cat, t3, ART_DOC, ART_PATH, _sectioned(40, names))
    caps = replace(DEFAULT_CAPS, max_selected_items=10)
    res, primary = _select(cat, t3, [RDR084], caps=caps)

    (doc,) = primary.documents
    assert doc.manifest_rows == 40 and doc.selected_rows == 10 and doc.partial is True
    assert primary.partial is True
    positions = [i.position for i in primary.items]
    assert positions == sorted(positions) and len(set(positions)) == 10
    assert max(positions) >= 35 and positions != list(range(10)), "selection must not be the document prefix"
    # the decision, evidence, implementation-status and open-question sections that exist are all in
    labels = {i.label for i in primary.items}
    assert {"Proposed Solution", "Research Findings", "Implementation Status", "Open Questions"} <= labels
    assert len(labels) == 10, "ten distinct sections, one chunk each"
    assert primary.selected_items == 10 <= caps.max_selected_items


def test_a_section_kind_that_is_absent_is_not_assumed_and_a_small_document_is_not_partial() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _sectioned(5, {1: "Proposed Solution"}))
    _res, primary = _select(cat, t3, [RDR084])
    assert primary.selected_items == 5 and primary.documents[0].partial is False and primary.partial is False
    assert {i.label for i in primary.items} == {"Proposed Solution", "Section 0", "Section 2", "Section 3", "Section 4"}


# ── Scenario 3: chunks without headings ───────────────────────────────────────────────────


def test_chunks_without_headings_fall_back_to_position_and_say_the_metadata_is_partial() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, [f"plain paragraph {i} " + "y" * 30 for i in range(40)])
    caps = replace(DEFAULT_CAPS, max_selected_items=8)
    _res, primary = _select(cat, t3, [RDR084], caps=caps)
    (doc,) = primary.documents
    positions = [i.position for i in primary.items]
    assert len(positions) == 8 and positions != list(range(8))
    assert positions[0] == 0 and positions[-1] >= 35, "spread end to end"
    assert doc.positional_fallback is True and doc.labelled_rows == 0 and doc.metadata_partial is True
    assert doc.partial is True
    assert all(i.label is None and i.label_source == "none" for i in primary.items)
    assert any("no headings" in line for line in primary.disclosures())


# ── Scenario 4: several sources, shared chashes, aggregate budgets ────────────────────────


def test_aggregate_budgets_hold_across_sources_and_a_shared_chunk_keeps_every_position() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    shared = "## Shared Boilerplate\nthe same paragraph in two documents"
    for tumbler, n in (("1.2.10", 30), ("1.2.11", 30), ("1.2.12", 30)):
        texts = _sectioned(n, tag=tumbler)
        texts[0] = shared  # position 0 of every document is the same text, so one chash
        _doc(cat, t3, tumbler, f"docs/rdr/rdr-0{tumbler[-2:]}-doc.md", texts)
    caps = replace(DEFAULT_CAPS, max_selected_items=9, max_manifest_rows=45, max_selected_chars=100_000)
    sources = [SourceRef.parse(f"RDR-0{n}") for n in (10, 11, 12)]
    res, primary = _select(cat, t3, sources, caps=caps)

    assert [s.status for s in res.sources] == ["resolved"] * 3
    assert primary.selected_items <= caps.max_selected_items
    assert primary.manifest_rows_hydrated <= caps.max_manifest_rows == 45
    assert primary.fetched_chars <= caps.max_fetched_chars
    assert {d.source for d in primary.documents} == {"RDR-010", "RDR-011", "RDR-012"}
    per_doc = defaultdict(int)
    for item in primary.items:
        per_doc[item.doc_id] += 1
    assert set(per_doc) == {"1.2.10", "1.2.11", "1.2.12"} and max(per_doc.values()) - min(per_doc.values()) <= 3, (
        "one document must not take the aggregate budget")
    shared_items = [i for i in primary.items if i.chash == _hash(shared)]
    assert len(shared_items) == 1, "identical text is one piece of evidence"
    holders = {(shared_items[0].doc_id, shared_items[0].position)} | set(shared_items[0].also_at)
    assert holders == {("1.2.10", 0), ("1.2.11", 0), ("1.2.12", 0)}, (
        "each document's position of the shared chunk is kept")
    fetched_ids = sum(n for _c, n in t3.fetches)
    assert fetched_ids <= caps.max_manifest_rows, "unique chashes: the shared one was fetched once"


def test_rows_beyond_the_row_cap_are_sampled_across_the_document_and_disclosed() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _sectioned(1000))
    caps = replace(DEFAULT_CAPS, max_manifest_rows=200, max_selected_items=12)
    _res, primary = _select(cat, t3, [RDR084], caps=caps)
    (doc,) = primary.documents
    assert doc.manifest_rows == 1000 and doc.rows_hydrated <= 200 and doc.rows_sampled is True
    assert primary.manifest_rows_hydrated <= 200
    assert max(i.position for i in primary.items) > 800, "a sampled hydration is not the document prefix"
    assert "max_manifest_rows" in primary.caps_hit and primary.partial is True
    assert any("sampled" in line for line in primary.disclosures())


def test_the_fetched_character_cap_stops_fetching_and_is_disclosed() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, [f"## S{i}\n" + "z" * 900 for i in range(60)])
    caps = replace(DEFAULT_CAPS, max_fetched_chars=10_000, fetch_batch_ids=10, max_selected_items=6)
    _res, primary = _select(cat, t3, [RDR084], caps=caps)
    assert "max_fetched_chars" in primary.caps_hit and primary.partial is True
    assert primary.fetched_chars <= 10_000 + 10 * 1000, "at most one batch past the cap"
    assert primary.selected_items >= 1
    assert max(i.position for i in primary.items) >= 40, "the rows fetched before the cap are spread, not the prefix"


def test_selected_characters_are_capped_and_an_oversized_chunk_is_skipped_not_truncated() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    texts = ["## Big\n" + "b" * 5_000] + [f"## Small {i}\n" + "s" * 100 for i in range(9)]
    _doc(cat, t3, ART_DOC, ART_PATH, texts)
    caps = replace(DEFAULT_CAPS, max_selected_chars=2_000)
    _res, primary = _select(cat, t3, [RDR084], caps=caps)
    assert primary.selected_chars <= 2_000
    assert all(len(i.text) == len(texts[i.position]) for i in primary.items), "a chunk is whole or absent"
    assert "max_selected_chars" in primary.caps_hit and primary.documents[0].partial is True


# ── Missing and mismatched manifests ──────────────────────────────────────────────────────


def test_a_missing_manifest_and_a_short_manifest_are_disclosed_not_swallowed() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", "docs/rdr/rdr-010-gone.md", ["## A\nx"])
    del cat.manifests["1.2.10"]
    _doc(cat, t3, "1.2.11", "docs/rdr/rdr-011-short.md", _sectioned(4), declared_count=9)
    _res, primary = _select(cat, t3, [SourceRef.parse("RDR-010"), SourceRef.parse("RDR-011")])
    gone, short = primary.documents
    assert gone.status == "no_manifest" and gone.selected_rows == 0 and gone.partial is True
    assert short.status == "ok" and short.manifest_rows == 4 and short.manifest_rows_expected == 9
    assert short.partial is True, "fewer manifest rows than the document's chunk_count is not complete"
    lines = " ".join(primary.disclosures())
    assert "RDR-010" in lines and "no manifest" in lines and "RDR-011" in lines and "9" in lines


def test_missing_chunk_text_is_counted_and_never_selected() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _sectioned(6))
    lost = cat.manifests[ART_DOC][2]
    del t3.store[(ART_COL, lost.chash)]
    _res, primary = _select(cat, t3, [RDR084])
    (doc,) = primary.documents
    assert doc.missing_content == 1 and doc.partial is True
    assert 2 not in {i.position for i in primary.items}


def test_unresolved_sources_yield_no_evidence_and_no_hydration() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    res, primary = _select(cat, t3, [RDR084], owner=None)
    assert res.sources[0].status == "candidates"
    assert primary.items == () and primary.documents == () and cat.count("get_manifests") == 0
    assert t3.fetches == []


def test_hydration_fetches_each_collection_once_per_batch_with_unique_ids() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    texts = _sectioned(25)
    _doc(cat, t3, ART_DOC, ART_PATH, texts)
    # move the last ten chunks of the manifest into a second collection, as a re-index can
    other = "rdr__1-2__bge-base-en-v15-768__v2"
    rows = cat.manifests[ART_DOC]
    for i in range(15, 25):
        rows[i] = replace(rows[i], collection=other)
        t3.put(other, texts[i])
    caps = replace(DEFAULT_CAPS, fetch_batch_ids=10)
    _res, _primary = _select(cat, t3, [RDR084], caps=caps)
    by_col = defaultdict(list)
    for col, n in t3.fetches:
        by_col[col].append(n)
    assert sum(by_col[ART_COL]) == 15 and sum(by_col[other]) == 10
    assert all(n <= 10 for ns in by_col.values() for n in ns)
    assert len(_primary.items) == DEFAULT_CAPS.max_selected_items and _primary.documents[0].missing_content == 0
    assert _primary.documents[0].rows_hydrated == 25


# ── Scenario 6 and the old-engine reply: who may label a chunk ───────────────────────────


def test_a_manifest_label_counts_only_where_labels_written_and_an_empty_one_means_unlabelled() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    texts = [
        "## Content Heading Zero\nzero",   # labels_written false -> content label, stale title ignored
        "## Content Heading One\none",     # labels_written true  -> the manifest title wins
        "## Content Heading Two\ntwo",     # labels_written true, empty -> unlabelled, content ignored
        "no heading here",                 # labels_written false -> inherits the content section
    ]
    labels = [(False, "Stale Title"), (True, "Manifest Title"), (True, ""), (False, "Another Stale")]
    _doc(cat, t3, ART_DOC, ART_PATH, texts, labels=labels)
    _res, primary = _select(cat, t3, [RDR084])
    by_pos = {i.position: i for i in primary.items}
    assert (by_pos[0].label, by_pos[0].label_source) == ("Content Heading Zero", "content")
    assert (by_pos[1].label, by_pos[1].label_source) == ("Manifest Title", "manifest")
    assert (by_pos[2].label, by_pos[2].label_source) == (None, "manifest")
    assert (by_pos[3].label, by_pos[3].label_source) == ("Content Heading Two", "content"), (
        "a content-labelled row inherits the content section, not the manifest's choice for its neighbour")
    doc = primary.documents[0]
    assert doc.labels_from_manifest == 2 and doc.labels_from_content == 2


def test_a_reply_with_no_label_keys_is_content_labelled() -> None:
    """Engine 0.1.164: the manifest rows carry no section_title, section_type, page_number or
    labels_written. The client decodes that as labels_written false."""
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _sectioned(4), labels=None)
    assert all(r.labels_written is False and r.section_title is None for r in cat.manifests[ART_DOC])
    _res, primary = _select(cat, t3, [RDR084])
    assert [i.label for i in primary.items] == ["Section 0", "Section 1", "Section 2", "Section 3"]
    assert {i.label_source for i in primary.items} == {"content"}


# ── Scenario 5: the same text in two documents ────────────────────────────────────────────


def test_the_other_documents_title_is_never_used_for_shared_text() -> None:
    """The T3 chunk row is keyed (collection, chash), so two documents holding the same text share
    one row whose metadata is the last writer's. The fetcher hands the selector text only, so the
    row's section_title cannot reach a label, however the row is stamped."""
    shared = "## Proposed Solution\nthe same text in ART RDR-084 and in another RDR"

    class Collection:
        def get(self, ids: list[str]) -> dict[str, list]:
            return {
                "ids": list(ids), "documents": [shared for _ in ids],
                "metadatas": [{"section_title": "RDR-002 Intro", "section_type": "other", "title": "RDR-002"} for _ in ids],
            }

    class T3:
        def get_or_create_collection(self, name: str) -> Collection:
            return Collection()

    fetch = t3_chunk_fetcher(T3())
    chash = _hash(shared)
    assert fetch(ART_COL, [chash]) == {chash: shared}, "text only: no metadata crosses the boundary"

    cat = FakeCatalog()
    _doc(cat, FakeT3(), ART_DOC, ART_PATH, [shared])
    res = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat)
    primary = select_primary_evidence(res, cat=cat, fetch_chunks=fetch)
    (item,) = primary.items
    assert item.label == "Proposed Solution" and "RDR-002" not in json.dumps(primary.to_wire())


# ── Entry point and wire shape ────────────────────────────────────────────────────────────


def test_gather_reads_the_evidence_request_and_discloses_an_ambiguous_one() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    ambiguous = build_evidence_request({"source": "RDR-084"}, scope="rdr", name_lookup=lambda n: "")
    res, primary = gather_primary_evidence(ambiguous, cat=cat, fetch_chunks=t3.fetch)
    assert res.sources[0].status == "candidates" and primary.items == ()

    art = build_evidence_request({"source": "RDR-084", "owner": ART_OWNER}, scope="rdr", name_lookup=lambda n: "")
    res, primary = gather_primary_evidence(art, cat=cat, fetch_chunks=t3.fetch)
    assert [i.doc_id for i in primary.items] == [ART_DOC]

    none = build_evidence_request({"owner": ART_OWNER}, scope="rdr", name_lookup=lambda n: "")
    res, primary = gather_primary_evidence(none, cat=cat, fetch_chunks=t3.fetch)
    assert res.sources == () and primary.items == ()


def test_the_wire_shapes_are_json_and_carry_the_disclosures() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, ART_DOC, ART_PATH, _sectioned(6))
    res, primary = _select(cat, t3, [RDR084])
    wire = json.loads(json.dumps({"resolution": res.to_wire(), "primary": primary.to_wire()}))
    src = wire["resolution"]["sources"][0]
    assert src["status"] == "resolved" and src["tumbler"] == ART_DOC and src["source"] == {"kind": "rdr", "value": "RDR-084"}
    assert wire["primary"]["selected_items"] == 6 and wire["primary"]["documents"][0]["doc_id"] == ART_DOC
    assert wire["primary"]["caps"]["max_selected_items"] == DEFAULT_CAPS.max_selected_items
    assert "text" not in json.dumps(wire["primary"]["items"]), "the wire form carries positions and labels, not the bodies"


# ── The request widens from one source to several ─────────────────────────────────────────


def test_the_source_binding_takes_one_reference_a_list_or_a_delimited_string() -> None:
    one, errs = parse_sources("RDR-084")
    assert (one, errs) == ((SourceRef.parse("RDR-084"),), ())
    for raw in (["RDR-070", "rdr_095", "89"], "RDR-070, rdr_095; 89", ("RDR-070", "RDR-095", "RDR-089")):
        many, errs = parse_sources(raw)
        assert [s.value for s in many] == ["RDR-070", "RDR-095", "RDR-089"][: len(many)] and not errs
    assert [s.value for s in parse_sources("RDR-070, RDR-070")[0]] == ["RDR-070"], "repeats collapse"
    ok, errs = parse_sources(["RDR-070", "the ART paper"])
    assert [s.value for s in ok] == ["RDR-070"] and len(errs) == 1 and "the ART paper" in errs[0]


def test_the_single_source_wire_shape_is_unchanged_and_several_sources_are_additive() -> None:
    lookup = lambda name: ""  # noqa: E731
    single = build_evidence_request({"source": "RDR-084", "owner": "1.2"}, scope="", name_lookup=lookup)
    assert single.source == SourceRef.parse("RDR-084") and single.sources == (single.source,)
    wire = single.to_wire()
    assert wire["source"] == {"kind": "rdr", "value": "RDR-084"} and wire["source_error"] is None
    assert wire["sources"] == [{"kind": "rdr", "value": "RDR-084"}]

    several = build_evidence_request({"source": ["RDR-070", "RDR-095"]}, scope="", name_lookup=lookup)
    assert several.source is None, "there is no single source to read"
    assert [s.value for s in several.sources] == ["RDR-070", "RDR-095"]
    assert several.to_wire()["source"] is None and len(several.to_wire()["sources"]) == 2
    assert several.disclosures() == [], "two good sources are not a problem to report"

    partial = build_evidence_request({"source": ["RDR-070", "nope"]}, scope="", name_lookup=lookup)
    assert [s.value for s in partial.sources] == ["RDR-070"] and "nope" in (partial.source_error or "")
    assert isinstance(partial, EvidenceRequest)


def test_a_golden_required_source_label_maps_to_a_source_ref_and_a_project() -> None:
    from bench.schema import load_golden_set
    from tests import _rdr232_golden as g

    labels = [s for q in load_golden_set(g.GOLDEN_PATH) for s in q.required_sources]
    assert len(labels) >= 10 and any("@" in s for s in labels), "the golden set names a project-qualified source"
    named = [s for s in labels if s != "HybridRAG"]  # a paper title: not an RDR id, not a SourceRef
    assert len(labels) - len(named) == 1, "HybridRAG is the one required source a SourceRef cannot name"
    for label in named:
        ref, project = SourceRef.parse_labeled(label)
        assert ref.kind == "rdr" and (project is None) == ("@" not in label)
    with pytest.raises(SourceRefError):
        SourceRef.parse_labeled("HybridRAG")
    assert SourceRef.parse_labeled("RDR-084@ART") == (SourceRef.parse("RDR-084"), "ART")
    with pytest.raises(SourceRefError):
        SourceRef.parse_labeled("RDR-084@")
    with pytest.raises(SourceRefError):
        SourceRef.parse("RDR-084@ART")


# ── nx_answer: the resolution reaches the caller ──────────────────────────────────────────


async def _answer(cat: FakeCatalog, *, bindings: dict, structured: bool) -> Any:
    """``nx_answer`` over a one-step ad-hoc plan, the catalog replaced by *cat*. The same
    patches ``test_rdr232_reserved_bindings._text_answer`` uses, plus the catalog."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from nexus.mcp.core import nx_answer
    from nexus.plans.match import Match

    match = Match(
        plan_id=0, name="ad-hoc", description="q", confidence=None, dimensions={}, tags="ad-hoc",
        plan_json=json.dumps({"steps": [{"tool": "search", "args": {"query": "$intent"}}]}),
        required_bindings=["intent"], optional_bindings=[], default_bindings={"intent": "q"}, parent_dims=None,
    )
    result = MagicMock()
    result.steps = [{"text": "the answer is 42"}]
    result.dropped_reduce_steps = []
    result.step_records = []
    db_stub = MagicMock()
    db_stub.telemetry._supports_nx_answer_run_complete.return_value = False
    with patch("nexus.plans.matcher.plan_match", return_value=[]), \
         patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
         patch("nexus.plans.runner.plan_run", AsyncMock(return_value=result)), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp.core._get_catalog", return_value=cat), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        return await nx_answer(question="q", bindings=bindings, structured=structured)


@pytest.mark.asyncio
async def test_nx_answer_says_so_when_a_named_source_is_ambiguous_and_when_it_resolves() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)

    text = await _answer(cat, bindings={"source": "RDR-084"}, structured=False)
    assert text.startswith("[evidence request: named source RDR-084 is ambiguous"), text
    assert ART_DOC in text and NEXUS_DOC in text and text.endswith("the answer is 42")

    env = await _answer(cat, bindings={"source": "RDR-084"}, structured=True)
    (src,) = env["named_sources"]["sources"]
    assert (src["status"], src["tumbler"]) == ("candidates", None)

    env = await _answer(cat, bindings={"source": "RDR-084", "owner": ART_OWNER}, structured=True)
    (src,) = env["named_sources"]["sources"]
    assert (src["status"], src["tumbler"], src["qualified"]) == ("resolved", ART_DOC, True)
    assert env["final_text"] == "the answer is 42", "a clean resolution adds no notice to the answer"

    env = await _answer(cat, bindings={"source": "RDR-084", "owner": "9.9"}, structured=True)
    (src,) = env["named_sources"]["sources"]
    assert src["status"] == "unresolved" and "9.9" in src["detail"]

    env = await _answer(cat, bindings={"concept": "x"}, structured=True)
    assert env["named_sources"] is None, "no named source: nothing was resolved and no catalog call was made"
    assert cat.count("find") == 3
