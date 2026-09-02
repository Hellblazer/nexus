# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the RDR canonical-tumbler resolution rule (RDR-201 Phase 3.1,
bead nexus-j9z30.20).

Pure-logic coverage: :mod:`nexus.catalog.rdr_canonical` operates entirely
on already-fetched ``CatalogEntry`` objects and a ``Tumbler`` — it never
touches the wire itself (fetching is the caller's job, e.g. via
``CatalogReader.all_documents``). No engine substrate is needed to exercise
the rule; these tests build a small fixture "catalog" as a plain list of
``CatalogEntry`` objects, matching the Finding-4 fragmentation shape
(the same on-disk RDR registered under multiple owners, and/or under both
the legacy ``prose`` and current ``rdr`` content types) plus the
cross-repo collision shape found live in this repo's own catalog during
the nexus-j9z30.20 fix round (a same-basename RDR document registered
under an entirely different repo/owner, e.g. ``/Users/hal.hildebrand/git/ART``).
"""
from __future__ import annotations

import structlog.testing

from nexus.catalog.rdr_canonical import (
    UNRESOLVABLE_EVENT,
    current_rdr_owner,
    group_rdr_candidates,
    rdr_key_of,
    rdr_source_prefix,
    resolve_all,
    resolve_canonical_tumbler,
)
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry

CURRENT_OWNER = Tumbler.parse("1.1")
OTHER_OWNER_A = Tumbler.parse("1.10")
OTHER_OWNER_B = Tumbler.parse("1.20")
FOREIGN_OWNER = Tumbler.parse("1.2")  # e.g. the live ART registration's owner

# This repo's own docs/rdr/ tree, and a foreign repo's, for source_uri scoping.
THIS_REPO_PREFIX = "file:///repo/nexus/docs/rdr/"
FOREIGN_REPO_PREFIX = "file:///repo/ART/docs/rdr/"
# A nested agent-worktree copy -- inside the repo root's own path, but NOT
# under THIS_REPO_PREFIX (a different docs/rdr/ directory entirely).
WORKTREE_NESTED_PREFIX = "file:///repo/nexus/.claude/worktrees/agent-x/docs/rdr/"


def _entry(
    tumbler: str,
    *,
    content_type: str = "rdr",
    file_path: str = "docs/rdr/rdr-201-closed-vocabularies-as-checked-tables.md",
    title: str = "",
    source_uri: str = "",
) -> CatalogEntry:
    """Minimal CatalogEntry fixture — only the fields this rule reads vary."""
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
        indexed_at="2026-09-01T00:00:00Z",
        source_uri=source_uri,
    )


class TestRdrKeyOf:
    def test_extracts_basename_from_file_path(self) -> None:
        e = _entry("1.1.5", file_path="docs/rdr/rdr-201-closed-vocabularies.md")
        assert rdr_key_of(e) == "rdr-201-closed-vocabularies"

    def test_extracts_basename_from_absolute_file_path(self) -> None:
        e = _entry(
            "1.1.5",
            file_path="/Users/hal/git/nexus/docs/rdr/rdr-058-pipeline-orchestration.md",
        )
        assert rdr_key_of(e) == "rdr-058-pipeline-orchestration"

    def test_extracts_from_title_when_no_file_path(self) -> None:
        """Deliberately narrow fallback (code-review 2026-09-01, item 3): only
        reached when file_path is empty. Kept rather than dropped because a
        handful of legacy entries genuinely carry no file_path -- refusing
        to key them at all would silently drop them from every report."""
        e = _entry("1.1.5", file_path="", title="RDR-052: something")
        assert rdr_key_of(e) == "rdr-052"

    def test_title_fallback_not_reached_when_file_path_present_but_unrecognisable(self) -> None:
        """The title fallback is file_path-empty ONLY -- a present-but-wrong-shaped
        file_path returns None directly, even if the title WOULD have matched."""
        e = _entry("1.1.5", file_path="docs/architecture.md", title="RDR-052: something")
        assert rdr_key_of(e) is None

    def test_none_when_no_pattern_present(self) -> None:
        e = _entry("1.1.5", file_path="docs/architecture.md", title="Architecture")
        assert rdr_key_of(e) is None

    def test_none_for_non_rdr_source_file(self) -> None:
        e = _entry("1.1.5", content_type="code", file_path="src/nexus/catalog/rdr_canonical.py")
        assert rdr_key_of(e) is None

    def test_postmortem_companion_is_not_the_same_key_as_the_rdr(self) -> None:
        """docs/rdr/post-mortem/ is a DIFFERENT subdirectory -- its files are
        distinct catalog documents (post-mortems), not duplicate
        registrations of the RDR they discuss, even though they share the
        RDR's numeric prefix in their own filename. Real-data regression
        (found while computing the live dry-run plan for this bead):
        rdr-191-unify-chunk-tables-enable-manifest-fk.md (the RDR) and
        post-mortem/rdr-191-unify-chunk-tables-manifest-fk.md (its
        post-mortem) were colliding under a loose 'rdr-NNN' key."""
        rdr = _entry("1.1.1", file_path="docs/rdr/rdr-191-unify-chunk-tables-enable-manifest-fk.md")
        postmortem = _entry(
            "1.1.2",
            file_path="docs/rdr/post-mortem/rdr-191-unify-chunk-tables-manifest-fk.md",
        )
        assert rdr_key_of(rdr) == "rdr-191-unify-chunk-tables-enable-manifest-fk"
        assert rdr_key_of(postmortem) is None  # not directly under rdr/

    def test_phase_artifact_siblings_are_not_the_same_key_as_the_rdr(self) -> None:
        """rdr-200-phase1-prereg.md etc. live directly in docs/rdr/ (same
        directory as the real RDR-200 document) and share the RDR-200
        numeric prefix, but are separate catalog documents with a
        DIFFERENT basename -- basename identity keeps them apart."""
        rdr = _entry("1.1.1", file_path="docs/rdr/rdr-200-nx-answer-continuation-mode.md")
        prereg = _entry("1.1.2", file_path="docs/rdr/rdr-200-phase1-prereg.md")
        assert rdr_key_of(rdr) != rdr_key_of(prereg)


class TestRdrSourcePrefix:
    def test_anchored_on_docs_rdr_not_bare_repo_root(self, tmp_path) -> None:
        repo = tmp_path / "nexus"
        repo.mkdir()
        prefix = rdr_source_prefix(repo)
        assert prefix == f"file://{repo.resolve()}/docs/rdr/"

    def test_worktree_nested_copy_does_not_match(self, tmp_path) -> None:
        """A nested agent-worktree checkout sits INSIDE the repo root's own
        path but is a DIFFERENT docs/rdr/ -- a bare-root prefix would wrongly
        match it; the docs/rdr/-anchored prefix must not."""
        repo = tmp_path / "nexus"
        repo.mkdir()
        prefix = rdr_source_prefix(repo)
        worktree_source_uri = f"file://{repo}/.claude/worktrees/agent-x/docs/rdr/rdr-143-x.md"
        assert not worktree_source_uri.startswith(prefix)
        real_source_uri = f"file://{repo}/docs/rdr/rdr-143-x.md"
        assert real_source_uri.startswith(prefix)


class TestGroupRdrCandidates:
    def test_groups_by_rdr_key_rdr_and_prose_only(self) -> None:
        entries = [
            _entry("1.1.1", content_type="rdr", file_path="docs/rdr/rdr-201-x.md"),
            _entry("1.10.9", content_type="prose", file_path="docs/rdr/rdr-201-x.md"),
            _entry("1.1.2", content_type="code", file_path="src/nexus/catalog/rdr_canonical.py"),
            _entry("1.1.3", content_type="rdr", file_path="docs/rdr/rdr-052-y.md"),
        ]
        groups = group_rdr_candidates(entries)
        assert set(groups) == {"rdr-201-x", "rdr-052-y"}
        assert len(groups["rdr-201-x"]) == 2
        assert len(groups["rdr-052-y"]) == 1

    def test_entries_with_no_rdr_key_are_excluded(self) -> None:
        entries = [_entry("1.1.1", content_type="rdr", file_path="docs/architecture.md")]
        assert group_rdr_candidates(entries) == {}

    def test_postmortem_and_phase_artifacts_never_merge_with_the_rdr_group(self) -> None:
        entries = [
            _entry("1.1.1", file_path="docs/rdr/rdr-200-nx-answer-continuation-mode.md"),
            _entry("1.1.2", file_path="docs/rdr/rdr-200-phase1-prereg.md"),
            _entry("1.1.3", file_path="docs/rdr/rdr-200-phase1-gate-result.md"),
            _entry("1.1.4", file_path="docs/rdr/post-mortem/rdr-200-nx-answer-continuation-mode.md"),
        ]
        groups = group_rdr_candidates(entries)
        # the post-mortem entry has no rdr_key (excluded entirely); the two
        # phase artifacts each get their OWN singleton group, distinct from
        # the real RDR-200 document's group.
        assert set(groups) == {
            "rdr-200-nx-answer-continuation-mode",
            "rdr-200-phase1-prereg",
            "rdr-200-phase1-gate-result",
        }
        assert all(len(v) == 1 for v in groups.values())

    def test_grouping_itself_is_not_repo_scoped(self) -> None:
        """group_rdr_candidates groups purely by basename identity -- a
        same-basename cross-repo collision lands in the SAME group here;
        repo scoping is resolve_canonical_tumbler's job, exercised below."""
        this_repo = _entry(
            "1.1.1", file_path="docs/rdr/rdr-005-x.md", source_uri=THIS_REPO_PREFIX + "rdr-005-x.md",
        )
        foreign_repo = _entry(
            "1.2.1", file_path="docs/rdr/rdr-005-x.md", source_uri=FOREIGN_REPO_PREFIX + "rdr-005-x.md",
        )
        groups = group_rdr_candidates([this_repo, foreign_repo])
        assert len(groups["rdr-005-x"]) == 2


