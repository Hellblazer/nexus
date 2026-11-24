# SPDX-License-Identifier: AGPL-3.0-or-later
"""The managed pre-push hook (RDR-252 "Every repository" item 4, nexus-igea9).

A managed ``pre-push`` stanza calls ``nx ci check-hold`` (a refused push gets no record) and starts a detached job that
waits, at most ten minutes, until the remote branch shows the pushed tip and only then calls ``nx ci record-push``.

These tests drive REAL ``git push`` runs against a real bare remote, with an ``nx`` stand-in on PATH that logs every
argv. What ``check-hold`` and ``record-push`` decide and write is tests/test_ci_push_gate.py's and
tests/test_ci_record_push.py's; here the hook's own behaviour is the subject: when it refuses, when it records, that it
sits beside an existing hook body and honours ``core.hooksPath``, that the push script's push is not recorded twice,
which repositories ``nx upgrade`` installs it in, and that ``nx doctor`` sees the hook type.

No test touches a real repository's hooks: every repo is a temp repo.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus import _git_hooks_meta as meta
from nexus.cli import main
from nexus.commands import hooks as hooks_mod
from nexus.commands.upgrade import _refresh_all_git_hooks
from nexus.groups import GroupRecord
from nexus.health import _check_git_hooks
from nexus.pre_push_hook import serving, stanza
from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END

_ID = ["-c", "user.email=t@t", "-c", "user.name=t"]
ZERO = "0" * 40

# The nx stand-in: appends {"argv", "origin_tip"} per call to $FAKE_NX_LOG. check-hold prints $FAKE_NX_GATE_OUT and exits
# $FAKE_NX_GATE_EXIT; record-push logs the tip the remote shows at that moment (its --branch), which is the proof the
# record is written only after the push landed.
FAKE_NX = r"""#!/usr/bin/env python3
import json, os, subprocess, sys
argv = sys.argv[1:]
env = os.environ.get
entry = {"argv": argv}
if argv[:2] == ["ci", "record-push"]:
    branch = argv[argv.index("--branch") + 1] if "--branch" in argv else "develop"
    entry["origin_tip"] = subprocess.run(
        ["git", "--git-dir", env("FAKE_NX_ORIGIN"), "rev-parse", "refs/heads/" + branch],
        capture_output=True, text=True).stdout.strip()
with open(env("FAKE_NX_LOG"), "a") as fh:
    fh.write(json.dumps(entry) + "\n")
if argv[:2] == ["tuple", "in"]:
    print(json.dumps({"claim_id": "claim-1"}))   # the push script's lock
if argv[:2] == ["ci", "check-hold"]:
    import time
    time.sleep(float(env("FAKE_NX_GATE_SLEEP", "0")))
    out = env("FAKE_NX_GATE_OUT", "")
    if out:
        print(out)
    sys.exit(int(env("FAKE_NX_GATE_EXIT", "0")))
if argv[:2] == ["ci", "record-push"]:
    print("PUSH_RECORD_OK 0123456789ab from=unknown")
