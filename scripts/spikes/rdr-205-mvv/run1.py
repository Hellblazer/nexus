#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-205 Phase 4 Step 3 (bead nexus-em75s.21) — MVV run 1.

Ten sub-agents, twenty tuples, the parked rd, the census agreement — the
run that closes Phase 4.

WHY THIS DRIVES THE CHECKOUT'S HOOK SCRIPTS DIRECTLY, NOT A REAL DISPATCH.
A genuine Claude Code session on this box runs the INSTALLED conexus
plugin (v7.40.0), which predates the tuple-space projection hooks this
RDR added (``subagent-start-tuple-async.sh`` / ``subagent-stop-tuple-
async.sh`` / the ``agent-dispatch-expect.sh`` EXPECT wiring). A real
``Agent`` dispatch here would exercise the OLD, pre-projection hooks and
prove nothing about this checkout's code. Driving
``conexus/hooks/scripts/{agent-dispatch-expect,subagent-start,subagent-
start-stamp,subagent-start-tuple-async,subagent-stop,subagent-stop-
tuple-async}.sh`` directly — as real subprocesses, stdin JSON payloads
shaped exactly like the ones ``tests/hooks/test_subagent_start_hook.py``,
``tests/hooks/test_agent_dispatch_expect.py`` and
``tests/hooks/test_subagent_stop_hook.py`` already pin as measured
Claude Code wire shapes — is the closest verification available until
the plugin ships this RDR's hooks. The in-session repeat against a real
dispatch is a residual for the RDR close (nexus-em75s.21's own text),
not for this bead.

Engine substrate: the same hermetic bundled-Postgres-17 +
``build-gate-jar.sh``-stamped-JAR machinery ``tests/_engine_substrate.
ensure_engine`` and MVV run 2's ``harness.py`` use. ``ledger/<session_id>``
is a SHIPPED v1 template (``service/src/main/resources/tuples/templates/
ledger.yaml``) — no ``NX_TUPLE_TEMPLATE_DIR`` override needed, unlike
run 2's test-only work-stealing template.

Tenant identity matters here in a way run 2 did not need to care about:
the async projector (``tuple_ledger_project.py``) is hard-pinned to
tenant ``"default"`` (RDR-205 "Identity and addressing" — no hook mints
anything, it only PRESENTS whatever cross-process lease it finds). This
harness therefore does NOT call ``mint_test_tenant`` (which mints an
isolated per-test tenant the hooks would never write into) — instead it
issues a mint-scoped credential against tenant ``"default"`` via the
ACTUAL consumer surface (``HttpTokenStore.issue_token(..., scope="mint")``,
the same call ``nx service token issue --scope mint`` makes), then mints
a real short-TTL data token through ``nexus.db.data_token.DataTokenManager``
and lets IT write the cross-process lease file — the exact file
``tuple_ledger_project.py`` reads, in the exact format, produced by the
real client code rather than hand-rolled JSON.

Usage::

    scripts/build-gate-jar.sh   # if service/ changed since the last build
    uv run python scripts/spikes/rdr-205-mvv/run1.py

Exit code is 0 iff every verification held. The JSON summary lands at
``--out`` (default ``scripts/spikes/rdr-205-mvv/last-run1-summary.json``,
not committed).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

_SPIKE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SPIKE_DIR.parents[2]
_HOOKS_DIR = _REPO_ROOT / "conexus" / "hooks" / "scripts"
_EXPECTATIONS_LIB = _REPO_ROOT / "tests" / "e2e" / "lib" / "expectations.sh"

# Dev-checkout process: every HTTP write this script or the hook
# subprocesses make trips nexus.db.service_endpoint.guard_production_write
# unless opted in (nexus-a2qhz). Every store here is pointed at
# state["base_url"] -- the 127.0.0.1 engine THIS SAME PROCESS boots via
# ensure_engine() a few lines below, never a resolved/exported production
# endpoint -- so the opt-in is genuine (same posture as MVV run 2's
# harness.py).
os.environ.setdefault(
    "NX_ALLOW_PROD_WRITE",
    "nexus-em75s.21 RDR-205 MVV run 1 -- every write targets the "
    "hermetic engine substrate this same process boots via "
    "tests._engine_substrate.ensure_engine(), never a resolved production "
    "endpoint.",
)

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import logging  # noqa: E402
import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

from tests._engine_substrate import ensure_engine  # noqa: E402

from nexus.db.data_token import DataTokenManager  # noqa: E402
from nexus.db.t2.http_token_store import HttpTokenStore  # noqa: E402
from nexus.db.t2.http_tuple_store import HttpTupleStore, ParkCapExceededError  # noqa: E402

_LEDGER_PREFIX = "ledger/"
_DEFAULT_TENANT = "default"
_PARK_TIMEOUT_S = 25  # CA 3's engine-side cap -- a caller loops past it, never sends more.


# ── engine + credential boot ────────────────────────────────────────────


@dataclass
class Boot:
    state: dict
    base_url: str
    mint_token: str
    data_token: str
    config_dir: Path
    version_body: dict
    head_sha: str


def boot(config_dir: Path) -> Boot:
    print("[boot] ensure_engine() -- booting hermetic PG17 + stamped service jar", flush=True)
    state = ensure_engine()
    base_url = state["base_url"]

    # nx service token issue --tenant default --scope mint (the actual
    # consumer surface; RDR-005 option A1) -- using the boot admin bearer
    # (bound server-side to the "*" wildcard tenant, TokenConstants.
    # BOOTSTRAP_ANY_TENANT), which authorizes issuing a bound credential
    # for any tenant string, "default" included.
    token_store = HttpTokenStore(base_url=base_url, _token=state["bearer"])
    issued = token_store.issue_token(_DEFAULT_TENANT, "rdr205-mvv-run1", scope="mint")
    mint_token = issued["token"]
    print(f"[boot] mint-scoped token issued for tenant={_DEFAULT_TENANT!r}", flush=True)

    # DataTokenManager mints the real short-TTL data token AND writes the
    # cross-process lease file (nexus.db.data_token's own format) into
    # config_dir -- the EXACT file conexus/hooks/scripts/
    # tuple_ledger_project.py reads directly. Using the real client code
    # to produce it, rather than hand-rolling the JSON, is the point: any
    # format drift between the two would show up as a hook-side SKIP, not
    # a silently-wrong fixture.
    dtm = DataTokenManager(config_dir=config_dir, mint_credential=lambda: mint_token)
    data_token = dtm.bearer_for(base_url, _DEFAULT_TENANT)
    if not data_token:
        raise RuntimeError("DataTokenManager.bearer_for returned no token")
    print(f"[boot] data token minted + lease published under {config_dir}", flush=True)

    import httpx

    version_resp = httpx.get(f"{base_url}/version", timeout=10.0)
    version_body = version_resp.json() if version_resp.status_code == 200 else {}
    head_sha = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    print(f"[boot] engine /version={version_body} worktree_head={head_sha}", flush=True)

    return Boot(
        state=state, base_url=base_url, mint_token=mint_token, data_token=data_token,
        config_dir=config_dir, version_body=version_body, head_sha=head_sha,
    )


# ── hook-shaped payloads (measured shapes -- tests/hooks/test_*.py) ─────


def _pretooluse_payload(
    *, session_id: str, tool_use_id: str, subagent_type: str,
) -> str:
    """PreToolUse(Agent) -- tests/hooks/test_agent_dispatch_expect.py's
    ``_pretooluse``, verbatim shape."""
    return json.dumps({
        "session_id": session_id,
        "transcript_path": "/tmp/t.jsonl",
        "cwd": str(_REPO_ROOT),
        "hook_event_name": "PreToolUse",
        "permission_mode": "acceptEdits",
        "prompt_id": "turn-1",
        "tool_use_id": tool_use_id,
        "tool_name": "Agent",
        "tool_input": {
            "description": "RDR-205 MVV run 1 probe agent",
            "prompt": "You are a probe teammate. Do nothing; SendMessage back.",
            "subagent_type": subagent_type,
            "run_in_background": True,
        },
    })


def _subagent_start_payload(*, session_id: str, agent_id: str, agent_type: str) -> str:
    """SubagentStart -- the measured unnamed-morphology shape (tests/hooks/
    test_agent_dispatch_expect.py's ``_subagent_start`` / RDR-184 scenario 27
    post-nexus-houpu remeasurement)."""
    return json.dumps({
        "session_id": session_id,
        "transcript_path": "/tmp/t.jsonl",
        "cwd": str(_REPO_ROOT),
        "hook_event_name": "SubagentStart",
        "prompt_id": "turn-1",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "task": "RDR-205 MVV run 1 probe agent",
        "prompt": "You are a probe teammate. Do nothing; SendMessage back.",
    })


def _transcript_with_sendmessage(tmp_dir: Path, agent_id: str) -> Path:
    """tests/hooks/test_subagent_stop_hook.py's ``_transcript`` shape,
    ``with_sendmessage=True`` -- a minimal agent transcript JSONL carrying
    one assistant SendMessage tool_use, so subagent-stop.sh's transcript
    scan records REPORTED rather than BLOCKED."""
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
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "SendMessage",
                        "input": {"to": "main", "content": f"done: {agent_id} report"},
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "finished"}]},
        },
    ]
    p = tmp_dir / f"{agent_id}.transcript.jsonl"
    p.write_text("\n".join(json.dumps(entry) for entry in lines) + "\n")
    return p


