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
    # each leg's verdict comes from the ladder, never from an inline pass
    assert "_s32_verdict leg=a" in code and "_s32_verdict leg=b" in code
    assert code.count('"$S32_VERDICT_KIND" "$S32_VERDICT_MSG"') == 2
    assert not re.search(r"^\s*pass \"32", code, re.M), "a leg must not pass outside _s32_verdict"


def test_the_plugin_is_installed_through_a_marketplace_and_verified_loaded() -> None:
    """A hand-written installed_plugins.json leaves the plugin unloaded in
    Claude Code 2.1.302 ('Marketplace nexus-plugins not found'), which made the
    first container run's control vacuous. The install is the shared
    cc_plugin_install (runner.sh, nexus-spwp2); tests/test_cc_validation_plugin_install.py
    pins the helper and that no scenario writes installed_plugins.json."""
    code = _code(SCENARIO)
    assert "cc_plugin_install" in code and "nexus-plugins" in code
    assert "claude plugin marketplace" not in code and "claude plugin install" not in code, (
        "scenario 32 must use the shared helper, not its own install"
    )
    helper = _bash_function(RUNNER, "cc_plugin_install")
    assert 'claude plugin marketplace add "$mkt"' in helper
    assert 'claude plugin install "$plugin@$mkt_name" --scope user' in helper
    assert 'grep -qF "Status: ✔ enabled"' in helper
    assert '"installPath":' not in code, "scenario 32 must not hand-write a plugin install entry"


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


# --- the verdict ladder: a truth table, driven from the real bash ------------

_GOOD_A = dict(leg="a", mode="default", loaded=1, version_ok=1, marker=1, real_hooks=2, shadow=1,
               prompt=1, tool=1, blanket=0, real_dec=0)
_GOOD_B = dict(leg="b", tag=CONTROL_TAG, mode="default", loaded=1, version_ok=1, marker=1, real_hooks=2,
               shadow=1, prompt=0, tool=1, blanket=1, real_dec=3, a_prompt=1)


def _verdict(**kw: object) -> tuple[str, str]:
    fn = _bash_function(SCENARIO, "_s32_verdict")
    args = " ".join(f"{k}={v}" for k, v in kw.items())
    r = _bash(f'{fn}\n_s32_verdict {args}\nprintf "%s\\n%s\\n" "$S32_VERDICT_KIND" "$S32_VERDICT_MSG"')
    assert r.returncode == 0, r.stderr
    kind, _, msg = r.stdout.partition("\n")
    return kind, msg.strip()


# (leg inputs, overrides, expected kind, fragment of the message)
_LADDER = [
    (_GOOD_A, {}, "pass", "PERMISSION PROMPT"),
    (_GOOD_A, dict(prompt=0), "fail", "INDETERMINATE"),
    # vacuous cases: the prompt was seen, but the guards under test are not shown to have run
    (_GOOD_A, dict(marker=0), "fail", "marker hook"),
    (_GOOD_A, dict(real_hooks=0), "fail", "only 0 of the 2 real routing guards"),
    (_GOOD_A, dict(real_hooks=1), "fail", "only 1 of the 2 real routing guards"),
    (_GOOD_A, dict(version_ok=0), "fail", "pinned build"),
    (_GOOD_A, dict(loaded=0), "fail", "did not load"),
    (_GOOD_A, dict(shadow=0), "fail", "shadow"),
    (_GOOD_A, dict(blanket=1), "fail", "REAL FINDING"),
    (_GOOD_A, dict(real_dec=1), "fail", "REAL FINDING"),
    (_GOOD_B, {}, "pass", "positive control"),
    (_GOOD_B, dict(marker=0), "fail", "marker hook"),
    (_GOOD_B, dict(real_hooks=0), "fail", "of the 2 real routing guards"),
    (_GOOD_B, dict(version_ok=0), "fail", "pinned build"),
    (_GOOD_B, dict(loaded=0), "fail", "did not load"),
    (_GOOD_B, dict(prompt=1), "fail", "DID NOT FAIL"),
    (_GOOD_B, dict(a_prompt=0), "fail", "NOT INFORMATIVE"),
    (_GOOD_B, dict(tool=0), "fail", "INDETERMINATE"),
    (_GOOD_B, dict(blanket=0), "fail", "no permissionDecision"),
    (_GOOD_B, dict(real_dec=0), "fail", 'permissionDecision "allow"'),
]


@pytest.mark.parametrize("base,override,kind,fragment", _LADDER)
def test_verdict_ladder_truth_table(base: dict, override: dict, kind: str, fragment: str) -> None:
    got_kind, msg = _verdict(**{**base, **override})
    assert got_kind == kind, msg
    assert fragment in msg, msg


