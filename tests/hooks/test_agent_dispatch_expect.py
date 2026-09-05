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
    subagent_type: str | None = "conexus:code-review-expert",
    *,
    background: bool | None = True,
    tool_use_id: str = "toolu_01aaaaaaaaaaaaaaaaaaaaaa",
    tool_name: str = "Agent",
    session_id: str = SESSION,
) -> str:
    tool_input: dict[str, object] = {
        "description": "review the diff",
        "prompt": "You are reviewer-1. Review and SendMessage back.",
    }
    if subagent_type is not None:
        # None => key OMITTED entirely (the real omitted-type payload shape).
        # Pass "" instead to exercise the present-but-empty-string arm.
        tool_input["subagent_type"] = subagent_type
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


class TestDefaultSubagentType:
    """nexus-a795d: a dispatch payload lacking subagent_type still starts a
    general-purpose agent (the harness's own default), while the pre-fix
    hook exited on the ``-n "$SUBAGENT_TYPE"`` guard without writing a row —
    a silent ledger blindspot, or a FALSE undeclared accusation against a
    later real general-purpose dispatch that WAS declared, depending on
    session shape. Fix mirrors the file's own run_in_background default:
    ``str(ti.get("subagent_type") or "general-purpose")``. Both shapes
    reproduced live 2026-08-03 (probe START ac93416a2d9d417d9; prior-session
    anomaly adc4471cb0cbba237 re-diagnosed as this, not a nested dispatch)."""

    def test_omitted_subagent_type_records_general_purpose(self, tmp_path: Path) -> None:
        """The key omitted entirely — the real omitted-type payload shape."""
        _run(_pretooluse(subagent_type=None), tmp_path)
        row = _expfile(tmp_path).read_text()
        assert "\tEXPECT\tgeneral-purpose\tbackground\t" in row, row

    def test_empty_string_subagent_type_records_general_purpose(self, tmp_path: Path) -> None:
        """The key present but empty — same treatment as fully absent."""
        _run(_pretooluse(subagent_type=""), tmp_path)
        row = _expfile(tmp_path).read_text()
        assert "\tEXPECT\tgeneral-purpose\tbackground\t" in row, row

    def test_omitted_subagent_type_records_correct_sync_mode(self, tmp_path: Path) -> None:
        """The default applies to the name only — mode still comes from
        run_in_background, unaffected by the subagent_type default."""
        _run(_pretooluse(subagent_type=None, background=False), tmp_path)
        row = _expfile(tmp_path).read_text()
        assert "\tEXPECT\tgeneral-purpose\tsync\t" in row, row

    def test_real_subagent_type_is_unchanged(self, tmp_path: Path) -> None:
        """A dispatch that DOES declare a type must not be touched by the
        default — regression guard against the default swallowing real
        values."""
        _run(_pretooluse(subagent_type="conexus:code-review-expert"), tmp_path)
        row = _expfile(tmp_path).read_text()
        assert "\tEXPECT\tconexus:code-review-expert\tbackground\t" in row, row

    def test_missing_session_id_still_exits_silently_when_type_omitted(
        self, tmp_path: Path
    ) -> None:
        """The SESSION_ID guard is untouched by this fix: an omitted type
        does not make a missing session id start writing rows."""
        proc = _run(_pretooluse(subagent_type=None, session_id=""), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        orch = tmp_path / "state" / "nexus" / "orchestration"
        assert not orch.exists() or not list(orch.glob("*.expectations"))


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

    def test_hand_written_row_must_carry_the_colon_verbatim(self, tmp_path: Path) -> None:
        """The HAND-WRITTEN path, which is what agents use while the hook is inert.

        The colon charset is the whole difference between a row that
        pairs and one that cannot. This pins the POST-RELEASE contract:
        the installed copy at v6.18.1 still rejects ``:`` outright
        (rc=2, verified against the live install), so at the pinned tag
        ``recognized=0`` is structural — the start stamp writes
        ``agent_type`` WITH the colon while the installed recogniser
        refuses colon-bearing types. nexus-mk3tw / nexus-qc4p1 lift that,
        and this test is what stops it regressing once shipped.

        Two arms, one difference: the colon.
        """
        _lib_call(
            "expectations_expect", tmp_path, SESSION,
            "conexus:code-review-expert", "background",
        )
        _run(
            _subagent_start("a29d3cfdd53ae3e98", "conexus:code-review-expert"),
            tmp_path, script=STAMP,
        )
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert census.returncode == 0, f"colon form must pair: {census.stdout}"
        assert "BLINDSPOT\tchecked=1 recognized=1 unrecognized=0" in census.stdout

    def test_hand_written_row_sanitized_to_a_dash_does_NOT_pair(
        self, tmp_path: Path
    ) -> None:
        """The control arm. Sanitizing destroys the only shared key.

        This is the exact signature of the four "broken guard" sessions.
        At the pinned tag it is unavoidable; after the release it becomes
        avoidable and therefore worth pinning, so that a future
        re-narrowing of the charset shows up here rather than as another
        run of silently unrecognised dispatches.
        """
        proc = _lib_call(
            "expectations_expect", tmp_path, SESSION,
            "conexus-code-review-expert", "background",
        )
        assert proc.returncode == 0, "the dash form is accepted — that is the trap"
        _run(
            _subagent_start("a29d3cfdd53ae3e98", "conexus:code-review-expert"),
            tmp_path, script=STAMP,
        )
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert "BLINDSPOT\tchecked=1 recognized=0 unrecognized=1" in census.stdout
        assert "expected_no_start=1" in census.stdout

    def test_undispatched_start_is_still_flagged(self, tmp_path: Path) -> None:
        """Non-vacuity: with the dispatch hook NOT run, the same start row
        is NAMED as undeclared — per dispatch, by type (nexus-houpu) — not
        skipped as unrecognisable the way the five broken sessions were.
        The census is the report (rc=0, undeclared=1 in CLASSIFIED); the
        rc=2 verdict on the same ledger comes from expectations_undeclared."""
        _run(
            _subagent_start("a29d3cfdd53ae3e98", "conexus:code-review-expert"),
            tmp_path, script=STAMP,
        )
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert census.returncode == 0, census.stdout
        assert (
            "AGENT\ta29d3cfdd53ae3e98\tconexus:code-review-expert\tNO_TERMINAL\tundeclared"
            in census.stdout
        ), census.stdout
        assert "undeclared=1" in census.stdout
        assert "BLINDSPOT\tchecked=1 recognized=0 unrecognized=1" in census.stdout
        undeclared = _lib_call("expectations_undeclared", tmp_path, SESSION)
        assert undeclared.returncode == 2, "a 0-of-N session is a deficit, rc=2"
        assert "UNDECLARED\ta29d3cfdd53ae3e98\tconexus:code-review-expert" in undeclared.stdout


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

    def test_concurrent_double_registration_writes_one_row(self, tmp_path: Path) -> None:
        """The shape the sequential test cannot reach. The write-side guard
        is a bounded mkdir lock, so this is the case that can actually leak
        a duplicate (nexus-3h0u6 is the same lesson on the START side)."""
        import concurrent.futures

        payload = _pretooluse(tool_use_id="toolu_race")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            for fut in [pool.submit(_run, payload, tmp_path) for _ in range(8)]:
                assert fut.result().returncode == 0
        rows = [ln for ln in _expfile(tmp_path).read_text().splitlines() if "\tEXPECT\t" in ln]
        assert len(rows) == 1, rows

    def test_duplicate_rows_do_not_inflate_the_credit_pool(self, tmp_path: Path) -> None:
        """Belt to the lock's braces, and the one that actually matters.

        A duplicate EXPECT row is NOT the harmless nuisance a duplicate START
        row is: it inflates the N-of-type credit and MASKS an undeclared
        start. The write-side lock is bounded, so it cannot be the only
        defence — the READER dedupes by dispatch_id, where identity is
        unambiguous. Fixture writes the duplicate directly, so it holds
        regardless of whether the lock ever leaks one."""
        _run(_pretooluse("general-purpose", tool_use_id="toolu_A"), tmp_path)
        f = _expfile(tmp_path)
        dup = f.read_text().splitlines()[0].split("\t")
        dup[0] = "2099-01-01T00:00:00Z"          # different ts => not an exact-line dup
        f.write_text(f.read_text() + "\t".join(dup) + "\n")
        _run(_subagent_start("a94a5d5448a23e359", "general-purpose"), tmp_path, script=STAMP)
        _run(_subagent_start("a4dae47be426023ec", "general-purpose"), tmp_path, script=STAMP)
        census = _lib_call("expectations_census", tmp_path, SESSION)
        assert "undeclared=1" in census.stdout, (
            "a re-registered dispatch inflated the credit pool and masked an "
            "undeclared start:\n" + census.stdout
        )
        assert "expect=1" in census.stdout, census.stdout
        undeclared = _lib_call("expectations_undeclared", tmp_path, SESSION)
        assert "UNDECLARED\ta4dae47be426023ec\tgeneral-purpose" in undeclared.stdout

    def test_stale_lockdir_is_reaped(self, tmp_path: Path) -> None:
        """A lockdir left by a killed hook must not tax every later dispatch
        in the session with the full lock budget, forever."""
        import os
        import time

        _run(_pretooluse(tool_use_id="toolu_first"), tmp_path)
        lock = Path(str(_expfile(tmp_path)) + ".expect.lock")
        lock.mkdir()
        old = time.time() - 3600
        os.utime(lock, (old, old))
        started = time.time()
        proc = _run(_pretooluse(tool_use_id="toolu_second"), tmp_path)
        assert proc.returncode == 0
        assert not lock.exists(), "stale lockdir survived"
        assert time.time() - started < 1.0, "paid the full lock budget on a stale lock"
        rows = [ln for ln in _expfile(tmp_path).read_text().splitlines() if "\tEXPECT\t" in ln]
        assert len(rows) == 2, rows


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
        """An empty subagent_type must default to general-purpose (nexus-a795d)
        AT ITS OWN FIELD POSITION — never shift, and never surface the next
        field's value as the name.

        Found by mutation, not by inspection: the parse used ``IFS=$'\\t'
        read``, and tab is IFS *whitespace*, so bash COLLAPSES empty fields
        and shifts every later value one position left. ``tool_use_id`` is
        deliberately set to the string ``"background"`` here — itself a
        valid dispatch-mode value — so that a shift bug would produce a
        row that LOOKS plausible (mode="background", i.e. the shifted
        tool_use_id lands where mode is expected) instead of failing loud.
        The delimiter is ``\\x1f`` (non-whitespace), which does not
        collapse empty fields, so no shift occurs and the row carries the
        DEFAULTED name in the subagent_type slot with the crafted
        tool_use_id still in the dispatch_id slot, unshifted."""
        proc = _run(_pretooluse(subagent_type="", tool_use_id="background"), tmp_path)
        assert proc.returncode == 0
        row = _expfile(tmp_path).read_text()
        assert "\tEXPECT\tgeneral-purpose\tbackground\t" in row, row
        fields = row.strip().split("\t")
        assert fields[4] == "background", (
            "dispatch_id must still carry the crafted tool_use_id, unshifted: " + row
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


#: VERBATIM captured PreToolUse payloads (2026-07-31), one sync dispatch and
#: one background dispatch, exactly as the harness emitted them — including
#: the ``effort`` object and the absence of ``agent_id``. Only the free-text
#: prompt is shortened; every structural field is untouched. Hand-built
#: fixtures encode what the author BELIEVES the payload looks like, which is
#: the assumption this whole change was required to stop making.
CAPTURED_PAYLOADS = [
    {
        "session_id": "e9a2c670-c452-408a-b796-a5fb5c2ede7e",
        "transcript_path": "/tmp/probe/t.jsonl",
        "cwd": "/private/tmp/nx-pretooluse-probe/work",
        "prompt_id": "f7f8b968-d0e7-4959-a7f5-12ee29a5608d",
        "permission_mode": "acceptEdits",
        "effort": {"level": "high"},
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "description": "probe-sync-a",
            "prompt": "Reply with exactly SUBDONE and finish.",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        },
        "tool_use_id": "toolu_01FP7i2pqZf83DZP3ZZ7aWbB",
    },
    {
        "session_id": "d3516804-d87c-4620-b0b4-303661fe850d",
        "transcript_path": "/tmp/probe/t.jsonl",
        "cwd": "/private/tmp/nx-pretooluse-probe/work",
        "prompt_id": "59a61524-cca7-44ef-a162-46b1352ab1af",
        "permission_mode": "acceptEdits",
        "effort": {"level": "high"},
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "description": "probe-bg-b",
            "prompt": "Reply with exactly BGDONE and finish.",
            "subagent_type": "general-purpose",
            "run_in_background": True,
        },
        "tool_use_id": "toolu_01528yfAkZL3w8Wt5LNuK1rm",
    },
]


