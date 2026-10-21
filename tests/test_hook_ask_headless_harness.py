# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Wiring pins for the headless hook-ask harness (nexus-mcfaj).

``tests/cc-validation/hook-ask-headless/`` measures what a PreToolUse
``permissionDecision: "ask"`` does in ``claude -p`` sessions. The sessions
run on chas and cost money, so this file pins everything that can be checked
without one: the probe hooks emit what the harness says they emit, the run
matrix keeps a control for every ask row, the launch shape never puts the
token on argv, the classifier maps evidence to outcomes, and the recorded
evidence still says what ``ask_envelope``'s docstring claims about it.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "cc-validation" / "hook-ask-headless"
EVIDENCE = HARNESS / "evidence" / "cc-2.1.302"
ROUTING = REPO / "conexus" / "hooks" / "scripts" / "routing"

_spec = importlib.util.spec_from_file_location("hook_ask_summarize", HARNESS / "summarize.py")
assert _spec and _spec.loader
summarize = importlib.util.module_from_spec(_spec)
sys.modules["hook_ask_summarize"] = summarize
_spec.loader.exec_module(summarize)

PROBE_PAYLOAD = json.dumps({"tool_name": "Bash", "tool_input": {"command": "touch HOOKASK_PROBE.ran"}})


def _run_probe(args: list[str], payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=payload, capture_output=True, text=True, timeout=30, check=False)


# -- probe hooks ----------------------------------------------------------


