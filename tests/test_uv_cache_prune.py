# SPDX-License-Identifier: AGPL-3.0-or-later
"""``uv cache prune`` waits a few seconds for uv's cache lock, then moves to
the background (nexus-mjmjl: Sam's upgrade sat silent for the prune's whole
600 s bound while another uv process held the cache)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus.progress import uv_cache_prune
from nexus.bounded_subprocess import run_bounded

BUSY = (
    "Cache is currently in-use, waiting for other uv processes to finish (use `--force` to override)\n"
    "error: Timeout (15s) when waiting for lock on `/c` at `c/.lock`, is another uv process running?"
)


def _is_prune(argv) -> bool:
    """``uv cache prune``, with the head bare or resolved off the cwd (Windows)."""
    return Path(argv[0]).stem.lower() == "uv" and list(argv[1:]) == ["cache", "prune"]


def _done(rc: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["uv"], rc, stdout, stderr)


def test_the_foreground_prune_waits_only_briefly_for_the_lock(tmp_path: Path) -> None:
    seen: dict = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return _done(0, "Removed 12 files (3.4MiB)")

    line = uv_cache_prune.prune(run=run, spawn=None, config_dir=tmp_path, environ={"PATH": "/bin"})
    assert _is_prune(seen["argv"])
    assert seen["env"]["UV_LOCK_TIMEOUT"] == str(uv_cache_prune.LOCK_WAIT_S)
    assert seen["env"]["PATH"] == "/bin"
    assert line == "uv cache prune: Removed 12 files (3.4MiB)"


def test_a_busy_cache_moves_the_prune_to_the_background_and_says_where_it_logs(tmp_path: Path) -> None:
    spawned: list = []

    def spawn(argv, env, log):
        spawned.append((_is_prune(argv), env["UV_LOCK_TIMEOUT"], Path(log.name)))

    line = uv_cache_prune.prune(run=lambda *a, **k: _done(2, BUSY), spawn=spawn, config_dir=tmp_path, environ={})
    log = tmp_path / "logs" / "uv-cache-prune.log"
    assert spawned == [(True, str(uv_cache_prune.BACKGROUND_LOCK_WAIT_S), log)]
    assert "another uv process is using uv's cache" in line
    assert "continues in the background" in line and str(log) in line


def test_without_a_spawner_it_says_what_to_run_later(tmp_path: Path) -> None:
    line = uv_cache_prune.prune(run=lambda *a, **k: _done(2, BUSY), spawn=None, config_dir=tmp_path, environ={})
    assert line.startswith("uv cache prune skipped: another uv process is using uv's cache")
    assert "nx self gc --prune-uv-cache" in line


def test_a_spawn_failure_is_reported_not_raised(tmp_path: Path) -> None:
    def spawn(argv, env, log):
        raise OSError("no such file: uv")

    line = uv_cache_prune.prune(run=lambda *a, **k: _done(2, BUSY), spawn=spawn, config_dir=tmp_path, environ={})
    assert "background prune could not start (no such file: uv)" in line


def test_another_failure_is_reported_as_before(tmp_path: Path) -> None:
    line = uv_cache_prune.prune(
        run=lambda *a, **k: _done(2, "error: permission denied"), spawn=None, config_dir=tmp_path, environ={},
    )
    assert line == "uv cache prune failed (rc=2): error: permission denied"


def test_no_uv_on_path_is_a_skip(tmp_path: Path) -> None:
    def run(*a, **k):
        raise FileNotFoundError("uv")

    assert uv_cache_prune.prune(run=run, spawn=None, config_dir=tmp_path, environ={}).startswith(
        "uv cache prune skipped:"
    )


@pytest.mark.parametrize(("stderr", "busy"), [(BUSY, True), ("error: permission denied", False), ("", False)])
def test_cache_busy(stderr: str, busy: bool) -> None:
    assert uv_cache_prune.cache_busy(stderr) is busy


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv on PATH")
def test_a_real_uv_holding_the_cache_makes_the_prune_return_in_seconds(tmp_path: Path, monkeypatch) -> None:
    """Real uv, real lock: a ``uv run`` holds a private cache open, and the
    prune returns after the short lock wait instead of blocking."""
    cache = tmp_path / "uv-cache"
    env = {**__import__("os").environ, "UV_CACHE_DIR": str(cache)}
    holder = subprocess.Popen(  # noqa: S603 — fixed argv
        ["uv", "run", "--no-project", "--python", sys.executable, "python", "-c", "import time; time.sleep(60)"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (cache / ".lock").exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert (cache / ".lock").exists(), "the holder never opened the cache"
        monkeypatch.setattr(uv_cache_prune, "LOCK_WAIT_S", 2)
        spawned: list = []
        started = time.monotonic()
        line = uv_cache_prune.prune(
            run=run_bounded, spawn=lambda argv, e, log: spawned.append(_is_prune(argv)), config_dir=tmp_path, environ=env,
        )
        took = time.monotonic() - started
    finally:
        holder.kill()
        holder.wait(timeout=10)
    assert took < 20, took
    assert spawned == [True], line
    assert "continues in the background" in line


def test_a_live_background_prune_is_not_started_twice(tmp_path: Path) -> None:
    spawned: list = []

    class Child:
        pid = 4242

    def spawn(argv, env, log):
        spawned.append(argv)
        return Child()

    busy = lambda *a, **k: _done(2, BUSY)  # noqa: E731 — a one-line fake
    first = uv_cache_prune.prune(run=busy, spawn=spawn, config_dir=tmp_path, environ={}, alive=lambda pid: pid == 4242)
    second = uv_cache_prune.prune(run=busy, spawn=spawn, config_dir=tmp_path, environ={}, alive=lambda pid: pid == 4242)
    assert len(spawned) == 1
    assert "gives up after 6 h" in first
    assert "already waiting" in second
    third = uv_cache_prune.prune(run=busy, spawn=spawn, config_dir=tmp_path, environ={}, alive=lambda _pid: False)
    assert len(spawned) == 2 and "continues in the background" in third
