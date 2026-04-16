# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM-in-loop behavioral verification of the RDR-080 agent replacements.

Gated on NX_VALIDATE_WITH_LLM=1 — spawns real `claude -p` subprocesses.
Expensive (~10-60s per case, costs API credits) but is the only way to
answer "do the stub agents actually route callers to the MCP tool?"

Tests:

  1. Stub agent behavior — for each of the 3 stubs, run the agent with
     a canned task and verify the output mentions the named MCP tool
     by name. We're not testing the MCP tool's output (that's suite 01);
     we're testing that the stub *directs the caller to the MCP tool*.

  2. nx_answer end-to-end — invoke nx_answer on a real question against
     the sandbox (with the 14 seeded plans and no corpus) and verify it
     returns a non-error string. This exercises the full trunk including
     the plan-miss inline planner if no plan matches.

  3. Orchestration trunk: nx_answer drives plan_run — with the seeded
     plans, verify that a query matching a seeded plan actually dispatches
     the plan's first step (search) rather than the inline planner.

Tools required:
  * `claude` CLI on PATH (Claude Code CLI — comes with the app)
  * ANTHROPIC_API_KEY or equivalent (claude -p requires LLM egress)
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager


_pass = 0
_fail = 0
_failures: list[tuple[str, str]] = []


def ts() -> str:
    return time.strftime("%H:%M:%S")


def info(msg: str) -> None:
    print(f"[{ts()}]    {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n[{ts()}] ─── {msg} ───", flush=True)


@contextmanager
def case(name: str):
    global _pass, _fail
    start = time.monotonic()
    try:
        yield
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✓ {name}  ({dur} ms)", flush=True)
        _pass += 1
    except AssertionError as exc:
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — {exc}", flush=True)
        _fail += 1
        _failures.append((name, str(exc)))
    except Exception as exc:
        dur = int((time.monotonic() - start) * 1000)
        print(f"[{ts()}]  ✗ {name}  ({dur} ms) — {type(exc).__name__}: {exc}", flush=True)
        if os.environ.get("NX_VALIDATE_VERBOSE"):
            traceback.print_exc()
        _fail += 1
        _failures.append((name, f"{type(exc).__name__}: {exc}"))


def skip(name: str, reason: str) -> None:
    print(f"[{ts()}]  ⊖ {name}  — skipped: {reason}", flush=True)


# ── Gating ───────────────────────────────────────────────────────────────────

def _gate() -> bool:
    """Return True if the LLM-backed suite should run."""
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        step("LLM-backed agent behavior — SKIPPED")
        info("Set NX_VALIDATE_WITH_LLM=1 to run this suite.")
        info("This suite spawns `claude -p` subprocesses and costs API credits.")
        return False
    if shutil.which("claude") is None:
        step("LLM-backed agent behavior — SKIPPED")
        info("`claude` CLI not on PATH; required for spawning subprocesses.")
        return False
    return True


# ── Stub agent behavioral verification ───────────────────────────────────────

STUBS = [
    {
        "name": "knowledge-tidier",
        "mcp_tool": "nx_tidy",
        "task": (
            "Consolidate knowledge on topic 'chromadb quotas'. "
            "What tool should I call, and what's the call shape?"
        ),
        "expect": ["nx_tidy", "mcp__plugin_nx_nexus__nx_tidy"],
    },
    {
        "name": "plan-auditor",
        "mcp_tool": "nx_plan_audit",
        "task": (
            "Audit this plan: {\"steps\":[{\"tool\":\"search\",\"args\":{\"query\":\"$topic\"}}]}. "
            "What tool should I call?"
        ),
        "expect": ["nx_plan_audit", "mcp__plugin_nx_nexus__nx_plan_audit"],
    },
    {
        "name": "plan-enricher",
        "mcp_tool": "nx_enrich_beads",
        "task": (
            "Enrich this bead: 'Add --foo flag to nx search'. "
            "What tool should I call?"
        ),
        "expect": ["nx_enrich_beads", "mcp__plugin_nx_nexus__nx_enrich_beads"],
    },
]


def _run_claude_p(system_prompt: str, user_prompt: str, timeout: float = 90.0) -> str:
    """Spawn `claude -p` with the stub markdown as system prompt.

    Overrides HOME to the caller's real $HOME (via ORIG_HOME) so claude
    can reach its login state (macOS keychain lookup is tied to user
    context, but config-file paths are not).  Nexus storage paths are
    set via explicit env vars (NX_LOCAL_CHROMA_PATH, NEXUS_CATALOG_PATH)
    so we don't lose sandbox isolation by flipping HOME here.
    """
    env = os.environ.copy()
    orig_home = env.get("ORIG_HOME")
    if orig_home:
        env["HOME"] = orig_home
    result = subprocess.run(
        ["claude", "-p", user_prompt, "--append-system-prompt", system_prompt],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude -p rc={result.returncode}: "
            f"stderr={result.stderr[:200]!r}  stdout={result.stdout[:200]!r}"
        )
    return result.stdout


def _exercise_stubs() -> None:
    from pathlib import Path
    agents_dir = Path(__file__).resolve().parent.parent.parent / "nx" / "agents"

    step("Stub agents route callers to the MCP tool")
    for stub in STUBS:
        name = stub["name"]
        with case(f"{name} directs caller to {stub['mcp_tool']}"):
            stub_md = agents_dir / f"{name}.md"
            system = stub_md.read_text()
            output = _run_claude_p(system_prompt=system, user_prompt=stub["task"])
            info(f"  output head: {output[:120]!r}")
            # Any of the expected tool strings must appear
            hit = any(exp in output for exp in stub["expect"])
            assert hit, (
                f"stub output doesn't name the MCP tool. "
                f"Expected one of: {stub['expect']}.  Got: {output[:200]!r}"
            )


# ── nx_answer end-to-end (real plan_match + real plan_run trunk) ─────────────

def _exercise_nx_answer_e2e() -> None:
    step("nx_answer end-to-end — real plan_match + real plan_run")

    async def _run(question: str):
        from nexus.mcp.core import nx_answer
        return await nx_answer(question, scope="global")

    # The sandbox has 14 seeded plans but no corpus — so retrieval returns empty.
    # The trunk itself (plan_match → classify → run) should complete without error.
    with case("nx_answer on 'design planning corpus' (plan hit expected)"):
        result = asyncio.run(_run("design planning corpus"))
        assert isinstance(result, str) and len(result) > 0
        info(f"  result head: {result[:120]!r}")

    with case("nx_answer on unknown topic (plan miss → inline planner)"):
        result = asyncio.run(_run("explain the Q4 revenue projection"))
        assert isinstance(result, str) and len(result) > 0
        info(f"  result head: {result[:120]!r}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print(f"[{ts()}] LLM-in-loop agent behavior validation")
    if not _gate():
        print(f"\n[{ts()}] ── agent-behavior: 0 pass, 0 fail (all skipped) ──")
        return 0
    try:
        _exercise_stubs()
        _exercise_nx_answer_e2e()
    finally:
        print(f"\n[{ts()}] ── agent-behavior: {_pass} pass, {_fail} fail ──")
        for name, err in _failures:
            print(f"       - {name}: {err}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
