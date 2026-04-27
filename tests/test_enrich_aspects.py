# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-089 P2.2: ``nx enrich aspects <collection>`` CLI tests.

The subcommand iterates the catalog (one entry per source document)
and calls ``extract_aspects`` directly — bypassing the
``post_document_hook`` chain to avoid double-firing on documents
already extracted at ingest. AspectRecords are upserted to T2
``document_aspects``; an optional ``--validate-sample N%`` runs
``operator_verify`` on a random sample and writes disagreements to
``./validation_failures.jsonl``.

Phase 1 supports ``knowledge__*`` collections only; other prefixes
short-circuit at the ``select_config`` step.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
from nexus.catalog import Catalog
from nexus.commands.enrich import enrich


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _make_record(
    *, source_path: str = "/p1.pdf",
    problem_formulation: str | None = "P",
    proposed_method: str | None = "M",
    model_version: str = "claude-haiku-4-5-20251001",
) -> AspectRecord:
    return AspectRecord(
        collection="knowledge__delos",
        source_path=source_path,
        problem_formulation=problem_formulation,
        proposed_method=proposed_method,
        experimental_datasets=["d1"],
        experimental_baselines=["b1"],
        experimental_results="R",
        extras={"venue": "V"},
        confidence=0.9,
        extracted_at=datetime.now(UTC).isoformat(),
        model_version=model_version,
        extractor_name="scholarly-paper-v1",
    )


