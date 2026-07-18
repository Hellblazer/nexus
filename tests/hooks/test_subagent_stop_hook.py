# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the SubagentStop orchestration-guard hook (RDR-184 Gap 1, bead
nexus-ccs9v.9).

The hook consults the P1.1 expectations file (bead nexus-ccs9v.7): agents NOT
listed are NEVER blocked; listed (named, background) agents are blocked at
most once when their transcript shows no SendMessage report. Ships
DEFAULT-OFF: NX_ORCH_STOP_GUARD = off (default) | observe | block.
Every uncertain path fails OPEN (never block on missing evidence).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-stop.sh"
PLUGIN_EXPECTATIONS = REPO_ROOT / "conexus" / "hooks" / "scripts" / "expectations.sh"
REFERENCE_EXPECTATIONS = REPO_ROOT / "tests" / "e2e" / "lib" / "expectations.sh"

SESSION = "sess-testorch"
NAME = "worker-a"
AGENT_ID = f"a{NAME}-6f59dab8bbb14864"


def _payload(
    *,
    session_id: str = SESSION,
    agent_id: str = AGENT_ID,
    agent_type: str = NAME,
    transcript: str = "",
    stop_hook_active: bool = False,
) -> str:
    return json.dumps(
        {
            "session_id": session_id,
            "hook_event_name": "SubagentStop",
            "agent_id": agent_id,
            "agent_type": agent_type,
            "agent_transcript_path": transcript,
            "stop_hook_active": stop_hook_active,
        }
    )


def _transcript(tmp_path: Path, *, with_sendmessage: bool) -> Path:
    """A minimal agent transcript JSONL, optionally containing a SendMessage
    tool_use (shaped like real Claude Code transcript entries)."""
    lines = [
        {"type": "user", "message": {"role": "user", "content": "do the thing"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "true"}}
                ],
            },
        },
    ]
    if with_sendmessage:
        lines.append(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t2",
                            "name": "SendMessage",
                            "input": {"to": "main", "content": "done: report"},
                        }
                    ],
                },
            }
        )
    lines.append(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "finished"}]},
        }
    )
    p = tmp_path / "agent_transcript.jsonl"
    p.write_text("\n".join(json.dumps(entry) for entry in lines) + "\n")
    return p


def _run_hook(
    stdin: str,
    tmp_path: Path,
    *,
    mode: str | None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    env.pop("NX_ORCH_STOP_GUARD", None)
    if mode is not None:
        env["NX_ORCH_STOP_GUARD"] = mode
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _expectations_file(tmp_path: Path, session_id: str = SESSION) -> Path:
    return tmp_path / "state" / "nexus" / "orchestration" / f"{session_id}.expectations"


def _expect_row(tmp_path: Path, name: str = NAME, mode: str = "background", session_id: str = SESSION) -> None:
    f = _expectations_file(tmp_path, session_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(f"2026-07-17T00:00:00Z\tEXPECT\t{name}\t{mode}\n")


def _decision(proc: subprocess.CompletedProcess[str]) -> dict | None:
    """Parse a {"decision": ...} JSON object from hook stdout, if any."""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "decision" in obj:
            return obj
    return None


class TestDefaultMode:
    def test_unset_mode_blocks_owing_agent(self, tmp_path: Path) -> None:
        """DEFAULT-ON (P1.G flipped 2026-07-17, bead .15): with
        NX_ORCH_STOP_GUARD unset, an owing unreported agent IS blocked."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode=None)
        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block"

    def test_unset_mode_still_failopen_for_unlisted(self, tmp_path: Path) -> None:
        """Default-ON must not change the fail-open floor: no EXPECT row =>
        no block, even with the guard defaulted on."""
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode=None)
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_explicit_off(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="off")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_unknown_mode_is_off(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="banana")
        assert proc.returncode == 0
        assert _decision(proc) is None


class TestBlockMode:
    def test_owing_unreported_agent_is_blocked_with_reason(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0, proc.stderr
        decision = _decision(proc)
        assert decision is not None, f"expected a block decision, stdout: {proc.stdout!r}"
        assert decision["decision"] == "block"
        assert "SendMessage" in decision["reason"]

    def test_block_records_blocked_row(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        content = _expectations_file(tmp_path).read_text()
        assert f"\tBLOCKED\t{AGENT_ID}\n" in content

    def test_stop_hook_active_never_reblocks(self, tmp_path: Path) -> None:
        """21c round-trip guard: the re-fired stop after a block carries
        stop_hook_active=true and must pass through untouched."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(transcript=str(t), stop_hook_active=True), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_blocked_row_suppresses_second_block(self, tmp_path: Path) -> None:
        """Belt to stop_hook_active's braces: a pre-existing BLOCKED row for
        this agent_id suppresses any further block."""
        _expect_row(tmp_path)
        f = _expectations_file(tmp_path)
        with f.open("a") as fh:
            fh.write(f"2026-07-17T00:00:01Z\tBLOCKED\t{AGENT_ID}\n")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_unlisted_agent_never_blocked(self, tmp_path: Path) -> None:
        """Sync dispatches stay unblockable by construction: no EXPECT row =>
        no block, regardless of transcript content."""
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_sync_expect_row_not_blocked(self, tmp_path: Path) -> None:
        _expect_row(tmp_path, mode="sync")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_unnamed_morphology_not_blocked(self, tmp_path: Path) -> None:
        """An unnamed dispatch (agent_id 'a<hash>', agent_type = subagent_type)
        must never match — even when an EXPECT background row exists for a
        name equal to its subagent_type (scenario-27 collision class)."""
        _expect_row(tmp_path, name="general-purpose")
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(agent_id="a16b397f79df79c42", agent_type="general-purpose", transcript=str(t)),
            tmp_path,
            mode="block",
        )
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_reported_agent_not_blocked(self, tmp_path: Path) -> None:
        """A SendMessage tool_use in the agent transcript counts as the
        report — no block."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_reported_agent_gets_reported_row(self, tmp_path: Path) -> None:
        """The found-report path records a REPORTED row — the .11 missed-block
        census needs it (EXPECT x REPORTED x WOULDBLOCK; critic S1)."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        content = _expectations_file(tmp_path).read_text()
        assert f"\tREPORTED\t{AGENT_ID}\n" in content

    def test_sendmessage_inside_tool_result_does_not_count(self, tmp_path: Path) -> None:
        """A SendMessage-shaped tool_use embedded in a tool_result (e.g. the
        agent READ a transcript containing one) is not the agent's own
        report — only assistant-message tool_use blocks count (critic S1
        compounding factor, reproduced pre-fix)."""
        _expect_row(tmp_path)
        p = tmp_path / "agent_transcript.jsonl"
        entry = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t9",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "embedded",
                                "name": "SendMessage",
                                "input": {"to": "main", "content": "decoy from a read file"},
                            }
                        ],
                    }
                ],
            },
        }
        p.write_text(json.dumps(entry) + "\n")
        proc = _run_hook(_payload(transcript=str(p)), tmp_path, mode="block")
        assert proc.returncode == 0
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block"

    def test_sendmessage_as_text_mention_does_not_count(self, tmp_path: Path) -> None:
        """Merely SAYING the word SendMessage in text is not a report — only
        a tool_use block named SendMessage counts."""
        _expect_row(tmp_path)
        p = tmp_path / "agent_transcript.jsonl"
        entry = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "I could use SendMessage but will not"}],
            },
        }
        p.write_text(json.dumps(entry) + "\n")
        proc = _run_hook(_payload(transcript=str(p)), tmp_path, mode="block")
        assert proc.returncode == 0
        decision = _decision(proc)
        assert decision is not None and decision["decision"] == "block"


