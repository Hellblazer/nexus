# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/git-push-develop.sh writes one push record per push to develop (RDR-252 Step 2, nexus-igea9.2).

After ``PUSH_OK`` the script posts one tuple to ``board/ci/nexus-develop--push`` with dims ``from``,
``kind=push`` and ``sha=<tip>`` and the body ``{"base": <develop before the push>, "n": <commits>}``. RDR-253
Step 3's stamper (the record's reader) links ``base`` to the tip to order develop's tips, and trusts the
record's stamp, never ``from``. The record never fails the push: this script is the push train's own tool,
so a broken record path must cost a push nothing.

These tests drive a real bare origin and a clone, as test_git_push_develop_sh.py does, with an ``nx``
stand-in on PATH that logs every argv and answers the lock's calls. The engine-backed half (the record is
stamped and the session index maps the stamp to the pushing identity) is Step 3's ``OwnerDecisionTest`` and
the Activation MVV run, not faked here.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "git-push-develop.sh"
RECORD_SUBSPACE = "board/ci/nexus-develop--push"

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]

# The nx stand-in. Every call appends its argv (one JSON list per line) to $FAKE_NX_LOG. The lock's
# calls succeed: `tuple in` prints a claim id, `tuple release` and the lock's `tuple out` exit 0. A write
# to the record subspace exits $FAKE_NX_RECORD_EXIT (default 0) after printing $FAKE_NX_RECORD_OUT.
FAKE_NX = r"""#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_NX_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\n")
if argv[:2] == ["tuple", "in"]:
    print(json.dumps({"claim_id": "claim-1"}))
elif argv[:2] == ["tuple", "out"] and len(argv) > 2 and argv[2] == "board/ci/nexus-develop--push":
    out = os.environ.get("FAKE_NX_RECORD_OUT", "")
    if out:
        print(out, file=sys.stderr)
    sys.exit(int(os.environ.get("FAKE_NX_RECORD_EXIT", "0")))
sys.exit(0)
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *_GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _commit(work: Path, name: str) -> str:
    (work / name).write_text(name)
    _git("add", name, cwd=work)
    _git("commit", "-q", "-m", name, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


@pytest.fixture()
def repos(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=develop", cwd=origin)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--initial-branch=develop", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    _commit(work, "base")
    _git("push", "-q", "-u", "origin", "develop", cwd=work)
    return origin, work


@pytest.fixture()
def fake_nx(tmp_path: Path) -> tuple[Path, Path]:
    """(the directory holding the stand-in, its argv log). Under tmp_path: outside any .venv and outside
    the fixture repo, so the script's installed-nx walk accepts it."""
    d = tmp_path / "installed-nx"
    d.mkdir()
    nx = d / "nx"
    nx.write_text(FAKE_NX)
    nx.chmod(0o755)
    return d, tmp_path / "nx-calls.log"


def _clean_path() -> str:
    """PATH without any .venv/bin or this checkout, so the only nx the script can find is the stand-in
    (or none). git, python3 and the coreutils stay."""
    keep = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if not p or "/.venv/" in p + "/" or p.startswith(str(REPO_ROOT)):
            continue
        if (Path(p) / "nx").exists():
            continue
        keep.append(p)
    return os.pathsep.join(keep)


def _run(work: Path, *argv: str, nx_dir: Path | None, log: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("NX_PUSH_", "FAKE_NX_"))}
    path = _clean_path()
    if nx_dir is not None:
        path = f"{nx_dir}{os.pathsep}{path}"
    merged = {**base, "PATH": path, "FAKE_NX_LOG": str(log), "NX_PUSH_SKIP_SCOPE_AUDIT": "record test, scope not under test",
              "NX_PUSH_BATCH_MINUTES": "0", **(env or {})}
    return subprocess.run([str(SCRIPT), *argv], cwd=work, env=merged, capture_output=True, text=True, timeout=60)