def _register_entries(cat: Catalog, source_paths: list[str]) -> None:
    """Register one paper-style entry per source_path under the
    knowledge__delos physical collection."""
    owner = cat.register_owner("knowledge", "curator")
    for sp in source_paths:
        cat.register(
            owner, Path(sp).stem,
            content_type="paper",
            physical_collection="knowledge__delos",
            file_path=sp,
        )


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Create an isolated environment: real tmp catalog + tmp T2 DB.
    Patches catalog_path() and default_db_path() so the CLI reads/writes
    only inside tmp_path. Returns (catalog_dir, db_path, cat)."""
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)

    db_path = tmp_path / "enrich_aspects.db"

    # nexus.config.catalog_path is the seam used by the CLI's
    # _select_entries function; patch it directly.
    import nexus.config
    monkeypatch.setattr(nexus.config, "catalog_path", lambda: catalog_dir)

    # default_db_path is imported lazily inside the CLI; patch the
    # canonical location.
    import nexus.commands._helpers as h
    monkeypatch.setattr(h, "default_db_path", lambda: db_path)

    return catalog_dir, db_path, cat


# ── Routing / unsupported collection ────────────────────────────────────────


class TestRouting:
    def test_unsupported_collection_aborts_cleanly(self, env) -> None:
        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects", "code__nexus"])
        assert result.exit_code == 0
        assert "No extractor config" in result.output
        assert "knowledge__" in result.output


# ── --dry-run ───────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_reports_count_and_cost_no_subprocess(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--dry-run prints document count + cost estimate and NEVER
        calls subprocess.run (the bead's load-bearing constraint).
        The prediction loop's T3 fetch is force-failed so the test
        exercises only the count/cost output path.
        """
        _, _, cat = env
        _register_entries(cat, [f"/papers/p{i}.pdf" for i in range(5)])

        # Block prediction loop's T3 fetch — keeps the test deterministic
        # in environments without chroma access.
        def _no_t3():
            raise RuntimeError("test: no t3")
        monkeypatch.setattr("nexus.mcp_infra.get_t3", _no_t3)

        with patch("subprocess.run", side_effect=AssertionError("must not call subprocess")):
            runner = CliRunner()
            result = runner.invoke(
                enrich, ["aspects", "knowledge__delos", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        assert "5 document(s)" in result.output
        assert "knowledge__delos" in result.output
        assert "$0.05" in result.output  # 5 × $0.01
        assert "--dry-run: skipping extraction" in result.output
        # Prediction step gracefully skipped when T3 is unavailable.
        assert "read-side prediction skipped" in result.output

    def test_dry_run_all_readable_proceeds(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the read-side check finds every entry readable, the
        dry-run reports 'All entries readable' without listing
        individual entries or recomputing cost.
        """
        from nexus.aspect_readers import ReadOk

        _, _, cat = env
        _register_entries(cat, [f"/papers/p{i}.pdf" for i in range(3)])

        class _FakeT3:
            pass
        monkeypatch.setattr("nexus.mcp_infra.get_t3", lambda: _FakeT3())
        monkeypatch.setattr(
            "nexus.aspect_readers.read_source",
            lambda uri, t3=None, **_kw: ReadOk(text="ok", metadata={}),
        )

        with patch("subprocess.run", side_effect=AssertionError("must not call subprocess")):
            runner = CliRunner()
            result = runner.invoke(
                enrich, ["aspects", "knowledge__delos", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        assert "All entries readable" in result.output
        # Skip listing should not appear when nothing is skipped.
        assert "Planned skips" not in result.output
        assert "by_reason" not in result.output

    def test_dry_run_predicts_skips_via_read_source(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--dry-run runs the read-side check per entry (no Claude
        subprocess) to enumerate ExtractFail entries that would be
        skipped, with by_reason summary. Closes the planning gap that
        let operators run a $5 extraction only to discover most entries
        would skip.
        """
        from nexus.aspect_readers import ReadFail, ReadOk

        _, _, cat = env
        _register_entries(cat, [
            "/papers/good1.pdf",
            "/papers/good2.pdf",
            "/papers/ghost1.pdf",
            "/papers/ghost2.pdf",
            "/papers/ghost3.pdf",
        ])

        # Force prediction path to succeed — return ReadOk for paths
        # containing 'good', ReadFail otherwise.
        class _FakeT3:
            pass
        monkeypatch.setattr("nexus.mcp_infra.get_t3", lambda: _FakeT3())

        def fake_read(uri, t3=None, **_kw):
            if "good" in uri:
                return ReadOk(text="content", metadata={})
            if "ghost1" in uri:
                return ReadFail(reason="empty", detail="no chunks")
            return ReadFail(reason="unreachable", detail="get_collection failed")

        monkeypatch.setattr("nexus.aspect_readers.read_source", fake_read)

        with patch("subprocess.run", side_effect=AssertionError("must not call subprocess")):
            runner = CliRunner()
            result = runner.invoke(
                enrich, ["aspects", "knowledge__delos", "--dry-run"],
            )

        assert result.exit_code == 0, result.output
        # Three entries would skip (1 empty, 2 unreachable).
        assert "Planned skips: 3 of 5" in result.output
        assert "empty=1" in result.output
        assert "unreachable=2" in result.output
        # Predicted cost reflects the 2 readable entries.
        assert "$0.02" in result.output


# ── default extraction path (no validate, no re-extract) ────────────────────


class TestDefaultExtraction:
    def test_extracts_and_upserts_each_entry(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without flags: iterate catalog, call extract_aspects per
        entry, upsert document_aspects."""
        _, db_path, cat = env
        _register_entries(cat, ["/papers/p1.pdf", "/papers/p2.pdf"])

        extracted_calls: list[str] = []

        def fake_extract(content, source_path, collection):
            extracted_calls.append(source_path)
            return _make_record(source_path=source_path)

        monkeypatch.setattr(
            "nexus.aspect_extractor.extract_aspects", fake_extract,
        )

        runner = CliRunner()
        result = runner.invoke(
            enrich,
            ["aspects", "knowledge__delos", "--validate-sample", "0"],
        )
        assert result.exit_code == 0, result.output
        assert "2 extracted" in result.output
        assert sorted(extracted_calls) == ["/papers/p1.pdf", "/papers/p2.pdf"]

        from nexus.db.t2 import T2Database
        with T2Database(db_path) as db:
            count = db.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects"
            ).fetchone()[0]
        assert count == 2

    def test_extract_fail_skips_without_upsert(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """RDR-096 P1.3 (closes #331's symptom): when ``extract_aspects``
        returns ``ExtractFail``, the loop logs + skips with NO row
        written. Per-collection summary reports the skip count and
        the by_reason breakdown.
        """
        from nexus.aspect_extractor import ExtractFail
        _, db_path, cat = env
        _register_entries(cat, [
            "/papers/good.pdf",
            "/papers/ghost-stale.pdf",
            "/papers/ghost-empty.pdf",
        ])

        def fake_extract(content, source_path, collection):
            if "good" in source_path:
                return _make_record(source_path=source_path)
            if "ghost-stale" in source_path:
                return ExtractFail(
                    uri=f"chroma://{collection}/{source_path}",
                    reason="empty",
                    detail="no chunks for ghost-stale",
                )
            return ExtractFail(
                uri=f"chroma://{collection}/{source_path}",
                reason="unreachable",
                detail="get_collection failed",
            )

        monkeypatch.setattr(
            "nexus.aspect_extractor.extract_aspects", fake_extract,
        )

        runner = CliRunner()
        result = runner.invoke(
            enrich,
            ["aspects", "knowledge__delos", "--validate-sample", "0"],
        )
        assert result.exit_code == 0, result.output

        # Exactly one row in document_aspects (the good entry) — no
        # null-field rows from the two ExtractFail entries.
        from nexus.db.t2 import T2Database
        with T2Database(db_path) as db:
            count = db.document_aspects.conn.execute(
                "SELECT COUNT(*) FROM document_aspects",
            ).fetchone()[0]
        assert count == 1

        # Summary surfaces both extracted and skip counts with reasons.
        assert "1 extracted" in result.output
        assert "2 skipped (read-failure)" in result.output
        assert "empty=1" in result.output
        assert "unreachable=1" in result.output

    def test_null_fields_record_counted_separately(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When extract_aspects returns a null-fields record (extractor
        failed 3x), the record is upserted but NOT counted as a
        success — and excluded from the validate-sample pool."""
        _, _, cat = env
        _register_entries(cat, ["/papers/p1.pdf"])

        def fake_extract(content, source_path, collection):
            return _make_record(
                source_path=source_path,
                problem_formulation=None,
                proposed_method=None,
            )

        monkeypatch.setattr(
            "nexus.aspect_extractor.extract_aspects", fake_extract,
        )

        runner = CliRunner()
        result = runner.invoke(
            enrich,
            ["aspects", "knowledge__delos", "--validate-sample", "0"],
        )
        assert result.exit_code == 0, result.output
        assert "0 extracted, 1 null-fields" in result.output


# ── --re-extract --extractor-version ─────────────────────────────────────────


class TestReExtract:
    def test_re_extract_filters_to_outdated_rows(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With --re-extract --extractor-version v, only entries whose
        existing aspect row has model_version < v (or no row at all)
        are processed."""
        _, db_path, cat = env
        _register_entries(cat, [
            "/papers/p1.pdf", "/papers/p2.pdf", "/papers/p3.pdf",
        ])

        # Pre-populate document_aspects: p1 at old version, p2 at new
        # version, p3 has no row.
        from nexus.db.t2 import T2Database
        with T2Database(db_path) as db:
            db.document_aspects.upsert(_make_record(
                source_path="/papers/p1.pdf",
                model_version="claude-haiku-4-1",
            ))
            db.document_aspects.upsert(_make_record(
                source_path="/papers/p2.pdf",
                model_version="claude-haiku-4-5-20251001",
            ))

        re_extracted: list[str] = []

        def fake_extract(content, source_path, collection):
            re_extracted.append(source_path)
            return _make_record(source_path=source_path)

        monkeypatch.setattr(
            "nexus.aspect_extractor.extract_aspects", fake_extract,
        )

        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects", "knowledge__delos",
            "--re-extract",
            "--extractor-version", "claude-haiku-4-5-20251001",
            "--validate-sample", "0",
        ])
        assert result.exit_code == 0, result.output
        # p1 (outdated) + p3 (missing) re-extracted; p2 (current) skipped.
        assert "/papers/p1.pdf" in re_extracted
        assert "/papers/p3.pdf" in re_extracted
        assert "/papers/p2.pdf" not in re_extracted

    def test_re_extract_requires_extractor_version_flag(self, env) -> None:
        """--re-extract without --extractor-version aborts with a
        clear error (no extraction attempted)."""
        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects", "knowledge__delos", "--re-extract",
        ])
        assert result.exit_code == 0
        assert "--re-extract requires --extractor-version" in result.output


# ── --validate-sample ───────────────────────────────────────────────────────


class TestValidateSample:
    def test_validate_sample_skipped_when_zero(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--validate-sample 0 skips validation entirely."""
        _, _, cat = env
        _register_entries(cat, ["/papers/p1.pdf"])

        monkeypatch.setattr(
            "nexus.aspect_extractor.extract_aspects",
            lambda content, source_path, collection: _make_record(
                source_path=source_path,
            ),
        )

        verify_calls: list = []

        async def never_called_verify(claim, evidence, timeout=60.0):
            verify_calls.append(claim)
            return {"verified": True, "reason": "", "citations": []}

        monkeypatch.setattr(
            "nexus.mcp.core.operator_verify", never_called_verify,
        )

        runner = CliRunner()
        result = runner.invoke(enrich, [
            "aspects", "knowledge__delos", "--validate-sample", "0",
        ])
        assert result.exit_code == 0, result.output
        assert "Validating" not in result.output
        assert verify_calls == []

    def test_validate_sample_runs_verify_and_writes_failures(
        self,
        env,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 100% sample with operator_verify returning verified=False
        writes one ``validation_failures.jsonl`` row per disagreement."""
        _, _, cat = env

        # Create real source files so the validate-sample read works.
        src1 = tmp_path / "p1.txt"
        src1.write_text("paper content 1")
        src2 = tmp_path / "p2.txt"
        src2.write_text("paper content 2")

        _register_entries(cat, [str(src1), str(src2)])

        monkeypatch.setattr(
            "nexus.aspect_extractor.extract_aspects",
            lambda content, source_path, collection: _make_record(
                source_path=source_path,
            ),
        )

        async def disagree(claim, evidence, timeout=60.0):
            return {
                "verified": False,
                "reason": "claim cites datasets not in evidence",
                "citations": ["page 3"],
            }

        monkeypatch.setattr(
            "nexus.mcp.core.operator_verify", disagree,
        )

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(enrich, [
                "aspects", "knowledge__delos",
                "--validate-sample", "100",
            ])
            assert result.exit_code == 0, result.output
            assert "Validating" in result.output
            assert "disagreement" in result.output
            failures_path = Path("validation_failures.jsonl")
            assert failures_path.exists()
            lines = failures_path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            row = json.loads(line)
            assert row["operator_verify_reason"] == "claim cites datasets not in evidence"
            assert row["citations"] == ["page 3"]
            assert "extracted_aspects" in row
            assert "timestamp" in row

    def test_validate_sample_default_is_5_percent(self, env) -> None:
        """The CLI default --validate-sample is 5% per the RDR's
        original Phase 2 spec. operator_verify catches hallucinations
        on the sample; strict-equality cross-run stability is a
        methodology metric not a model-quality metric and does not
        belong in the default-rate decision."""
        from nexus.commands.enrich import _DEFAULT_VALIDATE_SAMPLE_PCT
        assert _DEFAULT_VALIDATE_SAMPLE_PCT == 5


# ── Catalog missing ─────────────────────────────────────────────────────────


class TestCatalogMissing:
    def test_uninitialized_catalog_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the catalog is not initialized, the command aborts
        with a clear instruction to run 'nx catalog setup'.

        Bypasses the env fixture (which initializes the catalog) by
        pointing catalog_path at a non-existent directory directly.
        """
        empty_dir = tmp_path / "no-catalog-here"
        import nexus.config
        monkeypatch.setattr(nexus.config, "catalog_path", lambda: empty_dir)

        runner = CliRunner()
        result = runner.invoke(enrich, ["aspects", "knowledge__delos"])
        assert result.exit_code == 0
        assert "Catalog not initialized" in result.output
        assert "nx catalog setup" in result.output


# ── Group structure ─────────────────────────────────────────────────────────


class TestGroupStructure:
    def test_enrich_is_a_click_group(self) -> None:
        """The post-restructure ``enrich`` is a click group with at
        least the ``bib`` and ``aspects`` subcommands."""
        import click
        assert isinstance(enrich, click.Group)
        cmds = enrich.list_commands(ctx=None)  # type: ignore[arg-type]
        assert "bib" in cmds
        assert "aspects" in cmds

    def test_enrich_help_lists_subcommands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(enrich, ["--help"])
        assert result.exit_code == 0
        assert "bib" in result.output
        assert "aspects" in result.output


# ── Day 2 Operations: list / info / delete ──────────────────────────────────


class TestDay2Ops:
    """RDR-089 §Day 2 Operations: list, info, delete."""

    def test_list_prints_rows_with_population_count(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.db.t2 import T2Database

        _, db_path, _ = env
        with T2Database(db_path) as db:
            db.document_aspects.upsert(_make_record(
                source_path="/p1.pdf", problem_formulation="P1",
            ))
            db.document_aspects.upsert(_make_record(
                source_path="/p2.pdf",
                problem_formulation=None, proposed_method=None,
            ))

        runner = CliRunner()
        result = runner.invoke(
            enrich, ["list", "knowledge__delos"],
        )
        assert result.exit_code == 0, result.output
        assert "/p1.pdf" in result.output
        assert "/p2.pdf" in result.output
        assert "5/5" in result.output  # p1 fully populated
        assert "3/5" in result.output  # p2 with two None scalars
        assert "2 row(s)" in result.output

    def test_list_empty_collection(self, env) -> None:
        runner = CliRunner()
        result = runner.invoke(
            enrich, ["list", "knowledge__nope"],
        )
        assert result.exit_code == 0
        assert "No aspect rows" in result.output

    def test_list_respects_limit(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.db.t2 import T2Database

        _, db_path, _ = env
        with T2Database(db_path) as db:
            for i in range(5):
                db.document_aspects.upsert(_make_record(
                    source_path=f"/p{i}.pdf",
                ))

        runner = CliRunner()
        result = runner.invoke(
            enrich, ["list", "knowledge__delos", "--limit", "2"],
        )
        assert result.exit_code == 0
        assert "2 row(s)" in result.output

    def test_info_prints_full_record_json(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import json
        from nexus.db.t2 import T2Database

        _, db_path, _ = env
        with T2Database(db_path) as db:
            db.document_aspects.upsert(_make_record(
                source_path="/p1.pdf",
            ))

        runner = CliRunner()
        result = runner.invoke(
            enrich, ["info", "knowledge__delos", "/p1.pdf"],
        )
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        assert parsed["collection"] == "knowledge__delos"
        assert parsed["source_path"] == "/p1.pdf"
        assert parsed["problem_formulation"] == "P"
        assert parsed["experimental_datasets"] == ["d1"]
        assert parsed["extras"] == {"venue": "V"}
        assert parsed["extractor_name"] == "scholarly-paper-v1"

    def test_info_missing_row_prints_notice(self, env) -> None:
        runner = CliRunner()
        result = runner.invoke(
            enrich, ["info", "knowledge__delos", "/missing.pdf"],
        )
        assert result.exit_code == 0
        assert "No aspect row" in result.output

    def test_delete_removes_row(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.db.t2 import T2Database

        _, db_path, _ = env
        with T2Database(db_path) as db:
            db.document_aspects.upsert(_make_record(
                source_path="/p1.pdf",
            ))

        runner = CliRunner()
        result = runner.invoke(
            enrich,
            ["delete", "knowledge__delos", "/p1.pdf", "--yes"],
        )
        assert result.exit_code == 0, result.output
        assert "Deleted" in result.output

        with T2Database(db_path) as db:
            assert db.document_aspects.get(
                "knowledge__delos", "/p1.pdf",
            ) is None

    def test_delete_idempotent_on_missing_row(self, env) -> None:
        runner = CliRunner()
        result = runner.invoke(
            enrich,
            ["delete", "knowledge__delos", "/missing.pdf", "--yes"],
        )
        assert result.exit_code == 0
        assert "nothing to delete" in result.output

    def test_delete_requires_confirmation_without_yes(
        self, env, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without --yes, delete prompts for confirmation. Aborting at
        the prompt leaves the row in place."""
        from nexus.db.t2 import T2Database

        _, db_path, _ = env
        with T2Database(db_path) as db:
            db.document_aspects.upsert(_make_record(
                source_path="/p1.pdf",
            ))

        runner = CliRunner()
        # Send "n\n" to the prompt to abort.
        result = runner.invoke(
            enrich,
            ["delete", "knowledge__delos", "/p1.pdf"],
            input="n\n",
        )
        assert result.exit_code != 0  # click.confirm abort -> non-zero

        with T2Database(db_path) as db:
            assert db.document_aspects.get(
                "knowledge__delos", "/p1.pdf",
            ) is not None
