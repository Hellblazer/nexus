# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-spwp2: no cc-validation scenario hand-writes a plugin install, and every
scenario that depends on a plugin goes through the one shared helper.

Claude Code 2.1.302 does not load a plugin from a hand-written
``installed_plugins.json`` entry: ``claude plugin list`` says "failed to load:
Marketplace nexus-plugins not found" and no hook, agent or server of the plugin
exists in the session. Scenarios 12 and 18 (and the fake-plugin parts 13b and
14b) wrote that file, so they ran with NO plugin and passed or failed on a
plugin-less session. The fix is ``cc_plugin_install`` / ``cc_plugin_require`` in
``tests/cc-validation/runner.sh`` (a local-directory marketplace, then a check
that ``claude plugin list`` shows the plugin enabled at the version its
plugin.json declares). These tests pin the census, the helper's behaviour against
a fake ``claude`` that reproduces both the loaded and the not-loaded answers, and
that a hook-asserting scenario reads the debug log, not the transcript.

nexus-qsmc5 adds: the prompt lint over every scenario whose success marker could
be read off its own prompt, scenarios 05-07 launching WITHOUT the bypass flag with
a control that proves permissions are enforced, scenario 14's completion wait and
its spawn-based verdict, scenario 18's whole-delivery check, and the plugin-scoped
context matcher.

Nothing here starts Claude Code. The live runs are recorded in the bead.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO = pathlib.Path(__file__).resolve().parent.parent
CCV = REPO / "tests" / "cc-validation"
SCENARIOS = sorted((CCV / "scenarios").glob("[0-9]*.sh"))
RUNNER = CCV / "runner.sh"

#: The scenarios that install or depend on a plugin, found by the census in the
#: bead's commit (grep installed_plugins / enabledPlugins / plugin install /
#: marketplace over every scenario). The census count is the non-vacuity floor.
CENSUS = {"12", "13", "14", "18", "32"}
#: Scenarios in tests/cc-validation/scenarios when the census was taken.
SCENARIO_COUNT = 30

#: What marks a scenario as plugin-dependent: it installs one through the helper,
#: or carries any of the hand-written install's markers.
_DEPENDENT = re.compile(
    r"cc_plugin_(install|require)|installed_plugins\.json|enabledPlugins|installPath"
    r"|extraKnownMarketplaces|claude plugin |--plugin-dir"
)


def _code(path: pathlib.Path) -> str:
    """The file without comment-only lines."""
    return "\n".join(
        ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")
    )


def _num(path: pathlib.Path) -> str:
    return path.name.split("_", 1)[0].lstrip("0") or "0"


