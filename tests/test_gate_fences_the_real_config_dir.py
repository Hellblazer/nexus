# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The gate's isolation must not rest on one environment variable.

nexus-pfuns follow-up. `local-service-gate.sh` pinned `NEXUS_CONFIG_DIR` per
invocation and left `HOME` pointing at the operator's real home. That pin is
necessary and not sufficient: `nexus_config_dir()` falls back to
`Path.home()/".config"/"nexus"` whenever the variable is absent, so any process
failing to inherit it writes production state.

  2026-07-13  `get_credential()`'s config.yml fallback read the operator's real
              ~/.config/nexus/config.yml from inside the "fully isolated" gate.
              Fixed by pinning the variable; the fallback was left open.
  2026-08-24  the pfuns guard caught `last_seen_version` stamped 7.16.3 -- the
              INSTALLED tool's version, not the tree under test -- reddening a
              release leg in which all 560 tests passed.

TWO LAYERS, not three. An earlier revision added a sandbox-exec profile, printed
"real-config writes denied", and only exported the profile path -- nothing
consumed it. Wiring it up was then measured and rejected: `ps` is blocked under
sandbox-exec even with explicit (allow process-exec*) (allow process-info*), and
this repo's conftest substrate sweep shells out to `ps`. Both facts are pinned
below so neither the inert version nor the breaking version can return quietly.

These tests drive `tests/e2e/lib/fence_home.sh` DIRECTLY. A fence verified
against a reimplementation is not verified.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).parent.parent
_GATE = _REPO / "tests" / "e2e" / "local-service-gate.sh"
_FENCE_LIB = _REPO / "tests" / "e2e" / "lib" / "fence_home.sh"
_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")


def _run_fence(real_home: Path, gate_home: Path) -> subprocess.CompletedProcess:
    """Invoke the REAL fence_home implementation."""
    script = f'source "{_FENCE_LIB}"; fence_home "{real_home}" "{gate_home}" ".config/nexus"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


@pytest.fixture()
def mirrored(tmp_path: Path):
    """A synthetic real-home containing exactly the entries that mattered on
    2026-08-24, mirrored through the real implementation."""
    real = tmp_path / "real"
    for rel in (
        ".docker/run", ".m2/repository", ".cache/uv", ".cache/nexus/onnx_models",
        ".local/bin", ".claude/plugins", ".config/nexus", ".config/gh", "Documents",
    ):
        (real / rel).mkdir(parents=True, exist_ok=True)
    (real / ".testcontainers.properties").write_text("testcontainers.ryuk.disabled=true\n")
    (real / ".config" / "nexus" / "last_seen_version").write_text("7.17.0\n")
    (real / ".config" / "gh" / "hosts.yml").write_text("github.com: {}\n")

    gate = tmp_path / "gate"
    r = _run_fence(real, gate)
    assert r.returncode == 0, f"fence_home failed: {r.stderr}"
    return real, gate


# ── Layer 1: HOME is the backstop the env pin never had ─────────────────────


def test_config_dir_falls_back_to_home_when_the_pin_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanism the whole fence exists for. If this stops being true the
    fallback is no longer the exposure, and layer 1 guards nothing."""
    from nexus.config import nexus_config_dir

    monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert nexus_config_dir() == tmp_path / ".config" / "nexus"


def test_the_fenced_home_moves_the_config_dir_off_the_real_one(mirrored) -> None:
    real, gate = mirrored
    from nexus.config import nexus_config_dir

    env_home = os.environ.get("HOME")
    try:
        os.environ["HOME"] = str(gate)
        os.environ.pop("NEXUS_CONFIG_DIR", None)
        resolved = Path(nexus_config_dir())
        assert not str(resolved).startswith(str(real)), (
            f"a pin-less process still resolves into the real home: {resolved}"
        )
        assert str(resolved).startswith(str(gate))
    finally:
        if env_home is not None:
            os.environ["HOME"] = env_home


def test_the_shadowed_config_dir_is_empty_not_the_real_one(mirrored) -> None:
    """The fenced path must be a FRESH directory. A symlink through would make
    every layer above cosmetic."""
    real, gate = mirrored
    shadowed = gate / ".config" / "nexus"
    assert shadowed.is_dir() and not shadowed.is_symlink()
    assert not (shadowed / "last_seen_version").exists(), (
        "the real config dir leaked into the fenced home"
    )
    assert (real / ".config" / "nexus" / "last_seen_version").exists(), (
        "fencing must not disturb the real config dir"
    )


# ── The regression that broke the gate on 2026-08-24 ────────────────────────


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        (".docker", "holds the daemon socket at ~/.docker/run/docker.sock"),
        (".testcontainers.properties", "carries testcontainers.ryuk.disabled=true"),
        (".m2", "the Maven repository the jar rebuild resolves from"),
        (".cache", "71GB uv cache + the 504MB nexus model cache"),
        (".local", "fastembed cache and the installed tool"),
        (".claude", "plugin-install-mode fixtures health.py reads"),
        (".config/gh", "an unrelated ~/.config tenant that must not be collateral"),
    ],
)
def test_the_mirror_passes_through(mirrored, entry: str, why: str) -> None:
    """The allowlist design broke on the SECOND thing it touched: the Maven
    rebuild died on absent .testcontainers.properties and .docker. Enumerating
    "what HOME is for" cannot be completed, so the mirror is a denylist and
    these cases are the proof."""
    _real, gate = mirrored
    target = gate / entry
    assert target.exists(), f"{entry} did not survive the mirror — {why}"


def test_only_the_nexus_config_is_shadowed(mirrored) -> None:
    """Positive control on the SCOPE. A mirror that shadowed all of ~/.config
    would pass every passthrough case above except the gh one, and would
    silently break any other tool keeping state there."""
    _real, gate = mirrored
    assert (gate / ".config" / "gh" / "hosts.yml").exists()
    assert not (gate / ".config" / "nexus" / "last_seen_version").exists()


def test_top_level_non_dotfiles_are_mirrored_too(mirrored) -> None:
    """dotglob must not become the whole glob — a mirror that copied only
    dotfiles would drop everything else in HOME."""
    _real, gate = mirrored
    assert (gate / "Documents").exists()


# ── Layer 2: a bare `nx` must fail loudly, not succeed quietly ──────────────


def _write_shim(dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    shim = dest / "nx"
    shim.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "FATAL: bare 'nx' called inside local-service-gate (PPID=$PPID)." >&2
        echo "  It resolves to the INSTALLED tool, not the tree under test, and it" >&2
        echo "  writes the real ~/.config/nexus. Use 'uv run nx'." >&2
        exit 1
        """))
    shim.chmod(0o755)
    return shim


