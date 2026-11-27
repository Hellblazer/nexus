# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx settings show`` and ``nx settings check`` (nexus-sby49.13, RDR-259 Decision 5).

The commands read through ``nexus.hooks._audience``, the byte copy the hooks use, so what a user inspects is what the hooks
read. The same report is the plugin copy's ``python3 <plugin root>/hooks/scripts/_audience.py show``: the two outputs are
identical except for the version lines (the Minimum Viable Validation on a machine with ``nx``).

One exception is stated, not hidden: the telemetry missing-endpoint line is computed by ``nexus.commands.settings_cmd.
telemetry_endpoint_missing``, which must agree with the plugin's ``_endpoint_resolve.pinned_endpoint`` (nexus-sby49.10). The
stdlib ``show`` on this base prints no such line, so the parity test compares directories where ``telemetry`` is off or an
engine is configured, and the telemetry tests below check the line on its own.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import settings_cmd
from nexus.daemon.service_registry import ServiceRegistry, mint_owner_token, service_identity
from tests._audience_fixtures import _load_copy, build_posture

_REAL_PLUGIN_VERSION = settings_cmd.plugin_version_from_record
ROOT = Path(__file__).resolve().parents[1]
PLUGIN_COPY = ROOT / "conexus" / "hooks" / "scripts" / "_audience.py"
MISSING_ENDPOINT = "telemetry is on, but no engine is configured under the account's config directory"


@pytest.fixture
def pin(monkeypatch):
    """Pin the account home for the src copy (what the command imports) and give back the home."""
    from nexus.hooks import _audience  # noqa: PLC0415

    def _pin(posture):
        monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(posture.home))
        return posture

    return _pin


@pytest.fixture(autouse=True)
def _versions(monkeypatch, tmp_path):
    """The command's two version reads, fixed (a test that needs another passes its own)."""
    monkeypatch.setattr(settings_cmd, "cli_version", lambda: "8.5.9")
    monkeypatch.setattr(settings_cmd, "plugin_version_from_record", lambda: "8.5.9")


def _run(*args: str):
    return CliRunner().invoke(main, ["settings", *args], catch_exceptions=False)


def _plugin_show(posture, *argv: str) -> str:
    """The plugin copy's ``show`` on *posture*, in this process, with versions that differ from nx's lines only."""
    plugin = _load_copy(PLUGIN_COPY, "_audience_plugin_for_settings_parity")
    plugin._ACCOUNT_HOME_OVERRIDE = str(posture.home)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = plugin.main(["show", "--cwd", str(posture.repo), *argv], versions={"plugin": "1.0.0", "nx": "1.0.0"})
    assert rc == 0
    return out.getvalue()