def _subagent_stop_payload(
    *, session_id: str, agent_id: str, agent_type: str, transcript_path: Path,
) -> str:
    return json.dumps({
        "session_id": session_id,
        "hook_event_name": "SubagentStop",
        "agent_id": agent_id,
        "agent_type": agent_type,
        "agent_transcript_path": str(transcript_path),
        "stop_hook_active": False,
    })


# ── driving the hook subprocesses ────────────────────────────────────────


@dataclass
class HookEnv:
    """The env every hook subprocess in this run shares: an isolated
    config/state tree (never touches the real ~/.config/nexus or
    ~/.local/state) plus the vars that route every client at THIS engine,
    tenant "default", unconditionally.

    tuple_ledger_project.py itself only ever reads NX_SERVICE_URL plus its
    own cross-process data-token LEASE FILE under NEXUS_CONFIG_DIR (by
    design -- "no hook mints anything", never a static-token fallback).
    NX_SERVICE_TOKEN is set here for every OTHER `nx` CLI invocation a hook
    script makes in passing (subagent-start.sh's `nx catalog links-for-
    file` / `nx scratch list`, and this run's own `nx tuple list --json`
    census read) -- set to the real minted DATA token (tenant "default"
    is bound server-side to the token itself; AuthFilter Decision 1/Phase E
    ignores any X-Nexus-Tenant header either way, measured directly
    against this engine), never the root/admin bearer, so a stray CLI
    call in this env can only ever act as tenant "default" on THIS
    hermetic engine.
    """

    config_dir: Path
    state_dir: Path
    base_url: str
    data_token: str
    extra_path: str | None = None

    def env(self) -> dict[str, str]:
        e = dict(os.environ)
        e["NEXUS_CONFIG_DIR"] = str(self.config_dir)
        e["XDG_STATE_HOME"] = str(self.state_dir)
        e["NX_SERVICE_URL"] = self.base_url
        e["NX_SERVICE_TOKEN"] = self.data_token
        e.pop("NX_SERVICE_PORT", None)
        e.pop("NX_SERVICE_HOST", None)
        # NX_ORCH_STOP_GUARD left UNSET -- default-ON block mode, same as a
        # real installed session (P1.G, 2026-07-17).
        e.pop("NX_ORCH_STOP_GUARD", None)
        if self.extra_path:
            e["PATH"] = self.extra_path + os.pathsep + e.get("PATH", "")
        return e


