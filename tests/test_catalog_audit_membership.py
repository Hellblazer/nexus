# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nx catalog audit-membership`` (nexus-ow9f).

Detects cross-project source_uri contamination in a single
physical_collection — the ART-lhk1 pattern where 140 of 245 catalog
rows in ``rdr__ART-8c2e74c0`` had ``source_uri`` rooted in
``/Users/.../nexus/`` instead of the project's expected
``/Users/.../ART/`` root.

Coverage:

* Single-home collection → reports "no contamination" and exits 0.
* Multi-home collection → lists per-home counts, identifies dominant.
* ``--purge-non-canonical --dry-run`` → reports what would be deleted,
  writes nothing.
* ``--purge-non-canonical`` → actually deletes the non-canonical
  entries (with ``--yes`` to skip prompt).
* ``--json`` emits structured output.
* Empty collection → "no entries" exit 0.
* Unknown collection → "no entries" exit 0 (degenerate of empty case).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog import Catalog
from nexus.commands.catalog import catalog


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)

    import nexus.config
    monkeypatch.setattr(nexus.config, "catalog_path", lambda: catalog_dir)

    return cat


def _seed_contamination(cat: Catalog, *, collection: str = "rdr__myproject") -> None:
    """ART-lhk1 reproduction: register some entries with the project's
    own source_uri root + some with a different (contaminating) root."""
    owner = cat.register_owner("rdr", "rdr-curator")
    # 5 canonical entries (project's own root)
    for i in range(5):
        cat.register(
            owner, f"RDR-CANONICAL-{i}",
            content_type="rdr",
            physical_collection=collection,
            file_path=f"docs/rdr/RDR-CANONICAL-{i}.md",
            source_uri=f"file:///Users/test/projects/myproject/docs/rdr/RDR-CANONICAL-{i}.md",
        )
    # 3 contaminating entries (different repo)
    for i in range(3):
        cat.register(
            owner, f"RDR-LEAKED-{i}",
            content_type="rdr",
            physical_collection=collection,
            file_path=f"docs/rdr/RDR-LEAKED-{i}.md",
            source_uri=f"file:///Users/test/projects/elsewhere/docs/rdr/RDR-LEAKED-{i}.md",
        )


# ── single-home ─────────────────────────────────────────────────────────────


class TestSingleHome:
    def test_no_contamination_message(self, env: Catalog) -> None:
        owner = env.register_owner("rdr", "rdr-curator")
        for i in range(3):
            env.register(
                owner, f"RDR-{i}",
                content_type="rdr",
                physical_collection="rdr__clean",
                file_path=f"docs/rdr/RDR-{i}.md",
                source_uri=f"file:///Users/test/projects/clean/docs/rdr/RDR-{i}.md",
            )
        runner = CliRunner()
        result = runner.invoke(catalog, ["audit-membership", "rdr__clean"])
        assert result.exit_code == 0, result.output
        assert "no contamination" in result.output.lower()
        assert "3" in result.output


# ── multi-home detection ─────────────────────────────────────────────────────