def _without_versions(text: str) -> str:
    """The text with its version block (the ``versions:`` line up to the ``files:`` line) removed."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("versions:"))
    end = next(i for i, line in enumerate(lines) if line == "files:")
    return "\n".join(lines[:start] + lines[end:]) + "\n"


# --- show ---------------------------------------------------------------------------------------------------------------


def test_show_prints_paths_values_and_sources_for_a_plain_repository(pin, plain_posture):
    pin(plain_posture)
    result = _run("show", "--cwd", str(plain_posture.repo))
    assert result.exit_code == 0, result.output
    # The report names each file by its path; on Windows the repository root comes back in its own spelling (separators and the
    # case of the drive and of the temporary directory), so the test holds the tail that names the file and the directory the
    # posture built, not the whole string.
    shown = result.output.replace("\\", "/").lower()
    assert f"{plain_posture.repo.name}/.nexus/project.json".lower() in shown
    assert f"{plain_posture.home.name}/.config/nexus/conexus.json".lower() in shown
    assert "subagent_git = seatbelt  (source: default)" in result.output or "subagent_git" in result.output
    assert "no problems" in result.output


def test_show_prints_the_versions_and_warns_when_they_differ(pin, plain_posture, monkeypatch):
    pin(plain_posture)
    monkeypatch.setattr(settings_cmd, "plugin_version_from_record", lambda: "8.5.8")
    result = _run("show", "--cwd", str(plain_posture.repo))
    assert "versions: conexus plugin 8.5.8, nx 8.5.9" in result.output
    assert "warning: the plugin (8.5.8) and nx (8.5.9) differ" in result.output


def test_show_says_so_and_names_the_stdlib_command_when_the_plugin_record_is_absent(pin, plain_posture, monkeypatch):
    pin(plain_posture)
    monkeypatch.setattr(settings_cmd, "plugin_version_from_record", lambda: None)
    text = _run("show", "--cwd", str(plain_posture.repo)).output
    assert "conexus plugin unknown" in text
    assert "not in Claude Code's record of installed plugins" in text
    assert "python3 <plugin root>/hooks/scripts/_audience.py show" in text
    assert text.index("versions:") < text.index("installed plugins") < text.index("\nfiles:")


def test_show_prints_the_project_texts_under_their_labels(pin, project_strict_posture):
    pin(project_strict_posture)
    text = _run("show", "--cwd", str(project_strict_posture.repo)).output
    assert "This project's `.nexus/project.json` asks: run `scripts/agent-worktree-preflight.sh` first." in text
    assert "This project's `.nexus/project.json` adds these questions:" in text


def test_show_reports_the_retired_maintainer_profile_when_the_file_exists(pin, plain_posture):
    pin(plain_posture)
    retired = plain_posture.home / ".config" / "nexus" / "maintainer-profile"
    retired.parent.mkdir(parents=True)
    retired.write_text("x")
    text = _run("show", "--cwd", str(plain_posture.repo)).output
    assert "retired files that still exist:" in text and str(retired) in text and "no effect" in text


def test_show_json_has_the_same_content_as_the_text(pin, project_strict_posture):
    pin(project_strict_posture)
    cwd = str(project_strict_posture.repo)
    report = json.loads(_run("show", "--cwd", cwd, "--json").stdout)
    assert {"cwd", "session_root", "files", "head_differs", "settings", "texts", "problems", "retired", "versions"} <= set(report)
    assert report["versions"]["plugin"] == "8.5.9" and report["versions"]["nx"] == "8.5.9"
    by_key = {row["key"]: row for row in report["settings"]}
    assert by_key["subagent_git"]["value"] == "strict" and by_key["subagent_git"]["source"] == "project"
    text = _run("show", "--cwd", cwd).output
    for row in report["settings"]:
        assert row["key"] + " = " in text
    for t in report["texts"]:
        assert t["text"] in text


def test_show_defaults_to_the_current_directory(pin, project_strict_posture, monkeypatch):
    pin(project_strict_posture)
    monkeypatch.chdir(project_strict_posture.repo)
    assert str(project_strict_posture.repo) in _run("show").output


# --- the same output as the plugin copy ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["plain", "project-strict", "user-strict"])
def test_show_equals_the_plugin_copys_show_except_for_the_version_lines(pin, tmp_path, name):
    posture = pin(build_posture(tmp_path, name))
    mine = _run("show", "--cwd", str(posture.repo)).output
    theirs = _plugin_show(posture)
    assert _without_versions(mine) == _without_versions(theirs)
    assert mine != theirs, "the version lines are the one difference"


@pytest.mark.parametrize("name", ["plain", "project-strict", "user-strict"])
def test_show_json_equals_the_plugin_copys_json_except_for_the_versions(pin, tmp_path, name):
    posture = pin(build_posture(tmp_path, name))
    mine = json.loads(_run("show", "--cwd", str(posture.repo), "--json").stdout)
    theirs = json.loads(_plugin_show(posture, "--json"))
    mine.pop("versions"), theirs.pop("versions")
    assert mine == theirs


def test_show_equals_the_plugin_copy_with_a_broken_file_and_a_retired_file(pin, project_strict_posture):
    pin(project_strict_posture)
    (project_strict_posture.repo / ".nexus" / "project.json").write_text('{"version": 1, "subagent_gt": "strict"}')
    (project_strict_posture.home / ".config" / "nexus").mkdir(parents=True)
    (project_strict_posture.home / ".config" / "nexus" / "maintainer-profile").write_text("x")
    mine = _without_versions(_run("show", "--cwd", str(project_strict_posture.repo)).output)
    assert "subagent_gt" in mine and "retired files that still exist:" in mine
    assert mine == _without_versions(_plugin_show(project_strict_posture))


# --- check --------------------------------------------------------------------------------------------------------------


def test_check_exits_zero_and_says_so_on_a_clean_directory(pin, project_strict_posture):
    pin(project_strict_posture)
    result = _run("check", "--cwd", str(project_strict_posture.repo))
    assert result.exit_code == 0 and "no problems" in result.output


def test_check_exits_nonzero_on_a_misspelled_key_and_names_the_file(pin, project_strict_posture):
    pin(project_strict_posture)
    (project_strict_posture.repo / ".nexus" / "project.json").write_text('{"version": 1, "subagent_gt": "strict"}')
    result = _run("check", "--cwd", str(project_strict_posture.repo))
    assert result.exit_code == 1
    assert ".nexus/project.json" in result.output and "subagent_gt" in result.output


def test_check_exits_nonzero_on_an_unreadable_user_file(pin, plain_posture):
    pin(plain_posture)
    user = plain_posture.home / ".config" / "nexus" / "conexus.json"
    user.parent.mkdir(parents=True)
    user.write_text("{not json")
    result = _run("check", "--cwd", str(plain_posture.repo))
    assert result.exit_code == 1 and "conexus.json" in result.output


def test_check_prints_only_problems_not_the_settings_table(pin, project_strict_posture):
    pin(project_strict_posture)
    (project_strict_posture.repo / ".nexus" / "project.json").write_text('{"version": 1, "subagent_gt": "strict"}')
    out = _run("check", "--cwd", str(project_strict_posture.repo)).output
    assert "settings:" not in out and "versions:" not in out


def test_check_does_not_count_no_account_home(monkeypatch, tmp_path):
    from nexus.hooks import _audience  # noqa: PLC0415

    posture = build_posture(tmp_path, "plain")
    monkeypatch.setattr(_audience, "account_home", lambda: None)
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", None)
    shown = _run("show", "--cwd", str(posture.repo))
    assert shown.exit_code == 0
    result = _run("check", "--cwd", str(posture.repo))
    assert result.exit_code == 0, result.output


def test_check_does_not_count_a_remote_ref_that_is_missing(pin, tmp_path):
    """A shallow clone's missing origin ref is a machine problem: ``show`` reports it, ``check`` does not exit on it."""
    posture = pin(build_posture(tmp_path, "plain"))
    (posture.repo / ".nexus").mkdir()
    (posture.repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1, "integration_branch": "no-such-branch"}))
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(posture.repo)], check=True)
    shown = _run("show", "--cwd", str(posture.repo)).output
    assert "origin has no branch" in shown
    result = _run("check", "--cwd", str(posture.repo))
    assert result.exit_code == 0, result.output
    assert "origin has no branch" in result.output, "the machine problem is printed, not counted"