def _run_hook(script: Path, payload: str, henv: HookEnv, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        input=payload, capture_output=True, text=True, timeout=timeout,
        env=henv.env(), cwd=str(_REPO_ROOT),
    )


@dataclass
class AgentRun:
    agent_id: str
    agent_type: str
    dispatch_id: str
    start_rc: dict = field(default_factory=dict)
    stop_rc: dict = field(default_factory=dict)


def run_ten_agents(
    session_id: str, henv: HookEnv, transcript_dir: Path, *,
    late_report_agent_index: int, late_delay_s: float,
) -> tuple[list[AgentRun], threading.Thread, AgentRun]:
    """Dispatch ten sub-agents of two types through the six hook scripts,
    in the order a real session fires them: PreToolUse(Agent) once per
    dispatch, then SubagentStart's three entries, then (after simulated
    agent work) SubagentStop's two entries.

    One agent (``late_report_agent_index``) has its SubagentStop pair
    delayed by ``late_delay_s`` -- driven from a background thread -- so
    the parked-rd verification below has a genuine "not yet landed, then
    lands" transition to observe, rather than a report that was already
    sitting in the space before the park call was even issued.
    """
    agent_types = ["developer", "code-review-expert"]
    runs: list[AgentRun] = []
    for i in range(10):
        agent_type = agent_types[i % 2]
        agent_id = f"a{uuid.uuid4().hex[:16]}"
        dispatch_id = f"toolu_{i:02d}{uuid.uuid4().hex[:18]}"
        runs.append(AgentRun(agent_id=agent_id, agent_type=agent_type, dispatch_id=dispatch_id))

    print(f"[dispatch] {len(runs)} agents: "
          f"{sum(1 for r in runs if r.agent_type == 'developer')} developer, "
          f"{sum(1 for r in runs if r.agent_type == 'code-review-expert')} code-review-expert",
          flush=True)

    # ── PreToolUse(Agent) -> agent-dispatch-expect.sh (EXPECT rows) ──────
    for r in runs:
        proc = _run_hook(
            _HOOKS_DIR / "agent-dispatch-expect.sh",
            _pretooluse_payload(session_id=session_id, tool_use_id=r.dispatch_id, subagent_type=r.agent_type),
            henv,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"agent-dispatch-expect.sh rc={proc.returncode} stderr={proc.stderr}")

    # ── SubagentStart's three sibling entries (START row + ledger tuple) ─
    for r in runs:
        payload = _subagent_start_payload(session_id=session_id, agent_id=r.agent_id, agent_type=r.agent_type)
        for script in ("subagent-start.sh", "subagent-start-stamp.sh", "subagent-start-tuple-async.sh"):
            proc = _run_hook(_HOOKS_DIR / script, payload, henv)
            r.start_rc[script] = proc.returncode
            if proc.returncode != 0:
                raise RuntimeError(f"{script} rc={proc.returncode} stderr={proc.stderr}")

    # ── SubagentStop's two sibling entries (REPORTED row + report tuple) ─
    # Every agent but the designated late one stops immediately; the late
    # one is driven from a background thread after late_delay_s, so the
    # main thread can issue its parked rd() BEFORE that report lands.
    def _stop_one(r: AgentRun) -> None:
        transcript = _transcript_with_sendmessage(transcript_dir, r.agent_id)
        payload = _subagent_stop_payload(
            session_id=session_id, agent_id=r.agent_id, agent_type=r.agent_type,
            transcript_path=transcript,
        )
        for script in ("subagent-stop.sh", "subagent-stop-tuple-async.sh"):
            proc = _run_hook(_HOOKS_DIR / script, payload, henv)
            r.stop_rc[script] = proc.returncode
            if proc.returncode != 0:
                raise RuntimeError(f"{script} rc={proc.returncode} stderr={proc.stderr}")

    late_runs = [runs[late_report_agent_index]]
    immediate_runs = [r for i, r in enumerate(runs) if i != late_report_agent_index]

    for r in immediate_runs:
        _stop_one(r)

    def _delayed_stop() -> None:
        time.sleep(late_delay_s)
        for r in late_runs:
            _stop_one(r)
        print(f"[stop] late report landed for {late_runs[0].agent_id} after {late_delay_s}s", flush=True)

    late_thread = threading.Thread(target=_delayed_stop, daemon=True)
    late_thread.start()

    return runs, late_thread, late_runs[0]  # type: ignore[return-value]


