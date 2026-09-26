# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-dotwy: scripts/ci_board_post.py publishes CI state to board/ci-develop.

The posting tests run the script's ``main`` against the REAL engine substrate
(the ``t2_service_env`` tenant stands in for the CI token) and read the tuple
back through the tuple store, so the wire shape is proven against the
engine's own ``board/<topic>`` template, not a stub.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci_board_post.py"
spec = importlib.util.spec_from_file_location("ci_board_post", _SCRIPT)
cbp = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(cbp)

_SHA = "a" * 40


# ── pure: the verdict fold and the body cap ──────────────────────────────────


@pytest.mark.parametrize(
    ("results", "want"),
    [
        ({"a": "success", "b": "skipped"}, ("success", [])),
        ({"a": "success", "b": "failure"}, ("failure", ["b"])),
        ({"a": "cancelled", "b": "success"}, ("cancelled", ["a"])),
        ({"a": "cancelled", "b": "failure"}, ("failure", ["b", "a"])),
    ],
)
def test_verdict_fold(results, want) -> None:
    assert cbp.verdict_from_results(results) == want


def test_body_trims_failed_jobs_to_fit_the_template_cap() -> None:
    failed = [f"job-{i:03d}-" + "x" * 40 for i in range(60)]
    body = cbp.build_body(sha=_SHA, run="1", attempt="1", workflow="CI",
                          conclusion="failure", failed=failed, url="u")
    assert len(body.encode()) <= cbp.MAX_BODY_BYTES
    parsed = json.loads(body)
    assert parsed["conclusion"] == "failure"
    assert 0 < len(parsed["failed"]) < len(failed)


def test_empty_results_is_an_argument_error(monkeypatch) -> None:
    monkeypatch.setenv("NX_SERVICE_URL", "http://unused.invalid")
    monkeypatch.setenv("NX_BOARD_TOKEN", "t")
    assert cbp.main(["--kind", "ci-verdict", "--sha", _SHA, "--run", "1",
                     "--results", "{}"]) == 2


def test_missing_credentials_warn_and_do_not_fail(monkeypatch, capsys) -> None:
    monkeypatch.delenv("NX_SERVICE_URL", raising=False)
    monkeypatch.delenv("NX_BOARD_TOKEN", raising=False)
    assert cbp.main(["--kind", "ci-pending", "--sha", _SHA, "--run", "1"]) == 0
    assert "::warning" in capsys.readouterr().out


# ── against the real engine ─────────────────────────────────────────────────


def _board(tenant: str) -> list:
    from nexus.db.t2.http_tuple_store import HttpTupleStore

    store = HttpTupleStore(tenant=tenant)
    try:
        return store.rd(cbp.SUBSPACE, {"topic": cbp.TOPIC}, n=50)
    finally:
        store.close()


def test_pending_then_verdict_land_on_the_board(t2_service_env, monkeypatch, capsys) -> None:
    monkeypatch.setenv("NX_BOARD_TOKEN", os.environ["NX_SERVICE_TOKEN"])
    run = "36250000001"
    assert cbp.main(["--kind", "ci-pending", "--sha", _SHA, "--run", run]) == 0
    needs = {"pytest-gate": {"result": "success", "outputs": {}},
             "write-seam-gate": {"result": "failure", "outputs": {}},
             "ca3-pgvector-bundle-macos": {"result": "skipped", "outputs": {}}}
    assert cbp.main(["--kind", "ci-verdict", "--sha", _SHA, "--run", run,
                     "--url", "https://github.com/x/y/actions/runs/" + run,
                     "--results", json.dumps(needs)]) == 0
    out = capsys.readouterr().out
    assert "::warning" not in out, out

    rows = [r for r in _board(t2_service_env) if json.loads(r.body)["run"] == run]
    by_kind = {r.dims["kind"]: json.loads(r.body) for r in rows}
    assert set(by_kind) == {"ci-pending", "ci-verdict"}
    assert by_kind["ci-pending"]["conclusion"] == "pending"
    verdict = by_kind["ci-verdict"]
    assert verdict["sha"] == _SHA
    assert verdict["conclusion"] == "failure"
    assert verdict["failed"] == ["write-seam-gate"]
    assert all(r.dims["from"] == "ci" for r in rows)


def test_a_retried_post_lands_on_the_same_tuple(t2_service_env, monkeypatch) -> None:
    monkeypatch.setenv("NX_BOARD_TOKEN", os.environ["NX_SERVICE_TOKEN"])
    argv = ["--kind", "ci-pending", "--sha", "b" * 40, "--run", "36250000002"]
    assert cbp.main(argv) == 0
    assert cbp.main(argv) == 0
    rows = [r for r in _board(t2_service_env) if json.loads(r.body)["run"] == "36250000002"]
    assert len(rows) == 1


def test_a_refused_token_warns_without_echoing_it(t2_service_env, monkeypatch, capsys) -> None:
    bad = "not-a-real-board-token-0000000000000000000000"
    monkeypatch.setenv("NX_BOARD_TOKEN", bad)
    assert cbp.main(["--kind", "ci-pending", "--sha", _SHA, "--run", "3"]) == 0
    out = capsys.readouterr().out
    assert "::warning" in out and "HTTP 401" in out
    assert bad not in out


# ── ci.yml wiring ────────────────────────────────────────────────────────────

#: Jobs that never run on a develop push, so the verdict cannot wait on them.
_PR_ONLY_JOBS = {"release-ledger-gate"}


def test_verdict_job_waits_on_every_job_a_develop_push_runs() -> None:
    """A job added to ci.yml later must join board-verdict's needs, or the
    posted verdict would say success without it."""
    import yaml

    wf = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
    jobs = yaml.safe_load(wf.read_text())["jobs"]
    expected = set(jobs) - {"board-pending", "board-verdict"} - _PR_ONLY_JOBS
    assert set(jobs["board-verdict"]["needs"]) == expected
    assert jobs["board-verdict"]["if"].startswith("always() && ")
    for name in ("board-pending", "board-verdict"):
        assert "refs/heads/develop" in jobs[name]["if"]
        assert "NX_BOARD_TOKEN" in str(jobs[name]["steps"])
