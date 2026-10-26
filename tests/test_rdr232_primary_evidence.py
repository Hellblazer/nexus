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
        owners: Sequence[str] = (ART_OWNER, NEXUS_OWNER), repo_roots: dict[str, str] | None = None,
    ) -> None:
        self.repo_roots = repo_roots or {}
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
        names = {ART_OWNER: "ART", NEXUS_OWNER: "nexus"}
        if prefix not in self.owners:
            return None
        row = {"tumbler_prefix": prefix, "name": names.get(prefix, prefix), "owner_type": "repo"}
        if prefix in self.repo_roots:
            row["repo_root"] = self.repo_roots[prefix]
        return row

    def list_owners(self, *, include_deactivated: bool = False) -> list[dict]:
        self.calls.append(("list_owners", {}))
        names = {ART_OWNER: "ART", NEXUS_OWNER: "nexus"}
        return [
            {"tumbler_prefix": p, "name": names.get(p, p), "owner_type": "repo"} for p in sorted(self.owners)
        ]

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


def test_a_resolved_source_carries_the_rdr_number_of_its_document_or_none() -> None:
    """nexus-jypmj.1.7: the T2 lifecycle stream keys on it, for an RDR id and for a tumbler alike."""
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    _doc(cat, t3, "1.2.50", "src/art/listparse/mod_01.py", ["def f(): pass"], ctype="code")
    _doc(cat, t3, "1.2.51", "docs/rdr/post-mortem/rdr-084-list-parse.md", ["x"])
    (by_id,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    (by_tumbler,) = resolve_named_sources([SourceRef.parse(ART_DOC)], _resolved(None), cat=cat).sources
    (code,) = resolve_named_sources([SourceRef.parse("1.2.50")], _resolved(None), cat=cat).sources
    (post_mortem,) = resolve_named_sources([SourceRef.parse("1.2.51")], _resolved(None), cat=cat).sources
    assert (by_id.rdr_number, by_tumbler.rdr_number) == (84, 84)
    assert code.status == "resolved" and code.rdr_number is None
    assert post_mortem.status == "resolved" and post_mortem.rdr_number is None, "not directly inside an rdr/ directory"
    assert by_id.to_wire()["rdr_number"] == 84


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


async def _answer(
    cat: FakeCatalog, *, bindings: dict, structured: bool, t3: FakeT3 | None = None,
    t2_records: dict[tuple[str, str], str] | None = None, plan_run_calls: list | None = None,
    aspects: dict[str, Any] | None = None, aspect_calls: list[tuple[str, dict]] | None = None,
    question: str = "State the gate history of this RDR.",
) -> Any:
    """``nx_answer`` over a one-step ad-hoc plan, the catalog replaced by *cat*. The same
    patches ``test_rdr232_reserved_bindings._text_answer`` uses, plus the catalog. The default *question*
    has a gate facet: from Phase 2 (nexus-jypmj.2.2) the T2 lifecycle stream is read only for such a request."""
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
    records = t2_records or {}
    db_stub.memory.get.side_effect = lambda project=None, title=None, id=None, **_kw: (
        {"project": project, "title": title, "content": records[(project, title)]} if (project, title) in records else None)
    def _get_aspect(doc_id: str, **kw: Any) -> Any:
        if aspect_calls is not None:
            aspect_calls.append((doc_id, kw))
        return (aspects or {}).get(doc_id)

    db_stub.document_aspects.get_by_doc_id.side_effect = _get_aspect
    plan_run = AsyncMock(return_value=result)
    if plan_run_calls is not None:
        plan_run_calls.append(plan_run)
    with patch("nexus.plans.matcher.plan_match", return_value=[]), \
         patch("nexus.mcp.core._nx_answer_plan_miss", AsyncMock(return_value=match)), \
         patch("nexus.plans.runner.plan_run", plan_run), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db_stub)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp.core._get_catalog", return_value=cat), \
         patch("nexus.mcp.core._get_t3", return_value=MagicMock()), \
         patch("nexus.plans.primary_evidence.t3_chunk_fetcher", return_value=(t3 or FakeT3()).fetch), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None):
        t2_ctx.return_value.__enter__.return_value = db_stub
        return await nx_answer(question=question, bindings=bindings, structured=structured)


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

    env = await _answer(cat, bindings={"source": "RDR-084", "owner": ART_OWNER}, structured=True, t3=t3)
    (src,) = env["named_sources"]["sources"]
    assert (src["status"], src["tumbler"], src["qualified"]) == ("resolved", ART_DOC, True)
    # The ad-hoc plan is one search step: no operator consumed the primary evidence it read, and
    # the notice says so (nexus-jypmj.1.6). Nothing else is added for a clean resolution.
    assert env["final_text"].endswith("the answer is 42")
    # ... except that this RDR has no T2 records, which the lifecycle stream says (nexus-jypmj.1.7).
    assert env["final_text"].count("[evidence request:") == 2 and "was not merged into a reduction" in env["final_text"]
    assert "no T2 lifecycle records" in env["final_text"] and "ART_rdr" in env["final_text"]
    assert env["lifecycle_stream"]["sources"][0]["status"] == "no_records" and env["lifecycle_stream"]["records"] == []
    assert env["primary_evidence"]["selected_items"] == 1 and env["reserved_stream"]["merged"] is False

    # With its records in T2 the same call reserves them ahead of the chunk, through the real wiring.
    records = {("ART_rdr", "084"): "id: RDR-084\nstatus: accepted", ("ART_rdr", "084-gate-latest"): "outcome: PASSED"}
    env = await _answer(cat, bindings={"source": "RDR-084", "owner": ART_OWNER}, structured=True, t3=t3, t2_records=records)
    assert [r["kind"] for r in env["lifecycle_stream"]["records"]] == ["metadata", "gate-latest"]
    assert env["reserved_stream"]["reserved_items"] == 3, "two T2 records and the one chunk"
    assert env["final_text"].count("[evidence request:") == 1 and "no T2 lifecycle records" not in env["final_text"]

    env = await _answer(cat, bindings={"source": "RDR-084", "owner": "9.9"}, structured=True)
    (src,) = env["named_sources"]["sources"]
    assert src["status"] == "unresolved" and "9.9" in src["detail"]

    env = await _answer(cat, bindings={"concept": "x"}, structured=True)
    assert env["named_sources"] is None, "no named source: nothing was resolved and no catalog call was made"
    assert cat.count("find") == 4