def _calls(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _records(log: Path) -> list[list[str]]:
    return [c for c in _calls(log) if c[:3] == ["tuple", "out", RECORD_SUBSPACE]]


def _opt(call: list[str], flag: str) -> list[str]:
    return [call[i + 1] for i, a in enumerate(call) if a == flag and i + 1 < len(call)]


def test_push_ok_writes_one_push_record(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    base = _git("rev-parse", "HEAD", cwd=work)
    a = _commit(work, "a")
    b = _commit(work, "b")
    proc = _run(work, a, b, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = _records(log)
    assert _opt(record, "--key") == ["topic=nexus-develop--push"]
    assert sorted(_opt(record, "--dim")) == sorted(["from=unknown", "kind=push", f"sha={b}"])
    assert _opt(record, "--nonce") == [f"push-{b}"]
    (body,) = _opt(record, "--body")
    assert json.loads(body) == {"base": base, "n": 2}
    assert isinstance(json.loads(body)["n"], int)


def test_the_record_follows_the_lock_and_the_push(repos, fake_nx) -> None:
    """The lock path: claim, push, record, release; the record goes out after the push landed."""
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(log)
    kinds = [c[:2] for c in calls]
    i_claim = kinds.index(["tuple", "in"])
    i_record = calls.index(_records(log)[0])
    i_release = kinds.index(["tuple", "release"])
    assert i_claim < i_record < i_release
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a


def test_skip_lock_push_writes_the_record(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, "--skip-lock", "record test", a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PUSH_LOCK_SKIPPED" in proc.stderr
    assert [c[:2] for c in _calls(log)] == [["tuple", "out"]]  # no lock calls, only the record
    assert len(_records(log)) == 1


def test_skip_lock_with_no_installed_nx_writes_none_and_pushes(repos, tmp_path) -> None:
    origin, work = repos
    log = tmp_path / "nx-calls.log"
    a = _commit(work, "a")
    proc = _run(work, "--skip-lock", "record test", a, nx_dir=None, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"PUSH_OK n=1 tip={a}"
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a
    assert not log.exists()
    assert "PUSH_RECORD_SKIPPED" in proc.stderr


def test_push_to_another_branch_writes_none(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    _git("push", "-q", "origin", "develop:refs/heads/staging", cwd=work)
    _git("checkout", "-q", "-b", "staging", cwd=work)  # the script pushes the local branch of that name
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={"NX_PUSH_BRANCH": "staging"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git("rev-parse", "refs/heads/staging", cwd=origin) == a
    assert _records(log) == []


@pytest.mark.parametrize("skip_lock", [False, True], ids=["lock", "skip-lock"])
def test_nx_failure_never_fails_the_push(repos, fake_nx, skip_lock) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    argv = ["--skip-lock", "record test", a] if skip_lock else [a]
    proc = _run(work, *argv, nx_dir=nx_dir, log=log,
                env={"FAKE_NX_RECORD_EXIT": "1", "FAKE_NX_RECORD_OUT": "MaxLiveRowsExceeded\nsecond line"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"PUSH_OK n=1 tip={a}"
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a
    failed = [ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_RECORD_FAILED")]
    assert len(failed) == 1 and "MaxLiveRowsExceeded" in failed[0] and "second line" in failed[0]
    if not skip_lock:
        assert ["tuple", "release"] in [c[:2] for c in _calls(log)]  # the lock is still released


def test_a_missing_nx_mid_run_never_fails_the_push(repos, fake_nx) -> None:
    """The resolved nx vanishes between the lock and the record (an upgrade mid-push): one stderr line."""
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    gone = nx_dir / "nx"
    gone.write_text(FAKE_NX.replace(
        'elif argv[:2] == ["tuple", "out"] and len(argv) > 2 and argv[2] == "board/ci/nexus-develop--push":',
        'elif argv[:2] == ["tuple", "out"] and len(argv) > 2 and argv[2] == "board/ci/nexus-develop--push":\n'
        '    os.execv("/nonexistent/nx", ["nx"])'))
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"PUSH_OK n=1 tip={a}"
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a
    assert sum(ln.startswith("PUSH_RECORD_FAILED") for ln in proc.stderr.splitlines()) == 1


def test_no_claude_ancestor_writes_from_unknown(repos, fake_nx) -> None:
    """From a plain shell (no Claude session in the environment) the record says from=unknown; the
    stamper counts an unstamped record as no record for ownership but still chains its base."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    env = {k: "" for k in ("CLAUDE_CODE_SESSION_ID", "NX_SESSION_ID", "CLAUDECODE")}
    proc = _run(work, a, nx_dir=nx_dir, log=log, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = _records(log)
    assert "from=unknown" in _opt(record, "--dim")


def test_stdout_contract_unchanged(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    b = _commit(work, "b")
    proc = _run(work, a, b, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == f"PUSH_OK n=2 tip={b}\n"
    assert len(_records(log)) == 1


def test_a_noop_push_writes_no_record(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    proc = _run(work, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0 and proc.stdout.startswith("PUSH_NOOP")
    assert _records(log) == []


def test_a_refused_push_writes_no_record(repos, fake_nx) -> None:
    """A range the caller did not vouch for is refused before the push: nothing landed, nothing recorded."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    _commit(work, "b")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode != 0
    assert "PUSH_OK" not in proc.stdout
    assert _records(log) == []


def test_the_base_is_the_remote_tip_the_push_replaced(repos, fake_nx, tmp_path) -> None:
    """A peer pushed since this clone last pulled: base is the remote tip at push time, not this clone's
    old merge base (the stamper links base to tip, so a stale base would break the chain)."""
    origin, work = repos
    nx_dir, log = fake_nx
    peer = tmp_path / "peer"
    _git("clone", "-q", "--branch", "develop", str(origin), str(peer), cwd=tmp_path)
    p = _commit(peer, "peer")
    _git("push", "-q", "origin", "develop", cwd=peer)
    _git("pull", "-q", "--rebase", "origin", "develop", cwd=work)
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = _records(log)
    assert json.loads(_opt(record, "--body")[0])["base"] == p
