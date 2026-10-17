# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ueztp: cc-validation scenario 32 carries a POSITIVE CONTROL, and this
file pins that it cannot silently vanish.

Scenario 32 (GHSA-mc84-6gjq-vm2p: the plugin's Bash hooks must not
blanket-allow) passes on "the permission prompt was seen". Until this bead no
run showed the harness could see the prompt being skipped, so a green 32 could
have been the harness failing to exercise the hooks at all. The scenario now
runs a second session (32b) with the conexus plugin of git tag v7.64.1, the
vulnerable one, declared to FAIL the same check. These tests pin the wiring:
the tag, how its tree is obtained (from the tag, version-checked, never
hand-copied), that a missing control is a failure and not a skip, and the
container route that is the only place Claude Code runs.

Nothing here starts Claude Code. The live result is recorded in the bead.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO = pathlib.Path(__file__).resolve().parent.parent
CCV = REPO / "tests" / "cc-validation"
SCENARIO = CCV / "scenarios" / "32_pretooluse_no_blanket_allow.sh"
RUNNER = CCV / "runner.sh"
CONTAINER = CCV / "container"
README = CCV / "README.md"
CONTROL_TAG = "v7.64.1"


def _code(path: pathlib.Path) -> str:
    """The file without comment-only lines."""
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")
    )


def _bash_function(path: pathlib.Path, name: str) -> str:
    """Source text of a top-level bash function (from `name() {` to the closing
    `}` at column 0)."""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name}() not found in {path}"
    return m.group(0)


def _bash(script: str, **env: str) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **env},
    )


# --- the control is declared, pinned to the tag, and reported both ways ------


def test_control_tag_is_pinned_in_the_scenario() -> None:
    assert f'S32_CONTROL_TAG="{CONTROL_TAG}"' in _code(SCENARIO)


def test_scenario_runs_both_sessions_and_reports_each() -> None:
    code = _code(SCENARIO)
    assert '_s32_run_session "32a-current"' in code
    assert "_s32_run_session \"32b-control-${S32_CONTROL_TAG}\"" in code
    # 32a keeps prompt_seen as its pass condition; 32b passes only when the
    # prompt was SKIPPED and fails when it appears.
    assert re.search(r"elif \[\[ \$b_prompt_seen -eq 1 \]\]; then\s+fail ", code)
    assert re.search(r'else\s+pass "32b positive control', code)
    assert re.search(r'else\s+pass "32a: the PERMISSION PROMPT', code)


def test_the_plugin_is_installed_through_a_marketplace_and_verified_loaded() -> None:
    """A hand-written installed_plugins.json leaves the plugin unloaded in
    Claude Code 2.1.302 ('Marketplace nexus-plugins not found'), which made the
    first container run's control vacuous. The only installed_plugins.json the
    scenario may write is the empty teardown one."""
    code = _code(SCENARIO)
    assert "claude plugin marketplace add" in code
    assert "claude plugin install conexus@nexus-plugins" in code
    assert 'grep -q "Status: ✔ enabled"' in code
    assert '"installPath":' not in code, "scenario 32 must not hand-write a plugin install entry"
    # a session never starts on an unloaded plugin, and both verdicts say so
    assert "b_loaded -eq 0" in code and "a_loaded -eq 0" in code


def test_the_control_must_be_informative_and_observed_in_the_transcript() -> None:
    """The control passes only when the clean run showed the prompt (otherwise
    the two are indistinguishable, as in auto mode) and Claude Code's own
    transcript shows the v7.64.1 hooks emitting a permissionDecision."""
    code = _code(SCENARIO)
    assert "32b control is NOT INFORMATIVE" in code
    assert re.search(r"elif \[\[ \$a_prompt_seen -eq 0 \]\]; then\s+fail \"32b control is NOT INFORMATIVE", code)
    assert "b_real -eq 0" in code
    assert "a_real -gt 0" in code


def test_the_sessions_run_in_default_mode_unless_overridden() -> None:
    assert 'S32_PERMISSION_MODE="${CC_VAL_S32_MODE:-default}"' in _code(SCENARIO)


def test_a_missing_control_is_a_failure_never_a_skip() -> None:
    code = _code(SCENARIO)
    assert not re.search(r"^\s*skip\b", code, re.M), "scenario 32 must not skip any leg"
    assert re.search(
        r"else\s+fail \"32b control: the \$\{S32_CONTROL_TAG\} plugin tree could not be staged", code
    )


def test_control_tree_comes_from_the_tag_not_a_copy() -> None:
    fn = _bash_function(SCENARIO, "_s32_stage_control_plugin")
    assert 'git -C "$REPO_ROOT" archive "${S32_CONTROL_TAG}" conexus' in fn
    # and the extracted tree is version-checked against the tag
    assert 'got" != "$want"' in fn


def test_the_shadow_hooks_run_the_scripts_of_the_plugin_under_test() -> None:
    code = _code(SCENARIO)
    assert '"${plugin_root}/${script_rel}"' in code
    assert "$REPO_ROOT/conexus/hooks/scripts/routing" not in code.split("_s32_run_session()")[0].split(
        "_s32_write_shadow_hook()"
    )[1], "the shadow hook must not hard-wire the current plugin's scripts"


# --- the staging function behaves --------------------------------------------


