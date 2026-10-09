# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cnzei.6 (injection audit S5): the SessionStart combined-budget
check (test_session_start_combined_budget.py) has no SubagentStart
counterpart, even though the injection audit measured a real SubagentStart
context at ~10-12KB (T2 nexus/llm-guidance-audit-injection-2026-09-13).
This is that counterpart.

The sn emitter is a subprocess-invoked shell script with its own
JSON-envelope contract; the conexus emitter is the ported
``nexus.hooks.subagent_start.run()`` (RDR-215 bead nexus-q02nx.21 deleted
``conexus/hooks/scripts/subagent-start.sh``), driven in a CHILD PROCESS
via ``_CONEXUS_PY_DRIVER`` for the same reason ``test_subagent_start_hook.py``
drives it that way — these tests vary ``cwd`` per case, which is
process-global. A general-purpose, no-task/no-prompt payload is used
throughout so the census reflects what a REAL dispatch actually receives
(see test_subagent_start_hook.py::TestAgentTypeClassification for why a
fabricated task/prompt field would be misleading here).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SN_SCRIPT = _REPO_ROOT / "sn" / "hooks" / "scripts" / "subagent_start.py"

_CONEXUS_PY_DRIVER = """
import json, sys
import os
from nexus._hook_runtime._io import never_fail
from nexus.hooks import subagent_start, t2_prefix_scan

_seed = os.environ.get("NX_BUDGET_T2_SEED_FILE")
if _seed:
    _seeded = open(_seed, encoding="utf-8").read()
    t2_prefix_scan.scan = lambda project: _seeded

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
result = never_fail(lambda: subagent_start.run(payload), "subagent_start")
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(0)
"""

#: Shaped like a real dispatch: agent_id/agent_type/session_id/prompt_id,
#: no task, no prompt (see the injection audit's own measured payload
#: shape, and TestAgentTypeClassification in test_subagent_start_hook.py).
_REAL_SHAPE_PAYLOAD = json.dumps({
    "session_id": "budget-census-session",
    "hook_event_name": "SubagentStart",
    "agent_id": "abudget00000000000000000",
    "agent_type": "general-purpose",
    "prompt_id": "abc123",
})

#: nexus-wxe50: the hook's two live-state sections, seeded at a real
#: session's shape. Under pytest the hook runs with a fenced HOME and
#: NEXUS_CONFIG_DIR, so it found no Knowledge Map cache and a near-empty
#: T2 scan: 4724B of conexus where a real laptop session carried 5933B
#: (2026-10-09, same tree, same payload). The gate passed at 8194B against
#: 8200 while every real dispatch was over it, so it bounded a state no
#: dispatch has. Seeding both sections makes the measure the same on CI,
#: the laptop and chas.
#:
#: T2: ``t2_prefix_scan`` renders at most ``_HARD_CAP`` = 8 entries, the
#: first ``_SNIPPET_LIMIT`` = 3 per namespace with a 70-char snippet. The
#: seed is that cap in the real session's layout (two namespaces, 5 + 3,
#: truncation lines included): 898B real on 2026-10-09, this seed a little
#: over. Titles are free-length, so this is a measured shape, not a bound.
_T2_SEED = (
    "### T2 Memory (taxonomy_discover_health)\n"
    + "".join(
        f"  docs__1-{i}__voyage-context-3__v1 — "
        + '{"last_attempt_at": "2026-10-09T03:25:16Z", "last_outcome": "success",…\n'
        for i in range(1, 4)
    )
    + "  docs__1-85__voyage-context-3__v1\n"
    + "  code__1-85__voyage-code-3__v1\n"
    + "  … (4 more)\n\n"
    + "### T2 Memory\n"
    + "".join(
        f"  search-telemetry-measurements-2026-10-0{i} — "
        + "nexus-vpa9q measurements, live managed cloud, engine v0.1.154, 2026-10…\n"
        for i in range(1, 4)
    )
    + "  … (4580 more)\n\n"
    + "  … (3 older namespace(s) not checked — _MAX_NAMESPACES=5)"
)

#: Knowledge Map: ``nexus.context`` writes one line per content type with
#: at most ``_TOPICS_PER_PREFIX`` = 5 labels each. Labels are free-length;
#: the largest cache on the laptop on 2026-10-09 was 845B (nexus's own
#: 613B). The seed is four content types at five labels, about that size.
_KNOWLEDGE_MAP_SEED = "## Knowledge Map\n\n" + "".join(
    f"{prefix}: "
    + ", ".join(f"{prefix.capitalize()} topic label number {n} ({1000 + n})" for n in range(1, 6))
    + "\n"
    for prefix in ("code", "docs", "knowledge", "rdr")
)

#: Not seeded: the T1 scratch section. ``nx scratch list`` prints every
#: entry in the session with no cap, so it has no worst case to seed and
#: sits outside what this budget can bound. A session with a long
#: scratch pad goes over it by construction.

