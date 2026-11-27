# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python3 <plugin root>/hooks/scripts/_audience.py show`` and the report behind it (nexus-sby49.8, RDR-259 Decision 5).

``nx settings show`` (nexus-sby49.13) imports the same module, so what a user inspects is what the hooks read. The report lists
both files, every key's effective value and source, every settings text under its provenance label, every problem, the retired
files that still exist, and the versions. Both byte copies print the same.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

from tests._audience_fixtures import PROJECT_STRICT_SETTINGS, USER_STRICT_SETTINGS, build_posture

ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGIN_COPY = ROOT / "conexus" / "hooks" / "scripts" / "_audience.py"
VERSIONS = {"plugin": "8.5.9", "nx": "8.5.9"}


def _show(audience, argv, versions=VERSIONS) -> "tuple[int, str]":
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = audience.main(argv, versions=versions)
    return rc, out.getvalue()


def _names(text, path):
    """The report names *path*, spelled as given or as its real path: the project file is printed under the session root
    the reader resolved, and on native Windows a temp directory's 8.3 short name is not the long name the reader prints."""
    shown = os.path.normcase(text)
    return any(os.path.normcase(spelling) in shown for spelling in (str(path), os.path.realpath(path)))


@pytest.fixture
def pinned(audience_copy, monkeypatch):
    def pin(posture):
        monkeypatch.setattr(audience_copy, "_ACCOUNT_HOME_OVERRIDE", str(posture.home))
        return audience_copy

    return pin


def test_a_plain_repository_shows_every_key_at_its_default(pinned, plain_posture):
    audience = pinned(plain_posture)
    rc, text = _show(audience, ["show", "--cwd", str(plain_posture.repo)])
    assert rc == 0
    for key in audience.SETTINGS:
        if key != "version":
            assert key in text, key
    assert "default" in text and "no problems" in text.lower()
    assert _names(text, plain_posture.repo / ".nexus" / "project.json")
    assert _names(text, plain_posture.home / ".config" / "nexus" / "conexus.json")
    assert "not present" in text or "absent" in text


def test_the_project_strict_posture_shows_its_values_sources_and_labelled_texts(pinned, project_strict_posture):
    audience = pinned(project_strict_posture)
    report = audience.build_report(str(project_strict_posture.repo), versions=VERSIONS)
    by_key = {row["key"]: row for row in report["settings"]}
    assert by_key["subagent_git"]["value"] == "strict" and by_key["subagent_git"]["source"] == "project"
    assert by_key["advice_block"]["value"] == ["whole_disk_scan"]
    assert by_key["preflight"]["value"] == PROJECT_STRICT_SETTINGS["preflight"] and by_key["telemetry"]["source"] == "default"
    labels = {(t["key"], t["source"]): t["text"] for t in report["texts"]}
    assert ("preflight", "project") in labels and ("brief_questions", "project") in labels
    assert labels[("preflight", "project")].startswith("This project's `.nexus/project.json` asks: run `")
    _, text = _show(audience, ["show", "--cwd", str(project_strict_posture.repo)])
    assert "This project's `.nexus/project.json` asks: run `scripts/agent-worktree-preflight.sh` first." in text
    assert "This project's `.nexus/project.json` adds these questions:" in text
    assert "Does this change touch the cloud path?" in text


def test_the_user_strict_posture_labels_its_texts_as_the_users(pinned, user_strict_posture):
    audience = pinned(user_strict_posture)
    report = audience.build_report(str(user_strict_posture.repo), versions=VERSIONS)
    by_key = {row["key"]: row for row in report["settings"]}
    assert by_key["subagent_git"]["source"] == "user" and by_key["advice_block"]["value"] == ["pattern_kill"]
    assert [(t["key"], t["source"]) for t in report["texts"]] == [("brief_questions", "user")]
    _, text = _show(audience, ["show", "--cwd", str(user_strict_posture.repo)])
    assert "Your `conexus.json` adds these questions:" in text and USER_STRICT_SETTINGS["brief_questions"][0] in text
    assert "This project's" not in text, "a user text never carries the project label"


def test_an_outranked_project_value_is_shown_beside_the_effective_one(pinned, tmp_path):
    posture = build_posture(tmp_path, "user-strict")
    repo = posture.repo
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1, "subagent_git": "seatbelt"}), encoding="utf-8")
    audience = pinned(posture)
    report = audience.build_report(str(repo), versions=VERSIONS)
    row = {r["key"]: r for r in report["settings"]}["subagent_git"]
    assert row["value"] == "strict" and row["source"] == "user"
    assert row["files"][".nexus/project.json"] == "seatbelt" and row["files"]["conexus.json"] == "strict"


