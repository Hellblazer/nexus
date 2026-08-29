# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx catalog reconcile-stale`` (nexus-cdypx).

61.2% of production catalog docs carry ``chunk_count == 0``; 7,142 of
those point at 29 T3 collections that no longer exist (nexus-wq1e4).
This verb classifies the stale population into actionable sub-classes
and, behind a dry-run/--confirm gate, offers three narrow mutation arms.

All tests here use fakes (``_FakeCat`` / ``_FakeT3`` / ``_FakeWriter``)
monkeypatched onto ``nexus.commands.catalog`` — no real catalog / engine
substrate, per the design memo (T1 scratch 80500d58, LOCKED). Live
mutating verbs are never exercised against anything real.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands.catalog_cmds import reconcile_stale as reconcile_stale_mod


# ── Fakes ────────────────────────────────────────────────────────────────


class _FakeEntry:
    """Minimal CatalogEntry stand-in (mirrors test_catalog_cli.py's _FakeEntry)."""

    def __init__(
        self, tumbler, title, *, physical_collection, chunk_count=0,
        alias_of="", file_path="", source_uri="",
    ):
        self.tumbler = tumbler
        self.title = title
        self.physical_collection = physical_collection
        self.chunk_count = chunk_count
        self.alias_of = alias_of
        self.file_path = file_path
        self.source_uri = source_uri


class _FakeCat:
    def __init__(self, entries, *, doc_counts=None, owners_with_roots=None, manifests=None):
        self._entries = entries
        self._doc_counts = doc_counts or {}
        self._owners = owners_with_roots or {}
        self._manifests = manifests or {}

    def all_documents(self, limit=0):
        return list(self._entries)

    def stats(self):
        # The engine's catalog_stats.doc_count: every live row, aliases and
        # collection-less rows included (nexus-cwhci anchor).
        return {"doc_count": len(self._entries)}

    def collection_doc_counts(self, *, include_deleted=False):
        # nexus-8tnz2 fix-round: no tombstone population is modeled by this
        # fake (every fixture in this file is live-only) -- include_deleted
        # returns the SAME dict, which keeps every zero-live-doc row
        # classifying as "orphan" (never "tombstoned-only") unchanged.
        return dict(self._doc_counts)

    def owners_with_roots(self):
        return dict(self._owners)

    def get_manifests(self, doc_ids):
        return {d: self._manifests[d] for d in doc_ids if d in self._manifests}


class _FakeT3:
    def __init__(self, collections):
        self._collections = set(collections)

    def list_collections(self):
        return [{"name": n} for n in sorted(self._collections)]


class _FakeWriter:
    def __init__(self):
        self.resynced: list[str] = []
        self.deleted: list = []
        self.closed = False

    def resync_chunk_count_cache(self, doc_id):
        self.resynced.append(doc_id)

    def delete_many(self, tumblers):
        self.deleted.extend(tumblers)
        return list(tumblers)

    def close(self):
        self.closed = True


class _FailingWriter(_FakeWriter):
    """Recount writer that raises for a chosen subset of tumblers."""

    def __init__(self, fail_tumblers):
        super().__init__()
        self._fail = set(fail_tumblers)

    def resync_chunk_count_cache(self, doc_id):
        if doc_id in self._fail:
            raise RuntimeError(f"boom {doc_id}")
        super().resync_chunk_count_cache(doc_id)


def _writer_factory_raises():
    def _boom():
        raise AssertionError("catalog writer factory must not be called")
    return _boom


def _blank_report(**overrides) -> dict:
    """Full-shape ``_classify`` return value, for tests that bypass classification."""
    base = {
        "total_docs": 0, "substrate_anchor": {"status": "ok", "substrate_doc_count": 0, "walked_docs": 0, "delta": 0, "reason": None},
        "vanished_empty_manifest": [],
        "vanished_has_manifest": [],
        "zero_count_recount": [],
        "zero_count_rdr145_exempt": [],
        "zero_count_reindex_candidate": [],
        "zero_count_zero_content_by_design": [],
        "zero_count_orphaned_path": [],
        "zero_count_unresolvable_provenance": [],
        "zero_count_store_put_origin": [],
        "dishonest": [],
    }
    base.update(overrides)
    return base


# ── Mixed fixture covering every class/sub-class ──────────────────────────


def _mixed_entries(real_rel, missing_rel, gone_root_rel, real_abs, worktree_missing_abs):
    return [
        # vanished_collection / empty_manifest
        _FakeEntry("1.10.1", "Gone A", physical_collection="code__gone_a", chunk_count=0),
        # vanished_collection / has_manifest (diagnosis-only; dishonest-shaped: chunk_count > 0)
        _FakeEntry("1.10.2", "Gone B", physical_collection="code__gone_b", chunk_count=3),
        # zero_count_live / recount
        _FakeEntry("1.11.1", "Recount Me", physical_collection="knowledge__live", chunk_count=0),
        # zero_count_live / rdr145_exempt
        _FakeEntry("1.11.2", "Note Only", physical_collection="knowledge__notes", chunk_count=0),
        # zero_count_live / reindex_candidate (file_path)
        _FakeEntry(
            "1.12.1", "Reindex Me", physical_collection="code__live", chunk_count=0,
            file_path=real_rel,
        ),
        # zero_count_live / orphaned_path, reason=file_missing
        _FakeEntry(
            "1.12.2", "Orphaned", physical_collection="code__live", chunk_count=0,
            file_path=missing_rel,
        ),
        # zero_count_live / orphaned_path, reason=owner_root_gone
        _FakeEntry(
            "1.13.9", "Root Gone", physical_collection="code__live", chunk_count=0,
            file_path=gone_root_rel,
        ),
        # zero_count_live / orphaned_path, reason=file_missing, worktree-shaped path
        _FakeEntry(
            "1.12.30", "Worktree Gone", physical_collection="code__live", chunk_count=0,
            file_path=worktree_missing_abs,
        ),
        # zero_count_live / unresolvable_provenance, reason=no_repo_root
        _FakeEntry(
            "1.50.1", "No Root", physical_collection="code__live", chunk_count=0,
            file_path="src/unregistered.py",
        ),
        # zero_count_live / unresolvable_provenance, reason=malformed_tumbler
        _FakeEntry(
            "42", "Malformed", physical_collection="code__weird", chunk_count=0,
            file_path="src/whatever.py",
        ),
        # zero_count_live / unresolvable_provenance, reason=source_uri_only
        _FakeEntry(
            "1.12.20", "DEVONthink Only", physical_collection="code__devonthink", chunk_count=0,
            source_uri="x-devonthink-item://ABC123",
        ),
        # zero_count_live / unresolvable_provenance, reason=no_provenance
        _FakeEntry(
            "1.12.21", "No Provenance", physical_collection="code__mystery", chunk_count=0,
        ),
        # zero_count_live / reindex_candidate via file:// source_uri (RDR-096 P3.1)
        _FakeEntry(
            "1.12.22", "File URI Resolves", physical_collection="code__live", chunk_count=0,
            source_uri="file://" + real_abs,
        ),
        # dishonest
        _FakeEntry("1.13.1", "Dishonest", physical_collection="knowledge__live", chunk_count=7),
        # control: clean, fully-manifested doc — must never be classified
        _FakeEntry("1.14.1", "Clean", physical_collection="knowledge__live", chunk_count=2),
        # control: alias row — excluded entirely
        _FakeEntry(
            "1.15.1", "Alias", physical_collection="knowledge__live", chunk_count=0,
            alias_of="1.14.1",
        ),
        # control: empty physical_collection — excluded entirely
        _FakeEntry("1.16.1", "No collection", physical_collection="", chunk_count=0),
    ]


