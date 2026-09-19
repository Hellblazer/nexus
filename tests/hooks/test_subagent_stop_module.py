# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ported SubagentStop guard and its two scans (RDR-215 bead nexus-q02nx.12).

``test_subagent_stop_hook.py`` owns the decision table and drives both
implementations through a child process, for the reasons its ``_PY_DRIVER``
comment gives. This file owns the three things that harness cannot reach:

* the block REASON text, checked against the script's own bytes rather than
  against a copy someone retyped into the port;
* the two scans as functions, including the verdict tokens the bash caller's
  ``case`` arms matched;
* the no-spawn claim the tier change actually makes -- asserted in-process,
  since a child-process harness cannot evidence it.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus._hook_runtime._io import stop_decision
from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER
from nexus.hooks import expectations
from nexus.hooks import subagent_stop as hook
from nexus.hooks import subagent_stop_scans as scans
from nexus.mcp.core import _mcp_tool_error

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-stop.sh"
SCAN = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-stop-scan.py"
WRITES_SCAN = REPO_ROOT / "conexus" / "hooks" / "scripts" / "subagent-stop-writes-scan.py"


class TestTheReasonTextMatchesTheScript:
    """The block reason is the guard's entire user-visible surface.

    Every assertion here reads the CURRENT script text and compares it to the
    module constant. A retyped copy in the test would let the two drift
    together while the test stayed green, which is the inert-guard shape this
    repo keeps paying for. When the script is finally deleted this class goes
    with it -- by then the port IS the text.
    """

    @staticmethod
    def _script() -> str:
        return SCRIPT.read_text()

    def test_the_owes_reason_is_byte_identical(self):
        m = re.search(r'^REASON="(You are the named.*?)"$', self._script(), re.M)
        assert m, "the script no longer assigns REASON from a literal"
        expected = m.group(1).replace("${AGENT_TYPE}", "{agent_type}")
        assert hook._OWES_REASON == expected

    def test_the_lock_exhausted_note_is_byte_identical(self):
        # ^\s* because this assignment sits inside the `if` that appends
        # the note; an anchored ^REASON matched nothing and the test said
        # "the script no longer appends" when the script was fine.
        m = re.search(
            r'^\s*REASON="\$\{REASON\}( \(NOTE:.*?)"$', self._script(), re.M
        )
        assert m, "the script no longer appends the lock-exhausted note"
        assert hook._LOCK_EXHAUSTED_NOTE == m.group(1)

    def test_the_unlanded_reason_is_byte_identical(self):
        m = re.search(r'^\s+"(You sent your completion report.*?)"$', self._script(), re.M)
        assert m, "the script no longer emits the unlanded-write reason"
        expected = m.group(1).replace("${WRITES_VERDICT#UNLANDED }", "{detail}")
        assert hook._UNLANDED_REASON == expected


class TestTheEnvelopeIsRenderedNotPrintfd:
    """The port builds its envelope with json.dumps where the bash used
    printf with a raw ``%s``. The bytes must not move for any reachable
    input; the difference is only that correctness stops depending on a
    charset guard two calls away."""

    def test_the_bytes_match_the_scripts_printf_format(self):
        assert (
            stop_decision("block", reason="hello")
            == '{"decision": "block", "reason": "hello"}'
        )

    def test_a_quote_bearing_type_is_unreachable_not_merely_escaped(self):
        """Honest scoping. A type with a quote WOULD have produced malformed
        JSON from the bash printf, but it can never reach the envelope:
        expectations_owes_report's charset guard refuses it first and the
        verdict is "does not owe". So this is not a fixed bug, and the port
        must not be described as fixing one."""
        verdict = expectations.expectations_owes_report(
            "sess-quote", "a1", 'bad"type'
        )
        assert verdict.owes is False


