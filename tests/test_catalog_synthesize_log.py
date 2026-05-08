# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for `nx catalog synthesize-log` (issue #591 / nexus-hh1b).

Covers:
- --check exit codes (0 when not in fallback, 1 when fallback active)
- --dry-run prints event counts and writes nothing
- happy-path round-trip: fallback catalog -> synthesize-log -> fallback cleared
- no-op when not in fallback (without --force)
- --force re-synthesizes and preserves existing doc_ids
- snapshot is created and retained on PASS
- --no-verify skips post-write verification
- auto-restore on verify FAIL (snapshot rotated back into place)
- doctor warning text directs to synthesize-log
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


def _write_event_line(events_path: Path, payload_dict: dict) -> None:
    """Append one JSONL event envelope. Mirrors the helper in
    tests/test_catalog_mcp_bootstrap_fallback.py."""
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a") as f:
        f.write(json.dumps(payload_dict, separators=(",", ":")))
        f.write("\n")


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture
def healthy_catalog(catalog_env, monkeypatch):
    """A catalog with parity between events.jsonl and documents.jsonl."""
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    Catalog.init(catalog_env)
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab1234")
    for i in range(3):
        cat.register(
            owner, f"doc-{i}.md",
            content_type="prose",
            file_path=f"doc-{i}.md",
        )
    cat._db.close()
    return catalog_env


@pytest.fixture
def fallback_catalog(catalog_env, monkeypatch):
    """A catalog wedged into bootstrap-fallback state.

    10 legacy DocumentRegistered rows in documents.jsonl, only one stray
    event in events.jsonl - sparse enough to trip the 95% guardrail.
    """
    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
    Catalog.init(catalog_env)
    cat = Catalog(catalog_env, catalog_env / ".catalog.db")
    owner = cat.register_owner("nexus", "repo", repo_hash="abab1234")
    for i in range(10):
        cat.register(
            owner, f"doc-{i}.md",
            content_type="prose",
            file_path=f"doc-{i}.md",
        )
    cat._db.close()

    events_path = catalog_env / "events.jsonl"
    _write_event_line(events_path, {
        "type": "DocumentRegistered", "v": 0,
        "payload": {
            "doc_id": "1.1.99", "owner_id": "1.1",
            "content_type": "prose", "source_uri": "",
            "coll_id": "", "title": "stray.md", "tumbler": "1.1.99",
            "author": "", "year": 0, "file_path": "stray.md",
            "corpus": "", "physical_collection": "",
            "chunk_count": 0, "head_hash": "", "indexed_at": "",
            "alias_of": "", "meta": {}, "source_mtime": 0.0,
            "indexed_at_doc": "",
        },
        "ts": "2026-05-01T00:00:00+00:00",
    })

    monkeypatch.setenv("NEXUS_EVENT_SOURCED", "1")
    return catalog_env


# ─────────────────────────────────────────────────────────────────────
# --check
# ─────────────────────────────────────────────────────────────────────


class TestCheckMode:
    def test_check_returns_0_when_not_in_fallback(self, healthy_catalog):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "synthesize-log", "--check"])
        assert result.exit_code == 0, result.output
        assert "fallback" in result.output.lower()

    def test_check_returns_1_when_fallback_active(self, fallback_catalog):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "synthesize-log", "--check"])
        assert result.exit_code == 1
        assert "fallback" in result.output.lower()

    def test_check_does_not_write(self, fallback_catalog):
        events_path = fallback_catalog / "events.jsonl"
        before = events_path.read_bytes()
        runner = CliRunner()
        runner.invoke(main, ["catalog", "synthesize-log", "--check"])
        after = events_path.read_bytes()
        assert before == after


# ─────────────────────────────────────────────────────────────────────
# --dry-run
# ─────────────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_prints_event_counts(self, fallback_catalog):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "synthesize-log", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "DocumentRegistered" in result.output
        assert "OwnerRegistered" in result.output
        assert "TOTAL" in result.output

    def test_dry_run_does_not_modify_events_log(self, fallback_catalog):
        events_path = fallback_catalog / "events.jsonl"
        before = events_path.read_bytes()
        runner = CliRunner()
        runner.invoke(main, ["catalog", "synthesize-log", "--dry-run"])
        after = events_path.read_bytes()
        assert before == after

    def test_dry_run_creates_no_snapshot(self, fallback_catalog):
        runner = CliRunner()
        runner.invoke(main, ["catalog", "synthesize-log", "--dry-run"])
        siblings = list(fallback_catalog.parent.iterdir())
        snapshots = [p for p in siblings if "synth-snapshot" in p.name]
        assert snapshots == []


# ─────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────


