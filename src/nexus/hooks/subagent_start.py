# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SubagentStart context injector (RDR-215 bead nexus-q02nx.18).

Port of ``conexus/hooks/scripts/subagent-start.sh`` (contract map row 14,
399 lines). Injects storage-tier docs, orchestration directives, and
task/agent-type-scoped guidance into a freshly spawned subagent's initial
context, selectively skipping sections to save tokens for agent types that
do not need them (code-nav, code-review).

**MOVE, DO NOT REWRITE.** The agent-type/task-text classification regexes
and the catalog-awareness regex are carried VERBATIM (see the module
constants below, each extracted programmatically from the script's own
source rather than retyped, so a transcription slip was not available as
a failure mode). Dropping an alternative from one of those alternations
would silently remove guidance from a real dispatch shape.

**It exports/forces ``NX_SESSION_ID`` (nexus-7o1zh), just as the bash
does.** This hook runs detached from any live nx-mcp server, so it cannot
rely on inheriting a session from its environment -- without forcing it
from the payload, every ``nx``/``python3`` subprocess spawned below would
resolve a SIBLING session's machine-wide pointer (or nothing at all).
:func:`_nx_env` is that forcing point, applied uniformly to every
subprocess call this hook makes (git, ``nx catalog links-for-file``,
``nx scratch list``) -- mirroring bash's ``export``, which affects every
child process spawned after it runs, not just the one call it sits
nearest.

**The T2 memory section no longer shells out (RDR-215 bead nexus-b5ugt).**
It used to resolve ``t2_prefix_scan.py`` off ``$CLAUDE_PLUGIN_ROOT`` alone
(with no fallback) and launch it as a ``python3`` subprocess. That path
carried a live defect: ``conexus/.mcp.json`` sets the MCP server's ``env``
block to a LITERAL ``"${CLAUDE_PLUGIN_ROOT}"`` string (Claude Code does
not expand ``${...}`` inside an MCP ``env`` block), so every ``nx-mcp``
process's ``CLAUDE_PLUGIN_ROOT`` was that unexpanded literal and the
subprocess path never resolved -- the "## T2 Memory" section was silently
missing from every dispatched subagent's context. :func:`_t2_memory_section`
now calls :func:`nexus.hooks.t2_prefix_scan.scan` in-process instead; see
that module's docstring for the full port and its one deliberate
behavioural divergence from the plugin-resident mirror it replaces
(still present on disk -- see below -- but no longer on this call path).

The plugin-resident ``conexus/hooks/scripts/t2_prefix_scan.py`` file
itself was NOT deleted by this port, on the stated ground that
``mailbox_drain.py`` and the ``routing/`` guards were still plugin-resident.
nexus-t9klx ported all three, and the file was deleted at nexus-z9cz2.

**``SKIP_T2_SCAN`` is genuinely dead in the bash, and stays dead here.**
The script declares ``SKIP_T2_SCAN=0`` alongside ``SKIP_STORAGE_DOCS`` and
``SKIP_OPERATORS``, gates the T2-memory section on it, but never sets it
to 1 anywhere -- neither classification branch touches it. So the T2 scan
runs for EVERY agent type, including code-nav and code-review dispatches
that skip the storage-tier docs and analytical operators. This port drops
the inert flag rather than carry a variable that can never do anything,
which is a legitimate simplification (observable behaviour is identical:
the T2 scan is unconditional on classification, exactly as today), not a
fix -- flagged here rather than silently normalized away.

**It writes nothing** -- confirmed by inspection, same as the bash: every
branch either returns a string to inject or returns nothing.

**Its entire output is the product.** The bash captures all body stdout
into a tempfile via an fd-3 redirect and an ``EXIT`` trap, then renders one
JSON envelope from it. In Python that machinery collapses to building a
string and returning one :class:`~nexus._hook_runtime._io.HookResult` --
the legitimate simplification the RDR exists for.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from nexus._hook_runtime._io import HookResult, additional_context

__all__ = ["run"]

# -- Classification regexes (verbatim; see module docstring) -----------------

