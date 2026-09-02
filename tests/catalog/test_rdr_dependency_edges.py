# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the RDR-to-RDR dependency-edge generator (RDR-201 Phase 3.2,
bead nexus-j9z30.21).

``generate_rdr_dependency_links`` seeds catalog links from frontmatter that
already exists on disk (``supersedes``, ``superseded_by``, ``parent_rdr``,
``related_rdrs`` — Finding 4, T2 ``nexus_rdr/201-research-2``). Both
endpoints of every candidate edge are resolved through
:mod:`nexus.catalog.rdr_canonical`'s single resolution authority
(``resolve_all`` / ``resolve_canonical_tumbler`` / ``is_in_repo``) — this
suite never reimplements that rule; it only exercises the NEW work this
bead adds on top: extracting ``RDR-NNN`` references out of frontmatter
values, and disambiguating a numeric-prefix collision (a companion note or
phase artifact sharing an RDR's leading number, e.g. RDR-152's three
``rdr-152-*.md`` files) via each candidate's own frontmatter ``id: RDR-NNN``
self-declaration.

Pure-logic coverage, matching ``test_rdr_canonical_tumbler.py``'s style: a
small fixture "catalog" (list of ``CatalogEntry``) plus real files under
``tmp_path`` (so ``resolve_path`` + frontmatter parsing exercise the real
code path) stand in for the wire. ``DryRunLinkWriter`` (already shipped for
the other three generators) is the writer under test throughout — this
generator introduces no separate dry-run mechanism; it composes with the
existing one exactly like ``generate_rdr_filepath_links`` does.
"""
from __future__ import annotations

from pathlib import Path

import structlog.testing

from nexus.catalog.link_generator import (
    DryRunLinkWriter,
    ProposedLink,
    generate_rdr_dependency_links,
)
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry

CURRENT_OWNER = Tumbler.parse("1.1")
OTHER_OWNER = Tumbler.parse("1.10")


def _entry(
    tumbler: str,
    *,
    content_type: str = "rdr",
    file_path: str = "",
    title: str = "",
    source_uri: str = "",
) -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler),
        title=title,
        author="",
        year=0,
        content_type=content_type,
        file_path=file_path,
        corpus="nexus",
        physical_collection="",
        chunk_count=1,
        head_hash="",
        indexed_at="2026-09-02T00:00:00Z",
        source_uri=source_uri,
    )


def _write_rdr(tmp_path: Path, name: str, frontmatter: str, body: str = "") -> Path:
    """Write a minimal RDR .md file under ``<tmp_path>/docs/rdr/<name>``."""
    rdr_dir = tmp_path / "docs" / "rdr"
    rdr_dir.mkdir(parents=True, exist_ok=True)
    f = rdr_dir / name
    f.write_text(f"---\n{frontmatter}\n---\n\n{body or f'# {name}'}\n")
    return f


class _FakeCat:
    """Minimal ``CatalogReader`` double: ``all_documents`` + ``resolve_path``.

    ``resolve_path`` resolves a tumbler to the on-disk file the matching
    fixture entry's ``file_path`` names, mirroring the real
    ``HttpCatalogClient.resolve_path`` contract closely enough for this
    generator (which only ever calls ``resolve_path`` then reads text).
    """

    def __init__(self, entries: list[CatalogEntry], *, repo_root: Path) -> None:
        self._entries = entries
        self._repo_root = repo_root

    def all_documents(self, limit=1000, *, content_type: str = "", offset=0):
        if content_type:
            return [e for e in self._entries if e.content_type == content_type]
        return list(self._entries)

    def resolve_path(self, tumbler: Tumbler) -> Path | None:
        for e in self._entries:
            if e.tumbler == tumbler:
                if not e.file_path:
                    return None
                p = Path(e.file_path)
                return p if p.is_absolute() else self._repo_root / p
        return None


THIS_REPO_PREFIX = "file:///repo/nexus/docs/rdr/"


def _source_uri(tmp_path: Path, name: str) -> str:
    return f"file://{tmp_path}/docs/rdr/{name}"


class TestSupersedes:
    def test_forward_edge_created(self, tmp_path: Path) -> None:
        """``supersedes: RDR-014`` on RDR-015 -> supersedes(015, 014)."""
        _write_rdr(tmp_path, "rdr-015-a.md", 'title: "RDR-015"\nsupersedes: RDR-014')
        _write_rdr(tmp_path, "rdr-014-b.md", 'title: "RDR-014"')
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-015-a.md",
            source_uri=_source_uri(tmp_path, "rdr-015-a.md"),
        )
        target = _entry(
            "1.1.11", file_path="docs/rdr/rdr-014-b.md",
            source_uri=_source_uri(tmp_path, "rdr-014-b.md"),
        )
        cat = _FakeCat([source, target], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 1
        assert w.proposed == [
            ProposedLink(
                from_tumbler="1.1.10", to_tumbler="1.1.11",
                link_type="supersedes", created_by="rdr_dependency_extractor",
            )
        ]

    def test_superseded_by_produces_the_same_direction_as_supersedes(
        self, tmp_path: Path,
    ) -> None:
        """``superseded_by: RDR-015`` on RDR-014 -> supersedes(015, 014) —
        the SAME edge direction as the forward ``supersedes:`` case: the
        successor is always the ``from`` side regardless of which document
        declared the relationship."""
        _write_rdr(tmp_path, "rdr-014-b.md", 'title: "RDR-014"\nsuperseded_by: RDR-015')
        _write_rdr(tmp_path, "rdr-015-a.md", 'title: "RDR-015"')
        source = _entry(
            "1.1.11", file_path="docs/rdr/rdr-014-b.md",
            source_uri=_source_uri(tmp_path, "rdr-014-b.md"),
        )
        target = _entry(
            "1.1.10", file_path="docs/rdr/rdr-015-a.md",
            source_uri=_source_uri(tmp_path, "rdr-015-a.md"),
        )
        cat = _FakeCat([source, target], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 1
        p = w.proposed[0]
        assert (p.from_tumbler, p.to_tumbler, p.link_type) == ("1.1.10", "1.1.11", "supersedes")

    def test_non_rdr_value_produces_no_edge(self, tmp_path: Path) -> None:
        """``supersedes: docs/proposals/foo.md`` — not an RDR reference at
        all (no ``RDR-NNN`` prefix); silently produces nothing, not an
        error."""
        _write_rdr(
            tmp_path, "rdr-150-a.md",
            'title: "RDR-150"\nsupersedes: docs/proposals/foo.md',
        )
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-150-a.md",
            source_uri=_source_uri(tmp_path, "rdr-150-a.md"),
        )
        cat = _FakeCat([source], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 0
        assert w.proposed == []

    def test_parenthetical_suffix_still_extracts_the_rdr_reference(
        self, tmp_path: Path,
    ) -> None:
        """``supersedes: ["RDR-159 (partial: ...)"]`` — extra prose after
        the identifier does not block extraction (real corpus shape:
        rdr-185's supersedes field)."""
        _write_rdr(
            tmp_path, "rdr-185-a.md",
            'title: "RDR-185"\nsupersedes: ["RDR-159 (partial: the upgrade command surface)"]',
        )
        _write_rdr(tmp_path, "rdr-159-b.md", 'title: "RDR-159"')
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-185-a.md",
            source_uri=_source_uri(tmp_path, "rdr-185-a.md"),
        )
        target = _entry(
            "1.1.11", file_path="docs/rdr/rdr-159-b.md",
            source_uri=_source_uri(tmp_path, "rdr-159-b.md"),
        )
        cat = _FakeCat([source, target], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 1
        p = w.proposed[0]
        assert (p.from_tumbler, p.to_tumbler, p.link_type) == ("1.1.10", "1.1.11", "supersedes")


class TestForeignRepoQualifiedReference:
    def test_conexus_prefixed_reference_is_not_treated_as_local_rdr_001(
        self, tmp_path: Path,
    ) -> None:
        """``related_rdrs: [conexus:RDR-001]`` (real corpus shape, rdr-155)
        must NOT resolve against this repo's own RDR-001, even though one
        exists — the value does not start with ``RDR-``, so the extractor
        never treats the trailing digits as a same-repo reference."""
        _write_rdr(
            tmp_path, "rdr-155-a.md",
            'title: "RDR-155"\nrelated_rdrs: [conexus:RDR-001]',
        )
        _write_rdr(tmp_path, "rdr-001-b.md", 'title: "RDR-001"')
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-155-a.md",
            source_uri=_source_uri(tmp_path, "rdr-155-a.md"),
        )
        local_001 = _entry(
            "1.1.11", file_path="docs/rdr/rdr-001-b.md",
            source_uri=_source_uri(tmp_path, "rdr-001-b.md"),
        )
        cat = _FakeCat([source, local_001], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 0
        assert w.proposed == []


class TestParentRdrAndRelatedRdrs:
    def test_parent_rdr_creates_relates_edge_from_child_to_parent(
        self, tmp_path: Path,
    ) -> None:
        _write_rdr(
            tmp_path, "rdr-105-shakeout.md",
            'title: "RDR-105 Shakeout"\nid: companion-note\nparent_rdr: RDR-105',
        )
        _write_rdr(
            tmp_path, "rdr-105-main.md",
            'title: "RDR-105"\nid: RDR-105',
        )
        child = _entry(
            "1.1.10", file_path="docs/rdr/rdr-105-shakeout.md",
            source_uri=_source_uri(tmp_path, "rdr-105-shakeout.md"),
        )
        parent = _entry(
            "1.1.11", file_path="docs/rdr/rdr-105-main.md",
            source_uri=_source_uri(tmp_path, "rdr-105-main.md"),
        )
        cat = _FakeCat([child, parent], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 1
        p = w.proposed[0]
        assert (p.from_tumbler, p.to_tumbler, p.link_type) == ("1.1.10", "1.1.11", "relates")

    def test_related_rdrs_creates_one_edge_per_reference(self, tmp_path: Path) -> None:
        _write_rdr(
            tmp_path, "rdr-108-a.md",
            'title: "RDR-108"\nrelated_rdrs: [RDR-053, RDR-106]',
        )
        _write_rdr(tmp_path, "rdr-053-b.md", 'title: "RDR-053"')
        _write_rdr(tmp_path, "rdr-106-c.md", 'title: "RDR-106"')
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-108-a.md",
            source_uri=_source_uri(tmp_path, "rdr-108-a.md"),
        )
        t1 = _entry(
            "1.1.11", file_path="docs/rdr/rdr-053-b.md",
            source_uri=_source_uri(tmp_path, "rdr-053-b.md"),
        )
        t2 = _entry(
            "1.1.12", file_path="docs/rdr/rdr-106-c.md",
            source_uri=_source_uri(tmp_path, "rdr-106-c.md"),
        )
        cat = _FakeCat([source, t1, t2], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 2
        pairs = {(p.from_tumbler, p.to_tumbler, p.link_type) for p in w.proposed}
        assert pairs == {
            ("1.1.10", "1.1.11", "relates"),
            ("1.1.10", "1.1.12", "relates"),
        }


class TestNumericCollisionDisambiguation:
    """RDR-152-shaped fixture: three files share the ``152`` numeric
    prefix. Only the file that self-declares ``id: RDR-152`` is the valid
    resolution target; the OTHER same-prefix files (companion notes,
    themselves carrying a DIFFERENT ``id:``) are distinct documents, never
    silently picked."""

    def _write_152_trio(self, tmp_path: Path) -> None:
        _write_rdr(
            tmp_path, "rdr-152-note.md",
            'title: "RDR-152 note"\nid: companion-note\nparent_rdr: RDR-152',
        )
        _write_rdr(
            tmp_path, "rdr-152-other.md",
            'title: "RDR-152 other companion"\nrelates: [RDR-152]',  # no id: field at all
        )
        _write_rdr(
            tmp_path, "rdr-152-main.md",
            'title: "RDR-152 main"\nid: RDR-152',
        )

    def test_self_declared_id_wins_the_collision(self, tmp_path: Path) -> None:
        self._write_152_trio(tmp_path)
        note = _entry(
            "1.1.10", file_path="docs/rdr/rdr-152-note.md",
            source_uri=_source_uri(tmp_path, "rdr-152-note.md"),
        )
        other = _entry(
            "1.1.11", file_path="docs/rdr/rdr-152-other.md",
            source_uri=_source_uri(tmp_path, "rdr-152-other.md"),
        )
        main = _entry(
            "1.1.12", file_path="docs/rdr/rdr-152-main.md",
            source_uri=_source_uri(tmp_path, "rdr-152-main.md"),
        )
        cat = _FakeCat([note, other, main], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 1
        p = w.proposed[0]
        # note (1.1.10) -> main (1.1.12), never -> other (1.1.11)
        assert (p.from_tumbler, p.to_tumbler, p.link_type) == ("1.1.10", "1.1.12", "relates")

    def test_no_self_declaration_is_unresolvable_and_warns(self, tmp_path: Path) -> None:
        """Neither same-prefix candidate declares ``id: RDR-152`` — the
        collision cannot be broken; no edge, and a warning names both the
        source and the ambiguous target number."""
        _write_rdr(
            tmp_path, "rdr-152-note.md",
            'title: "RDR-152 note"\nid: companion-note\nparent_rdr: RDR-152',
        )
        _write_rdr(
            tmp_path, "rdr-152-other.md",
            'title: "RDR-152 other"',  # no id field either -- collision unbroken
        )
        note = _entry(
            "1.1.10", file_path="docs/rdr/rdr-152-note.md",
            source_uri=_source_uri(tmp_path, "rdr-152-note.md"),
        )
        other = _entry(
            "1.1.11", file_path="docs/rdr/rdr-152-other.md",
            source_uri=_source_uri(tmp_path, "rdr-152-other.md"),
        )
        cat = _FakeCat([note, other], repo_root=tmp_path)
        w = DryRunLinkWriter()
        with structlog.testing.capture_logs() as logs:
            count = generate_rdr_dependency_links(
                cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
            )
        assert count == 0
        assert w.proposed == []
        warnings = [
            entry for entry in logs
            if entry.get("event") == "rdr_dependency_target_unresolved"
        ]
        assert len(warnings) == 1
        assert warnings[0]["target_number"] == 152

    def test_target_number_with_zero_candidates_warns_and_creates_no_edge(
        self, tmp_path: Path,
    ) -> None:
        _write_rdr(
            tmp_path, "rdr-999-a.md",
            'title: "RDR-999"\nsupersedes: RDR-888',
        )
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-999-a.md",
            source_uri=_source_uri(tmp_path, "rdr-999-a.md"),
        )
        cat = _FakeCat([source], repo_root=tmp_path)
        w = DryRunLinkWriter()
        with structlog.testing.capture_logs() as logs:
            count = generate_rdr_dependency_links(
                cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
            )
        assert count == 0
        warnings = [
            entry for entry in logs
            if entry.get("event") == "rdr_dependency_target_unresolved"
        ]
        assert len(warnings) == 1
        assert warnings[0]["target_number"] == 888


class TestIncrementalModeAndIdempotency:
    def test_empty_new_tumblers_returns_zero_without_fetching(self, tmp_path: Path) -> None:
        source = _entry("1.1.10", file_path="docs/rdr/rdr-015-a.md")
        cat = _FakeCat([source], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
            new_tumblers=[],
        )
        assert count == 0

    def test_no_qualifying_new_content_type_returns_zero(self, tmp_path: Path) -> None:
        _write_rdr(tmp_path, "rdr-015-a.md", 'title: "RDR-015"\nsupersedes: RDR-014')
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-015-a.md",
            source_uri=_source_uri(tmp_path, "rdr-015-a.md"),
        )
        cat = _FakeCat([source], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
            new_tumblers=[Tumbler.parse("1.1.999")],
            new_content_types=frozenset({"code"}),
        )
        assert count == 0

    def test_idempotent_rerun_against_the_same_existing_keys_creates_zero(
        self, tmp_path: Path,
    ) -> None:
        _write_rdr(tmp_path, "rdr-015-a.md", 'title: "RDR-015"\nsupersedes: RDR-014')
        _write_rdr(tmp_path, "rdr-014-b.md", 'title: "RDR-014"')
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-015-a.md",
            source_uri=_source_uri(tmp_path, "rdr-015-a.md"),
        )
        target = _entry(
            "1.1.11", file_path="docs/rdr/rdr-014-b.md",
            source_uri=_source_uri(tmp_path, "rdr-014-b.md"),
        )
        cat = _FakeCat([source, target], repo_root=tmp_path)
        existing = {("1.1.10", "1.1.11", "supersedes")}
        w = DryRunLinkWriter(existing=existing)
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 0
        assert w.proposed == []