def test_verdict_ladder_rejects_unknown_input() -> None:
    kind, msg = _verdict(**{**_GOOD_A, "bogus": 1})
    assert kind == "fail" and "unknown argument" in msg
    kind, msg = _verdict(**{**_GOOD_A, "leg": "c"})
    assert kind == "fail" and "leg must be" in msg


def test_verdict_ladder_passes_only_when_every_precondition_holds() -> None:
    """Exhaustive sweep (one bash process): over every combination of the
    inputs a verdict reads, a pass appears exactly when the plugin loaded, the
    build is the pinned one, the marker and BOTH real guards ran, the shadow
    fired, and the leg's own prompt condition holds. A pass anywhere else is a
    vacuous green, the defect the 2026-10-17 review found."""
    fn = _bash_function(SCENARIO, "_s32_verdict")
    script = f"""{fn}
for leg in a b; do
for loaded in 0 1; do for version_ok in 0 1; do for marker in 0 1; do for real_hooks in 0 1 2; do
for shadow in 0 1; do for prompt in 0 1; do for tool in 0 1; do for blanket in 0 1; do
for real_dec in 0 1; do for a_prompt in 0 1; do
  _s32_verdict leg=$leg tag={CONTROL_TAG} mode=default loaded=$loaded version_ok=$version_ok marker=$marker \\
    real_hooks=$real_hooks shadow=$shadow prompt=$prompt tool=$tool blanket=$blanket real_dec=$real_dec a_prompt=$a_prompt
  echo "$S32_VERDICT_KIND $leg $loaded $version_ok $marker $real_hooks $shadow $prompt $tool $blanket $real_dec $a_prompt"
done; done; done; done; done; done; done; done; done; done
done
"""
    r = _bash(script)
    assert r.returncode == 0, r.stderr
    rows = [ln.split() for ln in r.stdout.splitlines()]
    assert len(rows) == 2 * 2 * 2 * 2 * 3 * 2 * 2 * 2 * 2 * 2 * 2, "the sweep did not cover every combination"
    passes = [r_ for r_ in rows if r_[0] == "pass"]
    assert passes, "no combination passes: the ladder is unreachable"
    for kind, leg, loaded, version_ok, marker, real_hooks, shadow, prompt, tool, blanket, real_dec, a_prompt in passes:
        assert (loaded, version_ok, marker, real_hooks, shadow) == ("1", "1", "1", "2", "1"), (kind, leg, passes[:3])
        if leg == "a":
            assert (prompt, blanket, real_dec) == ("1", "0", "0")
        else:
            assert (prompt, tool, blanket, a_prompt) == ("0", "1", "1", "1") and int(real_dec) > 0


# --- the evidence readers ----------------------------------------------------


def _run_fn(name: str, call: str) -> subprocess.CompletedProcess[str]:
    return _bash(f"{_bash_function(SCENARIO, name)}\n{call}")


def _debug_line(script: str, plugin: str = "conexus@nexus-plugins", status: int = 0) -> str:
    return (
        "2026-10-17T21:01:16.120Z [DEBUG] Hooks: PreToolUse:Bash [uv tool run --directory /tmp/mkt/conexus "
        f"--no-config --quiet --python >=3.12 python /tmp/mkt/conexus/hooks/scripts/routing/{script}] "
        f"(plugin {plugin}) finished with status {status} (53ms)\n"
    )


def test_real_hook_count_reads_the_debug_log(tmp_path: pathlib.Path) -> None:
    cred, git = "credential_print_guard.py", "subagent_git_write_requires_orchestrator.py"

    def count(text: str | None) -> str:
        log = tmp_path / "debug.log"
        log.unlink(missing_ok=True)
        if text is not None:
            log.write_text(text)
        return _run_fn("_s32_count_real_hooks", f'_s32_count_real_hooks "{log}"').stdout.strip()

    assert count(_debug_line(cred) + _debug_line(git)) == "2"
    assert count(_debug_line(cred)) == "1"
    assert count("") == "0"
    assert count(None) == "0", "a missing debug log is zero hooks, not an error"
    # a hook run by something other than the installed plugin does not count (a settings.json shadow hook)
    assert count(_debug_line(cred, plugin="other@market") + _debug_line(git, plugin="other@market")) == "0"
    # a guard that did not finish with status 0 did not run cleanly
    assert count(_debug_line(cred, status=1) + _debug_line(git)) == "1"
    # the SessionStart hooks are not the Bash guards
    assert count(_debug_line(cred).replace("PreToolUse:Bash", "SessionStart:startup")) == "0"


def _att(hook: str, stdout: str) -> str:
    return json.dumps({"attachment": {"type": "hook_success", "hookName": hook, "stdout": stdout}}) + "\n"