def _mixed_cat(tmp_path, *, owner_id="1.12"):
    real_rel = "src/real.py"
    missing_rel = "src/missing.py"
    gone_root_rel = "src/z.py"
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "real.py").write_text("# real\n")
    real_abs = str((tmp_path / "src" / "real.py").resolve())

    gone_root = tmp_path / "gone_root"  # deliberately never created

    entries = _mixed_entries(
        real_rel, missing_rel, gone_root_rel, real_abs,
        "/Users/hal/.claude/worktrees/wt1/src/gone.py",
    )
    doc_counts = {
        "code__gone_a": 1, "code__gone_b": 1,
        "knowledge__live": 3, "knowledge__notes": 1,
        "code__live": 6, "code__weird": 1, "code__devonthink": 1, "code__mystery": 1,
    }
    owners = {
        owner_id: str(tmp_path),
        "1.13": str(gone_root),
        # "1.50" deliberately absent -> no_repo_root
    }
    manifests = {
        "1.10.1": [],
        "1.10.2": [object()],
        "1.11.1": [object()],
        # everything else absent -> empty manifest
        "1.14.1": [object(), object()],
    }
    cat = _FakeCat(entries, doc_counts=doc_counts, owners_with_roots=owners, manifests=manifests)
    t3 = _FakeT3({
        "knowledge__live", "knowledge__notes", "code__live",
        "code__weird", "code__devonthink", "code__mystery",
    })
    return cat, t3


def _patch(monkeypatch, cat, t3, writer=None):
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: cat)
    monkeypatch.setattr("nexus.commands.catalog._make_t3", lambda: t3)
    if writer is not None:
        monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)


# ── Classification ─────────────────────────────────────────────────────────