class TestSkippedWriteIsLoud:
    """nexus-mqnkt: session 49d1c3ab (2026-09-01) left a START row with no
    matching EXPECT row and ZERO forensic trace — every skip path here used
    to exit 0 with nothing on stdout OR stderr, so a genuinely dropped
    write was indistinguishable from the hook never firing at all. The
    write-side investigation (the real ledger, still on disk, read
    directly: 51 EXPECT rows, all distinct non-empty dispatch_ids, zero
    duplicate lines — general-purpose showed 19 STARTs against 18 EXPECT
    rows, and the orphaned START was the session's very first dispatch,
    03:25:40Z, ~7 hours before the next EXPECT row) could not determine
    WHICH of this hook's fail-open branches actually fired that day: the
    hook's own design left no residue to distinguish a mode-gated skip, a
    malformed payload, an expectations_file rejection, or the harness never
    invoking the hook at all for the session's first tool call. This class
    does not claim to reproduce THAT incident — it verifies the concrete,
    checkable deliverable instead: every skip path that represents a write
    the hook could have attempted, but did not, now names itself on
    stderr, so a recurrence is diagnosable rather than invisible. Stdout
    stays empty on every one of these (unchanged: PreToolUse stdout is
    parsed by the harness, stderr is not, so this adds no risk of ever
    blocking a dispatch)."""

    def test_missing_session_id_is_named_on_stderr(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(session_id=""), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert "session_id" in proc.stderr
        assert "NOT written" in proc.stderr

    def test_path_traversal_session_id_is_named_on_stderr(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(session_id="../../../etc/pwn"), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert not (tmp_path / "state" / "nexus" / "etc").exists()
        assert "expectations_file rejected" in proc.stderr
        assert "../../../etc/pwn" in proc.stderr

    def test_missing_lib_is_named_on_stderr(self, tmp_path: Path) -> None:
        lone = tmp_path / "lone"
        lone.mkdir()
        copy = lone / SCRIPT.name
        copy.write_bytes(SCRIPT.read_bytes())
        proc = _run(_pretooluse(), tmp_path, script=copy)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert "could not source" in proc.stderr
        assert "expectations.sh" in proc.stderr

    def test_unexpected_tool_name_is_named_on_stderr(self, tmp_path: Path) -> None:
        proc = _run(_pretooluse(tool_name="Bash"), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert "tool_name 'Bash'" in proc.stderr
        assert "NOT written" in proc.stderr

    def test_expectations_expect_validation_error_passes_through_to_stderr(
        self, tmp_path: Path,
    ) -> None:
        """A subagent_type outside the agent-type charset makes
        expectations_expect refuse the row; its ERROR line must reach the
        hook's stderr instead of being swallowed (the fourth skip path)."""
        proc = _run(_pretooluse(subagent_type="bad type!"), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert "expectations_expect: ERROR" in proc.stderr
        assert "invalid name" in proc.stderr

    def test_explicit_off_mode_prints_nothing_anywhere(self, tmp_path: Path) -> None:
        """A deliberate NX_ORCH_STOP_GUARD=off opt-out is not a failure —
        no diagnostic is owed, on stdout OR stderr."""
        proc = _run(_pretooluse(), tmp_path, mode="off")
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert proc.stderr == ""

    def test_successful_write_prints_nothing_on_stderr_either(
        self, tmp_path: Path
    ) -> None:
        """Control: the new diagnostics must be scoped to the skip paths —
        a real write stays completely silent, both streams."""
        proc = _run(_pretooluse(), tmp_path)
        assert proc.returncode == 0
        assert proc.stdout == ""
        assert proc.stderr == ""
        assert "\tEXPECT\t" in _expfile(tmp_path).read_text()


class TestCapturedPayloads:
    """Replay the real bytes. Everything else in this file is a fixture the
    author wrote; this class is the only part that cannot drift from what the
    harness actually sends."""

    def test_captured_sync_dispatch_records_sync(self, tmp_path: Path) -> None:
        payload = CAPTURED_PAYLOADS[0]
        proc = _run(json.dumps(payload), tmp_path)
        assert proc.returncode == 0 and proc.stdout == ""
        row = _expfile(tmp_path, payload["session_id"]).read_text()
        assert row.split("\t")[1:] == [
            "EXPECT", "general-purpose", "sync", "toolu_01FP7i2pqZf83DZP3ZZ7aWbB\n",
        ], row

    def test_captured_background_dispatch_records_background(self, tmp_path: Path) -> None:
        payload = CAPTURED_PAYLOADS[1]
        proc = _run(json.dumps(payload), tmp_path)
        assert proc.returncode == 0 and proc.stdout == ""
        row = _expfile(tmp_path, payload["session_id"]).read_text()
        assert "\tEXPECT\tgeneral-purpose\tbackground\t" in row, row

    def test_captured_payloads_carry_no_agent_id(self) -> None:
        """The reason the EXPECT row cannot be keyed on an agent id: at
        PreToolUse the agent does not exist yet. If a future harness starts
        sending one, this fails and the pairing design should be revisited."""
        for payload in CAPTURED_PAYLOADS:
            assert "agent_id" not in payload

    def test_captured_payloads_carry_no_name_parameter(self) -> None:
        """The root cause of nexus-nu7fo, pinned against real bytes."""
        for payload in CAPTURED_PAYLOADS:
            assert set(payload["tool_input"]) == {
                "description", "prompt", "subagent_type", "run_in_background",
            }, payload["tool_input"]


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
        until the next plugin release ships — the declaration duty ends the
        moment a release advances the pin past it (the 7.0.0 cut emptied the
        ledger; the release-window predicate is the drift ledger's)."""
        ledger = (REPO_ROOT / "conexus" / "PENDING_RELEASE.md").read_text()
        pin = json.loads(
            (REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )["plugins"][0]["source"]["ref"]
        from plugin_channel import client_version_of

        version = client_version_of(pin)
        assert version is not None, (
            f"marketplace pin {pin!r} matches neither invariant-R ref shape"
        )
        if tuple(int(x) for x in version.split(".")) >= (7, 0, 0):
            # Shipped at 7.0.0: the hook lives in the pinned tag now. The
            # ledger owes an entry only if the file drifts AGAIN, which the
            # drift-ledger tests enforce generically.
            return
        assert "agent-dispatch-expect.sh" in ledger
