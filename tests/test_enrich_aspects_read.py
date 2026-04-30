# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``nx enrich aspects-show`` + ``nx enrich aspects-list`` (nexus-bkvk).

The read-side companions to ``nx enrich aspects`` (batch extract).
Pre-this-PR the only way to inspect extracted aspects was raw SQL
against ``~/.config/nexus/memory.db``. These verbs surface the
data via the CLI for human + script consumption.

* ``aspects-show <TUMBLER>`` — display one document's full aspect
  record. ``--json`` for structured output, ``--field <name>`` for
  single-field projection.
* ``aspects-list --collection X`` — tabular preview of all rows in
  a collection. ``--missing`` flips to "catalog rows without aspect
  rows" for gap detection. ``--json`` for structured output.

Tumbler resolution reuses ``_resolve_tumbler`` so ``aspects-show``
accepts both tumblers and titles, matching ``nx catalog show``.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
from nexus.catalog import Catalog
from nexus.commands.enrich import enrich
from nexus.db.t2 import T2Database


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Tmp catalog + tmp T2 + plumbing patches."""
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    db_path = tmp_path / "t2.db"

    import nexus.config
    monkeypatch.setattr(nexus.config, "catalog_path", lambda: catalog_dir)
    import nexus.commands._helpers as h
    monkeypatch.setattr(h, "default_db_path", lambda: db_path)
    return catalog_dir, db_path, cat


def _seed_one(env, *, source_path: str = "docs/papers/p.pdf") -> str:
    """Register a paper-shaped catalog row + write its aspect record.
    Returns the tumbler string."""
    cat_dir, db_path, cat = env
    owner = cat.register_owner(
        "myrepo", "repo", repo_hash="abcd1234",
        repo_root="/tmp/myrepo",
    )
    t = cat.register(
        owner, "Sample Paper",
        content_type="paper",
        physical_collection="knowledge__myrepo-papers",
        file_path=source_path,
    )
    rec = AspectRecord(
        collection="knowledge__myrepo-papers",
        source_path=source_path,
        problem_formulation="Stale-reads in multi-region replicas.",
        proposed_method="Vector-clock + bounded staleness reads with WAL replay.",
        experimental_datasets=["YCSB-A", "TPC-C"],
        experimental_baselines=["raft", "paxos-quorum"],
        experimental_results="2.3x throughput at p99=120ms vs raft baseline.",
        extras={"venue": "VLDB 2024", "ablations_present": True},
        confidence=0.92,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version="claude-haiku-4-5-20251001",
        extractor_name="scholarly-paper-v1",
        source_uri=f"file:///tmp/myrepo/{source_path}",
    )
    with T2Database(db_path) as db:
        db.document_aspects.upsert(rec)
    return str(t)


# ── aspects-show ────────────────────────────────────────────────────────────


class TestAspectsShow:
    def test_show_displays_all_fields_by_tumbler(self, env) -> None:
        tumbler = _seed_one(env)
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", tumbler])
        assert result.exit_code == 0, result.output
        # All 7 aspect fields surface in the output.
        for marker in (
            "Stale-reads in multi-region",
            "Vector-clock + bounded",
            "YCSB-A",
            "raft",
            "2.3x throughput",
            "VLDB 2024",
            "0.92",
        ):
            assert marker in result.output, f"missing {marker!r} in output"
        # Extractor metadata also visible.
        assert "scholarly-paper-v1" in result.output
        assert "claude-haiku-4-5" in result.output

    def test_show_resolves_by_title(self, env) -> None:
        _seed_one(env)
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", "Sample Paper"])
        assert result.exit_code == 0, result.output
        assert "Stale-reads" in result.output

    def test_show_json_emits_structured(self, env) -> None:
        tumbler = _seed_one(env)
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", tumbler, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["collection"] == "knowledge__myrepo-papers"
        assert data["problem_formulation"].startswith("Stale-reads")
        assert data["experimental_datasets"] == ["YCSB-A", "TPC-C"]
        assert data["experimental_baselines"] == ["raft", "paxos-quorum"]
        assert data["extras"]["venue"] == "VLDB 2024"
        assert data["confidence"] == 0.92

    def test_show_field_projection(self, env) -> None:
        tumbler = _seed_one(env)
        runner = CliRunner()
        result = runner.invoke(
            enrich, ["aspects-show", tumbler, "--field", "proposed_method"],
        )
        assert result.exit_code == 0, result.output
        assert "Vector-clock" in result.output
        # Other fields are NOT in projection output.
        assert "YCSB-A" not in result.output
        assert "scholarly-paper-v1" not in result.output

    def test_show_unknown_field_errors(self, env) -> None:
        tumbler = _seed_one(env)
        runner = CliRunner()
        result = runner.invoke(
            enrich, ["aspects-show", tumbler, "--field", "made_up"],
        )
        assert result.exit_code != 0
        assert "made_up" in result.output

    def test_show_no_aspect_record_friendly_message(self, env) -> None:
        """Catalog row exists but no aspect data extracted yet."""
        cat_dir, db_path, cat = env
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myrepo",
        )
        t = cat.register(
            owner, "Empty Paper",
            content_type="paper",
            physical_collection="knowledge__myrepo-papers",
            file_path="docs/papers/empty.pdf",
        )
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", str(t)])
        assert result.exit_code == 0
        assert "no aspect" in result.output.lower() or "not extracted" in result.output.lower()
        # And nudge the operator to the extract verb.
        assert "nx enrich aspects" in result.output

    def test_show_unknown_tumbler_errors(self, env) -> None:
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", "1.99.99"])
        assert result.exit_code != 0


# ── aspects-list ────────────────────────────────────────────────────────────


class TestAspectsList:
    def _seed_many(self, env, paths: list[str]) -> None:
        cat_dir, db_path, cat = env
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myrepo",
        )
        with T2Database(db_path) as db:
            for sp in paths:
                cat.register(
                    owner, Path(sp).stem,
                    content_type="paper",
                    physical_collection="knowledge__myrepo-papers",
                    file_path=sp,
                )
                db.document_aspects.upsert(AspectRecord(
                    collection="knowledge__myrepo-papers",
                    source_path=sp,
                    problem_formulation=f"Problem of {Path(sp).stem}",
                    proposed_method=f"Method of {Path(sp).stem}",
                    experimental_datasets=[],
                    experimental_baselines=[],
                    experimental_results="",
                    extras={},
                    confidence=0.5,
                    extracted_at=datetime.now(UTC).isoformat(),
                    model_version="m",
                    extractor_name="scholarly-paper-v1",
                ))

    def test_list_returns_all_rows_in_collection(self, env) -> None:
        self._seed_many(env, [
            "docs/papers/a.pdf",
            "docs/papers/b.pdf",
            "docs/papers/c.pdf",
        ])
        runner = CliRunner()
        result = runner.invoke(
            enrich, ["aspects-list", "--collection", "knowledge__myrepo-papers"],
        )
        assert result.exit_code == 0, result.output
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            assert name in result.output
        assert "Problem of a" in result.output

    def test_list_limit_caps_output(self, env) -> None:
        self._seed_many(env, [
            f"docs/papers/p{i}.pdf" for i in range(5)
        ])
        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects-list",
            "--collection", "knowledge__myrepo-papers",
            "--limit", "2",
        ])
        assert result.exit_code == 0, result.output
        # Two of the five sources appear, three are absent.
        appearances = sum(
            1 for i in range(5) if f"p{i}.pdf" in result.output
        )
        assert appearances == 2

    def test_list_missing_shows_catalog_rows_without_aspects(self, env) -> None:
        """``--missing`` filters to catalog rows in the collection that
        have NO matching aspect record. Used to find gaps after partial
        enrichment runs."""
        cat_dir, db_path, cat = env
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myrepo",
        )
        # 3 catalog rows; only the first has aspects.
        for i, sp in enumerate(("docs/papers/has.pdf",
                                 "docs/papers/missing1.pdf",
                                 "docs/papers/missing2.pdf")):
            cat.register(
                owner, Path(sp).stem,
                content_type="paper",
                physical_collection="knowledge__myrepo-papers",
                file_path=sp,
            )
        with T2Database(db_path) as db:
            db.document_aspects.upsert(AspectRecord(
                collection="knowledge__myrepo-papers",
                source_path="docs/papers/has.pdf",
                problem_formulation="x", proposed_method="y",
                experimental_datasets=[], experimental_baselines=[],
                experimental_results="", extras={}, confidence=0.5,
                extracted_at=datetime.now(UTC).isoformat(),
                model_version="m", extractor_name="scholarly-paper-v1",
            ))

        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects-list",
            "--collection", "knowledge__myrepo-papers",
            "--missing",
        ])
        assert result.exit_code == 0, result.output
        # Both gaps surface; the seeded one does NOT.
        assert "missing1.pdf" in result.output
        assert "missing2.pdf" in result.output
        assert "has.pdf" not in result.output

    def test_list_json_emits_array(self, env) -> None:
        self._seed_many(env, ["docs/papers/a.pdf", "docs/papers/b.pdf"])
        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects-list",
            "--collection", "knowledge__myrepo-papers",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert {d["source_path"] for d in data} == {
            "docs/papers/a.pdf", "docs/papers/b.pdf",
        }

    def test_list_empty_collection_friendly_message(self, env) -> None:
        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects-list", "--collection", "knowledge__no-such",
        ])
        assert result.exit_code == 0
        assert "no aspect" in result.output.lower() or "0" in result.output