class TestClassification:
    def test_classify_all_classes(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        report, unreadable = reconcile_stale_mod._classify(cat, t3)

        assert unreadable == []
        assert report["total_docs"] == 15  # excludes alias + no-collection rows

        assert {r["tumbler"] for r in report["vanished_empty_manifest"]} == {"1.10.1"}
        assert {r["tumbler"] for r in report["vanished_has_manifest"]} == {"1.10.2"}
        assert {r["tumbler"] for r in report["zero_count_recount"]} == {"1.11.1"}
        assert {r["tumbler"] for r in report["zero_count_rdr145_exempt"]} == {"1.11.2"}
        assert {r["tumbler"] for r in report["zero_count_reindex_candidate"]} == {"1.12.1", "1.12.22"}
        assert {r["tumbler"] for r in report["zero_count_orphaned_path"]} == {"1.12.2", "1.13.9", "1.12.30"}
        assert {r["tumbler"] for r in report["zero_count_unresolvable_provenance"]} == {
            "1.50.1", "42", "1.12.20", "1.12.21",
        }
        assert {r["tumbler"] for r in report["dishonest"]} == {"1.13.1"}
        # nexus-0y0gk: dishonest rows now carry an `origin` (same
        # provenance split as zero_count_*). "1.13.1" has neither
        # file_path nor source_uri and its collection is knowledge__ ->
        # unresolvable_provenance / no_provenance, same as "1.12.21".
        dishonest_row = report["dishonest"][0]
        assert dishonest_row["origin"] == "unresolvable_provenance"
        assert dishonest_row["reason"] == "no_provenance"

        reindex_by_tumbler = {r["tumbler"]: r for r in report["zero_count_reindex_candidate"]}
        assert reindex_by_tumbler["1.12.1"]["resolved_path"].endswith("src/real.py")
        assert reindex_by_tumbler["1.12.22"]["resolved_path"].endswith("src/real.py")

        orphaned_by_tumbler = {r["tumbler"]: r for r in report["zero_count_orphaned_path"]}
        assert orphaned_by_tumbler["1.12.2"]["reason"] == "file_missing"
        assert orphaned_by_tumbler["1.13.9"]["reason"] == "owner_root_gone"
        assert orphaned_by_tumbler["1.12.30"]["reason"] == "file_missing"
        for row in orphaned_by_tumbler.values():
            assert "file_path" in row
            assert "resolved_path" in row

        unresolvable_by_tumbler = {r["tumbler"]: r for r in report["zero_count_unresolvable_provenance"]}
        assert unresolvable_by_tumbler["1.50.1"]["reason"] == "no_repo_root"
        assert unresolvable_by_tumbler["42"]["reason"] == "malformed_tumbler"
        assert unresolvable_by_tumbler["1.12.20"]["reason"] == "source_uri_only"
        assert unresolvable_by_tumbler["1.12.21"]["reason"] == "no_provenance"

        # Vanished rows carry chunk_count so a dishonest-shaped doc (chunk_count
        # > 0, empty manifest) that also sits in a vanished collection stays
        # visible as such, not just blended into the plain vanished bucket.
        vanished_has_row = next(r for r in report["vanished_has_manifest"] if r["tumbler"] == "1.10.2")
        assert vanished_has_row["chunk_count"] == 3

    def test_per_collection_breakdown(self, tmp_path):
        cat, t3 = _mixed_cat(tmp_path)
        report, _ = reconcile_stale_mod._classify(cat, t3)

        vanished_breakdown = reconcile_stale_mod._breakdown_by_collection(
            report["vanished_empty_manifest"] + report["vanished_has_manifest"]
        )
        assert vanished_breakdown == [
            {"collection": "code__gone_a", "count": 1},
            {"collection": "code__gone_b", "count": 1},
        ]

        zc_breakdown = reconcile_stale_mod._breakdown_by_collection(
            report["zero_count_recount"] + report["zero_count_rdr145_exempt"]
            + report["zero_count_reindex_candidate"] + report["zero_count_orphaned_path"]
        )
        assert zc_breakdown == [
            {"collection": "code__live", "count": 5},
            {"collection": "knowledge__live", "count": 1},
            {"collection": "knowledge__notes", "count": 1},
        ]

    def test_rdr145_exempt_predicate_requires_knowledge_prefix_and_empty_paths(self):
        exempt = _FakeEntry(
            "2.1.1", "Note", physical_collection="knowledge__x", chunk_count=0,
        )
        not_exempt_wrong_prefix = _FakeEntry(
            "2.1.2", "Code", physical_collection="code__x", chunk_count=0,
        )
        not_exempt_has_path = _FakeEntry(
            "2.1.3", "Note w/ path", physical_collection="knowledge__x", chunk_count=0,
            file_path="notes/x.md",
        )
        assert reconcile_stale_mod._classify_never_chunked(exempt) == "rdr145_exempt"
        assert reconcile_stale_mod._classify_never_chunked(not_exempt_wrong_prefix) == "unclassified"
        assert reconcile_stale_mod._classify_never_chunked(not_exempt_has_path) == "unclassified"

    def test_store_put_signature_reason_predicate(self):
        """nexus-0y0gk critique fix-round: unit-level pin of
        ``_store_put_signature_reason`` in isolation, independent of the
        full classification pipeline."""
        chroma = _FakeEntry(
            "3.1.1", "Chroma", physical_collection="knowledge__x", chunk_count=9,
            source_uri="chroma://knowledge__x/Chroma",
        )
        assert reconcile_stale_mod._store_put_signature_reason(chroma) == "chroma_uri"

        single_chunk = _FakeEntry(
            "3.1.2", "Single Chunk", physical_collection="knowledge__x", chunk_count=1,
        )
        assert (
            reconcile_stale_mod._store_put_signature_reason(single_chunk)
            == "knowledge_single_chunk_no_path"
        )

        # file_path always wins -- never silently reclassified, even when
        # the rest of the signature (knowledge__, chunk_count==1) matches.
        file_backed = _FakeEntry(
            "3.1.3", "File Backed", physical_collection="knowledge__x", chunk_count=1,
            file_path="notes/x.md",
        )
        assert reconcile_stale_mod._store_put_signature_reason(file_backed) is None

        # chunk_count != 1 with no file_path/source_uri: not the
        # single-chunk-by-construction signature.
        wrong_chunk_count = _FakeEntry(
            "3.1.4", "Wrong Count", physical_collection="knowledge__x", chunk_count=3,
        )
        assert reconcile_stale_mod._store_put_signature_reason(wrong_chunk_count) is None

        # code__ collection: the single-chunk-no-path signature is
        # knowledge__-scoped only.
        wrong_collection = _FakeEntry(
            "3.1.5", "Wrong Collection", physical_collection="code__x", chunk_count=1,
        )
        assert reconcile_stale_mod._store_put_signature_reason(wrong_collection) is None

        # non-chroma:// source_uri: not the store_put signature.
        other_uri = _FakeEntry(
            "3.1.6", "DEVONthink", physical_collection="knowledge__x", chunk_count=1,
            source_uri="x-devonthink-item://ABC",
        )
        assert reconcile_stale_mod._store_put_signature_reason(other_uri) is None

    def test_post_sdp0u_store_put_doc_classifies_store_put_origin_not_exempt(self, tmp_path):
        """nexus-sdp0u fix-round (round-1 critique SIGNIFICANT #4), UPDATED
        by the nexus-0y0gk critique fix-round: a post-fix ``store_put``
        document carries a synthesized ``chroma://<collection>/<title>``
        ``source_uri`` — so a zero-count one of these can no longer satisfy
        ``_classify_never_chunked``'s ``rdr145_exempt`` predicate (which
        requires BOTH file_path and source_uri empty). This pins the
        DELIBERATE resulting classification: ``store_put_origin``/
        ``chroma_uri`` — recognizable-but-anomalous (manifests write
        synchronously at put time; failures surface loudly, so a post-fix
        store_put doc still at chunk_count==0 IS worth investigating), but
        no longer collapsed into the generic ``unresolvable_provenance``
        catch-all a genuinely-unknown-provenance doc lands in (0y0gk:
        lumping a recognizable signature with truly-dead docs made the
        3n7pr triage require a hand SQL query instead of being mechanical).
        Converts the silent coupling this critique flagged into a pinned
        contract."""
        entry = _FakeEntry(
            "5.1.1", "Post-Fix Note", physical_collection="knowledge__sdp0u",
            chunk_count=0,
            source_uri="chroma://knowledge__sdp0u/Post-Fix Note",
        )
        assert reconcile_stale_mod._classify_never_chunked(entry) == "unclassified"

        cat = _FakeCat([entry], doc_counts={"knowledge__sdp0u": 1})
        t3 = _FakeT3({"knowledge__sdp0u"})
        report, unreadable = reconcile_stale_mod._classify(cat, t3)

        assert unreadable == []
        assert report["zero_count_rdr145_exempt"] == []
        assert report["zero_count_unresolvable_provenance"] == []
        store_put_origin = report["zero_count_store_put_origin"]
        assert {r["tumbler"] for r in store_put_origin} == {"5.1.1"}
        assert store_put_origin[0]["reason"] == "chroma_uri"

    def test_dishonest_never_appears_in_any_action_list(self, tmp_path):
        cat, t3 = _mixed_cat(tmp_path)
        report, _ = reconcile_stale_mod._classify(cat, t3)

        dishonest_tumblers = {r["tumbler"] for r in report["dishonest"]}
        action_lists = (
            report["vanished_empty_manifest"] + report["zero_count_recount"]
            + report["zero_count_orphaned_path"]
        )
        assert dishonest_tumblers.isdisjoint({r["tumbler"] for r in action_lists})

    def test_dishonest_bucket_origin_covers_all_four_provenance_outcomes(self, tmp_path):
        """nexus-0y0gk (critique fix-round): the 3n7pr triage splits the
        dishonest population (chunk_count > 0, empty manifest) by origin --
        file-backed re-index vs. store_put-origin (FK-safe backfill) vs.
        can't-tell-at-all, in that priority order. Exercise all FOUR
        ``_resolve_dishonest_origin`` outcomes on dishonest rows
        specifically, mirroring the zero_count_* coverage above. Critique
        finding: 4 of the 5 live dishonest docs at fix time carried the
        store_put signature and were being lumped into
        unresolvable_provenance -- this pins that they no longer are."""
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "present.py").write_text("# present\n")

        entries = [
            # dishonest / reindex_candidate: file_path resolves and exists
            _FakeEntry(
                "9.1.1", "Dishonest Reindexable", physical_collection="code__d",
                chunk_count=4, file_path="src/present.py",
            ),
            # dishonest / orphaned_path (file_missing): file_path resolves,
            # confirmed absent
            _FakeEntry(
                "9.1.2", "Dishonest Orphaned", physical_collection="code__d",
                chunk_count=2, file_path="src/absent.py",
            ),
            # dishonest / unresolvable_provenance: no file_path, no
            # source_uri, non-knowledge__ collection -> no_provenance
            _FakeEntry(
                "9.1.3", "Dishonest Unresolvable", physical_collection="code__d",
                chunk_count=1,
            ),
            # dishonest / store_put_origin (chroma_uri): the nexus-sdp0u
            # synthesized source_uri signature, regardless of chunk_count.
            _FakeEntry(
                "9.1.4", "Dishonest Store-Put Chroma", physical_collection="knowledge__d",
                chunk_count=5, source_uri="chroma://knowledge__d/Dishonest Store-Put Chroma",
            ),
            # dishonest / store_put_origin (knowledge_single_chunk_no_path):
            # chunk_count==1, knowledge__*, neither file_path nor source_uri.
            _FakeEntry(
                "9.1.5", "Dishonest Store-Put Single Chunk", physical_collection="knowledge__d",
                chunk_count=1,
            ),
            # NOT store_put_origin, despite chunk_count==1 + knowledge__:
            # a real file_path always wins as reindex_candidate ("do not
            # silently move file-backed docs").
            _FakeEntry(
                "9.1.6", "Dishonest File-Backed Single Chunk", physical_collection="knowledge__d",
                chunk_count=1, file_path="src/present.py",
            ),
        ]
        doc_counts = {"code__d": 3, "knowledge__d": 3}
        owners = {"9.1": str(tmp_path)}
        cat = _FakeCat(entries, doc_counts=doc_counts, owners_with_roots=owners)
        t3 = _FakeT3({"code__d", "knowledge__d"})

        report, unreadable = reconcile_stale_mod._classify(cat, t3)

        assert unreadable == []
        dishonest_by_tumbler = {r["tumbler"]: r for r in report["dishonest"]}
        assert set(dishonest_by_tumbler) == {"9.1.1", "9.1.2", "9.1.3", "9.1.4", "9.1.5", "9.1.6"}

        reindex_row = dishonest_by_tumbler["9.1.1"]
        assert reindex_row["origin"] == "reindex_candidate"
        assert reindex_row["resolved_path"].endswith("src/present.py")
        assert "reason" not in reindex_row

        orphaned_row = dishonest_by_tumbler["9.1.2"]
        assert orphaned_row["origin"] == "orphaned_path"
        assert orphaned_row["reason"] == "file_missing"
        assert orphaned_row["file_path"] == "src/absent.py"

        unresolvable_row = dishonest_by_tumbler["9.1.3"]
        assert unresolvable_row["origin"] == "unresolvable_provenance"
        assert unresolvable_row["reason"] == "no_provenance"

        chroma_row = dishonest_by_tumbler["9.1.4"]
        assert chroma_row["origin"] == "store_put_origin"
        assert chroma_row["reason"] == "chroma_uri"

        single_chunk_row = dishonest_by_tumbler["9.1.5"]
        assert single_chunk_row["origin"] == "store_put_origin"
        assert single_chunk_row["reason"] == "knowledge_single_chunk_no_path"

        file_backed_row = dishonest_by_tumbler["9.1.6"]
        assert file_backed_row["origin"] == "reindex_candidate"
        assert file_backed_row["resolved_path"].endswith("src/present.py")

    def test_unresolvable_provenance_never_appears_in_any_action_list(self, tmp_path):
        cat, t3 = _mixed_cat(tmp_path)
        report, _ = reconcile_stale_mod._classify(cat, t3)

        unresolvable_tumblers = {r["tumbler"] for r in report["zero_count_unresolvable_provenance"]}
        action_lists = (
            report["vanished_empty_manifest"] + report["zero_count_recount"]
            + report["zero_count_orphaned_path"]
        )
        assert unresolvable_tumblers.isdisjoint({r["tumbler"] for r in action_lists})

    def test_nexus_6ims_resolution_uses_owner_root_not_cwd(self, tmp_path, monkeypatch):
        """Running from an unrelated cwd must not change classification."""
        cat, t3 = _mixed_cat(tmp_path)
        elsewhere = tmp_path.parent / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        monkeypatch.chdir(elsewhere)

        report, _ = reconcile_stale_mod._classify(cat, t3)

        assert {r["tumbler"] for r in report["zero_count_reindex_candidate"]} == {"1.12.1", "1.12.22"}
        assert {r["tumbler"] for r in report["zero_count_orphaned_path"]} == {"1.12.2", "1.13.9", "1.12.30"}
        assert {r["tumbler"] for r in report["zero_count_unresolvable_provenance"]} == {
            "1.50.1", "42", "1.12.20", "1.12.21",
        }

    def test_incomplete_guard_empty_t3_listing_populated_catalog(self, monkeypatch):
        entries = [_FakeEntry("3.1.1", "A", physical_collection="knowledge__x", chunk_count=1)]
        cat = _FakeCat(entries, doc_counts={"knowledge__x": 1})
        t3 = _FakeT3(set())  # empty listing

        report, unreadable = reconcile_stale_mod._classify(cat, t3)

        assert unreadable != []
        assert report["vanished_empty_manifest"] == []
        assert report["vanished_has_manifest"] == []

    def test_incomplete_guard_raises_click_exception_via_cli(self, tmp_path, monkeypatch):
        entries = [_FakeEntry("3.1.1", "A", physical_collection="knowledge__x", chunk_count=1)]
        cat = _FakeCat(entries, doc_counts={"knowledge__x": 1})
        t3 = _FakeT3(set())
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale"])

        assert result.exit_code != 0
        assert "INCOMPLETE" in result.output