def test_problems_are_listed_with_the_file_the_key_and_what_was_used(pinned, tmp_path):
    posture = build_posture(tmp_path, "plain")
    (posture.repo / ".nexus").mkdir()
    (posture.repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1, "subagent-git": "strict"}), encoding="utf-8")
    audience = pinned(posture)
    report = audience.build_report(str(posture.repo), versions=VERSIONS)
    assert [(p["file"], p["key"]) for p in report["problems"]] == [(".nexus/project.json", "subagent-git")]
    _, text = _show(audience, ["show", "--cwd", str(posture.repo)])
    assert "subagent-git" in text and "did you mean" in text and "subagent_git" in text


def test_retired_user_file_that_still_exists_is_listed_as_having_no_effect(pinned, tmp_path):
    posture = build_posture(tmp_path, "project-strict")
    retired_name = pinned(posture).RETIRED_USER_FILES[0]
    flag = posture.home / ".config" / "nexus" / retired_name
    flag.parent.mkdir(parents=True)
    flag.write_text("", encoding="utf-8")
    audience = pinned(posture)
    report = audience.build_report(str(posture.repo), versions=VERSIONS)
    retired = {pathlib.Path(r["path"]).name: r["note"] for r in report["retired"]}
    assert set(retired) == {retired_name}
    assert "no effect" in retired[retired_name]
    _, text = _show(audience, ["show", "--cwd", str(posture.repo)])
    assert "no effect" in text