class TestHappyPath:
    def test_synthesize_clears_fallback(self, fallback_catalog):
        from nexus.commands.catalog import _check_bootstrap_status

        assert _check_bootstrap_status()["fallback_active"] is True

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "synthesize-log"])
        assert result.exit_code == 0, result.output

        post = _check_bootstrap_status()
        assert post["fallback_active"] is False, post

    def test_synthesize_retains_snapshot_on_pass(self, fallback_catalog):
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "synthesize-log"])
        assert result.exit_code == 0, result.output

        siblings = list(fallback_catalog.parent.iterdir())
        snapshots = [p for p in siblings if "synth-snapshot" in p.name]
        assert len(snapshots) == 1, [p.name for p in siblings]
        assert (snapshots[0] / "events.jsonl").exists()

    def test_synthesize_replay_equality_passes_after(self, fallback_catalog):
        from nexus.commands.catalog import _run_replay_equality

        runner = CliRunner()
        runner.invoke(main, ["catalog", "synthesize-log"])

        report = _run_replay_equality()
        assert report["pass"] is True, report

    def test_no_op_when_not_in_fallback(self, healthy_catalog):
        events_path = healthy_catalog / "events.jsonl"
        before = events_path.read_bytes()

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "synthesize-log"])
        assert result.exit_code == 0
        assert "no-op" in result.output.lower() or "not in fallback" in result.output.lower()

        assert events_path.read_bytes() == before


# ─────────────────────────────────────────────────────────────────────
# --force
# ─────────────────────────────────────────────────────────────────────


class TestForce:
    def _doc_id_map(self, events_path: Path) -> dict[str, str]:
        out: dict[str, str] = {}
        with events_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") != "DocumentRegistered":
                    continue
                payload = obj.get("payload") or {}
                tumbler = payload.get("tumbler")
                doc_id = payload.get("doc_id")
                if tumbler and doc_id:
                    out[tumbler] = doc_id
        return out

    def test_force_synthesizes_when_not_in_fallback(self, healthy_catalog):
        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "synthesize-log", "--force"]
        )
        assert result.exit_code == 0, result.output

    def test_force_preserves_doc_ids_across_runs(self, fallback_catalog):
        runner = CliRunner()
        # First synthesis: fallback -> healthy.
        r1 = runner.invoke(main, ["catalog", "synthesize-log"])
        assert r1.exit_code == 0, r1.output
        first_map = self._doc_id_map(fallback_catalog / "events.jsonl")
        assert first_map, "first synthesis produced no doc_id mappings"

        # Second synthesis with --force: doc_ids must round-trip.
        r2 = runner.invoke(main, ["catalog", "synthesize-log", "--force"])
        assert r2.exit_code == 0, r2.output
        second_map = self._doc_id_map(fallback_catalog / "events.jsonl")

        assert second_map == first_map, (
            "doc_ids drifted across --force re-synthesis "
            "(would invalidate downstream T3 metadata)"
        )


# ─────────────────────────────────────────────────────────────────────
# --no-verify
# ─────────────────────────────────────────────────────────────────────


class TestNoVerify:
    def test_no_verify_skips_replay_equality(self, fallback_catalog, monkeypatch):
        called = {"flag": False}

        def _spy():
            called["flag"] = True
            return {"pass": True}

        monkeypatch.setattr(
            "nexus.commands.catalog._run_replay_equality", _spy
        )
        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "synthesize-log", "--no-verify"]
        )
        assert result.exit_code == 0, result.output
        assert called["flag"] is False, "verify ran despite --no-verify"


# ─────────────────────────────────────────────────────────────────────
# Auto-restore
# ─────────────────────────────────────────────────────────────────────


class TestAutoRestore:
    def test_verify_failure_restores_snapshot(self, fallback_catalog, monkeypatch):
        events_path = fallback_catalog / "events.jsonl"
        pre = events_path.read_bytes()

        def _failing_verify():
            return {"pass": False, "reason": "synthetic test failure"}

        monkeypatch.setattr(
            "nexus.commands.catalog._run_replay_equality", _failing_verify
        )

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "synthesize-log"])
        assert result.exit_code == 1

        # Live catalog content matches pre-synthesis exactly.
        assert events_path.read_bytes() == pre

        siblings = list(fallback_catalog.parent.iterdir())
        snapshots = [p for p in siblings if "synth-snapshot" in p.name]
        failed = [p for p in siblings if "synth-failed" in p.name]
        assert len(snapshots) == 1, (
            f"expected snapshot retained, got: {[p.name for p in siblings]}"
        )
        assert len(failed) == 1, (
            f"expected failed-state retained, got: {[p.name for p in siblings]}"
        )


# ─────────────────────────────────────────────────────────────────────
# Doctor warning text update
# ─────────────────────────────────────────────────────────────────────


class TestDoctorWarningText:
    def test_warning_points_to_synthesize_log(self, fallback_catalog):
        runner = CliRunner()
        result = runner.invoke(
            main, ["catalog", "doctor", "--replay-equality"]
        )
        # Doctor warning is emitted on stderr; CliRunner merges by default.
        combined = (result.output or "") + (result.stderr or "" if result.stderr_bytes is not None else "")
        assert "synthesize-log" in combined, combined
        assert "delete the catalog directory" not in combined.lower(), (
            "stale lossy guidance still present"
        )