# ── Zero-content-by-design classification (nexus-rqsh1) ───────────────────
#
# A gapped/never-chunked doc whose SOURCE is verifiably unchunkable (a
# zero-byte file, or binary content per classifier.looks_like_binary_content)
# must classify as zero_content_by_design, NOT reindex_candidate --
# re-indexing it can never produce a chunk, so it would otherwise reappear
# in every census forever (the nexus repo owner 1-1 evidence: 11 of its 12
# "gapped" docs -- 9 empty __init__.py files, a .bundle, a .npz fixture).


class TestZeroContentByDesign:
    def _cat(self, tmp_path, *, owner_id="1.60"):
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "__init__.py").write_bytes(b"")
        (tmp_path / "src" / "fixture.npz").write_bytes(
            b"\x93NUMPY\x01\x00v\x00{'descr':'<f8'}\xff\xfe\xfd\x00\x01\x02not valid utf-8"
        )
        (tmp_path / "src" / "real.py").write_text("# real code\n")

        entries = [
            # zero-byte source -> zero_content_by_design
            _FakeEntry(
                "1.60.1", "Empty init", physical_collection="code__zc", chunk_count=0,
                file_path="src/__init__.py",
            ),
            # binary source -> zero_content_by_design
            _FakeEntry(
                "1.60.2", "Binary fixture", physical_collection="code__zc", chunk_count=0,
                file_path="src/fixture.npz",
            ),
            # control: a real, chunkable text file -- must NOT move buckets
            _FakeEntry(
                "1.60.3", "Real file", physical_collection="code__zc", chunk_count=0,
                file_path="src/real.py",
            ),
        ]
        doc_counts = {"code__zc": 3}
        owners = {owner_id: str(tmp_path)}
        cat = _FakeCat(entries, doc_counts=doc_counts, owners_with_roots=owners)
        t3 = _FakeT3({"code__zc"})
        return cat, t3

    def test_zero_byte_and_binary_classify_zero_content_by_design(self, tmp_path):
        cat, t3 = self._cat(tmp_path)
        report, unreadable = reconcile_stale_mod._classify(cat, t3)

        assert unreadable == []
        assert {r["tumbler"] for r in report["zero_count_zero_content_by_design"]} == {
            "1.60.1", "1.60.2",
        }
        # control: the real file is untouched -- still reindex_candidate,
        # never moved into the new bucket.
        assert {r["tumbler"] for r in report["zero_count_reindex_candidate"]} == {"1.60.3"}

    def test_zero_content_rows_carry_resolved_path_and_manifest_len_zero(self, tmp_path):
        cat, t3 = self._cat(tmp_path)
        report, _ = reconcile_stale_mod._classify(cat, t3)
        by_tumbler = {r["tumbler"]: r for r in report["zero_count_zero_content_by_design"]}
        assert by_tumbler["1.60.1"]["resolved_path"].endswith("src/__init__.py")
        assert by_tumbler["1.60.2"]["resolved_path"].endswith("src/fixture.npz")
        assert by_tumbler["1.60.1"]["manifest_len"] == 0
        assert by_tumbler["1.60.2"]["manifest_len"] == 0

    def test_is_zero_content_by_design_unit(self, tmp_path):
        """Direct pin of the shared classifier.looks_like_binary_content
        reuse (integrity.py) -- zero-byte, binary, and normal text."""
        empty = tmp_path / "empty.py"
        empty.write_bytes(b"")
        binary = tmp_path / "data.npz"
        binary.write_bytes(b"\x93NUMPY\x01\x00\xff\xfe\xfd\x00binary-not-utf8")
        text = tmp_path / "real.py"
        text.write_text("# hello world\n")
        missing = tmp_path / "does_not_exist.py"

        from nexus.commands.catalog_cmds.integrity import _is_zero_content_by_design

        assert _is_zero_content_by_design(empty) is True
        assert _is_zero_content_by_design(binary) is True
        assert _is_zero_content_by_design(text) is False
        # a missing path is a different (orphaned) population -- never
        # guessed into zero_content_by_design.
        assert _is_zero_content_by_design(missing) is False

    def test_json_shape_includes_zero_content_by_design(self, tmp_path, monkeypatch):
        cat, t3 = self._cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert data["summary"]["zero_count_zero_content_by_design"] == 2
        assert {r["tumbler"] for r in data["zero_count_live"]["zero_content_by_design"]} == {
            "1.60.1", "1.60.2",
        }
        # non-vacuity: these docs must NOT vanish from the census -- they
        # are still counted in total_docs, just under a different, honest
        # bucket (killing the nexus-cotmr round-1 exemption error).
        assert data["summary"]["total_docs"] == 3

    def test_human_report_honest_wording_not_a_suppression(self, tmp_path, monkeypatch):
        cat, t3 = self._cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale"])

        assert result.exit_code == 0, result.output
        lower = result.output.lower()
        assert "zero-content-by-design" in lower
        assert "will never chunk" in lower
        assert "tombstone" in lower
        assert "2 zero-content-by-design" in result.output

    def test_tombstone_zero_content_dry_run_default_never_constructs_writer(
        self, tmp_path, monkeypatch,
    ):
        cat, t3 = self._cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "reconcile-stale", "--execute", "tombstone-zero-content"],
        )

        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()

    def test_tombstone_zero_content_confirmed_deletes_exact_set(self, tmp_path, monkeypatch):
        cat, t3 = self._cat(tmp_path)
        writer = _FakeWriter()
        _patch(monkeypatch, cat, t3, writer=writer)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "catalog", "reconcile-stale",
                "--execute", "tombstone-zero-content", "--no-dry-run", "--confirm",
            ],
        )

        assert result.exit_code == 0, result.output
        deleted_strs = {str(t) for t in writer.deleted}
        assert deleted_strs == {"1.60.1", "1.60.2"}
        assert "1.60.3" not in deleted_strs  # legit reindex candidate never tombstoned
        assert writer.closed