#: Budget for the common (non-worktree) case. UNCHANGED across the RDR-215
#: port, and that is a measurement rather than an assumption.
#:
#: The port was briefly credited with ~587B of growth (4098B against the
#: bash's 3511B) and this budget was raised to 8850 to match. Those two
#: numbers came from different days and different trees. Measured
#: 2026-09-19 back to back instead -- the pre-port bash from
#: origin/develop and nexus.hooks.subagent_start, same payload, same env,
#: alternating three times so any live-state drift would land on both:
#:
#:     round 0: bash 4890B  port 4890B  delta +0B
#:     round 1: bash 4890B  port 4890B  delta +0B
#:     round 2: bash 4890B  port 4890B  delta +0B
#:
#: Byte-identical. The 587B was live-state churn, which the comment that
#: the raise deleted had predicted in advance: conexus's content carries a
#: "Ready Beads"/"Active Bead" section built from T2/bd state on this box,
#: and a ~600B swing is its documented noise floor. RDR-215 Approach item
#: 9 says budgets keep their thresholds; this one does.
#:
#: nexus-cnzei.6 fix round (critic Critical 1) set the ~8% headroom
#: convention. A future failure here may still be live-state churn rather
#: than a code regression -- re-measure the way this comment does, against
#: the same tree in the same session, before concluding either way.
#:
#: nexus-wxe50 (2026-10-09) raised it from 8200, and the cause is the
#: measure, not growth: 8200 was set against the fenced state described at
#: ``_T2_SEED`` above, and real sessions were already over it before
#: nexus-yjg4v's 979B brief check (8.5KB) and after it (9.4KB). With both
#: live sections seeded: conexus 6082B + sn 3470B = 9552B, against 9403B
#: from a real laptop session on the same tree. 9552 * 1.08 ~= 10316.
#: The brief check stays as written (nexus_ffa, 2026-10-08): the remedy
#: for a gate that measured the wrong state is the gate.
_SUBAGENT_START_BUDGET_BYTES = 10350

#: Budget for an isolation:worktree dispatch: sn's subagent_start.py adds
#: worktree-section.md on top of its normal output, and conexus's ported
#: subagent_start.py resolves T2/Knowledge-Map paths via --git-common-dir
#: instead of --show-toplevel (see TestWorktreeProjectResolution in
#: test_subagent_start_hook.py) -- a different code path, not just a size
#: delta, so this is measured end-to-end through a real linked worktree
#: rather than computed as budget-plus-delta. Re-measured 2026-09-19
#: against the Python port: conexus 3502B (smaller than its own
#: non-worktree case -- the Knowledge Map cache lookup and Ready-Beads
#: section behave differently under a throwaway worktree fixture repo)
#: + sn 6054B = 9556B, stable across repeated runs. This budget was
#: already comfortable headroom over that (9556 * 1.077 ~= 10293) before
#: the port, so it carries over unchanged; re-measure before concluding
#: either way on a future failure, same as the budget above.
#:
#: nexus-wxe50 (2026-10-09): re-measured with both live sections seeded,
#: conexus 6082B + sn 5424B = 11506B. The seeds reach the worktree case
#: too, so the 3502B above was the fenced state again, not a property of
#: worktrees. 11506 * 1.08 ~= 12426.
_SUBAGENT_START_WORKTREE_BUDGET_BYTES = 12450


def _seeded_env(home: Path, cwd: str | None) -> dict[str, str]:
    """The subprocess env with both live-state sections seeded (nexus-wxe50).

    The Knowledge Map cache is keyed exactly as
    ``subagent_start._knowledge_map_section`` keys it: the main repo root
    from ``--git-common-dir``, its basename, and a sha1 of its realpath.
    HOME moves to *home* so the seed never touches a real cache.
    """
    run_dir = cwd or os.getcwd()
    common = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=run_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()
    repo_root = os.path.realpath(os.path.join(run_dir, os.path.dirname(common) or "."))
    repo_hash = hashlib.sha1(repo_root.encode()).hexdigest()[:8]  # noqa: S324 — mirrors the hook's cache key
    context_dir = home / ".config" / "nexus" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / f"{os.path.basename(repo_root)}-{repo_hash}.txt").write_text(
        _KNOWLEDGE_MAP_SEED, encoding="utf-8"
    )
    t2_seed = home / "t2-seed.txt"
    t2_seed.write_text(_T2_SEED, encoding="utf-8")
    return {**os.environ, "HOME": str(home), "NX_BUDGET_T2_SEED_FILE": str(t2_seed)}


def _assert_seeds_reached(conexus_ctx: str) -> None:
    """Non-vacuity: the measure carries both seeded sections, or the budget
    is bounding the fenced, smaller state this file was fixed to escape."""
    assert "search-telemetry-measurements-2026-10-01" in conexus_ctx, (
        "the seeded T2 rendering did not reach the hook's output"
    )
    assert "Code topic label number 1" in conexus_ctx, (
        "the seeded Knowledge Map cache did not reach the hook's output"
    )


