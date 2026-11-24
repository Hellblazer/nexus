# SPDX-License-Identifier: AGPL-3.0-or-later
"""Round 2 of the managed pre-push hook (nexus-igea9): what review r1 and critique r1 of 10daf43801 found.

Each test names the finding it pins and can fail: the ones that held a claim the first round never proved (the job records
only once the remote shows the tip, the gate watchdog, log rotation, the repo-venv guard) are written so removing the
mechanism turns them red. Real ``git push`` runs, a real bare remote, an ``nx`` stand-in on PATH (tests/test_pre_push_hook.py).
"""
from __future__ import annotations

import inspect
import os
import socket
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus import _git_hooks_meta as meta
from nexus._git_hooks_meta import SENTINEL_BEGIN
from nexus.cli import main
from nexus.commands import hooks as hooks_mod
from nexus.commands import index as index_cmd
from nexus.commands.upgrade import _refresh_all_git_hooks
from nexus.health import _check_git_hooks
from nexus.pre_push_hook import serving
from tests.test_pre_push_hook import Rig, _commit, _existing_body, _git, _opt, _registry_only, _sweep


@pytest.fixture()
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


class HangingRemote:
    """A TCP server that accepts and never answers, and notes when a client hangs up."""

    def __init__(self) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.accepted = threading.Event()
        self.closed = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.accepted.set()
            threading.Thread(target=self._hold, args=(conn,), daemon=True).start()

    def _hold(self, conn: socket.socket) -> None:
        try:
            while conn.recv(4096):
                pass
        except OSError:
            pass
        self.closed.set()
        conn.close()

    def stop(self) -> None:
        self.sock.close()


# ── review r1 ship-blocker 1: the detached job stays within its bound ─────────────────────────────────────────────────


def test_the_job_is_gone_within_its_bound_when_the_remote_never_answers(rig: Rig) -> None:
    remote = HangingRemote()
    try:
        _git("config", "remote.origin.pushurl", str(rig.origin), cwd=rig.work)
        _git("config", "remote.origin.url", f"git://127.0.0.1:{remote.port}/x.git", cwd=rig.work)
        rig.install()
        _commit(rig.work, "a")
        proc = rig.push(NX_PRE_PUSH_WAIT_S="2")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert remote.accepted.wait(10), "the job asked the remote"
        assert remote.closed.wait(2 + 4), "the ls-remote the job started outlived the job's bound (no per-call watchdog)"
        log = rig.config / "pre-push.log"
        end = time.monotonic() + 4
        while time.monotonic() < end and "gave up" not in (log.read_text() if log.exists() else ""):
            time.sleep(0.1)
        assert "gave up" in log.read_text()
        assert rig.verbs("record-push") == []
    finally:
        remote.stop()


def test_the_record_waits_for_the_remote_to_show_the_tip(rig: Rig, tmp_path: Path) -> None:
    """The tip the remote shows is read from a mirror that a post-receive hook updates a second and a half after the push
    returns: a job that records when the push exits, without looking, writes the record while the mirror still shows the old tip."""
    mirror = tmp_path / "mirror" / "acme" / "m.git"
    mirror.mkdir(parents=True)
    _git("init", "-q", "--bare", "--initial-branch=develop", cwd=mirror)
    _git("push", "-q", str(mirror), "develop", cwd=rig.work)
    hook = rig.origin / "hooks" / "post-receive"
    hook.write_text(f"#!/bin/sh\n( sleep 1.5; git --git-dir='{rig.origin}' push -q '{mirror}' develop ) < /dev/null > /dev/null 2>&1 &\n")
    hook.chmod(0o755)
    _git("config", "remote.origin.pushurl", str(rig.origin), cwd=rig.work)
    _git("config", "remote.origin.url", str(mirror), cwd=rig.work)
    rig.install()
    tip = _commit(rig.work, "a")
    proc = rig.push(FAKE_NX_ORIGIN=str(mirror))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (record,) = rig.wait_for_records(seconds=30)
    assert record["origin_tip"] == tip, "recorded before the remote showed the tip"


# ── review r1 ship-blocker 2 and 3: the sweep ────────────────────────────────────────────────────────────────────────


