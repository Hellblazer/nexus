# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run triage lines for 8.1.3, batch three (nexus-f3yqd.6): F2, F4, F9, F17, F22, F23."""
from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest

import nexus.upgrade_ladder.registry as ladder_registry
from nexus.commands.upgrade import _run_ladder
from nexus.upgrade_ladder.registry import LadderRegistry

from nexus.commands import init as init_mod
from nexus.daemon import installer
from nexus.mcp import setup_tools
from tests.upgrade.test_ladder_surfaces import InMemoryLedger, SurfaceRung, _DeferringRung

ROOT = Path(__file__).resolve().parents[1]


# F2: a CLI older than the setup tools (pre-7.81) runs its own `nx self install`, whose uv cache prune (bounded only
# since 8.1.0) can wait silently; the skill says so instead of letting the user think it hung.
def test_the_setup_skill_warns_an_old_cli_user_about_the_silent_prune() -> None:
    text = " ".join((ROOT / "conexus" / "commands" / "setup.md").read_text(encoding="utf-8").split())
    assert "can sit silent for up to ten minutes" in text
    assert "it is not stuck and to let it finish" in text


# F4: the setup tool's own failure named no cause and no log, and offered `nx init` (the install that may have failed).

def _child(monkeypatch: pytest.MonkeyPatch, *, code: int, out: str = "", err: str = "") -> None:
    monkeypatch.setattr(setup_tools, "run_bounded", lambda *a, **k: subprocess.CompletedProcess([], code, out, err))


def test_a_failed_setup_process_names_its_cause_before_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _child(monkeypatch, code=1, err="Traceback (most recent call last):\n  File x\nPermissionError: [Errno 13] setup dir\n")
    with pytest.raises(setup_tools.SetupError) as raised:
        setup_tools._call_script("nx_setup.py", ["status"])
    text = str(raised.value)
    assert text == (
        "The setup process failed (exit 1) and printed no answer. It printed: PermissionError: [Errno 13] setup dir"
        " Setup can also be done in a terminal: the user runs `nx init`."
    ), text


def test_with_no_stderr_the_message_says_only_what_it_knows(monkeypatch: pytest.MonkeyPatch) -> None:
    _child(monkeypatch, code=0, out="not json\n")
    with pytest.raises(setup_tools.SetupError) as raised:
        setup_tools._call_script("nx_setup.py", ["status"])
    assert str(raised.value) == (
        "The setup process printed something other than JSON. Setup can also be done in a terminal: the user runs `nx init`."
    )


# F9: with the autostart unit already registered and no lease yet, `nx init` exits 0 (the unit owns bring-up) where a
# fresh install in the same state exits 1. The output now says why, and names the logs the fresh branch names.
def test_a_rerun_with_the_unit_present_says_why_it_exits_zero_and_names_the_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(init_mod, "provision_service_stack", lambda embedder=None: True)
    monkeypatch.setattr(init_mod, "_poll_service_lease", lambda config_dir, **kw: None)
    monkeypatch.setattr(
        installer, "install_autostart",
        lambda *, tier, force=False: installer.InstallResult(status=installer.InstallStatus.ALREADY_PRESENT, dest=tmp_path / "unit"),
    )
    assert init_mod._provision_and_autostart_service(None) is None
    err = " ".join(capsys.readouterr().err.split())
    assert "registered before this run, so it owns bring-up" in err and "exits 0" in err, err
    assert str(tmp_path / "logs" / "storage_service.log") in err and "storage_service_native.log" in err, err


# F17: a ladder that did not converge said which step and nothing else; a deferral never said it would be retried.
def test_a_ladder_that_did_not_converge_says_what_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    failing = SurfaceRung("labels", verify_result=False)
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((failing,)))
    with pytest.raises(click.ClickException) as exc:
        _run_ladder(dry_run=False, auto_mode=False, _ledger_fn=InMemoryLedger)
    message = exc.value.message
    assert message.startswith("upgrade ladder did not converge — labels: verify-failed"), message
    assert message.endswith("Fix what each step names; `nx upgrade --dry-run` lists the steps still pending."), message


@pytest.mark.t2_tenant  # the walk defers, so the deferral log writes its T2 note (nexus-0sm1t)
def test_a_deferred_step_says_it_runs_again(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(ladder_registry, "default_registry", lambda **kw: LadderRegistry((_DeferringRung("census"),)))
    _run_ladder(dry_run=False, auto_mode=False, _ledger_fn=InMemoryLedger)
    out = " ".join(capsys.readouterr().out.split())
    assert "rung 'census' deferred — engine census unavailable. It runs again on the next `nx upgrade`" in out, out
    assert "`nx doctor` names a step that defers on every run" in out