#: subagent-start.sh:124
_CODE_NAV_TYPE_RE = re.compile(r"^explore$|codebase-deep-analyzer", re.IGNORECASE)
#: subagent-start.sh:125
_CODE_NAV_TASK_RE = re.compile(
    r"refactor|rename.*symbol|find.*method|type.hierarch|navigate.code", re.IGNORECASE
)
#: subagent-start.sh:129
_CODE_REVIEW_TYPE_RE = re.compile(r"code-review", re.IGNORECASE)
#: subagent-start.sh:130
_CODE_REVIEW_TASK_RE = re.compile(r"code.review|review.code|lint|style.check", re.IGNORECASE)
#: subagent-start.sh:297
_PHASE_GATE_TASK_RE = re.compile(
    r"close.*phase|phase.*clos|phase.*review|review.*gate|approach.*cross.walk|"
    r"cross.walk.*approach|phase.*close|closeout|rdr.*phase|phase.*rdr|"
    r"silent.scope|scope.reduc",
    re.IGNORECASE,
)
#: subagent-start.sh:365
_CATALOG_TASK_RE = re.compile(
    r"author|cit(e|ation|es|ed)|who wrote|what did.*write|papers? (by|about)|"
    r"provenance|corpus|collection|tumbler|what research|informed by|based on|"
    r"relationship|links? (from|to)|referenc|follow.on|build.on|"
    r"what (implements|supersedes)|link (audit|query|graph)|orphan|catalog|"
    r"rdr.*(close|accept|show|gate|research)|close.*rdr|accept.*rdr|supersed|"
    r"consolidat|tidy|knowledge|research|synthesiz|archive|store_put|store put|"
    r"debug.*finding|root.cause|prevention.pattern|architecture.*map|"
    r"pattern.*catalog|architect.*decision|risk.assess|insight.*developer|"
    r"analysis.*deep|analyz.*codebas",
    re.IGNORECASE,
)
#: subagent-start.sh:180
_FILE_PATH_RE = re.compile(r"(?:src|tests|docs|nx)/[\w/.-]+\.\w+")
#: subagent-start.sh:188
_ARROW_LINE_RE = re.compile(r"^\s+[←→]")

# -- Static injected content (verbatim heredoc bodies) ------------------------
#
# Each constant is the exact bytes `cat <<'TAG' ... TAG` would write: the
# heredoc body plus the one trailing newline the heredoc's own closing line
# consumes. Extracted programmatically from the script source rather than
# retyped -- see the extraction used to derive these in the port's own
# working notes; a byte-for-byte diff against the script is what
# `tests/hooks/test_subagent_start_byte_budget.py` and the retargeted
# `test_subagent_start_hook.py` both exercise.

_RELAY = (
    "\n## Relay Format (Required Fields)\n\n"
    "| Field | Description |\n"
    "|-------|-------------|\n"
    "| Task | 1-2 sentence summary |\n"
    "| Bead | Bead ID with status, or 'none' |\n"
    "| Input Artifacts | nx store, nx memory, nx scratch, files |\n"
    "| Deliverable | What the agent should produce |\n"
    "| Quality Criteria | Checkbox list |\n\n"
    'Relays are constructed by the caller. Subagents output "Recommended Next Step" '
    "blocks for the caller to use.\n"
)

_ORCH = (
    "\n## Orchestration (Required)\n\n"
    "| Directive | Rule |\n"
    "|-----------|------|\n"
    "| Completion | Background: SubagentHandback (or SendMessage) full result to main "
    "before idling: success/failure/blocked + live task ids. Foreground: final message "
    "IS the hand-back. |\n"
    "| Inbox | Re-check inbox right before composing any hand-back; newest directive "
    "wins |\n"
    "| Git | Shared tree: NEVER git add/commit (hook-ENFORCED; linked worktrees "
    "exempt). Hand back diffs+paths; orchestrator commits pathspec-limited |\n"
)

_WORKTREE_PREFLIGHT = (
    "| Preflight | If dispatched with isolation:worktree: run "
    "`scripts/agent-worktree-preflight.sh [required-sha]` as your FIRST action; stop "
    "on any PREFLIGHT_FAIL line (nexus-5kwkf) |\n"
)

_NX_TIERS = (
    "\n## nx storage (call as mcp__plugin_conexus_nexus__<tool>; paged: footer shows "
    "offset=N)\n\n"
    "Read widest -> narrowest BEFORE any work; check before duplicating effort:\n"
    "  T3 search            all sessions/projects   <- check before researching\n"
    "  T2 memory            project-scoped          <- check before project work\n"
    "  T1 scratch           shared with siblings    <- check before duplicating "
    "sibling work\n"
)

_NX_TOOLS = (
    "\nT1: scratch, scratch_manage\n"
    "T2: memory_get, memory_search, memory_put, memory_delete\n"
)