# --- the telemetry missing-endpoint line --------------------------------------------------------------------------------------


def _telemetry_account(posture, **settings) -> Path:
    config = posture.home / ".config" / "nexus"
    config.mkdir(parents=True, exist_ok=True)
    (config / "conexus.json").write_text(json.dumps({"version": 1, "telemetry": True, **settings}))
    return config


def test_telemetry_on_with_no_engine_under_the_config_directory_is_reported(pin, plain_posture):
    pin(plain_posture)
    _telemetry_account(plain_posture)
    text = _run("show", "--cwd", str(plain_posture.repo)).output
    assert MISSING_ENDPOINT in text and "an engine named only in the environment is not used for telemetry" in text
    check = _run("check", "--cwd", str(plain_posture.repo))
    assert MISSING_ENDPOINT in check.output
    assert check.exit_code == 0, "a machine problem, not a fault in a settings file"


def test_an_engine_named_only_in_the_environment_does_not_count(pin, plain_posture, monkeypatch):
    pin(plain_posture)
    _telemetry_account(plain_posture)
    for var, value in (("NX_SERVICE_URL", "https://decoy.example"), ("NX_SERVICE_TOKEN", "decoy"), ("NX_SERVICE_PORT", "1"),
                       ("NEXUS_CONFIG_DIR", str(plain_posture.home / "decoy"))):
        monkeypatch.setenv(var, value)
    assert MISSING_ENDPOINT in _run("show", "--cwd", str(plain_posture.repo)).output


def test_a_service_url_and_token_in_the_account_config_yml_is_an_engine(pin, plain_posture):
    pin(plain_posture)
    config = _telemetry_account(plain_posture)
    (config / "config.yml").write_text("credentials:\n  service_url: https://engine.example\n  service_token: tok\n")
    assert MISSING_ENDPOINT not in _run("show", "--cwd", str(plain_posture.repo)).output


def test_a_service_url_with_credentials_in_it_is_not_an_engine(pin, plain_posture):
    pin(plain_posture)
    config = _telemetry_account(plain_posture)
    (config / "config.yml").write_text("credentials:\n  service_url: https://user:pw@engine.example\n  service_token: tok\n")
    assert MISSING_ENDPOINT in _run("show", "--cwd", str(plain_posture.repo)).output


def test_a_service_url_with_no_token_anywhere_is_not_an_engine(pin, plain_posture):
    pin(plain_posture)
    config = _telemetry_account(plain_posture)
    (config / "config.yml").write_text("credentials:\n  service_url: https://engine.example\n")
    assert MISSING_ENDPOINT in _run("show", "--cwd", str(plain_posture.repo)).output


def test_a_live_local_lease_under_the_account_config_directory_is_an_engine(pin, plain_posture):
    pin(plain_posture)
    config = _telemetry_account(plain_posture)
    ServiceRegistry(dir=config, tier="storage_service").publish(
        service_identity(), endpoint={"host": "127.0.0.1", "port": 18101, "token": "t"}, version="8.5.9", owner_token=mint_owner_token(),
    )
    assert MISSING_ENDPOINT not in _run("show", "--cwd", str(plain_posture.repo)).output


