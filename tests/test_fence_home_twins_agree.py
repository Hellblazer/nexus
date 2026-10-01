# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The shell and Python HOME fences must produce the same shape.

nexus-pfuns. Two implementations exist deliberately: ``tests/e2e/lib/
fence_home.sh`` for the gate scripts (which run before ``uv sync`` on some
paths and cannot depend on a Python import) and ``tests/_fence_home.py`` for the
unit suite. Two copies of one rule drift until the stale one wins an argument it
should not, so this pins the contract rather than trusting the comments.

The rule: mirror EVERY top-level entry of the real home, recreate ``.config``
as a real directory mirroring its own entries, and shadow ``.config/nexus``,
``.config/systemd`` and ``Library/LaunchAgents`` (nexus-q81g7: the OS autostart
unit dirs) as fresh empty directories.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._fence_home import fence_home

_SHELL_FENCE = Path(__file__).parent / "e2e" / "lib" / "fence_home.sh"


def _seed(real: Path) -> None:
    """A home containing the entries that actually mattered on 2026-08-24."""
    for rel in (".docker/run", ".m2/repository", ".cache/uv", ".local/bin",
                ".claude/plugins", ".config/nexus", ".config/gh", "Documents",
                ".config/systemd/user", "Library/LaunchAgents", "Library/Caches"):
        (real / rel).mkdir(parents=True, exist_ok=True)
    (real / ".testcontainers.properties").write_text("testcontainers.ryuk.disabled=true\n")
    (real / ".config" / "nexus" / "last_seen_version").write_text("7.18.0\n")


def _shape(home: Path) -> set[str]:
    """(name, is-symlink) for the mirror's top level plus its .config level."""
    out = set()
    for p in sorted(home.iterdir()):
        out.add(f"{p.name}:{'link' if p.is_symlink() else 'dir'}")
    for top in (".config", "Library"):
        d = home / top
        if d.is_dir():
            for p in sorted(d.iterdir()):
                out.add(f"{top}/{p.name}:{'link' if p.is_symlink() else 'dir'}")
    return out


@pytest.mark.skipif(not _SHELL_FENCE.exists(), reason="fence_home.sh missing")
def test_shell_and_python_fences_produce_identical_shapes(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _seed(real)

    py_home = tmp_path / "py"
    fence_home(real, py_home, ".config/nexus")

    sh_home = tmp_path / "sh"
    r = subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; fence_home "{real}" "{sh_home}" ".config/nexus"'],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, f"shell fence failed: {r.stderr}"

    py, sh = _shape(py_home), _shape(sh_home)
    assert py == sh, (
        "the two fence implementations have drifted:\n"
        f"  python only: {sorted(py - sh)}\n"
        f"  shell only:  {sorted(sh - py)}"
    )


def test_python_fence_shadows_only_the_nexus_config_and_the_autostart_dirs(tmp_path: Path) -> None:
    real = tmp_path / "real"; real.mkdir(); _seed(real)
    home = fence_home(real, tmp_path / "fenced", ".config/nexus")

    # the shadowed leaf is a REAL empty dir, not a passthrough
    shadowed = home / ".config" / "nexus"
    assert shadowed.is_dir() and not shadowed.is_symlink()
    assert not (shadowed / "last_seen_version").exists()
    # everything else passes through
    for entry in (".docker", ".testcontainers.properties", ".m2", ".cache",
                  ".local", ".claude", "Documents", ".config/gh"):
        assert (home / entry).exists(), f"{entry} did not survive the mirror"


def test_install_fence_is_idempotent_and_records_the_real_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An xdist worker inherits the fenced HOME. A second fence there would
    mirror the MIRROR, and the guard would lose the operator's real path."""
    from tests._fence_home import REAL_HOME_ENV, install_fence

    real = tmp_path / "real"; real.mkdir(); _seed(real)
    # ``patch.dict`` restores EVERY variable install_fence sets (NX_FENCED_HOME,
    # XDG_RUNTIME_DIR, the DBUS pop, NX_TOOLS_DIR ...). monkeypatch restored only
    # the two this test named, so the rest leaked into the session and the
    # finish-time guards read the fenced mirror (nexus-q81g7 review).
    with patch.dict(os.environ):
        monkeypatch.setenv("HOME", str(real))
        monkeypatch.delenv(REAL_HOME_ENV, raising=False)

        first = install_fence(tmp_path / "f1")
        assert first is not None
        assert Path(os.environ[REAL_HOME_ENV]).resolve() == real.resolve()
        assert Path(os.environ["HOME"]) == tmp_path / "f1"

        second = install_fence(tmp_path / "f2")
        assert second is None, "a second fence installed over an already-fenced HOME"
        assert Path(os.environ["HOME"]) == tmp_path / "f1", "HOME was re-pointed"


import os  # noqa: E402 — used by the idempotence test above


# ── nexus-q81g7: the shadow LIST and the manager env half must agree too ────

_MANAGER_KEYS = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "DOCKER_HOST")


def _shell_shadows() -> set[str]:
    r = subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; printf "%s\\n" "${{FENCE_ALWAYS_SHADOWS[@]}}"'],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return {line for line in r.stdout.splitlines() if line}


