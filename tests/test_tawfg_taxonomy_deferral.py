# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tawfg (indexing-brittleness P0.3): the index run defers taxonomy
assignment when the engine is freshly restarted or assign failed recently.

Measured cause (nexus-mg8gx, 2026-09-25): 800 chunks lost their topic in the
minutes after engine-service-v0.1.131 went live, cold cache; r0vkh's 782 s
pool wedge was the same shape. Deferring is safe only because the nexus-iygza
drain assigns any chunk left without a topic on a later run, so a deferred
assign is neither attempted nor counted as a loss.

Engine uptime is read from /version's process_uptime_seconds, which the
engine documents as sound for EXCLUSION only (low uptime means a recent
restart; high uptime does not mean a warm cache). An absent or non-integer
field therefore defers nothing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import nexus.mcp_infra as mcp_infra
from nexus.cli import main
from nexus.config import nexus_config_dir
from nexus.mcp_infra import (
    TAXONOMY_DEFER_UPTIME_S,
    TAXONOMY_FAILURE_BACKOFF_S,
    DrainResult,
    decide_taxonomy_deferral,
    drain_unassigned_chunks,
    record_taxonomy_failure,
)

_NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _clear_deferral():
    mcp_infra.set_taxonomy_deferral("")
    yield
    mcp_infra.set_taxonomy_deferral("")


def _decide(tmp_path, uptime):
    return decide_taxonomy_deferral(
        uptime_fn=lambda: uptime, marker=tmp_path / "taxonomy_assign_failed_at", now_fn=lambda: _NOW,
    )


# ── the decision ──────────────────────────────────────────────────────────────


def test_a_fresh_restart_defers(tmp_path) -> None:
    reason = _decide(tmp_path, TAXONOMY_DEFER_UPTIME_S - 1)
    assert "restarted" in reason


def test_a_warm_or_unknown_engine_does_not_defer(tmp_path) -> None:
    assert _decide(tmp_path, TAXONOMY_DEFER_UPTIME_S) == ""
    assert _decide(tmp_path, None) == "", "absent uptime is no evidence of a restart"


def test_a_recent_failure_defers_and_an_old_one_does_not(tmp_path) -> None:
    marker = tmp_path / "taxonomy_assign_failed_at"
    record_taxonomy_failure(marker, now=_NOW - TAXONOMY_FAILURE_BACKOFF_S + 5)
    assert "failed" in _decide(tmp_path, None)
    record_taxonomy_failure(marker, now=_NOW - TAXONOMY_FAILURE_BACKOFF_S - 5)
    assert _decide(tmp_path, None) == ""


def test_an_unreadable_marker_defers_nothing(tmp_path) -> None:
    (tmp_path / "taxonomy_assign_failed_at").write_text("not a number")
    assert _decide(tmp_path, None) == ""


# ── what deferral does ────────────────────────────────────────────────────────


def _no_assign(*a, **kw):
    raise AssertionError("assign must not be attempted while deferred")


def test_the_flush_hook_skips_assign_while_deferred(monkeypatch) -> None:
    monkeypatch.setattr(mcp_infra, "_assign_from_chashes_with_retry", _no_assign)
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda _t3: True)
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: object())
    mcp_infra.reset_taxonomy_assign_run_stats()
    mcp_infra.set_taxonomy_deferral("engine restarted 30 s ago")

    mcp_infra.taxonomy_assign_batch_hook(["a" * 64], "docs__c", [], None, None)

    stats = mcp_infra.taxonomy_assign_run_stats()
    assert stats["attempted"] == 0 and stats["failed_batches"] == 0
    assert stats["deferred_chunks"] == 1


