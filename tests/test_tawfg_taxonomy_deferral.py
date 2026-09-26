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

import nexus.db.http_vector_client as hvc
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
def _clear_deferral(monkeypatch):
    # conftest zeroes the uptime threshold suite-wide; these tests exercise it.
    monkeypatch.delenv("NX_TAXONOMY_DEFER_UPTIME_S", raising=False)
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
    assert mcp_infra.taxonomy_failure_marker_path(nexus_config_dir()).exists()


def test_the_marker_is_per_engine_and_survives_token_rotation(tmp_path, monkeypatch) -> None:
    """A loss against one engine must not defer indexing against another
    engine on the same box (critic, round 1); a rotated token must not
    restart the backoff against the same, still-sick engine (round 2)."""
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://engine-a:1", "tok-a"))
    a = mcp_infra.taxonomy_failure_marker_path(tmp_path)
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://engine-b:1", "tok-a"))
    b = mcp_infra.taxonomy_failure_marker_path(tmp_path)
    monkeypatch.setattr(hvc, "_resolve_endpoint", lambda: ("http://engine-a:1/", "tok-rotated"))
    a_rotated = mcp_infra.taxonomy_failure_marker_path(tmp_path)

    assert a != b
    assert a == a_rotated
    assert "tok-a" not in a.name and "engine-a" not in a.name, "no raw endpoint or token on disk"

    record_taxonomy_failure(a, now=_NOW)
    assert "failed" in decide_taxonomy_deferral(uptime_fn=lambda: None, marker=a, now_fn=lambda: _NOW)
    assert decide_taxonomy_deferral(uptime_fn=lambda: None, marker=b, now_fn=lambda: _NOW) == ""


def test_thresholds_are_env_tunable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NX_TAXONOMY_DEFER_UPTIME_S", "60")
    assert _decide(tmp_path, 61) == ""
    assert "restarted" in _decide(tmp_path, 59)
    monkeypatch.setenv("NX_TAXONOMY_DEFER_UPTIME_S", "garbage")
    assert "restarted" in _decide(tmp_path, TAXONOMY_DEFER_UPTIME_S - 1), "bad value keeps the default"

    marker = tmp_path / "m"
    record_taxonomy_failure(marker, now=_NOW - 100)
    monkeypatch.setenv("NX_TAXONOMY_FAILURE_BACKOFF_S", "50")
    assert decide_taxonomy_deferral(uptime_fn=lambda: None, marker=marker, now_fn=lambda: _NOW) == ""


@pytest.mark.parametrize("payload,expected", [
    ({"process_uptime_seconds": 30}, 30),
    ({"process_uptime_seconds": 0}, 0),
    ({"process_uptime_seconds": True}, None),
    ({"process_uptime_seconds": "30"}, None),
    ({"process_uptime_seconds": 30.5}, None),
    ({}, None),
    (["not", "a", "dict"], None),
])
def test_uptime_probe_accepts_only_an_integer(monkeypatch, payload, expected) -> None:
    seen: dict = {}

    def _get(path, tenant=None):
        seen["path"], seen["tenant"] = path, tenant
        return payload

    monkeypatch.setattr(hvc, "_get", _get)
    assert mcp_infra.engine_process_uptime_seconds() == expected
    assert seen == {"path": "/version", "tenant": hvc._process_default_tenant()}


def test_uptime_probe_failure_is_no_evidence(monkeypatch) -> None:
    def _boom(path, tenant=None):
        raise ConnectionError("engine down")

    monkeypatch.setattr(hvc, "_get", _boom)
    assert mcp_infra.engine_process_uptime_seconds() is None


def test_a_future_dated_marker_defers_nothing(tmp_path) -> None:
    """Clock skew or a corrupted marker must not defer indefinitely."""
    record_taxonomy_failure(tmp_path / "m", now=_NOW + 3600)
    assert decide_taxonomy_deferral(
        uptime_fn=lambda: None, marker=tmp_path / "m", now_fn=lambda: _NOW,
    ) == ""


def test_no_taxonomy_still_defers_and_records(tmp_path, monkeypatch, t2_service_env) -> None:
    """--no-taxonomy skips discovery only; the per-flush assign still runs,
    so the deferral and the backoff marker apply to it (code review round 1)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo", "docs_collection": "docs__myrepo"}
    monkeypatch.setattr(mcp_infra, "engine_process_uptime_seconds", lambda: 30)

    def _lossy_index(*a, **kw):
        return {"files_changed": 1, "taxonomy_assign_batches_attempted": 1,
                "taxonomy_assign_batches_failed": 1, "taxonomy_assign_chunks_failed": 4}

    with patch("nexus.commands.index._registry", return_value=reg), \
            patch("nexus.indexer.index_repository", side_effect=_lossy_index):
        out = CliRunner().invoke(main, ["index", "repo", str(repo), "--no-taxonomy"])

    assert "Taxonomy deferred: engine restarted 30 s ago" in out.output, out.output
    assert out.exit_code != 0, out.output
    assert mcp_infra.taxonomy_failure_marker_path(nexus_config_dir()).exists()


def test_the_run_reports_how_many_chunks_it_deferred(tmp_path, monkeypatch, t2_service_env) -> None:
    def _index_with_deferral(*a, **kw):
        mcp_infra._record_taxonomy_deferred(7)
        return {"files_changed": 1}

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / ".git").mkdir()
    reg = MagicMock()
    reg.get.return_value = {"collection": "code__myrepo", "docs_collection": "docs__myrepo"}
    monkeypatch.setattr(mcp_infra, "engine_process_uptime_seconds", lambda: 30)
    mcp_infra.reset_taxonomy_assign_run_stats()
    with patch("nexus.commands.index._registry", return_value=reg), \
            patch("nexus.indexer.index_repository", side_effect=_index_with_deferral):
        out = CliRunner().invoke(main, ["index", "repo", str(repo)])

    assert out.exit_code == 0, out.output
    assert "Taxonomy: 7 chunk(s) deferred" in out.output, out.output


def test_discovery_waits_out_the_window_too(tmp_path, monkeypatch, t2_service_env) -> None:
    """nexus-x3gig: a run that changed files still skips discovery while
    deferred, and runs it when not."""
    calls: list = []
    monkeypatch.setattr(
        "nexus.commands.index.run_collection_postprocessing",
        lambda collections, **kw: calls.append(kw.get("discover_collections")),
    )
    monkeypatch.setattr(mcp_infra, "drain_unassigned_chunks", lambda name, **kw: DrainResult(name, True))

    def _run(uptime):
        calls.clear()
        monkeypatch.setenv("HOME", str(tmp_path))
        repo = tmp_path / "myrepo"
        repo.mkdir(exist_ok=True)
        (repo / ".git").mkdir(exist_ok=True)
        reg = MagicMock()
        reg.get.return_value = {"collection": "code__myrepo", "docs_collection": "docs__myrepo"}
        monkeypatch.setattr(mcp_infra, "engine_process_uptime_seconds", lambda: uptime)
        with patch("nexus.commands.index._registry", return_value=reg), \
                patch("nexus.indexer.index_repository", return_value={"files_changed": 3}):
            return CliRunner().invoke(main, ["index", "repo", str(repo)])

    out = _run(30)
    assert out.exit_code == 0, out.output
    # Deferred: postprocessing still runs (projection, links, L1) with an
    # empty discovery list (substantive critic: skipping the whole chain
    # left the L1 cache stale).
    assert calls == [[]], calls

    out = _run(None)
    assert out.exit_code == 0, out.output
    assert calls == [None] or (calls and calls[0] != []), calls