_NX_T3 = (
    "T3: search (where, cluster_by, topic), query (catalog-aware; follow_links, "
    "depth, subtree),\n"
    "    store_list, store_get, store_put, collection_list\n"
    "Plans (T2): plan_search, plan_save\n"
    'Hint: where="section_type!=references" filters noise.\n\n'
    "WRITE-BACK: findings not stored = findings lost. store_put (T3) or memory_put "
    "(T2) before returning.\n"
)

_NX_ANSWER = (
    "\nnx_answer ONLY when the answer must be REDUCED FROM MANY DOCUMENTS "
    "(cross-corpus\n"
    "synthesis, ranking/comparing across documents, RDR research). Measured p50 "
    "80s, p95 217s,\n"
    "can time out at 300s: budget minutes. File:line, single-fact, already in T2 -> "
    "search/query (seconds; mean ~8s, tail to ~45s).\n"
)

_NX_AUTOLINK = (
    "\nAUTO-LINK RECIPE (drives store_put -> catalog links):\n"
    "  1. catalog_search your task references -> get target tumblers\n"
    '  2. scratch put (tag: "link-context") with target tumblers\n'
    "  3. store_put -- auto-creates catalog links from scratch context\n\n"
    "If link-context already in scratch (sibling agent did 1+2), skip to step 3.\n\n"
    'AGENT TAG: pass agent="<your-role>" to memory_put so nx tier-status slices '
    "writes by agent (nexus-9clx).\n"
)

_PHASE_GATE = (
    "\n## Phase Boundary Gate (mandatory)\n\n"
    "If your task closes a phase-review bead, run `/conexus:phase-review-gate "
    "<rdr-id> --phase N` BEFORE close. Pass 1 enumerates §Approach items; Pass 2 "
    "validates each has a closing-bead pointer (`ItemN=nexus-xxxx`) or explicit "
    "`none`. BLOCKED on any unaccounted item; phase close is gated on PASSED. "
    "Skipping the gate is the silent-scope-reduction failure mode: RDR-112 Phase 1 "
    "(nexus-52lb) lost days when the T3 daemon drop surfaced three phases later.\n"
)

_SEQTHINK = (
    "\n## Sequential Thinking\n\n"
    "Tool: mcp__plugin_conexus_sequential-thinking__sequentialthinking\n"
    "Call it BEFORE every decision (what to read, which fix, how to read a result, "
    "what to\n"
    'report) — not only "complex" ones; the qualifier is how it goes unused. The '
    "thought is\n"
    "the visible record of your reasoning.\n"
    "Params: needsMoreThoughts=true (continue), isRevision=true+revisesThought=N "
    "(correct),\n"
    'branchFromThought=N+branchId="alt" (explore).\n'
)

_OPERATORS = (
    "\n## Analytical operators (RDR-080)\n\n"
    "5 ops wrap claude -p (300s default; nx_plan_audit/nx_tidy 600s). One claude -p "
    "round\n"
    "trip costs ~11s minimum before any real work. Call directly, no Agent "
    "dispatch:\n"
    "  operator_summarize, operator_extract, operator_rank, operator_compare, "
    "operator_generate\n\n"
    "Multi-step retrieval (plan-match gate): nx_answer.\n"
)

_CATALOG_TOOLS = (
    "\n## Catalog - metadata-first queries (author, corpus, citations, "
    "provenance)\n"
    "(call as mcp__plugin_conexus_nexus-catalog__<tool>)\n\n"
    "  search, show, resolve\n"
    '  links (direction="in"|"out", link_type, depth=2 -> nodes+edges; live docs '
    "only)\n"
    "  link (from_tumbler, to_tumbler, link_type, created_by, spans)\n"
    "  link_query (admin/audit; includes orphans)\n"
)

_CATALOG_LINKING = (
    'Link spans: "chash:<sha256hex>" preferred (content-addressed); ":<start>-<end>" '
    "sub-chunk;\n"
    'fallback "L-L" or "C:S-E" (positional, may go stale). chash via search '
    "chunk_text_hash.\n\n"
    "Link types: cites, implements, implements-heuristic, supersedes, quotes, "
    "relates, comments.\n\n"
    "For full plan execution use /conexus:query.\n"
)