def _stage(tree: pathlib.Path) -> subprocess.CompletedProcess[str]:
    fn = _bash_function(SCENARIO, "_s32_stage_control_plugin")
    return _bash(
        f'S32_CONTROL_TAG="{CONTROL_TAG}"; S32_CONTROL_PLUGIN_ROOT=""\n{fn}\n'
        '_s32_stage_control_plugin && echo "ROOT=$S32_CONTROL_PLUGIN_ROOT"',
        CC_VAL_CONTROL_PLUGIN_ROOT=str(tree), REPO_ROOT=str(REPO), TEST_HOME=str(tree.parent),
    )


def _fake_plugin(root: pathlib.Path, version: str) -> pathlib.Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "conexus", "version": version}))
    (root / "hooks" / "scripts" / "routing").mkdir(parents=True)
    (root / "hooks" / "hooks.json").write_text("{}")
    (root / "hooks" / "scripts" / "routing" / "credential_print_guard.py").write_text("")
    return root


def test_staging_accepts_a_tree_of_the_pinned_version(tmp_path: pathlib.Path) -> None:
    r = _stage(_fake_plugin(tmp_path / "conexus", CONTROL_TAG.lstrip("v")))
    assert r.returncode == 0, r.stderr
    assert "ROOT=" in r.stdout


def test_staging_rejects_a_tree_of_another_version(tmp_path: pathlib.Path) -> None:
    """Without this a wrong or current tree would stand in for the vulnerable
    plugin and the control would 'fail' for no reason."""
    r = _stage(_fake_plugin(tmp_path / "conexus", "7.82.0"))
    assert r.returncode != 0
    assert "not a v7.64.1 conexus plugin" in r.stderr


def test_staging_rejects_an_empty_tree(tmp_path: pathlib.Path) -> None:
    (tmp_path / "conexus").mkdir()
    assert _stage(tmp_path / "conexus").returncode != 0


def test_the_tag_extraction_path_archives_the_tag_and_checks_the_version(tmp_path: pathlib.Path) -> None:
    """The scenario's own fallback (no CC_VAL_CONTROL_PLUGIN_ROOT): an archive
    of the tag through the real function, against a throwaway repository that
    carries the tag, so it needs neither this clone's tags nor the network.
    What the real tag contains is checked by the live run (the bead's)."""
    repo = tmp_path / "repo"
    _fake_plugin(repo / "conexus", CONTROL_TAG.lstrip("v"))
    ident = ["-c", "user.name=t", "-c", "user.email=t@invalid"]
    for args in (["init", "-q"], ["add", "-A"], [*ident, "commit", "-qm", "c"], ["tag", CONTROL_TAG]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    # a later commit changes the plugin: the tag, not HEAD, must be extracted
    (repo / "conexus" / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "9.9.9"}))
    subprocess.run(["git", "-C", str(repo), *ident, "commit", "-qam", "later"], check=True, capture_output=True)
    fn = _bash_function(SCENARIO, "_s32_stage_control_plugin")
    home = tmp_path / "home"
    home.mkdir()
    r = _bash(
        f'S32_CONTROL_TAG="{CONTROL_TAG}"; S32_CONTROL_PLUGIN_ROOT=""\n{fn}\n'
        '_s32_stage_control_plugin && echo "ROOT=$S32_CONTROL_PLUGIN_ROOT"',
        REPO_ROOT=str(repo), TEST_HOME=str(home),
    )
    assert r.returncode == 0, r.stderr
    root = pathlib.Path(r.stdout.strip().removeprefix("ROOT="))
    assert json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"] == "7.64.1"


# --- the container route: the only place Claude Code runs --------------------


def test_container_files_exist_and_agree_on_the_tag() -> None:
    for name in ("Dockerfile", "run.sh", "entrypoint_in_container.sh"):
        assert (CONTAINER / name).is_file(), name
    assert f'CONTROL_TAG="{CONTROL_TAG}"' in _code(CONTAINER / "run.sh")


def test_container_run_passes_the_token_by_name_never_by_value() -> None:
    code = _code(CONTAINER / "run.sh")
    assert re.search(r"-e CLAUDE_CODE_OAUTH_TOKEN(?![=\w])", code)
    assert "CLAUDE_CODE_OAUTH_TOKEN=" not in code
    assert "docker run --rm" in code


def test_runner_container_mode_only_replaces_run_and_fails_loud_without_a_token() -> None:
    fn = _bash_function(RUNNER, "_cred_tool")
    sentinel = "sentinel-not-a-real-credential"
    ok = _bash(
        f"{fn}\n_cred_tool run -- echo ran",
        **{"NX_CC_VAL_IN_CONTAINER": "1", "CLAUDE_CODE_OAUTH_TOKEN": sentinel},
    )
    assert ok.returncode == 0 and ok.stdout.strip() == "ran"
    # the token is never echoed
    assert sentinel not in ok.stdout + ok.stderr
    empty = _bash(f"{fn}\n_cred_tool run -- echo ran", NX_CC_VAL_IN_CONTAINER="1")
    assert empty.returncode != 0 and "ran" not in empty.stdout
    assert "no automation token" in empty.stderr


def test_the_tmux_server_start_is_still_under_cred_tool_run() -> None:
    """tests/test_cc_validation_runner_automation_token.py pins this too; the
    container mode must not have moved the call out from under the wrapper."""
    lines = [ln for ln in _code(RUNNER).splitlines() if "new-session" in ln and "tmux" in ln]
    assert len(lines) == 1 and "_cred_tool run --" in lines[0]


def test_readme_documents_the_control_and_the_container_route() -> None:
    text = README.read_text(encoding="utf-8")
    assert "Running in a container" in text
    assert CONTROL_TAG in text
    assert "positive control" in text.lower()