@pytest.mark.parametrize("marker", [".nexus/dev-process.json", "scripts/agent-worktree-preflight.sh"])
@pytest.mark.parametrize("posture_name", ["plain", "project-strict"])
def test_the_retired_markers_are_not_reported_and_decide_nothing(pinned, tmp_path, marker, posture_name):
    """Stage 4 (RDR-259): the old marker files are neither a source nor a retired file; show says nothing about them."""
    posture = build_posture(tmp_path, posture_name)
    path = posture.repo.joinpath(*marker.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    audience = pinned(posture)
    report = audience.build_report(str(posture.repo), versions=VERSIONS)
    row = {r["key"]: r for r in report["settings"]}["subagent_git"]
    expected = ("strict", "project") if posture_name == "project-strict" else ("seatbelt", "default")
    assert (row["value"], row["source"]) == expected
    assert report["retired"] == []
    _, text = _show(audience, ["show", "--cwd", str(posture.repo)])
    assert "legacy" not in text and "dev-process" not in text


def test_the_report_warns_when_the_plugin_and_the_cli_differ(pinned, plain_posture):
    audience = pinned(plain_posture)
    _, same = _show(audience, ["show", "--cwd", str(plain_posture.repo)], versions={"plugin": "8.5.9", "nx": "8.5.9"})
    assert "8.5.9" in same and "differ" not in same.lower()
    _, skew = _show(audience, ["show", "--cwd", str(plain_posture.repo)], versions={"plugin": "8.5.9", "nx": "8.5.7"})
    assert "differ" in skew.lower() and "8.5.7" in skew
    _, none = _show(audience, ["show", "--cwd", str(plain_posture.repo)], versions={"plugin": "8.5.9", "nx": None})
    assert "not found" in none.lower() or "unknown" in none.lower()


def test_json_is_the_same_report(pinned, project_strict_posture):
    audience = pinned(project_strict_posture)
    rc, text = _show(audience, ["show", "--cwd", str(project_strict_posture.repo), "--json"])
    assert rc == 0
    assert json.loads(text) == json.loads(json.dumps(audience.build_report(str(project_strict_posture.repo), versions=VERSIONS)))


def test_head_difference_is_reported_inside_a_repository(pinned, tmp_path):
    from tests.hooks.git_shim import init_repo  # noqa: PLC0415

    posture = build_posture(tmp_path, "plain")
    repo = init_repo(tmp_path / "gitrepo", {".nexus/project.json": json.dumps({"version": 1, "subagent_git": "strict"})})
    (repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    audience = pinned(posture)
    report = audience.build_report(str(repo), versions=VERSIONS)
    assert report["head_differs"] is True
    row = {r["key"]: r for r in report["settings"]}["subagent_git"]
    assert row["value"] == "strict" and row["source"] == "project (HEAD)"
    same = init_repo(tmp_path / "same", {".nexus/project.json": json.dumps({"version": 1})})
    assert audience.build_report(str(same), versions=VERSIONS)["head_differs"] is False


def test_the_two_copies_print_the_same_report(tmp_path, monkeypatch):
    from tests._audience_fixtures import _load_copy  # noqa: PLC0415

    posture = build_posture(tmp_path, "project-strict")
    outputs = []
    for path, name in ((PLUGIN_COPY, "plugin"), (ROOT / "src" / "nexus" / "hooks" / "_audience.py", "src")):
        mod = _load_copy(path, "_audience_show_" + name)
        monkeypatch.setattr(mod, "_ACCOUNT_HOME_OVERRIDE", str(posture.home))
        outputs.append(_show(mod, ["show", "--cwd", str(posture.repo)]))
    assert outputs[0] == outputs[1] and outputs[0][0] == 0


def test_bad_arguments_exit_two(pinned, plain_posture):
    audience = pinned(plain_posture)
    for argv in ([], ["bogus"], ["show", "--cwd"], ["show", "--nope"]):
        rc, _ = _show(audience, argv)
        assert rc == 2, argv


def test_the_plugin_copy_runs_as_a_script(tmp_path):
    posture = build_posture(tmp_path, "project-strict")
    proc = subprocess.run(
        [sys.executable, str(PLUGIN_COPY), "show", "--cwd", str(posture.repo)], capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, check=False, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    assert "subagent_git" in proc.stdout and "strict" in proc.stdout and ".nexus/project.json" in proc.stdout


def test_a_working_copy_looser_than_head_says_the_committed_value_holds_until_it_is_committed(pinned, tmp_path):
    from tests.hooks.git_shim import init_repo  # noqa: PLC0415

    posture = build_posture(tmp_path, "plain")
    committed = {"version": 1, "subagent_git": "strict", "advice_block": ["pattern_kill"]}
    repo = init_repo(tmp_path / "loose", {".nexus/project.json": json.dumps(committed)})
    (repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    audience = pinned(posture)
    report = audience.build_report(str(repo), versions=VERSIONS)
    assert report["loosened"] == ["subagent_git", "advice_block"]
    _, text = _show(audience, ["show", "--cwd", str(repo)])
    assert "looser than HEAD for subagent_git, advice_block" in text and "until the change is committed" in text
    assert "differs from the working tree" in text


def test_a_working_copy_that_is_stricter_than_head_says_nothing_about_loosening(pinned, tmp_path):
    from tests.hooks.git_shim import init_repo  # noqa: PLC0415

    posture = build_posture(tmp_path, "plain")
    repo = init_repo(tmp_path / "tight", {".nexus/project.json": json.dumps({"version": 1})})
    (repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1, "subagent_git": "strict"}), encoding="utf-8")
    audience = pinned(posture)
    assert audience.build_report(str(repo), versions=VERSIONS)["loosened"] == []
    _, text = _show(audience, ["show", "--cwd", str(repo)])
    assert "looser" not in text


def test_a_head_with_no_copy_of_the_file_says_no_committed_copy_not_differs(pinned, tmp_path):
    from tests.hooks.git_shim import init_repo  # noqa: PLC0415

    posture = build_posture(tmp_path, "plain")
    repo = init_repo(tmp_path / "nocopy", {"README": "x"})
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "project.json").write_text(json.dumps({"version": 1, "subagent_git": "strict"}), encoding="utf-8")
    audience = pinned(posture)
    _, text = _show(audience, ["show", "--cwd", str(repo)])
    assert "project file at HEAD: no committed copy" in text and "differs" not in text


def test_outside_a_checkout_the_project_row_says_no_checkout(pinned, plain_posture, tmp_path):
    """A directory with no ``.git`` above it has no project file to read; the row says so, not "no account home"."""
    audience = pinned(plain_posture)
    outside = tmp_path / "no-repo-here"
    outside.mkdir()
    rc, text = _show(audience, ["show", "--cwd", str(outside)])
    assert rc == 0
    project_row = next(line for line in text.splitlines() if line.strip().startswith("project:"))
    assert "(no checkout)" in project_row, project_row
    assert "no account home" not in project_row, project_row