def test_a_lost_batch_trips_the_breaker_for_the_rest_of_the_run(monkeypatch) -> None:
    calls: list[int] = []

    def _lossy(collection, doc_ids, **kw):
        calls.append(len(doc_ids))
        return {"assigned": 0}, list(doc_ids), ["HTTP 500"]

    monkeypatch.setattr(mcp_infra, "_assign_from_chashes_with_retry", _lossy)
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda _t3: True)
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: object())
    monkeypatch.setattr(mcp_infra, "_record_taxonomy_tripwire", lambda *a, **kw: None)
    mcp_infra.reset_taxonomy_assign_run_stats()
    mcp_infra.set_taxonomy_deferral("", arm_breaker=True)

    mcp_infra.taxonomy_assign_batch_hook(["a" * 64, "b" * 64], "docs__c", [], None, None)
    mcp_infra.taxonomy_assign_batch_hook(["c" * 64], "docs__c", [], None, None)

    assert calls == [2], "the second batch must not be sent to an engine that just lost one"
    assert "failed" in mcp_infra.taxonomy_deferral()
    assert mcp_infra.taxonomy_assign_run_stats()["deferred_chunks"] == 1


def test_an_unarmed_process_never_trips_the_breaker(monkeypatch) -> None:
    """The MCP server calls the same hook for store_put; one failure there
    must not switch its assigns off for the life of the process."""
    monkeypatch.setattr(
        mcp_infra, "_assign_from_chashes_with_retry",
        lambda collection, doc_ids, **kw: ({"assigned": 0}, list(doc_ids), ["HTTP 500"]),
    )
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda _t3: True)
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: object())
    monkeypatch.setattr(mcp_infra, "_record_taxonomy_tripwire", lambda *a, **kw: None)

    mcp_infra.taxonomy_assign_batch_hook(["a" * 64], "docs__c", [], None, None)

    assert mcp_infra.taxonomy_deferral() == ""


def test_the_drain_skips_while_deferred() -> None:
    mcp_infra.set_taxonomy_deferral("engine restarted 30 s ago")
    result = drain_unassigned_chunks("docs__c")
    assert result.found == 0 and "deferred" in result.skipped_reason


# ── the index run ─────────────────────────────────────────────────────────────


def _index_repo(tmp_path, monkeypatch, *, uptime):
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo", "docs_collection": "docs__myrepo"}
    monkeypatch.setattr(mcp_infra, "engine_process_uptime_seconds", lambda: uptime)
    mcp_infra.reset_taxonomy_assign_run_stats()
    with patch("nexus.commands.index._registry", return_value=reg), \
            patch("nexus.indexer.index_repository", return_value={"files_changed": 0}):
        return CliRunner().invoke(main, ["index", "repo", str(repo)])


def test_index_repo_defers_after_a_restart_and_says_so(tmp_path, monkeypatch, t2_service_env) -> None:
    drained: list[str] = []
    monkeypatch.setattr(
        mcp_infra, "drain_unassigned_chunks",
        lambda name, **kw: drained.append(name) or DrainResult(name, True),
    )

    out = _index_repo(tmp_path, monkeypatch, uptime=30)

    assert out.exit_code == 0, out.output
    assert "Taxonomy deferred: engine restarted 30 s ago" in out.output, out.output
    assert drained == [], "the drain is deferred too"
    assert mcp_infra.taxonomy_deferral() == "", "the flag does not outlive the command"


def test_index_repo_runs_taxonomy_on_a_warm_engine(tmp_path, monkeypatch, t2_service_env) -> None:
    drained: list[str] = []
    monkeypatch.setattr(
        mcp_infra, "drain_unassigned_chunks",
        lambda name, **kw: drained.append(name) or DrainResult(name, True),
    )

    out = _index_repo(tmp_path, monkeypatch, uptime=TAXONOMY_DEFER_UPTIME_S * 10)

    assert out.exit_code == 0, out.output
    assert "Taxonomy deferred" not in out.output
    assert drained == ["docs__myrepo"]


def test_index_repo_records_a_loss_for_the_next_run(tmp_path, monkeypatch, t2_service_env) -> None:
    def _lossy(name, **kw):
        mcp_infra._record_taxonomy_assign_attempt()
        mcp_infra._record_taxonomy_assign_batch_failure(2)
        return DrainResult(name, True, found=2, lost=2)

    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", _lossy)

    out = _index_repo(tmp_path, monkeypatch, uptime=None)

    assert out.exit_code != 0, out.output
    assert (nexus_config_dir() / "taxonomy_assign_failed_at").exists()