@pytest.mark.asyncio
async def test_nx_answer_hands_plan_run_a_reserved_stream_that_leads_with_the_two_t2_records() -> None:
    """The call site itself (nexus-jypmj.1.7): nx_answer reads the lifecycle stream and passes it to the
    primary-stream builder, and the stream ``plan_run`` receives holds the two T2 records ahead of the
    chunk. Removing the ``_lifecycle`` argument at the call site leaves the chunk alone and fails this."""
    cat, t3 = FakeCatalog(), FakeT3()
    _two_projects(cat, t3)
    records = {("ART_rdr", "084"): "id: RDR-084\nstatus: accepted", ("ART_rdr", "084-gate-latest"): "outcome: PASSED"}
    runs: list = []
    env = await _answer(cat, bindings={"source": "RDR-084", "owner": ART_OWNER}, structured=True, t3=t3,
                        t2_records=records, plan_run_calls=runs)
    (run,) = runs
    stream = run.call_args.kwargs["reserved_stream"]
    assert [i.raw for i in stream.items[:2]] == ["id: RDR-084\nstatus: accepted", "outcome: PASSED"]
    assert len(stream.items) == 3 and stream.items[2].chash, "the chunk follows the two records"
    assert env["lifecycle_stream"]["reads"] <= 8


# ── nexus-jypmj.1.17: a stale-owner registration of the qualified project's own file ─────────
#
# Legacy RDRs were registered under an owner that is no longer the project's current one; only the
# source_uri under the project's docs/rdr/ shows they are its own. The qualifier names the current
# owner, so the resolver passes that owner's docs/rdr/ prefix and rdr_canonical admits them.

STALE_OWNER = "1.10"


def _stale_catalog(tmp_path, *, with_current: bool = False, stale_root=None, repo_root: bool = True,
                   root_value: str | None = None, stale_ctype: str = "rdr"):
    from nexus.catalog.rdr_canonical import rdr_source_prefix

    root = tmp_path / "ART"
    other = tmp_path / "elsewhere"
    roots = {ART_OWNER: root_value if root_value is not None else str(root)} if repo_root else {}
    cat, t3 = FakeCatalog(repo_roots=roots), FakeT3()
    prefix = rdr_source_prefix(stale_root if stale_root is not None else root)
    stale = _doc(cat, t3, "1.10.5", ART_PATH, ["# old\nbody"], collection=NEXUS_COL, ctype=stale_ctype)
    cat.entries[-1] = replace(stale, source_uri=prefix + ART_PATH.rsplit("/", 1)[-1])
    if with_current:
        _doc(cat, t3, ART_DOC, ART_PATH, ["# new\nbody"], collection=ART_COL)
    return cat, t3, other


def test_a_qualified_request_admits_a_stale_owner_registration_of_the_projects_own_file(tmp_path) -> None:
    cat, _t3, _ = _stale_catalog(tmp_path)
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert (src.status, src.tumbler, src.qualified) == ("resolved", "1.10.5", True), src.detail
    (unq,) = resolve_named_sources([RDR084], _resolved(None), cat=cat).sources
    assert (unq.status, unq.tumbler) == ("resolved", "1.10.5"), "the unqualified request already resolved it"


def test_the_qualified_projects_current_registration_wins_over_its_stale_one(tmp_path) -> None:
    cat, _t3, _ = _stale_catalog(tmp_path, with_current=True)
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert (src.status, src.tumbler) == ("resolved", ART_DOC), src.detail


def test_a_registration_under_another_owner_whose_file_is_not_the_projects_stays_ignored(tmp_path) -> None:
    cat, _t3, other = _stale_catalog(tmp_path, stale_root=tmp_path / "elsewhere")
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert src.status == "unresolved" and "1 in other projects ignored" in src.detail


def test_an_owner_with_no_repo_root_admits_by_owner_only_as_before(tmp_path) -> None:
    cat, _t3, _ = _stale_catalog(tmp_path, repo_root=False)
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert src.status == "unresolved" and "1 in other projects ignored" in src.detail


def test_a_foreign_rdr_registration_does_not_hide_the_projects_stale_prose_one(tmp_path) -> None:
    cat, t3, _ = _stale_catalog(tmp_path, stale_ctype="prose")
    _doc(cat, t3, NEXUS_DOC, NEXUS_PATH, ["# Nexus RDR-084\nbody"], collection=NEXUS_COL, ctype="rdr")
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert (src.status, src.tumbler, src.qualified) == ("resolved", "1.10.5", True), src.detail
    assert [c["content_type"] for n, c in cat.calls if n == "find"] == ["rdr", "prose"], "the prose pass ran"
    (unq,) = resolve_named_sources([RDR084], _resolved(None), cat=cat).sources
    assert (unq.status, unq.tumbler) == ("resolved", NEXUS_DOC), "unqualified: prose is read only without an rdr hit"


def test_a_qualified_rdr_hit_the_qualifier_admits_ends_the_search_before_prose(tmp_path) -> None:
    cat, t3, _ = _stale_catalog(tmp_path, with_current=True, stale_ctype="prose")
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert (src.status, src.tumbler) == ("resolved", ART_DOC), src.detail
    assert [c["content_type"] for n, c in cat.calls if n == "find"] == ["rdr"]


def test_two_stale_registrations_and_no_current_one_are_candidates_not_a_guess(tmp_path) -> None:
    from nexus.catalog.rdr_canonical import rdr_source_prefix

    cat, t3, _ = _stale_catalog(tmp_path)
    second = _doc(cat, t3, "1.11.7", ART_PATH, ["# older\nbody"], collection=NEXUS_COL, ctype="rdr")
    cat.entries[-1] = replace(second, source_uri=rdr_source_prefix(tmp_path / "ART") + ART_PATH.rsplit("/", 1)[-1])
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert src.status == "candidates" and src.tumbler is None, src.detail
    assert sorted(c.tumbler for c in src.candidates) == ["1.10.5", "1.11.7"]


def test_a_document_depth_qualifier_does_not_admit_a_stale_registration(tmp_path) -> None:
    cat, t3, _ = _stale_catalog(tmp_path)
    (src,) = resolve_named_sources([RDR084], _resolved(ART_DOC), cat=cat).sources
    assert src.status == "unresolved" and "1 in other projects ignored" in src.detail, src.detail
    _doc(cat, t3, ART_DOC, ART_PATH, ["# new\nbody"], collection=ART_COL)
    (cur,) = resolve_named_sources([RDR084], _resolved(ART_DOC), cat=cat).sources
    assert (cur.status, cur.tumbler) == ("resolved", ART_DOC), cur.detail


def test_a_relative_repo_root_degrades_to_owner_only_matching(tmp_path) -> None:
    from nexus.plans.primary_evidence import _owner_source_prefix

    assert _owner_source_prefix({"repo_root": "relative/ART"}) == ""
    assert _owner_source_prefix({"repo_root": ""}) == "" and _owner_source_prefix({}) == ""
    assert _owner_source_prefix({"repo_root": str(tmp_path / "ART")}).endswith("/ART/docs/rdr/")
    # the stale registration sits under the prefix a relative root would resolve to against the cwd:
    # it must NOT be admitted by a prefix invented from the process's working directory
    cat, _t3, _ = _stale_catalog(tmp_path, stale_root="relative/ART", root_value="relative/ART")
    (src,) = resolve_named_sources([RDR084], _resolved(ART_OWNER), cat=cat).sources
    assert src.status == "unresolved" and "1 in other projects ignored" in src.detail, src.detail