def test_the_sweep_installs_into_a_missing_hooks_dir_and_survives_one_it_cannot_make(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken, served, later = Rig(tmp_path, "broken"), Rig(tmp_path, "served"), Rig(tmp_path, "later")
    _git("config", "core.hooksPath", ".nohooks", cwd=served.work)
    (broken.work / "afile").write_text("x")
    _git("config", "core.hooksPath", "afile/hooks", cwd=broken.work)
    later.install()
    stale = later.hooks_dir / "pre-push"
    stale.write_text(stale.read_text().replace("check-hold", "check-hold --old"))
    summary = _sweep(monkeypatch, [broken, served, later], frozenset({"served-develop", "broken-develop"}))
    assert SENTINEL_BEGIN in (served.work / ".nohooks" / "pre-push").read_text(), "a missing hooks dir is made"
    assert hooks_mod.hook_stanza_state(later.work, "pre-push") == "armed", "a repo after the failing one is still refreshed"
    assert summary["errors"] == 1 and summary["installed"] == 1 and summary["refreshed"] == 1, summary


def test_a_shared_hooks_dir_outside_the_repo_gets_no_stanza_from_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    a, b = Rig(tmp_path, "a"), Rig(tmp_path, "b")
    for r in (a, b):
        _git("config", "core.hooksPath", str(shared), cwd=r.work)
    summary = _sweep(monkeypatch, [a, b], frozenset({"a-develop"}))
    assert not (shared / "pre-push").exists(), "one served repo must not arm every repo that shares the directory"
    assert summary["installed"] == 0
    assert any("shared" in n and str(shared) in n for n in summary["notes"]), summary


def test_the_explicit_verb_may_install_into_a_shared_hooks_dir(tmp_path: Path) -> None:
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    a = Rig(tmp_path, "a")
    _git("config", "core.hooksPath", str(shared), cwd=a.work)
    result = CliRunner().invoke(main, ["hooks", "install", "--pre-push", str(a.work)])
    assert result.exit_code == 0 and (shared / "pre-push").exists(), result.output


# ── the shell the hook runs in ────────────────────────────────────────────────────────────────────────────────────────


def test_a_normal_push_prints_nothing_from_the_hook_under_bin_sh(rig: Rig) -> None:
    """On this Mac /bin/sh is bash 3.2, which printed 'Terminated: 15 ( sleep ... )' on every push and a redirect error for
    the log on the first one (both reviewers reproduced it)."""
    assert (rig.hooks_dir.parent / "hooks").is_dir()
    rig.install()
    assert (rig.hooks_dir / "pre-push").read_text().startswith("#!/bin/sh\n")
    _commit(rig.work, "a")
    first = rig.push()
    assert first.returncode == 0 and first.stderr == "" and first.stdout == "", first.stderr
    rig.wait_for_records()
    _commit(rig.work, "b")
    second = rig.push()
    assert second.returncode == 0 and second.stderr == "" and second.stdout == "", second.stderr


def test_a_dash_e_hook_survives_an_unwritable_config_dir(rig: Rig, tmp_path: Path) -> None:
    hook = rig.hooks_dir / "pre-push"
    hook.write_text("#!/bin/sh -e\ntrue\n")
    hook.chmod(0o755)
    rig.install()
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    tip = _commit(rig.work, "a")
    proc = rig.push(NEXUS_CONFIG_DIR=str(blocker / "sub"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stderr == ""
    assert _git("rev-parse", "refs/heads/develop", cwd=rig.origin).stdout.strip() == tip
    assert len(rig.wait_for_records()) == 1


def test_a_refusal_names_the_red_fix_channel_a_push_has(rig: Rig) -> None:
    rig.install()
    _commit(rig.work, "a")
    proc = rig.push(FAKE_NX_GATE_EXIT="13", FAKE_NX_GATE_OUT="PUSH_REFUSED_DEVELOP_RED x")
    assert proc.returncode != 0
    assert "NX_PUSH_RED_FIX='<reason>' git push" in proc.stdout + proc.stderr


@pytest.mark.parametrize("var", ["NX_PUSH_GATE_TIMEOUT_S", "NX_PRE_PUSH_GATE_S"])
def test_a_hanging_nx_is_stopped_by_the_gate_watchdog(rig: Rig, var: str) -> None:
    """The push script's name for the bound is NX_PUSH_GATE_TIMEOUT_S; the hook reads it, and its first name as an alias."""
    rig.install()
    tip = _commit(rig.work, "a")
    t0 = time.monotonic()
    proc = rig.push(FAKE_NX_GATE_SLEEP="30", **{var: "1"})
    assert time.monotonic() - t0 < 12, "the push waited for the hung nx"
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PUSH_GATE_UNCHECKED" in proc.stderr
    assert _git("rev-parse", "refs/heads/develop", cwd=rig.origin).stdout.strip() == tip


def test_the_log_is_rotated_past_a_megabyte(rig: Rig) -> None:
    rig.install()
    log = rig.config / "pre-push.log"
    log.write_text("x" * 1_100_000)
    _commit(rig.work, "a")
    assert rig.push().returncode == 0
    rig.wait_for_records()
    assert (rig.config / "pre-push.log.1").stat().st_size >= 1_100_000, "the old log was moved aside"
    assert log.stat().st_size < 100_000, "and the new one starts small"


def test_the_hook_skips_a_repo_venv_nx_like_the_push_script(rig: Rig, tmp_path: Path) -> None:
    marker = tmp_path / "venv-nx-ran"
    body = f"#!/bin/sh\necho ran >> '{marker}'\nexit 0\n"
    in_repo = rig.work / ".venv" / "bin"
    elsewhere = tmp_path / "elsewhere" / ".venv" / "bin"
    for d in (in_repo, elsewhere):
        d.mkdir(parents=True)
        (d / "nx").write_text(body)
        (d / "nx").chmod(0o755)
    rig.install()
    rest = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "nx").exists())
    _commit(rig.work, "a")
    venv_only = f"{in_repo}{os.pathsep}{elsewhere}{os.pathsep}{rest}"
    assert rig.push(PATH=venv_only).returncode == 0
    time.sleep(1.0)
    assert not marker.exists() and rig.calls() == [], "only a venv nx is on PATH: the hook does nothing"
    _commit(rig.work, "b")
    both = f"{in_repo}{os.pathsep}{elsewhere}{os.pathsep}{rig.nx_dir}{os.pathsep}{rest}"
    assert rig.push(PATH=both).returncode == 0
    assert len(rig.wait_for_records()) == 1 and not marker.exists(), "the installed nx behind them is the one used"


# ── beside an existing hook: bytes, Windows line ends ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("original", ["#!/bin/sh\n", "", "#!/bin/sh", "#!/usr/bin/env sh\r\nexit 0\r\n", "echo no shebang\n"])
def test_uninstall_restores_the_bytes_that_were_there(rig: Rig, original: str) -> None:
    path = rig.hooks_dir / "pre-push"
    path.write_bytes(original.encode())
    path.chmod(0o755)
    rig.install()
    assert SENTINEL_BEGIN in path.read_bytes().decode()
    hooks_mod._uninstall_hook(rig.hooks_dir, "pre-push")
    assert path.read_bytes() == original.encode()


def test_a_hook_nexus_created_is_removed_with_its_stanza(rig: Rig) -> None:
    rig.install()
    assert hooks_mod._uninstall_hook(rig.hooks_dir, "pre-push") == "removed"
    assert not (rig.hooks_dir / "pre-push").exists()


def test_refresh_keeps_a_pre_existing_shebang_only_hook(rig: Rig) -> None:
    path = rig.hooks_dir / "pre-push"
    path.write_bytes(b"#!/bin/sh\n")
    rig.install()
    results = hooks_mod._refresh_managed_hooks(rig.hooks_dir)
    assert ("pre-push", "refreshed:appended") in results
    hooks_mod._uninstall_hook(rig.hooks_dir, "pre-push")
    assert path.read_bytes() == b"#!/bin/sh\n"


# ── the verbs ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_install_pre_push_installs_only_the_pre_push_hook(rig: Rig) -> None:
    result = CliRunner().invoke(main, ["hooks", "install", "--pre-push", str(rig.work)])
    assert result.exit_code == 0, result.output
    assert (rig.hooks_dir / "pre-push").exists()
    assert [n for n in meta.INDEX_HOOK_NAMES if (rig.hooks_dir / n).exists()] == [], "the three index hooks are a separate install"
    assert "Indexing will run" not in result.output


def test_uninstall_pre_push_removes_only_that_stanza(rig: Rig) -> None:
    runner = CliRunner()
    assert runner.invoke(main, ["hooks", "install", str(rig.work)]).exit_code == 0
    assert runner.invoke(main, ["hooks", "install", "--pre-push", str(rig.work)]).exit_code == 0
    result = runner.invoke(main, ["hooks", "uninstall", "--pre-push", str(rig.work)])
    assert result.exit_code == 0, result.output
    assert not (rig.hooks_dir / "pre-push").exists()
    assert SENTINEL_BEGIN in (rig.hooks_dir / "post-commit").read_text()


def test_upgrade_names_the_removal_that_removes_only_the_pre_push_hook(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = {"repos": 0, "refreshed": 0, "errors": 0, "malformed": 0, "installed": 1}
    monkeypatch.setattr("nexus.commands.hooks.refresh_all_managed_hooks", lambda echo=False: summary)
    _refresh_all_git_hooks()
    out = capsys.readouterr().out
    assert "uninstall --pre-push" in out


# ── nx upgrade says why it did not install ───────────────────────────────────────────────────────────────────────────


def test_upgrade_says_when_the_groups_could_not_be_read(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = _sweep(monkeypatch, [rig], None, "the groups could not be read (RuntimeError: engine down)")
    assert summary["installed"] == 0
    assert any("engine down" in n and "pre-push" in n for n in summary["notes"]), summary


def test_upgrade_says_nothing_for_a_process_with_no_identity(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    """No identity belongs to no group: there is nothing to tell, so the many users who hold none see no line."""
    summary = _sweep(monkeypatch, [rig], None, "")
    assert summary["installed"] == 0 and "notes" not in summary, summary


def test_upgrade_is_silent_when_nothing_serves_the_repo(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = _sweep(monkeypatch, [rig], frozenset({"other-develop"}))
    assert "notes" not in summary, "a repo no group serves is the normal case, not a skip to explain"


def test_upgrade_names_each_non_shell_hook_it_left_alone(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = rig.hooks_dir / "pre-push"
    hook.write_text("#!/usr/bin/env python3\nprint('x')\n")
    summary = _sweep(monkeypatch, [rig], frozenset({"rig-develop"}))
    assert any(str(hook) in n and "not a shell" in n for n in summary["notes"]), summary
    assert summary["installed"] == 0


def test_upgrade_prints_the_notes(capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = {"repos": 0, "refreshed": 0, "errors": 0, "malformed": 0, "installed": 0, "notes": ["pre-push not installed: why"]}
    monkeypatch.setattr("nexus.commands.hooks.refresh_all_managed_hooks", lambda echo=False: summary)
    _refresh_all_git_hooks()
    assert "pre-push not installed: why" in capsys.readouterr().out


def _track_beads_hook(rig: Rig) -> Path:
    d = rig.work / ".beads" / "hooks"
    d.mkdir(parents=True)
    body, _s, _r = _existing_body(rig.tmp)
    (d / "pre-push").write_text(body)
    _git("add", "-f", ".beads/hooks/pre-push", cwd=rig.work)
    _git("commit", "-q", "-m", "beads hook", cwd=rig.work)
    _git("config", "core.hooksPath", ".beads/hooks", cwd=rig.work)
    return d / "pre-push"


def test_installing_into_a_tracked_hook_file_warns_with_its_name(rig: Rig) -> None:
    hook = _track_beads_hook(rig)
    result = CliRunner().invoke(main, ["hooks", "install", "--pre-push", str(rig.work)])
    assert result.exit_code == 0, result.output
    assert "tracked" in result.output and str(hook.relative_to(rig.work)) in result.output, result.output


def test_the_sweep_warns_when_it_installs_into_a_tracked_hook_file(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = _track_beads_hook(rig)
    summary = _sweep(monkeypatch, [rig], frozenset({"rig-develop"}))
    assert summary["installed"] == 1
    assert any("tracked" in n and str(hook.relative_to(rig.work)) in n for n in summary["notes"]), summary


# ── the serving read says why ─────────────────────────────────────────────────────────────────────────────────────────


def test_the_serving_read_names_why_it_failed() -> None:
    class Broken:
        def memberships(self, identity: str) -> list:
            raise RuntimeError("engine down")

        def close(self) -> None:
            pass

    served, why = serving.read_served(identity="", client=Broken())
    assert served is None and why == "", "no identity: it belongs to no group, so there is nothing to say"
    served, why = serving.read_served(identity="i1", client=Broken())
    assert served is None and "engine down" in why


# ── doctor and the indexer's tip: one list each, and they differ ──────────────────────────────────────────────────────


def test_doctor_keeps_the_indexing_hint_for_a_repo_with_only_pre_push(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _registry_only(rig, monkeypatch)
    rig.install()
    results = _check_git_hooks()
    rows = [r for r in results if r.label == "git hooks" and str(rig.work) in r.detail]
    assert rows and any("nx hooks install" in s for s in rows[0].fix_suggestions), [(r.label, r.detail) for r in results]
    assert "pre-push" in rows[0].detail


def test_the_index_tip_does_not_count_pre_push_as_an_index_hook(tmp_path: Path) -> None:
    d = tmp_path / "hooks"
    d.mkdir()
    rig_text = hooks_mod._stanza_for("pre-push")
    (d / "pre-push").write_text(f"#!/bin/sh\n{rig_text}\n")
    assert meta.index_hooks_managed(d) is False
    (d / "post-commit").write_text(f"#!/bin/sh\n{SENTINEL_BEGIN}\ntrue\n# <<< nexus managed end <<<\n")
    assert meta.index_hooks_managed(d) is True


def test_nx_index_asks_the_shared_helper_for_its_tip() -> None:
    assert "index_hooks_managed(" in inspect.getsource(index_cmd)


# ── the board name: the adapter's, with dash runs collapsed (RDR-220) ────────────────────────────────────────────────
