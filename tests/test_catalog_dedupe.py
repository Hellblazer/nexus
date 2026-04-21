# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx catalog dedupe-owners (nexus-tmbh, part of nexus-b34f)."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.catalog.dedupe import (
    DedupePlan,
    apply_alias_plan,
    apply_plan,
    apply_remove_plan,
    plan_dedupe,
)
from nexus.catalog.tumbler import Tumbler
from nexus.cli import main


@pytest.fixture
def cat(tmp_path):
    """Minimal initialised catalog for dedupe tests."""
    return Catalog.init(tmp_path / "catalog")


class TestPlanClassification:
    def test_test_orphan_int_cce_flagged_for_removal(self, cat):
        cat.register_owner("int-cce-deadbeef", "curator")
        plan = plan_dedupe(cat)
        assert len(plan.remove) == 1
        assert plan.remove[0].orphan_name == "int-cce-deadbeef"
        assert "test-orphan" in plan.remove[0].reason

    def test_test_orphan_pdf_e2e_flagged_for_removal(self, cat):
        cat.register_owner("pdf-e2e-simple", "curator")
        plan = plan_dedupe(cat)
        assert len(plan.remove) == 1
        assert plan.remove[0].orphan_name == "pdf-e2e-simple"

    def test_synthetic_name_with_canonical_match_flagged_for_alias(self, cat):
        cat.register_owner("nexus", "repo", repo_hash="571b8edd", repo_root="/tmp/nexus")
        cat.register_owner("nexus-571b8edd", "curator")
        plan = plan_dedupe(cat)
        assert len(plan.alias) == 1
        assert plan.alias[0].orphan_name == "nexus-571b8edd"
        assert plan.alias[0].canonical_name == "nexus"

    def test_synthetic_name_without_canonical_skipped(self, cat):
        """Synthetic name but no matching repo — we don't know where to
        point the alias, so it's skipped for manual review."""
        cat.register_owner("ghost-abcd1234", "curator")
        plan = plan_dedupe(cat)
        assert len(plan.skip) == 1
        assert plan.skip[0].orphan_name == "ghost-abcd1234"

    def test_legitimate_curator_skipped(self, cat):
        """Clean curator names like ``papers`` or ``knowledge`` are
        skipped — no pattern match, no canonical target."""
        cat.register_owner("papers", "curator")
        cat.register_owner("knowledge", "curator")
        plan = plan_dedupe(cat)
        assert {op.orphan_name for op in plan.skip} == {"papers", "knowledge"}
        assert not plan.alias and not plan.remove

    def test_repo_owner_not_classified(self, cat):
        """Repo-type owners are never treated as orphans (they are
        well-formed by nexus-zbne construction)."""
        cat.register_owner("nexus", "repo", repo_hash="abc12345", repo_root="/tmp/nexus")
        plan = plan_dedupe(cat)
        assert plan.summary() == {
            "alias": 0, "remove": 0, "skip": 0,
            "alias_docs": 0, "remove_docs": 0, "skip_docs": 0,
        }

    def test_partial_hash_prefix_match(self, cat):
        """Orphan name uses 6 hex chars; canonical repo_hash is longer.
        The planner's prefix probe should still find the match."""
        cat.register_owner("nexus", "repo", repo_hash="571b8eddaabb", repo_root="/tmp/nexus")
        cat.register_owner("nexus-571b8e", "curator")
        plan = plan_dedupe(cat)
        assert len(plan.alias) == 1


class TestAliasApply:
    def test_alias_matches_by_file_path(self, cat, tmp_path):
        """Each orphan doc gets aliased to its canonical equivalent with
        the same ``file_path``."""
        canonical_owner = cat.register_owner(
            "nexus", "repo", repo_hash="571b8edd", repo_root=str(tmp_path),
        )
        canonical_doc = cat.register(
            canonical_owner, "indexer.py", content_type="code",
            file_path="src/nexus/indexer.py",
        )
        orphan_owner = cat.register_owner("nexus-571b8edd", "curator")
        orphan_doc = cat.register(
            orphan_owner, "indexer.py", content_type="code",
            file_path="src/nexus/indexer.py",
        )

        plan = plan_dedupe(cat)
        assert len(plan.alias) == 1
        aliased, unmatched = apply_alias_plan(cat, plan.alias[0])
        assert (aliased, unmatched) == (1, 0)

        # resolve() on the orphan tumbler now returns the canonical entry.
        assert cat.resolve(orphan_doc).tumbler == canonical_doc

    def test_alias_unmatched_file_path_reported(self, cat, tmp_path):
        """Orphan doc with no canonical counterpart is counted as
        unmatched and left alone (no alias set)."""
        canonical_owner = cat.register_owner(
            "nexus", "repo", repo_hash="571b8edd", repo_root=str(tmp_path),
        )
        cat.register(
            canonical_owner, "a.py", content_type="code", file_path="a.py",
        )
        orphan_owner = cat.register_owner("nexus-571b8edd", "curator")
        orphan_doc = cat.register(
            orphan_owner, "b.py", content_type="code", file_path="b.py",
        )

        plan = plan_dedupe(cat)
        aliased, unmatched = apply_alias_plan(cat, plan.alias[0])
        assert (aliased, unmatched) == (0, 1)
        # Orphan doc still has empty alias_of
        entry = cat.resolve(orphan_doc, follow_alias=False)
        assert entry is not None and entry.alias_of == ""