# ── nexus-jypmj.1.17: the manifest read is capped before it pulls, not only after ─────────────


def _three_docs(rows: int):
    cat, t3 = FakeCatalog(), FakeT3()
    refs = []
    for i, tumbler in enumerate(("1.2.10", "1.2.11", "1.2.12")):
        _doc(cat, t3, tumbler, f"docs/rdr/rdr-0{10 + i}-doc-{i}.md", _sectioned(rows, tag=str(i)))
        refs.append(SourceRef.parse(f"RDR-0{10 + i}"))
    return cat, t3, refs


def test_no_further_manifest_is_requested_once_the_rows_read_reach_the_cap() -> None:
    cat, t3, refs = _three_docs(30)
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=50, max_manifest_rows=50)
    _res, primary = _select(cat, t3, refs, caps=caps)
    pulled = [d for n, c in cat.calls if n == "get_manifests" for d in c["doc_ids"]]
    assert pulled == ["1.2.10", "1.2.11"], "the third document is never requested"
    first, second, third = primary.documents
    assert (first.status, second.status, third.status) == ("ok", "ok", "manifest_not_read")
    assert third.selected_rows == 0 and third.partial is True
    assert "max_manifest_rows_read" in primary.caps_hit
    assert any("RDR-012" in line and "not read" in line for line in primary.disclosures())
    assert {i.doc_id for i in primary.items} == {"1.2.10", "1.2.11"}


def test_manifests_within_the_cap_are_one_call_exactly_as_before() -> None:
    cat, t3, refs = _three_docs(30)
    _res, primary = _select(cat, t3, refs)
    assert [c["doc_ids"] for n, c in cat.calls if n == "get_manifests"] == [["1.2.10", "1.2.11", "1.2.12"]]
    assert [d.status for d in primary.documents] == ["ok", "ok", "ok"]
    assert "max_manifest_rows_read" not in primary.caps_hit


def _pulled(cat: FakeCatalog) -> list[list[str]]:
    return [list(c["doc_ids"]) for n, c in cat.calls if n == "get_manifests"]


def test_the_smallest_declared_documents_are_read_first_so_a_cap_costs_the_largest() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", "docs/rdr/rdr-010-big.md", _sectioned(40, tag="big"))
    _doc(cat, t3, "1.2.11", "docs/rdr/rdr-011-small.md", _sectioned(5, tag="small"))
    _doc(cat, t3, "1.2.12", "docs/rdr/rdr-012-mid.md", _sectioned(30, tag="mid"))
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=30)
    sources = [SourceRef.parse("RDR-010"), SourceRef.parse("RDR-011"), SourceRef.parse("RDR-012")]
    _res, primary = _select(cat, t3, sources, caps=caps)
    assert _pulled(cat) == [["1.2.11"], ["1.2.12"]], "small then mid, one call each; big is never requested"
    assert [(d.source, d.status) for d in primary.documents] == [
        ("RDR-010", "manifest_not_read"), ("RDR-011", "ok"), ("RDR-012", "ok"),
    ], "the largest document is the one the cap drops"
    assert primary.caps_hit == ("max_manifest_rows_read",)


def test_the_cap_is_soft_by_one_document_and_a_lone_source_larger_than_it_is_still_read() -> None:
    cat, t3, refs = _three_docs(30)
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=50, max_manifest_rows=50)
    _res, primary = _select(cat, t3, refs, caps=caps)
    read = sum(d.manifest_rows for d in primary.documents if d.status == "ok")
    assert read == 60 and 50 < read <= 50 + 30, "rows read reach the cap plus at most one document"
    lone, t3b = FakeCatalog(), FakeT3()
    _doc(lone, t3b, "1.2.10", "docs/rdr/rdr-010-huge.md", _sectioned(80, tag="huge"))
    _res, one = _select(lone, t3b, [SourceRef.parse("RDR-010")], caps=caps)
    assert [d.status for d in one.documents] == ["ok"] and one.documents[0].manifest_rows == 80


@pytest.mark.parametrize("recorded", [0, 2], ids=["recorded-zero", "recorded-understated"])
def test_a_misrecorded_chunk_count_reorders_the_read_but_the_stop_uses_the_rows_returned(recorded: int) -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.10", "docs/rdr/rdr-010-liar.md", _sectioned(60, tag="liar"), declared_count=recorded)
    _doc(cat, t3, "1.2.11", "docs/rdr/rdr-011-small.md", _sectioned(3, tag="small"))
    _doc(cat, t3, "1.2.12", "docs/rdr/rdr-012-next.md", _sectioned(48, tag="next"))
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=50, max_manifest_rows=50)
    sources = [SourceRef.parse("RDR-010"), SourceRef.parse("RDR-011"), SourceRef.parse("RDR-012")]
    _res, primary = _select(cat, t3, sources, caps=caps)
    # the order and the grouping trust the recorded count: the liar sorts first and shares a call with the
    # small one, so that call reads 63 rows against a cap of 50; the stop then counts the rows returned
    assert _pulled(cat) == [["1.2.10", "1.2.11"]]
    assert [d.status for d in primary.documents] == ["ok", "ok", "manifest_not_read"]
    assert primary.documents[0].manifest_rows == 60


def test_a_manifest_not_read_at_the_deadline_says_the_deadline_and_not_the_cap() -> None:
    cat, t3, refs = _three_docs(30)
    now = [0.0]
    real = cat.get_manifests

    def slow(doc_ids: list[str]):
        got = real(doc_ids)
        now[0] += 100.0  # the first call outlasts the whole read budget
        return got

    cat.get_manifests = slow  # type: ignore[method-assign]
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=50, max_manifest_rows=50, max_seconds=10.0)
    res = resolve_named_sources(refs, _resolved(ART_OWNER), cat=cat, caps=caps, clock=lambda: now[0])
    primary = select_primary_evidence(res, cat=cat, fetch_chunks=t3.fetch, caps=caps, clock=lambda: now[0])
    assert [d.status for d in primary.documents] == ["ok", "manifest_not_read", "manifest_not_read"]
    assert [d.not_read_reason for d in primary.documents] == ["", "deadline", "deadline"]
    assert "deadline" in primary.caps_hit and "max_manifest_rows_read" not in primary.caps_hit
    lines = [line for line in primary.disclosures() if "was not read" in line]
    assert len(lines) == 2 and all("deadline" in line and "cap on rows read" not in line for line in lines), lines