class TestSelfReferenceGuard:
    def test_a_document_referencing_its_own_number_creates_no_edge(
        self, tmp_path: Path,
    ) -> None:
        _write_rdr(
            tmp_path, "rdr-050-a.md",
            'title: "RDR-050"\nid: RDR-050\nrelated_rdrs: [RDR-050]',
        )
        source = _entry(
            "1.1.10", file_path="docs/rdr/rdr-050-a.md",
            source_uri=_source_uri(tmp_path, "rdr-050-a.md"),
        )
        cat = _FakeCat([source], repo_root=tmp_path)
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX,
        )
        assert count == 0
        assert w.proposed == []


class TestRealRepoNonVacuityFloor:
    """RDR-201 § Test Plan: 'an index of docs/rdr produces at least the
    ``supersedes`` edges the frontmatter already declares (non-vacuity
    floor)'.

    CORRECTION (measured 2026-09-02, nexus-j9z30.21): the RDR/bead text
    states this floor as 23 -- but 23 is the number of files that carry a
    ``supersedes:`` KEY, not the number of edges it produces. Direct
    enumeration of this repo's real ``docs/rdr/`` tree (23 files carry the
    field; most have empty ``[]`` values) finds exactly 7 non-empty raw
    values across those 23 files, one of which (``docs/proposals/
    m3docrag-application.md``) is not an RDR reference at all. The other
    6 (RDR-014, RDR-107, RDR-112, RDR-123, RDR-124, RDR-159) are real,
    resolvable, non-colliding RDR-to-RDR references and are exactly what
    this test measures against the actual tree via a real filesystem
    catalog fixture built from ``docs/rdr/*.md``. A run producing zero
    is still the failure case this floor exists to catch; 23 is not
    achievable from the ``supersedes`` field alone and asserting it would
    make this test permanently red for a reason that has nothing to do
    with the generator's correctness.
    """

    def _build_real_repo_cat(self, repo_root: Path) -> tuple[_FakeCat, list[CatalogEntry]]:
        rdr_dir = repo_root / "docs" / "rdr"
        entries: list[CatalogEntry] = []
        for i, f in enumerate(sorted(rdr_dir.glob("*.md"))):
            if not f.name.lower().startswith("rdr-"):
                continue  # mirrors rdr_key_of's own admission rule
            entries.append(
                _entry(
                    f"1.1.{20000 + i}",
                    file_path=f"docs/rdr/{f.name}",
                    source_uri=f"file://{f.resolve()}",
                )
            )
        return _FakeCat(entries, repo_root=repo_root), entries

    def test_real_tree_produces_at_least_six_supersedes_edges(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        assert (repo_root / "docs" / "rdr").is_dir(), "expected to run inside the nexus checkout"
        cat, _entries = self._build_real_repo_cat(repo_root)
        prefix = f"file://{(repo_root / 'docs' / 'rdr').resolve()}/"
        w = DryRunLinkWriter()
        count = generate_rdr_dependency_links(
            cat, writer=w, current_owner=CURRENT_OWNER, repo_source_prefix=prefix,
        )
        by_type = w.count_by_link_type()
        assert by_type.get("supersedes", 0) >= 6, (
            f"non-vacuity floor: expected >= 6 supersedes edges from the real "
            f"docs/rdr tree, got {by_type}"
        )
        assert count > 0