class TestTheReportScan:
    def _transcript(self, tmp_path: Path, entries: list[dict]) -> Path:
        p = tmp_path / "t.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return p

    def _assistant_tool_use(self, name: str) -> dict:
        return {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "id": "t1", "name": name}]},
        }

    @pytest.mark.parametrize("tool", ["SendMessage", "SubagentHandback"])
    def test_either_report_tool_is_found(self, tmp_path, tool):
        t = self._transcript(tmp_path, [self._assistant_tool_use(tool)])
        assert scans.report_verdict(str(t)) == "FOUND"

    def test_no_report_is_notfound(self, tmp_path):
        t = self._transcript(tmp_path, [self._assistant_tool_use("Bash")])
        assert scans.report_verdict(str(t)) == "NOTFOUND"

    def test_a_sendmessage_the_agent_merely_read_is_not_its_report(self, tmp_path):
        """Scoped to ASSISTANT tool_use blocks: SendMessage-shaped JSON
        nested in a tool_result is something the agent READ."""
        t = self._transcript(
            tmp_path,
            [
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "x",
                                "content": '{"name": "SendMessage"}',
                            }
                        ]
                    },
                }
            ],
        )
        assert scans.report_verdict(str(t)) == "NOTFOUND"

    def test_a_missing_transcript_is_skip_not_notfound(self, tmp_path):
        """SKIP and NOTFOUND both fail open at the caller, but they mean
        different things to anyone reading a ledger beside a transcript."""
        assert scans.report_verdict(str(tmp_path / "nope.jsonl")) == "SKIP"
        assert scans.report_verdict("") == "SKIP"

    def test_a_directory_is_skip(self, tmp_path):
        """The bash header names a readable DIRECTORY as a real crash input."""
        assert scans.report_verdict(str(tmp_path)) == "SKIP"

    def test_a_junk_line_does_not_void_the_scan(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_text(
            "not json at all\n" + json.dumps(self._assistant_tool_use("SendMessage")) + "\n"
        )
        assert scans.report_verdict(str(p)) == "FOUND"


class TestTheWritesScan:
    def _transcript(self, tmp_path: Path, entries: list[dict]) -> Path:
        p = tmp_path / "w.jsonl"
        p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return p

    def _call(self, tid: str, name: str, tool_input: dict | None = None) -> dict:
        return {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tid,
                        "name": name,
                        "input": tool_input or {},
                    }
                ]
            },
        }

    def _result(self, tid: str, content, is_error: bool | None = None) -> dict:
        block: dict = {"type": "tool_result", "tool_use_id": tid, "content": content}
        if is_error is not None:
            block["is_error"] = is_error
        return {"type": "user", "message": {"content": [block]}}

    def test_a_failed_write_is_unlanded(self, tmp_path):
        t = self._transcript(
            tmp_path,
            [
                self._call("w1", "mcp__plugin_conexus_nexus__memory_put"),
                self._result("w1", "Error: something broke"),
            ],
        )
        assert scans.writes_verdict(str(t)) == "UNLANDED 1 memory_put"

    def test_the_failure_prefix_comes_from_production_code(self, tmp_path):
        """Not a copied string. If ``_mcp_tool_error``'s prefix ever moves,
        this goes red beside the scan rather than passing while the scan
        silently matches nothing in production."""
        # Argument order is (tool, exception) -- and getting it backwards
        # STILL produced a string starting "Error: ", so this assertion
        # passed while testing nothing realistic. The marker below is the
        # literal 2026-08-25 outage condition, which is the point.
        produced = _mcp_tool_error(
            "memory_put", RuntimeError(SESSION_UNAUTHORIZED_MARKER)
        )
        assert produced.startswith("Error:")
        t = self._transcript(
            tmp_path,
            [
                self._call("w1", "mcp__plugin_conexus_nexus__memory_put"),
                self._result("w1", produced),
            ],
        )
        assert scans.writes_verdict(str(t)).startswith("UNLANDED 1 ")

    def test_a_successful_write_is_clean(self, tmp_path):
        t = self._transcript(
            tmp_path,
            [
                self._call("w1", "mcp__plugin_conexus_nexus__memory_put"),
                self._result("w1", "Stored: [1] p/t"),
            ],
        )
        assert scans.writes_verdict(str(t)) == "CLEAN"

    def test_a_non_write_tool_is_never_counted(self, tmp_path):
        t = self._transcript(
            tmp_path,
            [
                self._call("r1", "mcp__plugin_conexus_nexus__memory_get"),
                self._result("r1", "Error: nope"),
            ],
        )
        assert scans.writes_verdict(str(t)) == "CLEAN"

    @pytest.mark.parametrize(
        "action,expected",
        [("put", "UNLANDED 1 scratch"), ("search", "CLEAN"), ("list", "CLEAN")],
    )
    def test_scratch_counts_only_as_a_write(self, tmp_path, action, expected):
        t = self._transcript(
            tmp_path,
            [
                self._call("w1", "scratch", {"action": action}),
                self._result("w1", "Error: nope"),
            ],
        )
        assert scans.writes_verdict(str(t)) == expected

    def test_is_error_true_counts_without_the_prefix(self, tmp_path):
        t = self._transcript(
            tmp_path,
            [
                self._call("w1", "store_put"),
                self._result("w1", "anything at all", is_error=True),
            ],
        )
        assert scans.writes_verdict(str(t)) == "UNLANDED 1 store_put"

    def test_a_missing_transcript_is_clean_not_an_error(self, tmp_path):
        """Positive evidence only: absence must never read as a failure,
        which is what keeps this compatible with the hook's fail-open
        contract."""
        assert scans.writes_verdict(str(tmp_path / "nope.jsonl")) == "CLEAN"

    def test_two_failures_across_two_tools_are_named_sorted(self, tmp_path):
        t = self._transcript(
            tmp_path,
            [
                self._call("w1", "store_put"),
                self._result("w1", "Error: a"),
                self._call("w2", "memory_put"),
                self._result("w2", "Error: b"),
            ],
        )
        assert scans.writes_verdict(str(t)) == "UNLANDED 2 memory_put,store_put"


