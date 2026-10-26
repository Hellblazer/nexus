# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Shell claims hold once even when mkdir(1) reports a lost race as success.

nexus-bo01z. uutils coreutils 0.8.0, ``/usr/bin/mkdir`` on Ubuntu 26.04, checks
for the path and then creates it, and reports the EEXIST of a lost race as
success: eight simultaneous ``mkdir D`` all exit 0 there (measured on
qwentescence for nexus-6japn). GNU and BSD mkdir let exactly one through. So a
bare ``mkdir`` is not a mutex on that host, and every shell claim that used one
let every racer in at once.

The stand-in below reproduces that behaviour on any host with a wide window, and
logs each race it lost, so each test can assert a race actually happened before
it asserts the claim held. The count is of SIMULTANEOUS holders, never of exit
codes: a racer that thinks it holds the claim marks a file and keeps holding
until the test releases everyone.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from _racy_mkdir import executable, racy_mkdir_bin

_REPO = Path(__file__).resolve().parents[2]
_LEASE_LIB = _REPO / "scripts" / "lib" / "build-lease.sh"
_INSTALLER = _REPO / "src" / "nexus" / "_install" / "install_generation.sh"


def _race(tmp_path: Path, scripts: list[str], env: dict[str, str], go: Path,
          release: Path) -> tuple[int, list[subprocess.Popen]]:
    """Start every script, open the gate, and count how many hold at once.

    Each script waits for *go*, tries to claim, and on success touches its
    ``held-<i>`` file and waits for *release*. The count is taken once every
    racer has either exited or marked itself a holder.
    """
    procs = [subprocess.Popen(["bash", "-c", s], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) for s in scripts]
    time.sleep(0.5)
    go.touch()
    deadline = time.monotonic() + 60
    held = [tmp_path / f"held-{i}" for i in range(len(scripts))]
    while time.monotonic() < deadline:
        if all(p.poll() is not None or h.exists() for p, h in zip(procs, held)):
            break
        time.sleep(0.05)
    # Settle: a racer that lost a pid race may still be on its way to refusing.
    time.sleep(0.5)
    holders = sum(h.exists() for h in held)
    release.touch()
    for p in procs:
        p.communicate(timeout=60)
    return holders, procs


def _lease_env(tmp_path: Path, shim: Path) -> dict[str, str]:
    root = tmp_path / "lease-root"
    root.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("NX_BUILD_LEASE_ROOT", "NX_BUILD_LEASE_WAIT")}
    env.update(PATH=f"{shim}{os.pathsep}{os.environ['PATH']}", NX_BUILD_LEASE_ROOT=str(root))
    return env


def _lease_racer(i: int, tmp_path: Path, go: Path, release: Path, *, after: str = "") -> str:
    # *after* is a shell condition the racer waits on, past *go*, before its one
    # attempt.
    wait_after = f"while ! {after}; do :; done\n" if after else ""
    return (f'. {_LEASE_LIB}\nwhile [ ! -e {go} ]; do :; done\n{wait_after}'
            f'build_lease_acquire svc 2>/dev/null || exit $?\n'
            f': > {tmp_path}/held-{i}\nwhile [ ! -e {release} ]; do sleep 0.05; done\n'
            f'build_lease_release svc\n')


def test_build_lease_fresh_claim_has_one_holder_under_racy_mkdir(tmp_path: Path) -> None:
    shim, log = racy_mkdir_bin(tmp_path)
    env = _lease_env(tmp_path, shim)
    go, release = tmp_path / "go", tmp_path / "release"
    racers = 6
    holders, procs = _race(tmp_path, [_lease_racer(i, tmp_path, go, release)
                                      for i in range(racers)], env, go, release)
    assert log.exists() and log.read_text(encoding="utf-8").strip(), \
        "no mkdir lost a race: the test proved nothing"
    assert holders == 1, (holders, [p.returncode for p in procs])
    # Every racer either held (and released, rc 0) or was refused as held (75).
    assert sorted(p.returncode for p in procs) == [0] + [75] * (racers - 1), \
        [p.returncode for p in procs]


