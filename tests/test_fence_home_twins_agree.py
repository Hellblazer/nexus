# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The shell and Python HOME fences must produce the same shape.

nexus-pfuns. Two implementations exist deliberately: ``tests/e2e/lib/
fence_home.sh`` for the gate scripts (which run before ``uv sync`` on some
paths and cannot depend on a Python import) and ``tests/_fence_home.py`` for the
unit suite. Two copies of one rule drift until the stale one wins an argument it
should not, so this pins the contract rather than trusting the comments.

The rule (nexus-ld3aa): the fenced home starts EMPTY and only the entries of one
declared allowlist (``MIRRORED_ENTRIES`` / ``FENCE_MIRRORED_ENTRIES``) that the
real home has are linked through. Credential stores are never on it.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tests._fence_home import MIRRORED_ENTRIES, NEVER_MIRRORED, fence_home
from tests._module_seam import patch_in, setattr_in

_SHELL_FENCE = Path(__file__).parent / "e2e" / "lib" / "fence_home.sh"


def _seed(real: Path) -> None:
    """A home holding the allowlisted caches AND the things the fence must hide."""
    for rel in (".docker/run", ".m2/repository", ".gradle/caches", ".cargo/registry", ".rustup/toolchains",
                ".cache/uv", ".local/bin", ".local/state/nexus", ".claude/plugins", ".claude/agents",
                ".config/nexus", ".config/gh", ".config/op", ".config/1Password", ".config/git",
                ".config/systemd/user", "Library/LaunchAgents", "Library/Caches", "Library/Keychains",
                ".ssh", ".aws", ".gnupg", ".kube", ".dolt", ".beads", "Documents"):
        (real / rel).mkdir(parents=True, exist_ok=True)
    (real / ".testcontainers.properties").write_text("testcontainers.ryuk.disabled=true\n")
    for rel in (".gitconfig", ".netrc", ".npmrc", ".claude.json"):
        (real / rel).write_text("secret\n")
    (real / ".config" / "nexus" / "last_seen_version").write_text("7.18.0\n")


def _shape(home: Path) -> set[str]:
    """Every top-level entry of the fenced home as ``name:link|dir|file``."""
    out = set()
    for p in sorted(home.iterdir()):
        out.add(f"{p.name}:{'link' if p.is_symlink() else 'dir' if p.is_dir() else 'file'}")
    return out


def _shell_fence(real: Path, gate: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; fence_home "{real}" "{gate}"'],
        capture_output=True, text=True,
    )