# ── Worktree/tempdir classifier (platform-independence pin) ──────────────


class TestIsWorktreeOrTempdirPath:
    """Pure string-based pins for ``_is_worktree_or_tempdir_path``.

    Deliberately touches NO filesystem and uses only literal strings, so
    this is the mechanism in this file that is guaranteed
    platform-independent: it never depends on wherever pytest's own
    ``tmp_path`` fixture happens to physically live (system temp under
    ``/tmp`` by default on Linux CI vs ``/private/var/folders/...`` on
    macOS — see ``_TEMP_DIR_PREFIXES``'s own comment in
    ``reconcile_stale.py``). ``TestJsonOutput`` and ``TestHumanReport``
    below derive their expected worktree/tempdir *counts* from calling
    this same production function against the fixture's actual
    ``resolved_path`` values rather than hardcoding a platform-dependent
    literal count; this class is what pins the function's own behavior
    (the previously-red assertions were pinning a number that silently
    depended on where the CI runner's ``/tmp`` happened to place
    ``tmp_path`` — 1 on macOS, 3 on Linux — nexus-cdypx CI fix wave).
    """

    def test_worktree_marker_matches(self):
        assert reconcile_stale_mod._is_worktree_or_tempdir_path(
            "/Users/hal/.claude/worktrees/wt1/src/gone.py"
        )

    def test_linux_system_tmp_matches(self):
        assert reconcile_stale_mod._is_worktree_or_tempdir_path(
            "/tmp/pytest-of-runner/pytest-0/test_foo0/src/missing.py"
        )

    def test_private_tmp_matches(self):
        assert reconcile_stale_mod._is_worktree_or_tempdir_path(
            "/private/tmp/scratch/src/missing.py"
        )

    def test_macos_pytest_tmp_does_not_match(self):
        # Deliberately excluded (reconcile_stale.py's own comment):
        # /var/folders/ is macOS's broad per-user cache root and also
        # where pytest's tmp_path fixture lives there — too noisy a
        # signal to count as "confirmed-safe".
        assert not reconcile_stale_mod._is_worktree_or_tempdir_path(
            "/private/var/folders/57/xyz/T/pytest-of-hal/pytest-0/"
            "test_foo0/src/missing.py"
        )

    def test_ci_workspace_path_does_not_match(self):
        assert not reconcile_stale_mod._is_worktree_or_tempdir_path(
            "/home/runner/work/nexus/nexus/src/real.py"
        )

    def test_relative_path_does_not_match(self):
        assert not reconcile_stale_mod._is_worktree_or_tempdir_path("src/real.py")

    def test_empty_string_does_not_match(self):
        assert not reconcile_stale_mod._is_worktree_or_tempdir_path("")


# ── JSON shape ──────────────────────────────────────────────────────────