def test_the_shadow_lists_agree_and_never_mirror_a_credential_store() -> None:
    from tests._fence_home import ALWAYS_SHADOWS

    assert _shell_shadows() == set(ALWAYS_SHADOWS)
    for must_not_mirror in (".aws", ".gnupg", ".kube", ".ssh", ".config/gh", ".config/op"):
        assert must_not_mirror in ALWAYS_SHADOWS, f"{must_not_mirror} would be mirrored"
    for interim in (".local/state", ".claude/agents", ".claude/projects"):
        assert interim in ALWAYS_SHADOWS


def test_credential_stores_are_empty_in_both_fences(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    for rel in (".aws", ".ssh", ".gnupg", ".kube", ".config/gh", ".config/op",
                ".local/state/nexus", ".local/bin", ".claude/agents", ".claude/projects",
                ".claude/plugins"):
        (real / rel).mkdir(parents=True)
        (real / rel / "secret").write_text("x")
    py = fence_home(real, tmp_path / "py", ".config/nexus")
    sh = tmp_path / "sh"
    r = subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; fence_home "{real}" "{sh}" ".config/nexus"'],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    for home in (py, sh):
        for rel in (".aws", ".ssh", ".gnupg", ".kube", ".config/gh", ".config/op",
                    ".local/state", ".claude/agents", ".claude/projects"):
            d = home / rel
            assert d.is_dir() and not d.is_symlink() and list(d.iterdir()) == [], f"{home}: {rel}"
        # siblings of a shadowed leaf still mirror through
        assert (home / ".local" / "bin" / "secret").exists()
        assert (home / ".claude" / "plugins" / "secret").exists()


def _python_manager_env(tmp_path: Path, real_runtime: Path, platform: str, docker_host: str | None):
    from tests._fence_home import fence_manager_env

    gate = tmp_path / "py-gate"
    with patch.dict(os.environ), patch("tests._fence_home.sys.platform", platform):
        os.environ["XDG_RUNTIME_DIR"] = str(real_runtime)
        os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={real_runtime}/bus"
        os.environ.pop("DOCKER_HOST", None)
        if docker_host:
            os.environ["DOCKER_HOST"] = docker_host
        fence_manager_env(gate)
        return {k: os.environ.get(k) for k in _MANAGER_KEYS}, gate


def _shell_manager_env(tmp_path: Path, real_runtime: Path, uname: str, docker_host: str | None):
    gate = tmp_path / "sh-gate"
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = str(real_runtime)
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={real_runtime}/bus"
    env["NX_FENCE_UNAME"] = uname
    env.pop("DOCKER_HOST", None)
    if docker_host:
        env["DOCKER_HOST"] = docker_host
    script = (
        f'source "{_SHELL_FENCE}"; fence_home_env "{gate}"; '
        'for k in XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS DOCKER_HOST NX_FENCED_HOME; do '
        'if [ -n "${!k+x}" ]; then printf "%s=%s\\n" "$k" "${!k}"; else printf "%s=\\n" "$k"; fi; done'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = dict(line.split("=", 1) for line in r.stdout.splitlines())
    return {k: (out[k] or None) for k in _MANAGER_KEYS}, out["NX_FENCED_HOME"], gate


@pytest.mark.parametrize("docker_host", [None, "unix:///custom/docker.sock"])
@pytest.mark.parametrize("platform,uname", [("linux", "Linux"), ("darwin", "Darwin")])
def test_the_manager_env_half_agrees_between_the_twins(
    tmp_path: Path, platform: str, uname: str, docker_host: str | None,
) -> None:
    real_runtime = tmp_path / "run-user-1000"
    real_runtime.mkdir()
    py, py_gate = _python_manager_env(tmp_path, real_runtime, platform, docker_host)
    sh, fenced, sh_gate = _shell_manager_env(tmp_path, real_runtime, uname, docker_host)

    def norm(env, gate):
        return {k: (v.replace(str(gate), "<GATE>") if v else v) for k, v in env.items()}

    assert norm(py, py_gate) == norm(sh, sh_gate)
    assert fenced == str(sh_gate)
    # the substance, so agreement on two empty answers cannot pass: the real
    # bus is gone everywhere, and the runtime dir is fenced on Linux only
    assert py["DBUS_SESSION_BUS_ADDRESS"] is None
    if platform == "linux":
        assert py["XDG_RUNTIME_DIR"] == str(py_gate / "xdg-runtime")
    else:
        assert py["XDG_RUNTIME_DIR"] == str(real_runtime)
    assert py["DOCKER_HOST"] == docker_host


def test_a_rootless_docker_socket_survives_the_runtime_dir_fence(tmp_path: Path) -> None:
    """The real XDG_RUNTIME_DIR holds the rootless docker socket; fencing the
    variable must pin DOCKER_HOST first (both twins) or Testcontainers loses
    its daemon."""
    import socket as _socket

    real_runtime = Path(tempfile.mkdtemp(prefix="xr", dir="/tmp"))  # AF_UNIX path length limit
    sock_path = real_runtime / "docker.sock"
    srv = _socket.socket(_socket.AF_UNIX)
    srv.bind(str(sock_path))
    try:
        py, _ = _python_manager_env(tmp_path, real_runtime, "linux", None)
        sh, _, _ = _shell_manager_env(tmp_path, real_runtime, "Linux", None)
    finally:
        srv.close()
        sock_path.unlink(missing_ok=True)
        real_runtime.rmdir()
    assert py["DOCKER_HOST"] == f"unix://{sock_path}"
    assert sh["DOCKER_HOST"] == f"unix://{sock_path}"