class TestResolveCanonicalTumbler:
    def test_duplicate_owner_registrations_resolve_to_current_owner(self) -> None:
        """Finding 4: one RDR registered twice under two owner ids, both
        genuinely this repo's file (same source_uri under this repo's
        docs/rdr/) -- the current owner's copy wins."""
        candidates = [
            _entry("1.10.42", content_type="rdr", source_uri=THIS_REPO_PREFIX + "rdr-x.md"),
            _entry("1.1.99", content_type="rdr", source_uri=THIS_REPO_PREFIX + "rdr-x.md"),  # current owner
        ]
        resolved = resolve_canonical_tumbler(candidates, CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert resolved == Tumbler.parse("1.1.99")

    def test_prose_and_rdr_resolves_to_the_rdr_registration(self) -> None:
        """Finding 4: stale legacy prose registration beside the current rdr
        one -- the rdr registration wins even though it is not itself under
        the current owner, because it is admitted via source_uri."""
        prose = _entry("1.10.7", content_type="prose", source_uri=THIS_REPO_PREFIX + "rdr-x.md")
        rdr = _entry("1.20.7", content_type="rdr", source_uri=THIS_REPO_PREFIX + "rdr-x.md")  # NOT current owner
        resolved = resolve_canonical_tumbler([prose, rdr], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert resolved == rdr.tumbler

    def test_single_candidate_under_repo_source_uri_resolves(self) -> None:
        """A lone legacy registration (never reindexed, wrong owner) that
        source_uri nonetheless confirms is THIS repo's own file resolves."""
        lone = _entry("1.10.3", content_type="prose", source_uri=THIS_REPO_PREFIX + "rdr-x.md")
        resolved = resolve_canonical_tumbler([lone], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert resolved == lone.tumbler

    def test_single_candidate_from_a_different_repo_is_unresolvable(self) -> None:
        """CRITICAL fix (nexus-j9z30.20 fix round): a lone candidate that is
        NEITHER under the current owner NOR under this repo's source_uri
        prefix must be UNRESOLVABLE, never silently accepted just because
        it is the only candidate found. Live-measured shape:
        /Users/hal.hildebrand/git/ART is registered content_type="rdr"
        under a DIFFERENT owner in the SAME catalog, and could share a
        basename with one of this repo's own RDRs."""
        art_entry = _entry(
            "1.2.400",
            content_type="rdr",
            file_path="docs/rdr/rdr-005-shared-basename.md",
            source_uri=FOREIGN_REPO_PREFIX + "rdr-005-shared-basename.md",
        )
        with structlog.testing.capture_logs() as logs:
            resolved = resolve_canonical_tumbler(
                [art_entry], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX, rdr_key="rdr-005-shared-basename",
            )
        assert resolved is None
        warnings = [e for e in logs if e["event"] == UNRESOLVABLE_EVENT]
        assert len(warnings) == 1
        assert warnings[0]["rdr_key"] == "rdr-005-shared-basename"
        assert warnings[0]["candidates"] == ["1.2.400"]
        assert warnings[0]["plausible_count"] == 0

    def test_worktree_nested_singleton_is_unresolvable_when_it_is_the_only_candidate(self) -> None:
        """A stale agent-worktree-nested registration is not admitted by
        source_uri (WORKTREE_NESTED_PREFIX != THIS_REPO_PREFIX) and, if it
        somehow ends up the only candidate for a key, must not resolve."""
        stale = _entry(
            "1.10.4143", content_type="prose", source_uri=WORKTREE_NESTED_PREFIX + "rdr-143-x.md",
        )
        resolved = resolve_canonical_tumbler([stale], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert resolved is None

    def test_unresolvable_pair_warns_naming_both_candidates_and_returns_none(self) -> None:
        candidates = [
            _entry("1.10.5", content_type="rdr", source_uri=THIS_REPO_PREFIX + "rdr-x.md"),
            _entry("1.20.5", content_type="rdr", source_uri=THIS_REPO_PREFIX + "rdr-x.md"),
        ]
        with structlog.testing.capture_logs() as logs:
            resolved = resolve_canonical_tumbler(
                candidates, CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX, rdr_key="rdr-201",
            )
        assert resolved is None
        warnings = [e for e in logs if e["event"] == UNRESOLVABLE_EVENT]
        assert len(warnings) == 1
        event = warnings[0]
        assert event["rdr_key"] == "rdr-201"
        assert set(event["candidates"]) == {"1.10.5", "1.20.5"}

    def test_no_candidates_returns_none_without_warning(self) -> None:
        with structlog.testing.capture_logs() as logs:
            resolved = resolve_canonical_tumbler([], CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert resolved is None
        assert logs == []

    def test_tie_at_owner_step_is_unresolvable(self) -> None:
        """Two rdr registrations BOTH under the current owner (should not
        happen given the (owner, source_uri) uniqueness the engine enforces,
        but the rule must not guess if it ever does)."""
        candidates = [
            _entry("1.1.5", content_type="rdr", source_uri=THIS_REPO_PREFIX + "rdr-x.md"),
            _entry("1.1.6", content_type="rdr", source_uri=THIS_REPO_PREFIX + "rdr-x.md"),
        ]
        with structlog.testing.capture_logs() as logs:
            resolved = resolve_canonical_tumbler(candidates, CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert resolved is None
        assert any(e["event"] == UNRESOLVABLE_EVENT for e in logs)

    def test_empty_repo_source_prefix_degrades_to_owner_only_matching(self) -> None:
        """repo_source_prefix="" (a caller with no repo root to scope
        against) never means accept-everything -- it just removes the
        source_uri admission signal, leaving owner-match as the only one."""
        lone_wrong_owner = _entry("1.10.3", content_type="prose", source_uri=THIS_REPO_PREFIX + "rdr-x.md")
        resolved = resolve_canonical_tumbler([lone_wrong_owner], CURRENT_OWNER, repo_source_prefix="")
        assert resolved is None

        lone_current_owner = _entry("1.1.3", content_type="prose", source_uri="")
        resolved2 = resolve_canonical_tumbler([lone_current_owner], CURRENT_OWNER, repo_source_prefix="")
        assert resolved2 == lone_current_owner.tumbler


class TestResolveAll:
    def test_resolves_every_group_independently(self) -> None:
        entries = [
            _entry("1.10.1", content_type="rdr", file_path="docs/rdr/rdr-201-a.md", source_uri=THIS_REPO_PREFIX + "rdr-201-a.md"),
            _entry("1.1.1", content_type="rdr", file_path="docs/rdr/rdr-201-a.md", source_uri=THIS_REPO_PREFIX + "rdr-201-a.md"),
            _entry("1.20.2", content_type="rdr", file_path="docs/rdr/rdr-052-b.md", source_uri=THIS_REPO_PREFIX + "rdr-052-b.md"),
            _entry("1.30.2", content_type="rdr", file_path="docs/rdr/rdr-052-b.md", source_uri=THIS_REPO_PREFIX + "rdr-052-b.md"),
        ]
        with structlog.testing.capture_logs() as logs:
            result = resolve_all(entries, CURRENT_OWNER, repo_source_prefix=THIS_REPO_PREFIX)
        assert result["rdr-201-a"] == Tumbler.parse("1.1.1")
        assert result["rdr-052-b"] is None
        assert any(e["event"] == UNRESOLVABLE_EVENT and e["rdr_key"] == "rdr-052-b" for e in logs)


class _StubReader:
    """Minimal CatalogReader stub -- only owner_for_repo is exercised."""

    def __init__(self, owner_by_repo_hash: dict[str, Tumbler]) -> None:
        self._by_hash = owner_by_repo_hash

    def owner_for_repo(self, repo_hash: str) -> Tumbler | None:
        return self._by_hash.get(repo_hash)


class TestCurrentRdrOwner:
    def test_resolves_via_repo_hash_lookup(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity",
            lambda repo: ("nexus", "deadbeef"),
        )
        reader = _StubReader({"deadbeef": CURRENT_OWNER})
        assert current_rdr_owner(reader, tmp_path) == CURRENT_OWNER

    def test_none_when_repo_has_no_owner_yet(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr(
            "nexus.repo_identity._repo_identity",
            lambda repo: ("nexus", "deadbeef"),
        )
        reader = _StubReader({})
        assert current_rdr_owner(reader, tmp_path) is None