def _nx_env(session_id: str) -> dict[str, str]:
    """The environment every subprocess this hook spawns needs (nexus-7o1zh).

    Forces ``NX_SESSION_ID`` from the payload, matching the bash's
    unconditional ``export`` -- applied here to every subprocess call
    (git, the T2 scan, ``nx catalog links-for-file``, ``nx scratch list``),
    not only the one nearest the original export site, because bash's
    ``export`` affects every child process spawned afterward regardless of
    which command sits next to it in the file.

    **Deliberately does NOT set ``NX_T1_ALLOW_SHARED_FALLBACK``.**
    ``pre_close_verification.py``'s own ``_nx_env`` sets that flag, but its
    bash source (``pre_close_verification_hook.sh:86``) already exports it
    too -- that fix landed there, not here. ``subagent-start.sh`` forces
    ``NX_SESSION_ID`` (line 71) with no such flag, so an explicit session id
    with no live T1 lease under it can fail loud (``T1ServerNotFoundError``,
    nexus-f7xyq) exactly as it does in the bash today. That is a real,
    pre-existing gap in the script this ports, carried rather than
    silently closed -- see the module docstring's stance on scope.
    """
    env = dict(os.environ)
    if session_id:
        env["NX_SESSION_ID"] = session_id
    return env


def _run_captured(argv: list[str], *, env: dict[str, str], timeout: float) -> str:
    """Run *argv*, returning stdout with trailing newlines stripped.

    Mirrors bash's ``$(... 2>/dev/null)``: a nonzero exit, a timeout, or an
    outright failure to spawn are all indistinguishable from "no output" --
    the bash never inspects an exit code here either, only whether the
    captured text is non-empty.
    """
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=env
        )
    except Exception:  # noqa: BLE001 — carried: a missing/hanging tool is "no output"
        return ""
    return proc.stdout.rstrip("\n")


def _repo_root(env: dict[str, str]) -> str:
    """The MAIN repo root, resolved via ``--git-common-dir``.

    nexus-cnzei.2 (S6): ``--show-toplevel`` resolves to a worktree's OWN
    root for a worktree-isolated dispatch, whose basename is the
    worktree's directory name, not the project's. ``--git-common-dir``
    resolves to the same shared ``.git`` directory from either the primary
    checkout or any linked worktree, so its parent directory names the
    actual project consistently from both -- used for both the T2 scan and
    the Knowledge Map lookup below, one call shared between them (the bash
    calls this twice, once per section, under different variable names;
    same git state either way, so sharing the one call is a legitimate
    simplification, not a behaviour change).
    """
    if shutil.which("git") is None:
        return ""
    common_git_dir = _run_captured(
        ["git", "rev-parse", "--git-common-dir"], env=env, timeout=10
    )
    if not common_git_dir:
        return ""
    parent = os.path.dirname(common_git_dir) or "."
    try:
        return os.path.realpath(parent)
    except Exception:  # noqa: BLE001 — carried: an unresolvable path is "no project"
        return ""


def _t2_memory_section(repo_root: str) -> str:
    """The "## T2 Memory" section, or "" (subagent-start.sh:136-162).

    Calls :func:`nexus.hooks.t2_prefix_scan.scan` in-process (RDR-215
    bead nexus-b5ugt) rather than shelling out to the plugin-resident
    ``t2_prefix_scan.py`` -- see the module docstring above for the
    ``CLAUDE_PLUGIN_ROOT`` defect this replaces. The import is deferred so
    ``httpx``/the T2 HTTP client are only paid for when this section
    actually runs.
    """
    if not repo_root:
        return ""
    project = os.path.basename(repo_root)
    if not project:
        return ""
    from nexus.hooks.t2_prefix_scan import scan  # noqa: PLC0415 — deferred: see docstring above

    try:
        t2_out = scan(project)
    except Exception:  # noqa: BLE001 — best-effort context injection; a scan bug must not break the rest of the hook
        return ""
    if not t2_out:
        return ""
    return f"## T2 Memory\n{t2_out}\n\n"


def _extract_file_paths(task_text: str) -> list[str]:
    """The first 5 raw regex matches, deduplicated (subagent-start.sh:176-183).

    Order matches the bash exactly: cap at 5 BEFORE deduplicating via
    ``set()`` -- both here and there, iteration order over the resulting
    set is Python's normal (hash-randomized) set order, so this carries
    the bash's own nondeterminism rather than introducing new.
    """
    paths = _FILE_PATH_RE.findall(task_text)
    return list(set(paths[:5]))


def _linked_rdrs_section(task_text: str, env: dict[str, str]) -> str:
    """The "## Linked RDRs (files in task)" section, or "" (subagent-start.sh:173-199)."""
    if shutil.which("nx") is None:
        return ""
    file_paths = _extract_file_paths(task_text)
    if not file_paths:
        return ""
    link_chunks: list[str] = []
    for fp in file_paths:
        raw = _run_captured(
            ["nx", "catalog", "links-for-file", fp], env=env, timeout=30
        )
        matched = [ln for ln in raw.splitlines() if _ARROW_LINE_RE.match(ln)]
        if matched:
            link_chunks.append(f"  {fp}:\n" + "\n".join(matched) + "\n")
    link_out = "".join(link_chunks)
    if not link_out:
        return ""
    return "\n## Linked RDRs (files in task)\n" + link_out + "\n"