class TestFailOpen:
    def test_directory_transcript_fails_open(self, tmp_path: Path) -> None:
        """A directory-shaped agent_transcript_path passes -r but crashes a
        naive open(); the crash must fail OPEN (no block), never fall
        through to the block branch (critic S2, reproduced pre-fix)."""
        _expect_row(tmp_path)
        d = tmp_path / "transcript_dir"
        d.mkdir()
        proc = _run_hook(_payload(transcript=str(d)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_missing_transcript_fails_open(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        proc = _run_hook(
            _payload(transcript=str(tmp_path / "nope.jsonl")), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_empty_transcript_path_fails_open(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        proc = _run_hook(_payload(transcript=""), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_junk_stdin_fails_open(self, tmp_path: Path) -> None:
        proc = _run_hook("this is not json {", tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_missing_expectations_file_fails_open(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="block")
        assert proc.returncode == 0
        assert _decision(proc) is None

    def test_traversal_session_id_fails_open(self, tmp_path: Path) -> None:
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(
            _payload(session_id="../../evil", transcript=str(t)), tmp_path, mode="block"
        )
        assert proc.returncode == 0
        assert _decision(proc) is None


class TestObserveMode:
    def test_observe_never_blocks_but_records(self, tmp_path: Path) -> None:
        """Observe mode is the .11 measurement vehicle: no decision output,
        but a WOULDBLOCK row lands in the expectations file."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        proc = _run_hook(_payload(transcript=str(t)), tmp_path, mode="observe")
        assert proc.returncode == 0
        assert _decision(proc) is None
        content = _expectations_file(tmp_path).read_text()
        assert f"\tWOULDBLOCK\t{AGENT_ID}\n" in content

    def test_observe_does_not_mark_blocked(self, tmp_path: Path) -> None:
        """A WOULDBLOCK observation must not consume the real once-guard: no
        BLOCKED row from observe mode."""
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=False)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="observe")
        content = _expectations_file(tmp_path).read_text()
        assert "\tBLOCKED\t" not in content

    def test_observe_reported_agent_records_nothing(self, tmp_path: Path) -> None:
        _expect_row(tmp_path)
        t = _transcript(tmp_path, with_sendmessage=True)
        _run_hook(_payload(transcript=str(t)), tmp_path, mode="observe")
        content = _expectations_file(tmp_path).read_text()
        assert "\tWOULDBLOCK\t" not in content


class TestPluginWiring:
    def test_shellib_parity_with_reference(self) -> None:
        """The plugin ships a COPY of the reference shellib (plugin surface
        rides a release; tests/e2e/lib is the reference implementation +
        test bed). Byte-identity is the drift tripwire — same pattern as the
        version-lockstep manifests."""
        assert PLUGIN_EXPECTATIONS.exists(), "plugin copy of expectations.sh missing"
        assert PLUGIN_EXPECTATIONS.read_bytes() == REFERENCE_EXPECTATIONS.read_bytes(), (
            "conexus/hooks/scripts/expectations.sh has drifted from "
            "tests/e2e/lib/expectations.sh — edit the reference, then copy it over"
        )

    def test_registered_in_hooks_json(self) -> None:
        hooks = json.loads((REPO_ROOT / "conexus" / "hooks" / "hooks.json").read_text())
        subagent_stop = hooks["hooks"].get("SubagentStop", [])
        commands = [
            h["command"]
            for entry in subagent_stop
            for h in entry.get("hooks", [])
        ]
        assert any("subagent-stop.sh" in c for c in commands), (
            "subagent-stop.sh not registered under SubagentStop in hooks.json"
        )

    def test_script_is_bash_clean(self) -> None:
        proc = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10
        )
        assert proc.returncode == 0, proc.stderr
