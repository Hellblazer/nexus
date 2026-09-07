"""nexus-2uwag — ``nx upgrade`` converges the Claude Code plugins.

Every branch of ``nexus.plugin_lockstep``'s contract, driven through a fake
``claude`` binary on PATH (behaviour chosen by ``FAKE_CLAUDE_MODE``) and a
registry under a scrubbed HOME, plus the CLI-level wiring in ``nx upgrade``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus import plugin_lockstep as pl
from nexus.cli import main

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as fh: fh.write(" ".join(sys.argv[1:]) + "\n")
if sys.stdin.isatty(): sys.exit("stdin must not be a tty")
mode = os.environ.get("FAKE_CLAUDE_MODE", "updated")
plugin = sys.argv[3].split("@")[0] if len(sys.argv) > 3 else "?"
print(f'Checking for updates for plugin "{sys.argv[3]}" at user scope…')
if mode == "updated":
    print(f'✔ Plugin "{plugin}" updated from 7.34.1 to 7.35.0 for scope user. Restart to apply changes.')
elif mode == "latest":
    print(f'✔ {plugin} is already at the latest version (7.34.1).')
elif mode == "fail":
    print(f'✘ Failed to update plugin "{sys.argv[3]}": Plugin "{plugin}" not found'); sys.exit(1)
elif mode == "hang":
    import time; time.sleep(60)
elif mode == "sn_fails" and plugin == "sn":
    print("✘ network unreachable"); sys.exit(1)
else:
    print(f'✔ Plugin "{plugin}" updated from 7.34.1 to 7.35.0 for scope user. Restart to apply changes.')
'''


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    # The conftest autouse fixture parks the registry at a nonexistent path
    # for the rest of the suite; this file's tests use the sandbox HOME's.
    monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(tmp_path / ".claude" / "plugins" / "installed_plugins.json"))
    return tmp_path


@pytest.fixture
def registry(home: Path):
    def write(versions: dict[str, str] | None) -> Path:
        p = home / ".claude" / "plugins" / "installed_plugins.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        if versions is None:
            return p
        p.write_text(json.dumps({"version": 2, "plugins": {
            k: [{"scope": "user", "version": v, "installPath": f"/x/{k}/{v}"}] for k, v in versions.items()}}))
        return p
    return write


@pytest.fixture
def fake_claude(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bindir = home / "bin"; bindir.mkdir()
    exe = bindir / "claude"; exe.write_text(FAKE_CLAUDE); exe.chmod(0o755)
    log = home / "claude.log"; log.touch()
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


@pytest.fixture
def wheel(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pl, "wheel_version", lambda: "7.35.0")


# ── registry parsing ──────────────────────────────────────────────────────

def test_registry_absent_is_not_a_plugin_box(registry, wheel) -> None:
    registry(None)
    assert pl.installed_plugins() is None
    assert pl.converge_plugins().status == "not_a_plugin_box"


def test_registry_without_our_plugins_is_not_a_plugin_box(registry, wheel) -> None:
    registry({"beads@beads-marketplace": "1.2.3"})
    assert pl.converge_plugins().status == "not_a_plugin_box"


def test_registry_reads_both_plugins_and_keeps_marketplace_name(registry) -> None:
    registry({"conexus@nexus-plugins": "7.34.1", "sn@my-mp": "7.34.0", "beads@x": "1.0.0"})
    assert pl.installed_plugins() == {"conexus@nexus-plugins": pl.PluginInstall("7.34.1", "user"),
                                      "sn@my-mp": pl.PluginInstall("7.34.0", "user")}


def test_registry_newest_entry_wins_and_junk_is_ignored(home: Path) -> None:
    p = home / ".claude" / "plugins" / "installed_plugins.json"; p.parent.mkdir(parents=True)
    # newest is in the MIDDLE, so "last valid entry" would pick 7.33.0 and fail
    p.write_text(json.dumps({"version": 2, "plugins": {"conexus@mp": [
        {"version": "7.32.0"}, {"version": "7.34.1", "scope": "project"}, {"version": "7.33.0"},
        {"version": "garbage"}, "junk"]}}))
    assert pl.installed_plugins() == {"conexus@mp": pl.PluginInstall("7.34.1", "project")}


def test_older_flat_registry_shape_is_read(home: Path) -> None:
    p = home / ".claude" / "plugins" / "installed_plugins.json"; p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"conexus@nexus-plugins": {"version": "6.16.0"}}))
    assert pl.installed_plugins() == {"conexus@nexus-plugins": pl.PluginInstall("6.16.0", "user")}


def test_project_scope_install_is_updated_at_its_own_scope(registry, fake_claude: Path, wheel, home: Path) -> None:
    p = registry({})
    p.write_text(json.dumps({"version": 2, "plugins": {"conexus@nexus-plugins": [{"version": "7.34.1", "scope": "project"}]}}))
    r = pl.converge_plugins()
    assert r.outcomes[0].status == "updated"
    assert fake_claude.read_text().strip() == "plugin update conexus@nexus-plugins -s project -y"


def test_unreadable_registry_is_not_a_plugin_box(registry) -> None:
    p = registry({"conexus@mp": "7.34.1"}); p.write_text("{not json")
    assert pl.installed_plugins() is None


# ── decision ──────────────────────────────────────────────────────────────

def test_in_lockstep_touches_nothing(registry, fake_claude: Path, wheel) -> None:
    registry({"conexus@nexus-plugins": "7.35.0", "sn@nexus-plugins": "7.35.0"})
    r = pl.converge_plugins()
    assert r.status == "in_lockstep"
    assert fake_claude.read_text() == ""


def test_plugin_ahead_of_wheel_is_left_alone(registry, fake_claude: Path, wheel) -> None:
    registry({"conexus@nexus-plugins": "7.36.0", "sn@nexus-plugins": "7.35.0"})
    assert pl.converge_plugins().status == "in_lockstep"
    assert fake_claude.read_text() == ""


def test_claude_missing_names_the_manual_route(registry, wheel, monkeypatch: pytest.MonkeyPatch) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    monkeypatch.setenv("PATH", "/nonexistent")
    r = pl.converge_plugins()
    assert r.status == "claude_missing"
    lines: list[str] = []; pl.render(r, lines.append)
    assert len(lines) == 1 and "/plugin update" in lines[0] and "conexus@nexus-plugins" in lines[0]


def test_dry_run_runs_nothing(registry, fake_claude: Path, wheel) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    r = pl.converge_plugins(dry_run=True)
    assert [o.status for o in r.outcomes] == ["dry_run"]
    assert fake_claude.read_text() == ""
    lines: list[str] = []; pl.render(r, lines.append)
    assert "would run: claude plugin update conexus@nexus-plugins -s user -y" in lines[0]


# ── the update itself ─────────────────────────────────────────────────────

def test_behind_plugins_are_updated_with_yes_and_no_tty(registry, fake_claude: Path, wheel) -> None:
    registry({"conexus@nexus-plugins": "7.34.1", "sn@nexus-plugins": "7.34.1"})
    r = pl.converge_plugins()
    assert r.status == "ran" and r.restart_needed
    assert [(o.plugin_id, o.status, o.now) for o in r.outcomes] == [
        ("conexus@nexus-plugins", "updated", "7.35.0"), ("sn@nexus-plugins", "updated", "7.35.0")]
    assert fake_claude.read_text().splitlines() == [
        "plugin update conexus@nexus-plugins -s user -y", "plugin update sn@nexus-plugins -s user -y"]
    lines: list[str] = []; pl.render(r, lines.append)
    assert lines[0] == "Plugin update: conexus@nexus-plugins 7.34.1 -> 7.35.0"
    assert lines[-1].startswith("Plugin update: restart the Claude Code session")


def test_only_the_behind_plugin_is_touched(registry, fake_claude: Path, wheel) -> None:
    registry({"conexus@nexus-plugins": "7.35.0", "sn@nexus-plugins": "7.34.1"})
    r = pl.converge_plugins()
    assert [o.plugin_id for o in r.outcomes] == ["sn@nexus-plugins"]


def test_latest_published_behind_wheel_is_the_dev_tree_case_not_a_failure(
        registry, fake_claude: Path, wheel, monkeypatch: pytest.MonkeyPatch) -> None:
    registry({"conexus@nexus-plugins": "7.34.0"})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "latest")
    r = pl.converge_plugins()
    o = r.outcomes[0]
    assert o.status == "latest_published" and o.now == "7.34.1" and not r.restart_needed
    lines: list[str] = []; pl.render(r, lines.append)
    assert "newest published plugin" in lines[0] and "Run:" not in lines[0]


def test_failed_update_reports_reason_and_manual_command(
        registry, fake_claude: Path, wheel, monkeypatch: pytest.MonkeyPatch) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    r = pl.converge_plugins()
    o = r.outcomes[0]
    assert o.status == "failed" and "not found" in o.detail
    lines: list[str] = []; pl.render(r, lines.append)
    assert lines == [
        'Plugin update: conexus@nexus-plugins 7.34.1 still behind conexus 7.35.0: '
        '✘ Failed to update plugin "conexus@nexus-plugins": Plugin "conexus" not found. '
        'Run: claude plugin update conexus@nexus-plugins -s user -y']


def test_one_failure_does_not_stop_the_other(registry, fake_claude: Path, wheel, monkeypatch: pytest.MonkeyPatch) -> None:
    registry({"conexus@nexus-plugins": "7.34.1", "sn@nexus-plugins": "7.34.1"})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sn_fails")
    r = pl.converge_plugins()
    assert {o.plugin_id: o.status for o in r.outcomes} == {"conexus@nexus-plugins": "updated", "sn@nexus-plugins": "failed"}
    assert r.restart_needed


def test_two_plugins_fit_inside_the_rdr143_action_budget() -> None:
    # conexus/hooks/scripts/version_lockstep_action.py bounds the whole
    # `nx upgrade` at 120 s (_NX_UPGRADE_TIMEOUT); two updates must fit.
    assert 2 * pl.UPDATE_TIMEOUT_S <= 100


def test_timeout_is_a_named_failure(registry, fake_claude: Path, wheel, monkeypatch: pytest.MonkeyPatch) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    monkeypatch.setattr(pl, "UPDATE_TIMEOUT_S", 1)
    o = pl.converge_plugins().outcomes[0]
    assert o.status == "failed" and "timed out after 1s" in o.detail


def test_unrecognised_success_output_is_not_a_confirmed_update(registry, wheel) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout="something new the CLI now prints\n", stderr="")
    r = pl.converge_plugins(run=run, claude_path="/fake/claude")
    o = r.outcomes[0]
    assert o.status == "unknown" and o.now is None and not r.restart_needed
    lines: list[str] = []; pl.render(r, lines.append)
    assert "not confirmed" in lines[0] and "claude plugin list" in lines[0]


def test_updated_line_with_junk_version_is_not_trusted(registry, wheel) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout='Plugin "conexus" updated from 7.34.1 to latest for scope user.\n', stderr="")
    o = pl.converge_plugins(run=run, claude_path="/fake/claude").outcomes[0]
    assert o.status == "unknown"


# ── nx upgrade wiring ─────────────────────────────────────────────────────

@pytest.fixture
def quiet_upgrade():
    with (patch("nexus.commands.upgrade._cycle_supervised_daemons_to_current"),
          patch("nexus.commands.upgrade._converge_preconditions"),
          patch("nexus.commands.upgrade._refresh_all_git_hooks"),
          patch("nexus.commands.upgrade._migrate_repos_json_to_catalog")):
        yield


def test_nx_upgrade_runs_the_plugin_step_by_default(registry, fake_claude: Path, wheel, quiet_upgrade) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert "Plugin update: conexus@nexus-plugins 7.34.1 -> 7.35.0" in result.output
    assert fake_claude.read_text().strip() == "plugin update conexus@nexus-plugins -s user -y"


def test_nx_upgrade_dry_run_prints_and_runs_nothing(registry, fake_claude: Path, wheel, quiet_upgrade) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    result = CliRunner().invoke(main, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would run: claude plugin update conexus@nexus-plugins -s user -y" in result.output
    assert fake_claude.read_text() == ""


def test_nx_upgrade_auto_skips_the_network_bound_plugin_step(registry, fake_claude: Path, wheel, quiet_upgrade) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    result = CliRunner().invoke(main, ["upgrade", "--auto"])
    assert result.exit_code == 0, result.output
    assert fake_claude.read_text() == ""


def test_nx_upgrade_failed_plugin_update_still_exits_zero(
        registry, fake_claude: Path, wheel, quiet_upgrade, monkeypatch: pytest.MonkeyPatch) -> None:
    registry({"conexus@nexus-plugins": "7.34.1"})
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "fail")
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert "still behind conexus 7.35.0" in result.output and "Run: claude plugin update" in result.output


def test_nx_upgrade_in_lockstep_is_silent(registry, fake_claude: Path, wheel, quiet_upgrade) -> None:
    registry({"conexus@nexus-plugins": "7.35.0", "sn@nexus-plugins": "7.35.0"})
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert "Plugin update" not in result.output
