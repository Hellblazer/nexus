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
import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a") as fh: fh.write(" ".join(sys.argv[1:]) + "\n")
if sys.stdin.isatty(): sys.exit("stdin must not be a tty")
mode = os.environ.get("FAKE_CLAUDE_MODE", "updated")
verb = sys.argv[2] if len(sys.argv) > 2 else ""
if verb == "marketplace":
    # `claude plugin marketplace update <name>`: the real CLI does a git
    # fetch of the already-local clone; the test sets the clone's content
    # up directly, so this is a no-op success.
    sys.exit(0)
plugin_id = sys.argv[3] if len(sys.argv) > 3 else "?"
plugin = plugin_id.split("@")[0]
if verb == "uninstall":
    # `claude plugin uninstall <id> -s <scope> -y` (nexus-konsk ref-drift
    # path, step 1 of 2 -- a bare `install` alone is a no-op when the
    # plugin is already installed at the same declared version).
    ref_mode = os.environ.get("FAKE_CLAUDE_REF_MODE", "ref_moved")
    if ref_mode == "uninstall_fail":
        print(f'✘ Failed to uninstall plugin "{plugin_id}"'); sys.exit(1)
    sys.exit(0)
if verb == "install":
    # `claude plugin install <id> -s <scope> -y` (ref-drift path, step 2 of 2).
    ref_mode = os.environ.get("FAKE_CLAUDE_REF_MODE", "ref_moved")
    if ref_mode == "fail":
        print(f'✘ Failed to install plugin "{plugin_id}"'); sys.exit(1)
    if ref_mode == "no_confirm":
        # Exits 0 but never touches the registry -- the "not confirmed" case.
        print(f'✔ Plugin "{plugin}" installed for scope user.'); sys.exit(0)
    # ref_moved (default): mimic what a real install does -- rewrite the
    # registry's gitCommitSha for this plugin to the target the test named.
    reg_path = os.environ["NX_PLUGIN_REGISTRY"]
    new_sha = os.environ["FAKE_CLAUDE_NEW_SHA"]
    with open(reg_path) as fh:
        data = json.load(fh)
    data["plugins"][plugin_id][0]["gitCommitSha"] = new_sha
    with open(reg_path, "w") as fh:
        json.dump(data, fh)
    print(f'✔ Plugin "{plugin}" installed for scope user.')
    sys.exit(0)
print(f'Checking for updates for plugin "{plugin_id}" at user scope…')
if mode == "updated":
    print(f'✔ Plugin "{plugin}" updated from 7.34.1 to 7.35.0 for scope user. Restart to apply changes.')
elif mode == "latest":
    print(f'✔ {plugin} is already at the latest version (7.34.1).')
elif mode == "fail":
    print(f'✘ Failed to update plugin "{plugin_id}": Plugin "{plugin}" not found'); sys.exit(1)
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
    # The conftest autouse fixture parks the registry (and, nexus-konsk, the
    # known-marketplaces file) at a nonexistent path for the rest of the
    # suite; this file's tests use the sandbox HOME's.
    monkeypatch.setenv("NX_PLUGIN_REGISTRY", str(tmp_path / ".claude" / "plugins" / "installed_plugins.json"))
    monkeypatch.setenv("NX_PLUGIN_MARKETPLACES", str(tmp_path / ".claude" / "plugins" / "known_marketplaces.json"))
    return tmp_path