def _run_conexus(
    *, home: Path, cwd: str | None = None, payload: str = _REAL_SHAPE_PAYLOAD, timeout: float = 15
) -> str:
    env = _seeded_env(home, cwd)
    result = subprocess.run(
        [sys.executable, "-c", _CONEXUS_PY_DRIVER],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    assert result.returncode == 0, f"nexus.hooks.subagent_start exited {result.returncode}: {result.stderr}"
    payload_out = json.loads(result.stdout)
    return payload_out["hookSpecificOutput"]["additionalContext"]


def _run_sn(*, payload: str = _REAL_SHAPE_PAYLOAD, timeout: float = 15) -> str:
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        [sys.executable, str(_SN_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    assert result.returncode == 0, f"{_SN_SCRIPT.name} exited {result.returncode}: {result.stderr}"
    payload_out = json.loads(result.stdout)
    return payload_out["hookSpecificOutput"]["additionalContext"]


def test_sn_subagent_start_script_exists() -> None:
    """The conexus half is the ported ``nexus.hooks.subagent_start``
    module now (RDR-215 bead nexus-q02nx.21); an importable module needs
    no existence check the way a script path does."""
    assert _SN_SCRIPT.exists(), _SN_SCRIPT


def test_combined_subagent_start_total_under_budget(tmp_path) -> None:
    conexus_ctx = _run_conexus(home=tmp_path / "home")
    _assert_seeds_reached(conexus_ctx)
    sn_ctx = _run_sn()
    conexus_bytes = len(conexus_ctx.encode("utf-8"))
    sn_bytes = len(sn_ctx.encode("utf-8"))
    total = conexus_bytes + sn_bytes
    assert total < _SUBAGENT_START_BUDGET_BYTES, (
        f"combined SubagentStart total {total}B (conexus {conexus_bytes}B + "
        f"sn {sn_bytes}B) >= budget {_SUBAGENT_START_BUDGET_BYTES}B"
    )
    # Non-vacuity: both scripts must actually have produced content, or the
    # budget "passes" by measuring nothing.
    assert conexus_bytes > 1000, "nexus.hooks.subagent_start produced suspiciously little content"
    assert sn_bytes > 100, "sn subagent_start.py produced suspiciously little content"


def test_combined_subagent_start_total_under_budget_in_a_worktree(tmp_path) -> None:
    """nexus-cnzei.6 fix round (critic Critical 1): a worktree dispatch is
    a real, named, larger case (injection audit: ~12KB vs ~10KB) that the
    non-worktree test above cannot see — sn's subagent_start.py only emits
    worktree-section.md when it detects a linked worktree
    (sn/hooks/scripts/worktree_guard.py::is_linked_worktree, keyed on the
    payload's own `cwd` field), and conexus's ported subagent_start.py only takes
    its --git-common-dir path when its OS-level cwd actually is one. Both
    are exercised for real here: a throwaway git repo + `git worktree add`
    under tmp_path (same construction as
    TestWorktreeProjectResolution.test_t2_scan_uses_main_repo_name_not_worktree_dir_name
    in test_subagent_start_hook.py), conexus run with that directory as
    its subprocess cwd, sn given it via the payload's `cwd` field."""
    main_repo = tmp_path / "the-real-project"
    main_repo.mkdir()
    subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
    subprocess.run(
        ["git", "-C", str(main_repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
    )
    worktree_dir = tmp_path / "agent-worktree-budget-probe"
    subprocess.run(
        ["git", "-C", str(main_repo), "worktree", "add", "-q", str(worktree_dir), "-b", "wt-branch"],
        check=True,
    )

    conexus_ctx = _run_conexus(home=tmp_path / "home", cwd=str(worktree_dir))
    _assert_seeds_reached(conexus_ctx)
    worktree_payload = json.dumps({
        "session_id": "budget-census-session",
        "hook_event_name": "SubagentStart",
        "agent_id": "abudget00000000000000000",
        "agent_type": "general-purpose",
        "prompt_id": "abc123",
        "cwd": str(worktree_dir),
    })
    sn_ctx = _run_sn(payload=worktree_payload)
    assert "worktree" in sn_ctx.lower(), (
        "sn subagent_start.py did not emit worktree-section.md for a real linked "
        "worktree cwd — the worktree-detection path this test exercises is "
        "not firing, so this test would vacuously pass the smaller, wrong case"
    )

    conexus_bytes = len(conexus_ctx.encode("utf-8"))
    sn_bytes = len(sn_ctx.encode("utf-8"))
    total = conexus_bytes + sn_bytes
    assert total < _SUBAGENT_START_WORKTREE_BUDGET_BYTES, (
        f"combined SubagentStart worktree-mode total {total}B (conexus "
        f"{conexus_bytes}B + sn {sn_bytes}B) >= budget "
        f"{_SUBAGENT_START_WORKTREE_BUDGET_BYTES}B"
    )