sys.exit(0)
"""


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_ID, *args], cwd=cwd, check=check, capture_output=True, text=True)


def _commit(work: Path, name: str) -> str:
    (work / name).write_text(name)
    _git("add", name, cwd=work)
    _git("commit", "-q", "-m", name, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work).stdout.strip()


class Rig:
    """A work repo, its bare remote (``<tmp>/acme/<name>.git``, so the base derives as ``<name>-develop``), the nx
    stand-in, and the environment a push runs in."""

    def __init__(self, tmp: Path, name: str = "rig") -> None:
        self.tmp = tmp
        self.origin = tmp / "acme" / f"{name}.git"
        self.origin.mkdir(parents=True)
        _git("init", "-q", "--bare", "--initial-branch=develop", cwd=self.origin)
        self.work = tmp / f"{name}-work"
        self.work.mkdir()
        _git("init", "-q", "--initial-branch=develop", cwd=self.work)
        _git("remote", "add", "origin", str(self.origin), cwd=self.work)
        self.base = _commit(self.work, "base")
        _git("push", "-q", "-u", "origin", "develop", cwd=self.work)
        self.nx_dir = tmp / f"{name}-bin"
        self.nx_dir.mkdir()
        (self.nx_dir / "nx").write_text(FAKE_NX)
        (self.nx_dir / "nx").chmod(0o755)
        self.log = tmp / f"{name}-nx.log"
        self.config = tmp / f"{name}-config"
        self.config.mkdir()

    @property
    def hooks_dir(self) -> Path:
        return self.work / ".git" / "hooks"

    def env(self, **extra: str) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if not k.startswith(("NX_PUSH_", "NX_PRE_PUSH", "FAKE_NX_"))}
        return {**base, "PATH": f"{self.nx_dir}{os.pathsep}{base['PATH']}", "FAKE_NX_LOG": str(self.log),
                "FAKE_NX_ORIGIN": str(self.origin), "NEXUS_CONFIG_DIR": str(self.config),
                "NX_PRE_PUSH_POLL_S": "0.2", "NX_PRE_PUSH_WAIT_S": "30", **extra}

    def install(self) -> None:
        hooks_mod._install_hook(hooks_mod._effective_hooks_dir(self.work), "pre-push")

    def push(self, ref: str = "develop", **extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *_ID, "push", "-q", "origin", f"HEAD:refs/heads/{ref}"], cwd=self.work,
                              env=self.env(**extra), capture_output=True, text=True, timeout=60)

    def calls(self) -> list[dict]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def verbs(self, verb: str) -> list[dict]:
        return [c for c in self.calls() if c["argv"][:2] == ["ci", verb]]

    def wait_for_records(self, n: int = 1, seconds: float = 20.0) -> list[dict]:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if len(self.verbs("record-push")) >= n:
                break
            time.sleep(0.1)
        return self.verbs("record-push")


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _opt(call: dict, flag: str) -> list[str]:
    a = call["argv"]
    return [a[i + 1] for i, x in enumerate(a) if x == flag and i + 1 < len(a)]


# ── the stanza ────────────────────────────────────────────────────────────────


def test_the_stanza_is_sentinel_bounded_and_the_state_word_is_armed(rig: Rig) -> None:
    rig.install()
    text = (rig.hooks_dir / "pre-push").read_text()
    assert text.startswith("#!/bin/sh\n") and SENTINEL_BEGIN in text and text.rstrip().endswith(SENTINEL_END)
    assert hooks_mod.hook_stanza_state(rig.work, "pre-push") == "armed"
    assert hooks_mod.stanza_body(text) == hooks_mod.stanza_body(hooks_mod._stanza_for("pre-push"))
    assert hooks_mod._stanza_for("pre-push") != hooks_mod._stanza_for("post-commit")


# ── a refused push gets no record; an allowed push is recorded once, after it landed ──────────────────────────────────


def test_a_held_push_is_refused_and_writes_no_record(rig: Rig) -> None:
    rig.install()
    tip = _commit(rig.work, "a")
    proc = rig.push(FAKE_NX_GATE_EXIT="13", FAKE_NX_GATE_OUT="PUSH_REFUSED_DEVELOP_RED nexus-develop is red")
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "PUSH_REFUSED_DEVELOP_RED" in proc.stdout + proc.stderr, "the hook shows check-hold's refusal"
    assert _git("rev-parse", "refs/heads/develop", cwd=rig.origin).stdout.strip() == rig.base, "the push did not land"
    (hold,) = rig.verbs("check-hold")
    assert _opt(hold, "--remote") == ["origin"] and _opt(hold, "--branch") == ["develop"] and _opt(hold, "--tip") == [tip]
    time.sleep(1.5)   # a started job would have recorded by now (poll 0.2 s)
    assert rig.verbs("record-push") == [], "a refused push is never recorded"
    assert not (rig.config / "pre-push.log").exists(), "no detached job was started"


def test_an_allowed_push_is_recorded_once_after_the_remote_shows_the_tip(rig: Rig) -> None:
    rig.install()
    tip = _commit(rig.work, "a")
    proc = rig.push()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = rig.wait_for_records()
    assert record["origin_tip"] == tip, "nx ci record-push ran only once the remote branch showed the pushed tip"
    assert _opt(record, "--remote") == ["origin"] and _opt(record, "--branch") == ["develop"]
    assert _opt(record, "--from-sha") == [rig.base] and _opt(record, "--to-sha") == [tip] and _opt(record, "--count") == ["1"]
    assert "--red-fix" not in record["argv"]
    time.sleep(1.0)
    assert len(rig.verbs("record-push")) == 1, "recorded once"
    assert (rig.config / "pre-push.log").exists(), "the job logs under the nexus state dir"


def test_a_new_branch_records_with_an_empty_from_sha(rig: Rig) -> None:
    rig.install()
    _git("checkout", "-q", "-b", "main", cwd=rig.work)
    tip = _commit(rig.work, "m")
    assert rig.push("main").returncode == 0
    (record,) = rig.wait_for_records()
    assert _opt(record, "--from-sha") == [""] and _opt(record, "--to-sha") == [tip] and _opt(record, "--branch") == ["main"]
    assert "--count" not in record["argv"]


def test_a_red_fix_override_check_hold_applied_is_carried_into_the_record(rig: Rig) -> None:
    rig.install()
    _commit(rig.work, "a")
    proc = rig.push(NX_PUSH_RED_FIX="the fix", FAKE_NX_GATE_OUT="PUSH_RED_FIX_APPLIED the fix (nexus-develop is red)")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = rig.wait_for_records()
    assert _opt(record, "--red-fix") == ["the fix"]
    (hold,) = rig.verbs("check-hold")
    assert _opt(hold, "--red-fix") == ["the fix"]


def test_a_branch_the_board_does_not_watch_is_neither_checked_nor_recorded(rig: Rig) -> None:
    rig.install()
    _commit(rig.work, "a")
    assert rig.push("feature/x").returncode == 0
    time.sleep(1.0)
    assert rig.calls() == []


def test_a_check_that_cannot_run_fails_open_and_the_push_is_still_recorded(rig: Rig) -> None:
    """check-hold exiting 2 (a base it cannot derive) or crashing holds nobody: the push goes through."""
    rig.install()
    tip = _commit(rig.work, "a")
    proc = rig.push(FAKE_NX_GATE_EXIT="2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=rig.origin).stdout.strip() == tip


def test_a_box_without_nx_pushes_normally(rig: Rig) -> None:
    rig.install()
    tip = _commit(rig.work, "a")
    (rig.nx_dir / "nx").unlink()
    env = rig.env()
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if not (Path(p) / "nx").exists())
    proc = subprocess.run(["git", *_ID, "push", "-q", "origin", "HEAD:refs/heads/develop"], cwd=rig.work, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=rig.origin).stdout.strip() == tip


def test_the_job_gives_up_without_error_when_the_tip_never_shows(rig: Rig) -> None:
    """A push another hook refused leaves the remote without the tip: the job stops after its wait, writes nothing to
    the board and exits 0."""
    rig.install()
    _commit(rig.work, "a")
    # A second hook body, appended after ours, refuses the push once ours has started its job.
    hook = rig.hooks_dir / "pre-push"
    hook.write_text(hook.read_text() + "\nexit 1\n")
    proc = rig.push(NX_PRE_PUSH_WAIT_S="1")
    assert proc.returncode != 0
    time.sleep(2.5)
    assert rig.verbs("record-push") == []
    log = (rig.config / "pre-push.log").read_text()
    assert "gave up" in log


# ── beside an existing hook, and core.hooksPath ──────────────────────────────────────────────────────────────────────


def _existing_body(tmp: Path) -> tuple[str, Path, Path]:
    stdin_copy, ran = tmp / "beads-stdin.txt", tmp / "beads-ran.txt"
    body = (f"#!/usr/bin/env sh\n# beads pre-push\ncat > {stdin_copy}\necho \"$1\" >> {ran}\nexec true\n")
    return body, stdin_copy, ran


def test_the_stanza_sits_beside_an_existing_hook_body_that_ends_in_exec(rig: Rig, tmp_path: Path) -> None:
    body, stdin_copy, ran = _existing_body(tmp_path)
    (rig.hooks_dir / "pre-push").write_text(body)
    (rig.hooks_dir / "pre-push").chmod(0o755)
    rig.install()
    text = (rig.hooks_dir / "pre-push").read_text()
    assert "# beads pre-push" in text and "exec true" in text, "the existing body is kept"
    assert text.index(SENTINEL_BEGIN) < text.index("exec true"), "the stanza runs before an exec or exit in the body"
    tip = _commit(rig.work, "a")
    assert rig.push().returncode == 0
    assert ran.read_text().strip() == "origin", "the existing body still ran"
    assert tip in stdin_copy.read_text(), "and still read the ref lines git sent on stdin (the stanza re-feeds them)"
    (record,) = rig.wait_for_records()
    assert _opt(record, "--to-sha") == [tip]


def test_a_held_push_stops_before_the_existing_body(rig: Rig, tmp_path: Path) -> None:
    body, _stdin_copy, ran = _existing_body(tmp_path)
    (rig.hooks_dir / "pre-push").write_text(body)
    (rig.hooks_dir / "pre-push").chmod(0o755)
    rig.install()
    _commit(rig.work, "a")
    assert rig.push(FAKE_NX_GATE_EXIT="13").returncode != 0
    assert not ran.exists()


def test_uninstall_leaves_the_existing_body_and_install_is_idempotent(rig: Rig, tmp_path: Path) -> None:
    body, _s, _r = _existing_body(tmp_path)
    (rig.hooks_dir / "pre-push").write_text(body)
    rig.install()
    once = (rig.hooks_dir / "pre-push").read_text()
    assert hooks_mod._install_hook(rig.hooks_dir, "pre-push") == "already installed"
    assert (rig.hooks_dir / "pre-push").read_text() == once
    hooks_mod._uninstall_hook(rig.hooks_dir, "pre-push")
    assert (rig.hooks_dir / "pre-push").read_bytes() == body.encode(), "uninstall restores the bytes that were there"


def test_a_non_shell_hook_is_left_alone(rig: Rig) -> None:
    py = "#!/usr/bin/env python3\nprint('x')\n"
    (rig.hooks_dir / "pre-push").write_text(py)
    assert hooks_mod._install_hook(rig.hooks_dir, "pre-push").startswith("skipped")
    assert (rig.hooks_dir / "pre-push").read_text() == py


@pytest.mark.parametrize("hooks_path", [".beads/hooks", "abs"])
def test_core_hooks_path_is_honoured(rig: Rig, tmp_path: Path, hooks_path: str) -> None:
    where = rig.work / ".beads" / "hooks" if hooks_path != "abs" else tmp_path / "shared-hooks"
    where.mkdir(parents=True)
    _git("config", "core.hooksPath", hooks_path if hooks_path != "abs" else str(where), cwd=rig.work)
    body, _stdin_copy, ran = _existing_body(tmp_path)
    (where / "pre-push").write_text(body)
    (where / "pre-push").chmod(0o755)
    result = CliRunner().invoke(main, ["hooks", "install", "--pre-push", str(rig.work)])
    assert result.exit_code == 0, result.output
    assert SENTINEL_BEGIN in (where / "pre-push").read_text()
    assert not (rig.hooks_dir / "pre-push").exists(), "nothing went to .git/hooks"
    _commit(rig.work, "a")
    assert rig.push().returncode == 0
    assert ran.exists()
    rig.wait_for_records()
    assert len(rig.verbs("record-push")) == 1


# ── the push script's push is not recorded twice ──────────────────────────────────────────────────────────────────────


def test_the_hook_stands_down_for_the_push_scripts_push(rig: Rig) -> None:
    """scripts/git-push-develop.sh checks the hold and writes the record itself; it marks its `git push` with
    NX_PUSH_VIA_SCRIPT so the hook adds no second check and no second record."""
    rig.install()
    _commit(rig.work, "a")
    assert rig.push(NX_PUSH_VIA_SCRIPT="1").returncode == 0
    time.sleep(1.5)
    assert rig.calls() == []


def test_the_push_script_marks_its_push() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "git-push-develop.sh").read_text()
    lines = [ln for ln in script.splitlines() if ln.strip().startswith(('git push', 'NX_PUSH_VIA_SCRIPT=1 git push'))]
    assert lines and all(ln.strip().startswith("NX_PUSH_VIA_SCRIPT=1 git push") for ln in lines), lines


def test_a_real_script_push_through_an_installed_hook_records_once(rig: Rig) -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "git-push-develop.sh"
    rig.install()
    a = _commit(rig.work, "a")
    # The script's installed-nx walk wants an nx outside any .venv and outside the repo; the stand-in qualifies.
    env = rig.env(NX_PUSH_SKIP_SCOPE_AUDIT="hook test, scope not under test", NX_PUSH_BATCH_MINUTES="0",
                  NX_PUSH_TENANT_CHECK="0")
    env["PATH"] = os.pathsep.join(p for p in env["PATH"].split(os.pathsep) if "/.venv/" not in p + "/")
    proc = subprocess.run([str(script), a], cwd=rig.work, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    time.sleep(1.5)
    assert len(rig.verbs("record-push")) == 1, "the script's own record, and no second one from the hook"
    assert [c for c in rig.verbs("check-hold") if "--remote" in c["argv"]] == [], "the hook did not check a second time"


# ── which repositories get the hook: nx upgrade's refresh and the explicit verb ─────────────────────────────────────


def _repos(tmp_path: Path) -> tuple[Rig, Rig]:
    return Rig(tmp_path, "served"), Rig(tmp_path, "unserved")


def _sweep(monkeypatch: pytest.MonkeyPatch, repos: list[Rig], served: frozenset[str] | None, why: str = "") -> dict:
    monkeypatch.setattr(hooks_mod, "_iter_managed_repo_roots", lambda: [r.work for r in repos])
    monkeypatch.setattr(hooks_mod, "_served_bases", lambda: (served, why))
    return hooks_mod.refresh_all_managed_hooks(echo=False)


def test_upgrade_installs_only_where_a_group_the_identity_belongs_to_serves_the_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    served, unserved = _repos(tmp_path)
    summary = _sweep(monkeypatch, [served, unserved], frozenset({"served-develop"}))
    assert (served.hooks_dir / "pre-push").exists() and SENTINEL_BEGIN in (served.hooks_dir / "pre-push").read_text()
    assert not (unserved.hooks_dir / "pre-push").exists(), "a hook that can refuse pushes stays out of an unserved repo"
    assert summary["installed"] == 1 and summary["errors"] == 0
    # the index hooks are NOT installed by the sweep (it never installs those)
    assert not (served.hooks_dir / "post-commit").exists()


def test_upgrade_installs_nothing_when_the_serving_groups_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    served, _unserved = _repos(tmp_path)
    summary = _sweep(monkeypatch, [served], None)
    assert not (served.hooks_dir / "pre-push").exists()
    assert summary["installed"] == 0


def test_a_served_base_on_another_branch_of_the_repo_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served, _unserved = _repos(tmp_path)
    _sweep(monkeypatch, [served], frozenset({"served-main"}))
    assert (served.hooks_dir / "pre-push").exists()


def test_a_repo_with_no_origin_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served, _unserved = _repos(tmp_path)
    _git("remote", "remove", "origin", cwd=served.work)
    summary = _sweep(monkeypatch, [served], frozenset({"served-develop"}))
    assert not (served.hooks_dir / "pre-push").exists() and summary["installed"] == 0


def test_upgrade_installs_beside_an_existing_hook_in_a_served_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    served, _unserved = _repos(tmp_path)
    body, _s, _r = _existing_body(tmp_path)
    (served.hooks_dir / "pre-push").write_text(body)
    _sweep(monkeypatch, [served], frozenset({"served-develop"}))
    text = (served.hooks_dir / "pre-push").read_text()
    assert "# beads pre-push" in text and SENTINEL_BEGIN in text


def test_upgrade_rewrites_an_existing_stale_stanza_in_an_unserved_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh keeps its rule: a stanza already there is rewritten, whoever serves the base."""
    _served, unserved = _repos(tmp_path)
    unserved.install()
    hook = unserved.hooks_dir / "pre-push"
    hook.write_text(hook.read_text().replace("check-hold", "check-hold --old"))
    assert hooks_mod.hook_stanza_state(unserved.work, "pre-push") == "stale"
    summary = _sweep(monkeypatch, [unserved], frozenset())
    assert hooks_mod.hook_stanza_state(unserved.work, "pre-push") == "armed"
    assert summary["refreshed"] == 1 and summary["installed"] == 0