def test_documents_never_requested_after_a_failed_batch_are_not_read_with_the_failure_as_the_reason() -> None:
    cat, t3, refs = _three_docs(30)
    real = cat.get_manifests

    def second_call_fails(doc_ids: list[str]):
        if len(_pulled(cat)) == 1:
            cat.calls.append(("get_manifests", {"doc_ids": list(doc_ids)}))
            raise RuntimeError("engine unreachable")
        return real(doc_ids)

    cat.get_manifests = second_call_fails  # type: ignore[method-assign]
    caps = replace(DEFAULT_CAPS, max_manifest_rows_read=50, max_manifest_rows=50)
    _res, primary = _select(cat, t3, refs, caps=caps)
    assert _pulled(cat) == [["1.2.10"], ["1.2.11"]], "the third document was never requested"
    first, second, third = primary.documents
    assert (first.status, second.status, third.status) == ("ok", "manifest_error", "manifest_not_read")
    assert third.not_read_reason == "earlier_request_failed: RuntimeError"
    assert primary.caps_hit == (), "a failure is not a cap"
    assert any(
        "RDR-012" in line and "an earlier manifest request failed (RuntimeError)" in line and "never requested" in line
        for line in primary.disclosures()
    )
    assert any("RDR-011" in line and "could not be read" in line for line in primary.disclosures())


def test_a_failing_manifest_call_is_a_manifest_error_for_the_documents_of_that_call() -> None:
    cat, t3, refs = _three_docs(30)

    def boom(doc_ids: list[str]):
        cat.calls.append(("get_manifests", {"doc_ids": list(doc_ids)}))
        raise RuntimeError("engine unreachable")

    cat.get_manifests = boom  # type: ignore[method-assign]
    _res, primary = _select(cat, t3, refs)
    assert [d.status for d in primary.documents] == ["manifest_error"] * 3
    assert primary.items == ()


# ── Sibling files: the frontmatter id decides (nexus-xho6c) ───────────────────────────────

from nexus.plans.primary_evidence import (  # noqa: E402 — grouped with the tests they serve
    FILE_ABSENT,
    FILE_NOT_ALLOWED,
    FILE_PARSE_FAILED,
    FRONTMATTER_NOT_PARSED,
    OTHER_EXTRACTOR,
    ID_UNREADABLE,
    NO_ASPECT_ROW,
    FrontmatterId,
    _file_frontmatter_id,
    _read_id,
    recorded_frontmatter_id,
)

RDR200 = SourceRef.parse("RDR-200")
_MAIN200 = "docs/rdr/rdr-200-nx-answer-continuation-mode.md"
_SIBLINGS200 = (
    "docs/rdr/rdr-200-phase1-prereg.md", "docs/rdr/rdr-200-phase1-gate-result.md",
    "docs/rdr/rdr-200-phase1b-prereg.md",
)


class IdSource:
    """A frontmatter-id lookup over a fixed table: tumbler -> the recorded id ('' = parsed, no id key; a
    ``FrontmatterId`` as given; absent or ``None`` = nothing recorded). Keeps the ledger of every call."""

    def __init__(self, table: dict[str, Any], fail: Exception | None = None) -> None:
        self.table, self.fail = table, fail
        self.asked: list[str] = []
        self.timeouts: list[float] = []
        self.prefixes: list[str] = []

    def __call__(self, entry: CatalogEntry, timeout: float = 1.0, source_prefix: str = "") -> FrontmatterId:
        self.asked.append(str(entry.tumbler))
        self.timeouts.append(timeout)
        self.prefixes.append(source_prefix)
        if self.fail is not None:
            raise self.fail
        got = self.table.get(str(entry.tumbler))
        if isinstance(got, FrontmatterId):
            return got
        return FrontmatterId.unknown(NO_ASPECT_ROW) if got is None else FrontmatterId.of(got)


def _siblings(cat: FakeCatalog, t3: FakeT3, *, main_id: Any = "RDR-200") -> tuple[str, dict[str, Any]]:
    """The main RDR-200 file (1.2.20) and three siblings (1.2.21-23); returns the main tumbler and an id table
    in which only the main file carries an id."""
    _doc(cat, t3, "1.2.20", _MAIN200, ["# RDR-200\nbody"])
    table: dict[str, Any] = {"1.2.20": main_id}
    for i, path in enumerate(_SIBLINGS200, start=21):
        _doc(cat, t3, f"1.2.{i}", path, ["# note\nbody"])
        table[f"1.2.{i}"] = ""
    return "1.2.20", table


def test_a_bare_rdr_number_with_sibling_files_resolves_to_the_file_whose_frontmatter_carries_the_id() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    main, table = _siblings(cat, t3)
    ids = IdSource(table)
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=ids).sources
    assert (src.status, src.tumbler) == ("resolved", main), src.detail
    assert src.rdr_number == 200
    for path in _SIBLINGS200:
        assert path.rsplit("/", 1)[-1] in src.detail, "the passed-over sibling files are named"
    assert "frontmatter" in src.detail


def test_the_passed_over_siblings_reach_the_text_disclosure_of_a_resolved_source() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _siblings(cat, t3)
    res = resolve_named_sources([RDR200], _resolved(ART_OWNER), cat=cat, frontmatter_id=IdSource({
        "1.2.20": "RDR-200", "1.2.21": "", "1.2.22": "", "1.2.23": ""}))
    (src,) = res.sources
    assert src.status == "resolved" and src.qualified is True
    lines = res.disclosures()
    assert any("RDR-200" in ln and "frontmatter" in ln and "rdr-200-phase1-prereg.md" in ln for ln in lines), lines
    assert src.to_wire()["detail"] == src.detail, "the envelope carries the same note"


@pytest.mark.parametrize("recorded", ["RDR-200", "rdr-200", "200", 200, "Rdr_200", " RDR-200 ", "rdr-0200"])
def test_the_frontmatter_id_is_compared_the_way_a_source_binding_is_normalized(recorded: Any) -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    main, table = _siblings(cat, t3, main_id=recorded)
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert (src.status, src.tumbler) == ("resolved", main), (recorded, src.detail)


def test_a_file_carrying_another_rdrs_id_or_no_rdr_id_is_not_a_carrier() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3, main_id="RDR-201")
    table["1.2.21"] = "companion-note"
    table["1.2.22"] = "1.2.3"  # a tumbler-shaped value is not an RDR id
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert src.status == "candidates" and src.tumbler is None
    assert "no file" in src.detail and "RDR-200" in src.detail and "by its tumbler" in src.detail
    assert len(src.candidates) == 4


