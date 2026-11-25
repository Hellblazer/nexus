# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/git-push-develop.sh writes one push record per push to develop (RDR-252 Step 2, nexus-igea9.2).

After ``PUSH_OK`` the script runs ``nx ci record-push --base nexus-develop --from-sha <develop before the push>
--to-sha <tip> --count <commits>`` (nexus-igea9: the record's logic is nx's, so any repo's push script can call
it), which posts one tuple to ``board/ci/nexus-develop--push`` with dims ``from``, ``kind=push`` and ``sha=<tip>``
and the body ``{"base": <develop before the push>, "n": <commits>}``. RDR-252 Step 3's owner stamper
(nexus-igea9.3) reads it: base linked to tip orders develop's tips, and the record's session stamp names the
pusher; it never reads ``from``. The record never fails the push: this script is the push train's own
tool, so a broken record path must cost a push nothing.

These tests drive a real bare origin and a clone, as test_git_push_develop_sh.py does, with an ``nx``
stand-in on PATH that logs every argv, answers the lock's calls and prints the status line ``nx ci
record-push`` prints. What the command writes (the stamped record, the minimal fallback, ``from``, ``red_fix``) is
tests/test_ci_record_push.py's; the engine-backed half (the record is stamped and the session index maps the
stamp to the pushing identity) is Step 3's ``OwnerDecisionTest`` and the Activation MVV run, not faked here; a
real ``nx ci record-push`` against a throwaway engine runs in the last test of this file and in
test_git_push_develop_sh.py's TestPushLock, whose pushes now write it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from tests._tmp_root import child_env_with_tmpdir

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "git-push-develop.sh"
RECORD_SUBSPACE = "board/ci/nexus-develop--push"

_GIT_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]