def test_the_explicit_install_verb_installs_the_pre_push_hook_with_the_flag_only(rig: Rig) -> None:
    runner = CliRunner()
    plain = runner.invoke(main, ["hooks", "install", str(rig.work)])
    assert plain.exit_code == 0, plain.output
    assert not (rig.hooks_dir / "pre-push").exists(), "the default install adds nothing that can refuse a push"
    flagged = runner.invoke(main, ["hooks", "install", "--pre-push", str(rig.work)])
    assert flagged.exit_code == 0, flagged.output
    assert "pre-push" in flagged.output and SENTINEL_BEGIN in (rig.hooks_dir / "pre-push").read_text()


def test_update_and_uninstall_and_status_cover_the_pre_push_hook(rig: Rig) -> None:
    runner = CliRunner()
    rig.install()
    hook = rig.hooks_dir / "pre-push"
    hook.write_text(hook.read_text().replace("check-hold", "check-hold --old"))
    status = runner.invoke(main, ["hooks", "status", str(rig.work)])
    assert "pre-push" in status.output and "stale" in status.output, status.output
    assert runner.invoke(main, ["hooks", "update", str(rig.work)]).exit_code == 0
    assert hooks_mod.hook_stanza_state(rig.work, "pre-push") == "armed"
    assert runner.invoke(main, ["hooks", "uninstall", str(rig.work)]).exit_code == 0
    assert not hook.exists()