def test_build_lease_stale_reclaim_has_one_holder_under_racy_mkdir(tmp_path: Path) -> None:
    """The reclaim path re-creates the lease with its own mkdir, after renaming
    the stale one away. Fresh acquirers that arrive in that gap race it."""
    shim, log = racy_mkdir_bin(tmp_path)
    env = _lease_env(tmp_path, shim)
    dead = subprocess.run(["bash", "-c", "echo $$"], capture_output=True, text=True,
                          check=True).stdout.strip()
    stale = Path(env["NX_BUILD_LEASE_ROOT"]) / "svc"
    stale.mkdir()
    (stale / "pid").write_text(f"{dead}\n", encoding="utf-8")
    go, release = tmp_path / "go", tmp_path / "release"
    # Racer 0 is the only one that sees the stale lease and reclaims it (its
    # first mkdir fails on the existing lease and marks nothing). The others
    # start once its re-create mkdir is inside the window, so the reclaimer is
    # the one that really creates the directory.
    checked = tmp_path / "raced.checked"
    scripts = [_lease_racer(0, tmp_path, go, release)] + [
        _lease_racer(i, tmp_path, go, release, after=f'[ -n "$(ls -A {checked})" ]')
        for i in range(1, 4)]
    holders, procs = _race(tmp_path, scripts, env, go, release)
    assert log.exists() and log.read_text(encoding="utf-8").strip(), \
        "no mkdir lost a race: the test proved nothing"
    assert holders == 1, (holders, [p.returncode for p in procs])


def _stub_tools(bin_dir: Path, inside: Path, release: Path) -> None:
    """``uv venv`` records the generation it was handed and blocks until release;
    ``date`` pins the stamp so every racer collides on the same second."""
    real_date = shutil.which("date")
    assert real_date
    executable(bin_dir / "uv", f"""#!/bin/bash
if [ "$1" = venv ]; then
    for a in "$@"; do t="$a"; done
    echo "$t" > "{inside}/$PPID"
    while [ ! -e "{release}" ]; do sleep 0.05; done
fi
exit 1
""")
    executable(bin_dir / "date", f"""#!/bin/bash
[ "$*" = "-u +%Y%m%dT%H%M%SZ" ] && {{ echo 20261007T120000Z; exit 0; }}
exec "{real_date}" "$@"
""")


