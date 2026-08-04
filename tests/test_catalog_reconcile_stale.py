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

    def collection_doc_counts(self):
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
        "total_docs": 0,
        "vanished_empty_manifest": [],
        "vanished_has_manifest": [],
        "zero_count_recount": [],
        "zero_count_rdr145_exempt": [],
        "zero_count_reindex_candidate": [],
        "zero_count_orphaned_path": [],
        "zero_count_unresolvable_provenance": [],
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

    def test_post_sdp0u_store_put_doc_classifies_unresolvable_not_exempt(self, tmp_path):
        """nexus-sdp0u fix-round (round-1 critique SIGNIFICANT #4): a
        post-fix ``store_put`` document carries a synthesized
        ``chroma://<collection>/<title>`` ``source_uri`` — so a zero-count
        one of these can no longer satisfy ``_classify_never_chunked``'s
        ``rdr145_exempt`` predicate (which requires BOTH file_path and
        source_uri empty). This pins the DELIBERATE resulting
        classification: ``unresolvable_provenance``/``source_uri_only``,
        not ``rdr145_exempt`` — a post-fix store_put doc still at
        chunk_count==0 is anomalous (manifests write synchronously at put
        time; failures surface loudly) and should be investigated, not
        waved through as legitimate. Converts the silent coupling this
        critique flagged into a pinned contract."""
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
        unresolvable = report["zero_count_unresolvable_provenance"]
        assert {r["tumbler"] for r in unresolvable} == {"5.1.1"}
        assert unresolvable[0]["reason"] == "source_uri_only"

    def test_dishonest_never_appears_in_any_action_list(self, tmp_path):
        cat, t3 = _mixed_cat(tmp_path)
        report, _ = reconcile_stale_mod._classify(cat, t3)

        dishonest_tumblers = {r["tumbler"] for r in report["dishonest"]}
        action_lists = (
            report["vanished_empty_manifest"] + report["zero_count_recount"]
            + report["zero_count_orphaned_path"]
        )
        assert dishonest_tumblers.isdisjoint({r["tumbler"] for r in action_lists})

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
        assert {r["tumbler"] for r in data["zero_count_live"]["unresolvable_provenance"]} == {
            "1.50.1", "42", "1.12.20", "1.12.21",
        }
        assert data["zero_count_live"]["unresolvable_provenance_by_reason"] == {
            "no_repo_root": 1, "malformed_tumbler": 1, "source_uri_only": 1, "no_provenance": 1,
        }

        assert len(data["dishonest"]) == 1
        assert data["dishonest"][0]["tumbler"] == "1.13.1"
        assert data["dishonest"][0]["chunk_count"] == 7

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