def test_telemetry_off_reports_nothing_about_an_endpoint(pin, plain_posture):
    pin(plain_posture)
    assert "no engine is configured" not in _run("show", "--cwd", str(plain_posture.repo)).output


def test_show_equals_the_plugin_copys_show_when_telemetry_has_no_engine(pin, plain_posture):
    """nexus-sby49.14: the line comes from the reader, so the stdlib show prints it as nx does (RDR-259 Decision 5)."""
    pin(plain_posture)
    _telemetry_account(plain_posture)
    mine = _run("show", "--cwd", str(plain_posture.repo)).output
    theirs = _plugin_show(plain_posture)
    assert MISSING_ENDPOINT in theirs, "the plugin copy's own show reports it"
    assert _without_versions(mine) == _without_versions(theirs)


def test_show_json_equals_the_plugin_copys_json_when_telemetry_has_no_engine(pin, plain_posture):
    pin(plain_posture)
    _telemetry_account(plain_posture)
    mine = json.loads(_run("show", "--cwd", str(plain_posture.repo), "--json").stdout)
    theirs = json.loads(_plugin_show(plain_posture, "--json"))
    mine.pop("versions"), theirs.pop("versions")
    assert mine == theirs
    assert sum(MISSING_ENDPOINT in p["what"] for p in mine["problems"]) == 1, "once, not once per layer"


def test_the_missing_endpoint_line_is_in_the_json_problems(pin, plain_posture):
    pin(plain_posture)
    _telemetry_account(plain_posture)
    problems = json.loads(_run("show", "--cwd", str(plain_posture.repo), "--json").stdout)["problems"]
    hits = [p for p in problems if p["key"] == "telemetry"]
    assert len(hits) == 1 and hits[0]["category"] == "environment" and MISSING_ENDPOINT in hits[0]["what"]


def test_telemetry_endpoint_missing_reads_only_the_directory_it_is_given(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_SERVICE_URL", "https://decoy.example")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "decoy")
    assert settings_cmd.telemetry_endpoint_missing(tmp_path) is True
    (tmp_path / "config.yml").write_text("credentials:\n  service_url: https://engine.example\n  service_token: tok\n")
    assert settings_cmd.telemetry_endpoint_missing(tmp_path) is False


def test_telemetry_endpoint_missing_follows_the_origin_binding_of_the_plugins_pinned_endpoint(tmp_path):
    """nexus-ec37o: no bearer over plain http off this machine, and a lease token only to its own origin."""
    cfg = tmp_path / "config.yml"
    cfg.write_text("credentials:\n  service_url: http://engine.example\n  service_token: tok\n")
    assert settings_cmd.telemetry_endpoint_missing(tmp_path) is True
    cfg.write_text("credentials:\n  service_url: http://127.0.0.1:9999\n  service_token: tok\n")
    assert settings_cmd.telemetry_endpoint_missing(tmp_path) is False
    cfg.write_text("credentials:\n  service_url: https://engine.example\n")
    assert settings_cmd.telemetry_endpoint_missing(tmp_path) is True, "a saved URL with no token of its own and no lease"


# --- the plugin's record of its version -------------------------------------------------------------------------------------


def test_the_plugin_version_comes_from_claude_codes_installed_plugins_record(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "plugins").mkdir()
    record = {"version": 2, "plugins": {
        "sn@nexus-plugins": [{"scope": "user", "version": "1.2.3"}],
        "conexus@nexus-plugins": [{"scope": "user", "version": "8.5.9"}],
    }}
    (tmp_path / "plugins" / "installed_plugins.json").write_text(json.dumps(record))
    assert _REAL_PLUGIN_VERSION() == "8.5.9"


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"plugins": {}}', '{"plugins": {"conexus@x": []}}', '{"plugins": {"conexus@x": [{}]}}'])
def test_the_plugin_version_is_none_when_the_record_says_nothing(tmp_path, monkeypatch, content):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "plugins").mkdir()
    if content is not None:
        (tmp_path / "plugins" / "installed_plugins.json").write_text(content)
    assert _REAL_PLUGIN_VERSION() is None


# --- registration, help and documentation ---------------------------------------------------------------------------------


def test_nx_config_help_names_nx_settings():
    result = CliRunner().invoke(main, ["config", "--help"])
    assert result.exit_code == 0 and "nx settings" in re.sub(r"\s+", " ", result.output)


def test_settings_help_says_the_user_file_is_read_from_the_account_home():
    text = re.sub(r"\s+", " ", CliRunner().invoke(main, ["settings", "--help"]).output)
    assert "account home" in text and "NEXUS_CONFIG_DIR" in text
    assert "show" in text and "check" in text


def test_the_command_is_documented_in_the_cli_reference():
    reference = (ROOT / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert "## nx settings" in reference and "nx settings show" in reference and "nx settings check" in reference
