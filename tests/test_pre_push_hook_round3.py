# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 3 of the managed pre-push hook (nexus-igea9): the confirmation review of 0a4e669af4 and the base-naming rule.

Every mechanism here is pinned by a run that fails when the mechanism is removed: the wait for the pushing process, the
grace, ``GIT_TERMINAL_PROMPT=0`` as the job's ``git`` sees it, the under-the-repo ``nx`` skip with a path not named ``.venv``,
the no-``nx`` guard (no attempt to run it), the poll-interval guard (counted ``ls-remote`` calls), PATH splitting under zsh.
Real ``git push`` runs against a real bare remote with an ``nx`` stand-in (tests/test_pre_push_hook.py).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus import ci_base, ci_wait
from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END
from nexus.cli import main
from nexus.commands import hooks as hooks_mod
from nexus.pre_push_hook import serving, stanza
from tests.test_pre_push_hook import Rig, _commit, _git, _opt, _sweep


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _remote_spy(rig: Rig) -> Path:
    """Make the job's ``ls-remote`` go through a remote helper that logs the environment it runs in (git puts its own exec path
    ahead of PATH for a hook, so a stand-in ``git`` cannot be interposed; a ``git-remote-<name>`` helper is found on PATH). The
    helper never shows the tip. The push itself goes to the bare remote through ``pushurl``."""
    helper = rig.nx_dir / "git-remote-spy"
    helper.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if line == 'capabilities':\n"
        "        sys.stdout.write('fetch\\n\\n'); sys.stdout.flush()\n"
        "    elif line.startswith('list'):\n"
        "        with open(os.environ['FAKE_GIT_LOG'], 'a') as fh:\n"
        "            fh.write(json.dumps({'prompt': os.environ.get('GIT_TERMINAL_PROMPT')}) + '\\n')\n"
        "        sys.stdout.write('0123456789012345678901234567890123456789 refs/heads/other\\n\\n'); sys.stdout.flush()\n"
        "    elif not line:\n"
        "        break\n")
    helper.chmod(0o755)
    _git("config", "remote.origin.pushurl", str(rig.origin), cwd=rig.work)
    _git("config", "remote.origin.url", "spy::elsewhere", cwd=rig.work)
    return rig.tmp / "remote-spy.log"


def _spy_calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _refuse_after_ours(rig: Rig) -> None:
    """A second hook body, after ours, that refuses the push once our job has started: the remote never shows the tip."""
    hook = rig.hooks_dir / "pre-push"
    hook.write_text(hook.read_text() + "\nexit 1\n")


# ── item 4: the mechanisms the first two rounds left unproven ────────────────────────────────────────────────────────


def test_a_push_slower_than_the_grace_is_still_recorded(rig: Rig) -> None:
    """The job waits for the pushing process before it starts the grace: the remote's pre-receive sleeps three seconds, the
    grace is one. A job that skips the wait starts its grace at once, gives up after a second and loses the record."""
    pre_receive = rig.origin / "hooks" / "pre-receive"
    pre_receive.write_text("#!/bin/sh\nsleep 3\n")
    pre_receive.chmod(0o755)
    rig.install()
    tip = _commit(rig.work, "a")
    proc = rig.push(NX_PRE_PUSH_GRACE_S="1", NX_PRE_PUSH_WAIT_S="60")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = rig.wait_for_records(seconds=20)
    assert record["origin_tip"] == tip


def test_the_job_gives_up_after_the_grace_when_the_wait_is_long(rig: Rig) -> None:
    rig.install()
    _commit(rig.work, "a")
    _refuse_after_ours(rig)
    t0 = time.monotonic()
    assert rig.push(NX_PRE_PUSH_WAIT_S="60", NX_PRE_PUSH_GRACE_S="1").returncode != 0
    log = rig.config / "pre-push.log"
    end = time.monotonic() + 10
    while time.monotonic() < end and "gave up" not in (log.read_text() if log.exists() else ""):
        time.sleep(0.1)
    assert "gave up" in log.read_text(), "the grace did not end the job"
    assert time.monotonic() - t0 < 15, "the job waited out the whole bound"
    assert rig.verbs("record-push") == []


def test_the_jobs_git_runs_without_a_terminal_prompt(rig: Rig) -> None:
    spy_log = _remote_spy(rig)
    rig.install()
    _commit(rig.work, "a")
    assert rig.push(FAKE_GIT_LOG=str(spy_log), NX_PRE_PUSH_GRACE_S="1", NX_PRE_PUSH_WAIT_S="5").returncode == 0
    end = time.monotonic() + 15
    while time.monotonic() < end and not _spy_calls(spy_log):
        time.sleep(0.1)
    calls = _spy_calls(spy_log)
    assert calls, "the job asked the remote"
    assert {c["prompt"] for c in calls} == {"0"}, calls


