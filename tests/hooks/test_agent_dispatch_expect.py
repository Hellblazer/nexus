# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the PreToolUse(Agent) expectations-declaration hook (RDR-184
Gap-1 mechanization, bead nexus-qc4p1).

WHAT THESE PIN, and why each one exists rather than being obvious:

* The EXPECT row is written from the DISPATCH's own tool input, before the
  dispatch lands. The convention it replaces was a human step that failed
  five sessions running.
* The key PAIRS with what SubagentStart records. This is the whole point
  (nexus-nu7fo): hand-written rows were keyed on an orchestrator-invented
  name, and the Agent tool has no name parameter, so they could never pair
  with anything. ``test_row_pairs_with_subagent_start_stamp`` runs the REAL
  start-stamp hook against the REAL dispatch hook and asserts the census
  recognises the pair — a mutation that breaks the key makes it fail.
* A hook failure never blocks the dispatch. The ledger is an audit aid.
* sync vs background is recorded from ``run_in_background``.

PAYLOAD SHAPES ARE MEASURED, NOT ASSUMED (2026-07-31, live Claude Code, a
catch-all PreToolUse + SubagentStart logger over real dispatches):

    PreToolUse    tool_name = "Agent"
                  tool_input = {description, prompt, subagent_type,
                                run_in_background}
                  top level  = {session_id, tool_use_id, prompt_id, cwd,
                                hook_event_name, permission_mode, effort,
                                transcript_path}
    SubagentStart {agent_id, agent_type, cwd, hook_event_name, prompt_id,
                   session_id, transcript_path}

with ``agent_type == subagent_type`` verbatim and the same ``session_id``.
``tool_use_id`` is unique per dispatch but absent from SubagentStart, and
``prompt_id`` is the TURN id (identical across every dispatch in one
message) — so no per-instance pairing key exists in either direction. The
fixtures below reproduce those shapes exactly.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "agent-dispatch-expect.sh"
STAMP = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-start-stamp.sh"
LIB = REPO_ROOT / "tests" / "e2e" / "lib" / "expectations.sh"
PLUGIN_LIB = REPO_ROOT / "conexus" / "hooks" / "scripts" / "expectations.sh"

SESSION = "sess-dispatch"


def _pretooluse(
    subagent_type: str = "conexus:code-review-expert",
    *,
    background: bool | None = True,
    tool_use_id: str = "toolu_01aaaaaaaaaaaaaaaaaaaaaa",
    tool_name: str = "Agent",
    session_id: str = SESSION,
) -> str:
    tool_input: dict[str, object] = {
        "description": "review the diff",
        "prompt": "You are reviewer-1. Review and SendMessage back.",
        "subagent_type": subagent_type,
    }
    if background is not None:
        tool_input["run_in_background"] = background
    return json.dumps(
        {
            "session_id": session_id,
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/tmp",
            "hook_event_name": "PreToolUse",
            "permission_mode": "acceptEdits",
            "prompt_id": "turn-1",
            "tool_use_id": tool_use_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
    )


def _subagent_start(agent_id: str, agent_type: str, session_id: str = SESSION) -> str:
    """The measured SubagentStart shape: unnamed morphology ``a<hash>``."""
    return json.dumps(
        {
            "session_id": session_id,
            "transcript_path": "/tmp/t.jsonl",
            "cwd": "/tmp",
            "hook_event_name": "SubagentStart",
            "prompt_id": "turn-1",
            "agent_id": agent_id,
            "agent_type": agent_type,
        }
    )


def _env(tmp_path: Path, mode: str | None) -> dict[str, str]:
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path / "state")}
    env.pop("NX_ORCH_STOP_GUARD", None)
    if mode is not None:
        env["NX_ORCH_STOP_GUARD"] = mode
    return env


def _run(
    stdin: str, tmp_path: Path, *, mode: str | None = None, script: Path = SCRIPT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        input=stdin, capture_output=True, text=True, timeout=30, env=_env(tmp_path, mode),
    )


def _expfile(tmp_path: Path, session_id: str = SESSION) -> Path:
    return tmp_path / "state" / "nexus" / "orchestration" / f"{session_id}.expectations"