class TestJsonOutput:
    def test_json_shape(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)

        assert data["summary"]["total_docs"] == 15
        assert data["summary"]["vanished_empty_manifest"] == 1
        assert data["summary"]["vanished_has_manifest"] == 1
        assert data["summary"]["zero_count_recount"] == 1
        assert data["summary"]["zero_count_rdr145_exempt"] == 1
        assert data["summary"]["zero_count_reindex_candidate"] == 2
        assert data["summary"]["zero_count_orphaned_path"] == 3
        assert data["summary"]["zero_count_store_put_origin"] == 0
        assert data["summary"]["zero_count_unresolvable_provenance"] == 4
        assert data["summary"]["dishonest"] == 1

        assert data["vanished"]["empty_manifest"] == [{"collection": "code__gone_a", "count": 1}]
        assert data["vanished"]["has_manifest"] == [{"collection": "code__gone_b", "count": 1}]

        assert data["zero_count_live"]["recount"] == 1
        assert {r["tumbler"] for r in data["zero_count_live"]["recount_targets"]} == {"1.11.1"}
        assert data["zero_count_live"]["rdr145_exempt"] == 1
        assert {r["tumbler"] for r in data["zero_count_live"]["reindex_candidates"]} == {"1.12.1", "1.12.22"}
        assert {r["tumbler"] for r in data["zero_count_live"]["orphaned_path"]} == {
            "1.12.2", "1.13.9", "1.12.30",
        }
        assert data["zero_count_live"]["orphaned_path_by_reason"] == {
            "file_missing": 2, "owner_root_gone": 1,
        }
        # The worktree/tempdir signal depends on where pytest's own
        # tmp_path physically lives (system /tmp on Linux CI vs
        # /private/var/folders on macOS — TestIsWorktreeOrTempdirPath
        # above pins the classifier itself on literal, platform-
        # independent strings). Derive the expected count from the SAME
        # production function applied to this fixture's actual
        # resolved_path values rather than a platform-dependent literal.
        expected_worktree_count = sum(
            1 for r in data["zero_count_live"]["orphaned_path"]
            if reconcile_stale_mod._is_worktree_or_tempdir_path(r["resolved_path"])
        )
        # non-vacuity: the literal /.claude/worktrees/ row (1.12.30) is
        # never platform-dependent and always counts.
        assert expected_worktree_count >= 1
        assert data["zero_count_live"]["orphaned_path_worktree_count"] == expected_worktree_count
        assert data["zero_count_live"]["store_put_origin"] == []
        assert data["zero_count_live"]["store_put_origin_by_reason"] == {}
        assert {r["tumbler"] for r in data["zero_count_live"]["unresolvable_provenance"]} == {
            "1.50.1", "42", "1.12.20", "1.12.21",
        }
        assert data["zero_count_live"]["unresolvable_provenance_by_reason"] == {
            "no_repo_root": 1, "malformed_tumbler": 1, "source_uri_only": 1, "no_provenance": 1,
        }

        assert len(data["dishonest"]) == 1
        assert data["dishonest"][0]["tumbler"] == "1.13.1"
        assert data["dishonest"][0]["chunk_count"] == 7
        # nexus-0y0gk: dishonest rows carry an origin from the same
        # provenance split zero_count_* uses, plus a by-origin summary.
        assert data["dishonest"][0]["origin"] == "unresolvable_provenance"
        assert data["dishonest"][0]["reason"] == "no_provenance"
        assert data["dishonest_by_origin"] == {"unresolvable_provenance": 1}

        assert data["incomplete"] == []

    def test_json_and_execute_combined_is_refused(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "reconcile-stale", "--execute", "recount", "--json"],
        )

        assert result.exit_code != 0
        assert "--json" in result.output
        assert "--execute" in result.output


# ── Human report content ──────────────────────────────────────────────────


class TestHumanReport:
    def test_vanished_chunk_count_visible_in_human_report(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale"])

        assert result.exit_code == 0, result.output
        assert "chunk_count=3" in result.output

    def test_orphaned_path_worktree_signal_reported(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        # Derive the expected worktree/tempdir count from the same
        # production classifier applied to the fixture's actual
        # resolved_path values (see TestIsWorktreeOrTempdirPath and the
        # matching comment in TestJsonOutput.test_json_shape) — pytest's
        # own tmp_path lives under system /tmp on Linux CI but not on
        # macOS, so "1 of 3" is not a platform-independent literal.
        report, _ = reconcile_stale_mod._classify(cat, t3)
        zc_orphaned = report["zero_count_orphaned_path"]
        assert len(zc_orphaned) == 3
        expected_worktree_n = sum(
            1 for r in zc_orphaned
            if reconcile_stale_mod._is_worktree_or_tempdir_path(r.get("resolved_path", ""))
        )
        assert expected_worktree_n >= 1  # the literal /.claude/worktrees/ row always counts

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale"])

        assert result.exit_code == 0, result.output
        assert (
            f"{expected_worktree_n} of 3 orphaned-path row(s) are worktree/temp-dir"
            in result.output
        )

    def test_unresolvable_provenance_reasons_reported(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale"])

        assert result.exit_code == 0, result.output
        assert "unresolvable provenance" in result.output.lower()
        assert "no_repo_root" in result.output
        assert "malformed_tumbler" in result.output
        assert "source_uri_only" in result.output
        assert "no_provenance" in result.output

    def test_dishonest_by_origin_reported(self, tmp_path, monkeypatch):
        """nexus-0y0gk: the dishonest section's human report shows a
        by-origin breakdown, mirroring the by-reason lines the
        zero_count_* sections already print."""
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale"])

        assert result.exit_code == 0, result.output
        assert "by origin: unresolvable_provenance=1" in result.output
        assert "origin=unresolvable_provenance" in result.output


# ── Gates ───────────────────────────────────────────────────────────────


class TestGates:
    def test_report_mode_never_constructs_writer(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale"])

        assert result.exit_code == 0, result.output

    def test_execute_dry_run_default_never_constructs_writer(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--execute", "recount"])

        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()

    def test_no_dry_run_without_confirm_is_report_only(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        _patch(monkeypatch, cat, t3, writer=_writer_factory_raises())

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "reconcile-stale", "--execute", "recount", "--no-dry-run"],
        )

        assert result.exit_code == 0, result.output
        assert "--confirm" in result.output

    def test_execute_recount_confirmed_resyncs_exact_set(self, tmp_path, monkeypatch):
        cat, t3 = _mixed_cat(tmp_path)
        writer = _FakeWriter()
        _patch(monkeypatch, cat, t3, writer=writer)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["catalog", "reconcile-stale", "--execute", "recount", "--no-dry-run", "--confirm"],
        )

        assert result.exit_code == 0, result.output
        assert writer.resynced == ["1.11.1"]
        assert writer.closed
        assert "nx catalog verify" in result.output

    def test_execute_tombstone_vanished_confirmed_deletes_exact_empty_manifest_set(
        self, tmp_path, monkeypatch,
    ):
        cat, t3 = _mixed_cat(tmp_path)
        writer = _FakeWriter()
        _patch(monkeypatch, cat, t3, writer=writer)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "catalog", "reconcile-stale",
                "--execute", "tombstone-vanished", "--no-dry-run", "--confirm",
            ],
        )

        assert result.exit_code == 0, result.output
        deleted_strs = {str(t) for t in writer.deleted}
        assert deleted_strs == {"1.10.1"}
        assert "1.10.2" not in deleted_strs
        assert "skipped" not in result.output.lower()  # no invariant violation to skip here

    def test_execute_tombstone_orphaned_confirmed_deletes_exact_orphaned_set(
        self, tmp_path, monkeypatch,
    ):
        cat, t3 = _mixed_cat(tmp_path)
        writer = _FakeWriter()
        _patch(monkeypatch, cat, t3, writer=writer)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "catalog", "reconcile-stale",
                "--execute", "tombstone-orphaned", "--no-dry-run", "--confirm",
            ],
        )

        assert result.exit_code == 0, result.output
        deleted_strs = {str(t) for t in writer.deleted}
        assert deleted_strs == {"1.12.2", "1.13.9", "1.12.30"}
        assert "1.12.1" not in deleted_strs  # reindex candidate never tombstoned
        assert "1.12.22" not in deleted_strs  # reindex candidate (via source_uri) never tombstoned
        # unresolvable_provenance rows must never reach the writer, no matter the reason.
        assert "1.50.1" not in deleted_strs
        assert "42" not in deleted_strs
        assert "1.12.20" not in deleted_strs
        assert "1.12.21" not in deleted_strs

    def test_assert_empty_manifest_skips_non_empty(self):
        targets = [
            {"tumbler": "a", "manifest_len": 0},
            {"tumbler": "b", "manifest_len": 2},
        ]
        ok, skipped = reconcile_stale_mod._assert_empty_manifest(targets)
        assert [r["tumbler"] for r in ok] == ["a"]
        assert [r["tumbler"] for r in skipped] == ["b"]