def test_two_files_carrying_the_id_are_candidates_and_both_are_named() -> None:
    """RDR-040 in this repo: two real RDR files both say id: RDR-040."""
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.30", "docs/rdr/rdr-040-cce-postmortem-gaps.md", ["x"])
    _doc(cat, t3, "1.2.31", "docs/rdr/rdr-040-developer-agent-circuit-breaker.md", ["x"])
    ids = IdSource({"1.2.30": "RDR-040", "1.2.31": "RDR-040"})
    (src,) = resolve_named_sources([SourceRef.parse("RDR-040")], _resolved(None), cat=cat, frontmatter_id=ids).sources
    assert src.status == "candidates" and src.tumbler is None
    assert "2 files" in src.detail
    assert "rdr-040-cce-postmortem-gaps.md" in src.detail and "rdr-040-developer-agent-circuit-breaker.md" in src.detail


def test_an_unreadable_sibling_id_keeps_candidates_because_a_second_carrier_could_hide_there() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    table["1.2.22"] = None  # nothing recorded for this file
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert src.status == "candidates" and src.tumbler is None
    assert "rdr-200-phase1-gate-result.md" in src.detail and "could not be checked" in src.detail


@pytest.mark.parametrize("odd", [
    "RDR-200 (superseded)", ["RDR-200"], 200.0,
    pytest.param("RDR-" + "9" * 5000, id="rdr-then-5000-digits"),
    pytest.param(10 ** 5000, id="int-of-5000-digits"),
])
def test_a_present_but_unreadable_rdr_id_on_a_sibling_is_unknown_not_clear_and_never_raises(odd: Any) -> None:
    """A sibling whose id is meant as an RDR id but cannot be read might be a second carrier. The 5000-digit
    values are the code-review case: ``int()`` of them raises ``ValueError`` past 4300 digits."""
    cat, t3 = FakeCatalog(), FakeT3()
    main, table = _siblings(cat, t3)
    table["1.2.22"] = odd
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert (src.status, src.tumbler) == ("candidates", None), src.detail
    assert "rdr-200-phase1-gate-result.md" in src.detail
    assert "not a readable RDR id" in src.detail


def test_a_value_that_is_plainly_not_an_rdr_id_stays_clear() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    main, table = _siblings(cat, t3)
    table.update({"1.2.21": "companion-note", "1.2.22": True, "1.2.23": "phase-2-note"})
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert (src.status, src.tumbler) == ("resolved", main), src.detail


@pytest.mark.parametrize(("raw", "want"), [
    ("RDR-200", ("number", 200)), ("rdr_200", ("number", 200)), ("200", ("number", 200)), (200, ("number", 200)),
    ("040", ("number", 40)), ("", ("none", None)), (None, ("none", None)), ("companion-note", ("none", None)),
    ("1.2.3", ("none", None)), (True, ("none", None)), ("phase-2-note", ("none", None)),
    ("RDR-200 (superseded)", ("unreadable", None)), ("200 draft", ("unreadable", None)),
    ([200], ("unreadable", None)), ({"id": 1}, ("unreadable", None)), (200.0, ("unreadable", None)),
    pytest.param("RDR-" + "9" * 4400, ("unreadable", None), id="rdr-then-4400-digits"),
    pytest.param("9" * 4400, ("unreadable", None), id="4400-digit-string"),
    pytest.param(10 ** 4400, ("unreadable", None), id="int-of-4400-digits"),
    pytest.param("x" * 100_000, ("none", None), id="long-plain-string"),
])
def test_read_id_classifies_every_shape_and_never_raises(raw: Any, want: tuple[str, int | None]) -> None:
    assert _read_id(raw) == want


def test_a_failing_id_source_is_candidates_with_the_failure_named_and_never_raises() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _siblings(cat, t3)
    ids = IdSource({}, fail=RuntimeError("t2 down"))
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=ids).sources
    assert src.status == "candidates" and src.tumbler is None
    assert "RuntimeError" in src.detail
    assert len(ids.asked) == 1, "a failing source is not asked again for every sibling"


def test_the_owner_qualifier_applies_first_and_a_foreign_projects_files_are_never_asked_about() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    main, table = _siblings(cat, t3)
    _doc(cat, t3, "1.1.50", "docs/rdr/rdr-200-other-project.md", ["x"], collection=NEXUS_COL)
    table["1.1.50"] = "RDR-200"  # a second carrier, but in another project
    ids = IdSource(table)
    (art,) = resolve_named_sources([RDR200], _resolved(ART_OWNER), cat=cat, frontmatter_id=ids).sources
    assert (art.status, art.tumbler) == ("resolved", main), art.detail
    assert "1.1.50" not in ids.asked
    (open_,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert open_.status == "candidates", "unqualified, one carrier in each project: two carriers, nothing picked"


def test_a_single_file_is_resolved_without_asking_the_id_source() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.20", _MAIN200, ["x"])
    ids = IdSource({})
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=ids).sources
    assert (src.status, src.tumbler, src.detail) == ("resolved", "1.2.20", "")
    assert ids.asked == [], "no sibling, no lookup"


def test_a_document_tumbler_still_wins_outright_and_asks_nothing() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _siblings(cat, t3)
    ids = IdSource({"1.2.20": "RDR-200"})
    (src,) = resolve_named_sources([SourceRef.parse("1.2.22")], _resolved(None), cat=cat, frontmatter_id=ids).sources
    assert (src.status, src.tumbler) == ("resolved", "1.2.22")
    assert ids.asked == []


def test_two_registrations_of_the_carrier_file_are_one_carrier_not_two() -> None:
    """A file counts once however often it is registered: the canonical rule, not the carrier count,
    then reports the double registration."""
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    _doc(cat, t3, "1.2.40", _MAIN200, ["again"])
    table["1.2.40"] = "RDR-200"
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert src.status == "candidates" and "registered more than once" in src.detail, src.detail
    assert "files carry" not in src.detail


def test_without_an_id_source_siblings_are_candidates_exactly_as_before() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _siblings(cat, t3)
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat).sources
    assert src.status == "candidates" and src.tumbler is None
    assert "by its tumbler" in src.detail and "frontmatter" not in src.detail


def test_the_id_lookups_are_bounded_by_one_cap_and_a_group_over_it_is_candidates() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    ids = IdSource(table)
    caps = replace(DEFAULT_CAPS, max_frontmatter_lookups=2)
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=ids, caps=caps).sources
    assert src.status == "candidates" and len(ids.asked) <= 2
    assert "cap of 2" in src.detail
    assert "max_frontmatter_lookups" in DEFAULT_CAPS.to_wire()