def _classify(agent_type: str, task_text: str) -> tuple[bool, bool]:
    """(skip_storage_docs, skip_operators) for this dispatch (subagent-start.sh:119-134).

    nexus-cnzei.6: the real SubagentStart payload carries ``agent_type``
    and never ``task``/``prompt``, so the ``agent_type`` arms are what
    actually fire in production; the ``task_text`` arms are a fallback
    that stays live only for a caller that does supply those fields (tests,
    and any future dispatcher that starts to).
    """
    if _CODE_NAV_TYPE_RE.search(agent_type) or _CODE_NAV_TASK_RE.search(task_text):
        return True, True
    if _CODE_REVIEW_TYPE_RE.search(agent_type) or _CODE_REVIEW_TASK_RE.search(task_text):
        return True, True
    return False, False


def _knowledge_map_section(repo_root: str) -> str:
    """The L1 Knowledge Map cache dump, or "" (subagent-start.sh:306-336)."""
    if not repo_root:
        return ""
    context_dir = os.path.join(os.path.expanduser("~"), ".config", "nexus", "context")
    repo_hash = hashlib.sha1(repo_root.encode()).hexdigest()[:8]  # noqa: S324 — cache key, not security
    repo_name = os.path.basename(repo_root)
    context_file = os.path.join(context_dir, f"{repo_name}-{repo_hash}.txt")
    if not os.path.isfile(context_file):
        return ""
    try:
        content = Path(context_file).read_text()
    except Exception:  # noqa: BLE001 — carried: an unreadable cache file is "no map"
        return ""
    return "\n" + content


def _t1_scratch_section(env: dict[str, str]) -> str:
    """The "## T1 Scratch (shared session state)" section, or "" (subagent-start.sh:390-399)."""
    if shutil.which("nx") is None:
        return ""
    t1_entries = _run_captured(["nx", "scratch", "list"], env=env, timeout=30)
    if not t1_entries or t1_entries == "No scratch entries.":
        return ""
    return f"\n## T1 Scratch (shared session state)\n{t1_entries}\n\n"


def run(payload: dict | None) -> HookResult:
    """Build the SubagentStart ``additionalContext`` injection."""
    data = payload or {}
    session_id = str(data.get("session_id") or "")
    task_text = " ".join([str(data.get("task", "")), str(data.get("prompt", ""))]).lower()
    agent_id = str(data.get("agent_id") or "")
    agent_type = str(data.get("agent_type") or "")

    env = _nx_env(session_id)
    repo_root = _repo_root(env)

    parts: list[str] = []

    if agent_id:
        parts.append(f"Claimant id: {agent_id} — mailbox: mailbox/{agent_id}\n")

    parts.append(_t2_memory_section(repo_root))
    parts.append(_linked_rdrs_section(task_text, env))

    parts.append(_RELAY)
    parts.append(_ORCH)
    parts.append(_WORKTREE_PREFLIGHT)

    skip_storage_docs, skip_operators = _classify(agent_type, task_text)

    if not skip_storage_docs:
        parts.append(_NX_TIERS)
        parts.append(_NX_TOOLS)
        parts.append(_NX_T3)
        parts.append(_NX_ANSWER)
        parts.append(_NX_AUTOLINK)
        if _PHASE_GATE_TASK_RE.search(task_text):
            parts.append(_PHASE_GATE)
        parts.append(_knowledge_map_section(repo_root))

    parts.append(_SEQTHINK)

    if not skip_operators:
        parts.append(_OPERATORS)

    if _CATALOG_TASK_RE.search(task_text):
        catalog_path = os.environ.get("NEXUS_CATALOG_PATH") or os.path.join(
            os.path.expanduser("~"), ".config", "nexus", "catalog"
        )
        if os.path.isdir(os.path.join(catalog_path, ".git")) and os.path.isfile(
            os.path.join(catalog_path, "documents.jsonl")
        ):
            parts.append(_CATALOG_TOOLS)
            parts.append(_CATALOG_LINKING)

    parts.append(_t1_scratch_section(env))

    body = "".join(parts)
    return HookResult(stdout=additional_context("SubagentStart", body))