class TestMultiHome:
    def test_reports_per_home_counts_and_dominant(self, env: Catalog) -> None:
        _seed_contamination(env)
        runner = CliRunner()
        result = runner.invoke(catalog, ["audit-membership", "rdr__myproject"])
        assert result.exit_code == 0, result.output
        # Both source_uri homes appear with their counts.
        assert "/Users/test/projects/myproject" in result.output
        assert "/Users/test/projects/elsewhere" in result.output
        assert "5" in result.output
        assert "3" in result.output
        # Dominant home is identified.
        assert "dominant" in result.output.lower()

    def test_json_output_structured(self, env: Catalog) -> None:
        _seed_contamination(env)
        runner = CliRunner()
        result = runner.invoke(
            catalog, ["audit-membership", "rdr__myproject", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["collection"] == "rdr__myproject"
        assert data["total_entries"] == 8
        assert data["distinct_homes"] == 2
        assert data["dominant_home"].endswith("/myproject")
        # by_home has both homes; counts sum to 8.
        by_home = data["by_home"]
        assert sum(by_home.values()) == 8
        assert any("myproject" in k for k in by_home)
        assert any("elsewhere" in k for k in by_home)


# ── purge ────────────────────────────────────────────────────────────────────


class TestPurge:
    def test_purge_dry_run_writes_nothing(self, env: Catalog) -> None:
        _seed_contamination(env)
        runner = CliRunner()
        result = runner.invoke(catalog, [
            "audit-membership", "rdr__myproject",
            "--purge-non-canonical", "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        # Nothing actually deleted.
        rows = env._db.execute(
            "SELECT COUNT(*) FROM documents WHERE physical_collection = ?",
            ("rdr__myproject",),
        ).fetchone()[0]
        assert rows == 8, "dry-run must not delete anything"
        assert "would delete" in result.output.lower() or "dry-run" in result.output.lower()

    def test_purge_actually_deletes_non_canonical(self, env: Catalog) -> None:
        _seed_contamination(env)
        runner = CliRunner()
        result = runner.invoke(catalog, [
            "audit-membership", "rdr__myproject",
            "--purge-non-canonical", "--yes",
        ])
        assert result.exit_code == 0, result.output
        # Only 5 canonical entries should remain (5 myproject, 3 elsewhere → 5).
        rows = env._db.execute(
            "SELECT COUNT(*) FROM documents WHERE physical_collection = ?",
            ("rdr__myproject",),
        ).fetchone()[0]
        assert rows == 5
        # And the surviving rows all point at the canonical home.
        homes = {
            r[0] for r in env._db.execute(
                "SELECT source_uri FROM documents WHERE physical_collection = ?",
                ("rdr__myproject",),
            ).fetchall()
        }
        for uri in homes:
            assert "/projects/myproject/" in uri, uri

    def test_canonical_home_override_when_contaminant_dominates(
        self, env: Catalog,
    ) -> None:
        """ART-lhk1 specifically: the contaminating entries (140 nexus
        URIs) outnumber the legitimate ones (105 ART URIs), so the
        numerical dominant is the WRONG canonical. --canonical-home
        substring lets the operator override and purge the contaminants
        even when they're the majority."""
        owner = env.register_owner("rdr", "rdr-curator")
        # 7 contaminating nexus entries, 3 legitimate ART entries
        for i in range(7):
            env.register(
                owner, f"RDR-LEAK-{i}",
                content_type="rdr",
                physical_collection="rdr__inverted",
                file_path=f"docs/rdr/RDR-LEAK-{i}.md",
                source_uri=f"file:///Users/test/projects/contaminant/docs/rdr/RDR-LEAK-{i}.md",
            )
        for i in range(3):
            env.register(
                owner, f"RDR-REAL-{i}",
                content_type="rdr",
                physical_collection="rdr__inverted",
                file_path=f"docs/rdr/RDR-REAL-{i}.md",
                source_uri=f"file:///Users/test/projects/legit/docs/rdr/RDR-REAL-{i}.md",
            )
        runner = CliRunner()
        # Without override: dominant=contaminant (wrong)
        result = runner.invoke(catalog, ["audit-membership", "rdr__inverted"])
        assert "contaminant [dominant" in result.output

        # With override: canonical = the substring-matching home
        result = runner.invoke(catalog, [
            "audit-membership", "rdr__inverted",
            "--canonical-home", "/projects/legit",
            "--purge-non-canonical", "--yes",
        ])
        assert result.exit_code == 0, result.output
        # Only the 3 legit entries should remain.
        rows = env._db.execute(
            "SELECT COUNT(*) FROM documents WHERE physical_collection = ?",
            ("rdr__inverted",),
        ).fetchone()[0]
        assert rows == 3
        # And the survivors all match the canonical substring.
        homes = {
            r[0] for r in env._db.execute(
                "SELECT source_uri FROM documents WHERE physical_collection = ?",
                ("rdr__inverted",),
            ).fetchall()
        }
        for uri in homes:
            assert "/projects/legit/" in uri

    def test_canonical_home_substring_no_match_errors(
        self, env: Catalog,
    ) -> None:
        _seed_contamination(env)
        runner = CliRunner()
        result = runner.invoke(catalog, [
            "audit-membership", "rdr__myproject",
            "--canonical-home", "/no-such-path",
        ])
        assert result.exit_code != 0
        assert "no home" in result.output.lower() or "matches no" in result.output.lower()

    def test_purge_non_canonical_requires_multi_home(
        self, env: Catalog,
    ) -> None:
        """When the collection has only one home, --purge-non-canonical
        is a no-op (nothing to purge). Clean exit, count zero."""
        owner = env.register_owner("rdr", "rdr-curator")
        env.register(
            owner, "RDR-1",
            content_type="rdr",
            physical_collection="rdr__clean",
            file_path="docs/rdr/RDR-1.md",
            source_uri="file:///Users/test/projects/clean/docs/rdr/RDR-1.md",
        )
        runner = CliRunner()
        result = runner.invoke(catalog, [
            "audit-membership", "rdr__clean",
            "--purge-non-canonical", "--yes",
        ])
        assert result.exit_code == 0, result.output
        rows = env._db.execute(
            "SELECT COUNT(*) FROM documents WHERE physical_collection = ?",
            ("rdr__clean",),
        ).fetchone()[0]
        assert rows == 1


# ── empty / unknown collection ──────────────────────────────────────────────


class TestEmpty:
    def test_empty_collection_exits_clean(self, env: Catalog) -> None:
        runner = CliRunner()
        result = runner.invoke(catalog, ["audit-membership", "rdr__nothing-here"])
        assert result.exit_code == 0, result.output
        assert "no entries" in result.output.lower() or "0" in result.output


# ── --all-collections sweep mode (nexus-3e4s Phase 3) ───────────────────────


class TestAllCollections:
    """Sweep audit across every physical_collection in one call.

    Useful as a daily / post-release health check to confirm the
    register-time guard is doing its job and no NEW contamination is
    accumulating across the catalog.
    """

    def _seed_two_collections(self, cat: Catalog) -> None:
        """One contaminated collection, one clean one."""
        _seed_contamination(cat, collection="rdr__contaminated")
        owner = cat.register_owner("clean", "clean-curator")
        for i in range(4):
            cat.register(
                owner, f"R{i}", content_type="rdr",
                physical_collection="rdr__clean",
                file_path=f"x/R{i}.md",
                source_uri=f"file:///Users/test/projects/clean/x/R{i}.md",
            )

    def test_sweep_lists_contaminated_first(self, env: Catalog) -> None:
        self._seed_two_collections(env)
        runner = CliRunner()
        result = runner.invoke(catalog, ["audit-membership", "--all-collections"])
        assert result.exit_code == 0, result.output
        # Contaminated collection appears before clean in the output.
        out = result.output
        assert "rdr__contaminated" in out
        assert "rdr__clean" in out
        assert out.index("rdr__contaminated") < out.index("rdr__clean")

    def test_sweep_json_emits_structured_report(self, env: Catalog) -> None:
        self._seed_two_collections(env)
        runner = CliRunner()
        result = runner.invoke(
            catalog, ["audit-membership", "--all-collections", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["total_collections"] == 2
        assert data["contaminated_count"] == 1
        assert data["clean_count"] == 1
        # Per-collection records are sorted by contamination desc.
        cols = data["collections"]
        assert cols[0]["collection"] == "rdr__contaminated"
        assert cols[0]["distinct_homes"] == 2
        assert cols[0]["contaminated_entries"] == 3
        assert cols[1]["collection"] == "rdr__clean"
        assert cols[1]["distinct_homes"] == 1
        assert cols[1]["contaminated_entries"] == 0

    def test_sweep_empty_catalog(self, env: Catalog) -> None:
        runner = CliRunner()
        result = runner.invoke(catalog, ["audit-membership", "--all-collections"])
        assert result.exit_code == 0, result.output
        assert "0" in result.output or "no collections" in result.output.lower()

    def test_sweep_rejects_purge_flag(self, env: Catalog) -> None:
        """Bulk purge across all collections is too risky — the
        canonical-home heuristic can be wrong per-collection. Refuse."""
        self._seed_two_collections(env)
        runner = CliRunner()
        result = runner.invoke(catalog, [
            "audit-membership", "--all-collections", "--purge-non-canonical",
        ])
        assert result.exit_code != 0
        assert "purge" in result.output.lower()

    def test_sweep_rejects_canonical_home_override(self, env: Catalog) -> None:
        """A canonical-home override is per-collection by definition."""
        self._seed_two_collections(env)
        runner = CliRunner()
        result = runner.invoke(catalog, [
            "audit-membership", "--all-collections",
            "--canonical-home", "/projects/myproject",
        ])
        assert result.exit_code != 0
        assert "canonical-home" in result.output.lower()

    def test_collection_arg_or_all_required(self, env: Catalog) -> None:
        """Neither COLLECTION nor --all-collections → usage error."""
        runner = CliRunner()
        result = runner.invoke(catalog, ["audit-membership"])
        assert result.exit_code != 0
        assert "collection" in result.output.lower()


class TestAllCollectionsOwnerAware:
    """nexus-3e4s critique-followup C2.

    Pre-fix the sweep used dominant-home as its only signal, so a
    collection where every row pointed at the wrong project (single
    home, no internal disagreement) read as "clean" — the failure
    mode that masked ~4,200 wrong-home rows in ``code__ART-...``.

    Post-fix the sweep cross-references the owning repo's
    ``repo_root``: a single-home collection whose dominant home does
    NOT match the owner's tree is flagged as 100% contaminated and
    tagged ``wrong_home=true`` in the JSON record.
    """

    def _seed_repo_owned_clean(self, cat: Catalog, tmp_path) -> str:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="repo0001",
            repo_root=str(repo),
        )
        for i in range(4):
            f = repo / f"x{i}.py"
            f.touch()
            cat.register(
                owner, f"x{i}", content_type="code",
                file_path=str(f),
                physical_collection="code__myrepo-repo0001",
            )
        return "code__myrepo-repo0001"

    def _seed_repo_owned_all_wrong_home(
        self, cat: Catalog, tmp_path, monkeypatch,
    ) -> str:
        """Every row has source_uri pointing at the WRONG project,
        attributed to the right repo owner. Single-home (all wrong),
        which the pre-fix sweep silently treated as clean.

        Uses the env override so the rows go through register() and
        appear in BOTH JSONL and SQLite (a direct SQL insert would be
        wiped by the catalog's consistency rebuild on the next read).
        """
        repo = tmp_path / "rightrepo"
        repo.mkdir()
        owner = cat.register_owner(
            "rightrepo", "repo", repo_hash="repo0002",
            repo_root=str(repo),
        )
        monkeypatch.setenv("NEXUS_CATALOG_ALLOW_CROSS_PROJECT", "1")
        for i in range(3):
            cat.register(
                owner, f"f{i}", content_type="code",
                file_path=f"f{i}.py",
                physical_collection="code__rightrepo-repo0002",
                source_uri=f"file:///wrong/project/f{i}.py",
            )
        monkeypatch.delenv("NEXUS_CATALOG_ALLOW_CROSS_PROJECT")
        return "code__rightrepo-repo0002"

    def test_sweep_flags_single_home_wrong_home_collection(
        self, env: Catalog, tmp_path, monkeypatch,
    ) -> None:
        wrong_col = self._seed_repo_owned_all_wrong_home(env, tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(
            catalog, ["audit-membership", "--all-collections", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        rec = next(r for r in data["collections"] if r["collection"] == wrong_col)
        assert rec["wrong_home"] is True
        assert rec["contaminated_entries"] == rec["total_entries"] == 3
        assert rec["expected_home"]
        # The collection should appear in the contaminated bucket, not
        # the clean one.
        assert data["contaminated_count"] >= 1

    def test_sweep_clean_when_dominant_matches_owner_repo_root(
        self, env: Catalog, tmp_path,
    ) -> None:
        clean_col = self._seed_repo_owned_clean(env, tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            catalog, ["audit-membership", "--all-collections", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        rec = next(r for r in data["collections"] if r["collection"] == clean_col)
        assert rec["wrong_home"] is False
        assert rec["contaminated_entries"] == 0

    def test_sweep_skips_owner_check_for_curator_collections(
        self, env: Catalog,
    ) -> None:
        """Curator owners legitimately span sources — the wrong-home
        check does not apply."""
        owner = env.register_owner("hal-papers", "curator")
        env.register(
            owner, "p1", content_type="paper",
            physical_collection="docs__hal_papers",
            source_uri="file:///some/path/p1.pdf",
        )
        env.register(
            owner, "p2", content_type="paper",
            physical_collection="docs__hal_papers",
            source_uri="file:///some/path/p2.pdf",
        )
        runner = CliRunner()
        result = runner.invoke(
            catalog, ["audit-membership", "--all-collections", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        rec = next(
            r for r in data["collections"]
            if r["collection"] == "docs__hal_papers"
        )
        assert rec["wrong_home"] is False
        assert rec["expected_home"] == ""

    def test_sweep_text_output_marks_wrong_home(
        self, env: Catalog, tmp_path, monkeypatch,
    ) -> None:
        self._seed_repo_owned_all_wrong_home(env, tmp_path, monkeypatch)
        runner = CliRunner()
        result = runner.invoke(catalog, ["audit-membership", "--all-collections"])
        assert result.exit_code == 0, result.output
        assert "wrong-home" in result.output
        assert "expected=" in result.output
