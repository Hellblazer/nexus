# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-sby49.15 (RDR-259 step 4): cc-validation scenario 35 and its verdict ladder.

Scenario 35 runs a PreToolUse hook that answers allow (and no PermissionRequest hook) against an
`ask` rule for the same MCP tool, at user scope and at project scope, with a control leg that has no
rule. RDR-259 Decision 9 rests on the rule winning, so these tests pin that the ladder reads a
run's flags the way the scenario means: a control that does not run the tool, a hook that never
fired, or a prompt naming the wrong scope proves nothing and fails; "overridden" fails as a changed
answer on every build; "prompted" agrees with the baseline row of the version-answers table.

Nothing here starts Claude Code.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

from tests.test_cc_validation_plugin_install import CCV, REPO, RUNNER, SCENARIOS, _bash_function, _code

pytestmark = pytest.mark.lint

SCENARIO = next(p for p in SCENARIOS if p.name.startswith("35_"))


def _ladder(args: list[object], version: str = "2.1.332") -> list[str]:
    """Every PASS:/FAIL: line cc_ask_rule_verdict prints for these flags (it prints one per ask leg)."""
    deps = ["cc_note", "_cc_pass", "_cc_fail", "cc_version_check", "cc_ask_rule_verdict"]
    script = (
        f'REPO_ROOT="{REPO}"\npass() {{ echo "PASS:$*"; }}\nfail() {{ echo "FAIL:$*"; }}\n'
        + "\n".join(_bash_function(RUNNER, d) for d in deps)
        + f"\ncc_claude_version() {{ printf '%s' '{version}'; }}\ncc_ask_rule_verdict "
        + " ".join(f'"{a}"' for a in args) + "\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return [ln for ln in r.stdout.splitlines() if ln.startswith(("PASS:", "FAIL:"))]


# flags: hook_a ran_a prompt_a scope_a  hook_b ran_b prompt_b scope_b  hook_c ran_c prompt_c
PROMPTED = [1, 0, 1, "user", 1, 0, 1, "project", 1, 1, 0]


def test_both_rules_stopping_the_call_at_a_prompt_naming_their_scope_agree_with_the_baseline() -> None:
    lines = _ladder(PROMPTED)
    assert len(lines) == 2 and all(ln.startswith("PASS:") for ln in lines), lines
    assert "observed on Claude Code 2.1.332" in lines[0] and "35a" in lines[0] and "user-scope ask rule" in lines[0]
    assert "35b" in lines[1] and "project-scope ask rule" in lines[1]
    assert all("recorded for 2.1.332 on 2026-11-26" in ln for ln in lines), lines
    # the baseline holds for every build the table does not list, and for the container pin
    for version in ("2.1.302", "3.0.0"):
        assert all(ln.startswith("PASS:") for ln in _ladder(PROMPTED, version)), version


@pytest.mark.parametrize("leg", ["a", "b"])
def test_an_allow_that_ran_the_tool_through_the_ask_rule_fails_as_a_changed_answer(leg: str) -> None:
    flags = list(PROMPTED)
    base = 0 if leg == "a" else 4
    flags[base + 1] = 1   # tool_ran
    flags[base + 2] = 0   # no prompt
    flags[base + 3] = "none"
    lines = _ladder(flags)
    bad = [ln for ln in lines if ln.startswith("FAIL:")]
    assert len(bad) == 1 and "answer changed since 2.1.332" in bad[0] and f"35{leg}" in bad[0], lines
    # the other leg still reports on its own
    assert len([ln for ln in lines if ln.startswith("PASS:")]) == 1, lines


def test_a_control_that_did_not_run_the_tool_with_no_prompt_makes_every_leg_vacuous() -> None:
    for hc, rc, pc in ((0, 1, 0), (1, 0, 0), (1, 1, 1), (0, 0, 0)):
        flags = PROMPTED[:8] + [hc, rc, pc]
        lines = _ladder(flags)
        assert len(lines) == 1 and lines[0].startswith("FAIL:") and "CONTROL" in lines[0], (hc, rc, pc, lines)


@pytest.mark.parametrize("leg", ["a", "b"])
def test_a_hook_that_never_fired_proves_nothing(leg: str) -> None:
    flags = list(PROMPTED)
    flags[0 if leg == "a" else 4] = 0
    bad = [ln for ln in _ladder(flags) if ln.startswith("FAIL:")]
    assert len(bad) == 1 and "did not fire" in bad[0] and f"35{leg}" in bad[0], bad


@pytest.mark.parametrize("leg", ["a", "b"])
def test_a_leg_that_neither_ran_nor_prompted_proves_nothing(leg: str) -> None:
    flags = list(PROMPTED)
    base = 0 if leg == "a" else 4
    flags[base + 2] = 0
    flags[base + 3] = "none"
    bad = [ln for ln in _ladder(flags) if ln.startswith("FAIL:")]
    assert len(bad) == 1 and "nothing to conclude" in bad[0], bad


@pytest.mark.parametrize("leg,wrong", [("a", "project"), ("a", "other"), ("b", "user"), ("b", "other")])
def test_a_prompt_naming_the_wrong_scope_is_not_the_leg_s_rule(leg: str, wrong: str) -> None:
    flags = list(PROMPTED)
    flags[3 if leg == "a" else 7] = wrong
    bad = [ln for ln in _ladder(flags) if ln.startswith("FAIL:")]
    assert len(bad) == 1 and f"names {wrong} as the rule's scope" in bad[0], bad


def test_the_scenario_launches_enforced_answers_only_pretooluse_and_keeps_its_evidence() -> None:
    text = SCENARIO.read_text(encoding="utf-8")
    code = _code(SCENARIO)
    assert "claude_start_enforced" in code and "claude_start " not in code and "--dangerously-skip-permissions" not in code
    # a PreToolUse allow hook and no PermissionRequest hook: the answer under test
    assert '"PreToolUse"' in code and "PermissionRequest" not in code
    assert '"permissionDecision":"allow"' in code
    # three legs, a project that is not $TEST_HOME (so the two scopes are two files), the control last
    legs = re.findall(r'^_s35_leg "([^"]+)" ([01]) ([01])$', code, re.M)
    assert legs == [("35a-user-ask", "1", "0"), ("35b-project-ask", "0", "1"), ("35c-control", "0", "0")], legs
    assert 'S35_PROJ="$TEST_HOME/s35proj"' in code
    # every leg resets transcripts before its enforced start, keeps evidence, dismisses a pending prompt
    assert code.count("claude_start_enforced") == 1 and "cc_reset_transcripts" in code[: code.index("claude_start_enforced")]
    assert 'cc_keep_evidence "$label"' in code and "cc_dismiss_permission_prompt" in code and "cc_save_pane" in code
    assert "cc_ask_rule_verdict" in code
    assert "RDR-259" in text and "nexus-sby49.15" in text


def test_the_readme_and_the_table_name_scenario_35() -> None:
    readme = (CCV / "README.md").read_text(encoding="utf-8")
    assert re.search(r"^- \*\*35\*\*", readme, re.M), "README: a 35 entry among the observations that change with the version"
    assert "06, 07, 33, 34 and 35" in readme
    table = (REPO / "docs" / "tables" / "cc-version-answers.toml").read_text(encoding="utf-8")
    for row in ("35a::prompted", "35a::overridden", "35b::prompted", "35b::overridden"):
        assert f'id = "{row}"' in table, row
