# SPDX-License-Identifier: AGPL-3.0-or-later
"""The nexus-pfuns real-config-dir guard, end to end (nexus-ist38).

``tests/test_real_config_dir_guard_wiring.py`` already drives this guard
end to end, but through a ``pytester`` sandbox whose conftest HAND-WIRES
the hooks: it proves the guard's logic fails a run, and cannot prove THIS
repo's own ``tests/conftest.py`` registers those hooks, nor exercise the
benign (rc 0) verdict, nor xdist. Every other test calls the helpers
directly with hand-built snapshots -- below the production layer
entirely.

That gap was not hypothetical. ``_split_appends_from_state`` consumed
``_diff_config_dir_snapshots``'s ``"<VERB> <path>"`` entries as if they
were bare paths, so the benign-append split was DEAD for the guard's
whole life while eleven helper tests (all feeding bare paths) stayed
green, and real runs reddened over a growing ``routing_log.jsonl``
(observed twice on 2026-09-02 during an RDR-201 review).

So these cases drive the REAL thing: a child ``pytest`` process, this
repo's real conftest, its real hooks, and the exit code the shell
actually sees -- the benign verdict included, serially and under ``-n``.
``_REAL_CONFIG_DIR_ENV_OVERRIDE`` points the guard at a throwaway home,
so nothing here can read or write the operator's own ``~/.config/nexus/``.

Marked ``lint``: three child pytest sessions, roughly 30s, out of the hot loop.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A fast, substrate-free target so the child session is dominated by
#: conftest import rather than by the tests it runs. Its identity does not
#: matter -- the subject is the session hooks, not this file's assertions.
_INNER_TARGET = "tests/test_pfuns_append_only_logs_do_not_red_a_run.py"

_PROBE_PLUGIN = '''
import os, pathlib

def pytest_collection_finish(session):
    """Mutate the fake real-config dir mid-session, AFTER the guard's
    sessionstart baseline snapshot and BEFORE its sessionfinish check."""
    d = pathlib.Path(os.environ["NX_REAL_CONFIG_DIR_FOR_GUARD_TEST"]) / ".config" / "nexus"
    if os.environ["NX_PROBE_MODE"] == "append":
        with (d / "routing_log.jsonl").open("a") as fh:
            fh.write('{"evt": 2}\\n')
    else:
        (d / "backfill_state.json").write_text('{"a": 2}\\n')
'''


def _run_inner(tmp_path: Path, mode: str, *pytest_args: str) -> tuple[int, str]:
    """Run a child pytest session whose guard watches *tmp_path* as home,
    with a plugin that performs *mode* ("append" or "state") mid-session.
    Returns the child's real (returncode, combined output)."""
    fake_config = tmp_path / "home" / ".config" / "nexus"
    fake_config.mkdir(parents=True)
    (fake_config / "routing_log.jsonl").write_text('{"evt": 1}\n')
    (fake_config / "backfill_state.json").write_text('{"a": 1}\n')

    plugin_dir = tmp_path / "probeplug"
    plugin_dir.mkdir()
    (plugin_dir / "nxprobe.py").write_text(_PROBE_PLUGIN)

    env = dict(os.environ)
    env["NX_REAL_CONFIG_DIR_FOR_GUARD_TEST"] = str(tmp_path / "home")
    env["NX_PROBE_MODE"] = mode
    env["PYTHONPATH"] = os.pathsep.join(
        [str(plugin_dir), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", _INNER_TARGET, "-q", "-p", "nxprobe", *pytest_args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "xdist_args",
    [pytest.param(("-p", "no:xdist"), id="serial"), pytest.param(("-n", "2"), id="xdist")],
)
def test_a_growing_append_only_log_does_not_redden_a_real_run(
    tmp_path: Path, xdist_args: tuple[str, ...],
) -> None:
    """The regression nexus-ist38 filed: an append to ``routing_log.jsonl``
    is reported as a NOTE and the session still exits 0. Before the fix this
    exited 1 with a FAIL, over a log file growing by one line. Parametrized
    over both because the bead was filed on the ``-n auto`` case and the
    serial case alone would leave that shape unproven."""
    rc, out = _run_inner(tmp_path, "append", *xdist_args)
    assert "NOTE: nexus-pfuns" in out, out[-3000:]
    assert "FAIL: nexus-pfuns" not in out, out[-3000:]
    assert rc == 0, f"a growing append-only log must not fail the run (rc={rc})\n{out[-3000:]}"


def test_an_in_place_state_rewrite_reddens_a_real_run_under_xdist(tmp_path: Path) -> None:
    """The other half, and the half nexus-ist38 doubted: an in-place rewrite
    of production STATE fails the session, and the verdict survives ``-n``
    to the process exit code. (It does: the guard is controller-only, and a
    controller's ``session.exitstatus`` mutation in ``pytest_sessionfinish``
    is what ``wrap_session`` returns.) A green here with a FAIL line in the
    output would be the vacuous-gate shape this test exists to refuse."""
    rc, out = _run_inner(tmp_path, "state", "-n", "2")
    assert "FAIL: nexus-pfuns" in out, out[-3000:]
    assert "backfill_state.json" in out, out[-3000:]
    assert rc != 0, f"an in-place state rewrite must fail the run (rc={rc})\n{out[-3000:]}"