class TestRemoveApply:
    def test_remove_deletes_docs_and_owner(self, cat):
        owner = cat.register_owner("int-cce-deadbeef", "curator")
        cat.register(owner, "a.md", content_type="prose", file_path="a.md")
        cat.register(owner, "b.md", content_type="prose", file_path="b.md")

        plan = plan_dedupe(cat)
        assert len(plan.remove) == 1

        docs, links = apply_remove_plan(cat, plan.remove[0])
        assert docs == 2
        # Owner row gone
        row = cat._db.execute(
            "SELECT 1 FROM owners WHERE tumbler_prefix = ?", (str(owner),)
        ).fetchone()
        assert row is None
        # Doc rows gone
        clause, params = cat._prefix_sql(str(owner))
        remaining = cat._db.execute(
            f"SELECT COUNT(*) FROM documents WHERE {clause}", params
        ).fetchone()[0]
        assert remaining == 0

    def test_remove_drops_links_referencing_deleted_docs(self, cat):
        """Links from/to deleted tumblers are removed; unrelated links stay."""
        keeper = cat.register_owner("knowledge", "curator")
        keeper_doc = cat.register(keeper, "keep.md", content_type="prose", file_path="keep.md")

        orphan = cat.register_owner("int-cce-cafebabe", "curator")
        orphan_doc = cat.register(orphan, "gone.md", content_type="prose", file_path="gone.md")

        # Link from keeper to orphan — this should be removed.
        cat.link(keeper_doc, orphan_doc, link_type="relates", created_by="test")
        # Unrelated link keeper → keeper (self-link for test simplicity) — kept.
        # SQLite UNIQUE constraint on (from, to, type) — so use a different type.
        cat.link(keeper_doc, keeper_doc, link_type="relates", created_by="test",
                 allow_dangling=True)

        plan = plan_dedupe(cat)
        remove_op = next(op for op in plan.remove if op.orphan_name == "int-cce-cafebabe")
        _docs, links = apply_remove_plan(cat, remove_op)
        assert links >= 1  # at least the keeper→orphan edge

        # Unrelated link survives.
        remaining = cat._db.execute(
            "SELECT COUNT(*) FROM links WHERE from_tumbler = ? AND to_tumbler = ?",
            (str(keeper_doc), str(keeper_doc)),
        ).fetchone()[0]
        assert remaining == 1


class TestApplyPlanTotals:
    def test_full_sweep(self, cat, tmp_path):
        canonical = cat.register_owner(
            "nexus", "repo", repo_hash="571b8edd", repo_root=str(tmp_path),
        )
        cat.register(canonical, "a.py", content_type="code", file_path="a.py")
        orphan_sym = cat.register_owner("nexus-571b8edd", "curator")
        cat.register(orphan_sym, "a.py", content_type="code", file_path="a.py")

        orphan_test = cat.register_owner("int-cce-1234abcd", "curator")
        cat.register(orphan_test, "leak.md", content_type="prose", file_path="leak.md")

        cat.register_owner("papers", "curator")  # skipped

        plan = plan_dedupe(cat)
        totals = apply_plan(cat, plan)
        assert totals["orphans_aliased"] == 1
        assert totals["aliased_docs"] == 1
        assert totals["orphans_removed"] == 1
        assert totals["removed_docs"] == 1


# ── CLI integration ────────────────────────────────────────────────────────


class TestDedupeCLI:
    def test_dry_run_default(self, cat, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat._dir))
        cat.register_owner("int-cce-deadbeef", "curator")

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "dedupe-owners"])
        assert result.exit_code == 0, result.output
        assert "Would apply dedupe plan" in result.output
        assert "int-cce-deadbeef" in result.output
        assert "Dry-run only" in result.output

        # Owner still present — dry-run must not mutate.
        row = cat._db.execute(
            "SELECT 1 FROM owners WHERE name = ?", ("int-cce-deadbeef",)
        ).fetchone()
        assert row is not None

    def test_apply_removes_orphan(self, cat, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat._dir))
        cat.register_owner("int-cce-deadbeef", "curator")

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "dedupe-owners", "--apply"])
        assert result.exit_code == 0, result.output
        assert "orphans removed:" in result.output

        # Owner row is gone.
        row = cat._db.execute(
            "SELECT 1 FROM owners WHERE name = ?", ("int-cce-deadbeef",)
        ).fetchone()
        assert row is None

    def test_json_output_contains_summary_and_plan(self, cat, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat._dir))
        cat.register_owner("int-cce-deadbeef", "curator")
        cat.register_owner("papers", "curator")

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "dedupe-owners", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["dry_run"] is True
        assert payload["summary"]["remove"] == 1
        assert payload["summary"]["skip"] == 1
        assert any(op["orphan_name"] == "int-cce-deadbeef" for op in payload["remove"])
        assert any(op["orphan_name"] == "papers" for op in payload["skip"])