# ── Recount robustness (progress echo + per-doc failure isolation) ───────


class TestRecountRobustness:
    def _patch_bypassing_classify(self, monkeypatch, report, writer):
        monkeypatch.setattr(
            "nexus.commands.catalog_cmds.reconcile_stale._classify",
            lambda cat, t3: (report, []),
        )
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
        monkeypatch.setattr("nexus.commands.catalog._make_t3", lambda: object())
        monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)

    def test_recount_progress_echoed_every_100_docs(self, monkeypatch):
        targets = [
            {
                "tumbler": f"1.20.{i}", "title": "T", "physical_collection": "code__live",
                "manifest_len": 1, "chunk_count": 0,
            }
            for i in range(250)
        ]
        report = _blank_report(zero_count_recount=targets, total_docs=250)
        writer = _FakeWriter()
        self._patch_bypassing_classify(monkeypatch, report, writer)

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "reconcile-stale", "--execute", "recount", "--no-dry-run", "--confirm"],
        )

        assert result.exit_code == 0, result.output
        assert len(writer.resynced) == 250
        assert "100/250" in result.output
        assert "200/250" in result.output

    def test_recount_failure_isolated_continues_and_reports(self, monkeypatch):
        targets = [
            {
                "tumbler": f"1.20.{i}", "title": "T", "physical_collection": "code__live",
                "manifest_len": 1, "chunk_count": 0,
            }
            for i in (1, 2, 3)
        ]
        report = _blank_report(zero_count_recount=targets, total_docs=3)
        writer = _FailingWriter({"1.20.2"})
        self._patch_bypassing_classify(monkeypatch, report, writer)

        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "reconcile-stale", "--execute", "recount", "--no-dry-run", "--confirm"],
        )

        assert result.exit_code == 1
        assert writer.resynced == ["1.20.1", "1.20.3"]
        assert writer.closed
        assert "1 failure" in result.output
        assert "1.20.2" in result.output


# ── Registration ────────────────────────────────────────────────────────


def test_reconcile_stale_registered_under_catalog_group():
    catalog_group = main.commands["catalog"]
    assert "reconcile-stale" in catalog_group.commands
    assert catalog_group.commands["reconcile-stale"].callback is reconcile_stale_mod.reconcile_stale_cmd.callback


# ── nexus-cwhci: the substrate anchor (playbook §S4 non-vacuity) ──────────


class _AnchorCat(_FakeCat):
    """``doc_count`` may be an int (same count before and after the walk)
    or a list consumed one value per stats() call (before, after, ...)."""

    def __init__(self, entries, *, doc_count=None, raise_exc=None, **kw):
        super().__init__(entries, **kw)
        self._doc_count = doc_count
        self._raise = raise_exc
        self.stats_calls = 0

    def stats(self):
        self.stats_calls += 1
        if self._raise is not None:
            raise self._raise
        if isinstance(self._doc_count, list):
            return {"doc_count": self._doc_count[min(self.stats_calls - 1, len(self._doc_count) - 1)]}
        return {"doc_count": self._doc_count if self._doc_count is not None else len(self._entries)}


class _NoStatsCat(_FakeCat):
    stats = None  # a reader that cannot report a substrate count at all


