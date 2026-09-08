"""Python and shell resolve the build-lease root identically (nexus-g6xpa).

``tests/db/_service_fixture.build_in_progress_reason`` reads the lease that
``scripts/lib/build-lease.sh`` writes. If the two ever resolved different
directories the reader would be blind to a real concurrent build — the
asymmetry nexus-06fu4 closed would be back, silently. So the resolution is
pinned from both sides here: the default (git common dir) and the
``NX_BUILD_LEASE_ROOT`` override.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.db import _service_fixture

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LIB = _REPO_ROOT / "scripts" / "lib" / "build-lease.sh"


def _shell_root(env: dict[str, str]) -> Path:
    out = subprocess.run(
        ["bash", "-c", f"source '{_LIB}'; _build_lease_root"],
        capture_output=True, text=True, check=True, env=env, timeout=30,
    ).stdout.strip()
    return Path(out).resolve()


def test_default_root_matches_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NX_BUILD_LEASE_ROOT", raising=False)
    env = {k: v for k, v in os.environ.items() if k != "NX_BUILD_LEASE_ROOT"}
    shell = _shell_root(env)
    py = _service_fixture._build_lease_root().resolve()
    assert py == shell
    common = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout.strip()
    assert py == (Path(common) / "nexus-build-lease").resolve()


def test_override_matches_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "lease-root"
    monkeypatch.setenv("NX_BUILD_LEASE_ROOT", str(override))
    env = dict(os.environ, NX_BUILD_LEASE_ROOT=str(override))
    assert _service_fixture._build_lease_root().resolve() == _shell_root(env)
    assert _service_fixture._build_lease_root() == override


def test_reader_sees_a_lease_the_shell_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """End to end: the shell acquires under an override root, the Python
    reader reports the build in progress from the same override."""
    override = tmp_path / "lease-root"
    monkeypatch.setenv("NX_BUILD_LEASE_ROOT", str(override))
    env = dict(os.environ, NX_BUILD_LEASE_ROOT=str(override), NX_AGENT="parity-test")
    holder = subprocess.Popen(
        ["bash", "-c", f"source '{_LIB}'; build_lease_acquire service || exit 9; sleep 30"],
        env=env,
    )
    try:
        deadline = 100
        while deadline and not (override / "service" / "pid").exists():
            deadline -= 1
            subprocess.run(["sleep", "0.1"], check=False)
        assert (override / "service" / "pid").exists(), "shell holder never acquired"
        reason = _service_fixture.build_in_progress_reason(override)
        assert reason is not None and "parity-test" in reason
    finally:
        holder.kill()
        holder.wait(timeout=10)