def test_bare_nx_on_the_gate_path_fails_loudly(tmp_path: Path) -> None:
    shim_dir = tmp_path / "shim"
    _write_shim(shim_dir)
    env = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}")
    r = subprocess.run(["nx", "--version"], capture_output=True, text=True, env=env)
    assert r.returncode != 0, "a bare nx inside the gate succeeded"
    assert "FATAL" in r.stderr and "uv run nx" in r.stderr


def test_the_shim_shadows_a_real_nx_earlier_on_path(tmp_path: Path) -> None:
    """Positive control on ORDER. A shim appended rather than prepended would
    pass the test above on a box with no nx installed, and do nothing here."""
    decoy_dir = tmp_path / "decoy"
    decoy_dir.mkdir()
    (decoy_dir / "nx").write_text("#!/bin/sh\necho 'nx, version 9.9.9'\nexit 0\n")
    (decoy_dir / "nx").chmod(0o755)
    shim_dir = tmp_path / "shim"
    _write_shim(shim_dir)
    env = dict(os.environ, PATH=f"{shim_dir}:{decoy_dir}:{os.environ['PATH']}")
    r = subprocess.run(["nx", "--version"], capture_output=True, text=True, env=env)
    assert r.returncode != 0, "the decoy nx ran; the shim is not shadowing it"
    assert "9.9.9" not in r.stdout


# ── The fence must stay wired, and the rejected layer must stay rejected ────


def test_the_gate_sources_and_calls_the_real_fence(mirrored) -> None:
    """Every test above drives fence_home directly, so all of them would keep
    passing if the GATE stopped calling it. This is the one that notices."""
    body = _GATE.read_text()
    assert "lib/fence_home.sh" in body, "the gate no longer sources the fence helper"
    assert 'fence_home "$REAL_HOME" "$GATE_HOME" ".config/nexus"' in body
    assert 'export HOME="$GATE_HOME"' in body
    assert 'export PATH="$GATE_SHIM:$PATH"' in body


def test_the_gate_does_not_claim_sandbox_protection_it_lacks() -> None:
    """The inert-guard tripwire. An earlier revision printed 'real-config
    writes denied via sandbox-exec' while only exporting a profile path that
    nothing consumed. If sandbox-exec ever returns here it must be WIRED, not
    announced -- and the measurement below says wiring it breaks the leg."""
    body = _GATE.read_text()
    assert "denied via sandbox-exec" not in body, (
        "the gate advertises sandbox-exec protection; verify it is actually "
        "wrapping a command before allowing this string back"
    )


@pytest.mark.skipif(not _SANDBOX_EXEC.exists(), reason="sandbox-exec is macOS-only")
def test_sandbox_exec_still_blocks_ps_so_it_cannot_wrap_the_pytest_leg() -> None:
    """The measurement that rejected layer 3, pinned so it is not re-litigated
    from memory. conftest's substrate sweep shells out to `ps`; if a future
    macOS stops blocking it under Seatbelt, this test fails and layer 3 becomes
    reconsiderable on evidence rather than optimism."""
    profile = Path(os.environ["TMPDIR"] if "TMPDIR" in os.environ else "/tmp") / "nx-fence-ps-probe.sb"
    profile.write_text("(version 1)\n(allow default)\n(allow process-exec*)\n(allow process-info*)\n")
    try:
        r = subprocess.run(
            [str(_SANDBOX_EXEC), "-f", str(profile), "/bin/sh", "-c", "ps -o pid -p $$"],
            capture_output=True, text=True,
        )
        assert r.returncode != 0, (
            "sandbox-exec now permits `ps` even under an explicit allow — the "
            "reason layer 3 was rejected no longer holds; re-measure it"
        )
    finally:
        profile.unlink(missing_ok=True)
