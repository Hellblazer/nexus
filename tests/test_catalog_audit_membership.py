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
