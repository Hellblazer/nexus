# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/git-push-develop.sh writes one push record per push to develop (RDR-252 Step 2, nexus-igea9.2).

After ``PUSH_OK`` the script posts one tuple to ``board/ci/nexus-develop--push`` with dims ``from``,
``kind=push`` and ``sha=<tip>`` and the body ``{"base": <develop before the push>, "n": <commits>}``. RDR-252
Step 3's owner stamper (nexus-igea9.3) reads it: base linked to tip orders develop's tips, and the record's
session stamp (added by ``nx tuple out`` once RDR-253 Step 3's client resolver is installed) names the
pusher; it never reads ``from``. The record never fails the push: this script is the push train's own
tool, so a broken record path must cost a push nothing.

These tests drive a real bare origin and a clone, as test_git_push_develop_sh.py does, with an ``nx``
stand-in on PATH that logs every argv, answers the lock's calls and echoes a tuple id on stdout as the real
``nx tuple out`` does. The engine-backed half (the record is stamped and the session index maps the stamp
to the pushing identity) is Step 3's ``OwnerDecisionTest`` and the Activation MVV run, not faked here; a
real ``nx tuple out`` of the record against a throwaway engine runs in test_git_push_develop_sh.py's
TestPushLock, whose pushes now write it.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "git-push-develop.sh"
RECORD_SUBSPACE = "board/ci/nexus-develop--push"

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]