class TestBothScansAgreeWithTheScriptsTheyReplace:
    """The scripts are still on disk and still invoked by the live bash
    hook, so every verdict must match. Compared as VERDICT STRINGS, which
    is what the callers matched on."""

    def _run(self, script: Path, path: str) -> str:
        return subprocess.run(
            [sys.executable, str(script), path],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()

    def _write(self, tmp_path: Path, entries: list[dict], name: str) -> Path:
        p = tmp_path / name
        p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return p

    @pytest.mark.parametrize(
        "entries,label",
        [
            ([{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "SendMessage"}]}}], "reported"),
            ([{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "SubagentHandback"}]}}], "handback"),
            ([{"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "Bash"}]}}], "silent"),
            ([], "empty transcript"),
        ],
    )
    def test_the_report_verdict_matches(self, tmp_path, entries, label):
        p = self._write(tmp_path, entries, "r.jsonl")
        assert scans.report_verdict(str(p)) == self._run(SCAN, str(p)), label

    @pytest.mark.parametrize(
        "entries,label",
        [
            ([
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "w", "name": "memory_put", "input": {}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "w", "content": "Error: x"}]}},
            ], "one failed write"),
            ([
                {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": "w", "name": "store_put", "input": {}}]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "w", "content": "Stored: x"}]}},
            ], "one clean write"),
            ([], "empty transcript"),
        ],
    )
    def test_the_writes_verdict_matches(self, tmp_path, entries, label):
        p = self._write(tmp_path, entries, "w.jsonl")
        assert scans.writes_verdict(str(p)) == self._run(WRITES_SCAN, str(p)), label


