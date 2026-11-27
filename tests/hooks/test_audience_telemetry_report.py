# SPDX-License-Identifier: AGPL-3.0-or-later
"""The telemetry missing-endpoint report comes from the reader, so every surface that prints the problem list prints it
(nexus-sby49.14, RDR-259 Decisions 5 and 9).

``telemetry`` on with no engine under the account's config directory is a problem of the environment, not of a settings file.
It is decided once per copy of the reader (the plugin's ``_endpoint_resolve.pinned_endpoint``, the wheel's mirror of it) and
reaches ``settings_problems`` (the notice), ``build_report`` (``show`` and ``check``, stdlib and ``nx``). Both byte copies run
every case.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.hooks.git_shim import init_repo
from tests.hooks.test_pinned_endpoint_parity import _build

NO_ENGINE = "telemetry is on, but no engine is configured under the account's config directory"


def _account(audience, **settings) -> Path:
    config = Path(audience.HOME) / ".config" / "nexus"
    config.mkdir(parents=True, exist_ok=True)
    (config / "conexus.json").write_text(json.dumps({"version": 1, **settings}), encoding="utf-8")
    return config


def _named(problems) -> list:
    return [p for p in problems if NO_ENGINE in p.what]


def test_telemetry_on_with_no_engine_is_a_problem_in_the_settings_problem_list(audience_copy, tmp_path):
    repo = init_repo(tmp_path / "repo", {"README": "x\n"})
    _account(audience_copy, telemetry=True)
    [problem] = _named(audience_copy.settings_problems({"cwd": str(repo)}))
    assert problem.category == "environment" and problem.key == "telemetry" and problem.file == audience_copy.USER_LABEL
    assert "an engine named only in the environment is not used for telemetry" in problem.what


def test_the_problem_is_in_the_report_the_stdlib_show_prints(audience_copy, tmp_path):
    repo = init_repo(tmp_path / "repo", {"README": "x\n"})
    _account(audience_copy, telemetry=True)
    report = audience_copy.build_report(str(repo))
    assert len([p for p in report["problems"] if NO_ENGINE in p["what"]]) == 1
    assert NO_ENGINE in audience_copy.render_report(report)


def test_telemetry_off_says_nothing_about_an_endpoint(audience_copy, tmp_path):
    repo = init_repo(tmp_path / "repo", {"README": "x\n"})
    _account(audience_copy, telemetry=False)
    assert _named(audience_copy.settings_problems({"cwd": str(repo)})) == []
    assert not any(NO_ENGINE in p["what"] for p in audience_copy.build_report(str(repo))["problems"])


def test_an_engine_named_in_the_account_config_directory_is_no_problem(audience_copy, tmp_path):
    repo = init_repo(tmp_path / "repo", {"README": "x\n"})
    config = _account(audience_copy, telemetry=True)
    _build(config, url="https://engine.example", token="tok", lease=False)
    assert _named(audience_copy.settings_problems({"cwd": str(repo)})) == []


def test_a_cleartext_engine_off_this_machine_names_no_engine(audience_copy, tmp_path):
    """The decision is the sender's: a url the sender would refuse to post to is no engine."""
    repo = init_repo(tmp_path / "repo", {"README": "x\n"})
    config = _account(audience_copy, telemetry=True)
    _build(config, url="http://evil.example:80", token="tok", lease=False)
    assert len(_named(audience_copy.settings_problems({"cwd": str(repo)}))) == 1


def test_the_environment_names_no_engine_for_the_report(audience_copy, tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo", {"README": "x\n"})
    _account(audience_copy, telemetry=True)
    monkeypatch.setenv("NX_SERVICE_URL", "https://engine.example")
    monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
    assert len(_named(audience_copy.settings_problems({"cwd": str(repo)}))) == 1


def test_no_account_home_adds_no_endpoint_problem_of_its_own(audience_copy, tmp_path, monkeypatch):
    repo = init_repo(tmp_path / "repo", {"README": "x\n"})
    monkeypatch.setattr(audience_copy, "account_home", lambda: None)
    assert _named(audience_copy.settings_problems({"cwd": str(repo)})) == []


@pytest.mark.parametrize("kind", ["plugin", "wheel"])
def test_the_two_decisions_are_the_one_the_sender_makes(tmp_path, monkeypatch, kind):
    """The plugin copy asks ``pinned_endpoint``; the wheel copy asks the mirror. Neither is its own rule."""
    from tests._audience_fixtures import _load_copy  # noqa: PLC0415

    root = Path(__file__).resolve().parents[2]
    path = root / "conexus" / "hooks" / "scripts" / "_audience.py" if kind == "plugin" else root / "src" / "nexus" / "hooks" / "_audience.py"
    if kind == "plugin":
        monkeypatch.syspath_prepend(str(path.parent))
    mod = _load_copy(path, f"_audience_{kind}_decision")
    cfg = tmp_path / "cfg"
    _build(cfg, url="https://engine.example", token="tok", lease=False)
    assert mod.telemetry_endpoint_missing(cfg) is False
    other = tmp_path / "other"
    _build(other, url="http://evil.example", token="tok", lease=False)
    assert mod.telemetry_endpoint_missing(other) is True