def test_no_id_read_starts_past_the_deadline_and_the_deadline_is_the_reason() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    now = [0.0]
    ids = IdSource(table)

    real_find = cat.find

    def find(*a: Any, **kw: Any) -> list[CatalogEntry]:
        out = real_find(*a, **kw)
        now[0] = 100.0  # the finds took the whole budget; the id reads have none left
        return out

    cat.find = find  # type: ignore[method-assign]
    (src,) = resolve_named_sources(
        [RDR200], _resolved(None), cat=cat, frontmatter_id=ids, clock=lambda: now[0], deadline=50.0,
    ).sources
    assert src.status == "candidates" and ids.asked == []
    assert "deadline" in src.detail


def test_each_id_read_is_given_the_time_left_clamped_to_one_call() -> None:
    """The lookup's timeout is the smaller of the per-call cap and what remains of the deadline (S3)."""
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    ids = IdSource(table)
    (_s,) = resolve_named_sources(
        [RDR200], _resolved(None), cat=cat, frontmatter_id=ids, clock=lambda: 0.0, deadline=2.0,
    ).sources
    assert ids.timeouts and all(t == pytest.approx(2.0) for t in ids.timeouts), ids.timeouts
    ids2 = IdSource(table)
    resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=ids2, clock=lambda: 0.0, deadline=500.0)
    assert ids2.timeouts and all(t == pytest.approx(DEFAULT_CAPS.frontmatter_call_seconds) for t in ids2.timeouts)


def test_a_second_carrier_stops_the_lookups() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _doc(cat, t3, "1.2.30", "docs/rdr/rdr-040-a.md", ["x"])
    _doc(cat, t3, "1.2.31", "docs/rdr/rdr-040-b.md", ["x"])
    for i in range(32, 36):
        _doc(cat, t3, f"1.2.{i}", f"docs/rdr/rdr-040-c{i}.md", ["x"])
    ids = IdSource({"1.2.30": "RDR-040", "1.2.31": "RDR-040"})
    resolve_named_sources([SourceRef.parse("RDR-040")], _resolved(None), cat=cat, frontmatter_id=ids)
    assert len(ids.asked) == 2


def test_the_owners_docs_rdr_prefix_is_handed_to_the_lookup_for_containment(tmp_path) -> None:
    cat, t3 = FakeCatalog(repo_roots={ART_OWNER: str(tmp_path)}), FakeT3()
    _, table = _siblings(cat, t3)
    ids = IdSource(table)
    resolve_named_sources([RDR200], _resolved(ART_OWNER), cat=cat, frontmatter_id=ids)
    assert ids.prefixes and all(p == f"file://{tmp_path.resolve()}/docs/rdr/" for p in ids.prefixes), ids.prefixes
    ids = IdSource(table)
    resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=ids)
    assert set(ids.prefixes) == {""}


# ── Why an id is unknown, and what to do about it ────────────────────────────────────────


def test_candidates_over_missing_rows_name_the_count_the_cause_and_the_remedy() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _siblings(cat, t3)
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource({})).sources
    assert src.status == "candidates"
    assert "no aspect row yet for 4" in src.detail
    assert f"`nx enrich aspects {ART_COL} --validate-sample 0`" in src.detail and "--all" not in src.detail
    assert "asynchronously" in src.detail and "by its tumbler" in src.detail


def test_rows_with_no_parsed_frontmatter_get_the_all_flag_remedy_and_each_cause_is_counted() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    table["1.2.20"] = FrontmatterId.unknown(FRONTMATTER_NOT_PARSED, FILE_ABSENT)
    table["1.2.21"] = FrontmatterId.unknown(FRONTMATTER_NOT_PARSED)
    table["1.2.22"] = FrontmatterId.unknown(NO_ASPECT_ROW, FILE_ABSENT)
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert src.status == "candidates"
    assert "an aspect row whose frontmatter was not parsed for 2" in src.detail
    assert "no aspect row yet for 1" in src.detail
    assert "the file is not readable on this host for 2" in src.detail
    assert f"`nx enrich aspects {ART_COL} --all --validate-sample 0`" in src.detail
    assert f"`nx enrich aspects {ART_COL} --validate-sample 0`" in src.detail
    assert "no model call" not in src.detail, "the default sample check calls a model; the remedy turns it off, not hides it"


def test_rows_by_another_extractor_get_the_all_flag_remedy() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    table["1.2.20"] = FrontmatterId.unknown(OTHER_EXTRACTOR)
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert f"`nx enrich aspects {ART_COL} --all --validate-sample 0`" in src.detail, (
        "gap-fill skips a document that has any row, so the other extractor's row needs --all")


def test_an_unreadable_id_value_is_named_without_an_enrich_remedy() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _, table = _siblings(cat, t3)
    table["1.2.22"] = "RDR-200 (superseded)"
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=IdSource(table)).sources
    assert "an id value that is not a readable RDR id for 1" in src.detail
    assert "nx enrich" not in src.detail


# ── The recorded id: the aspects row, then the file ──────────────────────────────────────


def _aspect(doc_id: str, extras: dict, *, extractor: str = "rdr-frontmatter-v1", confidence: float | None = 1.0) -> Any:
    from nexus.db.t2.records import AspectRecord

    return AspectRecord(
        collection=ART_COL, source_path="x.md", problem_formulation=None, proposed_method=None,
        extras=extras, extractor_name=extractor, doc_id=doc_id, confidence=confidence,
    )


def _rdr_dir(tmp_path):
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True)
    return d


def _entry_at(tumbler: str, path: str = _MAIN200, *, source_uri: str = "") -> CatalogEntry:
    return _entry(tumbler, path, source_uri=source_uri)


def _lookup(get_aspect: Any, entry: CatalogEntry, prefix: str = "") -> FrontmatterId:
    return recorded_frontmatter_id(get_aspect)(entry, 1.0, prefix)


def test_the_aspect_extractor_name_this_module_trusts_is_the_rdr_frontmatter_extractors() -> None:
    from nexus.aspect_extractor import _REGISTRY
    from nexus.plans.primary_evidence import RDR_FRONTMATTER_EXTRACTOR

    assert RDR_FRONTMATTER_EXTRACTOR == _REGISTRY["rdr__"].extractor_name


def test_the_recorded_id_is_the_aspect_rows_rdr_id() -> None:
    asked: list[tuple[str, float]] = []

    def aspects(doc_id: str, timeout: float) -> Any:
        asked.append((doc_id, timeout))
        return _aspect(doc_id, {"rdr_id": "RDR-200", "rdr_status": "accepted"})

    got = recorded_frontmatter_id(aspects)(_entry_at("1.2.20"), 3.5, "")
    assert (got.known, got.value) == (True, "RDR-200")
    assert asked == [("1.2.20", 3.5)], "the row is looked up by the catalog tumbler, with the call's timeout"


def test_a_parsed_aspect_row_without_an_rdr_id_is_a_recorded_absence_not_an_unknown() -> None:
    got = _lookup(lambda d, t: _aspect(d, {"rdr_status": "closed"}), _entry_at("1.2.21"))
    assert (got.known, got.value) == (True, "")