class TestTheNoSpawnClaim:
    """The reason this bead exists.

    SubagentStop has a 10 s timeout, and the script's own header documents a
    routine load-correlated SIGKILL from the harness -- despite a measured
    97 MB transcript scanning in 0.14 s. The scanning was never the problem;
    the process tree was. Each bash invocation spawned bash, sourced the
    ledger library, then spawned python3 TWICE more for the two scans.

    This is the only place that can evidence the removal, because
    ``test_subagent_stop_hook.py`` drives the port through a child process
    on purpose.
    """

    def _big_transcript(self, tmp_path: Path, target_bytes: int = 8_000_000) -> Path:
        """A transcript with no report in it, so the scan must read every
        line rather than short-circuiting on an early match."""
        p = tmp_path / "big.jsonl"
        line = json.dumps(
            {
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "t", "name": "Bash",
                     "input": {"command": "x" * 400}}
                ]},
            }
        ) + "\n"
        with p.open("w", encoding="utf-8") as fh:
            written = 0
            while written < target_bytes:
                fh.write(line)
                written += len(line)
        return p

    def test_both_scans_read_a_large_transcript_well_inside_the_budget(self, tmp_path):
        p = self._big_transcript(tmp_path)
        assert p.stat().st_size > 5_000_000

        started = time.monotonic()
        assert scans.report_verdict(str(p)) == "NOTFOUND"
        assert scans.writes_verdict(str(p)) == "CLEAN"
        elapsed = time.monotonic() - started

        # The hook timeout is 10s and BOTH scans run on the blocking path.
        # Sized against a false positive on a loaded box rather than tuned
        # just above the observed time: this bounds a pathology, not
        # performance.
        assert elapsed < 5.0, (
            f"both scans over {p.stat().st_size} bytes took {elapsed:.2f}s; "
            "the SubagentStop budget is 10s for everything"
        )

    def test_the_hook_spawns_no_child_process(self, monkeypatch, tmp_path):
        """The actual claim. Not 'it is faster' -- 'there is no process'.

        Both scans were separate ``python3`` invocations and the ledger was a
        sourced bash library; on the tool tier the whole decision is one
        function call. A regression that reintroduced a subprocess would be
        invisible to every timing assertion on an idle box and would fail in
        production exactly when the box is loaded, which is the failure this
        bead is fixing.
        """
        spawns: list = []
        scans_called: list[str] = []
        real_popen = subprocess.Popen

        for _name in ("report_verdict", "writes_verdict"):
            _real = getattr(hook, _name)

            def _watch(name=_name, real=_real):
                def _call(*a, **k):
                    scans_called.append(name)
                    return real(*a, **k)
                return _call

            monkeypatch.setattr(hook, _name, _watch())

        def _record(*args, **kwargs):
            spawns.append(args[0] if args else kwargs.get("args"))
            return real_popen(*args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", _record)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
        monkeypatch.setenv("NX_ORCH_STOP_GUARD", "block")

        # SEED A GENUINELY-OWING LEDGER, or this test does not reach the
        # thing it is named for. Without an EXPECT/START pair,
        # expectations_owes_report returns False on its first readable-rows
        # check and run() returns BEFORE calling report_verdict or
        # writes_verdict -- the two functions that used to be separate
        # python3 spawns and are the entire subject of the no-spawn claim.
        # Measured at the bead .16 critique: the earlier version of this
        # test called them ZERO times while its own docstring called itself
        # "the actual claim". A regression reintroducing a subprocess
        # inside either scan would have passed it untouched.
        ledger = tmp_path / "state" / "nexus" / "orchestration"
        ledger.mkdir(parents=True)
        (ledger / "sess-nospawn.expectations").write_text(
            "2026-09-19T00:00:00Z\tEXPECT\tworker\tbackground\td1\n"
            "2026-09-19T00:00:01Z\tSTART\ta-nospawn\tworker\n"
        )

        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "t", "name": "Bash"}]}}) + "\n"
        )
        hook.run(
            {
                "session_id": "sess-nospawn",
                "agent_id": "a-nospawn",
                "agent_type": "worker",
                "agent_transcript_path": str(transcript),
                "stop_hook_active": False,
            }
        )
        assert spawns == [], f"the ported hook spawned {spawns}"
        assert scans_called == ["report_verdict", "writes_verdict"], (
            "the no-spawn assertion must run on the branch that CALLS the "
            f"scans; it reached {scans_called or 'neither'}"
        )

    def test_neither_scan_can_reach_a_subprocess_at_all(self):
        """A shape guard beside the behavioural one, for the same reason
        ``test_expectations_module.py::TestTheClaimIsStructurallyAtomic``
        exists: a behavioural test can only catch a spawn on the paths it
        happens to walk. ``subagent_stop_scans`` replaced two ``python3``
        invocations, so the property worth pinning is that the module
        cannot spawn anything on ANY path."""
        src = Path(hook.__file__).with_name("subagent_stop_scans.py").read_text()
        tree = ast.parse(src)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert "subprocess" not in imported, (
            "subagent_stop_scans imports subprocess — the two scans it "
            "replaced WERE subprocesses, and removing that cost is the "
            "whole point of the tier change"
        )
        assert "os" not in imported or "system" not in src, (
            "subagent_stop_scans may use os.path, but os.system would be a "
            "spawn by another name"
        )