def test_probe_hook_ask_emits_ask_and_logs(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    proc = _run_probe(["bash", str(HARNESS / "hooks" / "probe_hook.sh"), "ask", str(log)], PROBE_PAYLOAD)
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"
    assert out["permissionDecisionReason"]
    assert "ask fired caller=main" in log.read_text()


def test_probe_hook_none_logs_but_decides_nothing(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    proc = _run_probe(["bash", str(HARNESS / "hooks" / "probe_hook.sh"), "none", str(log)], PROBE_PAYLOAD)
    assert proc.stdout == ""
    assert "none fired" in log.read_text()


def test_probe_hook_ignores_other_commands_and_marks_subagents(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    hook = ["bash", str(HARNESS / "hooks" / "probe_hook.sh"), "ask", str(log)]
    other = _run_probe(hook, json.dumps({"tool_input": {"command": "ls"}}))
    assert other.stdout == "" and not log.exists()
    _run_probe(hook, json.dumps({"agent_id": "a1", "tool_input": {"command": "touch HOOKASK_PROBE.ran"}}))
    assert "caller=subagent" in log.read_text()


def test_ask_real_emits_the_shipped_envelope(tmp_path: Path) -> None:
    log = tmp_path / "hook.log"
    proc = _run_probe([sys.executable, str(HARNESS / "hooks" / "ask_real.py"), str(ROUTING), str(log)], PROBE_PAYLOAD)
    sys.path.insert(0, str(ROUTING))
    try:
        import _lib  # type: ignore[import-not-found]

        want = json.loads(_lib.ask_envelope("hook-ask-headless real ask_envelope (nexus-mcfaj)"))
    finally:
        sys.path.remove(str(ROUTING))
    assert json.loads(proc.stdout) == want
    assert "ask-real fired caller=main" in log.read_text()


# -- the run matrix -------------------------------------------------------


def _runs() -> dict[str, tuple[str, str, str, str]]:
    text = (HARNESS / "batch.sh").read_text()
    block = re.search(r"^RUNS=\(\n(.*?)^\)", text, re.S | re.M)
    assert block, "RUNS array not found in batch.sh"
    rows: dict[str, tuple[str, str, str, str]] = {}
    for m in re.finditer(r'^\s*"([^"|]+)\|([^"|]+)\|([^"|]+)\|([^"|]+)\|([^"|]+)"', block.group(1), re.M):
        rows[m.group(1)] = (m.group(2), m.group(3), m.group(4), m.group(5))
    return rows


def test_matrix_covers_every_mode_and_each_ask_has_a_control() -> None:
    rows = _runs()
    assert set(rows) == {
        "c_default", "a_default", "c_dontask", "a_dontask", "c_auto", "a_auto",
        "c_auto_norule", "a_auto_norule", "a_bgsub", "a_real",
    }, rows.keys()
    # permission modes: default (-), dontAsk, auto; plus a background subagent
    assert {perm for perm, *_ in rows.values()} == {"-", "dontAsk", "auto"}
    assert rows["a_bgsub"][2] == "bgsub"
    for rid, (perm, hook, style, rule) in rows.items():
        if hook == "none":
            continue
        # a_real shares c_default's control: same mode, style and rule
        ctl = {"a_real": "c_default", "a_bgsub": "c_default"}.get(rid, "c_" + rid[2:])
        cperm, chook, cstyle, crule = rows[ctl]
        assert chook == "none"
        assert (cperm, crule) == (perm, rule), rid
        if rid != "a_bgsub":
            assert cstyle == style, rid


# -- launch shape ---------------------------------------------------------


def test_launch_shape_keeps_the_token_off_argv_and_runs_on_chas_only() -> None:
    batch = (HARNESS / "batch.sh").read_text()
    # bare -e: docker reads the value from its own environment
    assert re.search(r"-e CLAUDE_CODE_OAUTH_TOKEN(?![=\w])", batch)
    assert "CLAUDE_CODE_OAUTH_TOKEN=" not in batch
    assert "NX_NO_TELEMETRY=1" in batch
    assert "docker run --rm" in batch
    run = (HARNESS / "run.sh").read_text()
    assert "claude_credentials.py" in run and "run --remote" in run
    assert "lab-run" in run
    # Claude Code is never started by run.sh itself, only through the remote runner
    assert not re.search(r"^\s*claude\s", run, re.M)


def test_in_container_run_is_headless_bounded_and_trust_seeded() -> None:
    body = (HARNESS / "in_container.sh").read_text()
    assert re.search(r"timeout --kill-after=\d+ .*claude -p ", body, re.S)
    assert "--output-format stream-json" in body
    assert "hasTrustDialogAccepted" in body  # untrusted workspaces ignore permissions.allow
    assert "NX_NO_TELEMETRY=1" in body
    assert "Bash(touch HOOKASK_PROBE*)" in body  # not read-only: echo is auto-allowed everywhere


# -- classifier -----------------------------------------------------------


def _write_run(
    root: Path,
    name: str,
    *,
    rc: int = 0,
    hook: bool = True,
    ran: bool = False,
    denial: bool = False,
    stderr: str = "",
) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "rc").write_text(f"{rc}\n")
    (d / "elapsed").write_text("4\n")
    (d / "version.txt").write_text("2.1.302 (Claude Code)\n")
    (d / "hook.log").write_text("1 ask fired caller=main\n" if hook else "")
    (d / "stderr.txt").write_text(stderr)
    if ran:
        (d / "ran.marker").write_text("")
    events: list[dict[str, object]] = []
    if denial:
        events.append({"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "why"}]}})
        events.append({"type": "result", "permission_denials": [{"tool_name": "Bash"}]})
    else:
        events.append({"type": "result", "permission_denials": []})
    (d / "stream.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return d


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [
        ({"ran": True}, "RAN"),
        ({"denial": True}, "DENIED"),
        ({"rc": 124}, "HUNG"),
        ({"rc": 137, "denial": True}, "HUNG"),
        ({"hook": False, "ran": True}, "NOT-EXERCISED"),
        ({"ran": True, "stderr": "Ignoring 1 permissions.allow entry from x: not trusted"}, "NOT-EXERCISED"),
        ({}, "NO-ATTEMPT"),
    ],
)
def test_classify_maps_evidence_to_outcome(tmp_path: Path, kwargs: dict[str, object], outcome: str) -> None:
    d = _write_run(tmp_path, "r", **kwargs)  # type: ignore[arg-type]
    assert summarize.classify(d)["outcome"] == outcome


def test_classify_missing_run_is_missing_not_a_pass(tmp_path: Path) -> None:
    (tmp_path / "gone").mkdir()
    assert summarize.classify(tmp_path / "gone")["outcome"] == "MISSING"


def test_a_denial_on_an_earlier_result_event_still_counts(tmp_path: Path) -> None:
    """The background-subagent run emits two result events; the denial rides the first."""
    d = _write_run(tmp_path, "bg")
    (d / "stream.jsonl").write_text(
        json.dumps({"type": "result", "permission_denials": [{"tool_name": "Bash"}]})
        + "\n"
        + json.dumps({"type": "result", "permission_denials": []})
        + "\n"
    )
    assert summarize.classify(d)["outcome"] == "DENIED"


# -- recorded evidence ----------------------------------------------------


def test_recorded_evidence_matches_the_documented_claim() -> None:
    """Every ask row denied, every control ran, no row hung; the docstring cites this version."""
    dirs = sorted(p for p in EVIDENCE.iterdir() if p.is_dir())
    assert len(dirs) >= 10, f"only {len(dirs)} recorded runs; the table in ask_envelope needs all of them"
    rows = {d.name: summarize.classify(d) for d in dirs}
    asks = [r for n, r in rows.items() if n.startswith("a_")]
    controls = [r for n, r in rows.items() if n.startswith("c_")]
    assert asks and controls
    assert all(r["outcome"] == "DENIED" for r in asks), {r["id"]: r["outcome"] for r in asks}
    assert all(r["outcome"] == "RAN" for r in controls), {r["id"]: r["outcome"] for r in controls}
    assert all(r["rc"] == 0 for r in rows.values())
    assert all(
        int((EVIDENCE / name / "elapsed").read_text().strip()) < 10 for name in rows
    ), "the docstring says every recorded run finished in under 10 s"
    assert rows["a_bgsub"]["callers"] == ["subagent"]
    lib = (ROUTING / "_lib.py").read_text()
    version = {r["version"] for r in rows.values()}
    assert version == {"2.1.302 (Claude Code)"}
    assert "2.1.302" in lib and "nexus-mcfaj" in lib