def test_upgrade_reports_what_it_installed(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = {"repos": 0, "refreshed": 0, "errors": 0, "malformed": 0, "installed": 2}
    monkeypatch.setattr("nexus.commands.hooks.refresh_all_managed_hooks", lambda echo=False: summary)
    _refresh_all_git_hooks()
    out = capsys.readouterr().out
    assert "pre-push" in out and "2" in out, out


# ── the serving read ───────────────────────────────────────────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self, memberships: list[str], records: dict) -> None:
        self._m, self._r = memberships, records
        self.closed = False

    def memberships(self, identity: str) -> list[dict]:
        return [{"identity": identity, "group": g} for g in self._m]

    def record(self, name: str):
        return self._r.get(name)

    def close(self) -> None:
        self.closed = True


def _rec(name: str, serves: tuple[str, ...], *, live: bool | None = True, ended_at: str | None = None):
    return GroupRecord(name=name, leader="i1", purpose=None, requested_by=None, formed_at=None, ends=None, ended_at=ended_at,
                       ack_deadline=0, ack_deadline_by_topic={}, serves=serves, live=live)


def test_served_bases_unions_the_live_groups_of_the_identity() -> None:
    client = _FakeClient(["a", "b", "gone", "nobody"],
                         {"a": _rec("a", ("x-develop",)), "b": _rec("b", ("y-main", "y-develop")),
                          "gone": _rec("gone", ("z-develop",), live=False)})
    assert serving.served_bases(identity="i1", client=client) == frozenset({"x-develop", "y-main", "y-develop"})
    assert client.closed is False, "a client the caller passed is the caller's to close"


def test_served_bases_is_none_when_nothing_can_be_read() -> None:
    class Broken(_FakeClient):
        def memberships(self, identity):
            raise RuntimeError("engine down")

    assert serving.served_bases(identity="i1", client=Broken([], {})) is None
    assert serving.served_bases(identity="", client=_FakeClient([], {})) is None, "no identity: no groups to belong to"


# ── doctor sees the hook type ─────────────────────────────────────────────────────────────────────────────────────────


class _NoCatalogOwners:
    """A catalog reader with no repo owners: doctor then walks the repos.json registry alone, and no test here needs a tenant."""

    def list_owners_by_type(self, owner_type: str) -> list:
        return []


def _registry_only(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(rig.config))
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _NoCatalogOwners())
    (rig.config / "repos.json").write_text(json.dumps({"repos": {str(rig.work): {}}}))


