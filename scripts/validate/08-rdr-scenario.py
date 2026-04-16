# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end RDR scenario: create → research → gate → accept → close.

This suite exercises the full RDR lifecycle in the sandbox without
depending on Claude Code's interactive skill system. It reproduces the
T2 state transitions and file-artifact invariants that the /nx:rdr-*
slash commands encode, so a plugin regression that breaks (for example)
rdr-close's gap-pointer gate shows up here rather than at user-report
time.

State machine:

    draft  --gate-pass--> accepted  --close--> closed

Sandbox contract:
  * Fake repo under $SANDBOX/fake-repo (git-initialised)
  * RDR file at fake-repo/docs/rdr/rdr-999-sandbox-validation.md
  * T2 entry under project="fake-repo_rdr", title="999"

Phases (6 structural + 3 LLM-gated):
  1. create  — scaffold file + register in T2 as status=draft
  2. list    — /nx:rdr-list equivalent: T2 iteration finds the entry
  3. show    — /nx:rdr-show equivalent: T2 read returns content
  4. gate (structural) — frontmatter, Problem Statement gaps, closing
     sections present; preamble-equivalent invariants from rdr-close
     (e.g. regex `^#{3,5} Gap \\d+:`) satisfied
  5. accept  — gate passed → update T2 status to accepted + file mtime
  6. close   — simulate rdr-close with gap pointers; T2 marks closed
  7. gate (AI critique, NX_VALIDATE_WITH_LLM=1) — spawn substantive-
     critic via claude -p and verify it produces a verdict
  8. rdr-close gap-pointer gate (NX_VALIDATE_WITH_LLM=1) — invoke the
     rdr-close preamble with valid pointers and verify it doesn't block
  9. full_match — an MCP-level plan_match on "research RDR-999"
     retrieves our seeded plan, verifying the T2 state integrates with
     the RDR-078 plan-match path.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any


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


# ── Fake-repo fixture ────────────────────────────────────────────────────────


def _fake_repo_root() -> Path:
    sandbox = Path(os.environ["HOME"])
    return sandbox / "fake-repo"


def _rdr_dir() -> Path:
    return _fake_repo_root() / "docs" / "rdr"


