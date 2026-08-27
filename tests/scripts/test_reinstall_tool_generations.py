# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""scripts/reinstall-tool.sh under side-by-side generations. nexus-utpuw.8 (P4).

THIS MODULE IS THE ACCEPTANCE CRITERION (nexus-utpuw comment 1): bare
``scripts/reinstall-tool.sh``, zero flags, zero steps, must ALWAYS succeed under
any number of live sessions. No refusal, no ``--force``, no ``--cycle-daemons``,
no close-your-sessions instruction.

The old script could not offer that and was not wrong to refuse: ``uv tool
install --reinstall`` rebuilds the venv IN PLACE, so every live holder suffers
delayed lazy-import failures afterwards (nexus-q3xrx -- vanished cacert, version
reading as 0.0.0, ModuleNotFoundError for modules that are on disk). Safety was
structurally impossible, so the flags existed to choose which damage to take.

Under generations the install lands in a FRESH directory and ``current`` is
repointed atomically. The tree a running process resolved at spawn stays
byte-identical underneath it. Nothing is killed, nothing is restarted, nothing
is refused -- so the tests that used to assert the choreography assert its
absence instead. Holders are now a FACT to report, not an obstacle.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from _generation_harness import SAFE_BASE_PATH, fabricate_generation, stub_uv

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "reinstall-tool.sh"

pytestmark = pytest.mark.skipif(
    os.uname().sysname not in ("Darwin", "Linux"),
    reason="ps/argv shapes are platform specific",
)


@pytest.fixture
def env(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    stub_uv(stub_bin)
    source = tmp_path / "src"
    source.mkdir()
    # Newer than the installed generation, so the downgrade guard stays quiet.
    (source / "pyproject.toml").write_text('[project]\nversion = "9.9.9"\n')
    return tools, bin_dir, stub_bin, source


def _run(env_t, *args, extra_env: dict | None = None):
    tools, bin_dir, stub_bin, source = env_t
    e = {
        "PATH": f"{stub_bin}:{SAFE_BASE_PATH}",
        "HOME": str(tools.parent / "home"),
        "NX_TOOLS_DIR": str(tools),
        "NX_BIN_DIR": str(bin_dir),
    }
    e.update(extra_env or {})
    return subprocess.run(
        ["bash", str(_SCRIPT), str(source), *args],
        capture_output=True, text=True, env=e, timeout=180,
    )


class _Holder:
    """A REAL process running from a generation, the way a live nx-mcp is."""

    def __init__(self, gen: Path, entry: str = "nx-mcp"):
        self.proc = subprocess.Popen(
            [str(gen / "bin" / entry)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            if self.proc.poll() is None:
                break
            time.sleep(0.05)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        try:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(timeout=5)
        except Exception:
            pass


@pytest.fixture
def holder(env):
    tools, _, _, _ = env
    gen = fabricate_generation(tools, "OLD", version="1.0.0", nx_sleeps=True)
    (tools / "current").symlink_to(gen)
    h = _Holder(gen)
    yield h, gen
    h.kill()


# --------------------------------------------------------------------------
# the acceptance criterion
# --------------------------------------------------------------------------

def test_bare_invocation_succeeds_under_a_live_holder(env, holder) -> None:
    """THE bead. Zero flags, a live holder, exit 0. The old script exited 3."""
    h, _ = holder
    assert h.alive, "fixture failed to produce a live holder"

    result = _run(env)

    assert result.returncode == 0, (
        f"bare invocation failed under a live holder\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "REFUSING" not in result.stdout + result.stderr, (
        "the refusal path survived; the acceptance criterion deletes it"
    )


def test_a_live_holder_is_never_killed(env, holder) -> None:
    """The inverse of every _wait_dead assertion in the deleted choreography.
    Under generations the holder keeps running from its ORIGINAL tree."""
    h, gen = holder

    _run(env)

    assert h.alive, "the reinstall killed a live holder"
    assert (gen / "bin" / "nx-mcp").exists(), (
        "the holder's own generation was mutated underneath it -- the whole "
        "point of side-by-side is that this tree stays byte-identical"
    )


def test_the_holder_is_reported_not_refused(env, holder) -> None:
    """Guard semantics invert: holders become one informational line."""
    result = _run(env)

    out = result.stdout
    assert "converge" in out, (
        f"no informational holder line naming convergence:\n{out}"
    )


def test_current_advances_to_the_new_generation(env, holder) -> None:
    """Non-vacuity for the three tests above: exiting 0 while installing
    nothing would satisfy all of them."""
    _, old_gen = holder
    tools, _, _, _ = env

    _run(env)

    current = (tools / "current").resolve()
    assert current != old_gen.resolve(), (
        "current still points at the old generation -- nothing was installed"
    )
    assert (current / "nexus-install.json").is_file(), (
        "current points at a directory with no receipt, which is not a generation"
    )


@pytest.mark.parametrize("flag", ["--force", "--cycle-daemons", "--cycle-mcp", "--no-cycle"])
def test_the_deleted_flags_are_refused_by_name(env, holder, flag) -> None:
    """Deleted, not silently ignored. A caller passing --force believes it
    forced something; accepting the flag and doing nothing special is how a
    removed safety story turns into a false one. The acceptance criterion says
    these do not exist, so saying so is the honest implementation."""
    result = _run(env, flag)

    assert result.returncode != 0, f"{flag} was silently accepted"
    assert flag in result.stdout + result.stderr, (
        f"{flag} was rejected without naming it, so the caller cannot tell why"
    )
