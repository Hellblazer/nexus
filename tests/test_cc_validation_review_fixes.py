# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qsmc5 review fixes for the cc-validation harness, pinned by behaviour.

Each test here fails without the fix it names: the 21/24/25 verdicts read the session
transcript and not the model's account (and a no-op injection fails the same check), 06 and 07
report an observation for the running Claude Code version against a checked table, evidence
is kept for 05-07, transcripts are cleared between scenarios, the debug-log readers handle a
multi-line hook command, and run.sh names the tracked files it stages that differ from HEAD.

Nothing here starts Claude Code.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import pathlib
import re
import subprocess
import tomllib

import pytest

from nexus.tables.load import load_table
from nexus.tables.resolve import resolve

from tests.test_cc_validation_plugin_install import (
    CCV,
    REPO,
    RUNNER,
    SCENARIOS,
    _bash_function,
    _code,
    _flags_verdict,
    _helpers,
    _num,
    _run,
)

pytestmark = pytest.mark.lint

TABLE = REPO / "docs" / "tables" / "cc-version-answers.toml"
LIB = CCV / "lib"


def _scenario_file(prefix: str) -> pathlib.Path:
    return next(p for p in SCENARIOS if p.name.startswith(prefix))


# --- the user-turn reader (21/24/25 read what was SENT to the model) -------------------