@pytest.fixture
def registry(home: Path):
    def entry(k: str, v: str | dict) -> dict:
        if isinstance(v, dict):
            d = {"scope": v.get("scope", "user"), "version": v["version"], "installPath": f"/x/{k}/{v['version']}"}
            if "gitCommitSha" in v:
                d["gitCommitSha"] = v["gitCommitSha"]
            return d
        return {"scope": "user", "version": v, "installPath": f"/x/{k}/{v}"}

    def write(versions: dict[str, str | dict] | None) -> Path:
        p = home / ".claude" / "plugins" / "installed_plugins.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        if versions is None:
            return p
        p.write_text(json.dumps({"version": 2, "plugins": {
            k: [entry(k, v)] for k, v in versions.items()}}))
        return p
    return write


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def marketplace(home: Path):
    """A real local git repo standing in for an already-refreshed
    marketplace clone (nexus-konsk), plus the ``known_marketplaces.json``
    entry pointing at it. Returns ``(repo, sha_before, sha_after)``:
    ``sha_before`` is what the client-tag pin (``v7.35.0``) resolves to,
    ``sha_after`` is what a same-version anchored plugin-only cut
    (``plugin-v7.35.0-1``) moves the pin to on the SAME branch -- exactly
    the RDR-197 channel shape: the version never moves, the ref does."""
    def make(marketplace_name: str = "nexus-plugins", plugin_name: str = "conexus") -> tuple[Path, str, str]:
        repo = home / "mp-src" / marketplace_name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "Test")
        cp_dir = repo / ".claude-plugin"
        cp_dir.mkdir()

        def write_marketplace(ref: str) -> None:
            (cp_dir / "marketplace.json").write_text(json.dumps({"plugins": [
                {"name": plugin_name, "version": "7.35.0",
                 "source": {"source": "git-subdir", "url": "https://example.invalid/x.git",
                            "path": plugin_name, "ref": ref}}]}))

        write_marketplace("v7.35.0")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "base")
        _git(repo, "tag", "v7.35.0")
        sha_before = _git_out(repo, "rev-parse", "HEAD")

        write_marketplace("plugin-v7.35.0-1")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "plugin-only cut")
        _git(repo, "tag", "plugin-v7.35.0-1")
        sha_after = _git_out(repo, "rev-parse", "HEAD")

        known = home / ".claude" / "plugins" / "known_marketplaces.json"
        known.parent.mkdir(parents=True, exist_ok=True)
        known.write_text(json.dumps({marketplace_name: {"installLocation": str(repo)}}))
        return repo, sha_before, sha_after
    return make


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
    # nexus-konsk: the COMMON ref-drift case (no drift found -- one shared
    # marketplace refresh plus a local rev-parse per plugin, no reinstall)
    # is cheap and fits comfortably alongside the above.
    assert pl.MARKETPLACE_REFRESH_TIMEOUT_S + 2 * pl.REF_RESOLVE_TIMEOUT_S <= 60
    # The RARE worst case -- BOTH plugins actually drifted, so both get a
    # full uninstall+install -- is accepted to exceed the 120 s budget
    # (uninstall+install is the verified two-step dance; a bare `install`
    # on an already-installed plugin is a no-op, measured against the real
    # CLI). A timed-out detached action just leaves the marker stale and
    # retries next session (its own documented failure handling) -- never
    # a hang, never lost data. Document the shape rather than pretend a
    # tighter number: this is the honest worst case.
    worst_case = pl.MARKETPLACE_REFRESH_TIMEOUT_S + 2 * (pl.UNINSTALL_TIMEOUT_S + pl.UPDATE_TIMEOUT_S)
    assert worst_case == 140


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


# ── ref-drift: a same-version plugin-only cut (nexus-konsk) ────────────────
# RDR-197's channel moves a plugin's release ref without ever moving the
# client `version` field -- the CLI's own `update` verb then reports
# "already at the latest version" and does nothing (measured, nexus-semdv).
# This is the case: registry version == wheel version, but the registry's
# gitCommitSha is behind what the marketplace's pinned ref now resolves to.

def test_same_version_ref_move_is_picked_up_and_reinstalled(
        registry, fake_claude: Path, wheel, marketplace, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha_before, sha_after = marketplace()
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before}})
    monkeypatch.setenv("FAKE_CLAUDE_NEW_SHA", sha_after)
    r = pl.converge_plugins()
    assert r.status == "ran" and r.restart_needed
    assert len(r.outcomes) == 1
    o = r.outcomes[0]
    assert o.plugin_id == "conexus@nexus-plugins" and o.status == "ref_moved" and o.now == "7.35.0"
    assert sha_before[:7] in o.detail and sha_after[:7] in o.detail
    log = fake_claude.read_text().splitlines()
    assert "plugin marketplace update nexus-plugins" in log
    assert "plugin uninstall conexus@nexus-plugins -s user -y" in log
    assert "plugin install conexus@nexus-plugins -s user -y" in log
    assert log.index("plugin uninstall conexus@nexus-plugins -s user -y") < \
           log.index("plugin install conexus@nexus-plugins -s user -y"), "uninstall must run before install"
    lines: list[str] = []; pl.render(r, lines.append)
    assert lines[0] == f"Plugin update: conexus@nexus-plugins 7.35.0: picked up a plugin-only release ({o.detail})"
    assert lines[-1].startswith("Plugin update: restart the Claude Code session")


def test_same_version_same_ref_is_silent_and_no_reinstall_attempted(
        registry, fake_claude: Path, wheel, marketplace) -> None:
    repo, sha_before, sha_after = marketplace()
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_after}})
    r = pl.converge_plugins()
    assert r.outcomes == [] and not r.restart_needed
    log = fake_claude.read_text()
    assert "plugin marketplace update nexus-plugins" in log
    assert "install" not in log  # covers "uninstall" too (substring)


