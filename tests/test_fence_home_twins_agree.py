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
real home has are linked through. No credential store is on it by name.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from tests import _tmp_root as tr
from tests._fence_home import MIRRORED_ENTRIES, NEVER_MIRRORED, fence_home
from tests._module_seam import patch_in, setattr_in

_SHELL_FENCE = Path(__file__).parent / "e2e" / "lib" / "fence_home.sh"


def _seed(real: Path) -> None:
    """A home holding the allowlisted caches AND the things the fence must hide."""
    for rel in (".docker/run", ".m2/repository", ".m2/wrapper/dists", ".gradle/caches", ".cargo/registry", ".rustup/toolchains",
                ".cache/uv", ".local/bin", ".local/state/nexus", ".claude/plugins", ".claude/agents",
                ".config/nexus", ".config/gh", ".config/op", ".config/1Password", ".config/git",
                ".config/systemd/user", "Library/LaunchAgents", "Library/Caches", "Library/Keychains",
                ".ssh", ".aws", ".gnupg", ".kube", ".dolt", ".beads", "Documents"):
        (real / rel).mkdir(parents=True, exist_ok=True)
    (real / ".testcontainers.properties").write_text("testcontainers.ryuk.disabled=true\n")
    (real / ".m2" / "settings.xml").write_text("<server><password>secret</password></server>\n")
    (real / ".docker" / "config.json").write_text('{"auths": {"registry": {"auth": "secret"}}}\n')
    for rel in (".gitconfig", ".netrc", ".npmrc", ".claude.json"):
        (real / rel).write_text("secret\n")
    (real / ".config" / "nexus" / "last_seen_version").write_text("7.18.0\n")


def _shape(home: Path) -> set[str]:
    """Every entry of the fenced home, at any depth but never THROUGH a link, as
    ``relative/path:link|dir|file``."""
    out = set()
    for root, dirs, files in os.walk(home):
        for name in (*dirs, *files):
            p = Path(root) / name
            kind = "link" if p.is_symlink() else "dir" if p.is_dir() else "file"
            out.add(f"{p.relative_to(home)}:{kind}")
    return out


def _expected_shape() -> set[str]:
    """What fencing the seeded home must produce: each allowlisted path as a link
    and, for a nested one, a real parent directory."""
    out = set()
    for name in MIRRORED_ENTRIES:
        out.add(f"{name}:link")
        for parent in Path(name).parents:
            if str(parent) != ".":
                out.add(f"{parent}:dir")
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
    assert py == _expected_shape()


def test_python_fence_mirrors_exactly_the_allowlist(tmp_path: Path) -> None:
    real = tmp_path / "real"; real.mkdir(); _seed(real)
    home = fence_home(real, tmp_path / "fenced")

    for entry in MIRRORED_ENTRIES:
        assert (home / entry).is_symlink() and (home / entry).exists(), f"{entry} was not mirrored"
    assert _shape(home) == _expected_shape()


def test_an_allowlist_entry_the_real_home_lacks_is_skipped(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / ".m2" / "repository").mkdir(parents=True)  # no .m2/wrapper, no .testcontainers.properties
    py = fence_home(real, tmp_path / "py")
    r = _shell_fence(real, tmp_path / "sh")
    assert r.returncode == 0, r.stderr
    assert _shape(py) == _shape(tmp_path / "sh") == {".m2:dir", ".m2/repository:link"}
    # and a home with NO .m2 at all gets no empty .m2 directory either
    bare = tmp_path / "bare"; bare.mkdir()
    assert _shape(fence_home(bare, tmp_path / "py2")) == set()
    assert _shell_fence(bare, tmp_path / "sh2").returncode == 0 and _shape(tmp_path / "sh2") == set()


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


#: The allowlist as a LITERAL. Equality between the twins is not enough: an entry
#: added to both stays green there. Widening the mirror means editing this set,
#: in a diff a reviewer reads (nexus-ld3aa review). .gradle .cargo .rustup were
#: dropped: no consumer in this repo. nexus-u73rf (decided): .docker dropped and
#: .m2 narrowed to its two cache directories.
_EXPECTED_MIRROR = {".m2/repository", ".m2/wrapper", ".testcontainers.properties"}