# The nx stand-in. Every call appends its argv (one JSON list per line) to $FAKE_NX_LOG. The lock's calls
# succeed: `tuple in` prints a claim id, `tuple release` and the lock's `tuple out` exit 0. `ci record-push` first
# logs ["@origin", <origin's develop tip now>] (from $FAKE_NX_ORIGIN), sleeps $FAKE_NX_RECORD_SLEEP seconds, prints
# $FAKE_NX_RECORD_OUT to stderr, prints its status line ($FAKE_NX_RECORD_STATUS, default PUSH_RECORD_OK ...) on
# success, and exits $FAKE_NX_RECORD_EXIT (default 0). `ci check-hold` sleeps $FAKE_NX_GATE_SLEEP, prints
# $FAKE_NX_GATE_OUT (its lines, as the real command prints them) and exits $FAKE_NX_GATE_EXIT (default 0).
FAKE_NX = r"""#!/usr/bin/env python3
import json, os, subprocess, sys, time
argv = sys.argv[1:]
log = os.environ["FAKE_NX_LOG"]
env = os.environ.get
def note(entry):
    with open(log, "a") as fh:
        fh.write(json.dumps(entry) + "\n")
note(argv)
if argv[:2] == ["tuple", "in"]:
    print(json.dumps({"claim_id": "claim-1"}))
elif argv[:2] == ["ci", "check-hold"]:
    time.sleep(float(env("FAKE_NX_GATE_SLEEP", "0")))
    out = env("FAKE_NX_GATE_OUT", "")
    if out:
        print(out)
    sys.exit(int(env("FAKE_NX_GATE_EXIT", "0")))
elif argv[:3] == ["tuple", "out", "board/ci/nexus-develop--push"]:
    # the legacy record write an nx with no `ci record-push` is asked to do ($FAKE_NX_LEGACY_SLEEP/_OUT/_EXIT)
    time.sleep(float(env("FAKE_NX_LEGACY_SLEEP", "0")))
    out = env("FAKE_NX_LEGACY_OUT", "")
    if out:
        print(out, file=sys.stderr)
    code = int(env("FAKE_NX_LEGACY_EXIT", "0"))
    if code == 0:
        print("tuple-id-0123456789abcdef")  # the real `nx tuple out` echoes the id only on success
    sys.exit(code)
elif argv[:2] == ["ci", "record-push"]:
    origin = env("FAKE_NX_ORIGIN")
    if origin:
        tip = subprocess.run(["git", "--git-dir", origin, "rev-parse", "refs/heads/develop"],
                             capture_output=True, text=True).stdout.strip()
        note(["@origin", tip])
    time.sleep(float(env("FAKE_NX_RECORD_SLEEP", "0")))
    out = env("FAKE_NX_RECORD_OUT", "")
    if out:
        print(out, file=sys.stderr)
    code = int(env("FAKE_NX_RECORD_EXIT", "0"))
    if code == 0:
        print(env("FAKE_NX_RECORD_STATUS", "PUSH_RECORD_OK 0123456789ab from=unknown"))
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


def _env(work: Path, nx_dir: Path | None, log: Path, env: dict | None = None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("NX_PUSH_", "FAKE_NX_"))}
    path = _clean_path()
    if nx_dir is not None:
        path = f"{nx_dir}{os.pathsep}{path}"
    return {**base, "PATH": path, "FAKE_NX_LOG": str(log), "FAKE_NX_ORIGIN": str(work.parent / "origin"),
            "NX_PUSH_SKIP_SCOPE_AUDIT": "record test, scope not under test", "NX_PUSH_BATCH_MINUTES": "0",
            **(env or {})}


def _run(work: Path, *argv: str, nx_dir: Path | None, log: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPT), *argv], cwd=work, env=_env(work, nx_dir, log, env), capture_output=True,
                          text=True, timeout=60)


def _log(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _calls(log: Path) -> list[list[str]]:
    return [c for c in _log(log) if c[:1] != ["@origin"]]


def _records(log: Path) -> list[list[str]]:
    return [c for c in _calls(log) if c[:2] == ["ci", "record-push"]]


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
    assert _opt(record, "--base") == ["nexus-develop"]
    assert _opt(record, "--from-sha") == [base] and _opt(record, "--to-sha") == [b] and _opt(record, "--count") == ["2"]
    assert "--red-fix" not in record, "no hold was overridden"


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
    # no lock calls: the develop-red check and the record are the only calls (nexus-igea9)
    assert [c[:2] for c in _calls(log)] == [["ci", "check-hold"], ["ci", "record-push"]]
    assert len(_records(log)) == 1


def test_skip_lock_with_no_installed_nx_writes_none_and_pushes(repos, tmp_path) -> None:
    origin, work = repos
    log = tmp_path / "nx-calls.log"
    a = _commit(work, "a")
    proc = _run(work, "--skip-lock", "record test", a, nx_dir=None, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[0] == f"PUSH_OK n=1 tip={a}"  # a failed record adds its own line below (nexus-igea9 S3)
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
    assert proc.stdout.splitlines()[0] == f"PUSH_OK n=1 tip={a}"  # a failed record adds its own line below (nexus-igea9 S3)
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
        'elif argv[:2] == ["ci", "record-push"]:',
        'elif argv[:2] == ["ci", "record-push"]:\n'
        '    os.execv("/nonexistent/nx", ["nx"])'))
    proc = _run(work, a, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.splitlines()[0] == f"PUSH_OK n=1 tip={a}"  # a failed record adds its own line below (nexus-igea9 S3)
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
    assert proc.stdout.splitlines()[0] == f"PUSH_OK n=1 tip={a}"  # a failed record adds its own line below (nexus-igea9 S3)
    assert _git("rev-parse", "refs/heads/develop", cwd=origin) == a
    assert elapsed < 15, f"the script waited {elapsed:.1f}s on a hung record write"
    assert any("ran past 1s" in ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_RECORD_FAILED"))


def test_the_script_hands_the_record_command_no_identity_claim(repos, fake_nx) -> None:
    """``from`` is the command's to resolve (the pusher's identity id from its own token, else ``unknown``): the script must
    not derive a claim from its session environment and pass it on. Two pushes, one with a Claude session in the env and
    one from a plain shell, hand the command the same arguments; a script that read ``CLAUDE_CODE_SESSION_ID`` into the
    record fails the first (nexus-igea9.3 L7)."""
    _origin, work = repos
    nx_dir, log = fake_nx
    seen: list[list[str]] = []
    for env in ({"CLAUDE_CODE_SESSION_ID": "sess-claimed", "NX_SESSION_ID": "sess-claimed", "CLAUDECODE": "1"},
                {k: "" for k in ("CLAUDE_CODE_SESSION_ID", "NX_SESSION_ID", "CLAUDECODE")}):
        proc = _run(work, _commit(work, f"c{len(seen)}"), nx_dir=nx_dir, log=log, env=env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        seen.append(_records(log)[-1])
    assert not any("sess-claimed" in arg for call in seen for arg in call), seen
    assert not any("--from" in call or "--dim" in call for call in seen), seen  # --from-sha is a sha, never an identity


def test_a_timeout_with_a_leading_zero_is_read_in_base_10(repos, fake_nx) -> None:
    """``NX_PUSH_RECORD_TIMEOUT_S=08`` is 8 seconds. Read as bash octal it is an arithmetic error and the 20 s default
    silently wins (nexus-igea9.3 L7)."""
    _origin, work = repos
    nx_dir, log = fake_nx
    proc = _run(work, _commit(work, "a"), nx_dir=nx_dir, log=log,
                env={"FAKE_NX_RECORD_SLEEP": "30", "NX_PUSH_RECORD_TIMEOUT_S": "08"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "value too great for base" not in proc.stderr
    assert any("ran past 8s" in ln for ln in proc.stderr.splitlines() if ln.startswith("PUSH_RECORD_FAILED")), proc.stderr


def test_a_script_killed_mid_write_leaves_no_temp_file(repos, fake_nx, tmp_path: Path) -> None:
    """TERM while the record write runs: the EXIT trap removes the record's temp file (nexus-igea9.3 L7).

    The TERM is sent once the fake nx has logged the record call, not when the temp file first appears: the script
    creates the file with mktemp and records its name for the trap on the next line, so a TERM in between would leave
    the file by design of the test, not of the script (develop flake, CI run 44531708407 shard 1)."""
    _origin, work = repos
    nx_dir, log = fake_nx
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    a = _commit(work, "a")
    env = child_env_with_tmpdir(_env(work, nx_dir, log, {"FAKE_NX_RECORD_SLEEP": "30", "NX_PUSH_RECORD_TIMEOUT_S": "25"}),
                                tmpdir)
    proc = subprocess.Popen([str(SCRIPT), a], cwd=work, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        while not _records(log):                         # the nx call runs only after the trap knows the file
            assert proc.poll() is None and time.monotonic() < deadline, "the record write never started"
            time.sleep(0.1)
        assert list(tmpdir.glob("push-record.*")), "the write is under way, so its temp file exists"
        proc.terminate()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert list(tmpdir.glob("push-record.*")) == [], "the record's temp file outlived the script"


def test_stdout_contract_unchanged(repos, fake_nx) -> None:
    """The record command's PUSH_RECORD_OK line is the normal case and adds nothing to the summary."""
    _origin, work = repos
    nx_dir, log = fake_nx
    a = _commit(work, "a")
    b = _commit(work, "b")
    proc = _run(work, a, b, nx_dir=nx_dir, log=log)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == f"PUSH_OK n=2 tip={b}\n"
    assert "PUSH_RECORD_" not in proc.stdout + proc.stderr
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
    assert _opt(record, "--from-sha") == [p]


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
    """The first live run: the script's own `nx ci record-push` against a real engine (a fresh tenant), read
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
    assert proc.stdout.splitlines()[0] == f"PUSH_OK n=1 tip={a}"  # a failed record adds its own line below (nexus-igea9 S3)
    # The record landed: no FAILED, FALLBACK or LEGACY line. A throwaway env with no session token and no identity token has
    # nothing to stamp it with, and the command says so (PUSH_RECORD_UNSTAMPED); inside a Claude session it is stamped, silent.
    assert not any(w in proc.stderr for w in ("PUSH_RECORD_FAILED", "PUSH_RECORD_FALLBACK", "PUSH_RECORD_LEGACY", "PUSH_RECORD_SKIPPED")), proc.stderr
    assert all(ln.startswith("PUSH_RECORD_UNSTAMPED ") for ln in proc.stderr.splitlines() if ln.startswith("PUSH_RECORD_")), proc.stderr
    rd = subprocess.run(["nx", "tuple", "rd", RECORD_SUBSPACE, "--json"], env={**os.environ, **env},
                        capture_output=True, text=True, timeout=30)
    assert rd.returncode == 0, rd.stdout + rd.stderr
    rows = json.loads(rd.stdout)
    assert len(rows) == 1, rows
    row = rows[0]
    fields = _fields(row)
    assert fields.get("kind") == "push" and fields.get("sha") == a, row
    # the pusher's identity id when this process has one (a run inside a Claude session does), else unknown
    assert fields.get("from") == "unknown" or re.fullmatch(r"i[0-9]+", str(fields.get("from"))), row
    body = row.get("body")
    body = json.loads(body) if isinstance(body, str) else body
    assert body == {"base": base, "n": 1}, row