def _bash_function(path: pathlib.Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", text, re.S | re.M)
    assert m, f"{name}() not found in {path}"
    return m.group(0)


def _dependents() -> dict[str, pathlib.Path]:
    return {_num(p): p for p in SCENARIOS if _DEPENDENT.search(_code(p))}


# --- the census ---------------------------------------------------------------


def test_the_scenario_set_is_the_one_the_census_examined() -> None:
    assert len(SCENARIOS) == SCENARIO_COUNT, (
        f"{len(SCENARIOS)} scenarios, the census examined {SCENARIO_COUNT}: a new scenario must be "
        "classified (plugin-dependent or not) and CENSUS/SCENARIO_COUNT updated"
    )


def test_the_plugin_dependent_scenarios_are_the_census() -> None:
    found = set(_dependents())
    assert found == CENSUS, f"plugin-dependent scenarios {sorted(found)} != census {sorted(CENSUS)}"
    assert len(found) >= 5, "non-vacuity floor: the census found fewer than five plugin-dependent scenarios"


# --- no scenario hand-writes an install ---------------------------------------


@pytest.mark.parametrize("path", SCENARIOS, ids=lambda p: p.name)
def test_no_scenario_hand_writes_installed_plugins_or_enablement(path: pathlib.Path) -> None:
    code = _code(path)
    writes = [
        ln for ln in code.splitlines()
        if re.search(r">{1,2}\s*\"?[^\s\"]*installed_plugins\.json", ln) or "tee " in ln and "installed_plugins" in ln
    ]
    assert not writes, f"{path.name} writes installed_plugins.json: {writes}"
    assert '"installPath"' not in code, f"{path.name} hand-writes a plugin install entry"
    assert "enabledPlugins" not in code, (
        f"{path.name} hand-writes enabledPlugins; `claude plugin install` writes it"
    )


def test_the_runner_writes_only_the_empty_registry() -> None:
    fn = _bash_function(RUNNER, "_cc_plugins_baseline")
    assert '{"version": 2, "plugins": {}}' in fn
    code = _code(RUNNER)
    hits = [ln for ln in code.splitlines() if re.search(r">\s*\"?[^\s\"]*installed_plugins\.json", ln)]
    assert len(hits) == 1, f"the runner must write installed_plugins.json in exactly one place: {hits}"


# --- every dependent scenario goes through the shared helper ------------------


def test_every_plugin_dependent_scenario_calls_the_shared_helper() -> None:
    deps = _dependents()
    assert len(deps) >= 5
    for num, path in deps.items():
        assert re.search(r"\bcc_plugin_(require|install)\b", _code(path)), (
            f"scenario {num} depends on a plugin but never calls cc_plugin_require/cc_plugin_install"
        )


@pytest.mark.parametrize("num", ["12", "13", "14", "18"])
def test_the_plain_scenarios_use_the_failing_precondition_and_tear_down(num: str) -> None:
    code = _code(_dependents()[num])
    assert re.search(r"if cc_plugin_require \S+ \S+ \S+; then", code), (
        f"scenario {num} must start its session only inside `if cc_plugin_require ...; then`"
    )
    assert "cc_plugin_uninstall_all" in code
    assert not re.search(r"^\s*skip\b.*(plugin (is )?not (loaded|installed)|did not load)", code, re.M), "an unloaded plugin must fail, not skip"


def test_scenario_32_installs_through_the_helper_and_fails_the_leg_when_unloaded() -> None:
    code = _code(CCV / "scenarios" / "32_pretooluse_no_blanket_allow.sh")
    assert "cc_plugin_install" in code
    assert not re.search(r"^\s*skip\b", code, re.M)
    assert "claude plugin marketplace add" not in code, "scenario 32 must not carry its own copy of the install"


def test_the_helper_installs_through_a_marketplace_and_checks_the_list() -> None:
    fn = _bash_function(RUNNER, "cc_plugin_install")
    assert 'claude plugin marketplace add "$mkt"' in fn
    assert 'claude plugin install "$plugin@$mkt_name" --scope user' in fn
    assert 'grep -qF "Status: ✔ enabled"' in fn
    assert "grep -qxF \"$CC_PLUGIN_VERSION\"" in fn, "the Version: value must match exactly, not as a substring"
    assert 'grep -qF "Version:' not in fn
    assert "failed to load" in fn


# --- a hook-asserting scenario reads the debug log, not the transcript --------


@pytest.mark.parametrize("num,helper", [("12", "cc_mcp_hook_ran"), ("13", "cc_hook_ran"), ("18", "cc_hook_ran")])
def test_hook_assertions_use_the_debug_log(num: str, helper: str) -> None:
    code = _code(_dependents()[num])
    assert helper in code, f"scenario {num} asserts a hook ran without {helper}"
    assert ".jsonl" not in code and "claude/projects" not in code, (
        f"scenario {num} reads the session transcript, which omits a hook that printed nothing"
    )


def test_scenario_12_names_the_plugin_in_its_mcp_hook_assertion() -> None:
    assert 'cc_mcp_hook_ran "$CC_PLUGIN_ID" "hook_subagent_start"' in _code(_dependents()["12"])


def test_scenario_18_also_needs_the_context_to_have_been_delivered_whole() -> None:
    code = _code(_dependents()["18"])
    assert 'cc_hook_context_is_whole "SubagentStart" "subagent_start.py" "$CC_PLUGIN_ID" $WHOLE18' in code
    assert "cc_hook_context_injected" not in code, "non-empty context is not both sections"


def _s18_whole_sizes() -> list[int]:
    """Run scenario 18's own size computation against the real sn section files."""
    text = (CCV / "scenarios" / "18_real_sn_subagent.sh").read_text(encoding="utf-8")
    start = text.index("python3 - ") + len("python3 - ")
    start = text.index("\n", start) + 1
    end = text.index("\nPY\n", start)
    r = subprocess.run(
        ["python3", "-", str(REPO / "sn" / "hooks" / "scripts")], input=text[start:end], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    return [int(x) for x in r.stdout.split()]


def test_scenario_18_expected_size_is_what_the_real_hook_emits(tmp_path: pathlib.Path) -> None:
    """The size the scenario waits for is the length of the sn hook's real output:
    both sections for an ordinary dispatch, with the worktree section first for a
    linked-worktree one. A partial delivery (one section) is neither size."""
    import importlib.util

    both, with_worktree = _s18_whole_sizes()
    spec = importlib.util.spec_from_file_location("sn_subagent_start", REPO / "sn" / "hooks" / "scripts" / "subagent_start.py")
    mod = importlib.util.module_from_spec(spec)
    sys_path = list(__import__("sys").path)
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    finally:
        __import__("sys").path[:] = sys_path
    assert len(mod.build(json.dumps({"cwd": str(tmp_path)}))) == both, "scenario 18 would wait for a size the hook does not emit"
    scripts = REPO / "sn" / "hooks" / "scripts"
    serena = len((scripts / "serena-section.md").read_text(encoding="utf-8"))
    context7 = len((scripts / "context7-section.md").read_text(encoding="utf-8"))
    assert both == serena + context7 and both not in (serena, context7)
    assert with_worktree > both


def test_scenario_12_judges_the_reply_against_the_id_the_hook_emitted() -> None:
    """A fixed probe phrase is satisfiable from a prompt; a per-dispatch id the
    debug log recorded is not. The verdict must compare the two."""
    code = _code(_dependents()["12"])
    assert "Claimant id: [0-9a-f]" in code and "CLAIM-12K7-$id" in code
    assert "check before duplicating" not in code
    # the reply is the subagent's own, never the main agent's paraphrase on the pane
    assert 'REPLIES12="$(cc_subagent_replies || true)"' in code
    assert "cc_wait_subagent_reply" in code
    assert 'capture -500 | grep' not in code and 'PANE12' not in code


def _s12_verdict(tmp_path: pathlib.Path, debug_log: str, replies: str, hook_ok: bool) -> str:
    """Run scenario 12's own verdict block under the runner's errexit/pipefail with
    stubbed helpers; return the one pass/fail line it printed."""
    text = (CCV / "scenarios" / "12_real_nx_subagent.sh").read_text(encoding="utf-8")
    start = text.index("    # The ids the hook emitted")
    end = text.index("    claude_exit")
    dbg = tmp_path / "dbg12.log"
    dbg.write_text(debug_log)
    script = (
        "set -euo pipefail\n"
        'pass() { echo "PASS:$*"; }\nfail() { echo "FAIL:$*"; }\ncapture() { echo PANE; }\n'
        f"cc_subagent_replies() {{ printf '%s\\n' {replies!r}; }}\n"
        f"cc_mcp_hook_ran() {{ {'true' if hook_ok else 'false'}; }}\n"
        f'CC_DEBUG_FILE="{dbg}"\nCC_PLUGIN_ID=conexus@nexus-plugins\n' + text[start:end] + '\necho REACHED_END\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "REACHED_END" in r.stdout, f"the verdict block died before its end: {r.stdout}{r.stderr}"
    return next(ln for ln in r.stdout.splitlines() if ln.startswith(("PASS:", "FAIL:")))


_S12_LOG = "[DEBUG] Hook SubagentStart: Claimant id: 0123456789abcdef \\u2014 mailbox\n"


def test_scenario_12_verdicts(tmp_path: pathlib.Path) -> None:
    good = "CLAIM-12K7-0123456789abcdef"
    assert _s12_verdict(tmp_path, _S12_LOG, good, True).startswith("PASS:")
    # right id, but no clean plugin hook call in the debug log
    assert _s12_verdict(tmp_path, _S12_LOG, good, False).startswith("FAIL:")
    # the subagent says absent
    assert "not injecting" in _s12_verdict(tmp_path, _S12_LOG, "ABSENT-12K7", True)
    # a claim of an id the hook never emitted (a guess or a leak from elsewhere)
    assert "not one the hook emitted" in _s12_verdict(tmp_path, _S12_LOG, "CLAIM-12K7-ffffffffffffffff", True)
    # the hook emitted nothing, so the debug log holds no id (errexit must not end the block)
    assert "not injecting" in _s12_verdict(tmp_path, "[DEBUG] nothing\n", "ABSENT-12K7", True)
    assert "not one the hook emitted" in _s12_verdict(tmp_path, "[DEBUG] nothing\n", good, True)
    # no reply at all
    assert "indeterminate" in _s12_verdict(tmp_path, _S12_LOG, "", True)


def test_scenario_18_reads_the_subagents_own_reply() -> None:
    code = _code(_dependents()["18"])
    assert "OUT=$(cc_subagent_replies)" in code and "cc_wait_subagent_reply" in code
    assert "OUT=$(capture" not in code


def test_a_plugin_session_always_writes_a_debug_log() -> None:
    fn = _bash_function(RUNNER, "claude_start")
    assert '--debug-file ${CC_DEBUG_FILE}' in fn


# --- prompt-based scenarios pin their permission mode -------------------------


def test_every_scenario_pins_a_default_mode() -> None:
    missing = [p.name for p in SCENARIOS if '"defaultMode"' not in _code(p) and "--permission-mode" not in _code(p)]
    assert not missing, f"scenarios that leave the permission mode to Claude Code's default: {missing}"
    assert len(SCENARIOS) >= SCENARIO_COUNT


#: Settings modes under which a permission prompt (or its absence) is part of
#: what a scenario measures. acceptEdits/bypassPermissions only keep a session
#: from stalling and mean the same thing with the skip flag.
_PERMISSION_SENSITIVE = re.compile(r'"defaultMode":\s*"(default|auto|plan)"')
#: lib.sh's claude_start, which always launches --dangerously-skip-permissions
#: (not claude_start_auto / claude_start_s32, which pick their own mode).
_LIB_START = re.compile(r"(?<![\w])claude_start(?![\w])")
#: Scenarios that pin a permission-sensitive mode and still launch through the
#: skip-permissions claude_start. nexus-qsmc5 emptied this: 05, 06 and 07 now launch
#: with claude_start_enforced. A NEW scenario doing this fails the test below.
KNOWN_SKIP_PERMISSIONS: dict[str, str] = {}
#: The scenarios whose point is the permission system: each must launch enforced
#: and carry its own control (found by the census, floor below).
PERMISSION_SCENARIOS = {"5", "6", "7"}


def _relies_on_mode_under_skip_flag(path: pathlib.Path) -> bool:
    code = _code(path)
    return bool(_PERMISSION_SENSITIVE.search(code)) and bool(_LIB_START.search(code))


def test_no_scenario_relies_on_a_permission_mode_the_launch_flag_overrides() -> None:
    assert "--dangerously-skip-permissions" in (REPO / "tests" / "e2e" / "lib.sh").read_text(encoding="utf-8"), (
        "lib.sh claude_start no longer launches with the skip flag: this census has lost its premise"
    )
    found = {_num(p) for p in SCENARIOS if _relies_on_mode_under_skip_flag(p)}
    assert found == set(KNOWN_SKIP_PERMISSIONS), (
        f"scenarios that pin a permission-sensitive defaultMode but launch with --dangerously-skip-permissions: "
        f"{sorted(found)}. Launch with claude_start_enforced and prove the mode with a control"
    )
    # non-vacuity: the census must have SEEN the permission scenarios, launching enforced
    enforced = {
        _num(p) for p in SCENARIOS
        if _PERMISSION_SENSITIVE.search(_code(p)) and "claude_start_enforced" in _code(p)
    }
    assert PERMISSION_SCENARIOS <= enforced, f"the permission scenarios {sorted(PERMISSION_SCENARIOS - enforced)} do not launch enforced"
    sensitive = {_num(p) for p in SCENARIOS if _PERMISSION_SENSITIVE.search(_code(p))}
    assert len(sensitive) >= 5, f"non-vacuity: the census saw only {sorted(sensitive)} permission-sensitive scenarios"


def test_the_census_regexes_see_the_shapes_they_are_for() -> None:
    assert _PERMISSION_SENSITIVE.search('"permissions": { "defaultMode": "default" }')
    assert _PERMISSION_SENSITIVE.search('"defaultMode": "auto"')
    assert not _PERMISSION_SENSITIVE.search('"defaultMode": "acceptEdits"')
    assert _LIB_START.search("claude_start")
    assert not _LIB_START.search("claude_start_auto") and not _LIB_START.search("claude_start_s32")
    assert not _LIB_START.search("claude_start_enforced"), "the enforced launcher is not the bypass launcher"
    # the census catches the old shape: a pinned default mode launched through claude_start
    old = '"permissions": { "allow": [], "defaultMode": "default" }\nclaude_start\n'
    assert _PERMISSION_SENSITIVE.search(old) and _LIB_START.search(old)


# --- the lib.sh launcher: bypass by default, none when asked --------------------


def _launch(tmp_path: pathlib.Path, prelude: str, status: str) -> str:
    """Run lib.sh's claude_start with tmux stubbed; return what it typed into the pane."""
    script = (
        'source "$LIB"\n'
        'send_keys() { echo "SENT:$1"; }\ncapture() { echo "' + status + '"; }\nsleep() { :; }\n'
        f'{prelude}\nclaude_start\n'
    )
    r = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "LIB": str(REPO / "tests" / "e2e" / "lib.sh")},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    sent = [ln for ln in r.stdout.splitlines() if ln.startswith("SENT:")]
    assert len(sent) == 1, r.stdout
    return sent[0]


def test_lib_claude_start_launches_with_the_bypass_flag_unless_told_otherwise(tmp_path: pathlib.Path) -> None:
    assert "--dangerously-skip-permissions" in _launch(tmp_path, "", "Type a message")
    assert "--dangerously-skip-permissions" in _launch(tmp_path, "unset CLAUDE_PERMISSION_FLAG", "Type a message")
    # SET to empty: no bypass flag, and the ready sentinel is the one an unbypassed status bar shows
    typed = _launch(tmp_path, 'CLAUDE_PERMISSION_FLAG=""', "? for shortcuts")
    assert "--dangerously-skip-permissions" not in typed and "claude_fd_exec.sh" in typed
    # a caller may pass any other flag instead
    assert "--permission-mode=plan" in _launch(tmp_path, 'CLAUDE_PERMISSION_FLAG="--permission-mode=plan"', "? for shortcuts")


def test_claude_start_enforced_launches_without_the_bypass_flag_and_clears_the_pane() -> None:
    fn = _bash_function(RUNNER, "claude_start_enforced")
    script = (
        fn + '\nclaude_start() { echo "FLAG=[${CLAUDE_PERMISSION_FLAG-UNSET}]"; }\n'
        'send_keys() { echo "KEYS:$1"; }\n_tmux() { echo "TMUX:$*"; }\nsleep() { :; }\nTMUX_SESSION=s\n'
        'claude_start_enforced\necho "AFTER=[${CLAUDE_PERMISSION_FLAG-UNSET}]"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "FLAG=[]" in r.stdout, r.stdout + r.stderr
    assert "AFTER=[UNSET]" in r.stdout, "the override must not leak into later launches"
    assert "KEYS:clear" in r.stdout and "TMUX:clear-history -t s" in r.stdout, "an earlier prompt in the scrollback must not be read as this session's"


def test_the_runner_wrapper_still_adds_the_launch_args_for_the_enforced_launcher() -> None:
    wrapper = _bash_function(RUNNER, "claude_start")
    assert "_lib_claude_start" in wrapper and "_preseed_trust" in wrapper and "--debug-file" in wrapper
    assert "CLAUDE_PERMISSION_FLAG" not in wrapper, "the wrapper must pass the caller's flag choice through untouched"


# --- the permission scenarios prove permissions are enforced ----------------------


def _flags_verdict(fn_name: str, args: list[str]) -> str:
    script = (
        'pass() { echo "PASS:$*"; }\nfail() { echo "FAIL:$*"; }\n'
        + _bash_function(RUNNER, fn_name) + f"\n{fn_name} " + " ".join(f'"{a}"' for a in args) + "\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return next(ln for ln in r.stdout.splitlines() if ln.startswith(("PASS:", "FAIL:")))


def test_cc_allow_rule_verdict_needs_the_control_to_prompt_and_the_rule_to_grant() -> None:
    def v(cprompt: int, cran: int, aran: int, aprompt: int) -> str:
        return _flags_verdict("cc_allow_rule_verdict", ["05", "mcp__stub__*", str(cprompt), str(cran), str(aran), str(aprompt)])

    assert v(1, 0, 1, 0).startswith("PASS:")
    # the control did not prompt: the session bypasses permissions, so a tool run proves nothing
    assert "not enforced" in v(0, 1, 1, 0) and v(0, 1, 1, 0).startswith("FAIL:")
    assert v(0, 0, 1, 0).startswith("FAIL:")
    # the control prompted but the tool ran anyway (an answered or bypassed prompt)
    assert v(1, 1, 1, 0).startswith("FAIL:")
    # the allowed leg prompted: the rule did not grant
    assert "did NOT grant" in v(1, 0, 0, 1)
    assert "did NOT grant" in v(1, 0, 1, 1)
    # the allowed leg neither prompted nor ran the tool (server not connected)
    assert "nothing to conclude" in v(1, 0, 0, 0)


def test_cc_preempt_verdict_treats_the_no_allow_run_as_the_control() -> None:
    def v(ha: int, ra: int, pa: int, hb: int, rb: int, pb: int) -> str:
        return _flags_verdict("cc_preempt_verdict", [str(x) for x in (ha, ra, pa, hb, rb, pb)])

    # B reached no gate at all (no hook, no prompt): permissions are not enforced, whatever A did
    assert "not enforced" in v(0, 1, 0, 0, 1, 0) and v(0, 1, 0, 0, 1, 0).startswith("FAIL:")
    assert v(0, 1, 0, 0, 0, 0).startswith("FAIL:")
    # the answers the scenario exists to give
    assert v(0, 1, 0, 1, 1, 0).startswith("PASS:") and "PREEMPTS" in v(0, 1, 0, 1, 1, 0)
    assert v(1, 1, 0, 1, 1, 0).startswith("PASS:") and "regardless" in v(1, 1, 0, 1, 1, 0)
    # a prompt that the allow rule did not clear
    assert v(0, 0, 1, 1, 1, 0).startswith("FAIL:") and "did not grant" in v(0, 0, 1, 1, 1, 0)
    # sub-A did not run the tool
    assert v(0, 0, 0, 1, 1, 0).startswith("FAIL:")
    # B prompted but the hook never fired: it does not govern this prompt
    assert "never fired" in v(0, 1, 0, 0, 0, 1)
    # B's hook fired but the tool did not run
    assert "not honoured" in v(0, 1, 0, 1, 0, 0)


def test_cc_allow_rule_verdict_report_mode_accepts_either_honest_answer() -> None:
    """Scenario 06 asks whether a rule SHAPE matches: matches and does-not-match are both
    answers, but only behind a control that proves the mode prompts."""
    def v(cprompt: int, cran: int, aran: int, aprompt: int) -> str:
        return _flags_verdict("cc_allow_rule_verdict", ["06", "mcp__plugin_*__*", str(cprompt), str(cran), str(aran), str(aprompt), "report"])

    assert v(1, 0, 1, 0).startswith("PASS:") and "MATCHES" in v(1, 0, 1, 0)
    assert v(1, 0, 0, 1).startswith("PASS:") and "does NOT match" in v(1, 0, 0, 1)
    # no control prompt: no answer, in either direction
    assert v(0, 1, 1, 0).startswith("FAIL:") and v(0, 1, 0, 1).startswith("FAIL:")
    # neither a prompt nor a tool run (server not connected)
    assert v(1, 0, 0, 0).startswith("FAIL:")
    # and a prompt that the tool ran through is not a clean answer
    assert v(1, 0, 1, 1).startswith("FAIL:")


def test_cc_report_context_lines_is_informational(tmp_path: pathlib.Path) -> None:
    dbg = tmp_path / "dbgr.log"
    dbg.write_text("Hook SubagentStart (python3 /x/hook_inject.py) provided additionalContext (61 chars)\nnoise\n")
    r = _run(tmp_path, f'CC_DEBUG_FILE="{dbg}"; cc_report_context_lines; echo "rc=$?"')
    assert "hook_inject.py" in r.stdout and "rc=0" in r.stdout, r.stdout + r.stderr
    dbg.write_text("nothing here\n")
    r = _run(tmp_path, f'CC_DEBUG_FILE="{dbg}"; cc_report_context_lines; echo "rc=$?"')
    assert "NO additionalContext" in r.stdout and "rc=0" in r.stdout
    r = _run(tmp_path, 'CC_DEBUG_FILE=""; cc_report_context_lines; echo "rc=$?"')
    assert "no debug log" in r.stdout and "rc=0" in r.stdout


@pytest.mark.parametrize("num", sorted(PERMISSION_SCENARIOS))
def test_each_permission_scenario_launches_enforced_and_ends_in_a_control_verdict(num: str) -> None:
    path = next(p for p in SCENARIOS if _num(p) == num)
    code = _code(path)
    assert "claude_start_enforced" in code and not _LIB_START.search(code)
    assert "cc_permission_prompt_up" in code and "cc_dismiss_permission_prompt" in code
    assert ("cc_preempt_verdict" if num == "7" else "cc_allow_rule_verdict") in code
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:30])
    assert "nexus-qsmc5" in head and "KNOWN LIMIT" not in head
    if num == "6":
        assert '"$ALLOWED_PROMPT" report' in code, "06 reports whichever answer is true"
    if num != "7":
        # the control leg uses a rule that does NOT match the tool and waits for the prompt
        assert re.search(r'_s0[56]_leg "mcp__(other|zzz)_[^"]*" "[^"]+" 1', code)
        assert "cc_wait_permission_prompt" in code


def test_the_permission_prompt_helpers() -> None:
    up = _bash_function(RUNNER, "cc_permission_prompt_up")
    script = lambda pane: (  # noqa: E731
        f'capture() {{ printf "%s\\n" {pane!r}; }}\n' + up + "\ncc_permission_prompt_up && echo UP || echo DOWN\n"
    )
    r = subprocess.run(["bash", "-c", script("Call stub\n Do you want to proceed?\n 1. Yes")], capture_output=True, text=True)
    assert r.stdout.strip() == "UP", r.stderr
    r = subprocess.run(["bash", "-c", script("DONE")], capture_output=True, text=True)
    assert r.stdout.strip() == "DOWN"
    dismiss = _bash_function(RUNNER, "cc_dismiss_permission_prompt")
    assert "Escape" in dismiss and "return 0" in dismiss


def test_scenario_32_runs_in_default_mode_not_auto() -> None:
    code = _code(CCV / "scenarios" / "32_pretooluse_no_blanket_allow.sh")
    assert 'S32_PERMISSION_MODE="${CC_VAL_S32_MODE:-default}"' in code
    assert '--permission-mode=${S32_PERMISSION_MODE}' in code


# --- the container carries what a loaded plugin needs -------------------------


def test_the_container_stages_sn_and_builds_the_cli_from_the_staged_tree() -> None:
    run = _code(CCV / "container" / "run.sh")
    assert re.search(r"tests/e2e/lib conexus sn\b", run), "scenario 18 installs the sn plugin"
    # The CLI comes from the same tree as the plugins under test.
    assert re.search(r"git ls-files .* src .*pyproject\.toml", run), "run.sh must stage the CLI source (src/, pyproject.toml)"
    assert 'cli-src' in run and 'docker build -q -t "$IMAGE" "$CTX"' in run
    assert "NEXUS_CLI_VERSION" not in run and "--build-arg" not in run, "no version-keyed PyPI install"
    docker = _code(CCV / "container" / "Dockerfile")
    assert "COPY cli-src" not in docker, "a COPY layer keeps the source in the image after a later rm (nexus-qsmc5)"
    assert "--mount=type=bind,source=cli-src,target=/tmp/cli-src" in docker
    assert "uv tool install /tmp/cli-src" in docker
    assert "conexus==" not in docker and "NEXUS_CLI_VERSION" not in docker, "the CLI must not be fetched from PyPI by version"
    assert "test -x /usr/local/bin/nx-mcp" in docker
    assert "mcp==" in docker, "the stub scenarios need the mcp package"


# --- the helper's behaviour, against a fake `claude` --------------------------

_FAKE_CLAUDE = r"""#!/usr/bin/env bash
# fake claude: just the three `plugin` subcommands the helper calls.
mode="${FAKE_CLAUDE_MODE:-ok}"
state="$HOME/.fake-claude"; mkdir -p "$state"
case "${2:-}" in
  marketplace) echo "$4" > "$state/mkt_dir"; echo "Added"; exit 0 ;;
  install)
    id="$3"; plugin="${id%@*}"; mkt="${id#*@}"; dir="$(cat "$state/mkt_dir")"
    [ "$mode" = noinstall ] && { echo "install failed" >&2; exit 1; }
    ver="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["plugins"][0]["version"])' "$dir/.claude-plugin/marketplace.json")"
    dest="$HOME/.claude/plugins/cache/$mkt/$plugin/$ver"; mkdir -p "$(dirname "$dest")"; cp -R "$dir/$plugin" "$dest"
    python3 - "$HOME/.claude/plugins/installed_plugins.json" "$id" "$dest" "$ver" <<'PY'
import json, sys
path, pid, dest, ver = sys.argv[1:]
json.dump({"version": 2, "plugins": {pid: [{"scope": "user", "installPath": dest, "version": ver}]}}, open(path, "w"))
PY
    echo "Installed $id"; exit 0 ;;
  list)
    id="$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"]),""))' "$HOME/.claude/plugins/installed_plugins.json" 2>/dev/null)"
    printf 'Installed plugins:\n\n'
    case "$mode" in
      unloaded) printf '  ❯ %s\n    Version: dev\n    Scope: user\n    Status: ✘ failed to load\n    Error: Marketplace nexus-plugins not found\n' "${id:-conexus@nexus-plugins}" ;;
      wrongversion) printf '  ❯ %s\n    Version: 0.0.0\n    Scope: user\n    Status: ✔ enabled\n' "$id" ;;
      suffixversion) printf '  ❯ %s\n    Version: %s1\n    Scope: user\n    Status: ✔ enabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      prefixversion) printf '  ❯ %s\n    Version: 1%s\n    Scope: user\n    Status: ✔ enabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      loaderror) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✔ enabled\n    Error: hooks/hooks.json failed to load\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      disabled) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✘ disabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
      *) printf '  ❯ %s\n    Version: %s\n    Scope: user\n    Status: ✔ enabled\n' "$id" "$(python3 -c 'import json,sys;print(next(iter(json.load(open(sys.argv[1]))["plugins"].values()))[0]["version"])' "$HOME/.claude/plugins/installed_plugins.json")" ;;
    esac
    exit 0 ;;
esac
exit 2
"""


def _helpers() -> str:
    names = ["_cc_plugins_baseline", "cc_plugin_install", "cc_plugin_require", "_cc_evidence_copy",
             "cc_keep_evidence", "cc_plugin_uninstall_all", "cc_hook_ran", "cc_mcp_hook_ran",
             "cc_hook_context_chars", "cc_hook_context_injected", "cc_hook_context_is_whole",
             "_cc_transcript_items", "cc_subagent_replies", "cc_subagent_tool_uses", "cc_session_replies",
             "cc_wait_subagent_reply", "cc_wait_session_reply", "cc_report_context_lines"]
    return "\n".join(_bash_function(RUNNER, n) for n in names)


def _plugin(root: pathlib.Path, name: str = "conexus", version: str = "7.82.0") -> pathlib.Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": name, "version": version}))
    (root / "hooks").mkdir()
    (root / "hooks" / "hooks.json").write_text("{}")
    return root


def _run(tmp_path: pathlib.Path, body: str, mode: str = "ok", **extra: str) -> subprocess.CompletedProcess[str]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    claude = fakebin / "claude"
    claude.write_text(_FAKE_CLAUDE)
    claude.chmod(0o755)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": f"{fakebin}:{os.environ['PATH']}", "HOME": str(tmp_path / "realhome"),
        "TEST_HOME": str(home), "FAKE_CLAUDE_MODE": mode, **extra,
    }
    prelude = (
        'set -u\nfail() { echo "FAILCALL:$*"; }\nskip() { echo "SKIPCALL:$*"; }\n_SCENARIO="t scenario"\n'
        + _helpers() + "\n"
    )
    return subprocess.run(["bash", "-c", prelude + body], capture_output=True, text=True, env=env)


def test_a_loaded_plugin_passes_the_precondition(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    r = _run(tmp_path, f'cc_plugin_install "{root}" conexus nexus-plugins; echo "rc=$? id=$CC_PLUGIN_ID v=$CC_PLUGIN_VERSION dbg=$CC_DEBUG_FILE"')
    assert "rc=0 id=conexus@nexus-plugins v=7.82.0" in r.stdout, r.stdout + r.stderr
    assert "cc-debug-conexus.log" in r.stdout
    assert (tmp_path / "home" / "plugin-list.txt").read_text().count("✔ enabled") == 1


def test_the_hand_written_registry_shape_is_refused(tmp_path: pathlib.Path) -> None:
    """The 2.1.302 answer for a plugin that was never installed through a
    marketplace: 'failed to load: Marketplace ... not found'. The precondition
    must FAIL (a fail() call, never skip()) and return non-zero."""
    root = _plugin(tmp_path / "src" / "conexus")
    r = _run(tmp_path, f'cc_plugin_require "{root}" conexus nexus-plugins; echo "rc=$?"', mode="unloaded")
    assert "rc=1" in r.stdout, r.stdout + r.stderr
    assert "FAILCALL:t scenario: plugin conexus@nexus-plugins is NOT loaded" in r.stdout
    assert "SKIPCALL" not in r.stdout
    assert "Marketplace nexus-plugins not found" in r.stderr, "the list output must reach the log"


@pytest.mark.parametrize(
    "mode", ["unloaded", "wrongversion", "suffixversion", "prefixversion", "loaderror", "disabled", "noinstall"]
)
def test_every_not_loaded_answer_fails_the_install(tmp_path: pathlib.Path, mode: str) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    r = _run(tmp_path, f'cc_plugin_install "{root}" conexus nexus-plugins; echo "rc=$? id=[$CC_PLUGIN_ID] dbg=[$CC_DEBUG_FILE]"', mode=mode)
    assert "rc=1 id=[] dbg=[]" in r.stdout, f"{mode}: {r.stdout}{r.stderr}"


def test_a_plugin_without_a_version_fails(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "src" / "conexus"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}")
    r = _run(tmp_path, f'cc_plugin_install "{root}" conexus nexus-plugins; echo "rc=$?"')
    assert "rc=1" in r.stdout


def test_install_replaces_prior_plugin_state_and_teardown_empties_it(tmp_path: pathlib.Path) -> None:
    a = _plugin(tmp_path / "src" / "a", name="conexus", version="1.0.0")
    b = _plugin(tmp_path / "src" / "b", name="sn", version="2.0.0")
    body = (
        f'cc_plugin_install "{a}" conexus nexus-plugins || echo A_FAILED\n'
        f'cc_plugin_install "{b}" sn nexus-plugins || echo B_FAILED\n'
        'python3 -c \'import json,os;print(sorted(json.load(open(os.environ["TEST_HOME"]+"/.claude/plugins/installed_plugins.json"))["plugins"]))\'\n'
        'cc_plugin_uninstall_all\n'
        'python3 -c \'import json,os;print(json.load(open(os.environ["TEST_HOME"]+"/.claude/plugins/installed_plugins.json")))\'\n'
        'echo "after: id=[$CC_PLUGIN_ID] dbg=[$CC_DEBUG_FILE]"'
    )
    r = _run(tmp_path, body)
    assert "A_FAILED" not in r.stdout and "B_FAILED" not in r.stdout, r.stdout + r.stderr
    assert "['sn@nexus-plugins']" in r.stdout
    assert "{'version': 2, 'plugins': {}}" in r.stdout
    assert "after: id=[] dbg=[]" in r.stdout


def test_the_prepare_callback_runs_on_the_copy_before_the_install(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    body = (
        'mark() { echo prepared > "$1/hooks/PREPARED"; }\n'
        f'cc_plugin_install "{root}" conexus nexus-plugins mark; echo "rc=$?"\n'
        'ls "$CC_PLUGIN_INSTALLED_ROOT/hooks"'
    )
    r = _run(tmp_path, body)
    assert "rc=0" in r.stdout and "PREPARED" in r.stdout, r.stdout + r.stderr
    assert not (root / "hooks" / "PREPARED").exists(), "the source tree must stay untouched"


def test_evidence_is_kept_when_a_directory_is_mounted(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    ev = tmp_path / "evidence"
    body = f'cc_plugin_install "{root}" conexus nexus-plugins; echo x > "$CC_DEBUG_FILE"; cc_plugin_uninstall_all'
    r = _run(tmp_path, body, CC_VAL_EVIDENCE_DIR=str(ev))
    assert r.returncode == 0, r.stderr
    kept = {p.name for p in (ev / "t").iterdir()}
    assert {"plugin-list.txt", "plugin-install.log", "cc-debug-conexus.log", "installed_plugins.json"} <= kept


# --- the debug-log matchers ---------------------------------------------------

_LOG_OK = (
    "2026-10-17T21:31:37.343Z [DEBUG] Hooks: SubagentStart:general-purpose [uv tool run python /x/hooks/scripts/subagent_start.py]"
    " (plugin sn@nexus-plugins) finished with status 0\n"
)


def _hook_ran(tmp_path: pathlib.Path, log: str | None, call: str) -> bool:
    dbg = tmp_path / "dbg.log"
    dbg.unlink(missing_ok=True)
    if log is not None:
        dbg.write_text(log)
    r = _run(tmp_path, f'CC_DEBUG_FILE="{dbg}"; {call} && echo YES || echo NO')
    assert r.stdout.strip() in {"YES", "NO"}, r.stdout + r.stderr
    return r.stdout.strip() == "YES"


def test_cc_hook_ran_reads_the_debug_log(tmp_path: pathlib.Path) -> None:
    assert _hook_ran(tmp_path, _LOG_OK, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"')
    assert _hook_ran(tmp_path, _LOG_OK, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins" "subagent_start.py"')


@pytest.mark.parametrize(
    "log,call",
    [
        (_LOG_OK, 'cc_hook_ran "SubagentStart" "conexus@nexus-plugins"'),
        (_LOG_OK, 'cc_hook_ran "PreToolUse" "sn@nexus-plugins"'),
        (_LOG_OK, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins" "other_script.py"'),
        (_LOG_OK.replace("status 0", "status 1"), 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"'),
        ("", 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"'),
        (None, 'cc_hook_ran "SubagentStart" "sn@nexus-plugins"'),
    ],
)
def test_cc_hook_ran_does_not_match_what_did_not_run(tmp_path: pathlib.Path, log: str | None, call: str) -> None:
    assert not _hook_ran(tmp_path, log, call)


_MCP_CALL = "2026-10-17T21:30:28.203Z [DEBUG] Hooks: mcp_tool calling plugin:conexus:nexus/hook_subagent_start with 4 arg(s)\n"
_MCP_ERR = "2026-10-17T21:21:26.714Z [ERROR] Hooks: mcp_tool hook error: Unknown tool: 'hook_subagent_start'. Nexus is not installed.\n"


def test_cc_mcp_hook_ran_needs_the_call_and_no_error(tmp_path: pathlib.Path) -> None:
    ok = 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"'
    assert _hook_ran(tmp_path, _MCP_CALL, ok)
    assert not _hook_ran(tmp_path, _MCP_CALL + _MCP_ERR, ok)
    assert not _hook_ran(tmp_path, _MCP_ERR, ok)
    assert not _hook_ran(tmp_path, _MCP_CALL, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_other"')
    assert not _hook_ran(tmp_path, None, ok)


def test_cc_mcp_hook_ran_matches_only_the_named_plugins_call(tmp_path: pathlib.Path) -> None:
    """Another plugin's server exposing the same tool name must not count."""
    other = "2026-10-17T21:30:28.203Z [DEBUG] Hooks: mcp_tool calling plugin:sn:nexus/hook_subagent_start with 4 arg(s)\n"
    assert not _hook_ran(tmp_path, other, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"')
    assert _hook_ran(tmp_path, other, 'cc_mcp_hook_ran "sn@nexus-plugins" "hook_subagent_start"')
    # the plugin name is matched whole, not as a prefix of a longer plugin name
    longer = "2026-10-17T21:30:28.203Z [DEBUG] Hooks: mcp_tool calling plugin:conexus-extra:nexus/hook_subagent_start with 1 arg(s)\n"
    assert not _hook_ran(tmp_path, longer, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"')
    # both present: the named plugin's call is enough
    assert _hook_ran(tmp_path, other + _MCP_CALL, 'cc_mcp_hook_ran "conexus@nexus-plugins" "hook_subagent_start"')


_CTX_OK = (
    "2026-10-17T21:31:37.346Z [DEBUG] Hook SubagentStart (uv tool run --directory ${CLAUDE_PLUGIN_ROOT} --no-config "
    "--quiet --python >=3.12 python ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/subagent_start.py) provided additionalContext (3462 chars)\n"
)


def test_cc_hook_context_injected_needs_accepted_nonempty_context(tmp_path: pathlib.Path) -> None:
    sn = "sn@nexus-plugins"
    call = f'cc_hook_context_injected "SubagentStart" "subagent_start.py" "{sn}"'
    log = _LOG_OK + _CTX_OK
    assert _hook_ran(tmp_path, log, call)
    assert _hook_ran(
        tmp_path,
        "Hook SubagentStart (plugin:conexus:nexus/hook_subagent_start) provided additionalContext (4388 chars)\n",
        'cc_hook_context_injected "SubagentStart" "hook_subagent_start" "conexus@nexus-plugins"',
    )
    assert not _hook_ran(tmp_path, _LOG_OK + _CTX_OK.replace("(3462 chars)", "(0 chars)"), call)
    assert not _hook_ran(tmp_path, log, f'cc_hook_context_injected "SubagentStart" "other_script.py" "{sn}"')
    assert not _hook_ran(tmp_path, log, f'cc_hook_context_injected "SessionStart" "subagent_start.py" "{sn}"')
    assert not _hook_ran(tmp_path, _LOG_OK, call), "a hook that finished but delivered nothing is not injection"
    assert not _hook_ran(tmp_path, None, call)


def test_cc_hook_context_injected_is_scoped_to_the_plugin(tmp_path: pathlib.Path) -> None:
    """The line a command hook's context is logged on names no plugin, so the
    plugin comes from the `(plugin <id>) finished` line of the same script, and an
    mcp_tool hook's line names its plugin whole (nexus-qsmc5)."""
    # sn's hook finished and delivered: sn passes, conexus (which ran nothing here) does not
    log = _LOG_OK + _CTX_OK
    assert _hook_ran(tmp_path, log, 'cc_hook_context_injected "SubagentStart" "subagent_start.py" "sn@nexus-plugins"')
    assert not _hook_ran(tmp_path, log, 'cc_hook_context_injected "SubagentStart" "subagent_start.py" "conexus@nexus-plugins"')
    # the context line exists but the plugin's own hook never finished: another source delivered it
    assert not _hook_ran(tmp_path, _CTX_OK, 'cc_hook_context_injected "SubagentStart" "subagent_start.py" "sn@nexus-plugins"')
    assert not _hook_ran(tmp_path, _LOG_OK.replace("status 0", "status 1") + _CTX_OK,
                         'cc_hook_context_injected "SubagentStart" "subagent_start.py" "sn@nexus-plugins"')
    # an mcp_tool line is matched by the plugin it names, whole
    mcp = "Hook SubagentStart (plugin:conexus:nexus/hook_subagent_start) provided additionalContext (4388 chars)\n"
    ok = 'cc_hook_context_injected "SubagentStart" "hook_subagent_start" "{}"'
    assert _hook_ran(tmp_path, mcp, ok.format("conexus@nexus-plugins"))
    assert not _hook_ran(tmp_path, mcp, ok.format("sn@nexus-plugins"))
    assert not _hook_ran(tmp_path, mcp, ok.format("conexus-extra@nexus-plugins"))
    assert not _hook_ran(tmp_path, mcp.replace("plugin:conexus:", "plugin:conexus-extra:"), ok.format("conexus@nexus-plugins"))
    # another plugin's mcp line does not stand in for a command hook of the same script name
    other = "Hook SubagentStart (plugin:conexus:nexus/subagent_start.py) provided additionalContext (99 chars)\n"
    assert not _hook_ran(tmp_path, _LOG_OK + other, 'cc_hook_context_injected "SubagentStart" "subagent_start.py" "sn@nexus-plugins"')
    # the plugin id is required
    assert not _hook_ran(tmp_path, log, 'cc_hook_context_injected "SubagentStart" "subagent_start.py"')


def _chars(tmp_path: pathlib.Path, log: str, call: str) -> str:
    dbg = tmp_path / "dbgc.log"
    dbg.write_text(log)
    r = _run(tmp_path, f'CC_DEBUG_FILE="{dbg}"; {call}; echo "rc=$?"')
    return r.stdout.strip()


def test_cc_hook_context_chars_and_the_whole_delivery_gate(tmp_path: pathlib.Path) -> None:
    sn = '"SubagentStart" "subagent_start.py" "sn@nexus-plugins"'
    assert _chars(tmp_path, _LOG_OK + _CTX_OK, f"cc_hook_context_chars {sn}") == "3462\nrc=0"
    assert _chars(tmp_path, _LOG_OK, f"cc_hook_context_chars {sn}") == "rc=1"
    # scenario 18's gate: the delivered size must be one of the sizes of the WHOLE text
    whole = f"cc_hook_context_is_whole {sn} 3462 5414"
    assert _chars(tmp_path, _LOG_OK + _CTX_OK, whole) == "rc=0"
    partial = _LOG_OK + _CTX_OK.replace("(3462 chars)", "(2713 chars)")   # one section only
    assert _chars(tmp_path, partial, whole) == "rc=1", "a partial delivery must not pass the whole-delivery gate"
    assert _chars(tmp_path, _LOG_OK + _CTX_OK.replace("(3462 chars)", "(749 chars)"), whole) == "rc=1"
    assert _chars(tmp_path, _LOG_OK + _CTX_OK.replace("(3462 chars)", "(5414 chars)"), whole) == "rc=0"
    assert _chars(tmp_path, _LOG_OK, whole) == "rc=1"


# --- the subagent's own reply ---------------------------------------------------


def _agent_transcripts(tmp_path: pathlib.Path) -> pathlib.Path:
    proj = tmp_path / "home" / ".claude" / "projects" / "-home-x" / "sess"
    (proj / "subagents").mkdir(parents=True)
    def line(role: str, blocks: list[dict]) -> str:
        return json.dumps({"type": role, "message": {"role": role, "content": blocks}}) + "\n"
    (proj / "subagents" / "agent-abc.jsonl").write_text(
        line("user", [{"type": "text", "text": "PROMPT-ECHO-12K7"}])
        + line("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "TOOLCALL-12K7"}}])
        + "not json\n"
        + line("assistant", [{"type": "text", "text": "CLAIM-12K7-0123456789abcdef"}])
    )
    # the main session's own transcript: the main agent's paraphrase, not evidence
    (proj.parent / "main.jsonl").write_text(line("assistant", [{"type": "text", "text": "MAIN-PARAPHRASE-12K7"}]))
    return proj


def test_cc_subagent_replies_returns_only_the_subagents_assistant_text(tmp_path: pathlib.Path) -> None:
    _agent_transcripts(tmp_path)
    r = _run(tmp_path, "cc_subagent_replies")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "CLAIM-12K7-0123456789abcdef", r.stdout + r.stderr


def test_cc_wait_subagent_reply_finds_the_token_or_times_out(tmp_path: pathlib.Path) -> None:
    _agent_transcripts(tmp_path)
    r = _run(tmp_path, 'cc_wait_subagent_reply "CLAIM-12K7-[0-9a-f]{8,}" 2; echo "rc=$?"')
    assert "rc=0" in r.stdout, r.stdout + r.stderr
    r = _run(tmp_path, 'cc_wait_subagent_reply "MAIN-PARAPHRASE-12K7" 2; echo "rc=$?"')
    assert "rc=1" in r.stdout and "TIMEOUT" in r.stdout, "the main transcript must not count as the subagent's reply"
    r = _run(tmp_path, 'cc_wait_subagent_reply "PROMPT-ECHO-12K7|TOOLCALL-12K7" 2; echo "rc=$?"')
    assert "rc=1" in r.stdout, "a user turn or tool call is not a reply"


# --- evidence never carries a credential ---------------------------------------


def test_evidence_refuses_files_holding_a_credential_shaped_string(tmp_path: pathlib.Path) -> None:
    root = _plugin(tmp_path / "src" / "conexus")
    ev = tmp_path / "evidence"
    home = tmp_path / "home"
    body = (
        f'cc_plugin_install "{root}" conexus nexus-plugins || echo INSTALL_FAILED\n'
        'echo "debug line with sk-ant-oat01-FAKEFAKE inside" > "$CC_DEBUG_FILE"\n'
        'mkdir -p "$TEST_HOME/.claude/projects/p"\n'
        'echo "{\\"clean\\": true}" > "$TEST_HOME/.claude/projects/p/clean.jsonl"\n'
        'echo "{\\"env\\": \\"sk-ant-api03-FAKEFAKE\\"}" > "$TEST_HOME/.claude/projects/p/dirty.jsonl"\n'
        'cc_plugin_uninstall_all\n'
    )
    r = _run(tmp_path, body, CC_VAL_EVIDENCE_DIR=str(ev))
    assert "INSTALL_FAILED" not in r.stdout, r.stdout + r.stderr
    kept = {p.name for p in (ev / "t").iterdir()}
    assert "cc-debug-conexus.log" not in kept, "a debug log holding sk-ant- must not be copied"
    assert "dirty.jsonl" not in kept
    assert {"clean.jsonl", "plugin-list.txt", "plugin-install.log", "installed_plugins.json"} <= kept
    assert "refused to keep cc-debug-conexus.log" in r.stderr and "refused to keep dirty.jsonl" in r.stderr
    assert "FAKEFAKE" not in r.stderr and "FAKEFAKE" not in r.stdout, "the refusal must not echo the content"
    for f in (ev / "t").iterdir():
        assert "sk-ant-" not in f.read_text(), f"{f.name} carries a credential-shaped string"
    assert home.exists()


# --- scenario 14: completion wait, spawn-based verdict --------------------------

S14 = CCV / "scenarios" / "14_inline_mcpservers_variations.sh"


def _14b_verdict(tmp_path: pathlib.Path, ctrl: int, agent_ran: int, spawned: int, mcp_in_inv: int = 0, log_called: int = 0) -> str:
    """Run scenario 14's own 14b verdict block with the given measurements."""
    text = S14.read_text(encoding="utf-8")
    start = text.index("if [[ $agent_ran -ne 1 ]]; then\n    skip \"14b:")
    end = text.index("\nfi\n", start) + len("\nfi\n")
    block = text[start:end]
    script = (
        'pass() { echo "PASS:$*"; }\nskip() { echo "SKIP:$*"; }\nfail() { echo "FAIL:$*"; }\n'
        f"CTRL_14A_LOADED={ctrl}\nagent_ran={agent_ran}\nstub_spawned_14b={spawned}\n"
        f"mcp_in_inv={mcp_in_inv}\nlog_called={log_called}\n" + block
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().split(":", 1)[0]


def test_14b_verdict_does_not_depend_on_the_deferred_tool_schema(tmp_path: pathlib.Path) -> None:
    # Spawned = the server loaded for the plugin agent, whatever the agent listed or called
    for inv in (0, 1):
        for called in (0, 1):
            for ctrl in (0, 1):
                assert _14b_verdict(tmp_path, ctrl, 1, 1, inv, called) == "PASS", (inv, called, ctrl)
    # Not spawned, with the positive control: the reading is "does not load", whatever the agent listed or called
    for inv in (0, 1):
        for called in (0, 1):
            assert _14b_verdict(tmp_path, 1, 1, 0, inv, called) == "PASS", (inv, called)
    # Not spawned and no positive control: a stub that cannot start is not a finding
    assert _14b_verdict(tmp_path, 0, 1, 0) == "SKIP"
    # the agent never ran: not a verdict
    assert _14b_verdict(tmp_path, 1, 0, 0) == "SKIP"
    assert _14b_verdict(tmp_path, 1, 0, 1) == "SKIP"


def test_14a_is_the_only_thing_that_sets_the_control() -> None:
    code = _code(S14)
    assert code.count("CTRL_14A_LOADED=1") == 1
    assert "CTRL_14A_LOADED=0" in code
    assert code.index("CTRL_14A_LOADED=1") < code.index('scenario "14b ')


def test_14_waits_for_each_agent_to_finish_before_reading_its_forensics() -> None:
    """The fixed claude_wait returned at once (the main agent does not spin while a
    subagent works) and the session was ended while the agent was mid-task: the
    agents' transcripts ended "[Request interrupted by user]" (nexus-qsmc5)."""
    code = _code(S14)
    for token in ("14A_DONE", "14B_DONE", "14C_DONE"):
        assert f'wait_agent_done "{token}"' in code, token
        assert f"finish by\nreplying {token}" in code or f"finish by replying {token}" in code.replace("\n", " "), token
    assert "claude_wait 90" not in code, "a fixed wait after dispatch reads the files before the agent is done"
    assert "cc_wait_subagent_reply" in _bash_snippet(code, "wait_agent_done")


def _bash_snippet(code: str, name: str) -> str:
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n.*?^\}}\n", code, re.S | re.M)
    assert m, name
    return m.group(0)


def test_14_agents_load_the_deferred_tool_schema_first() -> None:
    """14a and 14c lacked the ToolSearch warmup (nexus-qsmc5)."""
    code = S14.read_text(encoding="utf-8")
    for agent in ("agent14a", "agent14b", "agent14c"):
        m = re.search(rf"name: {agent}\n.*?\n---\n(.*?)\nEOF", code, re.S)
        assert m, agent
        assert "ToolSearch" in m.group(1) and "select:mcp__stub__record" in m.group(1), f"{agent} must load the schema first"
    # and 14c's parent warms the project server's schemas, as 05 and 07 do
    c14 = code[code.index('scenario "14c '):]
    assert c14.index("List your available tools whose name starts with mcp__") < c14.index("Use Task to dispatch agent14c")


def test_14_reads_the_payload_case_insensitively_and_keeps_each_part_apart(tmp_path: pathlib.Path) -> None:
    """A live run had agent14c call the stub with payload "14C-PROOF" for the "14c-PROOF"
    it was told; the exact-case grep read that landed call as no call and 14c failed on
    two of four runs (nexus-qsmc5)."""
    code = _code(S14)
    for x in "abc":
        assert f'grep -qi "14{x}-PROOF" "$STUB_LOG"' in code, x
        assert f'grep -q "14{x}-PROOF"' not in code, x
    # the match the scenario uses finds the model's upper-cased payload
    log = tmp_path / "stub.log"
    log.write_text('{"tool": "record", "payload": "14C-PROOF", "ts": 1}\n')
    r = subprocess.run(["bash", "-c", f'grep -qi "14c-PROOF" "{log}" && echo HIT || echo MISS'], capture_output=True, text=True)
    assert r.stdout.strip() == "HIT"
    # each part starts with no transcripts of the parts before it, so the per-part report is that part's
    assert code.count('rm -rf "$TEST_HOME/.claude/projects"') == 3
    for label in ("14a", "14b", "14c"):
        assert f'report_agents "{label}"' in code, label
    assert "cc_keep_evidence" in _bash_snippet(code, "report_agents")


def test_stub_spawned_reads_the_stubs_own_startup_marker(tmp_path: pathlib.Path) -> None:
    fn = _bash_snippet(_code(S14), "stub_spawned")
    log = tmp_path / "stub.log"

    def spawned(content: str | None) -> bool:
        log.unlink(missing_ok=True)
        if content is not None:
            log.write_text(content)
        r = subprocess.run(["bash", "-c", fn + '\nSTUB_LOG="' + str(log) + '"; stub_spawned && echo YES || echo NO'], capture_output=True, text=True)
        return r.stdout.strip() == "YES"

    assert spawned('{"event": "process_launched", "python": "/x", "name": "stub", "ts": 1}\n')
    assert spawned('{"event": "process_launched", "python": "/x"}\n{"event": "mcp_imported_ok"}\n')
    assert not spawned("")
    assert not spawned(None)
    assert not spawned('{"tool": "record", "payload": "14b-PROOF"}\n'), "a tool call is not a spawn marker"
    # the marker is the one fixtures/stub_server.py writes
    assert '"process_launched"' in (CCV / "fixtures" / "stub_server.py").read_text(encoding="utf-8")


# --- no probe phrase is written whole in the prompt that asks for it ----------------

#: What each injection-probing scenario judges the reply on, besides the reply
#: tokens (found from the scenario's own poll_for/grep/cc_wait_* patterns). None may
#: appear whole in any claude_prompt of the scenario: the pane shows the prompt and
#: the subagent sees it, so a probe written into it is satisfiable without any
#: injection (nexus-spwp2 review). Keys are _num() of the scenario file (no leading zero).
PROBES = {
    "1": ["SUB-PLAIN-MARKER-K7Q3", "NO-MARKER", "FOUND-01S", "NONE-01S"],
    "2": ["SUB-JSON-MARKER-X9P2", "NO-MARKER", "FOUND-02S", "NONE-02S"],
    "3": ["SUB-MCP-MARKER-W7K4", "NO-MARKER", "FOUND-03S", "NONE-03S"],
    "4": ["SESS-PLAIN-MARKER-INTERACTIVE-Q9X", "NO-MARKER", "FOUND-04S", "NONE-04S"],
    "11": ["PARENT-NO-STUB"],
    "12": ["Claimant id:", "check before duplicating sibling work"],
    "13": ["UNIQUE-PROJECT-A-MARKER-KQX73", "UNIQUE-PLUGIN-B-MARKER-WYZ91", "UNIQUE-CONTROL-C-MARKER-RJF44"],
    # 15 builds its prompt from variables; the markers are 15's probe_inject arguments
    "15": ["$marker", "${marker}", "STDOUT-15A-MARKER-Q9X", "STDOUT-15B-MARKER-T7K", "STDOUT-15C-MARKER-W3J", "STDOUT-15D-MARKER-R2P"],
    "17": ["NESTED-AGENT-OK", "NESTED-SPAWN-BLOCKED", "FORK-PARENT-OK"],
    "18": ["project-from-cwd", "fetch current docs"],
    # slash-command probes: the command body becomes the prompt the model quotes from
    "21": ["OUTFENCE-END-42"],
    "24": ["Source Locations", "Project type:"],
    "25": ["Target file:", "Working state"],
}
#: Scenarios whose write_skill/write_command bodies are prompts too: the fork's own
#: Task call prompt is printed on the pane (17), and a command body is what the model
#: reads and quotes (21, 24, 25).
BODY_SCANNED = {"17", "21", "24", "25"}
#: Tokens a scenario greps for that are NOT pane replies: a payload compared in the
#: stub's own log, which a prompt cannot fake. Scenario 11 passes PARENT-CALL-PROBE
#: to the tool call and reads STUB_LOG for it.
SIDE_EFFECT_TOKENS = {"11": {"PARENT-CALL-PROBE"}}
_PROMPT = re.compile(r'claude_prompt\s+"((?:[^"\\]|\\.)*)"')
_BODY = re.compile(r"write_(?:skill|command) \"[^\"]+\" /dev/stdin <<'EOF'\n(.*?)\nEOF", re.S)
_PATTERN_ARG = re.compile(r'(?:poll_for|grep\s+-[A-Za-z]*|cc_wait_(?:subagent|session)_reply)\s+"([^"]+)"')


def _scenario(num: str) -> pathlib.Path:
    return next(p for p in SCENARIOS if _num(p) == num)


def _prompts(path: pathlib.Path) -> list[str]:
    out = _PROMPT.findall(_code(path))
    if _num(path) in BODY_SCANNED:
        out += _BODY.findall(path.read_text(encoding="utf-8"))
    return out


def _reply_tokens(path: pathlib.Path) -> set[str]:
    out: set[str] = set()
    for pat in _PATTERN_ARG.findall(_code(path)):
        for alt in re.split(r"\||\.\*", pat):
            if re.fullmatch(r"[A-Za-z0-9_-]{6,}", alt):
                out.add(alt)
    return out


def _whole_in_prompts(prompts: list[str], probes: list[str] | set[str]) -> list[tuple[str, str]]:
    squashed = [(p, " ".join(p.split())) for p in prompts]
    return [(probe, p[:60]) for probe in sorted(probes) for p, sq in squashed if probe in p or probe in sq]


@pytest.mark.parametrize("num", sorted(PROBES, key=int))
def test_no_prompt_contains_its_own_probe_phrase_whole(num: str) -> None:
    path = _scenario(num)
    prompts = _prompts(path)
    tokens = _reply_tokens(path)
    assert len(prompts) >= 1, f"scenario {num}: found no claude_prompt, so the census examined nothing"
    tokens -= SIDE_EFFECT_TOKENS.get(num, set())
    assert len(set(PROBES[num]) | tokens) >= 1, f"scenario {num}: found {sorted(tokens)} reply tokens and {PROBES[num]} probes"
    hits = _whole_in_prompts(prompts, set(PROBES[num]) | tokens)
    assert not hits, f"scenario {num} writes a probe whole in its own prompt: {hits}"


def test_the_prompt_census_counts() -> None:
    """Non-vacuity: how many prompts and tokens the scenarios hand the lint."""
    total_prompts = sum(len(_prompts(_scenario(n))) for n in PROBES)
    total_tokens = sum(len(_reply_tokens(_scenario(n))) for n in PROBES)
    assert len(PROBES) >= 13, len(PROBES)
    assert total_prompts >= 20, total_prompts
    assert total_tokens >= 20, total_tokens
    # the scenarios the bead named are covered
    assert {"1", "2", "3", "4", "15", "17"} <= set(PROBES)


def test_every_scenario_claude_prompt_probe_is_either_linted_or_a_side_effect_check() -> None:
    """Census of the rest: a scenario NOT in PROBES must not grep the pane for a
    token that its own claude_prompt writes whole (the files-and-logs scenarios
    check side effects, which a prompt cannot fake)."""
    unlinted = []
    for path in SCENARIOS:
        if _num(path) in PROBES:
            continue
        prompts = _PROMPT.findall(_code(path))
        for tok in _reply_tokens(path):
            if any(tok in pr for pr in prompts):
                unlinted.append((path.name, tok))
    # known and checked by hand: the marker is a file the prompted command writes (32), a payload
    # compared in the stub's own log (11's PARENT-CALL-PROBE, 29/30 tool names), not a pane reply
    allowed = {("11_agent_inline_mcpservers.sh", "PARENT-CALL-PROBE"), ("29_sn_worktree_guard.sh", "replace_in_files"),
               ("29_sn_worktree_guard.sh", "zzz-marker"), ("32_pretooluse_no_blanket_allow.sh", "scenario32-nexus-452oy-marker")}
    assert set(unlinted) <= allowed, f"a scenario greps for a token its own prompt contains: {sorted(set(unlinted) - allowed)}"


def test_the_prompt_lint_catches_a_whole_probe() -> None:
    prompts = ['Does your context contain fetch  current docs? reply FOUND-13Z', 'ask SERENA-OK-9F2K']
    hits = _whole_in_prompts(prompts, ["fetch current docs", "SERENA-OK-9F2K", "NOT-THERE"])
    assert {h[0] for h in hits} == {"fetch current docs", "SERENA-OK-9F2K"}
    assert not _whole_in_prompts(["the words fetch current followed by one space and the word docs"], ["fetch current docs"])


def test_the_prompt_lint_catches_the_shapes_the_old_scenarios_had() -> None:
    # 01: the reply token written whole in the prompt
    assert _whole_in_prompts(["Quote the line. If none, reply NO-MARKER."], PROBES["1"])
    # 15: the marker variable interpolated whole
    assert _whole_in_prompts(['Is the literal text $marker there? Reply YES with the marker, or NO.'], PROBES["15"])
    # 17: the nested agent's token written whole in the skill body the fork prompt comes from
    assert _whole_in_prompts(["Prompt: 'Reply with the literal token NESTED-AGENT-OK.'"], PROBES["17"])
    # 11 / 21 / 24 / 25
    assert _whole_in_prompts(["reply with the literal token PARENT-NO-STUB."], PROBES["11"])
    assert _whole_in_prompts(["whether the marker OUTFENCE-END-42 and the fenced block appear"], PROBES["21"])
    assert _whole_in_prompts(['whether the "### Source Locations" heading and the "**Project type:**" list'], PROBES["24"])
    # and the new wordings are clean
    assert not _whole_in_prompts(["reply with the word NESTED immediately followed by -AGENT-OK-17 (no space)"], PROBES["17"])
    assert not _whole_in_prompts(["the word FOUND immediately followed by -01S (no space)"], PROBES["1"])


def _run_prompt_text(path: pathlib.Path) -> list[str]:
    return _prompts(path)


def test_scenario_15_builds_its_prompt_from_pieces(tmp_path: pathlib.Path) -> None:
    """Run 15's probe_inject with stubs and look at the prompt it would type."""
    text = (CCV / "scenarios" / "15_stdout_injection_thresholds.sh").read_text(encoding="utf-8")
    fn_text = text[text.index("probe_inject() {"):text.index("\n# ── 15a")]   # the settings heredoc inside has a col-0 "}"
    prompt_file = tmp_path / "prompt.txt"
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)

    def run(replies: str) -> tuple[str, str]:
        script = (
            f'TEST_HOME="{home}"\npass() {{ echo "PASS:$*"; }}\nfail() {{ echo "FAIL:$*"; }}\n'
            f'claude_start() {{ :; }}\nclaude_exit() {{ :; }}\nclaude_prompt() {{ printf "%s" "$1" > "{prompt_file}"; }}\n'
            f'cc_wait_subagent_reply() {{ return 0; }}\ncc_subagent_replies() {{ printf "%s\\n" {replies!r}; }}\n'
            + fn_text + '\nprobe_inject "15a" "echo hi" "STDOUT-15A-MARKER-Q9X"\necho "VISIBLE=$result_visible"\n'
        )
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout, prompt_file.read_text()

    out, prompt = run("FOUND-15aS STDOUT-15A-MARKER-Q9X")
    assert "STDOUT-15A-MARKER-Q9X" not in prompt, "the marker is in the prompt whole"
    assert "STDOUT-15A-MARKER-" in prompt and "Q9X" in prompt and "FOUND-15aS" not in prompt and "NONE-15aS" not in prompt
    assert "VISIBLE=1" in out and "PASS:" in out
    out, _ = run("NONE-15aS")
    assert "VISIBLE=0" in out and "PASS:" in out
    # a FOUND without the whole marker, and no reply at all, are not readings
    out, _ = run("FOUND-15aS something else")
    assert "VISIBLE=-1" in out and "FAIL:" in out
    out, _ = run("")
    assert "VISIBLE=-1" in out and "FAIL:" in out
    # the same marker under ANOTHER probe's tag is not this probe's reply
    out, _ = run("FOUND-15bS STDOUT-15A-MARKER-Q9X")
    assert "VISIBLE=-1" in out


def _block(path: pathlib.Path, start: str, end: str) -> str:
    text = path.read_text(encoding="utf-8")
    a = text.index(start)
    return text[a:text.index(end, a)]


def _shargs(text: str) -> str:
    """The lines of text as separate single-quoted bash arguments."""
    return " ".join(repr(ln) for ln in text.split("\n"))


def _verdict_run(block: str, replies: str, extra: str = "") -> str:
    script = (
        'set -euo pipefail\npass() { echo "PASS:$*"; }\nfail() { echo "FAIL:$*"; }\n'
        "cc_report_context_lines() { :; }\ncc_keep_evidence() { :; }\n"
        f"cc_subagent_replies() {{ printf '%s\\n' {_shargs(replies)}; }}\ncc_session_replies() {{ printf '%s\\n' {_shargs(replies)}; }}\n"
        + extra + block + '\necho REACHED_END\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "REACHED_END" in r.stdout, f"the verdict block died before its end: {r.stdout}{r.stderr}"
    return next(ln for ln in r.stdout.splitlines() if ln.startswith(("PASS:", "FAIL:")))


@pytest.mark.parametrize(
    "num,var,marker,tag",
    [("01", "REPLIES01", "SUB-PLAIN-MARKER-K7Q3", "01S"), ("02", "REPLIES02", "SUB-JSON-MARKER-X9P2", "02S"),
     ("03", "REPLIES03", "SUB-MCP-MARKER-W7K4", "03S"), ("04", "REPLIES04", "SESS-PLAIN-MARKER-INTERACTIVE-Q9X", "04S")],
)
def test_scenarios_01_to_04_judge_the_reply_not_the_prompt(num: str, var: str, marker: str, tag: str) -> None:
    path = next(p for p in SCENARIOS if p.name.startswith(num + "_"))
    code = _code(path)
    assert "capture -300" not in code, "the pane shows the prompt and the hook's own output"
    block = _block(path, f'{var}="$(cc_', "claude_exit")
    found = _verdict_run(block, f"FOUND-{tag} {marker}")
    assert found.startswith("PASS:")
    # the pane-matching trap: the model quotes only the prompt's prefix, or only the token
    assert _verdict_run(block, f"FOUND-{tag} {marker.rsplit('-', 1)[0]}-").startswith("FAIL:")
    assert _verdict_run(block, f"FOUND-{tag}").startswith("FAIL:")
    assert _verdict_run(block, "unrelated text").startswith("FAIL:")
    none = _verdict_run(block, f"NONE-{tag}")
    if num == "01":
        assert none.startswith("PASS:") and "did NOT inject" in none   # the -p finding is a legitimate reading
        assert "DID inject" in found
    else:
        assert none.startswith("FAIL:")
    # the verdict reads the right transcripts
    assert ("cc_session_replies" if num == "04" else "cc_subagent_replies") in code


def test_scenario_17_requires_a_subagent_to_have_called_the_task_tool() -> None:
    path = CCV / "scenarios" / "17_fork_can_spawn_subagent.sh"
    block = _block(path, 'REPLIES17="$(cc_', "claude_exit")

    def run(replies: str, tools: str) -> str:
        return _verdict_run(block, replies, extra=f"cc_subagent_tool_uses() {{ printf '%s\\n' {_shargs(tools)}; }}\n")

    assert run("NESTED-AGENT-OK-17", "Skill\nTask").startswith("PASS:")
    assert run("NESTED-AGENT-OK-17", "Agent").startswith("PASS:")
    # the token without a dispatch is the pane trap again: not evidence of a spawn
    blocked = run("NESTED-AGENT-OK-17", "Bash")
    assert blocked.startswith("FAIL:") and "no subagent called the Task/Agent tool" in blocked
    assert run("NESTED-SPAWN-BLOCKED-17", "").startswith("PASS:")
    assert run("nothing", "Task").startswith("FAIL:")
    # a tool named like Task is not Task
    assert run("NESTED-AGENT-OK-17", "TaskList").startswith("FAIL:")


def test_scenario_17_tells_the_fork_to_load_the_deferred_task_tool_first() -> None:
    """The Task/Agent tool is deferred for a fork: one live run answered BLOCKED without
    loading it, the next loaded it with ToolSearch and spawned (nexus-qsmc5)."""
    text = (CCV / "scenarios" / "17_fork_can_spawn_subagent.sh").read_text(encoding="utf-8")
    body = _BODY.findall(text)[0]
    assert "ToolSearch" in body and "select:Agent" in body
    assert body.index("select:Agent") < body.index("Use the Task tool to dispatch"), "load it before using it"
    assert "still unavailable to you after step 0" in body, "BLOCKED only after the load was tried"


def test_scenario_11_reads_the_parents_own_text_for_its_denial_token() -> None:
    code = _code(CCV / "scenarios" / "11_agent_inline_mcpservers.sh")
    assert 'capture -60 | grep -q "PARENT-NO-STUB"' not in code
    assert "cc_session_replies" in code and "PARENT immediately followed by -NO-STUB" in code


def test_cc_subagent_tool_uses_lists_only_subagent_tool_calls(tmp_path: pathlib.Path) -> None:
    _agent_transcripts(tmp_path)
    r = _run(tmp_path, "cc_subagent_tool_uses")
    assert r.stdout.split() == ["Bash"], r.stdout + r.stderr   # the main transcript and user turns do not count


def test_cc_session_replies_returns_only_the_main_sessions_text(tmp_path: pathlib.Path) -> None:
    _agent_transcripts(tmp_path)
    r = _run(tmp_path, "cc_session_replies")
    assert r.stdout.strip() == "MAIN-PARAPHRASE-12K7", r.stdout + r.stderr
    r = _run(tmp_path, 'cc_wait_session_reply "MAIN-PARAPHRASE-12K7" 2; echo "rc=$?"')
    assert "rc=0" in r.stdout
    r = _run(tmp_path, 'cc_wait_session_reply "CLAIM-12K7" 2; echo "rc=$?"')
    assert "rc=1" in r.stdout and "TIMEOUT" in r.stdout, "a subagent's reply is not the main session's"


# --- run.sh says what untracked files it stages ----------------------------------


def test_run_sh_reports_the_untracked_files_it_stages(tmp_path: pathlib.Path) -> None:
    run_sh = CCV / "container" / "run.sh"
    fn = _bash_function(run_sh, "stage_report_untracked")
    repo = tmp_path / "repo"
    (repo / "src" / "nexus").mkdir(parents=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def sh(*args: str) -> None:
        subprocess.run(args, cwd=repo, check=True, capture_output=True, env=env)

    sh("git", "init", "-q")
    (repo / "src" / "nexus" / "tracked.py").write_text("x\n")
    (repo / ".gitignore").write_text("ignored.py\n")
    sh("git", "add", "src/nexus/tracked.py", ".gitignore")
    sh("git", "commit", "-q", "-m", "init")

    def report(*paths: str) -> str:
        r = subprocess.run(
            ["bash", "-c", f'ROOT="{repo}"\n{fn}\nstage_report_untracked "cli-src" {" ".join(paths)}'],
            capture_output=True, text=True, env=env,
        )
        assert r.returncode == 0, r.stderr
        return r.stdout

    assert "0 untracked files staged" in report("src")
    (repo / "src" / "nexus" / "stray.py").write_text("y\n")
    (repo / "src" / "nexus" / "ignored.py").write_text("z\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "outside.md").write_text("o\n")
    out = report("src")
    assert "1 untracked non-ignored file(s) staged" in out
    assert "src/nexus/stray.py" in out
    assert "tracked.py" not in out and "ignored.py" not in out and "outside.md" not in out
    # more than 20: the count is the whole count and the list is capped
    for i in range(25):
        (repo / "src" / f"extra{i:02d}.py").write_text("e\n")
    out = report("src")
    assert "26 untracked non-ignored file(s) staged" in out and "... and 6 more" in out
    # and the real script calls it after BOTH tar stages, for the paths each stages
    code = _code(run_sh)
    assert 'stage_report_untracked "harness" tests/cc-validation tests/e2e/lib.sh tests/e2e/lib conexus sn' in code
    assert 'stage_report_untracked "cli-src" src conexus/plans conexus/daemon dt/scripts README.md LICENSE pyproject.toml' in code
