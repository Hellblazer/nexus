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
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
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
    """Live service catalog + tmp T2 path + plumbing patches (nexus-i711w:
    the local SQLite catalog is gone; seeding routes through the same
    service-only factories the verbs read with)."""
    from tests._catalog_fixture_ops import ActiveCatalog  # noqa: PLC0415

    catalog_dir = tmp_path / "catalog"
    db_path = tmp_path / "t2.db"
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db_path)
    return catalog_dir, db_path, ActiveCatalog()


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
        doc_id=str(t),
    )
    with T2Database(db_path) as db:
        db.document_aspects.upsert(rec)
    return str(t)


# ── aspects-show ────────────────────────────────────────────────────────────


class TestAspectsShow:
    def test_show_unknown_tumbler_errors(self, env) -> None:
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", "1.99.99"])
        assert result.exit_code != 0

    def test_unsupported_prefix_states_the_fact_not_a_dead_remedy(self, env) -> None:
        """nexus-hj7mg: measured 2026-08-24 — a code__* document (the large
        majority of the catalog) with no aspect record got told to `Run
        'nx enrich aspects code__...'`, which then aborts with 'No
        extractor config registered for collection'. The remedy must
        state the actual fact (aspects are not extracted for this
        prefix) instead of naming a command that fails, and must derive
        the supported-prefix list from the same registry
        `nx enrich aspects` consults."""
        cat_dir, db_path, cat = env
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myrepo",
        )
        t = cat.register(
            owner, "t3.py",
            content_type="code",
            physical_collection="code__myrepo",
            file_path="src/t3.py",
        )
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", str(t)])
        assert result.exit_code == 0, result.output
        assert "Run 'nx enrich aspects" not in result.output, (
            f"must not recommend a command that aborts: {result.output!r}"
        )
        assert "code__myrepo" in result.output
        assert "not extracted" in result.output
        assert "knowledge__*" in result.output
        assert "rdr__*" in result.output

    def test_supported_prefix_with_no_record_still_names_the_extract_command(
        self, env,
    ) -> None:
        """Control: a knowledge__* document with no aspect record yet is
        the genuinely runnable case — the original 'Run nx enrich
        aspects ...' remedy must still fire there."""
        cat_dir, db_path, cat = env
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myrepo",
        )
        t = cat.register(
            owner, "Unprocessed Paper",
            content_type="paper",
            physical_collection="knowledge__myrepo-papers",
            file_path="docs/papers/unprocessed.pdf",
        )
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects-show", str(t)])
        assert result.exit_code == 0, result.output
        assert "Run 'nx enrich aspects knowledge__myrepo-papers'" in result.output


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
                doc_tumbler = cat.register(
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
                    doc_id=str(doc_tumbler),
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


    def test_list_json_emits_array(self, env) -> None:
        self._seed_many(env, ["docs/papers/a.pdf", "docs/papers/b.pdf"])
        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects-list",
            "--collection", "knowledge__myrepo-papers",
            "--json",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
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