def test_ref_drift_skipped_when_no_marketplace_info(registry, fake_claude: Path, wheel) -> None:
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": "deadbeef"}})
    r = pl.converge_plugins()
    assert r.outcomes == []
    assert fake_claude.read_text() == ""


def test_ref_drift_skipped_when_registry_has_no_commit_sha(registry, fake_claude: Path, wheel, marketplace) -> None:
    marketplace()
    registry({"conexus@nexus-plugins": "7.35.0"})  # no gitCommitSha field at all
    r = pl.converge_plugins()
    assert r.outcomes == []
    assert "install" not in fake_claude.read_text()


def test_ref_drift_reinstall_failure_reports_manual_command(
        registry, fake_claude: Path, wheel, marketplace, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha_before, sha_after = marketplace()
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before}})
    monkeypatch.setenv("FAKE_CLAUDE_REF_MODE", "fail")
    r = pl.converge_plugins()
    o = r.outcomes[0]
    assert o.status == "ref_check_failed" and "Failed to install" in o.detail
    lines: list[str] = []; pl.render(r, lines.append)
    assert lines == [
        "Plugin update: conexus@nexus-plugins 7.35.0: a plugin-only release exists but reinstall failed: "
        "uninstalled but reinstall failed: ✘ Failed to install plugin \"conexus@nexus-plugins\". "
        "Run: claude plugin uninstall conexus@nexus-plugins -s user -y "
        "&& claude plugin install conexus@nexus-plugins -s user -y"]


def test_ref_drift_uninstall_failure_reports_manual_command(
        registry, fake_claude: Path, wheel, marketplace, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha_before, sha_after = marketplace()
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before}})
    monkeypatch.setenv("FAKE_CLAUDE_REF_MODE", "uninstall_fail")
    r = pl.converge_plugins()
    o = r.outcomes[0]
    assert o.status == "ref_check_failed" and "Failed to uninstall" in o.detail
    log = fake_claude.read_text()
    assert "plugin install" not in log  # never attempted after a failed uninstall


def test_ref_drift_install_exit_zero_but_sha_unconfirmed_is_not_trusted(
        registry, fake_claude: Path, wheel, marketplace, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha_before, sha_after = marketplace()
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before}})
    monkeypatch.setenv("FAKE_CLAUDE_REF_MODE", "no_confirm")
    r = pl.converge_plugins()
    o = r.outcomes[0]
    assert o.status == "ref_check_failed" and "not confirmed" in o.detail
    assert not r.restart_needed


def test_ref_drift_dry_run_makes_no_calls(registry, fake_claude: Path, wheel, marketplace) -> None:
    repo, sha_before, sha_after = marketplace()
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before}})
    r = pl.converge_plugins(dry_run=True)
    assert r.outcomes == []
    assert fake_claude.read_text() == ""


def test_ref_drift_is_checked_once_per_marketplace_not_per_plugin(
        registry, fake_claude: Path, wheel, marketplace, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha_before, sha_after = marketplace(plugin_name="conexus")
    # sn shares the same marketplace clone; add it to that clone's manifest too.
    mp_json = repo / ".claude-plugin" / "marketplace.json"
    data = json.loads(mp_json.read_text())
    data["plugins"].append({"name": "sn", "version": "7.35.0",
                            "source": {"source": "git-subdir", "url": "https://example.invalid/x.git",
                                       "path": "sn", "ref": "plugin-v7.35.0-1"}})
    mp_json.write_text(json.dumps(data))
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "add sn"); _git(repo, "tag", "-f", "plugin-v7.35.0-1")
    sha_after2 = _git_out(repo, "rev-parse", "HEAD")
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before},
              "sn@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before}})
    monkeypatch.setenv("FAKE_CLAUDE_NEW_SHA", sha_after2)
    r = pl.converge_plugins()
    assert {o.plugin_id: o.status for o in r.outcomes} == {
        "conexus@nexus-plugins": "ref_moved", "sn@nexus-plugins": "ref_moved"}
    log = fake_claude.read_text().splitlines()
    assert log.count("plugin marketplace update nexus-plugins") == 1


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


def test_nx_upgrade_reports_a_same_version_ref_move(
        registry, fake_claude: Path, wheel, marketplace, quiet_upgrade, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, sha_before, sha_after = marketplace()
    registry({"conexus@nexus-plugins": {"version": "7.35.0", "gitCommitSha": sha_before}})
    monkeypatch.setenv("FAKE_CLAUDE_NEW_SHA", sha_after)
    result = CliRunner().invoke(main, ["upgrade"])
    assert result.exit_code == 0, result.output
    assert "picked up a plugin-only release" in result.output