@pytest.mark.parametrize("poll", ["0", "abc", "-1", "1.2.3"])
def test_a_bad_poll_interval_does_not_spin(rig: Rig, poll: str) -> None:
    """A zero, negative or non-numeric NX_PRE_PUSH_POLL_S falls back to the default instead of calling ls-remote in a loop."""
    spy_log = _remote_spy(rig)
    rig.install()
    _commit(rig.work, "a")
    _refuse_after_ours(rig)
    rig.push(NX_PRE_PUSH_POLL_S=poll, NX_PRE_PUSH_WAIT_S="3", FAKE_GIT_LOG=str(spy_log))
    time.sleep(4)
    assert len(_spy_calls(spy_log)) <= 4, f"{len(_spy_calls(spy_log))} ls-remote calls in about 3 seconds"


def test_an_nx_under_the_repo_is_skipped_whatever_its_directory_is_called(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "repo-nx-ran"
    tools = rig.work / "tools" / "bin"
    tools.mkdir(parents=True)
    (tools / "nx").write_text(f"#!/bin/sh\necho ran >> '{marker}'\nexit 0\n")
    (tools / "nx").chmod(0o755)
    rig.install()
    rest = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "nx").exists())
    _commit(rig.work, "a")
    assert rig.push(PATH=f"{tools}{os.pathsep}{rest}").returncode == 0
    time.sleep(1.0)
    assert not marker.exists() and rig.calls() == [], "an nx under the repo (no .venv in its path) must not run"
    _commit(rig.work, "b")
    assert rig.push(PATH=f"{tools}{os.pathsep}{rig.nx_dir}{os.pathsep}{rest}").returncode == 0
    assert len(rig.wait_for_records()) == 1 and not marker.exists()


def test_with_no_nx_the_hook_makes_no_attempt_to_run_one(rig: Rig) -> None:
    """Without the guard the hook would try to run an empty command and print a gate warning, then start a job: no output,
    no log file and no call prove none of that happened."""
    rig.install()
    rest = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "nx").exists())
    _commit(rig.work, "a")
    proc = rig.push(PATH=rest)
    assert proc.returncode == 0
    assert proc.stderr == "" and proc.stdout == "", proc.stderr
    time.sleep(1.0)
    assert not (rig.config / "pre-push.log").exists() and rig.calls() == []


SHELLS = [pytest.param(sh, marks=pytest.mark.skipif(not Path(sh).exists(), reason=f"{sh} is not installed"))
          for sh in ("/bin/sh", "/bin/bash", "/bin/dash", "/bin/zsh", "/bin/ksh")]


@pytest.mark.parametrize("shell", SHELLS)
def test_the_stanza_runs_under_each_shell_a_hook_may_name(rig: Rig, shell: str) -> None:
    """is_shell_hook accepts these interpreters, so the stanza has to work in them: zsh does not split an unquoted $PATH, which
    left nx unfound and the hook silently doing nothing."""
    hook = rig.hooks_dir / "pre-push"
    hook.write_text(f"#!{shell}\n")
    hook.chmod(0o755)
    rig.install()
    tip = _commit(rig.work, "a")
    proc = rig.push()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stderr == "", proc.stderr
    assert len(rig.verbs("check-hold")) == 1, "nx was called for the hold check"
    (record,) = rig.wait_for_records()
    assert _opt(record, "--to-sha") == [tip]


# ── item 2: a hook that is not text ──────────────────────────────────────────────────────────────────────────────────