def test_a_row_that_shows_no_parsed_frontmatter_is_unknown_and_the_file_decides(tmp_path) -> None:
    """The live shape (nexus-xho6c, 377 of 377 rows): extras empty, confidence 1.0. And the null-fields row
    an extractor that failed leaves: confidence None. Neither says the document has no id."""
    d = _rdr_dir(tmp_path)
    carrier = d / "rdr-200-nx-answer-continuation-mode.md"
    carrier.write_text("---\ntitle: x\nid: RDR-200\n---\n# body\n")
    note = d / "rdr-200-phase1-prereg.md"
    note.write_text("---\ntitle: y\nid: companion-note\n---\n# body\n")
    for row in (lambda doc: _aspect(doc, {}), lambda doc: _aspect(doc, {}, confidence=None),
                lambda doc: _aspect(doc, {"rdr_id": "RDR-999"}, confidence=None)):
        got = _lookup(lambda doc, t, row=row: row(doc), _entry_at("1.2.20", source_uri=carrier.as_uri()))
        assert (got.known, got.value) == (True, "RDR-200"), "the row says nothing; the file is the carrier"
        got = _lookup(lambda doc, t, row=row: row(doc), _entry_at("1.2.21", source_uri=note.as_uri()))
        assert (got.known, got.value) == (True, "companion-note")
    gone = _lookup(lambda doc, t: _aspect(doc, {}), _entry_at("1.2.22", source_uri=(d / "gone.md").as_uri()))
    assert not gone.known and gone.reasons == (FRONTMATTER_NOT_PARSED, FILE_ABSENT)
    gone = _lookup(lambda doc, t: _aspect(doc, {}, confidence=None), _entry_at("1.2.22"))
    assert not gone.known and gone.reasons[0] == FRONTMATTER_NOT_PARSED


def test_live_shaped_rows_resolve_end_to_end_through_the_files(tmp_path) -> None:
    """Every sibling's aspect row has extras={} (the live fact). Under 'row read as known, no id' no group
    ever had a carrier; now the files decide."""
    d = _rdr_dir(tmp_path)
    cat, t3 = FakeCatalog(), FakeT3()
    main = d / _MAIN200.rsplit("/", 1)[-1]
    main.write_text("---\nid: RDR-200\n---\n")
    _doc(cat, t3, "1.2.20", _MAIN200, ["x"])
    cat.entries[-1] = replace(cat.entries[-1], source_uri=main.as_uri())
    for i, rel in enumerate(_SIBLINGS200, start=21):
        note = d / rel.rsplit("/", 1)[-1]
        note.write_text("---\nid: companion-note\n---\n")
        _doc(cat, t3, f"1.2.{i}", rel, ["x"])
        cat.entries[-1] = replace(cat.entries[-1], source_uri=note.as_uri())
    lookup = recorded_frontmatter_id(lambda doc, t: _aspect(doc, {}))
    (src,) = resolve_named_sources([RDR200], _resolved(None), cat=cat, frontmatter_id=lookup).sources
    assert (src.status, src.tumbler) == ("resolved", "1.2.20"), src.detail


def test_an_aspect_row_from_another_extractor_or_no_row_is_unknown_without_a_file() -> None:
    other = _lookup(lambda d, t: _aspect(d, {"rdr_id": "RDR-200"}, extractor="other-v1"), _entry_at("1.2.21"))
    assert not other.known and other.reasons[0] == "other_extractor"
    none = _lookup(lambda d, t: None, _entry_at("1.2.21"))
    assert not none.known and none.reasons == (NO_ASPECT_ROW, "no_file_source")


def test_an_aspect_store_error_falls_back_to_the_file_and_the_store_is_not_asked_again(tmp_path) -> None:
    d = _rdr_dir(tmp_path)
    f1, f2 = d / "rdr-200-a.md", d / "rdr-200-b.md"
    f1.write_text("---\nid: RDR-200\n---\n")
    f2.write_text("---\nid: companion-note\n---\n")
    calls: list[str] = []

    def aspects(doc_id: str, timeout: float) -> Any:
        calls.append(doc_id)
        raise TimeoutError("t2 slow")

    lookup = recorded_frontmatter_id(aspects)
    a = lookup(_entry_at("1.2.20", source_uri=f1.as_uri()), 1.0, "")
    b = lookup(_entry_at("1.2.21", source_uri=f2.as_uri()), 1.0, "")
    assert (a.known, a.value) == (True, "RDR-200") and (b.known, b.value) == (True, "companion-note")
    assert calls == ["1.2.20"], "after the first failure the store is not asked again this request"
    c = lookup(_entry_at("1.2.22"), 1.0, "")
    assert not c.known and c.reasons[0] == "aspect_store_error"


def test_the_file_is_read_only_as_a_fallback_and_only_where_it_exists(tmp_path) -> None:
    d = _rdr_dir(tmp_path)
    path = d / "rdr-200-nx-answer-continuation-mode.md"
    path.write_text("---\ntitle: x\nid: RDR-200\n---\n# body\n")
    note = d / "rdr-200-phase1-prereg.md"
    note.write_text("---\ntitle: y\nid: companion-note\n---\n# body\n")
    bare = d / "rdr-200-bare.md"
    bare.write_text("# no frontmatter\n")
    empty_header = d / "rdr-200-empty-header.md"
    empty_header.write_text("---\n---\n# body\n")
    unclosed = d / "rdr-200-unclosed.md"
    unclosed.write_text("---\nid: RDR-200\n# the header never closes\n")
    octal = d / "rdr-040-octal.md"
    octal.write_text("---\nid: 040\n---\n")
    row = lambda doc, t: None  # noqa: E731 — no aspect rows: the file is all there is
    assert _lookup(row, _entry_at("1.2.20", source_uri=path.as_uri())).value == "RDR-200"
    assert _lookup(row, _entry_at("1.2.21", source_uri=note.as_uri())).value == "companion-note"
    assert _read_id(_lookup(row, _entry_at("1.2.28", source_uri=octal.as_uri())).value) == ("number", 40), (
        "id: 040 is the number 40, never octal 32")
    for one in (bare, empty_header):
        got = _lookup(row, _entry_at("1.2.22", source_uri=one.as_uri()))
        assert (got.known, got.value) == (True, ""), one.name
    got = _lookup(row, _entry_at("1.2.27", source_uri=unclosed.as_uri()))
    assert not got.known and FILE_PARSE_FAILED in got.reasons, "an unclosed header is unknown"
    assert FILE_ABSENT in _lookup(row, _entry_at("1.2.24", source_uri=(d / "gone.md").as_uri())).reasons
    assert "no_file_source" in _lookup(row, _entry_at("1.2.25", source_uri="")).reasons
    assert "no_file_source" in _lookup(row, _entry_at("1.2.26", source_uri="https://example.invalid" + str(path))).reasons

    path.write_text("---\nid: RDR-999\n---\n")
    full = _lookup(lambda doc, t: _aspect(doc, {"rdr_id": "RDR-200"}), _entry_at("1.2.20", source_uri=path.as_uri()))
    assert full.value == "RDR-200", "a parsed row outranks the file"