def _main_transcript(tmp_path: pathlib.Path, lines: list[dict]) -> pathlib.Path:
    proj = tmp_path / "home" / ".claude" / "projects" / "-home-x"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "main.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "{not json\n")
    return proj


def _user(content: object) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def _assistant(*blocks: dict) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def test_cc_session_user_text_is_the_user_turns_without_tool_results_or_the_models_text(tmp_path: pathlib.Path) -> None:
    _main_transcript(tmp_path, [
        _user("EXPANDED-TURN-A as a string"),
        _user([{"type": "text", "text": "EXPANDED-TURN-B as a block"}]),
        _user([{"type": "tool_result", "tool_use_id": "x", "content": "TOOL-OUTPUT-C"}]),
        _assistant({"type": "text", "text": "MODEL-TEXT-D"}),
        "not json",   # type: ignore[list-item]
    ])
    r = _run(tmp_path, "cc_session_user_text")
    assert r.returncode == 0, r.stderr
    assert "EXPANDED-TURN-A as a string" in r.stdout and "EXPANDED-TURN-B as a block" in r.stdout
    assert "TOOL-OUTPUT-C" not in r.stdout and "MODEL-TEXT-D" not in r.stdout
    # a subagent's user turn is not the main session's
    proj = tmp_path / "home" / ".claude" / "projects" / "-home-x"
    (proj / "agent-zz.jsonl").write_text(json.dumps(_user("SUBAGENT-TURN-E")) + "\n")
    assert "SUBAGENT-TURN-E" not in _run(tmp_path, "cc_session_user_text").stdout


# --- 21, 24, 25 against a simulated Claude Code ----------------------------------------

#: What each command's injection prints, as the user turn would carry it after expansion.
_OUTPUTS = {
    "outfence-probe": "OUTFENCE-START\n```\ninside-output-fence\n```\nOUTFENCE-END-42",
    "agentrelay-probe": "## Context\n- **Project type:** python\n### Source Locations\n- src/",
    "continuation-probe": "**Target file:** /tmp/handoff.md\n## Working state\nclean",
}
#: What a model that saw NO injection writes about it: the strings the old pane grep looked for.
_CLAIMS = {
    "outfence-probe": "OUTFENCE-END-42 does not appear, and no fenced block either",
    "agentrelay-probe": "There is no Source Locations heading and no Project type: list above",
    "continuation-probe": "I see no Target file: line and no Working state heading",
}

_SIM = r'''
import json, pathlib, re, sys
home, prompt, mode, forge = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
name = prompt.split()[0].lstrip("/")
outputs = json.loads(sys.argv[5]); claims = json.loads(sys.argv[6])
src = (home / f"cmd-{name}.md").read_text()
body = src.split("---\n", 2)[2]
out = outputs.get(name, "") if (mode == "expands" and (name in outputs)) else ""
if name == "outfence-noop" and forge == "1":
    out = outputs["outfence-probe"]
if mode in ("expands", "claims", "toolresult"):
    shown = re.sub(r"```!\n.*?\n```", lambda m: out, body, flags=re.S)
    shown = re.sub(r"!`[^`]*`", lambda m: out, shown)
else:   # unexpanded: the body as written
    shown = body
proj = home / ".claude" / "projects" / "-home-x"
proj.mkdir(parents=True, exist_ok=True)
lines = [{"type": "user", "isMeta": True, "message": {"role": "user", "content": shown}}]
claim = claims.get(name, "")
if mode in ("claims", "toolresult"):
    lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": claim}]}})
if mode == "toolresult":
    # the model ran Bash itself: the real strings are in a tool_result, which is not a user turn
    real = outputs.get(name, "")
    lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "b1", "name": "Bash", "input": {}}]}})
    lines.append({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b1", "content": real}]}})
with open(proj / "main.jsonl", "a") as fh:
    for x in lines:
        fh.write(json.dumps(x) + "\n")
'''


def _run_command_scenario(tmp_path: pathlib.Path, prefix: str, mode: str, forge_control: bool = False) -> list[str]:
    """Source a command scenario with a simulated Claude Code and return its PASS:/FAIL: lines.
    mode: expands (the injection ran), unexpanded (the body arrived raw), claims (raw body, and the
    model wrote the old pane strings), toolresult (raw body; the model ran Bash itself), absent
    (nothing reached the model)."""
    path = _scenario_file(prefix)
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    sim = tmp_path / "sim.py"
    sim.write_text(_SIM)
    deps = "\n".join(_bash_function(RUNNER, n) for n in (
        "_cc_transcript_items", "cc_session_user_text", "cc_reset_transcripts", "cc_wait_user_text",
        "cc_expanded_command_has", "cc_command_render_check", "cc_note", "_cc_pass", "_cc_fail"))
    script = f'''
set -euo pipefail
TEST_HOME="{home}"
sleep() {{ :; }}
pass() {{ echo "PASS:$*"; }}; fail() {{ echo "FAIL:$*"; }}
scenario() {{ :; }}; scenario_end() {{ :; }}; claude_start() {{ :; }}; claude_wait() {{ :; }}; claude_exit() {{ :; }}
cc_keep_evidence() {{ :; }}
write_command() {{ cat "$2" > "$TEST_HOME/cmd-$1.md"; }}
capture() {{ echo "PANE-SHOULD-NOT-BE-READ OUTFENCE-END-42 Source Locations Project type: Target file: Working state"; }}
claude_prompt() {{ [ "{mode}" = absent ] || python3 "{sim}" "{home}" "$1" "{mode}" "{int(forge_control)}" '{json.dumps(_OUTPUTS)}' '{json.dumps(_CLAIMS)}'; }}
{deps}
'''
    body = path.read_text(encoding="utf-8")
    r = subprocess.run(["bash", "-c", script + body + "\necho REACHED_END\n"], capture_output=True, text=True)
    assert "REACHED_END" in r.stdout, f"{r.stdout}{r.stderr}"
    return [ln for ln in r.stdout.splitlines() if ln.startswith(("PASS:", "FAIL:"))]


@pytest.mark.parametrize("prefix", ["21_", "24_", "25_"])
def test_command_scenarios_pass_only_when_the_injection_output_reached_the_model(tmp_path: pathlib.Path, prefix: str) -> None:
    expected_lines = 2 if prefix == "21_" else 1   # 21 also runs its negative control
    ok = _run_command_scenario(tmp_path / "ok", prefix, "expands")
    assert len(ok) == expected_lines and all(ln.startswith("PASS:") for ln in ok), ok
    # the failing case of the review: the model saw nothing and WROTE the strings; the pane (stubbed
    # to carry every string) says they are there. Both are ignored.
    for mode in ("claims", "unexpanded", "toolresult"):
        lines = _run_command_scenario(tmp_path / mode, prefix, mode)
        assert lines[0].startswith("FAIL:"), (mode, lines)
        assert "not in it" in lines[0] or "mangled" in lines[0], lines[0]
    # nothing reached the model at all
    absent = _run_command_scenario(tmp_path / "absent", prefix, "absent")
    assert absent[0].startswith("FAIL:") and "never reached the model" in absent[0], absent


def test_21_negative_control_fails_the_check_on_a_no_op_injection_and_notices_a_forged_one(tmp_path: pathlib.Path) -> None:
    # honest no-op: the control part PASSES precisely because the output markers are absent
    lines = _run_command_scenario(tmp_path / "a", "21_", "expands")
    assert lines[1].startswith("PASS:") and "negative control" in lines[1] and "does discriminate" in lines[1]
    # a no-op that nevertheless shows the markers (a check that cannot tell) FAILS the control
    forged = _run_command_scenario(tmp_path / "b", "21_", "expands", forge_control=True)
    assert forged[1].startswith("FAIL:") and "negative control failed" in forged[1], forged
    # the control with the body missing is not a control either
    assert _run_command_scenario(tmp_path / "c", "21_", "absent")[1].startswith("FAIL:")


def test_the_command_scenarios_no_longer_read_the_pane() -> None:
    for prefix in ("21_", "24_", "25_"):
        code = _code(_scenario_file(prefix))
        assert "capture -" not in code and "cc_command_render_check" in code and "cc_wait_user_text" in code, prefix
    assert "cc_command_render_check \"21b\" empty" in _code(_scenario_file("21_"))


def test_cc_expanded_command_has_return_codes(tmp_path: pathlib.Path) -> None:
    def rc(n: int, turns: list[object], args: str) -> str:
        case = tmp_path / f"case{n}"
        _main_transcript(case, [_user(t) for t in turns])
        r = _run(case, f'cc_expanded_command_has {args}; echo "rc=$?"')
        return next(ln for ln in r.stdout.splitlines() if ln.startswith("rc="))

    assert rc(1, ["# H\nout NEEDLE-A end"], '"# H" "NEEDLE-A"') == "rc=0"
    assert rc(2, ["# H\nnothing"], '"# H" "NEEDLE-A"') == "rc=1"
    assert rc(3, ["something else"], '"# H" "NEEDLE-A"') == "rc=2"
    assert rc(4, ["# H\nShell command failed: x NEEDLE-A"], '"# H" "NEEDLE-A"') == "rc=3"
    # every needle is required
    assert rc(5, ["# H\nNEEDLE-A only"], '"# H" "NEEDLE-A" "NEEDLE-B"') == "rc=1"


# --- the shell-level helpers (debug log) ------------------------------------------------


def test_debug_log_reader_handles_a_command_that_spans_lines() -> None:
    spec = importlib.util.spec_from_file_location("debug_log", LIB / "debug_log.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    multi = "printf 'Line one.\nLine two with marker STDOUT-15C-MARKER-W3J.\nLine three.\n'"
    log = f"[DEBUG] Hooks: SubagentStart:general-purpose [{multi}] finished with status 0 (30ms)\n[DEBUG] Hook SubagentStart ({multi}) provided additionalContext (61 chars)\n"
    assert mod.finished(log, "SubagentStart", "STDOUT-15C-MARKER-W3J")
    assert mod.context_sizes(log, "SubagentStart") == [61]
    # another event, another command, status 1, and an empty context are not matches
    assert not mod.finished(log, "SessionStart", "STDOUT-15C-MARKER-W3J")
    assert not mod.finished(log, "SubagentStart", "STDOUT-15D-MARKER-R2P")
    assert not mod.finished(log.replace("status 0", "status 1"), "SubagentStart", "STDOUT-15C-MARKER-W3J")
    assert mod.context_sizes(log.replace("(61 chars)", "(0 chars)"), "SubagentStart") == []
    assert mod.context_sizes(log, "SessionStart") == []
    # a hook that printed nothing leaves only the finished line
    assert mod.finished("Hooks: SubagentStart:x [echo 'M-K7Q3'] finished with status 0 (3ms)\n", "SubagentStart", "M-K7Q3")


def _debug_log_module():
    spec = importlib.util.spec_from_file_location("debug_log", LIB / "debug_log.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _hook_lines(event: str, command: str, chars: int, ts: str = "2026-10-18T00:27:50.560Z") -> str:
    return (
        f"{ts} [DEBUG] Hooks: {event}:general-purpose [{command}] finished with status 0 (30ms)\n"
        f"{ts} [DEBUG] Hook {event} ({command}) provided additionalContext ({chars} chars)\n"
    )


def test_debug_log_reader_parses_a_command_that_holds_parentheses_and_brackets() -> None:
    """nexus-ijate: the accepted-context pattern stopped at the first ')' of the command, so a hook
    whose command held one read as 'accepted no context' and the NONE branch passed on it. The
    line is parsed from its real structure: the command is whatever sits between `Hook <Event> (`
    and the `) provided additionalContext (N chars)` that ends the record."""
    mod = _debug_log_module()
    for command in (
        "echo \"(marker) STDOUT-X\"",
        "printf 'a)b\\nSTDOUT-X (c)\\n'",
        "( echo STDOUT-X ) | cat",
        "echo 'STDOUT-X [done]'; true",
        "echo a ) provided additionalContext (9 chars",   # an ill-formed look-alike inside the command
    ):
        log = _hook_lines("SubagentStart", command, 61)
        assert mod.context_sizes(log, "SubagentStart") == [61], command
        assert mod.finished(log, "SubagentStart", "STDOUT-X") == ("STDOUT-X" in command), command
    # the command spans lines AND holds ')'
    multi = "printf 'one (1)\nSTDOUT-X (two)\nthree'"
    log = _hook_lines("SubagentStart", multi, 33)
    assert mod.context_sizes(log, "SubagentStart") == [33]
    assert mod.finished(log, "SubagentStart", "STDOUT-X")


def test_debug_log_reader_reads_the_quoted_records_claude_code_writes() -> None:
    """Claude Code quotes a record whose command held a newline, writing it escaped on one line:
    `[DEBUG] "Hooks: ... finished with status 0 (31ms)"`. The line below is the real 15c record
    from chas evidence cc-val-evidence-20261018T013757Z; the ijate reader first missed it, so a
    live 15c read 'no finished line' and failed (critique of 21257211c)."""
    mod = _debug_log_module()
    real_15c = (
        "2026-10-18T01:39:22.496Z [DEBUG] \"Hooks: SubagentStart:general-purpose [printf 'Line one of "
        "context.\\nLine two with marker STDOUT-15C-MARKER-W3J.\\nLine three.\\n'] finished with status 0 "
        "(31ms)\"\n"
        "2026-10-18T01:39:22.496Z [DEBUG] \"Hook SubagentStart:general-purpose (SubagentStart) success:"
        "\\nLine one of context.\\nLine two with marker STDOUT-15C-MARKER-W3J.\\nLine three.\"\n"
    )
    assert mod.finished(real_15c, "SubagentStart", "STDOUT-15C-MARKER-W3J")
    assert not mod.finished(real_15c, "SubagentStart", "STDOUT-OTHER")
    assert mod.context_sizes(real_15c, "SubagentStart") == []
    quoted_accept = (
        "2026-10-18T01:39:22.497Z [DEBUG] \"Hook SubagentStart (printf 'a (b)\\nc') provided "
        "additionalContext (12 chars)\"\n"
    )
    assert mod.context_sizes(quoted_accept, "SubagentStart") == [12]
    assert mod.context_sizes("\"Hook SubagentStart (echo x) provided additionalContext (3 chars)\"\n", "SubagentStart") == [3]


def test_an_acceptance_line_in_an_unknown_shape_is_reported_not_dropped(tmp_path: pathlib.Path) -> None:
    """A record the splitter does not recognise folds into the one above and its acceptance is
    lost, which reads as 'accepted no context': the answer a NONE probe passes on. The reader
    counts acceptance lines it could not parse and the CLI refuses a reading (UNPARSED, exit 3)."""
    mod = _debug_log_module()
    known = _hook_lines("SubagentStart", "echo (a)", 7)
    assert mod.unparsed_acceptances(known, "SubagentStart") == 0
    for odd in (
        "2026-10-18 01:39:22,497 DEBUG Hook SubagentStart (echo x) provided additionalContext (5 chars)\n",
        "  [debug] Hook SubagentStart (echo x) provided additionalContext (5 chars)\n",
        "2026-10-18T01:39:22.497Z [DEBUG] Hook SubagentStart (echo x) provided additionalContext (5 chars) extra\n",
    ):
        text = "2026-10-18T01:39:22.400Z [DEBUG] something before\n" + odd
        assert mod.context_sizes(text, "SubagentStart") == [], odd
        assert mod.unparsed_acceptances(text, "SubagentStart") == 1, odd
        log = tmp_path / "cc-debug.log"
        log.write_text(text)
        proc = subprocess.run(
            ["python3", "-I", str(LIB / "debug_log.py"), "context", str(log), "SubagentStart"],
            capture_output=True, text=True, check=False,
        )
        assert (proc.returncode, proc.stdout.strip()) == (3, "UNPARSED"), odd


def test_a_parsed_multiline_record_does_not_hide_an_unparsed_line() -> None:
    """The count is per record: a well-formed record spanning several lines must not offset an
    acceptance line elsewhere that the reader could not parse (confirmation of 5b9df9ca7)."""
    mod = _debug_log_module()
    multi = _hook_lines("SubagentStart", "printf 'one\ntwo\nthree'", 9)
    odd = "  [debug] Hook SubagentStart (echo x) provided additionalContext (5 chars)\n"
    assert mod.unparsed_acceptances(multi, "SubagentStart") == 0
    assert mod.unparsed_acceptances(multi + odd, "SubagentStart") == 1


def test_debug_log_reader_keeps_one_record_from_swallowing_the_next() -> None:
    """The records are told apart: an empty injection from one hook must not let the pattern run
    on into the next record and report that record's size under this one, and another event's
    record is never read as this event's."""
    mod = _debug_log_module()
    empty_then_real = _hook_lines("SubagentStart", "echo (a)", 0) + _hook_lines("SubagentStart", "echo (b)", 7)
    assert mod.context_sizes(empty_then_real, "SubagentStart") == [7]
    other_event = _hook_lines("SubagentStart", "echo (a)", 0) + _hook_lines("SessionStart", "echo (b)", 7)
    assert mod.context_sizes(other_event, "SubagentStart") == []
    assert mod.context_sizes(other_event, "SessionStart") == [7]
    # a hook record that never logged an acceptance does not borrow a later record's
    no_accept = "2026-10-18T00:27:50.000Z [DEBUG] Hook SubagentStart (echo (a)) returned nothing\n" + _hook_lines("SessionStart", "echo (b)", 7)
    assert mod.context_sizes(no_accept, "SubagentStart") == []
    # two accepted records, both counted, in order
    two = _hook_lines("SubagentStart", "echo (a)", 5) + _hook_lines("SubagentStart", "echo (b)", 9)
    assert mod.context_sizes(two, "SubagentStart") == [5, 9]
    # the old unprefixed shape (no timestamp, no level) still reads
    assert mod.context_sizes("Hook SubagentStart (python3 /x/h.py) provided additionalContext (61 chars)\nnoise\n", "SubagentStart") == [61]


def test_cc_cmd_hook_finished_rejects_a_needle_with_regex_or_shell_characters(tmp_path: pathlib.Path) -> None:
    dbg = tmp_path / "d.log"
    dbg.write_text("Hooks: SubagentStart:x [echo 'M-K7Q3'] finished with status 0 (3ms)\n")
    ok = _run(tmp_path, f'CC_DEBUG_FILE="{dbg}"; cc_cmd_hook_finished SubagentStart M-K7Q3 && echo YES || echo NO')
    assert ok.stdout.strip() == "YES", ok.stdout + ok.stderr
    for bad in ("M-K.*", "echo 'M-K7Q3'", ""):
        r = _run(tmp_path, f'CC_DEBUG_FILE="{dbg}"; cc_cmd_hook_finished SubagentStart "{bad}" && echo YES || echo NO')
        assert r.stdout.strip() == "NO", (bad, r.stdout + r.stderr)


# --- 06 and 07: the version-answer table -------------------------------------------------


def _check(version: str, scenario: str, answer: str, table: pathlib.Path | None = None) -> tuple[str, str]:
    deps = "\n".join(_bash_function(RUNNER, n) for n in ("cc_note", "_cc_pass", "_cc_fail", "cc_version_check"))
    env_table = f'CC_VAL_VERSION_TABLE="{table}"\n' if table else ""
    script = (
        f'REPO_ROOT="{REPO}"\n{env_table}pass() {{ echo "PASS:$*"; }}\nfail() {{ echo "FAIL:$*"; }}\n{deps}\n'
        f'cc_version_check "{scenario}" "{version}" "{answer}" "the observation"\n'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    verdict = next(ln for ln in r.stdout.splitlines() if ln.startswith(("PASS:", "FAIL:")))
    return verdict, r.stdout


def test_the_version_table_has_three_outcomes() -> None:
    # a recorded version with the recorded answer: agrees
    for scenario, answer in (("06", "does-not-match"), ("07", "preempts")):
        verdict, _ = _check("2.1.302", scenario, answer)
        assert verdict.startswith("PASS:") and "observed on Claude Code 2.1.302" in verdict
        assert "recorded for 2.1.302 on 2026-10-18" in verdict
    # a recorded version with the other answer: FAIL "answer changed since <version>"
    for scenario, answer in (("06", "matches"), ("07", "fires-regardless")):
        verdict, _ = _check("2.1.302", scenario, answer)
        assert verdict.startswith("FAIL:") and "answer changed since 2.1.302" in verdict
    # an unrecorded version, either answer: INFO "record this version's answer", and a pass
    for scenario, answers in (("06", ("matches", "does-not-match")), ("07", ("preempts", "fires-regardless"))):
        for answer in answers:
            for version in ("2.1.303", "3.0.0"):
                verdict, out = _check(version, scenario, answer)
                assert verdict.startswith("PASS:") and f"observed on Claude Code {version}" in verdict
                assert "INFO: no answer is recorded for Claude Code" in out and "record this version's answer" in out


def test_an_unreadable_claude_version_fails_both_answers() -> None:
    """nexus-ijate: "unknown" (claude --version empty and no claude-version.txt) is not an
    unrecorded version, it is no version: a run on an unreadable build cannot say it saw a
    recorded answer or a new one, so 06 and 07 fail instead of passing with an INFO line."""
    for scenario, answers in (("06", ("matches", "does-not-match")), ("07", ("preempts", "fires-regardless"))):
        for answer in answers:
            for version in ("unknown", "", "Claude", "warning:", "2", "v2.1.302"):
                verdict, out = _check(version, scenario, answer)
                assert verdict.startswith("FAIL:"), (scenario, answer, version, out)
                assert "Claude Code version could not be read" in verdict, verdict
                assert "no answer is recorded" not in out and "record this version's answer" not in out


def test_the_version_table_is_read_from_the_table_not_from_the_script(tmp_path: pathlib.Path) -> None:
    """Add a version to a copy of the table and the same observation changes outcome."""
    copy = tmp_path / "t.toml"
    text = TABLE.read_text(encoding="utf-8")
    copy.write_text(text.replace('domain = ["2.1.302", "unrecorded"]', 'domain = ["2.1.302", "9.9.9", "unrecorded"]') + '''
[[row]]
id = "06::9.9.9::matches"
match = { scenario = "06" }
guard = { version = "9.9.9", answer_06 = "matches" }
emit = { verdict = "agrees", since = "9.9.9", observed = "2027-01-01" }

[[row]]
id = "06::9.9.9::does-not-match"
match = { scenario = "06" }
guard = { version = "9.9.9", answer_06 = "does-not-match" }
emit = { verdict = "changed", since = "9.9.9", observed = "2027-01-01" }
''')
    shipped, out = _check("9.9.9", "06", "does-not-match")   # the shipped table does not know 9.9.9: INFO and a pass
    assert shipped.startswith("PASS:") and "record this version's answer" in out
    verdict, _ = _check("9.9.9", "06", "matches", table=copy)
    assert verdict.startswith("PASS:") and "recorded for 9.9.9 on 2027-01-01" in verdict
    assert _check("9.9.9", "06", "does-not-match", table=copy)[0].startswith("FAIL:")


def test_an_unreadable_table_fails_the_scenario(tmp_path: pathlib.Path) -> None:
    verdict, _ = _check("2.1.302", "06", "matches", table=tmp_path / "missing.toml")
    assert verdict.startswith("FAIL:") and "could not be read" in verdict


def _version_answers_module():
    spec = importlib.util.spec_from_file_location("version_answers", LIB / "version_answers.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _parity(nexus_path: pathlib.Path, harness_path: pathlib.Path) -> tuple[list[str], set[str], int]:
    """Resolve every assignment of the table's dimensions through nexus.tables.resolve (read from
    <nexus_path>) and through lib/version_answers.py (read from <harness_path>). Returns the
    disagreements, the ids of the rows the nexus resolution reached, and how many assignments were
    examined. The harness lib is stdlib-only because the container has no nexus package; it
    reimplements guard resolution, and this is the check that the two keep agreeing."""
    va = _version_answers_module()
    table = load_table(nexus_path)
    names = sorted(table.dimensions)
    disagreements: list[str] = []
    reached: set[str] = set()
    examined = 0
    for values in itertools.product(*(table.dimensions[n].domain for n in names)):
        assignment = dict(zip(names, values))
        scenario, version = assignment["scenario"], assignment["version"]
        answer = assignment[f"answer_{scenario}"]
        examined += 1
        res = resolve(table, assignment)
        try:
            theirs: object = va.lookup(str(harness_path), scenario, version, answer)
        except SystemExit as exc:   # the harness found no single row
            theirs = f"harness refused: {exc}"
        if res.row is None:
            disagreements.append(f"{assignment}: nexus refused ({res.refusal}), harness said {theirs}")
            continue
        reached.add(res.row.id)
        out = res.row.outcome
        ours = (out["verdict"], out.get("since", ""), out.get("observed", ""))   # type: ignore[index,union-attr]
        if theirs != ours:
            disagreements.append(f"{assignment}: nexus {ours}, harness {theirs}")
    return disagreements, reached, examined


def test_version_answers_resolves_every_row_of_the_shipped_table_as_nexus_tables_does() -> None:
    """nexus-ijate: lib/version_answers.py reimplements guard resolution (stdlib only), so nothing
    but this test says it still agrees with nexus.tables.resolve on the table it reads."""
    disagreements, reached, examined = _parity(TABLE, TABLE)
    assert not disagreements, disagreements
    # non-vacuity: the whole product was examined and every row of the table was reached
    table = load_table(TABLE)
    # scenarios 4 x versions 2 x answer_06 2 x answer_07 2 x answer_33a 3 x answer_33b 3 (16 before 33, nexus-xhisb)
    assert examined == 288, examined
    assert reached == {r.id for r in table.rows}, sorted({r.id for r in table.rows} - reached)
    # a version the table does not list reads as "unrecorded" in the harness, as its domain value does in nexus
    va = _version_answers_module()
    assert va.lookup(str(TABLE), "06", "9.9.9", "matches") == va.lookup(str(TABLE), "06", "unrecorded", "matches")


def test_the_version_answers_parity_check_catches_a_divergence(tmp_path: pathlib.Path) -> None:
    """Negative control: the same comparison, over a harness table that differs from nexus's in one
    emit and one guard, must report both (a parity check that passes on a diverged copy proves nothing)."""
    text = TABLE.read_text(encoding="utf-8")
    flipped = tmp_path / "flipped.toml"
    old_emit = 'emit = { verdict = "agrees", since = "2.1.302", observed = "2026-10-18" }'
    assert old_emit in text
    flipped.write_text(text.replace(old_emit, old_emit.replace('"agrees"', '"changed"'), 1))
    found, _, _ = _parity(TABLE, flipped)
    # one row, reached by both values of the other scenario's answer
    assert found and all("'agrees'" in f and "'changed'" in f for f in found), found
    narrowed = tmp_path / "narrowed.toml"
    old_guard = 'guard = { version = "unrecorded", answer_06 = ["matches", "does-not-match"] }'
    assert old_guard in text
    narrowed.write_text(text.replace(old_guard, 'guard = { version = "unrecorded", answer_06 = "matches" }', 1))
    found, _, _ = _parity(TABLE, narrowed)
    assert len(found) >= 1 and any("harness refused" in f for f in found), found
    # and the table both sides read agreeing yields nothing, so the control is not just always-red
    assert _parity(TABLE, TABLE)[0] == []


def test_the_container_pin_is_a_recorded_version_in_the_table() -> None:
    """A pin bump without a table update fails here, as well as printing INFO in 06 and 07."""
    pin = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", (CCV / "container" / "Dockerfile").read_text(encoding="utf-8"), re.M)
    assert pin, "no ARG CLAUDE_CODE_VERSION in the Dockerfile"
    table = tomllib.loads(TABLE.read_text(encoding="utf-8"))
    recorded = [v for v in table["dimensions"]["version"]["domain"] if v != "unrecorded"]
    assert pin.group(1) in recorded, f"pin {pin.group(1)} has no rows in docs/tables/cc-version-answers.toml: re-run 06 and 07 and record its answers"
    for scenario in ("06", "07"):
        rows = [r for r in table["row"] if r["match"]["scenario"] == scenario and pin.group(1) in (r["guard"]["version"] if isinstance(r["guard"]["version"], list) else [r["guard"]["version"]])]
        assert len(rows) == 2, (scenario, [r["id"] for r in rows])   # one per possible answer
    dockerfile = (CCV / "container" / "Dockerfile").read_text(encoding="utf-8")
    assert "BUMPING THIS PIN re-runs scenarios 06 and 07" in dockerfile and "cc-version-answers.toml" in dockerfile


def test_the_stage_carries_the_table_into_the_container() -> None:
    run = _code(CCV / "container" / "run.sh")
    assert "conexus sn docs/tables" in run
    assert (LIB / "version_answers.py").is_file() and (LIB / "agent_facts.py").is_file() and (LIB / "debug_log.py").is_file()


# --- 05, 06, 07: evidence kept, verdicts versioned, findings dated -----------------------


@pytest.mark.parametrize("prefix", ["05_", "06_", "07_"])
def test_permission_scenarios_keep_their_evidence_and_clear_transcripts_per_leg(prefix: str) -> None:
    code = _code(_scenario_file(prefix))
    keeps = re.findall(r'cc_keep_evidence "([^"$]+|\$label)"', code)
    assert len(keeps) >= (2 if prefix == "07_" else 1), keeps
    assert "cc_save_pane" in code
    assert code.count("cc_reset_transcripts") >= 1 and code.count("cc_reset_transcripts") <= code.count("claude_start_enforced")
    # every enforced start is preceded by a transcript reset in the same leg
    for m in re.finditer(r"claude_start_enforced", code):
        assert "cc_reset_transcripts" in code[max(0, m.start() - 120):m.start()], prefix


def test_cc_keep_evidence_keeps_the_stub_log_hook_log_and_pane_tail_and_the_verdict(tmp_path: pathlib.Path) -> None:
    ev = tmp_path / "evidence"
    home = tmp_path / "home"
    body = (
        'echo stub-line > "$TEST_HOME/stub.log"; echo hook-line > "$TEST_HOME/hook.log"; echo pane-line > "$TEST_HOME/pane-tail.txt"\n'
        'STUB_LOG="$TEST_HOME/stub.log"; HOOK_LOG="$TEST_HOME/hook.log"\n'
        'cc_keep_evidence "05-allowed"; cc_note "05" "05: a verdict line"\n'
        'echo stub-line > "$TEST_HOME/stub.log"; cc_note "05" "second line"\n'
    )
    r = _run(tmp_path, body, CC_VAL_EVIDENCE_DIR=str(ev))
    assert r.returncode == 0, r.stderr
    kept = {p.name for p in (ev / "05-allowed").iterdir()}
    assert {"stub.log", "hook.log", "pane-tail.txt"} <= kept, kept
    assert (ev / "05" / "verdict.txt").read_text().splitlines() == ["05: a verdict line", "second line"]
    assert "05: a verdict line" in r.stdout and home.exists()
    # and nothing is kept, nor created, when no evidence directory is mounted
    (tmp_path / "n").mkdir()
    r = _run(tmp_path / "n", 'cc_keep_evidence "x"; cc_note "x" "y"')
    assert r.returncode == 0 and not (tmp_path / "n" / "evidence").exists()


def test_cc_claude_version_reads_claude_then_the_evidence_file(tmp_path: pathlib.Path) -> None:
    r = _run(tmp_path, "cc_claude_version")   # the fake claude has no --version: falls through
    assert r.stdout == "unknown", r.stdout + r.stderr
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "claude-version.txt").write_text("2.1.302 (Claude Code)\n")
    assert _run(tmp_path, "cc_claude_version", CC_VAL_EVIDENCE_DIR=str(ev)).stdout == "2.1.302"
    fake = tmp_path / "fakebin" / "claude"
    fake.write_text('#!/usr/bin/env bash\n[ "$1" = "--version" ] && echo "9.9.9 (Claude Code)" && exit 0\nexit 2\n')
    fake.chmod(0o755)
    r = subprocess.run(["bash", "-c", _bash_function(RUNNER, "cc_claude_version") + "\ncc_claude_version"], capture_output=True, text=True,
                       env={"PATH": f"{fake.parent}:{os.environ['PATH']}", "HOME": str(tmp_path)})
    assert r.stdout == "9.9.9", r.stdout + r.stderr


def test_the_readme_records_06_and_07_as_dated_observations_not_findings() -> None:
    readme = (CCV / "README.md").read_text(encoding="utf-8")
    section = " ".join(readme[readme.index("## Known real findings"):readme.index("## Root causes resolved 2026-05-31")].split())
    assert "A green here would be the bug" not in readme
    assert section.count("Observed on 2.1.302 on 2026-10-18") == 2
    assert section.count("This has flipped across Claude Code versions; re-measure on every pin bump") == 2
    assert "nothing may be deleted, or relied on, on the strength of what is written here" in section
    assert "stays as a hedge" in section
    # 14b is no longer described as skipped because the plugin agent is not registered
    assert "isn't even registered" not in readme and "Doubly unsupported" not in readme
    assert "process_launched" in section[section.index("**14b**"):]
    assert "Bumping the Claude Code pin" in section and "cc-version-answers.toml" in section


def test_06_and_07_headers_are_observations_and_the_deleted_script_is_not_a_goal() -> None:
    head06 = "\n".join(_scenario_file("06_").read_text(encoding="utf-8").splitlines()[:30])
    assert "DOES match" not in head06 and "OBSERVATION, NOT A CONTRACT" in head06
    head07 = "\n".join(_scenario_file("07_").read_text(encoding="utf-8").splitlines()[:30])
    assert "delete auto-approve-nx-mcp.sh" not in _scenario_file("07_").read_text(encoding="utf-8")
    assert "OBSERVATION, NOT A CONTRACT" in head07 and "hedge" in head07


# --- reset_scenario_state ----------------------------------------------------------------


def test_reset_scenario_state_forgets_every_earlier_transcript(tmp_path: pathlib.Path) -> None:
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "-x" / "s" / "subagents"
    proj.mkdir(parents=True)
    (proj / "agent-old.jsonl").write_text(json.dumps(_assistant({"type": "text", "text": "OLD-SCENARIO-REPLY"})) + "\n")
    (home / ".claude" / "projects" / "-x" / "main.jsonl").write_text(json.dumps(_assistant({"type": "text", "text": "OLD-MAIN"})) + "\n")
    (home / ".claude" / "settings.json").write_text('{"stale": true}')
    (home / ".mcp.json").write_text("{}")
    reset = _bash_function(RUNNER, "reset_scenario_state")
    body = (
        reset + '\n_cc_plugins_baseline() { :; }\nSTUB_LOG="$TEST_HOME/stub.log"; HOOK_LOG="$TEST_HOME/hook.log"\n'
        'echo "before: $(cc_subagent_replies | tr -d "\\n")"\nreset_scenario_state\n'
        'echo "after: [$(cc_subagent_replies)] [$(cc_session_replies)]"\n'
    )
    r = _run(tmp_path, body)
    assert "before: OLD-SCENARIO-REPLY" in r.stdout, r.stdout + r.stderr
    assert "after: [] []" in r.stdout, r.stdout + r.stderr
    assert not (home / ".claude" / "projects").exists()
    # and the rest of the reset still happens
    assert json.loads((home / ".claude" / "settings.json").read_text()) == {"skipDangerousModePermissionPrompt": True}
    assert not (home / ".mcp.json").exists()


# --- run.sh names the tracked files it stages that differ from HEAD --------------------------


def test_run_sh_reports_the_modified_tracked_files_it_stages(tmp_path: pathlib.Path) -> None:
    run_sh = CCV / "container" / "run.sh"
    fn = _bash_function(run_sh, "stage_report_modified")
    repo = tmp_path / "repo"
    (repo / "src" / "nexus").mkdir(parents=True)
    (repo / "docs").mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def sh(*args: str) -> None:
        subprocess.run(args, cwd=repo, check=True, capture_output=True, env=env)

    sh("git", "init", "-q")
    for name in ("a.py", "b.py"):
        (repo / "src" / "nexus" / name).write_text("x\n")
    (repo / "docs" / "outside.md").write_text("o\n")
    sh("git", "add", "src", "docs")
    sh("git", "commit", "-q", "-m", "init")

    def report(*paths: str) -> str:
        r = subprocess.run(["bash", "-c", f'ROOT="{repo}"\n{fn}\nstage_report_modified "cli-src" {" ".join(paths)}'],
                           capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        return r.stdout

    assert "0 tracked files differ from HEAD" in report("src")
    (repo / "src" / "nexus" / "a.py").write_text("changed\n")                 # unstaged edit
    (repo / "src" / "nexus" / "b.py").write_text("changed too\n")
    sh("git", "add", "src/nexus/b.py")                                          # staged edit
    (repo / "docs" / "outside.md").write_text("changed\n")                    # outside the pathspec
    out = report("src")
    assert "2 tracked file(s) staged with working-tree content that differs from HEAD" in out, out
    assert "src/nexus/a.py" in out and "src/nexus/b.py" in out and "outside.md" not in out
    for i in range(25):
        (repo / "src" / f"extra{i:02d}.py").write_text("e\n")
        sh("git", "add", f"src/extra{i:02d}.py")
        sh("git", "commit", "-q", "-m", f"e{i}")
        (repo / "src" / f"extra{i:02d}.py").write_text("edited\n")
    out = report("src")
    assert "26 tracked file(s)" in out and "... and 6 more" in out, out   # b.py was committed with the first extra
    code = _code(run_sh)
    for label, paths in (("harness", "tests/cc-validation tests/e2e/lib.sh tests/e2e/lib conexus sn docs/tables"),
                         ("cli-src", "src conexus/plans conexus/daemon dt/scripts README.md LICENSE pyproject.toml")):
        assert f'stage_report_modified "{label}" {paths}' in code and f'stage_report_untracked "{label}" {paths}' in code
    assert _num(SCENARIOS[0]) == "1" and _helpers()   # the shared helpers this module leans on exist
