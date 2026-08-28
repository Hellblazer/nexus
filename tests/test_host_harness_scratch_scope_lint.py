# SPDX-License-Identifier: AGPL-3.0-or-later
"""Repo-wide lint: every host-run harness that indexes or stores into a
service is on an exact, reviewed allowlist naming WHY it is safe
(nexus-8tnz2).

PROBLEM (T2 ``nexus/catalog-cleanup-2026-08-03-executed-and-prevention``
[21385] item 3, prevention doctrine unbuilt until this lint): benchmark/
gate debris lands in the production tenant and nothing at write time keeps
it out. Live 2026-08-28 (``collection_list``, read-only): 13
``code__test-repo-<hex>__voyage-code-3__v1`` collections (1 doc each),
``docs__hotfix_smoke``, ``docs__local_smoketest_336``, ``knowledge__val530``,
``docs__1-2188`` (2,828 docs; owner ``1.2188`` does not exist) -- all with
zero catalog owner/documents. ``nx catalog doctor`` already classifies this
population as ``t3_orphans``; nothing at write time keeps it from
recurring.

THE CENSUS (nexus-8tnz2 developer audit, 2026-08-28, extended in the
fix-round after code-review-expert / substantive-critic round 1: full
per-file table in the bead's hand-back): every ``nx index`` / ``nx
store`` / ``nx collection`` / ``store_put`` / ``nexus.mcp.core`` site
across every TRACKED SHELL (``.sh``) AND PYTHON (``.py``) file under
``tests/e2e/**`` and ``scripts/**`` falls into one of five already-safe
shapes -- NOT ONE of the 34 files found needed a new provisioning
mechanism:

  READ-ONLY       ``nx collection list`` / ``nx store list``, or an
                   ``operator_*`` / ``nx_answer`` MCP tool call -- no
                   catalog/T3 write path exists to land debris through.
                   All 10 ``operator_*`` tools carry ``readOnlyHint``
                   (src/nexus/mcp/core.py); ``nx_answer``'s only write is
                   a T2 ``nx_answer_runs`` telemetry row, not catalog/T3
                   debris.
  CONTAINER       runs INSIDE a throwaway Docker container that
                   provisions its own bundled PG/engine fresh per run (the
                   migration-rehearsal ``rehearse_*.sh`` family plus
                   ``seed_legacy.py``, invoked only from inside it), each
                   with its own header comment saying so -- the same
                   isolation class ``test_nx_init_autostart_collision_lint
                   .py``'s ``CONTAINER_ALLOWLIST`` already recognizes for
                   the launchd/systemd-domain hazard. No shared/production
                   tenant is reachable from inside the container at all.
  NX_LOCAL+SANDBOX forces ``NX_LOCAL=1`` (no cloud tenant concept exists in
                   local mode at all) or an isolated ``NEXUS_CONFIG_DIR``/
                   ``$HOME`` under a scratch directory that is not the
                   operator's real config -- including an IN-FILE guard
                   (``scripts/validate/01-mcp-core.py``'s
                   ``_refuse_unless_sandboxed()``) for a file that calls
                   MCP tools directly in-process and is independently
                   ``__main__``-runnable, where reliance on an invoker's
                   env alone was the exact gap fix-round CRITICAL 1 named.
  MARKER+SNAPSHOT  the one harness that DELIBERATELY targets the
                   operator's live cloud service (the throughput bench):
                   marker-scoped owner names (``BENCH_MARKER="benchidx-"``),
                   a before/after ``nx collection list`` snapshot, and
                   EXIT-time teardown that deletes every marker-scoped
                   survivor.
  PROSE-ONLY       a docstring/comment/print()-suggested-command mentioning
                   one of these verbs, never executed by the file itself.

Because every EXISTING site is already safe, there is no shared preamble
to build or source (a first draft of this bead built
``tests/e2e/lib/scratch_tenant.sh`` before this census came back --
DEVIATION, reverted: a mechanism with zero consumers is preventive scope
beyond the evidence). ``ALLOWLIST`` below is therefore not an escape
hatch from a stricter default -- it IS the enforcement: every matched
line must be a NAMED, exact-counted entry, so a NEW site outside it fails
loudly rather than silently joining a population nothing is watching.

A future site that genuinely needs the operator's live service has TWO
conforming routes, named in the failure directive below:

  1. Self-provision an engine and mint its own tenant: ``POST
     <engine>/v1/tenants/create`` under the boot bearer (operator-only;
     TokenAdminHandler.java) -- the ``tests/_engine_substrate.py``
     ``mint_test_tenant`` precedent, or the ``local-service-gate.sh``
     self-provisioned-service-plus-fixture-minted-tenant precedent.
  2. The throughput bench's MARKER+SNAPSHOT shape: a marker-scoped owner
     name, a before/after ``nx collection list`` assertion, and
     EXIT-time teardown of every marker-scoped survivor.

A NEW site outside the ledger, or an EXISTING allowlisted file whose
match count changed, fails loudly by exact file:line -- never a silent
pass. Deliberately dumb (regex, per line), matching the template
(``test_nx_init_autostart_collision_lint.py``)'s own stated philosophy:
the goal is a tripwire that cannot be silently satisfied, not a shell
parser -- a line that merely MENTIONS one of these verbs inside an echo/
label string still counts as a "site" needing a reviewed, named entry.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

#: `nx index`, `nx store`, `nx collection` (bare or via this repo's `_nx`
#: HOME-swap wrapper form -- same wrapper the nexus-d5yu5 lint scans
#: identically to the bare form, since a wrapped HOME changes nothing about
#: which tenant a write lands in either); the `store_put` MCP tool name
#: (as invoked via a `claude_prompt`-driven live session, a direct Python
#: `store_put(...)` call, or a shell function of that name); and a direct
#: `nexus.mcp.core` reference (nexus-8tnz2 fix-round CRITICAL 1 -- a
#: qualified `nexus.mcp.core.<other_write_tool>(...)` call would carry
#: "nexus.mcp.core" without the bare name "store_put" appearing at all).
#: Scanned identically across shell (`.sh`) AND Python (`.py`) sources --
#: the ORIGINAL scan globbed `*.sh` only, which missed two real in-process
#: MCP-tool call sites entirely (scripts/validate/01-mcp-core.py,
#: scripts/spikes/spike_rdr089_delos.py -- see ALLOWLIST/DEVIATION below).
_NX_INDEX_RE = re.compile(r"(?<![\w])_?nx\s+index(?=\s|$)")
_NX_STORE_RE = re.compile(r"(?<![\w])_?nx\s+store(?=\s|$)")
_NX_COLLECTION_RE = re.compile(r"(?<![\w])_?nx\s+collection(?=\s|$)")
_STORE_PUT_RE = re.compile(r"\bstore_put\b")
_NEXUS_MCP_CORE_RE = re.compile(r"nexus\.mcp\.core")
_ANY_INVOCATION_RE = re.compile(
    "|".join(
        p.pattern for p in (
            _NX_INDEX_RE, _NX_STORE_RE, _NX_COLLECTION_RE, _STORE_PUT_RE, _NEXUS_MCP_CORE_RE,
        )
    )
)

#: Exact per-file allowlist ledger (nexus-8tnz2 developer audit,
#: 2026-08-28). Maps relative path -> (exact live match count, reason).
#: Both directions matter: a live count above the ledger is a NEW or GROWN
#: site (review required); a live count below means a site was
#: fixed/removed and the entry must be lowered (see
#: test_ledger_matches_live_count) -- a stale high entry is a free slot a
#: future unguarded site could occupy unreviewed.
ALLOWLIST: dict[str, tuple[int, str]] = {
    # ── MARKER+SNAPSHOT: the one harness that deliberately targets the
    # operator's live cloud service. Does all three things the module
    # docstring names: BENCH_MARKER="benchidx-" scoped owner names,
    # before/after `nx collection list` snapshots (collections-before.txt /
    # collections-after.txt), and teardown.sh deletes every benchidx-*
    # collection the after-snapshot still shows.
    "tests/e2e/index-throughput-bench/run.sh": (
        2, "MARKER+SNAPSHOT precedent: cloud-mode throughput bench against the "
           "operator's live service by design -- marker-scoped owner "
           "(BENCH_MARKER=\"benchidx-\") + before-snapshot `nx collection list` "
           "here, teardown.sh below completes the after-snapshot + delete + "
           "EXIT-time teardown.",
    ),
    "tests/e2e/index-throughput-bench/teardown.sh": (
        2, "MARKER+SNAPSHOT precedent (see run.sh): after-snapshot `nx collection "
           "list` + `nx collection delete` of every benchidx-* survivor -- the "
           "teardown half of the same marker-scoped/before-after/"
           "teardown-on-EXIT triple.",
    ),
    # ── CONTAINER: runs INSIDE a throwaway Docker container that
    # provisions its OWN bundled PG/engine fresh per run (own header
    # comment: "runs INSIDE the container"). No shared/production tenant
    # is reachable from inside the container at all -- the container
    # itself is the isolation boundary, the same class
    # test_nx_init_autostart_collision_lint.py's CONTAINER_ALLOWLIST
    # already recognizes for the launchd/systemd-domain hazard.
    "tests/e2e/migration-rehearsal/lib/store_put_census.sh": (
        1, "CONTAINER: shared function `census_concurrent_store_puts`, sourced+"
           "called only by rehearse_shakeout.sh (container-executed, see below) "
           "or by tests/test_shakeout_store_put_census.py with a STUB `nx` first "
           "on $PATH -- the real `nx store put` binary is never reached from the "
           "host.",
    ),
    "tests/e2e/migration-rehearsal/rehearse.sh": (
        10, "CONTAINER: runs INSIDE the container (own header comment); "
            "provisions its own bundled PG/engine fresh per run via `nx init "
            "--service`; the remaining matches are further invocation/echo "
            "lines, all still container-scoped.",
    ),
    "tests/e2e/migration-rehearsal/rehearse_acquire.sh": (
        3, "CONTAINER: runs INSIDE the container (own header comment: "
           "\"PUBLISHED-ARTIFACT acquire gate\"); provisions its own bundled "
           "PG/engine fresh per run; 2 extra matches are echo/label lines.",
    ),
    "tests/e2e/migration-rehearsal/rehearse_candidate_migration.sh": (
        8, "CONTAINER: runs INSIDE the container (own header comment); Stage 2b "
           "is `nx init --service` provisioning its own bundled PG fresh per "
           "run; 7 more matches, all container-scoped.",
    ),
    "tests/e2e/migration-rehearsal/rehearse_era_hop.sh": (
        1, "CONTAINER: runs INSIDE the container (own header comment: "
           "\"ERA-SPANNING HOP MVV\"); provisions its own bundled PG/engine "
           "fresh per run.",
    ),
    "tests/e2e/migration-rehearsal/rehearse_fullstack.sh": (
        8, "CONTAINER: runs INSIDE the container (own header comment: "
           "\"Full-stack isolated shakeout\"); provisions its own bundled "
           "PG/engine fresh per run; 7 extra matches are store_put/search/"
           "nx_answer MCP-tool mentions inside the container.",
    ),
    "tests/e2e/migration-rehearsal/rehearse_shakeout.sh": (
        7, "CONTAINER: runs INSIDE the container (own header comment); Phase D "
           "sources lib/store_put_census.sh (see above) for the "
           "concurrent-store-put census, entirely inside the throwaway "
           "container; the remaining matches are further invocation/echo "
           "lines from the same phase.",
    ),
    "tests/e2e/migration-rehearsal/rehearse_shakeout_e2e.sh": (
        22, "CONTAINER: runs INSIDE the container (own header comment: "
            "\"Daily-driver install-to-shakeout journey\"); provisions its own "
            "bundled PG/engine fresh per run, including an `nx index pdf` leg "
            "on a synthetic fixture; the remaining ~20 matches are further "
            "`nx index pdf`/progress-label lines through its 10-step journey, "
            "all inside the container.",
    ),
    # ── NX_LOCAL+SANDBOX: no cloud "production tenant" concept applies
    # (the bundled engine is a private per-install Postgres under the
    # scratch HOME/SANDBOX/NEXUS_CONFIG_DIR) -- the specific hazard this
    # lint guards against (shared cloud tenant debris) cannot occur here.
    "scripts/validate/03-cli.sh": (
        3, "NX_LOCAL+SANDBOX: scripts/validate/lib.sh sets NX_LOCAL=1 + "
           "HOME=\"$SANDBOX\"; `nx collection list` / `nx store list` are also "
           "both read-only listings, no write path at all; the third match is "
           "the \"nx collection list\" step label.",
    ),
    "tests/e2e/scenarios/03_skills.sh": (
        1, "NX_LOCAL+SANDBOX: LOCAL mode + isolated $TEST_HOME "
           "(tests/e2e/run.sh forces `export NX_LOCAL=1` and swaps $HOME before "
           "any scenario runs) -- `nx index repo` indexes into the scratch "
           "HOME's own bundled engine.",
    ),
    "tests/e2e/scenarios/cc-catalog-decomposition-smoke.sh": (
        2, "NX_LOCAL+SANDBOX: runs against $HOME/nexus-sandbox, populated by "
           "release-sandbox.sh's own isolated generation install -- not the "
           "operator's live production install. The `store_put` occurrences "
           "are a `claude_prompt` payload (an English instruction to a live "
           "Claude session) and an `echo` progress line, not a direct "
           "`nx`/curl invocation.",
    ),
    "tests/e2e/fresh-install-mvv.sh": (
        3, "NX_LOCAL+SANDBOX: HOME=\"$HOME_DIR\" (L364, L395) + NX_LOCAL=1 "
           "(L367); the real `_nx store put` / `_nx index md` invocations "
           "(L572, L590, L614) target the scratch HOME's own bundled local "
           "engine.",
    ),
    "tests/e2e/local-index-memory-gate.sh": (
        2, "NX_LOCAL+SANDBOX (isolated config dir): HOME=\"$HOME_DIR\" "
           "NEXUS_CONFIG_DIR=\"$ISOLATED_CONFIG_DIR\" NX_LOCAL=1 (L639-644), "
           "with an explicit _die guard (L599) if NEXUS_CONFIG_DIR ever equals "
           "the operator's real config dir; both matches (L280, L886) are "
           "progress-message labels, not invocations.",
    ),
    "tests/e2e/rdr195-voyage-mvv.sh": (
        2, "NX_LOCAL+SANDBOX + self-provisioned engine: HOME=\"$SANDBOX_HOME\" "
           "+ NX_LOCAL=1 (L279-280) -- a real Voyage API key is used for "
           "embedding, but every write lands in the throwaway local engine "
           "under $SANDBOX_HOME, never a cloud tenant.",
    ),
    "tests/e2e/release-sandbox.sh": (
        24, "NX_LOCAL+SANDBOX: SANDBOX=\"$HOME/nexus-sandbox\" (L40), with "
            "HOME=\"$SANDBOX\" + NX_LOCAL=1 re-exported at each entry point "
            "(L685, L764, L1339); real invocations at L898, L913, L969, L971, "
            "L983, L991, L1011 all target the sandbox's own bundled engine; "
            "the remaining ~17 matches are echo/`_index_floor_check` progress "
            "labels quoting step names like \"nx index repo\".",
    ),
    "tests/e2e/warm-reindex-skip-gate.sh": (
        4, "NX_LOCAL+SANDBOX: the repo's `_nx()` HOME-swap wrapper (L77-97: "
           "HOME=\"$HOME_DIR\", NX_LOCAL=1) fronts all 4 matches, each a real "
           "`_nx index repo` call through it into the scratch HOME's own "
           "bundled engine.",
    ),
    "tests/e2e/cloud-client-path-gate.sh": (
        1, "READ-ONLY: every leg in this gate is a read (per its own header, "
           "it asserts the engine's read-path contracts survive the public "
           "edge); the single match is prose inside a Python error string "
           "(\"...nx collection prune are all inert for every cloud "
           "client\"), not an invocation.",
    ),
    # ── Python sites (nexus-8tnz2 fix-round CRITICAL 1: the scan originally
    # globbed *.sh only). Every operator_* import below is READ-ONLY by
    # construction: all 10 operator_* MCP tools carry readOnlyHint
    # (src/nexus/mcp/core.py:5178-5936) with no store_put / nx index /
    # collection-create path; nx_answer's only write is the T2
    # nx_answer_runs telemetry row (core.py:7057) -- not catalog/T3 debris.
    "scripts/bench/paths.py": (
        2, "READ-ONLY: nx_answer import (telemetry-only write, not catalog/T3 debris).",
    ),
    "scripts/bench/synthesis_tier_study.py": (
        2, "READ-ONLY: line 238 dispatches summarize/generate/compare/aggregate via "
           "getattr(core, fn_name) -- all read-only operator_* tools.",
    ),
    "scripts/bundle_sandbox_probe.py": (
        1, "READ-ONLY: operator_extract/operator_summarize only (lines 98-113).",
    ),
    "scripts/migrate_art_papers.py": (
        1, "PROSE-ONLY: line 229 is inside a print() suggesting a command to a human "
           "operator, never executed by this script itself.",
    ),
    "scripts/spikes/bench_rdr089_sql_fast_path.py": (
        3, "READ-ONLY: operator_filter/operator_groupby/operator_aggregate imports.",
    ),
    "scripts/spikes/spike_a_check_stability.py": (
        1, "READ-ONLY: operator_check import.",
    ),
    "scripts/spikes/spike_rdr090_5q.py": (
        1, "READ-ONLY: nx_answer import (telemetry-only write, not catalog/T3 debris).",
    ),
    "scripts/spikes/spike_rdr094_lifecycle.py": (
        2, "READ-ONLY: launches `python -m nexus.mcp.core` under a per-run tmpdir "
           "NEXUS_CONFIG_DIR set in main(); the harness only sends OS signals + "
           "closes stdin to probe lifecycle, never a tools/call.",
    ),
    "scripts/spikes/spike_rdr096_a1_chroma_reassembly.py": (
        1, "READ-ONLY: operator_verify import.",
    ),
    "scripts/spikes/spike_rdr096_multichunk_reassembly.py": (
        2, "READ-ONLY/PROSE-ONLY: operator_verify import (line 152); line 361 is a "
           "descriptive note string, not an invocation.",
    ),
    "scripts/validate/01-mcp-core.py": (
        21, "NX_LOCAL+SANDBOX (in-file guard, nexus-8tnz2 fix-round CRITICAL 1): calls "
            "MCP tools (store_put, collection_list, memory_put, ...) directly "
            "in-process via nexus.mcp.core -- safe when run through "
            "scripts/validate/run-all.sh (its sourced lib.sh exports NX_LOCAL=1 + "
            "swaps $HOME before this file runs), but also independently "
            "__main__-runnable. `_refuse_unless_sandboxed()` (this file's own "
            "`main()`) now refuses loudly (exit 2) unless NX_LOCAL=1 is already set, "
            "closing the gap the substantive-critic review flagged rather than "
            "relying on invoker discipline alone.",
    ),
    "scripts/validate/05-plugin-wiring.py": (
        1, "READ-ONLY: ._tool_manager.list_tools() introspection only (lines 70-79), "
           "never a tool invocation.",
    ),
    "scripts/validate/07-agent-behavior.py": (
        1, "READ-ONLY: nx_answer import (telemetry-only write, not catalog/T3 debris).",
    ),
    "tests/e2e/index-throughput-bench/test_aggregate.py": (
        1, "PROSE-ONLY: docstring mention of \"nx index repo\" describing what this "
           "test aggregates, not a real invocation.",
    ),
    "tests/e2e/migration-rehearsal/seed_legacy.py": (
        4, "CONTAINER: every real invocation is `python /home/nexus/seed_legacy.py` "
           "from the rehearse_*.sh family (container-executed, see above); run.sh "
           "only `cp`'s this file into container staging, never runs it on the host.",
    ),
}


@dataclass(frozen=True)
class _Site:
    file: str
    line: int
    text: str


def _tracked_scripts() -> list[Path]:
    """Every tracked shell (`.sh`) AND Python (`.py`) file under
    `tests/e2e/**` / `scripts/**` -- nexus-8tnz2 fix-round CRITICAL 1: the
    original scan globbed `.sh` only, missing real in-process MCP-tool
    call sites that carry no shell invocation at all."""
    out = subprocess.run(
        [
            "git", "ls-files",
            "tests/e2e/*.sh", "tests/e2e/**/*.sh", "scripts/*.sh", "scripts/**/*.sh",
            "tests/e2e/*.py", "tests/e2e/**/*.py", "scripts/*.py", "scripts/**/*.py",
        ],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    seen: set[str] = set()
    paths: list[Path] = []
    for rel in out.splitlines():
        if not rel or rel in seen:
            continue
        seen.add(rel)
        paths.append(REPO_ROOT / rel)
    return paths


def _find_unguarded_sites(lines: list[str]) -> list[tuple[int, str]]:
    """Return (1-based line number, line text) for every line invoking
    `nx index` / `nx store` / `nx collection` / `store_put`. Comment lines
    are skipped -- a genuine violation is a command that RUNS, not prose
    mentioning one; a descriptive echo/label string that MENTIONS one of
    these verbs still counts (deliberately dumb -- see module docstring)."""
    hits: list[tuple[int, str]] = []
    for i, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if not _ANY_INVOCATION_RE.search(raw):
            continue
        hits.append((i, stripped))
    return hits


def _scan_repo() -> list[_Site]:
    sites: list[_Site] = []
    for script in _tracked_scripts():
        if not script.exists():
            # `git ls-files` reflects the INDEX, not the working tree -- a
            # file `rm`'d but not yet `git rm`'d (this repo's own worktree
            # policy: agents never stage) is still listed here. Skip it
            # rather than crash; the orchestrator's own `git add -u` at
            # commit time reconciles the index.
            continue
        lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
        rel = script.relative_to(REPO_ROOT).as_posix()
        for lineno, text in _find_unguarded_sites(lines):
            sites.append(_Site(file=rel, line=lineno, text=text))
    return sites


# ── Falsification controls (the detector must actually detect) ─────────────


def test_detector_flags_nx_index() -> None:
    lines = ['echo "provisioning"', "uv run nx index repo \"$run_dir\"", "echo done"]
    assert _find_unguarded_sites(lines) == [(2, 'uv run nx index repo "$run_dir"')]


def test_detector_flags_nx_store() -> None:
    lines = ["nx store put \"$docf\" --collection knowledge__x --title t"]
    assert _find_unguarded_sites(lines) == [
        (1, 'nx store put "$docf" --collection knowledge__x --title t'),
    ]


def test_detector_flags_nx_collection() -> None:
    lines = ["uv run nx collection delete \"$coll\" --yes"]
    assert _find_unguarded_sites(lines) == [(1, 'uv run nx collection delete "$coll" --yes')]


def test_detector_flags_store_put_literal() -> None:
    lines = ["claude_prompt \"Then store_put with project=x, title=y\""]
    assert _find_unguarded_sites(lines) == [
        (1, 'claude_prompt "Then store_put with project=x, title=y"'),
    ]


def test_detector_flags_the_wrapper_form() -> None:
    """The repo's `_nx()` HOME-swap wrapper does not change which tenant a
    write lands in -- it must be scanned identically to the bare form."""
    lines = ["_nx index repo \"$REPO_ROOT\""]
    assert _find_unguarded_sites(lines) == [(1, '_nx index repo "$REPO_ROOT"')]


def test_detector_ignores_comment_prose() -> None:
    lines = ["# e.g. 'nx index repo .' would populate the catalog"]
    assert _find_unguarded_sites(lines) == []


def test_scanner_is_nonvacuous() -> None:
    """The sweep must actually see the tracked tree, and must see the
    named lawful sites -- an empty result here means the enumeration or
    the regex broke, not that every site vanished."""
    scripts = _tracked_scripts()
    assert len(scripts) >= len(ALLOWLIST), (
        f"suspicious sweep: only {len(scripts)} scripts enumerated, but "
        f"ALLOWLIST names {len(ALLOWLIST)} files"
    )
    live_sites = _scan_repo()
    assert live_sites, (
        "scan found zero nx index/store/collection/store_put sites anywhere, "
        f"but ALLOWLIST names {len(ALLOWLIST)} files with matches -- the "
        "scanner broke (path drift, regex regression), it did not discover "
        "that every site died"
    )


# ── The pinned invariant ─────────────────────────────────────────────────


def test_every_host_harness_site_is_allowlisted() -> None:
    """nexus-8tnz2: every nx index/store/collection/store_put site under
    tests/e2e/** or scripts/** must be one of the named ALLOWLIST files.
    A new site outside it fails loudly, naming the file, the line, and
    the remedy -- it never reaches CI red silently; it reaches the shared
    production tenant."""
    live_sites = _scan_repo()

    offenders = [s for s in live_sites if s.file not in ALLOWLIST]
    assert not offenders, (
        "host-run harness site (nx index/store/collection, or store_put) not "
        "in the named ALLOWLIST:\n  "
        + "\n  ".join(f"{s.file}:{s.line}  {s.text!r}" for s in offenders)
        + "\n\nEvery site here needs a NAMED, reviewed reason it cannot land "
          "debris in the shared production tenant -- READ-ONLY (no write "
          "path), CONTAINER (runs inside a throwaway Docker container that "
          "provisions its own bundled PG/engine), or NX_LOCAL+SANDBOX "
          "(NX_LOCAL=1 or an isolated NEXUS_CONFIG_DIR/$HOME -- local mode "
          "has no cloud tenant concept at all). If none of those apply and "
          "this harness genuinely needs the operator's live service, it "
          "needs ONE of two conforming routes before it can be added here: "
          "(1) self-provision an engine and mint its own tenant via "
          "POST <engine>/v1/tenants/create under the boot bearer (the "
          "tests/_engine_substrate.py mint_test_tenant precedent), or "
          "(2) the throughput bench's MARKER+SNAPSHOT shape -- a "
          "marker-scoped owner name, a before/after `nx collection list` "
          "assertion, and EXIT-time teardown of every marker-scoped "
          "survivor (see tests/e2e/index-throughput-bench/run.sh). "
          "nexus-8tnz2."
    )


def test_ledger_matches_live_count() -> None:
    """Exact-ledger discipline (both directions): the live per-file count
    of allowlisted sites must equal ALLOWLIST's named count. A live count
    BELOW the ledger means a site was fixed/removed and the entry must be
    lowered or deleted -- a stale high entry is a free slot a future
    unguarded site could occupy unreviewed. A live count ABOVE the ledger
    is already caught by test_every_host_harness_site_is_allowlisted (a
    file already allowlisted gaining an EXTRA site still deserves
    review)."""
    live_sites = _scan_repo()
    live_counts: dict[str, int] = {}
    for s in live_sites:
        live_counts[s.file] = live_counts.get(s.file, 0) + 1

    ledger_counts = {f: n for f, (n, _reason) in ALLOWLIST.items()}
    mismatches = sorted(
        f"{f}: live={live_counts.get(f, 0)} ledger={ledger_counts.get(f, 0)}"
        for f in live_counts.keys() | ledger_counts.keys()
        if live_counts.get(f, 0) != ledger_counts.get(f, 0)
    )
    assert not mismatches, (
        "ALLOWLIST count drifted from the live scan (raise the entry for a "
        "genuine new site with review; lower or delete it if a site was "
        "fixed/removed): " + ", ".join(mismatches)
    )