# ── parked rd verification ───────────────────────────────────────────────


def parked_rd_for_report(store: HttpTupleStore, subspace: str, agent_id: str, *, overall_budget_s: float) -> tuple[bool, float, int]:
    """Park on the named agent's report tuple, looping past the engine's
    per-call cap (CA 3, 25s) rather than sending a longer single request
    -- exactly the orchestration skill's "a wait of minutes is a LOOP of
    parked calls" contract. Returns (found, elapsed_s, park_calls_made).
    """
    deadline = time.monotonic() + overall_budget_s
    calls = 0
    start = time.monotonic()
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        this_timeout = int(min(_PARK_TIMEOUT_S, max(1, remaining)))
        calls += 1
        try:
            rows = store.rd(subspace, keys_pattern={"agent_id": agent_id, "kind": "report"}, timeout_s=this_timeout)
        except ParkCapExceededError:
            continue  # caller-side cap misuse; loop again with a corrected timeout
        if rows:
            return True, time.monotonic() - start, calls
    return False, time.monotonic() - start, calls


# ── census agreement (space vs. TSV) ─────────────────────────────────────


def _nx_wrapper(worktree_root: Path, wrapper_dir: Path) -> Path:
    """A ``nx`` on PATH that resolves to THIS worktree's dev build (the
    globally installed release, v7.40.0 at the time of this run, has no
    ``nx tuple`` command yet) -- so expectations.sh's
    ``_expectations_census_space``, which shells out to a bare ``nx tuple
    list --prefix ledger/ --json``, exercises this checkout's code."""
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper = wrapper_dir / "nx"
    wrapper.write_text(
        "#!/bin/bash\n"
        f'exec uv run --project "{worktree_root}" nx "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper_dir


def run_expectations_census(session_id: str, henv: HookEnv, nx_wrapper_dir: Path) -> str:
    env = henv.env()
    env["PATH"] = str(nx_wrapper_dir) + os.pathsep + env.get("PATH", "")
    proc = subprocess.run(
        ["bash", "-c", f"source '{_EXPECTATIONS_LIB}'; expectations_census '{session_id}'"],
        capture_output=True, text=True, timeout=60, env=env, cwd=str(_REPO_ROOT),
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"expectations_census unexpected rc={proc.returncode} stderr={proc.stderr}")
    return proc.stdout


# ── main ──────────────────────────────────────────────────────────────────


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(_SPIKE_DIR / "last-run1-summary.json"))
    ap.add_argument("--late-delay-s", type=float, default=3.0)
    ap.add_argument("--overall-park-budget-s", type=float, default=30.0)
    args = ap.parse_args(argv[1:])

    run_id = uuid.uuid4().hex[:12]
    session_id = f"rdr205run1{run_id}"  # path-safe charset: alnum start, alnum/_/-
    print(f"[run] session_id={session_id}", flush=True)

    with tempfile.TemporaryDirectory(prefix="rdr205-mvv-run1-") as tmp:
        tmp_path = Path(tmp)
        config_dir = tmp_path / "config"
        state_dir = tmp_path / "state"
        transcript_dir = tmp_path / "transcripts"
        nx_wrapper_dir = tmp_path / "bin"
        for d in (config_dir, state_dir, transcript_dir):
            d.mkdir(parents=True, exist_ok=True)

        b = boot(config_dir)
        henv = HookEnv(config_dir=config_dir, state_dir=state_dir, base_url=b.base_url, data_token=b.data_token)
        _nx_wrapper(_REPO_ROOT, nx_wrapper_dir)

        # The harness's OWN verification store: the SAME data token the
        # hooks' async projector presents (strictly bound server-side to
        # tenant "default" -- AuthFilter Decision 1, Phase E/nexus-
        # gmiaf.32.5: the token's own tenant_id is authoritative and the
        # client X-Nexus-Tenant header is IGNORED, not merely overridable,
        # so the root/admin bearer used to mint credentials cannot be
        # coerced into reading tenant "default" via that header -- measured
        # directly against this engine, not assumed from the older
        # docstring's now-retired wildcard-grant description). Using the
        # data token itself guarantees the verification reads land in
        # EXACTLY the tenant the hooks wrote to.
        verify_store = HttpTupleStore(base_url=b.base_url, _token=b.data_token)

        subspace = f"{_LEDGER_PREFIX}{session_id}"

        t0 = time.monotonic()
        runs, late_thread, late_run = run_ten_agents(
            session_id, henv, transcript_dir,
            late_report_agent_index=0, late_delay_s=args.late_delay_s,
        )
        dispatch_elapsed = time.monotonic() - t0
        print(f"[dispatch] ten agents' EXPECT+START (+ 9/10 STOP) landed in {dispatch_elapsed:.2f}s", flush=True)

        # ── Verification 1: the parked rd for the late agent's report ────
        # Issued immediately after dispatch returns -- the late agent's
        # report is NOT written yet (it lands late_delay_s later, from the
        # background thread started inside run_ten_agents). A genuine
        # park-then-wake, not a poll-then-see.
        print(f"[park] parking on {late_run.agent_id}'s report (budget {args.overall_park_budget_s}s)...", flush=True)
        park_t0 = time.monotonic()
        found, park_elapsed, park_calls = parked_rd_for_report(
            verify_store, subspace, late_run.agent_id, overall_budget_s=args.overall_park_budget_s,
        )
        late_thread.join(timeout=max(1.0, args.late_delay_s + 5.0))
        print(f"[park] found={found} elapsed={park_elapsed:.2f}s calls={park_calls} "
              f"(late_delay_s={args.late_delay_s})", flush=True)

        # ── Verification 2: twenty tuples over ten agent ids ─────────────
        all_rows = verify_store.rd(subspace, n=100)
        by_agent: dict[str, set[str]] = {}
        for row in all_rows:
            by_agent.setdefault(row.keys.get("agent_id", ""), set()).add(row.keys.get("kind", ""))
        stats = verify_store.subspace_stats(subspace)
        print(f"[space] subspace_stats total={stats.total} available={stats.available} "
              f"claimed={stats.claimed} dead={stats.dead}", flush=True)
        print(f"[space] {len(all_rows)} tuples over {len(by_agent)} agent ids", flush=True)

        per_agent_ok = []
        for r in runs:
            kinds = by_agent.get(r.agent_id, set())
            ok = kinds == {"start", "report"}
            per_agent_ok.append({"agent_id": r.agent_id, "agent_type": r.agent_type, "kinds": sorted(kinds), "ok": ok})

        # ── Verification 3: census agreement, space vs. TSV ──────────────
        census_out = run_expectations_census(session_id, henv, nx_wrapper_dir)
        agent_lines = [ln for ln in census_out.splitlines() if ln.startswith("AGENT\t")]
        space_present = [ln for ln in census_out.splitlines() if ln.startswith("SPACE_PRESENT\t")]
        print("[census] TSV AGENT rows:", len(agent_lines), flush=True)
        print("[census]", "\n[census] ".join(space_present) if space_present else "[census] NO SPACE_PRESENT LINE", flush=True)

        # Row-for-row: every TSV AGENT row's terminal state is REPORTED,
        # and the corresponding agent_id shows both kinds in the space.
        tsv_agent_ids = set()
        row_for_row = []
        for ln in agent_lines:
            _, aid, ty, terminal, decl = ln.split("\t")
            tsv_agent_ids.add(aid)
            space_kinds = by_agent.get(aid, set())
            row_for_row.append({
                "agent_id": aid, "agent_type": ty, "tsv_terminal": terminal,
                "declared": decl, "space_kinds": sorted(space_kinds),
                "agrees": terminal == "REPORTED" and space_kinds == {"start", "report"},
            })

        summary = {
            "session_id": session_id,
            "engine": {
                "base_url": b.base_url,
                "version": b.version_body,
                "worktree_head": b.head_sha,
            },
            "agents": [{"agent_id": r.agent_id, "agent_type": r.agent_type, "dispatch_id": r.dispatch_id} for r in runs],
            "dispatch_elapsed_s": dispatch_elapsed,
            "parked_rd": {
                "agent_id": late_run.agent_id, "late_delay_s": args.late_delay_s,
                "found": found, "elapsed_s": park_elapsed, "park_calls": park_calls,
            },
            "space_census": {
                "subspace": subspace, "total": stats.total, "available": stats.available,
                "n_agent_ids": len(by_agent), "per_agent": per_agent_ok,
            },
            "tsv_census": {
                "agent_row_count": len(agent_lines), "space_present_lines": space_present,
                "raw": census_out,
            },
            "row_for_row_agreement": row_for_row,
            "retention_window_days": 90,
        }

        out_path = Path(args.out)
        out_path.write_text(json.dumps(summary, indent=2))
        print(f"[out] summary written to {out_path}", flush=True)

        ok = (
            found
            and stats.total == 20
            and len(by_agent) == 10
            and all(a["ok"] for a in per_agent_ok)
            and len(agent_lines) == 10
            and len(tsv_agent_ids) == 10
            and all(r["agrees"] for r in row_for_row)
        )
        print(f"[verdict] {'PASSED' if ok else 'FAILED'}", flush=True)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