def test_transcript_decisions_distinguish_allow_from_any_decision(tmp_path: pathlib.Path) -> None:
    allow = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}})
    deny = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}})
    d = tmp_path / "projects" / "p"
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        _att("PreToolUse:Bash", allow) + _att("PreToolUse:Bash", deny) + _att("PreToolUse:Bash", "")
        + _att("SessionStart:startup", allow) + "not json\n" + _att("PreToolUse:Bash", "plain text")
    )

    def n(shape: str) -> str:
        return _run_fn("_s32_transcript_decisions", f'_s32_transcript_decisions "{tmp_path}/projects" {shape}').stdout.strip()

    assert n("any") == "2"
    assert n("allow") == "1", "32b needs the allow shape, not any permissionDecision"
    empty = tmp_path / "none"
    empty.mkdir()
    assert _run_fn("_s32_transcript_decisions", f'_s32_transcript_decisions "{empty}" any').stdout.strip() == "0"


def test_version_check_requires_a_pin_and_an_exact_match() -> None:
    def ok(want: str, got: str) -> bool:
        return _run_fn("_s32_version_ok", f'_s32_version_ok "{want}" "{got}"').returncode == 0

    assert ok("2.1.302", "2.1.302")
    assert not ok("2.1.302", "2.1.303")
    assert not ok("", "2.1.302"), "an unpinned run is not a match"
    assert not ok("2.1.302", "")


def test_marker_hook_is_appended_to_the_bash_group_and_nothing_else_changes(tmp_path: pathlib.Path) -> None:
    src = json.loads((REPO / "conexus" / "hooks" / "hooks.json").read_text())
    p = tmp_path / "hooks.json"
    p.write_text(json.dumps(src))
    r = _run_fn("_s32_add_marker_hook", f'_s32_add_marker_hook "{p}" /tmp/m')
    assert r.returncode == 0, r.stderr
    out = json.loads(p.read_text())
    before = [g for g in src["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"][0]["hooks"]
    after = [g for g in out["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"][0]["hooks"]
    assert after[:-1] == before and len(before) == 2, "the plugin's own Bash hooks must be untouched"
    assert after[-1] == {"type": "command", "command": "touch", "args": ["/tmp/m"]}
    # every other event and group is byte-for-byte what the plugin ships
    src["hooks"]["PreToolUse"] = [g for g in src["hooks"]["PreToolUse"] if g.get("matcher") != "Bash"]
    out["hooks"]["PreToolUse"] = [g for g in out["hooks"]["PreToolUse"] if g.get("matcher") != "Bash"]
    assert src == out


def test_marker_hook_refuses_an_ambiguous_matcher_group(tmp_path: pathlib.Path) -> None:
    p = tmp_path / "hooks.json"
    p.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}, {"matcher": "Bash", "hooks": []}]}}))
    assert _run_fn("_s32_add_marker_hook", f'_s32_add_marker_hook "{p}" /tmp/m').returncode != 0


# --- the container: bounded, pinned, no stale names --------------------------


def test_container_run_is_resource_bounded() -> None:
    code = _code(CONTAINER / "run.sh")
    for flag in ("--memory 4g", "--cpus 2", "--pids-limit"):
        assert flag in code, flag


def test_claude_code_is_pinned_in_the_image_and_handed_to_the_scenario() -> None:
    dockerfile = _code(CONTAINER / "Dockerfile")
    m = re.search(r"^ARG CLAUDE_CODE_VERSION=(\d+\.\d+\.\d+)$", dockerfile, re.M)
    assert m, "the Dockerfile must pin ARG CLAUDE_CODE_VERSION"
    assert 'bash -s "${CLAUDE_CODE_VERSION}"' in dockerfile
    assert "DISABLE_AUTOUPDATER=1" in dockerfile, "a pinned install must not update itself at run time"
    run = _code(CONTAINER / "run.sh")
    assert "CC_VAL_EXPECTED_CLAUDE_VERSION" in run
    assert "claude-version.txt" in _code(CONTAINER / "entrypoint_in_container.sh")
    assert "_s32_version_ok" in _code(SCENARIO)


def test_no_stale_names_survive() -> None:
    for path in (RUNNER, SCENARIO, README, *CONTAINER.iterdir()):
        text = path.read_text(encoding="utf-8")
        assert "run_in_container.sh" not in text, f"{path.name} names a script that does not exist (run.sh)"
    assert "claude_start_auto" not in SCENARIO.read_text(encoding="utf-8").replace(
        "scenario 28's or 16's claude_start_auto", ""
    )
    text = README.read_text(encoding="utf-8")
    assert "does not forward the environment" not in text
    assert "dockerd" in text, "the README must give the real reason the container is not behind lab-run"
