# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the .mcpb bundle's resolve-with-retry bootstrap (nexus-r433b).

The Claude Desktop extension resolves ``conexus[local]>=X.Y.Z`` from PyPI on
first launch. PyPI's simple index lags the upload by ~10-25 minutes after a
release (four consecutive releases measured), so an install inside that
window used to die with a bare resolver error before any of our code ran —
the resolution happened inside ``uv run src/server.py`` itself.

``mcpb/src/bootstrap.py`` now owns the resolution: ``uv sync`` with bounded
backoff on exactly the propagation-window failure class, then exec of the
real server. These tests pin the retry loop's classification and bounds
(injected runner/sleeper — no real uv, no network) and the manifest wiring
that makes Desktop launch the bootstrap under ``--no-project`` (without
which uv would resolve the project BEFORE our retry code could run, which
is the exact defect this fixes).

The bootstrap is not part of the wheel (it ships only inside the .mcpb
zip), so it is loaded by file path rather than imported as a package.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_PATH = REPO_ROOT / "mcpb" / "src" / "bootstrap.py"
MANIFEST_PATH = REPO_ROOT / "mcpb" / "manifest.json"


@pytest.fixture(scope="module")
def bootstrap():
    spec = importlib.util.spec_from_file_location("mcpb_bootstrap", BOOTSTRAP_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── failure classification ──────────────────────────────────────────────────

_NO_SOLUTION_GE = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because only conexus<=7.24.1 is available and conexus-mcpb depends on
      conexus[local]>=7.25.0, we can conclude that conexus-mcpb's
      requirements are unsatisfiable.
"""

_NO_SOLUTION_EQ = """\
  × No solution found when resolving dependencies:
  ╰─▶ Because there is no version of conexus==7.25.0 and you require
      conexus==7.25.0, we can conclude that your requirements are
      unsatisfiable.
"""


def test_no_solution_ge_is_propagation_class(bootstrap):
    assert bootstrap._is_resolution_unavailable(_NO_SOLUTION_GE) is True


def test_no_solution_eq_is_propagation_class(bootstrap):
    assert bootstrap._is_resolution_unavailable(_NO_SOLUTION_EQ) is True


def test_registry_miss_is_propagation_class(bootstrap):
    text = "error: Package `conexus` was not found in the package registry"
    assert bootstrap._is_resolution_unavailable(text) is True


def test_other_failures_are_not_retried_class(bootstrap):
    # Network down, permissions, disk: NOT the propagation window.
    assert bootstrap._is_resolution_unavailable("error: Permission denied (os error 13)") is False
    assert (
        bootstrap._is_resolution_unavailable(
            "error: Failed to fetch: `https://pypi.org/simple/conexus/`\n"
            "  Caused by: Connection reset by peer"
        )
        is False
    )


def test_no_solution_about_another_package_is_not_ours(bootstrap):
    text = "× No solution found when resolving dependencies: no version of leftpad==1.0"
    assert bootstrap._is_resolution_unavailable(text) is False


# ── retry loop bounds (injected runner/sleeper — no uv, no network) ─────────


def _proc(rc: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)


class _Runner:
    def __init__(self, procs):
        self.procs = list(procs)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self.procs.pop(0)


def test_immediate_success_never_sleeps(bootstrap):
    runner = _Runner([_proc(0)])
    sleeps: list[float] = []
    bootstrap._sync_with_retry("/bundle", run=runner, sleep=sleeps.append)
    assert sleeps == []
    assert runner.calls == [["uv", "sync", "--directory", "/bundle"]]


def test_propagation_failures_retry_with_backoff_then_succeed(bootstrap):
    runner = _Runner([_proc(1, _NO_SOLUTION_GE), _proc(1, _NO_SOLUTION_GE), _proc(0)])
    sleeps: list[float] = []
    bootstrap._sync_with_retry(
        "/bundle", run=runner, sleep=sleeps.append, sleeps=(60, 120, 240, 480)
    )
    assert sleeps == [60, 120]
    assert len(runner.calls) == 3


def test_retries_are_bounded_and_exhaustion_fails_loud(bootstrap, capsys):
    schedule = (1, 2, 3)
    runner = _Runner([_proc(1, _NO_SOLUTION_GE)] * (len(schedule) + 1))
    sleeps: list[float] = []
    with pytest.raises(SystemExit) as exc:
        bootstrap._sync_with_retry("/bundle", run=runner, sleep=sleeps.append, sleeps=schedule)
    assert exc.value.code == 1
    assert sleeps == [1, 2, 3]
    assert len(runner.calls) == len(schedule) + 1
    err = capsys.readouterr().err
    assert "PyPI" in err and "propagat" in err
    # uv's own output surfaces so the terminal failure is diagnosable.
    assert "No solution found" in err


def test_non_propagation_failure_fails_immediately(bootstrap, capsys):
    runner = _Runner([_proc(13, "error: Permission denied (os error 13)")])
    sleeps: list[float] = []
    with pytest.raises(SystemExit) as exc:
        bootstrap._sync_with_retry("/bundle", run=runner, sleep=sleeps.append)
    assert exc.value.code == 13
    assert sleeps == []
    assert len(runner.calls) == 1
    assert "Permission denied" in capsys.readouterr().err


def test_default_schedule_spans_the_measured_window(bootstrap):
    """The measured propagation window is ~10-25 min; a user typically lands
    mid-window. The default backoff must cover at least 10 minutes so the
    common case rides it out rather than exhausting early."""
    assert sum(bootstrap._RETRY_SLEEPS) >= 600


# ── manifest wiring ─────────────────────────────────────────────────────────


def test_manifest_launches_bootstrap_with_no_project(bootstrap):
    """--no-project is load-bearing: without it, `uv run` resolves the
    bundle's project deps BEFORE bootstrap.py executes — the resolver
    failure then kills the extension before any retry code can run, which
    is the pre-r433b behavior this whole arrangement replaces."""
    manifest = json.loads(MANIFEST_PATH.read_text())
    server = manifest["server"]
    assert server["entry_point"] == "src/bootstrap.py"
    assert server["mcp_config"]["command"] == "uv"
    assert server["mcp_config"]["args"] == [
        "run",
        "--no-project",
        "--directory",
        "${__dirname}",
        "src/bootstrap.py",
    ]


def test_bundle_ships_both_bootstrap_and_server(bootstrap):
    assert BOOTSTRAP_PATH.exists()
    # The exec target must still exist — bootstrap hands off to it.
    assert (REPO_ROOT / "mcpb" / "src" / "server.py").exists()
    # And .mcpbignore must not exclude either (they live in src/, only
    # caches and lockfiles are excluded).
    ignore = (REPO_ROOT / "mcpb" / ".mcpbignore").read_text()
    assert "server.py" not in ignore
    assert "bootstrap.py" not in ignore


def test_bootstrap_execs_uv_run_server(bootstrap, monkeypatch):
    """main() syncs then execs the real server through uv run (stdio must
    land on the server process for the MCP handshake — exec, not spawn)."""
    execs: list[list[str]] = []
    monkeypatch.setattr(bootstrap.os, "execvp", lambda prog, argv: execs.append([prog, *argv]))
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: None)
    monkeypatch.delenv("NX_MCPB_SKIP_RESOLVE_RETRY", raising=False)
    bootstrap.main()
    bundle = str(Path(BOOTSTRAP_PATH).parent.parent)
    assert execs == [["uv", "uv", "run", "--directory", bundle, "src/server.py"]]


def test_skip_env_bypasses_sync(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.os, "execvp", lambda prog, argv: None)
    called = []
    monkeypatch.setattr(bootstrap, "_sync_with_retry", lambda d: called.append(d))
    monkeypatch.setenv("NX_MCPB_SKIP_RESOLVE_RETRY", "1")
    bootstrap.main()
    assert called == []