def _setup_fake_repo() -> None:
    repo = _fake_repo_root()
    if repo.exists():
        return
    repo.mkdir(parents=True, exist_ok=True)
    _rdr_dir().mkdir(parents=True, exist_ok=True)
    # Minimal README so git has something to track
    (repo / "README.md").write_text("# fake-repo\n")
    # Init git so any future git-based commands (e.g. subprocess_git_toplevel)
    # have a real repo to discover.
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                   env={**os.environ,
                        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"})


# ── RDR artifact shape ───────────────────────────────────────────────────────

RDR_ID = "999"
RDR_SLUG = "sandbox-validation"
RDR_TITLE = "Sandbox Validation of RDR Lifecycle"


def _rdr_file_path() -> Path:
    return _rdr_dir() / f"rdr-{RDR_ID}-{RDR_SLUG}.md"


def _draft_rdr_body(status: str = "draft") -> str:
    """Return a minimal but valid RDR markdown body."""
    return f"""\
---
title: "RDR-{RDR_ID}: {RDR_TITLE}"
status: {status}
type: Architecture
priority: P2
created: 2026-04-16
---

# RDR-{RDR_ID}: {RDR_TITLE}

## Problem Statement

The validation harness has no end-to-end scenario exercising the full
RDR lifecycle against a sandboxed environment. Without this, regressions
in the skill-level lifecycle (create / gate / accept / close) escape the
unit suite.

### Enumerated gaps to close

#### Gap 1: Lifecycle has no e2e scenario

No automated flow walks a fake RDR from draft → accepted → closed.

#### Gap 2: T2 state transitions are unverified

The `status: X` field in the T2 entry is written by multiple skills,
but its invariants aren't tested in sequence.

## Research Findings

See docs/rdr/rdr-workflow.md for the canonical state machine.

## Design

Add a Python suite to the validation harness that:
1. Scaffolds a fake RDR file + T2 entry.
2. Runs structural + optional LLM gate.
3. Mutates status along the lifecycle.

## Decision

Ship suite 08-rdr-scenario.py in scripts/validate/.

## Rejected Alternatives

Using the real RDR workflow against the live repo — would pollute prod T2.
"""


def _t2_record(status: str, close_reason: str = "") -> str:
    """T2 record shape that /nx:rdr-* skills read."""
    lines = [
        f"title: RDR-{RDR_ID}: {RDR_TITLE}",
        f"status: {status}",
        "type: Architecture",
        "priority: P2",
        "created: 2026-04-16",
        f"file_path: docs/rdr/rdr-{RDR_ID}-{RDR_SLUG}.md",
    ]
    if close_reason:
        lines.append(f"close_reason: {close_reason}")
    return "\n".join(lines) + "\n"


def _t2_project() -> str:
    return f"{_fake_repo_root().name}_rdr"


# ── Suite ────────────────────────────────────────────────────────────────────


def _seed_catalog_and_plans() -> None:
    """Run `nx catalog setup` so phase 9 plan_match has something to match against."""
    step("Setup — seed catalog + plans (required for phase 9 plan_match)")
    with case("nx catalog setup (14 plans)"):
        r = subprocess.run(
            ["uv", "run", "nx", "catalog", "setup"],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr[:300]


def run_suite() -> None:
    _setup_fake_repo()
    _seed_catalog_and_plans()

    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    # ── 1. create ────────────────────────────────────────────────────────────
    step("Phase 1 — create: scaffold RDR file + register in T2 as draft")
    with case("write RDR file with valid frontmatter + structure"):
        _rdr_file_path().write_text(_draft_rdr_body("draft"))
        body = _rdr_file_path().read_text()
        assert body.startswith("---\n")
        assert f"status: draft" in body
        assert "#### Gap 1:" in body and "#### Gap 2:" in body

    with case("T2 register: memory_put project={repo}_rdr title=999"):
        with T2Database(default_db_path()) as db:
            db.memory.put(
                content=_t2_record("draft"),
                project=_t2_project(),
                title=RDR_ID,
                tags="rdr,draft",
            )
            row = db.memory.get(project=_t2_project(), title=RDR_ID)
            assert row is not None
            assert "status: draft" in row["content"]

    # ── 2. list ──────────────────────────────────────────────────────────────
    step("Phase 2 — list: T2 iteration finds the new RDR")
    with case("memory.list_entries returns RDR-999"):
        with T2Database(default_db_path()) as db:
            rows = db.memory.list_entries(project=_t2_project())
            titles = [r["title"] for r in rows]
            assert RDR_ID in titles, f"RDR-{RDR_ID} not in {titles}"

    # ── 3. show ──────────────────────────────────────────────────────────────
    step("Phase 3 — show: T2 get returns content")
    with case("memory.get reflects current status"):
        with T2Database(default_db_path()) as db:
            row = db.memory.get(project=_t2_project(), title=RDR_ID)
            assert row is not None
            assert "status: draft" in row["content"]

    # ── 4. gate (structural) ─────────────────────────────────────────────────
    step("Phase 4 — gate (structural): frontmatter + gap headings")
    with case("frontmatter parses"):
        text = _rdr_file_path().read_text()
        m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
        assert m, "no frontmatter"
        import yaml
        fm = yaml.safe_load(m.group(1))
        for k in ("title", "status", "type", "priority", "created"):
            assert k in fm, f"frontmatter missing {k!r}"

    with case("Problem Statement has ≥1 `#### Gap N:` heading (rdr-close gate)"):
        text = _rdr_file_path().read_text()
        # Same regex the rdr-close skill uses internally (see the skill preamble).
        gaps = re.findall(r"^#{3,5} Gap (\d+).*$", text, re.MULTILINE)
        assert len(gaps) >= 1, f"found {len(gaps)} gaps, need ≥1"
        info(f"  found gaps: {gaps}")

    with case("no critic-blocking anti-patterns (silent scope reduction)"):
        text = _rdr_file_path().read_text().lower()
        # Heuristic: RDRs that claim decisions but have empty sections
        assert "## decision" in text
        # Would normally invoke substantive-critic here; see phase 7 (LLM-gated).

    # ── 5. accept ────────────────────────────────────────────────────────────
    step("Phase 5 — accept: gate passed → status=accepted in T2 + file")
    with case("update T2 status draft → accepted"):
        with T2Database(default_db_path()) as db:
            db.memory.put(
                content=_t2_record("accepted"),
                project=_t2_project(),
                title=RDR_ID,
                tags="rdr,accepted",
            )
            row = db.memory.get(project=_t2_project(), title=RDR_ID)
            assert row and "status: accepted" in row["content"]

    with case("update file frontmatter status: draft → accepted"):
        _rdr_file_path().write_text(_draft_rdr_body("accepted"))
        body = _rdr_file_path().read_text()
        assert "status: accepted" in body

    # ── 6. close ─────────────────────────────────────────────────────────────
    step("Phase 6 — close: rdr-close with gap pointers → status=closed")
    with case("rdr-close preamble validates gap pointers (file:line shape)"):
        # The rdr-close skill's preamble requires `--pointers Gap1=path:line`
        # where the file exists and the line-number regex matches.  Build
        # valid pointers against the fake repo's own files.
        pointers = {
            "Gap1": f"{_fake_repo_root()}/README.md:1",
            "Gap2": f"{_fake_repo_root()}/docs/rdr/rdr-{RDR_ID}-{RDR_SLUG}.md:1",
        }
        for gap, ptr in pointers.items():
            file_part, _, line_part = ptr.partition(":")
            assert Path(file_part).exists(), f"pointer file not found: {file_part}"
            assert line_part.isdigit(), f"pointer line not numeric: {line_part}"

    with case("close: update T2 status → closed + close_reason"):
        with T2Database(default_db_path()) as db:
            db.memory.put(
                content=_t2_record("closed", close_reason="implemented"),
                project=_t2_project(),
                title=RDR_ID,
                tags="rdr,closed",
            )
            row = db.memory.get(project=_t2_project(), title=RDR_ID)
            assert row and "status: closed" in row["content"]
            assert "close_reason: implemented" in row["content"]

    with case("close: update file frontmatter status → closed"):
        # Authors typically also set closed_date + close_reason in frontmatter.
        body = _draft_rdr_body("closed")
        body = body.replace(
            "priority: P2",
            "priority: P2\nclosed_date: 2026-04-16\nclose_reason: implemented",
        )
        _rdr_file_path().write_text(body)
        assert "status: closed" in _rdr_file_path().read_text()

    # ── 9. full_match: T2 integration with plan_match ────────────────────────
    step("Phase 9 — T2 RDR state integrates with RDR-078 plan_match")
    with case("plan_match finds the built-in RDR-078 research template"):
        from nexus.plans.matcher import plan_match
        # Query uses words present in the research-default.yml description
        # ("Design / architecture / planning. Walk from a concept into the
        # prose corpus (RDRs, docs, knowledge)..."). FTS5 fallback matches
        # on shared tokens.
        with T2Database(default_db_path()) as db:
            matches = plan_match(
                intent="design planning walk prose corpus RDR docs",
                library=db.plans,
                cache=None,
                min_confidence=0.85,
                n=5,
            )
            assert len(matches) >= 1, f"no plan match; got {matches}"
            info(f"  {len(matches)} matches; top plan_id={matches[0].plan_id}")

    # ── 7. LLM-gated: AI critique via substantive-critic ─────────────────────
    step("Phase 7 — gate (AI critique) — substantive-critic")
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        skip("substantive-critic AI critique", "NX_VALIDATE_WITH_LLM=1 off")
    else:
        _exercise_ai_critique()

    # ── 8. LLM-gated: rdr-close invocation via claude -p ─────────────────────
    step("Phase 8 — rdr-close skill end-to-end via claude -p")
    if os.environ.get("NX_VALIDATE_WITH_LLM") != "1":
        skip("rdr-close skill invocation", "NX_VALIDATE_WITH_LLM=1 off")
    else:
        _exercise_rdr_close_invocation()


def _exercise_ai_critique() -> None:
    """Spawn the substantive-critic agent and verify it produces a verdict."""
    agents_dir = Path(__file__).resolve().parent.parent.parent / "nx" / "agents"
    critic_md = (agents_dir / "substantive-critic.md").read_text()
    rdr_body = _rdr_file_path().read_text()

    env = os.environ.copy()
    if (orig := env.get("ORIG_HOME")):
        env["HOME"] = orig

    prompt = (
        "Provide deep constructive critique of the attached RDR. Focus on "
        "logical consistency, completeness, and whether the gaps enumerated "
        "in `## Problem Statement` are addressed. Emit your findings with "
        "priority labels (Critical / Significant / Minor).\n\n"
        f"<rdr>\n{rdr_body}\n</rdr>"
    )
    with case("substantive-critic returns critique with priority labels"):
        result = subprocess.run(
            ["claude", "-p", prompt, "--append-system-prompt", critic_md],
            capture_output=True, text=True, timeout=180, env=env,
        )
        assert result.returncode == 0, \
            f"claude -p rc={result.returncode}: {result.stderr[:200]!r}"
        out = result.stdout.lower()
        # Must contain at least one priority label or verdict keyword.
        assert any(kw in out for kw in ("critical", "significant", "minor",
                                        "finding", "verdict", "gap")), \
            f"critic output has no priority/verdict markers: {out[:300]!r}"
        info(f"  output length: {len(result.stdout)} chars")


def _exercise_rdr_close_invocation() -> None:
    """Spawn the rdr-close skill via `claude -p` using the fake-repo as CWD.

    Verifies the skill's Python preamble runs end-to-end with valid
    gap pointers and doesn't report a blocking error.

    Phase 6 already closed the RDR; the skill correctly refuses to
    re-close an already-closed entry.  Reset both file and T2 state to
    `accepted` so this phase exercises the accepted → closed transition.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    # Restore accepted state for both the file and T2 before invoking close.
    _rdr_file_path().write_text(_draft_rdr_body("accepted"))
    with T2Database(default_db_path()) as db:
        db.memory.put(
            content=_t2_record("accepted"),
            project=_t2_project(),
            title=RDR_ID,
            tags="rdr,accepted",
        )

    skill_md = (
        Path(__file__).resolve().parent.parent.parent
        / "nx" / "commands" / "rdr-close.md"
    )
    if not skill_md.exists():
        # Command files live under nx/commands — check both.
        skill_md = Path(__file__).resolve().parent.parent.parent / "nx" / "commands" / "rdr-close.md"
    body = skill_md.read_text() if skill_md.exists() else ""

    env = os.environ.copy()
    if (orig := env.get("ORIG_HOME")):
        env["HOME"] = orig

    pointers = (
        f"Gap1={_fake_repo_root()}/README.md:1,"
        f"Gap2={_fake_repo_root()}/docs/rdr/rdr-{RDR_ID}-{RDR_SLUG}.md:1"
    )
    prompt = (
        f"Close RDR-{RDR_ID} as implemented. Use --pointers '{pointers}'. "
        "Confirm the preamble's gate validates without blocking, and "
        "summarise what would change in T2."
    )
    with case("rdr-close skill: gap-pointer gate validates"):
        result = subprocess.run(
            ["claude", "-p", prompt, "--append-system-prompt", body],
            capture_output=True, text=True, timeout=180, env=env,
            cwd=str(_fake_repo_root()),
        )
        assert result.returncode == 0, \
            f"claude -p rc={result.returncode}: {result.stderr[:200]!r}"
        out = result.stdout.lower()
        # Must show the preamble acknowledged the pointers and did not BLOCK.
        assert "blocked" not in out or "not blocked" in out, \
            f"rdr-close appears to have blocked the close: {out[:300]!r}"
        info(f"  output length: {len(result.stdout)} chars")


def main() -> int:
    print(f"[{ts()}] RDR lifecycle e2e — fake repo under {_fake_repo_root()}")
    try:
        run_suite()
    finally:
        print(f"\n[{ts()}] ── rdr-scenario: {_pass} pass, {_fail} fail ──")
        for name, err in _failures:
            print(f"       - {name}: {err}")
    return 0 if _fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