def test_doctor_reports_drift_in_a_stale_pre_push_stanza(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry_only(rig, monkeypatch)
    rig.install()
    hook = rig.hooks_dir / "pre-push"
    hook.write_text(hook.read_text().replace("check-hold", "check-hold --old"))
    results = _check_git_hooks()
    drift = [r for r in results if "stanza drift" in r.label.lower()]
    assert drift and "pre-push" in drift[0].detail, [(r.label, r.detail) for r in results]
    assert any("nx hooks update" in s for s in drift[0].fix_suggestions)


def test_doctor_lists_an_armed_pre_push_hook_as_installed(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry_only(rig, monkeypatch)
    rig.install()
    results = _check_git_hooks()
    ok = [r for r in results if r.label == "git hooks" and str(rig.work) in r.detail]
    assert ok and "pre-push" in ok[0].detail, [(r.label, r.detail) for r in results]


def test_the_hook_name_lists_agree() -> None:
    """One definition of the hook types: the sweep, status, doctor and the indexer's tip all read _git_hooks_meta."""
    assert meta.INDEX_HOOK_NAMES == ("post-commit", "post-merge", "post-rewrite")
    assert meta.MANAGED_HOOK_NAMES == (*meta.INDEX_HOOK_NAMES, meta.PRE_PUSH_HOOK)
    assert stanza.PRE_PUSH_HOOK == meta.PRE_PUSH_HOOK == "pre-push"