def test_the_two_allowlists_are_equal() -> None:
    shell = _shell_mirrored()
    assert len(shell) == len(set(shell)), f"duplicate in the shell allowlist: {shell}"
    assert sorted(shell) == sorted(MIRRORED_ENTRIES)


def test_the_allowlist_is_exactly_the_three_measured_paths() -> None:
    assert set(MIRRORED_ENTRIES) == _EXPECTED_MIRROR and len(MIRRORED_ENTRIES) == 3
    assert set(_shell_mirrored()) == _EXPECTED_MIRROR


def test_a_toolchain_cache_with_no_repo_consumer_is_not_mirrored(tmp_path: Path) -> None:
    real = tmp_path / "real"; real.mkdir(); _seed(real)
    for home in (fence_home(real, tmp_path / "py"), (_shell_fence(real, tmp_path / "sh"), tmp_path / "sh")[1]):
        for dropped in (".gradle", ".cargo", ".rustup"):
            assert (real / dropped).is_dir(), "the seed must hold it, or this proves nothing"
            assert not (home / dropped).exists() and not (home / dropped).is_symlink(), dropped


def test_no_credential_store_is_on_either_allowlist() -> None:
    """The allowlist entries are the ONLY mirrors, and none is, or sits under,
    a credential store or one of the trees whose mirroring cost a live service."""
    for credential in (".ssh", ".aws", ".gnupg", ".kube", ".netrc", ".npmrc", ".gitconfig",
                       ".claude", ".config/gh", ".config/op", ".config/1Password", ".config/git",
                       ".dolt", ".beads", "Library/Keychains"):
        assert credential in NEVER_MIRRORED, f"{credential} missing from NEVER_MIRRORED"
    for allowed in (*MIRRORED_ENTRIES, *_shell_mirrored()):
        assert not allowed.startswith("/") and ".." not in Path(allowed).parts, f"{allowed!r}: not a home-relative path"
        assert allowed not in (".m2", ".docker"), f"{allowed!r}: whole-directory mirror of a credential-bearing tree"
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
        assert _shape(home) == _expected_shape()
        for never in NEVER_MIRRORED:
            assert not (home / never).exists() and not (home / never).is_symlink(), f"{home}: {never} visible"
        assert not any(home.rglob("secret")), f"{home}: a credential file is reachable"


def test_a_maven_settings_file_and_a_docker_config_are_not_visible_through_either_twin(tmp_path: Path) -> None:
    """nexus-u73rf, decided: ``.m2/settings.xml`` (server passwords) and
    ``.docker/config.json`` (registry auths) exist in the stand-in real home next
    to the mirrored ``.m2/repository``, and neither twin may expose them, by path
    or through any mirror link."""
    real = tmp_path / "real"; real.mkdir(); _seed(real)
    (real / ".m2" / "repository" / "dep.jar").write_text("jar")
    assert (real / ".m2" / "settings.xml").is_file() and (real / ".docker" / "config.json").is_file()
    py = fence_home(real, tmp_path / "py")
    assert _shell_fence(real, tmp_path / "sh").returncode == 0
    for home in (py, tmp_path / "sh"):
        assert (home / ".m2" / "repository" / "dep.jar").read_text() == "jar", "the cache itself must still be reachable"
        assert not (home / ".m2" / "settings.xml").exists() and not (home / ".m2" / "settings.xml").is_symlink()
        assert not (home / ".docker").exists() and not (home / ".docker").is_symlink()
        assert not (home / ".docker" / "config.json").exists()
        assert not any(".docker" in str(p) for p in home.rglob("*")), f"{home}: .docker is reachable"
        # the .m2 in the fence is a real directory, not a link to the real one
        assert (home / ".m2").is_dir() and not (home / ".m2").is_symlink()