def test_a_binary_pre_push_hook_is_left_alone_and_the_sweep_goes_on(rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    later = Rig(tmp_path, "later")
    later.install()
    stale = later.hooks_dir / "pre-push"
    stale.write_text(stale.read_text().replace("check-hold", "check-hold --old"))
    binary = rig.hooks_dir / "pre-push"
    binary.write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    summary = _sweep(monkeypatch, [rig, later], frozenset({"rig-develop"}))
    assert binary.read_bytes() == b"\xff\xfe\x00 not utf-8 \x80", "a binary hook is never rewritten"
    assert hooks_mod.hook_stanza_state(later.work, "pre-push") == "armed", "the repo after it is still refreshed"
    assert summary["errors"] == 0 and summary["installed"] == 0, summary
    assert any(str(binary) in n and "not a text file" in n for n in summary["notes"]), summary


def test_the_verbs_do_not_traceback_on_a_binary_hook(rig: Rig) -> None:
    binary = rig.hooks_dir / "pre-push"
    binary.write_bytes(b"\xff\xfe\x00 not utf-8 \x80")
    runner = CliRunner()
    for argv in (["status"], ["install", "--pre-push"], ["uninstall", "--pre-push"], ["update"], ["install"]):
        result = runner.invoke(main, ["hooks", *argv, str(rig.work)])
        assert result.exit_code == 0 and result.exception is None, (argv, result.output, result.exception)
    assert binary.read_bytes() == b"\xff\xfe\x00 not utf-8 \x80"


# ── item 5: a hook the first round created, with no created-note ─────────────────────────────────────────────────────


def test_a_hook_that_is_exactly_a_legacy_created_stanza_is_removed_whole(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    block = f"{SENTINEL_BEGIN}\nthe first round's stanza body\n{SENTINEL_END}"
    monkeypatch.setattr(stanza, "LEGACY_CREATED_DIGESTS", frozenset({hashlib.sha256(block.encode()).hexdigest()}))
    path = rig.hooks_dir / "pre-push"
    path.write_text(f"#!/bin/sh\n{block}\n")
    assert hooks_mod._uninstall_hook(rig.hooks_dir, "pre-push") == "removed"
    assert not path.exists()
    # the same shape with any other stanza body is a hook that was there before: its bytes come back
    path.write_text(f"#!/bin/sh\n{SENTINEL_BEGIN}\nanother body\n{SENTINEL_END}\n")
    assert hooks_mod._uninstall_hook(rig.hooks_dir, "pre-push") == "stanza removed"
    assert path.read_bytes() == b"#!/bin/sh\n"


def test_the_legacy_digest_is_pinned_and_is_not_the_current_stanza() -> None:
    assert len(stanza.LEGACY_CREATED_DIGESTS) == 1
    assert all(re.fullmatch(r"[0-9a-f]{64}", d) for d in stanza.LEGACY_CREATED_DIGESTS)
    assert hashlib.sha256(stanza.STANZA.encode()).hexdigest() not in stanza.LEGACY_CREATED_DIGESTS, (
        "a hook the current code installs into a shebang-only file must restore that file, not delete it")


# ── item 3: the base is the adapter's topic, exactly ─────────────────────────────────────────────────────────────────

#: (repo, branch, the topic the CI adapter writes). The adapter is conexus infra/terraform/ci-board-adapter/lambda/
#: github_board_adapter.py: topic_for(repo, branch) = normalise_segment(f"{repo.lower()}-{branch}"), where normalise_segment
#: maps each run of characters outside [A-Za-z0-9._-] to "-", collapses runs of "-", and strips leading "._-"; the topic is then
#: cut to MAX_TOPIC_CHARS (80) and stripped of trailing "._-". Each expected value is worked out from those rules by hand.
ADAPTER_TOPICS = [
    ("nexus", "develop", "nexus-develop"),
    ("recording-rig", "main", "recording-rig-main"),
    ("a--b", "develop", "a-b-develop"),
    ("A_B", "main", "a_b-main"),
    ("ART", "main", "art-main"),
    ("x.y", "develop", "x.y-develop"),
    (".dot", "develop", "dot-develop"),
    ("-_.lead", "develop", "lead-develop"),
    ("café", "main", "caf-main"),
    ("repo", "feat/ü", "repo-feat"),
    ("repo", "a//b", "repo-a-b"),
    ("r" * 100, "develop", "r" * 80),
    ("r" * 79, "develop", "r" * 79),
    ("r" * 78 + ".", "develop", "r" * 78),
    (".", ".", ""),
]


@pytest.mark.parametrize(("repo", "branch", "expected"), ADAPTER_TOPICS)
def test_join_is_the_topic_the_ci_adapter_writes(repo: str, branch: str, expected: str) -> None:
    assert ci_base.join(repo, branch) == expected


def test_the_cap_matches_the_adapters() -> None:
    assert ci_base.MAX_TOPIC_CHARS == 80


def test_derive_and_the_board_names_use_the_adapters_topic(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("remote", "add", "origin", "https://github.com/o/a--b.git", cwd=repo)
    assert ci_base.derive("origin", "develop", cwd=repo) == "a-b-develop"
    assert ci_wait.done_subspace("a--b", "develop") == "board/ci/a-b-develop--done"
    assert "a-b-develop" in serving.repo_bases(repo)