def _lib_call(func: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Invoke a shellib function the way real callers do: source, then call."""
    quoted = " ".join(f"'{a}'" for a in args)
    return subprocess.run(
        ["bash", "-c", f"source '{LIB}'; {func} {quoted}"],
        capture_output=True, text=True, timeout=30, env=_env(tmp_path, None),
    )


class TestWritesTheRow:
    def test_default_mode_writes_expect_row_before_dispatch(self, tmp_path: Path) -> None:
        """DEFAULT-ON, like the start stamp: unset NX_ORCH_STOP_GUARD writes."""
        proc = _run(_pretooluse(), tmp_path)
        assert proc.returncode == 0, proc.stderr
        row = _expfile(tmp_path).read_text()
        assert "\tEXPECT\tconexus:code-review-expert\tbackground\t" in row

    def test_row_carries_the_dispatch_tool_use_id(self, tmp_path: Path) -> None:
        _run(_pretooluse(tool_use_id="toolu_01zzz"), tmp_path)
        fields = _expfile(tmp_path).read_text().strip().split("\t")
        assert fields[1] == "EXPECT"
        assert fields[4] == "toolu_01zzz", (
            "the 5th field must carry tool_use_id: it is the idempotence key AND "
            "what keeps two same-second same-type rows from collapsing"
        )

    def test_explicit_off_writes_nothing(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(), tmp_path, mode="off")
        assert proc.returncode == 0
        assert not _expfile(tmp_path).exists()

    def test_observe_mode_writes(self, tmp_path: Path) -> None:
        _run(_pretooluse(), tmp_path, mode="observe")
        assert "\tEXPECT\t" in _expfile(tmp_path).read_text()

    def test_stdout_is_silent(self, tmp_path: Path) -> None:
        """No envelope, no chatter. Silence is how PreToolUse says 'proceed'."""
        proc = _run(_pretooluse(), tmp_path)
        assert proc.stdout == "", f"hook emitted stdout: {proc.stdout!r}"

    def test_task_spelling_also_recorded(self, tmp_path: Path) -> None:
        _run(_pretooluse(tool_name="Task"), tmp_path)
        assert "\tEXPECT\t" in _expfile(tmp_path).read_text()

    def test_non_agent_tool_is_ignored(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(tool_name="Bash"), tmp_path)
        assert proc.returncode == 0
        assert not _expfile(tmp_path).exists()


class TestSyncVsBackground:
    """Only background rows ever cause an agent to owe a report, so the
    discriminator has to be real — a hook that cannot tell them apart must
    not mark everything background. ``run_in_background`` IS present in the
    payload (measured: True for a background dispatch, False for a sync
    one), so the discrimination is available and is asserted here."""

    def test_background_true_records_background(self, tmp_path: Path) -> None:
        _run(_pretooluse(background=True), tmp_path)
        assert "\tbackground\t" in _expfile(tmp_path).read_text()

    def test_background_false_records_sync(self, tmp_path: Path) -> None:
        _run(_pretooluse(background=False), tmp_path)
        content = _expfile(tmp_path).read_text()
        assert "\tsync\t" in content
        assert "\tbackground\t" not in content

    def test_absent_field_records_background(self, tmp_path: Path) -> None:
        """The Agent tool's documented default is background. Recording an
        obligation that turns out to be sync is noise; dropping a real one
        is the Gap-1 failure itself."""
        _run(_pretooluse(background=None), tmp_path)
        assert "\tbackground\t" in _expfile(tmp_path).read_text()


class TestPairsWithSubagentStart:
    """nexus-nu7fo: the row must pair with what SubagentStart records.

    Run BOTH real hooks — dispatch first, then the start stamp with the
    measured payload shape — and assert the audit surfaces actually see a
    pair. Before this change the same sequence produced
    ``recognized=0 / BLINDSPOT``, twenty-five times across five sessions.
    """

    def test_row_pairs_with_subagent_start_stamp(self, tmp_path: Path) -> None:
        _run(_pretooluse(subagent_type="conexus:code-review-expert"), tmp_path)
        _run(
            _subagent_start("a29d3cfdd53ae3e98", "conexus:code-review-expert"),
            tmp_path, script=STAMP,
        )
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert census.returncode == 0, f"BLINDSPOT hard-failure: {census.stdout}"
        assert "BLINDSPOT\tchecked=1 recognized=1 unrecognized=0" in census.stdout
        assert "AGENT\ta29d3cfdd53ae3e98\tconexus:code-review-expert\t" in census.stdout
        assert census.stdout.rstrip().endswith("unrecognized=0")
        assert "\tdeclared\n" in census.stdout
        undeclared = _lib_call("expectations_undeclared", tmp_path, SESSION)
        assert undeclared.returncode == 0
        assert "SUMMARY\tchecked=1 recognized=1 unrecognized=0 undeclared=0" in undeclared.stdout
        assert "UNDECLARED" not in undeclared.stdout

    def test_undispatched_start_is_still_flagged(self, tmp_path: Path) -> None:
        """Non-vacuity: with the dispatch hook NOT run, the same start row
        reads exactly as the five broken sessions did."""
        _run(
            _subagent_start("a29d3cfdd53ae3e98", "conexus:code-review-expert"),
            tmp_path, script=STAMP,
        )
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert census.returncode == 1, "a 0-of-N session must hard-fail"
        assert "BLINDSPOT\tchecked=1 recognized=0 unrecognized=1" in census.stdout


class TestSameTypeDispatchedTwice:
    """The multiple-dispatches-of-one-type case. Chosen resolution:
    N-OF-TYPE matching, no ordinal — because no per-instance key exists in
    EITHER payload (tool_use_id is absent from SubagentStart; prompt_id is
    the turn id, shared by every dispatch in the message), so an ordinal
    would be exactly as unpairable as the name was."""

    def test_two_dispatches_two_starts_all_declared(self, tmp_path: Path) -> None:
        _run(_pretooluse("general-purpose", tool_use_id="toolu_A"), tmp_path)
        _run(_pretooluse("general-purpose", tool_use_id="toolu_B"), tmp_path)
        _run(_subagent_start("a94a5d5448a23e359", "general-purpose"), tmp_path, script=STAMP)
        _run(_subagent_start("a4dae47be426023ec", "general-purpose"), tmp_path, script=STAMP)
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert census.returncode == 0, census.stdout
        assert "BLINDSPOT\tchecked=2 recognized=2 unrecognized=0" in census.stdout
        assert census.stdout.count("\tdeclared\n") == 2
        assert "undeclared=0" in census.stdout
        assert "EXPECTED_NO_START" not in census.stdout

    def test_partial_mechanization_leaves_a_deficit(self, tmp_path: Path) -> None:
        """ONE EXPECT row, TWO starts of that type. The N-of-type credit is
        what makes this visible; plain set membership would report a clean
        undeclared=0 and hide a half-working hook."""
        _run(_pretooluse("general-purpose", tool_use_id="toolu_A"), tmp_path)
        _run(_subagent_start("a94a5d5448a23e359", "general-purpose"), tmp_path, script=STAMP)
        _run(_subagent_start("a4dae47be426023ec", "general-purpose"), tmp_path, script=STAMP)
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert "undeclared=1" in census.stdout, census.stdout
        undeclared = _lib_call("expectations_undeclared", tmp_path, SESSION)
        assert "UNDECLARED\ta4dae47be426023ec\tgeneral-purpose" in undeclared.stdout
        assert "SUMMARY\tchecked=2 recognized=2 unrecognized=0 undeclared=1" in undeclared.stdout

    def test_two_same_second_rows_are_not_collapsed(self, tmp_path: Path) -> None:
        """The census drops EXACT-duplicate lines (nexus-3h0u6). Two
        same-type dispatches inside one second would be byte-identical
        without the tool_use_id field, silently under-counting the deficit."""
        _run(_pretooluse("general-purpose", tool_use_id="toolu_A"), tmp_path)
        _run(_pretooluse("general-purpose", tool_use_id="toolu_B"), tmp_path)
        rows = [ln for ln in _expfile(tmp_path).read_text().splitlines() if "\tEXPECT\t" in ln]
        assert len(rows) == 2
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert "expect=2" in census.stdout, census.stdout


class TestIdempotence:
    def test_double_registration_writes_one_row(self, tmp_path: Path) -> None:
        """Plugin hooks.json and a project settings.json may both register
        this script. Two firings of the SAME dispatch must compose to one
        row, or the deficit count is inflated into nonsense."""
        payload = _pretooluse(tool_use_id="toolu_same")
        _run(payload, tmp_path)
        _run(payload, tmp_path)
        rows = [ln for ln in _expfile(tmp_path).read_text().splitlines() if "\tEXPECT\t" in ln]
        assert len(rows) == 1, rows


class TestFailOpen:
    """A hook that errors must never block a legitimate dispatch. Exit 0 +
    empty stdout is how the harness is told 'no decision, proceed'."""

    def test_malformed_json_does_not_block(self, tmp_path: Path) -> None:
        proc = _run("{not json at all", tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_empty_stdin_does_not_block(self, tmp_path: Path) -> None:
        proc = _run("", tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_missing_session_id_does_not_block(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(session_id=""), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_path_traversal_session_id_does_not_block_or_escape(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(session_id="../../../etc/pwn"), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert not (tmp_path / "state" / "nexus" / "etc").exists()

    def test_unwritable_state_dir_does_not_block(self, tmp_path: Path) -> None:
        state = tmp_path / "state"
        state.mkdir()
        state.chmod(0o500)
        try:
            proc = _run(_pretooluse(), tmp_path)
            assert proc.returncode == 0
            assert proc.stdout == ""
        finally:
            state.chmod(0o700)

    def test_missing_lib_does_not_block(self, tmp_path: Path) -> None:
        """Source failure is the one path that cannot be exercised in
        place, so the script is copied somewhere its sibling lib is absent."""
        lone = tmp_path / "lone"
        lone.mkdir()
        copy = lone / SCRIPT.name
        copy.write_bytes(SCRIPT.read_bytes())
        proc = _run(_pretooluse(), tmp_path, script=copy)
        assert proc.returncode == 0
        assert proc.stdout == ""

    def test_empty_field_does_not_shift_the_parse(self, tmp_path: Path) -> None:
        """A missing subagent_type must record NOTHING — never a row whose
        name is the next field along.

        Found by mutation, not by inspection: the parse used ``IFS=$'\\t'
        read``, and tab is IFS *whitespace*, so bash COLLAPSES empty fields
        and shifts every later value one position left.

        HOW FAR THAT ACTUALLY GOT, stated precisely rather than talked up:
        with the REAL field order a single shift puts ``tool_use_id`` in
        the mode slot, the shellib rejects a mode that is not
        background|sync, and nothing is written — so the corrupt-row
        outcome needs a ``tool_use_id`` that is itself a valid mode, which
        the framework never generates. It is a latent parse defect, not a
        live one. What WAS live is subtler and worse for the suite: the
        fail-open tests were reaching their exits through the SHIFTED path
        (a payload with no session_id exited at the tool-name gate holding
        ``tool_name`` in its session slot), so mutating the guard they name
        left them green. That is why this test crafts the isolating input
        instead of relying on a realistic one — it pins the PARSE."""
        proc = _run(_pretooluse(subagent_type="", tool_use_id="background"), tmp_path)
        assert proc.returncode == 0
        assert not _expfile(tmp_path).exists(), (
            "field shift produced a row: " + _expfile(tmp_path).read_text()
        )

    def test_empty_session_id_does_not_shift_the_parse(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(session_id=""), tmp_path)
        assert proc.returncode == 0
        orch = tmp_path / "state" / "nexus" / "orchestration"
        assert not orch.exists() or not list(orch.glob("*.expectations"))

    def test_unknown_subagent_type_charset_does_not_block(self, tmp_path: Path) -> None:
        """A type outside the ledger's charset is refused by the lib. The
        dispatch must still proceed — unrecorded, never blocked."""
        proc = _run(_pretooluse(subagent_type="bad type/with slash"), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""


class TestPluginWiring:
    def test_registered_on_agent_pretooluse(self) -> None:
        hooks = json.loads((REPO_ROOT / "conexus" / "hooks" / "hooks.json").read_text())
        entries = hooks["hooks"].get("PreToolUse", [])
        matched = [
            entry for entry in entries
            if any("agent-dispatch-expect.sh" in h["command"] for h in entry.get("hooks", []))
        ]
        assert matched, "agent-dispatch-expect.sh not registered under PreToolUse"
        matcher = matched[0]["matcher"]
        assert "Agent" in matcher, f"matcher must fire on the Agent tool, got {matcher!r}"

    def test_shellib_parity_with_reference(self) -> None:
        assert PLUGIN_LIB.read_bytes() == LIB.read_bytes(), (
            "conexus/hooks/scripts/expectations.sh has drifted from "
            "tests/e2e/lib/expectations.sh — edit the reference, then copy it over"
        )

    def test_script_is_bash_clean(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10
        )
        assert proc.returncode == 0, proc.stderr

    def test_declared_in_pending_release_ledger(self) -> None:
        """conexus/ loads from the pinned release tag, so this hook is INERT
        until the next plugin release ships."""
        ledger = (REPO_ROOT / "conexus" / "PENDING_RELEASE.md").read_text()
        assert "agent-dispatch-expect.sh" in ledger