class TestSubstrateAnchor:
    def _entries(self):
        return [
            _FakeEntry("1.60.1", "live", physical_collection="knowledge__live", chunk_count=3),
            _FakeEntry("1.60.2", "alias", physical_collection="knowledge__live", alias_of="1.60.1"),
            _FakeEntry("1.60.3", "ghost", physical_collection=""),
        ]

    def test_ok_when_the_engine_count_matches_the_walk(self, monkeypatch):
        runner = CliRunner()
        cat = _AnchorCat(self._entries(), doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale"])
        assert result.exit_code == 0, result.output
        assert "Substrate anchor: OK — the engine counts 3 live catalog document(s) and this walk saw 3" in result.output
        assert "1 alias, 1 without a collection excluded" in result.output
        assert "1 non-alias catalog document(s) examined" in result.output

    def test_json_carries_the_anchor(self, monkeypatch):
        runner = CliRunner()
        cat = _AnchorCat(self._entries(), doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["substrate_anchor"] == {
            "status": "ok", "substrate_doc_count": 3, "substrate_doc_count_before": 3,
            "substrate_doc_count_after": 3, "walked_docs": 3, "delta": 0, "reason": None,
            "moved_during_walk": False,
        }
        assert payload["walked_docs"] == 3 and payload["alias_docs"] == 1 and payload["no_collection_docs"] == 1

    def test_mismatch_is_incomplete_not_a_pass(self, monkeypatch):
        runner = CliRunner()
        # The engine holds 5 live rows; the walk only saw 3 — a probe failure.
        cat = _AnchorCat(self._entries(), doc_count=5, doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale"])
        assert result.exit_code != 0
        assert "Substrate anchor: MISMATCH — the engine counted 5 live catalog document(s) before the walk and 5 after it; this walk saw 3, outside that bracket (delta -2" in result.output
        assert "INCOMPLETE: substrate anchor MISMATCH" in result.output
        # The census is still printed above the refusal — the operator sees both.
        assert "non-alias catalog document(s) examined" in result.output

    def test_unavailable_anchor_is_incomplete(self, monkeypatch):
        runner = CliRunner()
        cat = _NoStatsCat(self._entries(), doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale"])
        assert result.exit_code != 0
        assert "Substrate anchor: UNAVAILABLE (catalog reader exposes no stats())" in result.output
        assert "INCOMPLETE: substrate anchor unavailable" in result.output

    def test_stats_error_is_unavailable_with_the_cause(self, monkeypatch):
        runner = CliRunner()
        cat = _AnchorCat(self._entries(), raise_exc=RuntimeError("engine 503"), doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--json"])
        assert result.exit_code != 0
        assert "RuntimeError: engine 503" in result.output


    def test_writes_landing_during_the_walk_are_corroborated_not_mismatched(self, monkeypatch):
        """A busy box indexes while the census walks: the engine counted 3
        before the walk and 4 after it, the walk saw 3 — inside the bracket,
        so the walk is corroborated (code-review-expert, 2026-08-28)."""
        runner = CliRunner()
        cat = _AnchorCat(self._entries(), doc_count=[3, 4], doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--json"])
        assert result.exit_code == 0, result.output
        anchor = json.loads(result.stdout)["substrate_anchor"]
        assert anchor["status"] == "ok" and anchor["moved_during_walk"] is True
        assert (anchor["substrate_doc_count_before"], anchor["substrate_doc_count_after"]) == (3, 4)
        assert cat.stats_calls == 2

    def test_walk_outside_both_brackets_is_a_mismatch(self, monkeypatch):
        runner = CliRunner()
        cat = _AnchorCat(self._entries(), doc_count=[6, 7], doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale"])
        assert result.exit_code != 0
        assert "counted 6 live catalog document(s) before the walk and 7 after it; this walk saw 3, outside that bracket" in result.output

    def test_mismatch_blocks_every_mutation_arm(self, monkeypatch):
        """The anchor guard runs before any --execute arm acts: a census the
        substrate does not corroborate must not be the basis of a delete."""
        runner = CliRunner()
        writer = _FakeWriter()
        cat = _AnchorCat(self._entries(), doc_count=9, doc_counts={"knowledge__live": 2}, manifests={"1.60.1": ["a" * 64]})
        _patch(monkeypatch, cat, _FakeT3({"knowledge__live"}), writer)
        result = runner.invoke(main, [
            "catalog", "reconcile-stale", "--execute", "tombstone-ghost-notes", "--no-dry-run", "--confirm",
        ])
        assert result.exit_code != 0
        assert "INCOMPLETE: substrate anchor MISMATCH" in result.output
        assert "the mutation arms refuse on it too" in result.output
        assert writer.deleted == [] and writer.resynced == [], "no catalog write may follow an uncorroborated census"


# ── nexus-41zr9: the §5.4 write-time guard census ─────────────────────────


class TestWriteTimeGuardCensus:
    def _cat(self):
        return _AnchorCat(
            [_FakeEntry("1.60.1", "live", physical_collection="knowledge__live", chunk_count=3)],
            doc_counts={"knowledge__live": 1}, manifests={"1.60.1": ["a" * 64]},
        )

    def test_every_mutation_arm_has_a_row(self):
        mod = reconcile_stale_mod
        assert set(mod._WRITE_TIME_GUARDS) == {
            "recount", "tombstone-vanished", "tombstone-orphaned",
            "tombstone-zero-content", "tombstone-ghost-notes",
            "drop-orphan-collections",
        }
        for verb, g in mod._WRITE_TIME_GUARDS.items():
            assert g["status"] in {"shipped", "shipped-with-residuals", "UNGUARDED", "n/a"}, verb
            assert g["guard"], verb

    @pytest.mark.parametrize("verb,expect", [
        ("recount", "UNGUARDED — the chunk_count desync writer (the wu8s1/94fxl class) is still unfound"),
        ("tombstone-vanished", "shipped-with-residuals — SCOPED, not root-cause: this repo's OWN tracked host-run harnesses"),
        ("tombstone-orphaned", "shipped-with-residuals — worktree/temp indexing refused at registration"),
        ("tombstone-zero-content", "shipped — unchunkable sources"),
        ("tombstone-ghost-notes", "shipped — store_put notes get a title-derived identity"),
        ("drop-orphan-collections", "shipped-with-residuals — SCOPED, not root-cause (same honesty note as tombstone-vanished above)"),
    ])
    def test_each_arm_prints_its_guard_before_acting(self, monkeypatch, verb, expect):
        runner = CliRunner()
        _patch(monkeypatch, self._cat(), _FakeT3({"knowledge__live"}), _FakeWriter())
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--execute", verb])  # dry-run default
        assert result.exit_code == 0, result.output
        assert "Write-time guard (playbook §5.4, as of 2026-08-28): " + expect in result.output
        if verb == "tombstone-orphaned":
            assert "residuals: nexus-rng8r" in result.output
            assert "yx75p" not in result.output  # the enqueue-side gap is not this arm's population
        if verb == "recount":
            assert "residuals: nexus-wu8s1" in result.output
        if verb in ("tombstone-vanished", "drop-orphan-collections"):
            assert "residuals: nexus-8tnz2" in result.output
        # the guard line precedes the arm's candidate report
        assert result.output.index("Write-time guard") < result.output.index(f"\n{verb}:")

    def test_tombstone_vanished_no_longer_carries_an_unowned_residual(self, monkeypatch):
        """nexus-8tnz2: the write-time guard (the scratch-scope lint) now
        exists, so this row's ``unowned_residual`` key -- "benchmark/gate
        debris collections need namespace isolation, not artifact gating,
        no bead names it" -- is REMOVED (the design of record's Task C).
        Flips the pre-nexus-8tnz2
        test_unowned_residual_is_named_on_every_run."""
        runner = CliRunner()
        _patch(monkeypatch, self._cat(), _FakeT3({"knowledge__live"}), _FakeWriter())
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--execute", "tombstone-vanished"])
        assert result.exit_code == 0, result.output
        assert "UNOWNED residual" not in result.output
        assert "nexus-8tnz2" in result.output

    def test_json_census_carries_the_table(self, monkeypatch):
        runner = CliRunner()
        _patch(monkeypatch, self._cat(), _FakeT3({"knowledge__live"}))
        result = runner.invoke(main, ["catalog", "reconcile-stale", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["write_time_guards_as_of"] == "2026-08-28"
        table = payload["write_time_guards"]
        assert table["tombstone-vanished"]["status"] == "shipped-with-residuals"
        assert table["tombstone-vanished"]["residual_beads"] == ["nexus-8tnz2"]
        assert "unowned_residual" not in table["tombstone-vanished"]
        assert table["drop-orphan-collections"]["status"] == "shipped-with-residuals"
        assert table["drop-orphan-collections"]["residual_beads"] == ["nexus-8tnz2"]
        assert table["tombstone-orphaned"]["residual_beads"] == ["nexus-rng8r"]
        assert table["recount"]["status"] == "UNGUARDED" and table["recount"]["residual_beads"] == ["nexus-wu8s1"]