# The nx stand-in. Every call appends its argv (one JSON list per line) to $FAKE_NX_LOG. The lock's calls
# succeed: `tuple in` prints a claim id, `tuple release` and the lock's `tuple out` exit 0. A write to the
# record subspace first logs ["@origin", <origin's develop tip now>] (from $FAKE_NX_ORIGIN), sleeps
# $FAKE_NX_RECORD_SLEEP seconds, prints $FAKE_NX_RECORD_OUT to stderr, echoes a tuple id on stdout on success (the
# real `nx tuple out` does), and exits $FAKE_NX_RECORD_EXIT (default 0).
FAKE_NX = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
argv = sys.argv[1:]
log = os.environ["FAKE_NX_LOG"]
def note(entry):
    with open(log, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
note(argv)
if argv[:2] == ["tuple", "in"]:
    print(json.dumps({"claim_id": "claim-1"}))
elif argv[:2] == ["tuple", "out"] and len(argv) > 2 and argv[2] == "board/ci/nexus-develop--push":
    origin = os.environ.get("FAKE_NX_ORIGIN")
    if origin:
        tip = subprocess.run(["git", "--git-dir", origin, "rev-parse", "refs/heads/develop"],
                             capture_output=True, text=True).stdout.strip()
        note(["@origin", tip])
    time.sleep(float(os.environ.get("FAKE_NX_RECORD_SLEEP", "0")))
    out = os.environ.get("FAKE_NX_RECORD_OUT", "")
    if out:
        print(out, file=sys.stderr)
    code = int(os.environ.get("FAKE_NX_RECORD_EXIT", "0"))
    if code == 0:
        print("tuple-id-0123456789abcdef")  # the real `nx tuple out` echoes the id only on success
    sys.exit(code)
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
    """PATH without any .venv/bin, this checkout, or any directory holding an nx, so the only nx the
    script can find is the stand-in (or none). git, python3 and the coreutils stay."""
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
    merged = {**base, "PATH": path, "FAKE_NX_LOG": str(log), "FAKE_NX_ORIGIN": str(work.parent / "origin"),
              "NX_PUSH_SKIP_SCOPE_AUDIT": "record test, scope not under test", "NX_PUSH_BATCH_MINUTES": "0",
              **(env or {})}
    return subprocess.run([str(SCRIPT), *argv], cwd=work, env=merged, capture_output=True, text=True, timeout=60)


def _log(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _calls(log: Path) -> list[list[str]]:
    return [c for c in _log(log) if c[:1] != ["@origin"]]


def _records(log: Path) -> list[list[str]]:
    return [c for c in _calls(log) if c[:3] == ["tuple", "out", RECORD_SUBSPACE]]


def _opt(call: list[str], flag: str) -> list[str]:
    return [call[i + 1] for i, a in enumerate(call) if a == flag and i + 1 < len(call)]


def test_push_ok_writes_one_push_record(repos, fake_nx) -> None:
    _origin, work = repos
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


def test_the_record_is_written_after_the_push_landed(repos, fake_nx) -> None:
    """When nx is called for the record, origin's develop is already the pushed tip."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [e for e in _log(log) if e[:1] == ["@origin"]] == [["@origin", a]]


def test_the_lock_is_released_before_the_record_is_written(repos, fake_nx) -> None:
    """Claim, push, release, then the record: a slow tuple space while the record is written never keeps
    other sessions' pushes waiting on the lock."""
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    calls = _calls(log)
    kinds = [c[:2] for c in calls]
    assert kinds.count(["tuple", "release"]) == 1  # released once, not again by the EXIT trap
    i_claim = kinds.index(["tuple", "in"])
    i_release = kinds.index(["tuple", "release"])
    i_record = calls.index(_records(log)[0])
    assert i_claim < i_release < i_record
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a


def test_a_push_git_rejects_writes_no_record_and_releases_the_lock(repos, fake_nx) -> None:
    """origin's pre-receive hook refuses the push: the script fails, nothing is recorded, the lock goes."""
    origin, work = repos
    nx_dir, log = fake_nx
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'rejected by test hook' >&2\nexit 1\n")
    hook.chmod(0o755)
    before = _git("rev-parse", "refs/heads/develop", cwd=origin)
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode != 0
    assert "PUSH_OK" not in proc.stdout
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == before
    assert _records(log) == []
    assert ["tuple", "release"] in [c[:2] for c in _calls(log)]


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
                env={"FAKE_NX_RECORD_EXIT": "1", "FAKE_NX_RECORD_OUT": "2026-11-18 [info] a log line\n\nError: MaxLiveRowsExceeded"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"PUSH_OK n=1 tip={a}"
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a
    failed = [ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_RECORD_FAILED")]
    assert len(failed) == 1
    assert "exit 1" in failed[0] and "MaxLiveRowsExceeded" in failed[0]  # nx's last line, its error
    if not skip_lock:
        assert ["tuple", "release"] in [c[:2] for c in _calls(log)]  # the lock is still released


def test_a_crashing_nx_never_fails_the_push(repos, fake_nx) -> None:
    """The resolved nx dies on the record write (it was replaced mid-push, say): one stderr line."""
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    (nx_dir / "nx").write_text(FAKE_NX.replace(
        'elif argv[:2] == ["tuple", "out"] and len(argv) > 2 and argv[2] == "board/ci/nexus-develop--push":',
        'elif argv[:2] == ["tuple", "out"] and len(argv) > 2 and argv[2] == "board/ci/nexus-develop--push":\n'
        '    os.execv("/nonexistent/nx", ["nx"])'))
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"PUSH_OK n=1 tip={a}"
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a
    assert sum(ln.startswith("PUSH_RECORD_FAILED") for ln in proc.stderr.splitlines()) == 1


def test_a_hung_write_is_stopped_and_never_holds_the_push(repos, fake_nx) -> None:
    origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    started = time.monotonic()
    proc = _run(work, a, nx_dir=nx_dir, log=log, env={"FAKE_NX_RECORD_SLEEP": "30", "NX_PUSH_RECORD_TIMEOUT_S": "1"})
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"PUSH_OK n=1 tip={a}"
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a
    assert elapsed < 15, f"the script waited {elapsed:.1f}s on a hung record write"
    assert any("ran past 1s" in ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_RECORD_FAILED"))


def test_no_claude_ancestor_writes_from_unknown(repos, fake_nx) -> None:
    """From a plain shell the record says from=unknown, as the RDR prescribes (the stamper counts it as no
    record for ownership and still chains its base). Today the seam returns unknown in every case, so this
    pins the label the stamper and readers see; it starts to discriminate once the seam resolves an
    identity through a shell verb for RDR-253's resolver."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    env = {k: "" for k in ("CLAUDE_CODE_SESSION_ID", "NX_SESSION_ID", "CLAUDECODE")}
    proc = _run(work, a, nx_dir=nx_dir, log=log, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = _records(log)
    assert "from=unknown" in _opt(record, "--dim")


def test_stdout_contract_unchanged(repos, fake_nx) -> None:
    """nx tuple out echoes a tuple id on stdout; none of it reaches the script's stdout."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    b = _commit(work, "b")
    proc = _run(work, a, b, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == f"PUSH_OK n=2 tip={b}\n"
    assert "tuple-id-" not in proc.stdout + proc.stderr
    assert len(_records(log)) == 1


def test_a_noop_push_writes_no_record(repos, fake_nx) -> None:
    _origin, work = repos
    nx_dir, log = fake_nx
    proc = _run(work, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0 and proc.stdout.startswith("PUSH_NOOP")
    assert _records(log) == []


def test_a_refused_push_writes_no_record(repos, fake_nx) -> None:
    """A range the caller did not vouch for is refused (exit 2) before the push: nothing recorded."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    _commit(work, "b")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stdout.startswith("PUSH_REFUSED_FOREIGN")
    assert _records(log) == []


def test_the_base_is_the_remote_tip_the_push_replaced(repos, fake_nx, tmp_path) -> None:
    """A peer pushed, and this clone's remote-tracking ref is stale (rewound here): base is the remote tip
    the script's own fetch reads, not the clone's stale ref (the stamper links base to tip, so a stale
    base would break the chain)."""
    origin, work = repos
    nx_dir, log = fake_nx
    old = _git("rev-parse", "HEAD", cwd=work)
    peer = tmp_path / "peer"
    _git("clone", "-q", "--branch", "develop", str(origin), str(peer), cwd=tmp_path)
    p = _commit(peer, "peer")
    _git("push", "-q", "origin", "develop", cwd=peer)
    _git("pull", "-q", "--rebase", "origin", "develop", cwd=work)
    _git("update-ref", "refs/remotes/origin/develop", old, cwd=work)  # stale tracking ref
    a = _commit(work, "a")
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = _records(log)
    assert json.loads(_opt(record, "--body")[0])["base"] == p


# ── the record against a real engine (a throwaway per-test tenant) ───────────────────

def _push_helpers():
    """test_git_push_develop_sh.py's fixtures and TestPushLock engine helpers, loaded by path as
    test_git_push_develop_sh_scrub.py does (that file is near its size ceiling)."""
    import importlib.util  # noqa: PLC0415 -- loaded only for this test
    import sys  # noqa: PLC0415

    name = "_git_push_develop_helpers"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_git_push_develop_sh.py"))
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _fields(row: dict) -> dict:
    """Every scalar key/value in a tuple row, wherever the row nests its dims and keys."""
    out: dict = {}
    for k, v in row.items():
        if isinstance(v, dict):
            out.update(_fields(v))
        elif isinstance(v, (str, int)) and k not in out:
            out[k] = v
    return out


def test_a_real_nx_writes_the_record_the_engine_serves_back(repos, tmp_path) -> None:
    """The first live run: the script's own `nx tuple out` against a real engine (a fresh tenant), read
    back with `nx tuple rd`. Proves the flags, the keys+nonce template and the body shape line up with the
    engine, which the stand-in above cannot."""
    h = _push_helpers()
    lock = h.TestPushLock
    state = lock._engine_state()
    _tenant, token = lock._mint(state)
    env = lock._lock_env(state, token, tmp_path, label="record")
    origin, work = repos
    base = _git("rev-parse", "HEAD", cwd=work)
    a = _commit(work, "a")
    proc = h._run(work, a, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == f"PUSH_OK n=1 tip={a}"
    assert "PUSH_RECORD_" not in proc.stderr, proc.stderr
    rd = subprocess.run(["nx", "tuple", "rd", RECORD_SUBSPACE, "--json"], env={**os.environ, **env},
                        capture_output=True, text=True, timeout=30)
    assert rd.returncode == 0, rd.stdout + rd.stderr
    rows = json.loads(rd.stdout)
    assert len(rows) == 1, rows
    row = rows[0]
    fields = _fields(row)
    assert fields.get("kind") == "push" and fields.get("from") == "unknown" and fields.get("sha") == a, row
    body = row.get("body")
    body = json.loads(body) if isinstance(body, str) else body
    assert body == {"base": base, "n": 1}, row