def test_install_generation_claim_is_exclusive_under_racy_mkdir(tmp_path: Path) -> None:
    shim, log = racy_mkdir_bin(tmp_path)
    inside, release, go = tmp_path / "inside", tmp_path / "release", tmp_path / "go"
    inside.mkdir()
    _stub_tools(shim, inside, release)
    tools = tmp_path / "tools"
    tools.mkdir()
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
           "NX_TOOLS_DIR": str(tools), "HOME": str(tmp_path / "home")}
    racers = 4
    procs = [subprocess.Popen(
        ["bash", "-c", f'while [ ! -e {go} ]; do :; done\n'
                       f'exec bash {_INSTALLER} --source conexus --version 7.18.0'],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(racers)]
    time.sleep(0.5)
    go.touch()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if len(list(inside.iterdir())) + sum(p.poll() is not None for p in procs) >= racers:
            break
        time.sleep(0.05)
    time.sleep(0.5)
    claimed = [f.read_text(encoding="utf-8").strip() for f in inside.iterdir()]
    release.touch()
    errs = [p.communicate(timeout=60)[1][-300:] for p in procs]
    assert log.exists() and log.read_text(encoding="utf-8").strip(), \
        "no mkdir lost a race: the test proved nothing"
    # Every racer reached its build, and no two were building into the same tree.
    assert len(claimed) == racers, (claimed, errs)
    assert len(set(claimed)) == racers, (sorted(claimed), errs)


# ---- tests/e2e/lib/lock.sh (nexus-x8sk8) ---------------------------------------------------------
# lock_acquire does not claim with mkdir: mkdir only makes a pid-unique staging directory, and the
# claim is the `mv` of that populated directory onto the lock path plus a pid readback. Two lanes
# prove it: the racy mkdir (which cannot collide on a unique name, so no race is logged here, only
# that the shim ran), and an `mv` that nests into an existing directory and exits 0, the shape
# every non-Linux mv has and the one a uutils mv may add under a race.

_LOCK_LIB = _REPO / "tests" / "e2e" / "lib" / "lock.sh"

# Every mv waits at a barrier until all racers are at their mv (so all passed lock.sh's
# `[[ ! -e lockdir ]]` check while the lock path was vacant), then staggers by arrival rank: the
# first moves its staging dir onto the lock path, the rest find a directory there, nest into it and
# exit 0. Each nest is logged, so the test can assert it was exercised.
_NESTING_MV = """#!/bin/bash
for a in "$@"; do src="$dst"; dst="$a"; done
: > "{arrivals}/$$"
for _ in $(seq 1 100); do
    [ "$(ls "{arrivals}" | wc -l)" -ge {racers} ] && break
    sleep 0.05
done
rank=$(ls "{arrivals}" | awk -v me="$$" '$1 + 0 <= me + 0' | wc -l | tr -d ' ')
sleep "0.${{rank}}5"
if [ -d "$dst" ]; then
    echo "$src -> $dst" >> "{nested}"
    "{real}" "$src" "$dst/"
    exit 0
fi
exec "{real}" "$@"
"""


def _lock_racer(i: int, tmp_path: Path, go: Path, release: Path, lockdir: Path) -> str:
    return (f'. {_LOCK_LIB}\nwhile [ ! -e {go} ]; do :; done\n'
            f'lock_acquire {lockdir} 0 2>/dev/null || exit $?\n'
            f': > {tmp_path}/held-{i}\nwhile [ ! -e {release} ]; do sleep 0.05; done\n'
            f'lock_release {lockdir}\n')


def test_lock_acquire_has_one_holder_under_racy_mkdir(tmp_path: Path) -> None:
    shim, _log = racy_mkdir_bin(tmp_path)
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    go, release, lockdir = tmp_path / "go", tmp_path / "release", tmp_path / "lock"
    racers = 6
    holders, procs = _race(tmp_path, [_lock_racer(i, tmp_path, go, release, lockdir)
                                      for i in range(racers)], env, go, release)
    # lock_acquire's staging names are pid-unique, so no mkdir loses a race; what the shim proves is
    # that every racer's mkdir went through it (and so through the widened window).
    assert len(list((tmp_path / "raced.checked").iterdir())) >= racers, "the racy mkdir never ran"
    assert holders == 1, (holders, [p.returncode for p in procs])
    assert sorted(p.returncode for p in procs) == [0] + [1] * (racers - 1), \
        [p.returncode for p in procs]


def test_lock_acquire_has_one_holder_when_mv_nests_into_the_claimed_lock(tmp_path: Path) -> None:
    racers = 6
    real_mv = shutil.which("mv")
    assert real_mv
    arrivals, nested = tmp_path / "arrivals", tmp_path / "nested"
    arrivals.mkdir()
    shim = tmp_path / "bin"
    executable(shim / "mv", _NESTING_MV.format(arrivals=arrivals, nested=nested, real=real_mv,
                                               racers=racers))
    env = {**os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}
    go, release, lockdir = tmp_path / "go", tmp_path / "release", tmp_path / "lock"
    holders, procs = _race(tmp_path, [_lock_racer(i, tmp_path, go, release, lockdir)
                                      for i in range(racers)], env, go, release)
    assert nested.exists() and nested.read_text(encoding="utf-8").strip(), \
        "no mv nested into the claimed lock: the test proved nothing"
    assert holders == 1, (holders, [p.returncode for p in procs])
    assert sorted(p.returncode for p in procs) == [0] + [1] * (racers - 1), \
        [p.returncode for p in procs]