_CACHE_KEYS = (
    "NX_ONNX_MODEL_DIR", "NX_MINILM_CACHE_DIR", "NX_ORT_NATIVE_DIR",
    "HF_HUB_CACHE", "NX_TEST_SUBSTRATE_CACHE",
)
#: Inputs the pins read; cleared on both sides so the host env cannot leak in.
_CACHE_INPUTS = ("XDG_CACHE_HOME", "HF_HOME")


@pytest.mark.parametrize("outer", [
    {},
    {"NX_ONNX_MODEL_DIR": "/m"},
    {"HF_HUB_CACHE": "/hub", "NX_TEST_SUBSTRATE_CACHE": "/sub"},
    {k: "" for k in _CACHE_KEYS},
    {"XDG_CACHE_HOME": "/xdg"},
    {"HF_HOME": "/hf"},
])
def test_the_cache_pins_agree_between_the_twins(outer: dict[str, str], tmp_path: Path) -> None:
    from tests._fence_home import fence_cache_env

    real = tmp_path / "real"
    cleared = (*_CACHE_KEYS, *_CACHE_INPUTS)
    with patch.dict(os.environ):
        for k in cleared:
            os.environ.pop(k, None)
        os.environ.update(outer)
        fence_cache_env(real)
        py = {k: os.environ.get(k) for k in _CACHE_KEYS}

    env = {k: v for k, v in os.environ.items() if k not in cleared}
    env.update(outer)
    script = (
        f'source "{_SHELL_FENCE}"; fence_cache_env "{real}"; '
        + "".join(f'printf "{k}=%s\\n" "${k}"; ' for k in _CACHE_KEYS)
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    sh = dict(line.split("=", 1) for line in r.stdout.splitlines())
    assert py == sh
    assert all(py.values()), py
    cache = Path(outer.get("XDG_CACHE_HOME") or real / ".cache")
    if not outer.get("NX_ONNX_MODEL_DIR") and not any(outer.values()):
        assert py["NX_ONNX_MODEL_DIR"] == str(real / ".cache" / "nexus" / "onnx_models")
        assert py["NX_MINILM_CACHE_DIR"] == str(real / ".cache" / "chroma" / "onnx_models")
        assert py["NX_ORT_NATIVE_DIR"] == str(real / ".cache" / "nexus" / "ort_native")
        assert py["HF_HUB_CACHE"] == str(real / ".cache" / "huggingface" / "hub")
        assert py["NX_TEST_SUBSTRATE_CACHE"] == str(real / ".cache" / "nexus-test-substrate")
    if outer.get("XDG_CACHE_HOME"):
        assert py["HF_HUB_CACHE"] == str(cache / "huggingface" / "hub")
        assert py["NX_TEST_SUBSTRATE_CACHE"] == str(cache / "nexus-test-substrate")
    if outer.get("HF_HOME"):
        assert py["HF_HUB_CACHE"] == "/hf/hub"
    if outer.get("HF_HUB_CACHE"):
        assert py["HF_HUB_CACHE"] == "/hub" and py["NX_TEST_SUBSTRATE_CACHE"] == "/sub"


def test_the_hf_pin_is_the_hub_dir_never_hf_home(tmp_path: Path) -> None:
    """HF_HOME is the parent of ``token`` and ``stored_tokens``; pinning it would
    hand the operator's HuggingFace credential to every test."""
    from tests._fence_home import fence_cache_env

    with patch.dict(os.environ):
        for k in (*_CACHE_KEYS, *_CACHE_INPUTS):
            os.environ.pop(k, None)
        fence_cache_env(tmp_path / "real")
        assert "HF_HOME" not in os.environ
        assert "XDG_CACHE_HOME" not in os.environ
        assert Path(os.environ["HF_HUB_CACHE"]).name == "hub"
    assert 'export HF_HOME' not in _SHELL_FENCE.read_text()


def test_the_fence_pin_reaches_the_substrate_cache_with_the_home_empty(tmp_path: Path) -> None:
    """The point of NX_TEST_SUBSTRATE_CACHE: under an empty fenced HOME the
    substrate cache still resolves to the REAL cache, not a cold dir in the fence."""
    from tests._fence_home import fence_cache_env
    from tests.db._service_fixture import substrate_cache_root

    real, fenced = tmp_path / "real", tmp_path / "fenced"
    with patch.dict(os.environ):
        for k in (*_CACHE_KEYS, *_CACHE_INPUTS):
            os.environ.pop(k, None)
        os.environ["HOME"] = os.environ["USERPROFILE"] = str(fenced)
        fence_cache_env(real)
        assert substrate_cache_root() == real / ".cache" / "nexus-test-substrate"
        assert not str(substrate_cache_root()).startswith(str(fenced))


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
    (real / ".m2" / "repository").mkdir(parents=True)
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
    assert sorted(linked) == [(".testcontainers.properties", ".testcontainers.properties"), ("repository", "repository")]

    # Off Windows the same refusal is not swallowed.
    setattr_in(monkeypatch, fh, "sys.platform", "linux")
    with pytest.raises(OSError):
        fh.fence_home(real, tmp_path / "gate2")


# ── nexus-7df8e: the fenced home is removed when the session ends ───────────

def _record_install(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Make *home* the one this process "installed", as install_fence records it."""
    import tests._fence_home as fh

    monkeypatch.setattr(fh, "_installed_home", home)
    monkeypatch.setenv(fh.FENCED_HOME_ENV, str(home))


def test_remove_fenced_home_deletes_the_home_and_never_the_real_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests._fence_home import remove_fenced_home

    real = tmp_path / "real"; real.mkdir(); _seed(real)
    (real / ".m2" / "repository" / "artifact.jar").write_text("x")
    home = fence_home(real, tmp_path / "nx-suite-home-abc")
    (home / "stray").mkdir(); (home / "stray" / "f").write_text("x")  # something the session wrote
    _record_install(monkeypatch, home)
    assert remove_fenced_home(0) is True
    assert not home.exists()
    assert (real / ".m2" / "repository" / "artifact.jar").read_text() == "x", "rmtree followed a mirror link"
    assert (real / ".docker" / "run").is_dir()


def test_remove_fenced_home_deletes_only_the_home_this_process_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-ld3aa confirmation, the ship-blocker: NX_FENCED_HOME is inherited by
    every child session, so it names the PARENT's home there. Only the path
    install_fence recorded in THIS process may be deleted, whatever the env says."""
    import tests._fence_home as fh

    parent = tmp_path / "nx-suite-home-parent"; parent.mkdir(); (parent / "keep").write_text("x")
    monkeypatch.setenv(fh.FENCED_HOME_ENV, str(parent))
    monkeypatch.setattr(fh, "_installed_home", None)
    assert fh.remove_fenced_home(0) is False, "nothing installed here: a no-op, whatever the env says"
    assert (parent / "keep").exists()

    mine = tmp_path / "nx-suite-home-mine"; mine.mkdir()
    monkeypatch.setattr(fh, "_installed_home", mine)
    assert fh.remove_fenced_home(0) is False, "the env names a different home than the recorded one"
    assert (parent / "keep").exists() and mine.is_dir()
    monkeypatch.setenv(fh.FENCED_HOME_ENV, str(mine))
    assert fh.remove_fenced_home(0) is True and not mine.exists() and (parent / "keep").exists()


def test_install_fence_records_the_home_it_made_and_a_second_call_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tests._fence_home as fh

    real = tmp_path / "real"; real.mkdir()
    monkeypatch.setattr(fh, "_installed_home", None)
    with patch.dict(os.environ, {"HOME": str(real)}):
        os.environ.pop(fh.REAL_HOME_ENV, None)
        first = tmp_path / "nx-suite-home-first"
        assert fh.install_fence(first) == first
        assert fh._installed_home == first
        # already fenced: a child's install is a no-op and must not displace the record
        assert fh.install_fence(tmp_path / "nx-suite-home-second") is None
        assert fh._installed_home == first


def test_remove_fenced_home_refuses_a_directory_it_did_not_make(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests._fence_home import remove_fenced_home

    other = tmp_path / "somebodys-home"; other.mkdir(); (other / "keep").write_text("x")
    _record_install(monkeypatch, other)  # even a recorded path must carry the prefix
    assert remove_fenced_home(0) is False
    assert (other / "keep").exists()


def test_remove_fenced_home_requires_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests._fence_home import remove_fenced_home

    rel = tmp_path / "nx-suite-home-rel"; rel.mkdir(); (rel / "keep").write_text("x")
    monkeypatch.chdir(tmp_path)
    _record_install(monkeypatch, Path("nx-suite-home-rel"))
    assert remove_fenced_home(0) is False
    assert (rel / "keep").exists(), "a relative name was resolved against the cwd and deleted"


def test_remove_fenced_home_keeps_a_failed_session_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests._fence_home import KEEP_FENCED_HOME_ENV, remove_fenced_home

    real = tmp_path / "real"; real.mkdir()
    home = fence_home(real, tmp_path / "nx-suite-home-keep")
    _record_install(monkeypatch, home)
    monkeypatch.setenv(KEEP_FENCED_HOME_ENV, "1")
    assert remove_fenced_home(0) is True and not home.exists(), "a PASSING session is never kept"
    home = fence_home(real, tmp_path / "nx-suite-home-keep2")
    _record_install(monkeypatch, home)
    assert remove_fenced_home(1) is False and home.is_dir(), "kept on failure with the opt-in"
    monkeypatch.delenv(KEEP_FENCED_HOME_ENV)
    assert remove_fenced_home(1) is True and not home.exists(), "failure without the opt-in still removes"


def _shell_remove(gate: Path, status: int, *, fenced: str | None, keep: bool) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("NX_FENCED_HOME", "NX_KEEP_FENCED_HOME")}
    if fenced is not None:
        env["NX_FENCED_HOME"] = fenced
    if keep:
        env["NX_KEEP_FENCED_HOME"] = "1"
    return subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; fence_remove_home "{gate}" {status}'],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _gate_home(tmp_path: Path, name: str) -> Path:
    """The gate's own shape: <TMPDIR>/nx-local-service-gate.XXXXXX/home."""
    return tmp_path / f"nx-local-service-gate.{name}" / "home"


def test_the_shell_twin_removes_the_home_with_the_same_keep_rule(tmp_path: Path) -> None:
    real = tmp_path / "real"; real.mkdir(); _seed(real)
    (real / ".m2" / "repository" / "artifact.jar").write_text("x")

    gate = _gate_home(tmp_path, "one")
    assert _shell_fence(real, gate).returncode == 0
    assert _shell_remove(gate, 0, fenced=str(gate), keep=True).returncode == 0
    assert not gate.exists()
    assert (real / ".m2" / "repository" / "artifact.jar").read_text() == "x", "removal followed a mirror link"

    gate = _gate_home(tmp_path, "two")
    assert _shell_fence(real, gate).returncode == 0
    assert _shell_remove(gate, 1, fenced=str(gate), keep=True).returncode == 1 and gate.is_dir()
    assert _shell_remove(gate, 1, fenced=str(gate), keep=False).returncode == 0 and not gate.exists()

    other = tmp_path / "not-the-fence"; other.mkdir()
    assert _shell_remove(other, 0, fenced=str(tmp_path / "elsewhere"), keep=False).returncode == 1
    assert _shell_remove(other, 0, fenced=None, keep=False).returncode == 1
    assert other.is_dir()


def test_the_shell_twin_refuses_a_home_that_is_not_the_gates_scratch_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-7df8e bead 1: the only check used to be gate_home == NX_FENCED_HOME,
    which the caller controls. A stand-in real home named as BOTH must survive,
    with its .m2 link intact, and so must every near miss of the gate's shape."""
    real = tmp_path / "standin-real-home"; real.mkdir(); _seed(real)
    (real / ".m2" / "repository" / "artifact.jar").write_text("x")
    link_into_real = tmp_path / "scratch-link"
    link_into_real.symlink_to(real)
    monkeypatch.chdir(tmp_path)  # the shell inherits the cwd: the relative case below really exists
    (tmp_path / "nx-local-service-gate.rel" / "home").mkdir(parents=True)
    near_misses = [
        real,                                                      # the stand-in real home itself
        tmp_path / "home",                                         # right basename, no gate scratch parent
        tmp_path / "nx-local-service-gate.x" / "not-home",         # right parent, wrong basename
        tmp_path / "other.x" / "home",                             # wrong parent prefix
        link_into_real,                                            # a symlink to the real home
        Path("nx-local-service-gate.rel") / "home",                # right shape, but relative
    ]
    (tmp_path / "other.x" / "home").mkdir(parents=True)
    (tmp_path / "nx-local-service-gate.x" / "not-home").mkdir(parents=True)
    (tmp_path / "home").mkdir()
    for home in near_misses:
        proc = _shell_remove(home, 0, fenced=str(home), keep=False)
        assert proc.returncode == 1, f"{home}: {proc.stdout}{proc.stderr}"
    assert (real / ".m2" / "repository" / "artifact.jar").read_text() == "x"
    assert (real / ".m2").is_dir(), "the stand-in's .m2 was touched"
    assert link_into_real.is_symlink()
    for d in (tmp_path / "other.x" / "home", tmp_path / "nx-local-service-gate.x" / "not-home", tmp_path / "home",
              tmp_path / "nx-local-service-gate.rel" / "home"):
        assert d.is_dir()


def _suite_session(tmp_path: Path, *, fail: bool, keep: bool) -> tuple[subprocess.CompletedProcess, list[Path]]:
    """Run one real pytest session under THIS repo's conftest with TMPDIR pointed at
    *tmp_path*, so the fence it installs lands there. Returns the process and the
    fenced homes left behind."""
    seam_dir = tmp_path / "seam"
    seam_dir.mkdir(parents=True)
    (seam_dir / "_fail_seam.py").write_text(
        "import pytest\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_runtest_call(item):\n"
        "    raise AssertionError('forced failure')\n"
    )
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir(parents=True)
    drop = {"NX_REAL_HOME", "NX_FENCED_HOME", "NX_NO_HOME_FENCE", "NX_KEEP_FENCED_HOME", "NX_SUITE_LEASE_HELD_BY",
            "NX_SUITE_LEASE_WAIT", "NX_SUITE_LEASE_UNGUARDED", "NEXUS_PG_BIN", "PYTEST_ADDOPTS"}
    # child_env_with_tmpdir also drops NX_TEST_TMP_ORIG: in a lab-run grant the child's
    # session fixture would otherwise scan and reap the box's real tempdir (nexus-g2f0p)
    env = tr.child_env_with_tmpdir({k: v for k, v in os.environ.items() if k not in drop}, tmpdir)
    # the real home, as the fence recorded it, so the child fences the OPERATOR's home like a fresh run
    env["HOME"] = os.environ.get("NX_REAL_HOME") or os.environ["HOME"]
    env.update(
        PYTHONPATH=str(seam_dir), PYTEST_ADDOPTS="",
        NX_TEST_T2_SUBSTRATE="none",
        NX_BUILD_LEASE_ROOT=str(tmp_path / "leases"),
    )
    if keep:
        env["NX_KEEP_FENCED_HOME"] = "1"
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist"]
    if fail:
        args += ["-p", "_fail_seam"]
    args += [f"{Path(__file__)}::test_an_allowlist_entry_the_real_home_lacks_is_skipped"]
    proc = subprocess.run(args, cwd=_REPO, env=env, capture_output=True, text=True, timeout=300)
    return proc, sorted(tmpdir.glob("nx-suite-home-*"))


_REPO = Path(__file__).parent.parent


def test_a_real_session_leaves_no_fenced_home_behind(tmp_path: Path) -> None:
    """End to end: the fence is installed at session start under the repo's own
    conftest and is gone after the session, pass or fail; kept only on a failed
    session with NX_KEEP_FENCED_HOME."""
    ok, left = _suite_session(tmp_path / "ok", fail=False, keep=True)
    assert "1 passed" in ok.stdout, ok.stdout + ok.stderr
    assert left == [], f"a passing session left {left}"

    bad, left = _suite_session(tmp_path / "bad", fail=True, keep=False)
    assert bad.returncode == 1 and "1 failed" in bad.stdout, bad.stdout + bad.stderr
    assert left == [], f"a failed session without the opt-in left {left}"

    kept, left = _suite_session(tmp_path / "kept", fail=True, keep=True)
    assert kept.returncode == 1 and "1 failed" in kept.stdout, kept.stdout + kept.stderr
    assert len(left) == 1, f"a failed session with NX_KEEP_FENCED_HOME must keep its home: {left}"


def test_a_nested_session_leaves_the_parents_fenced_home_alone(tmp_path: Path) -> None:
    """nexus-ld3aa confirmation, the ship-blocker. Many tests spawn a child pytest
    session with os.environ, so the child inherits NX_FENCED_HOME and NX_REAL_HOME
    from the parent. install_fence no-ops there, but the child's sessionfinish used
    to delete the parent's home by the env var, and its conftest left an empty
    nx-suite-home-* of its own. test_a_real_session_leaves_no_fenced_home_behind
    cannot see this: it drops both keys."""
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    parent = tmpdir / "nx-suite-home-parent"
    parent.mkdir()
    (parent / "keep").write_text("x")
    (parent / "nx-tools").mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (real / ".m2").mkdir()
    (real / ".m2" / "a.jar").write_text("x")
    (parent / ".m2").symlink_to(real / ".m2")
    drop = {"NX_NO_HOME_FENCE", "NX_KEEP_FENCED_HOME", "NX_SUITE_LEASE_HELD_BY", "NX_SUITE_LEASE_WAIT",
            "NX_SUITE_LEASE_UNGUARDED", "NEXUS_PG_BIN", "PYTEST_ADDOPTS"}
    env = tr.child_env_with_tmpdir({k: v for k, v in os.environ.items() if k not in drop}, tmpdir)
    env.update(
        HOME=str(parent), NX_REAL_HOME=str(parent), NX_FENCED_HOME=str(parent),
        PYTEST_ADDOPTS="", NX_TEST_T2_SUBSTRATE="none",
        NX_BUILD_LEASE_ROOT=str(tmp_path / "leases"),
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:xdist",
         f"{Path(__file__)}::test_an_allowlist_entry_the_real_home_lacks_is_skipped"],
        cwd=_REPO, env=env, capture_output=True, text=True, timeout=300,
    )
    assert "1 passed" in proc.stdout, proc.stdout + proc.stderr
    assert (parent / "keep").read_text() == "x" and (parent / "nx-tools").is_dir(), \
        "the child session deleted the PARENT session's fenced home"
    assert (real / ".m2" / "a.jar").read_text() == "x"
    assert sorted(p.name for p in tmpdir.glob("nx-suite-home-*")) == ["nx-suite-home-parent"], \
        "the child session left an nx-suite-home-* of its own"


def test_both_twins_pass_the_container_runtime_env_through(tmp_path: Path) -> None:
    """The laptop runs containers on colima: DOCKER_HOST and
    TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE arrive from ~/.zshenv, not from the
    mirrored ``.docker``. Neither twin may clear or rewrite them."""
    from tests._fence_home import fence_manager_env

    pins = {"DOCKER_HOST": "unix:///colima/docker.sock",
            "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE": "/colima/docker.sock"}
    with patch.dict(os.environ, pins):
        fence_manager_env(tmp_path / "py-gate")
        assert {k: os.environ.get(k) for k in pins} == pins
    env = {**os.environ, **pins}
    out = subprocess.run(
        ["bash", "-c", f'source "{_SHELL_FENCE}"; fence_home_env "{tmp_path / "sh-gate"}"; '
                       'printf "%s\\n%s\\n" "$DOCKER_HOST" "$TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE"'],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == list(pins.values())
