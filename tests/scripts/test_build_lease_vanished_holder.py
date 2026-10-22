# SPDX-License-Identifier: AGPL-3.0-or-later
"""A holder that releases between a contender's failed claim and its directory
check is contention, not a filesystem error (nexus-etut9).

build_lease_acquire's claim fails with EEXIST while P holds the lease; if P
releases before Q's ``[[ -d "$dir" ]]``, Q saw no directory and reported
"could not create build lease ... not because it already exists" with rc 74,
instead of claiming the now-free lease. Develop CI 39780138434 (e7917e691)
shard 4 hit it in local_service_gate_guard_test.sh Test 3.

Each test replaces ``_build_lease_claim`` with one that reproduces that
interleave exactly once: the holder's directory exists when mkdir fails, and
is gone before the claim returns. Its call log proves the interleave ran.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LEASE_LIB = _REPO / "scripts" / "lib" / "build-lease.sh"

# Call N of the claim (counted in $CALLS, since each call runs in a command
# substitution) is the interleave when N is listed in $VANISH_ON: a holder creates the
# directory, our mkdir fails EEXIST, and the holder releases. Every other call
# is the real claim.
_STUB = r'''
_build_lease_claim() {
    echo x >> "$CALLS"
    case " $VANISH_ON " in *" $(wc -l < "$CALLS" | tr -d ' ') "*)
        mkdir "$1" 2>/dev/null
        mkdir "$1"; local rc=$?
        rm -rf "$1"
        return $rc ;;
    esac
    mkdir "$1" && (set -C; printf '%s\n' "$$" > "$1/pid") 2>/dev/null
}
'''


def _acquire(tmp_path: Path, vanish_on: int | str, prelude: str = "") -> tuple[int, str, str, int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "leases"
    calls = tmp_path / "calls"
    script = (
        f'source "{_LEASE_LIB}"\n{_STUB}\n{prelude}\n'
        'rc=0; build_lease_acquire service test || rc=$?\n'
        'echo "rc=$rc"; cat "$(_build_lease_dir service)/pid" 2>/dev/null; echo "self=$$"\n'
    )
    env = dict(os.environ, NX_BUILD_LEASE_ROOT=str(root), CALLS=str(calls), VANISH_ON=str(vanish_on))
    p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    lines = p.stdout.split()
    rc = int(next(x for x in lines if x.startswith("rc="))[3:])
    n_calls = len(calls.read_text().splitlines()) if calls.exists() else 0
    return rc, p.stdout, p.stderr, n_calls


def test_a_holder_releasing_mid_claim_lets_the_contender_claim(tmp_path: Path) -> None:
    rc, out, err, n_calls = _acquire(tmp_path, vanish_on=1)
    assert n_calls >= 1, f"the interleave did not run\n{err}"
    assert "not because it already exists" not in err, err
    assert rc == 0, f"rc={rc}\n{err}"
    assert n_calls == 2, f"expected one retry after the vanished holder, got {n_calls} call(s)"
    holder, self_pid = out.split()[1], out.split()[2]
    assert self_pid == f"self={holder}", out


def test_the_same_interleave_after_a_stale_reclaim(tmp_path: Path) -> None:
    """The re-create after a stale reclaim has the same check-then-act shape."""
    prelude = (
        'd="$(_build_lease_dir service)"; mkdir -p "$d"; '
        "echo 999999 > \"$d/pid\"; echo stale > \"$d/label\""
    )
    rc, out, err, n_calls = _acquire(tmp_path, vanish_on=2, prelude=prelude)
    assert "reclaiming stale build lease" in err, err
    assert n_calls >= 2, f"the interleave did not run: {n_calls} claim call(s)\n{err}"
    assert "not because someone else claimed it first" not in err, err
    assert rc == 0, f"rc={rc}\n{err}"
    assert n_calls == 3, f"expected one retry after the vanished holder, got {n_calls} call(s)"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the directory mode")
def test_a_real_filesystem_error_is_still_74(tmp_path: Path) -> None:
    root = tmp_path / "leases"
    root.mkdir()
    root.chmod(0o500)
    try:
        script = f'source "{_LEASE_LIB}"\nrc=0; build_lease_acquire service test || rc=$?; echo "rc=$rc"\n'
        env = dict(os.environ, NX_BUILD_LEASE_ROOT=str(root))
        p = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
    finally:
        root.chmod(0o700)
    assert "rc=74" in p.stdout, p.stdout + p.stderr
    assert "not because it already exists" in p.stderr, p.stderr
    assert "ermission denied" in p.stderr, f"mkdir's own error is not in the message: {p.stderr}"


def test_two_vanishes_still_claim_and_three_are_reported_as_an_error(tmp_path: Path) -> None:
    """The retry bound, _BUILD_LEASE_CLAIM_ATTEMPTS=3: a holder that comes and
    goes on every attempt is indistinguishable from a filesystem error."""
    rc, _, err, n_calls = _acquire(tmp_path / "two", vanish_on="1 2")
    assert (rc, n_calls) == (0, 3), err
    rc, _, err, n_calls = _acquire(tmp_path / "three", vanish_on="1 2 3")
    assert (rc, n_calls) == (74, 3), err
    assert "not because it already exists" in err, err
