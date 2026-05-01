# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nx catalog doctor --replay-equality (RDR-101 Phase 1 PR C).

Coverage:
- Verb fails loudly when catalog is not initialized (no JSONL files).
- Verb requires a check flag (passing nothing is a usage error).
- Verb passes on a freshly-rebuilt catalog (synthesizer + projector
  produce the same state as Catalog.rebuild()).
- Verb fails and reports diffs when the live SQLite drifts from JSONL.
- ``--json`` mode emits a structured report consumable by other tooling.
- Verb is read-only against the live ``.catalog.db`` (mtime unchanged).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.commands.catalog import doctor_cmd


def _appendl(path: Path, obj: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(obj) + "\n")


def _build_initialized_catalog(catalog_dir: Path) -> Path:
    """Build a fresh Catalog with a few rows so rebuild() produces a non-trivial db."""
    Catalog.init(catalog_dir)
    cat = Catalog(catalog_dir, catalog_dir / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="571b8edd")
    cat.register(owner, "doc-A.md", content_type="prose", file_path="doc-A.md")
    cat.register(owner, "doc-B.md", content_type="prose", file_path="doc-B.md")
    cat._db.close()
    return catalog_dir


@pytest.fixture()
def isolated_nexus(tmp_path: Path) -> Path:
    """Return the catalog dir the autouse ``_isolate_catalog`` fixture in
    ``tests/conftest.py`` configures via ``NEXUS_CATALOG_PATH``.

    The autouse fixture sets ``NEXUS_CATALOG_PATH`` to
    ``tmp_path/test-catalog`` so tests can never pollute the real user
    catalog. The doctor verb's ``catalog_path()`` honours that env var
    before falling back to ``NEXUS_CONFIG_DIR``, so any catalog we want
    the verb to inspect must live at exactly that path.
    """
    return tmp_path / "test-catalog"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── Usage / fail-loud paths ──────────────────────────────────────────────


class TestUsage:
    def test_no_flag_is_usage_error(self, isolated_nexus, runner):
        # ``nx catalog doctor`` with no flags should be a usage error
        # (Phase 1 only supports --replay-equality; future flags arrive
        # in later phases and the verb fails loud rather than running an
        # unspecified default).
        result = runner.invoke(doctor_cmd, [])
        assert result.exit_code != 0
        assert "Pass a check flag" in (result.output + (result.stderr or ""))

    def test_missing_catalog_is_clean_error(self, isolated_nexus, runner):
        # No catalog initialized at NEXUS_CONFIG_DIR. The verb must
        # surface a helpful message, not a stack trace.
        result = runner.invoke(doctor_cmd, ["--replay-equality"])
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()


# ── Happy path: replay equality passes ───────────────────────────────────


class TestReplayEqualityPasses:
    def test_passes_on_freshly_rebuilt_catalog(self, isolated_nexus, runner):
        cat_dir = isolated_nexus
        _build_initialized_catalog(cat_dir)

        result = runner.invoke(doctor_cmd, ["--replay-equality"])
        assert result.exit_code == 0, (
            f"Expected pass, got exit {result.exit_code}\n"
            f"stdout: {result.output}\nexc: {result.exception!r}"
        )
        assert "PASS" in result.output
        # Sanity: each table line has a checkmark
        assert "owners" in result.output
        assert "documents" in result.output
        assert "links" in result.output

    def test_json_mode_emits_structured_report(self, isolated_nexus, runner):
        cat_dir = isolated_nexus
        _build_initialized_catalog(cat_dir)

        result = runner.invoke(doctor_cmd, ["--replay-equality", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["pass"] is True
        assert payload["events_applied"] >= 3  # 1 owner + 2 documents
        assert "tables" in payload
        assert payload["tables"]["owners"]["equal"] is True
        assert payload["tables"]["documents"]["equal"] is True
        assert payload["tables"]["documents"]["live_count"] == 2

    def test_does_not_mutate_live_catalog_db(self, isolated_nexus, runner):
        cat_dir = isolated_nexus
        _build_initialized_catalog(cat_dir)
        live_db = cat_dir / ".catalog.db"
        before_mtime = live_db.stat().st_mtime
        # Sleep one filesystem-resolution-tick so a write would shift mtime
        # detectably even on coarse-grained Linux filesystems.
        import time as _time
        _time.sleep(0.05)

        result = runner.invoke(doctor_cmd, ["--replay-equality"])
        assert result.exit_code == 0

        after_mtime = live_db.stat().st_mtime
        assert before_mtime == after_mtime, (
            "doctor --replay-equality must be read-only against "
            ".catalog.db; mtime advanced from "
            f"{before_mtime} to {after_mtime}"
        )


# ── Failure path: live SQLite diverges from JSONL ────────────────────────


class TestReplayEqualityFails:
    def test_reports_diff_when_live_db_drifts(self, isolated_nexus, runner):
        cat_dir = isolated_nexus
        _build_initialized_catalog(cat_dir)

        # Inject a divergence by directly mutating the live SQLite (not
        # via Catalog API, which would also rewrite JSONL).
        live_db = cat_dir / ".catalog.db"
        conn = sqlite3.connect(str(live_db))
        try:
            conn.execute(
                "UPDATE documents SET title = 'TAMPERED' "
                "WHERE tumbler = (SELECT tumbler FROM documents LIMIT 1)"
            )
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(doctor_cmd, ["--replay-equality"])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        # The diff section names the documents table
        assert "documents" in result.output
        assert "only in live" in result.output or "only in projected" in result.output

    def test_json_failure_payload(self, isolated_nexus, runner):
        cat_dir = isolated_nexus
        _build_initialized_catalog(cat_dir)

        # Introduce a divergence: remove a row from the live db.
        live_db = cat_dir / ".catalog.db"
        conn = sqlite3.connect(str(live_db))
        try:
            conn.execute("DELETE FROM documents")
            conn.commit()
        finally:
            conn.close()

        result = runner.invoke(doctor_cmd, ["--replay-equality", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["pass"] is False
        # Documents table is the source of the disagreement
        docs_diff = payload["tables"]["documents"]
        assert docs_diff["equal"] is False
        assert docs_diff["live_count"] == 0
        assert docs_diff["projected_count"] >= 1