@pytest.mark.skipif(not _SHELL_FENCE.exists(), reason="fence_home.sh missing")
def test_shell_and_python_fences_produce_identical_shapes(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    _seed(real)

    py_home = tmp_path / "py"
    fence_home(real, py_home)

    sh_home = tmp_path / "sh"
    r = _shell_fence(real, sh_home)
    assert r.returncode == 0, f"shell fence failed: {r.stderr}"

    py, sh = _shape(py_home), _shape(sh_home)
    assert py == sh, (
        "the two fence implementations have drifted:\n"
        f"  python only: {sorted(py - sh)}\n"
        f"  shell only:  {sorted(sh - py)}"
    )
    # the substance, so agreement on two empty homes cannot pass
    assert py == {f"{n}:link" for n in MIRRORED_ENTRIES}


def test_python_fence_mirrors_exactly_the_allowlist(tmp_path: Path) -> None:
    real = tmp_path / "real"; real.mkdir(); _seed(real)
    home = fence_home(real, tmp_path / "fenced")

    for entry in MIRRORED_ENTRIES:
        assert (home / entry).is_symlink() and (home / entry).exists(), f"{entry} was not mirrored"
    assert sorted(p.name for p in home.iterdir()) == sorted(MIRRORED_ENTRIES)


def test_an_allowlist_entry_the_real_home_lacks_is_skipped(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / ".m2").mkdir(parents=True)
    py = fence_home(real, tmp_path / "py")
    r = _shell_fence(real, tmp_path / "sh")
    assert r.returncode == 0, r.stderr
    assert _shape(py) == _shape(tmp_path / "sh") == {".m2:link"}


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


def _shell_mirrored() -> list[str]:
    r = subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; printf "%s\\n" "${{FENCE_MIRRORED_ENTRIES[@]}}"'],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    return [line for line in r.stdout.splitlines() if line]


def test_the_two_allowlists_are_equal() -> None:
    shell = _shell_mirrored()
    assert len(shell) == len(set(shell)), f"duplicate in the shell allowlist: {shell}"
    assert sorted(shell) == sorted(MIRRORED_ENTRIES)
    # non-vacuity: the measured toolchain caches, not an empty pair
    for name in (".m2", ".gradle", ".docker", ".cargo", ".rustup", ".testcontainers.properties"):
        assert name in shell


def test_no_credential_store_is_on_either_allowlist() -> None:
    """The allowlist entries are the ONLY mirrors, and none is, or sits under,
    a credential store or one of the trees whose mirroring cost a live service."""
    for credential in (".ssh", ".aws", ".gnupg", ".kube", ".netrc", ".npmrc", ".gitconfig",
                       ".claude", ".config/gh", ".config/op", ".config/1Password", ".config/git",
                       ".dolt", ".beads", "Library/Keychains"):
        assert credential in NEVER_MIRRORED, f"{credential} missing from NEVER_MIRRORED"
    for allowed in (*MIRRORED_ENTRIES, *_shell_mirrored()):
        assert "/" not in allowed, f"{allowed!r}: the allowlist is top-level names only"
        for never in NEVER_MIRRORED:
            assert allowed != never and not allowed.startswith(never + "/") and not never.startswith(allowed + "/"), (
                f"allowlist entry {allowed!r} is, or overlaps, never-mirrored {never!r}"
            )


def test_a_seeded_credential_store_is_never_visible_in_either_fence(tmp_path: Path) -> None:
    """Behavioural: seed every credential store with a file, fence through both
    twins, and require that nothing but the allowlisted entries exists in the
    result, and that no seeded secret is reachable at any depth."""
    real = tmp_path / "real"
    real.mkdir()
    _seed(real)
    for rel in (".ssh", ".aws", ".gnupg", ".kube", ".config/gh", ".config/op", ".config/1Password",
                ".config/git", ".dolt", ".beads", ".claude", "Library/Keychains"):
        (real / rel).mkdir(parents=True, exist_ok=True)
        (real / rel / "secret").write_text("x")

    py = fence_home(real, tmp_path / "py")
    r = _shell_fence(real, tmp_path / "sh")
    assert r.returncode == 0, r.stderr
    for home in (py, tmp_path / "sh"):
        assert sorted(p.name for p in home.iterdir()) == sorted(MIRRORED_ENTRIES)
        for never in NEVER_MIRRORED:
            assert not (home / never).exists() and not (home / never).is_symlink(), f"{home}: {never} visible"
        assert not any(home.rglob("secret")), f"{home}: a credential file is reachable"


_CACHE_KEYS = ("NX_ONNX_MODEL_DIR", "NX_MINILM_CACHE_DIR", "NX_ORT_NATIVE_DIR")


@pytest.mark.parametrize("outer", [{}, {"NX_ONNX_MODEL_DIR": "/m"},
                                   {"NX_ONNX_MODEL_DIR": "", "NX_MINILM_CACHE_DIR": "", "NX_ORT_NATIVE_DIR": ""}])
def test_the_model_cache_pin_agrees_between_the_twins(outer: dict[str, str], tmp_path: Path) -> None:
    from tests._fence_home import fence_cache_env

    real = tmp_path / "real"
    with patch.dict(os.environ):
        for k in _CACHE_KEYS:
            os.environ.pop(k, None)
        os.environ.update(outer)
        fence_cache_env(real)
        py = {k: os.environ.get(k) for k in _CACHE_KEYS}

    env = {k: v for k, v in os.environ.items() if k not in _CACHE_KEYS}
    env.update(outer)
    script = (
        f'source "{_SHELL_FENCE}"; fence_cache_env "{real}"; '
        'printf "NX_ONNX_MODEL_DIR=%s\\nNX_MINILM_CACHE_DIR=%s\\nNX_ORT_NATIVE_DIR=%s\\n" '
        '"$NX_ONNX_MODEL_DIR" "$NX_MINILM_CACHE_DIR" "$NX_ORT_NATIVE_DIR"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    sh = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert py == sh
    assert all(py.values()), py
    if not outer:
        assert py["NX_ONNX_MODEL_DIR"] == str(real / ".cache" / "nexus" / "onnx_models")
        assert py["NX_MINILM_CACHE_DIR"] == str(real / ".cache" / "chroma" / "onnx_models")
        assert py["NX_ORT_NATIVE_DIR"] == str(real / ".cache" / "nexus" / "ort_native")


def _python_manager_env(tmp_path: Path, real_runtime: Path, platform: str, docker_host: str | None):
    from tests._fence_home import fence_manager_env

    gate = tmp_path / "py-gate"
    with patch.dict(os.environ), patch_in("tests._fence_home", "sys.platform", platform):
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


def test_a_refused_symlink_on_windows_links_through_without_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-admin Windows account (the win-release runner's) cannot create
    # symlinks: WinError 1314 killed the whole Windows test set at session
    # start (rehearsal run 37410530509). The fence links through instead.
    import tests._fence_home as fh

    real = tmp_path / "real"
    (real / ".m2").mkdir(parents=True)
    (real / ".testcontainers.properties").write_text("x")
    (real / "proj").mkdir()
    (real / ".gitconfig").write_text("x")

    def refuse(self: Path, target: Path, *a: object, **k: object) -> None:
        raise OSError(1314, "A required privilege is not held by the client")

    linked: list[tuple[str, str]] = []
    monkeypatch.setattr(Path, "symlink_to", refuse)
    monkeypatch.setattr(fh, "_windows_link", lambda link, entry: linked.append((link.name, entry.name)))
    setattr_in(monkeypatch, fh, "sys.platform", "win32")
    fh.fence_home(real, tmp_path / "gate")
    assert sorted(linked) == [(".m2", ".m2"), (".testcontainers.properties", ".testcontainers.properties")]

    # Off Windows the same refusal is not swallowed.
    setattr_in(monkeypatch, fh, "sys.platform", "linux")
    with pytest.raises(OSError):
        fh.fence_home(real, tmp_path / "gate2")