def test_the_file_fallback_is_contained(tmp_path) -> None:
    """Only a .md under docs/rdr (and under the owner's prefix when it has one); no host part; symlinks
    are followed before the check (code review S1, critique F5)."""
    d = _rdr_dir(tmp_path)
    ok = d / "rdr-200-a.md"
    ok.write_text("---\nid: RDR-200\n---\n")
    other = tmp_path / "notes" / "rdr-200-b.md"
    other.parent.mkdir()
    other.write_text("---\nid: RDR-200\n---\n")
    txt = d / "rdr-200-c.txt"
    txt.write_text("---\nid: RDR-200\n---\n")
    link = d / "rdr-200-link.md"
    link.symlink_to(other)
    row = lambda doc, t: None  # noqa: E731

    def read(path_or_uri: Any, prefix: str = "") -> FrontmatterId:
        uri = path_or_uri if isinstance(path_or_uri, str) else path_or_uri.as_uri()
        return _file_frontmatter_id(_entry_at("1.2.20", source_uri=uri), prefix)

    assert read(ok).value == "RDR-200"
    for refused in (other, txt, link):
        got = read(refused)
        assert not got.known and got.reasons == (FILE_NOT_ALLOWED,), (refused.name, got)
    host = read("file://elsewhere" + str(ok))
    assert not host.known and host.reasons == (FILE_NOT_ALLOWED,), "a non-empty netloc is refused"
    own = f"file://{tmp_path.resolve()}/docs/rdr/"
    assert read(ok, own).value == "RDR-200"
    foreign = f"file://{(tmp_path / 'checkout2').resolve()}/docs/rdr/"
    got = read(ok, foreign)
    assert not got.known and got.reasons == (FILE_NOT_ALLOWED,), "outside the owner's own docs/rdr tree"
    assert _lookup(row, _entry_at("1.2.20", source_uri=other.as_uri()), own).reasons[-1] == FILE_NOT_ALLOWED


def test_a_repo_root_behind_a_symlink_does_not_refuse_its_files(tmp_path) -> None:
    """The file is resolved before the check, so the owner's prefix is resolved too: a prefix spelled through
    a symlink still contains the file it points at."""
    real = tmp_path / "real"
    d = _rdr_dir(real)
    ok = d / "rdr-200-a.md"
    ok.write_text("---\nid: RDR-200\n---\n")
    via = tmp_path / "via"
    via.symlink_to(real)
    prefix = f"file://{via}/docs/rdr/"  # as an owner whose root is registered through the link spells it
    got = _file_frontmatter_id(_entry_at("1.2.20", source_uri=(via / "docs" / "rdr" / ok.name).as_uri()), prefix)
    assert got.known and got.value == "RDR-200", got
    foreign = f"file://{tmp_path / 'checkout2'}/docs/rdr/"
    refused = _file_frontmatter_id(_entry_at("1.2.20", source_uri=ok.as_uri()), foreign)
    assert not refused.known and refused.reasons == (FILE_NOT_ALLOWED,), "a different tree is still refused"


def test_a_parse_failure_is_unknown_and_logs_nothing_of_the_file(tmp_path, monkeypatch) -> None:
    from structlog.testing import capture_logs

    d = _rdr_dir(tmp_path)
    f = d / "rdr-200-secret.md"
    f.write_text("---\nid: RDR-200\n---\nPRIVATE-BODY\n")

    def boom(_text: str) -> Any:
        raise ValueError("PRIVATE-BODY leaked through a parser message")

    monkeypatch.setattr("nexus.aspect_extractor._split_rdr_frontmatter", boom)
    with capture_logs() as logs:
        got = _file_frontmatter_id(_entry_at("1.2.20", source_uri=f.as_uri()))
    assert not got.known and got.reasons == (FILE_PARSE_FAILED,)
    assert "PRIVATE-BODY" not in repr(logs) and logs, "a warning is logged, with the exception type only"


@pytest.mark.asyncio
async def test_nx_answer_resolves_a_bare_rdr_with_siblings_through_the_aspects_store() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    main, _ = _siblings(cat, t3)
    rows = {"1.2.20": _aspect("1.2.20", {"rdr_id": "RDR-200"})}
    rows.update({f"1.2.{i}": _aspect(f"1.2.{i}", {"rdr_id": "companion-note"}) for i in (21, 22, 23)})
    calls: list[tuple[str, dict]] = []
    env = await _answer(cat, bindings={"source": "RDR-200"}, structured=True, t3=t3, aspects=rows, aspect_calls=calls)
    (src,) = env["named_sources"]["sources"]
    assert (src["status"], src["tumbler"]) == ("resolved", main), src["detail"]
    assert "rdr-200-phase1-prereg.md" in src["detail"]
    assert calls and all(
        0 < kw["timeout"] <= DEFAULT_CAPS.frontmatter_call_seconds and kw["retry_read_timeout"] is False
        for _doc, kw in calls
    ), f"each aspect read is clamped to the budget and not retried at the clamp: {calls}"
    assert any("frontmatter" in ln and "rdr-200-phase1-prereg.md" in ln for ln in env["final_text"].splitlines())


@pytest.mark.asyncio
async def test_nx_answer_over_live_shaped_rows_and_no_files_discloses_the_reason_and_the_remedy() -> None:
    cat, t3 = FakeCatalog(), FakeT3()
    _siblings(cat, t3)
    rows = {f"1.2.{i}": _aspect(f"1.2.{i}", {}) for i in (20, 21, 22, 23)}  # extras={} on every row, as live
    env = await _answer(cat, bindings={"source": "RDR-200"}, structured=True, t3=t3, aspects=rows)
    (src,) = env["named_sources"]["sources"]
    assert (src["status"], src["tumbler"]) == ("candidates", None), "nothing is guessed from an unparsed row"
    assert "an aspect row whose frontmatter was not parsed for 4" in src["detail"]
    assert f"`nx enrich aspects {ART_COL} --all --validate-sample 0`" in src["detail"]
    assert "nx enrich aspects" in env["final_text"], "the remedy reaches the text a caller reads"

    env = await _answer(cat, bindings={"source": "RDR-200"}, structured=True, t3=t3, aspects={})
    (src,) = env["named_sources"]["sources"]
    assert (src["status"], src["tumbler"]) == ("candidates", None), "no row recorded: nothing is guessed"
    assert "no aspect row yet for 4" in src["detail"]
