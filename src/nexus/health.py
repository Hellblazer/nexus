# SPDX-License-Identifier: AGPL-3.0-or-later
"""Health check data model and runner for nx doctor / nx console."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import structlog

from nexus.config import default_db_path

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader

_log = structlog.get_logger(__name__)

_CHECK = "✓"
_WARN = "✗"
# RDR-129 B4 (nexus-uq8a4): a third, soft state — the check could not complete
# but the condition is benign/transient (e.g. a healthy-but-busy database), so
# it renders distinctly from both a pass (✓) and a hard fail (✗) and never
# marks the run as failed.
_SOFT_WARN = "⚠"

# nexus-g7ijj fix round: the managed/local "not probeable from here" detail
# strings were duplicated verbatim across the three service-check skip
# sites (_check_storage_service_health, _check_migration_state,
# _check_rls_present) — extracted once so a future wording change can't
# drift between them.
_MANAGED_DEPLOYMENT_SKIP_DETAIL = (
    "managed deployment — this check runs server-side with the "
    "store operator's credentials (nexus-y3wuu); not probeable "
    "from this client; skipping"
)
_LOCAL_MODE_NOT_CONFIGURED_DETAIL = "service mode not configured (pg_credentials absent); skipping"


@dataclass
class HealthResult:
    """One health check result.

    ``ok`` / ``warn`` encode three states:

    * ``ok=True``                  → pass (✓)
    * ``ok=False, warn=True``      → soft warning (⚠) — benign/transient,
      never fatal, never marks the run failed (RDR-129 B4)
    * ``ok=False, warn=False``     → hard failure (✗)
    """

    label: str
    ok: bool
    detail: str = ""
    fix_suggestions: list[str] = field(default_factory=list)
    fatal: bool = False
    warn: bool = False


# ── Formatting ────────────────────────────────────────────────────────────────


def format_health_for_cli(
    results: list[HealthResult], *, local_mode: bool
) -> tuple[str, bool]:
    """Format health results for CLI output.

    Returns (formatted_output, any_fatal_failure).
    Output is byte-for-byte compatible with the prior inline doctor_cmd format.
    """
    lines: list[str] = ["Nexus health check:\n"]
    failed = False

    for r in results:
        if r.ok:
            status = _CHECK
        elif r.warn:
            status = _SOFT_WARN
        else:
            status = _WARN
        msg = f"  {status} {r.label}"
        if r.detail:
            msg += f": {r.detail}"
        lines.append(msg)

        if r.fix_suggestions:
            prefix = "Fix: " if not r.ok else "Suggest: "
            cont_indent = " " * (4 + len(prefix))
            for i, fix_line in enumerate(r.fix_suggestions):
                if i == 0:
                    lines.append(f"    {prefix}{fix_line}")
                else:
                    lines.append(f"{cont_indent}{fix_line}")

        if r.fatal and not r.ok:
            failed = True

    if failed:
        if local_mode:
            lines.append(
                "\nSome checks failed. Run 'nx doctor' again after fixing the issues above."
            )
        else:
            lines.append(
                "\nRun 'nx config init' to configure managed-service credentials, "
                "or 'nx init --service' to provision a local service stack."
            )

    return "\n".join(lines), failed


#: ``nx doctor`` exit codes. The glyph already tells a human "failure"; the
#: exit code must tell a script the same thing, or the instrument lies to
#: one of its two readers. Measured 2026-08-27 (nexus-be6x8): a sweep that
#: printed two genuine ✗ lines exited 0, because only 2 of 78 checks carried
#: ``fatal=True`` and both were green -- 97.4% of checks could not move rc.
EXIT_HEALTHY = 0      # every check ok, or ⚠ (soft/benign, RDR-129 B4)
EXIT_FAILURES = 1     # at least one hard ✗: something needs fixing
EXIT_FATAL = 2        # at least one fatal ✗: nexus cannot function


def health_exit_code(results: list[HealthResult]) -> int:
    """The exit code the main sweep reports for *results*.

    ``fatal`` keeps its distinct meaning (nothing will start) and its own
    code, so a caller can tell "fix something" from "nothing works".
    ``warn`` never moves the code (the RDR-129 B4 contract).
    """
    code = EXIT_HEALTHY
    for r in results:
        if r.ok:
            continue
        if r.fatal:
            return EXIT_FATAL
        if not r.warn:
            code = EXIT_FAILURES
    return code


def format_health_for_json(
    results: list[HealthResult], *, local_mode: bool
) -> str:
    """JSON rendering of the main doctor sweep (nexus-0vycz).

    One object with a ``checks`` array -- each entry carrying at least
    ``name``/``ok``/``status``/``detail`` -- plus summary counts, so a
    machine consumer (the shakedown playbook's S3 signal-density audit)
    can classify every check without scraping unicode glyphs out of the
    human-readable report. ``status`` mirrors the three-state model
    ``format_health_for_cli`` renders as glyphs: ``"ok"`` (✓), ``"warn"``
    (✗ soft/benign, RDR-129 B4), ``"fail"`` (✗ hard). ``fatal`` is carried
    through raw so a consumer can reproduce the same pass/fail exit-code
    semantics this module uses (only ``fatal and not ok`` fails the run --
    some hard-fail checks are non-fatal by design).
    """
    checks = []
    for r in results:
        if r.ok:
            status = "ok"
        elif r.warn:
            status = "warn"
        else:
            status = "fail"
        checks.append({
            "name": r.label,
            "ok": r.ok,
            "status": status,
            "detail": r.detail,
            "fatal": r.fatal,
            "fix_suggestions": list(r.fix_suggestions),
        })
    summary = {
        "total": len(checks),
        "ok": sum(1 for c in checks if c["status"] == "ok"),
        "warn": sum(1 for c in checks if c["status"] == "warn"),
        "fail": sum(1 for c in checks if c["status"] == "fail"),
    }
    payload = {
        "checks": checks,
        "summary": summary,
        "local_mode": local_mode,
    }
    return json.dumps(payload, indent=2)


# ── Individual checks ────────────────────────────────────────────────────────


def _python_ok() -> tuple[bool, str]:
    """Return (meets_requirement, version_string) for the running Python."""
    vi = sys.version_info
    ver = f"{vi.major}.{vi.minor}.{vi.micro}"
    return vi >= (3, 12), ver


def _check_python() -> list[HealthResult]:
    ok, ver = _python_ok()
    r = HealthResult(
        label="Python ≥ 3.12",
        ok=ok,
        detail=ver if ok else f"{ver} — 3.12+ required",
        fatal=True,
    )
    if not ok:
        r.fix_suggestions = [
            "brew install python@3.12                                 (macOS)",
            "apt install python3.12                                   (Ubuntu/Debian)",
            "winget install --id Python.Python.3.12 --scope user      (Windows)",
            "https://www.python.org/downloads/",
        ]
    return [r]


def _install_advice():
    """The advice module, deferred (health is imported early and widely)."""
    from nexus import install_advice  # noqa: PLC0415 — deferred import

    return install_advice


def _upgrade_advice(fallback: str) -> str:
    """How to upgrade THIS box. MOVED to :mod:`nexus.install_advice` (.13).

    The rule was born here in .11 and is needed by callers that cannot import
    ``health`` — ``stranded_install`` records at its own line 102 that it is a
    leaf with import constraints, and ``pdf_extractor`` and ``mcp_infra`` were
    each carrying their own hardcoded uv string. One answer, one module.

    This wrapper stays because it reads better at the call sites here. Note the
    generation branch now names ``nx self install`` rather than
    ``scripts/reinstall-tool.sh``: .11 predates .14, and the packaged installer
    needs no checkout (see the module docstring there for the full reason).
    """
    from nexus import install_advice  # noqa: PLC0415 — deferred import

    return install_advice.upgrade_advice(fallback)


def _check_generation_layout() -> list[HealthResult]:
    """The generation layout itself: is there a working install to run from?

    nexus-utpuw.11. health.py carried ZERO references to install_layout, so
    every way the layout can break was invisible to the one command whose job
    is noticing. Three things can go wrong and they are not the same severity:

    * ``current`` missing or dangling is a HARD failure. Every shim resolves it
      at spawn, so nothing starts. Not a warning.
    * A shim that uv has taken back is a FAILURE, and it is nexus-utpuw.7's
      accepted risk realised: between migration and reap uv still holds a valid
      receipt, so a stray ``uv tool upgrade conexus`` re-symlinks over the
      nexus-owned shim files and live sessions resolve through uv's tree again.
      The mitigation is that re-running the installer repairs it, which only
      helps if somebody is told.
    * A ``gen-*`` directory with no receipt is NOT a fault. It is a build that
      died before finishing; nothing ever pointed ``current`` at it and GC reaps
      it. Reporting wreckage as breakage trains the operator to ignore this row,
      which is how a real fault gets missed.

    The shim names are what the installed distribution DECLARES (plus the
    dependency scripts the writer also shims), asked of the generation's own
    interpreter -- the installer's rule, not a hardcoded inventory and not a
    listing of ``<current>/bin`` (GH #1487). A hardcoded inventory is the
    failure class this whole arc removes, and doctor is the last place to
    reintroduce one.
    """
    label = "Generation layout"
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred import

        tools = install_layout.tools_dir()
        bin_dir = install_layout.bin_dir()

        # WHAT MATTERS IS NOT PRESENT-VS-ABSENT, IT IS WHETHER ANYTHING WAS EVER
        # INSTALLED. Generations on disk with no pointer is a BROKEN install;
        # an empty tools root is a box that has not installed yet, and
        # hard-failing that would fail every fresh machine (RG-C).
        link = install_layout.current_link(tools=tools)
        if not link.is_symlink() and not link.exists():
            generations = install_layout.list_generations(tools=tools)
            if not generations:
                return [HealthResult(
                    label=label, ok=True,
                    detail="no generation install on this box (nothing to check)",
                )]
            return [HealthResult(
                label=label, ok=False, fatal=True,
                detail=(
                    f"{len(generations)} generation(s) exist but current does "
                    "not — nothing resolves, so nothing will start"
                ),
                fix_suggestions=["scripts/reinstall-tool.sh"],
            )]

        current = install_layout.current_generation()
    except Exception as exc:  # noqa: BLE001 — never crash doctor
        # UNCERTAIN MEANS SAY SO. Returning ok here would be the silent green
        # this bead exists to remove, relocated one function over.
        _log.warning("doctor_generation_layout_check_failed", error=str(exc))
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"could not check the generation layout — {exc}",
        )]

    if not current.is_dir():
        return [HealthResult(
            label=label, ok=False, fatal=True,
            detail=(
                f"current points at {current}, which does not exist — every "
                "shim resolves current at spawn, so nothing will start"
            ),
            fix_suggestions=["scripts/reinstall-tool.sh"],
        )]

    if not install_layout.receipt_path(current).is_file():
        return [HealthResult(
            label=label, ok=False, fatal=True,
            detail=(
                f"current points at {current.name}, which has no receipt — that "
                "is an unfinished build, not a generation"
            ),
            fix_suggestions=["scripts/reinstall-tool.sh"],
        )]

    # THE OWNED SET IS WHAT THE DISTRIBUTION DECLARES, not a listing of
    # <current>/bin. ~/.local/bin is a SHARED directory: pyenv, asdf and
    # homebrew leave a `python` symlink there (RG-C), and uv leaves versioned
    # interpreter links (`python3.12`, from `uv python install`) whose name the
    # generation venv's own bin/ also carries. Reading the listing reported
    # those as uv reclaiming a shim and offered `nx self install` as the fix,
    # which would have rewritten the user's default python3.12 into a nexus
    # shim (GH #1487, nexus-50hm9). A row that cries wolf is this check's own
    # docstring warning inverted; a fix that hijacks python3 is worse.
    try:
        owned = install_layout.owned_shim_names(current)
    except install_layout.InstallLayoutError as exc:
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=(
                f"could not ask {current.name} which console scripts it declares "
                f"— {exc}; the shim-ownership check cannot run"
            ),
        )]
    reclaimed = [name for name in sorted(owned) if (bin_dir / name).is_symlink()]
    if reclaimed:
        return [HealthResult(
            label=label, ok=False, fatal=True,
            detail=(
                f"{', '.join(reclaimed)} in {bin_dir} are symlinks, not "
                "nexus-owned shims — uv has taken them back (a stray "
                "`uv tool upgrade conexus` does this), so those commands "
                "resolve through uv's tree instead of current"
            ),
            fix_suggestions=[
                "nx self install    # rewrites the shims to current and registers uv's tree for reap (nx upgrade --auto does the same)",
            ],
        )]

    generations = install_layout.list_generations(tools=tools)
    results = [HealthResult(
        label=label, ok=True,
        detail=(
            f"current -> {current.name}, {len(generations)} complete "
            f"generation(s), shims owned by nexus"
        ),
    )]
    results.extend(_check_shims_match_template(current, bin_dir, tools, owned))
    results.extend(_check_base_interpreters(current, generations))
    results.extend(_check_orphan_uv_install())
    results.extend(_check_generation_holders(current, generations, tools=tools))
    return results


def _check_shims_match_template(current, bin_dir, tools, owned) -> list[HealthResult]:
    """A shim must match the template, not merely be a regular file.

    The not-a-symlink check catches uv reclaiming a name. It does NOT catch a
    shim with the right SHAPE and the wrong CONTENT: one baked before
    NX_TOOLS_DIR moved, or hand-edited, resolves a pointer that is not this
    layout's and sends every spawn somewhere else. The template is rendered by
    install_layout.render_shim, which is the same function the writer uses, so
    this compares against the source of truth rather than a restatement of it.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import
    mismatched = []
    checked = 0
    for name in sorted(owned):  # the caller asked the generation once; no second spawn
        shim = bin_dir / name
        if not shim.is_file() or shim.is_symlink():
            continue  # absent or reclaimed — the row above owns those
        try:
            expected = install_layout.render_shim(name, tools=tools)
        except Exception:  # noqa: BLE001 — a name the template refuses is not ours to judge here
            continue
        checked += 1
        if shim.read_text() != expected:
            mismatched.append(name)
    if not mismatched:
        # Emit the PASS too. Every other row in this group does, and a check
        # that is silent when healthy cannot be distinguished from one that did
        # not run -- which is the exact confusion this bead exists to remove.
        return [HealthResult(
            label="Shim contents", ok=True,
            detail=f"{checked} shim(s) match the current template",
        )]
    return [HealthResult(
        label="Shim contents", ok=False, fatal=True,
        detail=(
            f"{', '.join(mismatched)} in {bin_dir} do not match the current "
            "shim template — they resolve a different pointer than this layout, "
            "so spawns go somewhere else"
        ),
        fix_suggestions=["nx self install    # rewrites the shims from the current template"],
    )]


def _check_base_interpreters(current, generations) -> list[HealthResult]:
    """Every retained generation's base interpreter still exists.

    A generation's venv does NOT contain its interpreter: pyvenv.cfg records a
    ``home =`` pointing at one uv manages elsewhere, and uv prunes those. When
    it prunes the one a generation points at, that tree stops working and
    nothing here can prevent it — the research amendment calls this "the one
    failure we can only detect, never prevent", which is the whole reason to
    detect it.

    On CURRENT it is fatal: nothing will start. On an older generation it is a
    warning — that tree is a rollback target, not the running install, and
    failing a working box over a dead rollback target is how this row gets
    ignored.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    missing_current = None
    missing_old: list[str] = []
    for gen in generations:
        try:
            base = Path(install_layout.read_receipt(gen).base_interpreter)
        except Exception:  # noqa: BLE001 — an unreadable receipt is the layout row's business
            continue
        if base.exists():
            continue
        if gen == current:
            missing_current = base
        else:
            missing_old.append(gen.name)

    if missing_current is not None:
        return [HealthResult(
            label="Base interpreter", ok=False, fatal=True,
            detail=(
                f"current ({current.name}) records base interpreter "
                f"{missing_current}, which no longer exists — uv prunes managed "
                "interpreters, and this generation cannot run without it"
            ),
            fix_suggestions=["scripts/reinstall-tool.sh"],
        )]
    if missing_old:
        return [HealthResult(
            label="Base interpreter", ok=False, warn=True,
            detail=(
                f"{', '.join(missing_old)} record base interpreters that no "
                "longer exist — those generations are no longer usable as "
                "rollback targets (uv pruned the interpreter)"
            ),
        )]
    return [HealthResult(
        label="Base interpreter", ok=True,
        detail="every retained generation's base interpreter is present",
    )]


def _check_orphan_uv_install() -> list[HealthResult]:
    """A uv-managed conexus alive alongside the generation layout.

    Expected DURING the migration window — nexus-utpuw.7 leaves the legacy tree
    in place until it has zero holders. What makes it worth naming is that uv
    still holds a valid receipt for it, so a stray ``uv tool upgrade conexus``
    rebuilds that tree and re-symlinks over the shims (.7's accepted risk).
    """
    # nexus-orhp5: one resolver for "where is uv's tool dir". This site
    # honoured UV_TOOL_DIR but not XDG_DATA_HOME, so it reported "no uv
    # install" on an XDG-relocated box while one sat right there.
    from nexus.install_layout import uv_conexus_venv  # noqa: PLC0415 — deferred, avoids an import cycle

    legacy = uv_conexus_venv()
    if not (legacy / "bin").is_dir():
        return [HealthResult(
            label="Orphan uv install", ok=True,
            detail="no uv-managed conexus alongside the generation layout",
        )]
    # REGISTERED OR NOT is the fact that decides whether this box converges.
    # A registered tree is in gc.sh's ledger and is reaped by the next
    # `nx self install` once nothing runs from it. An unregistered one is
    # never reaped by anything -- the state every checkout-driven box sat in
    # until nexus-hibpr -- and the row used to render both identically.
    from nexus.install_layout import legacy_generation_link  # noqa: PLC0415 — deferred import

    link = legacy_generation_link()
    registered = link.is_symlink() and link.resolve() == legacy.resolve()
    if registered:
        return [HealthResult(
            label="Orphan uv install", ok=False, warn=True,
            detail=(
                f"a uv-managed conexus at {legacy} is registered for reap and "
                "goes away at the next `nx self install` once nothing runs "
                "from it — until then uv still holds a valid receipt for it, "
                "so `uv tool upgrade conexus` would rebuild it and re-symlink "
                "over the nexus shims"
            ),
            fix_suggestions=[
                "nx self install    # reaps it once its holders are gone",
            ],
        )]
    return [HealthResult(
        label="Orphan uv install", ok=False, warn=True,
        detail=(
            f"a uv-managed conexus at {legacy} is NOT in the generation ledger — "
            "nothing will ever reap it, and `uv tool upgrade conexus` would "
            "rebuild it and re-symlink over the nexus shims"
        ),
        fix_suggestions=[
            "nx self install    # registers it for reap, then reaps once its holders are gone",
            "scripts/reinstall-tool.sh    # same, from a checkout",
        ],
    )]


def _check_generation_holders(
    current, generations, *, tools: Path | None = None,
) -> list[HealthResult]:
    """Who is still running from an older generation. INFORMATIONAL.

    Holders are a fact, not a fault: they keep running from their own tree and
    converge at their next spawn. A row that failed on them would contradict
    the acceptance criterion this whole arc exists to satisfy, which is that a
    live session stops being an obstacle to installing.
    """
    from nexus import install_census  # noqa: PLC0415 — deferred import

    snapshot = install_census.ps_snapshot()
    held = []
    for gen in generations:
        if gen == current:
            continue
        try:
            pids = install_census.generation_holder_pids(gen, snapshot=snapshot)
        except Exception:  # noqa: BLE001 — a census failure is not a layout fault
            continue
        if pids:
            held.append(f"{gen.name}: {len(pids)} ({', '.join(str(p) for p in pids[:4])})")
    # The legacy uv tree is an "older generation" too -- the oldest one there
    # is -- and it is receipt-less, so it is never in *generations*. Ask the
    # census for it by structure (nexus-k52g0: 9 processes on the 7.19.0 uv
    # tree rendered as "nothing is still bound to an older generation").
    for legacy in install_census.legacy_tree_candidates(tools=tools):
        try:
            pids = install_census.generation_holder_pids(legacy, snapshot=snapshot)
        except Exception:  # noqa: BLE001 — a census failure is not a layout fault
            continue
        if pids:
            held.append(
                f"legacy uv tree {legacy}: {len(pids)} "
                f"({', '.join(str(p) for p in pids[:4])})"
            )
    if not held:
        return [HealthResult(
            label="Holders", ok=True,
            detail="nothing is still bound to an older generation",
        )]
    return [HealthResult(
        label="Holders", ok=True,
        detail=(
            "still bound to an older generation, converging at their next "
            f"spawn — {'; '.join(held)}"
        ),
    )]


def _check_process_skew() -> list[HealthResult]:
    """nexus-4xgfy: the disk can be upgraded while every running process
    still executes the old code from memory — three live incidents
    (6.7.0/6.7.1 upgrades) where doctor said 'latest' and the whole
    machine was stale. Enumerate running conexus processes, compare their
    start times against the installed distribution's mtime, and WARN with
    the per-process remedy. Also names the install's uv-receipt source so
    'uv tool upgrade did nothing' is self-explanatory.
    """
    try:
        from nexus.upgrade_finish import (  # noqa: PLC0415 — deferred import
            detect_stale_processes,
            enumerate_processes,
            install_source,
        )

        report = detect_stale_processes()
    except Exception as exc:  # noqa: BLE001 — must not crash `nx doctor`; degraded to WARN, never silent-ok
        # nexus-bawvu: a bare `return []` here made the row VANISH on probe
        # failure — indistinguishable from "no stale processes found", which
        # is exactly the state the oyo2g stall diagnosis depends on this row
        # NOT being in. Same no-silent-fallback posture as the stranded-
        # install / legacy-catalog checks below: report "could not check"
        # loudly instead of disappearing.
        _log.warning("doctor_process_freshness_check_failed", error=str(exc))
        return [HealthResult(
            label="Process freshness",
            ok=False,
            warn=True,
            detail=f"could not check — probe failed: {exc}",
        )]
    if not report.stale:
        # A GREEN MUST STATE WHAT IT EXAMINED. This row used to say "all running
        # conexus processes match the installed version" whether it had looked
        # at twelve processes or none, and under the generation layout it was
        # always none: the markers were pinned to the CURRENT generation while a
        # stale process by definition runs from a different one, so report.stale
        # was empty by construction (nexus-utpuw.10). The sentence was a vacuous
        # truth dressed as a positive finding, on the one check whose whole
        # purpose is noticing that the machine is stale.
        #
        # Two ps views rather than one: the count is display-only and never
        # gates a decision, so a process starting between them can make the
        # count off by one but cannot make this row wrong.
        try:
            examined = len(enumerate_processes())
        except Exception:  # noqa: BLE001 — a display count must never fail the row
            examined = -1

        if examined == 0:
            detail = (
                "no running conexus processes found — nothing to compare "
                f"against the installed {report.installed_version}"
            )
        elif examined < 0:
            detail = (
                f"no stale processes found against the installed "
                f"{report.installed_version}; could not count how many were examined"
            )
        else:
            detail = (
                f"{examined} running conexus process(es) all match the installed "
                f"{report.installed_version} (install source: "
                f"{install_source().split(' — ')[0]})"
            )
        return [HealthResult(label="Process freshness", ok=True, detail=detail)]
    names = ", ".join(
        f"{p.kind} pid {p.pid}" for p in report.stale[:6]
    )
    return [HealthResult(
        label="Process freshness",
        ok=False,
        warn=True,
        detail=(
            f"{len(report.stale)} process(es) predate the installed "
            f"{report.installed_version} and are running OLD code: {names}. "
            "Run `nx daemon restart-stale` (restarts what is safe; names "
            "the Claude sessions only you can close)."
        ),
    )]


def _check_cli_version() -> list[HealthResult]:
    """Check whether a newer conexus version is available on PyPI."""
    try:
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

        current = _pkg_version("conexus")
    except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return []  # silent — installed version unknown

    # Check PyPI for latest (3-second timeout, network-tolerant)
    import json  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
    import urllib.error  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
    import urllib.request  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/conexus/json",
            headers={"User-Agent": f"nx-doctor/{current}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = data["info"]["version"]
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, TimeoutError):
        return [HealthResult(
            label="conexus version",
            ok=True,
            detail=f"{current} (PyPI check skipped — offline?)",
        )]

    # Compare via tuple parsing
    def _parse(v: str) -> tuple[int, ...]:
        try:
            parts = tuple(int(x) for x in v.split(".")[:3])
            return parts + (0,) * (3 - len(parts))
        except ValueError:
            return (0, 0, 0)

    cur_t = _parse(current)
    latest_t = _parse(latest)

    if cur_t >= latest_t:
        return [HealthResult(
            label="conexus version",
            ok=True,
            detail=f"{current} (latest)",
        )]

    r = HealthResult(
        label="conexus version",
        ok=True,  # not fatal — just informational
        detail=f"{current} → {latest} available",
    )
    r.fix_suggestions = [
        _install_advice().upgrade_advice(
            "uv tool upgrade conexus", note=f"→ {latest}"
        ),
    ]
    return [r]


def local_embedder_advisory(
    choice: str | None, active_model: str
) -> HealthResult | None:
    """Surface the two user-invisible local-embedder states (RDR-144 P5a).

    The active embedder is resolved silently by ``_resolve_local_model``; the
    user never sees which model actually ran. ``nx doctor`` renders the two
    divergences that matter:

    * **State 1 — default 384**: no ``nx init`` choice recorded and the
      bundled 384-dim minilm is active. An advisory nudge toward ``nx init``
      for the materially better bge-768.
    * **State 2 — degraded bge**: the user chose bge-768 via ``nx init`` but
      the ``[local]`` extra is missing, so the resolver silently fell back to
      384. This is a no-silent-fallback-for-correctness violation; flag it as
      actionable, not a structlog line only.

    ``choice`` is :func:`nexus.config.local_embed_model_choice` (the persisted
    ``local.embed_model`` or ``None``); ``active_model`` is the resolved
    ``LocalEmbeddingFunction.model_name``. Returns a soft-warning
    ``HealthResult`` (never fatal — search still works, just sub-optimally) or
    ``None`` when the active model already matches the user's intent.
    """
    from nexus.db.local_ef import _TIER0_MODEL, _TIER1_MODEL  # noqa: PLC0415 — deferred to avoid circular import

    if choice == _TIER1_MODEL and active_model == _TIER0_MODEL:
        # State 2: chose bge, but the extra is missing -> silent 384 fallback.
        return HealthResult(
            label="Local embedder",
            ok=False,
            warn=True,
            detail=(
                "you selected bge-768 (nx init) but the [local] extra is not "
                "installed — search is silently running at 384-dim "
                "(all-MiniLM-L6-v2), materially worse than your choice"
            ),
            # nexus-hbgso: `nx init` cannot install an extra (the picker it
            # once hosted was removed at RDR-174 P1.3) and bare `pip install`
            # has no meaningful target under the generation layout — both
            # named dead ends on the one check whose job is getting the user
            # off a 384-dim embedder. Route through install_advice so the
            # generation-vs-legacy-uv distinction lives in one place.
            fix_suggestions=_install_advice().local_extra_advice(),
        )

    if choice is None and active_model == _TIER0_MODEL:
        # State 1: default 384, never chose -> advisory upgrade nudge.
        return HealthResult(
            label="Local embedder",
            ok=False,
            warn=True,
            detail=(
                "running with the default 384-dim embedder (all-MiniLM-L6-v2)"
            ),
            fix_suggestions=[
                "Run `nx init` to upgrade to bge-768 for materially better "
                "local search quality",
            ],
        )

    return None


def _check_t3_local() -> list[HealthResult]:
    results: list[HealthResult] = []
    results.append(HealthResult(label="T3 mode", ok=True, detail="local (no API keys needed)"))
    # RDR-155 P4a.2 (nexus-1k8s1): the nexus-service serves T3 in local mode
    # too — probe it unconditionally (critique finding 2: a pgvector-only
    # install with the service down must not doctor all-green).
    results.append(_check_vector_service())

    # Service mode (pg_credentials present) reshapes the Python local-embedder
    # surface below (nexus-ybw87): a --service install embeds T3 server-side in
    # the Java service (bge-768, reported authoritatively by
    # _check_service_bge_model). The Python LocalEmbeddingFunction here only
    # serves T1/local-Python paths, NOT T3 — so we qualify its label and suppress
    # the T3-framed upgrade advisory, which would otherwise contradict the
    # service-embedder result on the very next line.
    from nexus.config import local_embed_model_choice, nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred to avoid circular import

    _service_mode = (nexus_config_dir() / CREDENTIALS_FILENAME).exists()

    # Embedding model
    from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415 — deferred to avoid circular import
    ef = LocalEmbeddingFunction()
    if _service_mode:
        results.append(HealthResult(
            label="Embedding model (local Python / T1)", ok=True,
            detail=f"{ef.model_name} ({ef.dimensions}d) — T3 embeds server-side "
                   f"via the bge-768 service",
        ))
    else:
        results.append(HealthResult(
            label="Embedding model", ok=True,
            detail=f"{ef.model_name} ({ef.dimensions}d)",
        ))

    # RDR-144 P5a: config-aware upgrade / degradation advisory. Replaces the
    # old unconditional minilm nudge (which pestered users who explicitly
    # chose 384 and never caught the chose-bge-but-extra-missing degrade).
    # Suppressed in service mode (see above): the advisory is about the Python
    # local embedder, which does not serve a service user's T3.
    if not _service_mode:
        advisory = local_embedder_advisory(local_embed_model_choice(), ef.model_name)
        if advisory is not None:
            results.append(advisory)

    # Collection count.
    #
    # RDR-155 P4a.2 (nexus-1k8s1): the T3-daemon probe is retired with the
    # Chroma serving path — T3 serving routes through the pgvector-backed
    # nexus-service, so the collection census queries it via ``make_t3()``.
    # (P4b: the legacy Chroma disk-usage report died with the migration
    # machinery; Chroma-era salvage goes through the LAST_MIGRATION_CAPABLE
    # release.)
    #
    # The GH-1061 E1 dimension-mismatch probe retired with the serving path
    # too: it dummy-queried raw Chroma collections to catch stored-vs-active
    # embedder drift, but on the pgvector path embedding is server-side and
    # the collection-name model segment dispatches the dimension fail-loud
    # at write time (PgVectorRepository.dimForCollection) — the hazard class
    # the probe existed for cannot occur silently anymore.
    try:
        from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid circular import

        # Graceful-degrade contract (RDR-156 P3): list_collections() swallows
        # transport errors and returns [] — a down service reads as "0
        # collections" here, NOT as a failure. That is intentional: the fatal
        # vector-service reachability probe (_check_vector_service) fires
        # separately and is the failure surface; this check is informational.
        cols = make_t3().list_collections()
        col_count = len(cols)
        results.append(HealthResult(
            label="T3 collections", ok=True,
            detail=f"{col_count} collections (pgvector service)",
        ))
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        _log.debug("doctor_t3_collections_failed", error=str(exc))
        results.append(HealthResult(label="T3 collections", ok=True, detail="could not query"))

    return results


def _check_service_bge_model() -> list[HealthResult]:
    """RDR-160 (nexus-gzqvg): surface a missing/incomplete bge-768 service model.

    In local mode the Java service embeds every collection with bge-768 and reads
    the STANDARD fp32 ONNX from a fixed path; without it the service fail-loud-
    crashes at boot (the {@code Bge768Embedder} preflight), which is opaque if you
    have not seen it before. ``nx doctor`` surfaces the gap earlier.

    Gated on SERVICE mode (``pg_credentials`` present) because only the Java
    service reads this file: a pure-Python local install uses the fastembed cache,
    and cloud mode embeds server-side via Voyage. Called from the local-mode
    branch of :func:`run_health_checks`, so cloud mode never reaches it. Returns
    ``[]`` (no output) when this is not a service install.

    ``service_bge_model_present()`` applies the same size floors as provisioning,
    so a truncated download or a quantized/fused substitute reads as "incomplete"
    and is flagged, not silently accepted.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred to avoid circular import

    if not (nexus_config_dir() / CREDENTIALS_FILENAME).exists():
        return []  # not a service install — the Java service is what reads this model

    from nexus.db.service_bge_model import (  # noqa: PLC0415 — deferred to avoid circular import
        service_bge_model_dir,
        service_bge_model_present,
    )

    model_dir = service_bge_model_dir()
    if service_bge_model_present():
        return [HealthResult(
            label="Service embedder (bge-768)",
            ok=True,
            detail=f"standard ONNX present at {model_dir}",
        )]
    return [HealthResult(
        label="Service embedder (bge-768)",
        ok=False,
        # SOFT warn, not fatal: this is the "surface it earlier" advisory. The
        # HARD gate is the Bge768Embedder boot preflight. A fatal here would
        # (a) red-X doctor for a mid-setup user who has pg_credentials but has
        # not provisioned/started the service yet, and (b) stack a third fatal
        # on top of _check_vector_service / _check_storage_service_health when the
        # service is simply down — noise, not signal.
        warn=True,
        detail=(
            f"the local Java service embeds with bge-768 but its ONNX is missing "
            f"or incomplete at {model_dir} — the service will not boot until it is "
            f"provisioned"
        ),
        fix_suggestions=[
            "Provision it: nx init --service",
            "Or stage the STANDARD fp32 export (Xenova/bge-base-en-v1.5 model.onnx "
            "+ tokenizer.json — NOT fastembed's model_optimized.onnx) at that path.",
        ],
    )]


def _check_service_crossencoder_model() -> list[HealthResult]:
    """RDR-188 P1.3: surface a missing/incomplete ms-marco cross-encoder model.

    In local service mode the Java engine reranks with the ms-marco-MiniLM
    cross-encoder read from a fixed path. Unlike the bge model (boot-fatal),
    a missing cross-encoder only degrades the fused rerank stage — LOUD, per
    request (``rerank_degraded=true``) — so this is a soft warn with the
    provisioning remedy, mirroring :func:`_check_service_bge_model`'s gating:
    only a service install (``pg_credentials`` present) reads this file.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred to avoid circular import

    if not (nexus_config_dir() / CREDENTIALS_FILENAME).exists():
        return []  # not a service install — the Java engine is what reads this model

    from nexus.db.service_crossencoder_model import (  # noqa: PLC0415 — deferred to avoid circular import
        service_crossencoder_model_dir,
        service_crossencoder_model_present,
    )

    model_dir = service_crossencoder_model_dir()
    if service_crossencoder_model_present():
        return [HealthResult(
            label="Service reranker (ms-marco cross-encoder)",
            ok=True,
            detail=f"ONNX present at {model_dir}",
        )]
    return [HealthResult(
        label="Service reranker (ms-marco cross-encoder)",
        ok=False,
        warn=True,
        detail=(
            f"the local engine reranks with the ms-marco cross-encoder but its ONNX "
            f"is missing or incomplete at {model_dir} — server-side rerank degrades "
            f"loud (rerank_degraded=true) until it is provisioned"
        ),
        fix_suggestions=[
            "Provision it: nx init",
        ],
    )]


#: Bounded tail size read by :func:`_last_boot_failure_detail` (nexus-4m6i0.7).
#: The service can crash-loop BEFORE it answers any HTTP request, so the
#: only evidence of *why* is in its own log file — never the whole file,
#: just the most recent bytes, to keep this diagnostic O(1)-ish and never a
#: meaningful drag on `nx doctor`.
_BOOT_FAILURE_TAIL_BYTES: int = 64 * 1024

#: Liquibase's failure marker, verbatim across both the wrapped GH #1390
#: report and the raw stack trace: "Migration failed for changeset
#: <changelog-path>::<changeset-id>::<author>".
_LIQUIBASE_CHANGESET_RE = re.compile(
    r"Migration failed for changeset\s+(?P<path>\S+?)::(?P<id>[^:\s]+)::(?P<author>\S+)"
)
#: The SQL error one-liner Liquibase's PSQLException wrapper emits, usually
#: a few lines after the changeset marker (e.g. "Caused by: ...PSQLException:
#: \n  ERROR: constraint ... does not exist").
_ERROR_LINE_RE = re.compile(r"^[ \t]*(ERROR:.*)$", re.MULTILINE)
#: Cap on the surfaced error one-liner so a doctor line never becomes an
#: unbounded stack-trace dump.
_ERROR_LINE_MAX_CHARS: int = 200
#: How far past the changeset marker the ERROR-line association may reach.
#: The real Liquibase trace (GH #1390 verbatim) places the PSQLException's
#: ERROR line within ~300 chars of the marker; a match beyond this window
#: is presumed to be an unrelated later error and is NOT attributed to the
#: changeset (the id-only form is returned instead).
_ERROR_SEARCH_WINDOW_CHARS: int = 1000


def _last_boot_failure_detail(log_path: Path) -> str | None:
    """Best-effort tail-parse for the most recent Liquibase boot failure.

    RDR (nexus-4m6i0.7): during a Liquibase-VALIDATE crash-loop (GH #1390 /
    ms57z) the service dies before it can answer any HTTP request, so the
    root cause has to come from its own log file, not a live probe. Reads at
    most the last :data:`_BOOT_FAILURE_TAIL_BYTES` of *log_path* and looks
    for the LAST ``Migration failed for changeset <path>::<id>::<author>``
    marker plus, if present nearby, the SQL error one-liner that follows it.

    Returns ``None`` on ANY failure — missing file, not a regular file,
    unreadable, no marker found — this is diagnostic sugar layered on top of
    the hard "unreachable" signal, never load-bearing, and must never raise.
    """
    try:
        if not log_path.is_file():
            return None
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            if size > _BOOT_FAILURE_TAIL_BYTES:
                f.seek(size - _BOOT_FAILURE_TAIL_BYTES)
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    matches = list(_LIQUIBASE_CHANGESET_RE.finditer(tail))
    if not matches:
        return None
    last = matches[-1]
    changeset_id = last.group("id")

    # Best-effort: scan forward from the marker for the nearest ERROR: line
    # (Liquibase wraps the underlying PSQLException a few lines below).
    # BOUNDED window (nexus-4m6i0.7 critique): an unbounded forward search
    # could glue a DISTANT, UNRELATED error (e.g. a later "disk quota
    # exceeded") onto this changeset marker — fabricating a causal pairing
    # that actively misdirects the operator, strictly worse than showing
    # the changeset id alone. The real Liquibase trace puts the
    # PSQLException within a few lines of the marker; anything farther
    # away is presumed unrelated and we degrade to the id-only form.
    remainder = tail[last.end() : last.end() + _ERROR_SEARCH_WINDOW_CHARS]
    error_match = _ERROR_LINE_RE.search(remainder)
    if error_match:
        error_line = error_match.group(1).strip()[:_ERROR_LINE_MAX_CHARS]
        return f"Liquibase changeset {changeset_id}: {error_line}"
    return f"Liquibase changeset {changeset_id}"


def _boot_failure_advisory() -> str | None:
    """Soft wrapper: resolve the local service log path and tail-parse it.

    Guards cloud-mode / no-local-service installs (no log path exists) and
    any resolution failure — degrades to ``None`` silently, never raises.
    """
    try:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import

        log_path = nexus_config_dir() / "logs" / "storage_service_native.log"
        detail = _last_boot_failure_detail(log_path)
    except Exception:  # noqa: BLE001 — best-effort: must never crash the reachability probe
        return None
    if detail is None:
        return None
    return f"last recorded boot failure: {detail}"


def _check_vector_service() -> HealthResult:
    """Reachability probe for the pgvector-backed vector serving surface.

    RDR-155 P4a.2 (nexus-1k8s1): post-cutover the nexus-service IS the T3
    serving path in BOTH modes, so this probe runs unconditionally — it must
    not be gated on legacy ChromaCloud credential presence (a pgvector-only
    install with the service down would otherwise doctor all-green;
    P4a.2 critique finding 2).
    """
    try:
        # Raw GET so failures surface (HttpVectorClient.list_collections
        # deliberately swallows errors for its callers).
        from nexus.db.http_vector_client import _get  # noqa: PLC0415 — deferred to avoid circular import
        _get("/v1/vectors/collections")
        return HealthResult(
            label="Vector service (/v1/vectors)", ok=True, detail="reachable",
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        # nexus-srt1m: discriminate on the HTTP status before calling anything
        # "not reachable". ``VectorServiceError.code`` carries the status for an
        # HTTP-error response and ``None`` for a transport failure — a service
        # that answers 401 is RUNNING, so reporting unreachability (and
        # prescribing "start the service") points at the wrong subsystem. The
        # 2026-07-25 incident: a rotated bearer token printed
        # "not reachable / Fix: Start the nexus-service" three lines above a
        # green "✓ Managed/remote service — release_version 0.1.55", and in
        # cloud mode there is no local service to start in the first place.
        code = getattr(exc, "code", None)
        if code in (401, 403):
            _log.debug("vector_service_auth_failed", status=code, error=str(exc))
            return HealthResult(
                label="Vector service (/v1/vectors)",
                ok=False,
                detail=f"authentication failed (HTTP {code}) — the service is "
                "reachable but rejected the token",
                fix_suggestions=[
                    "Refresh NX_SERVICE_TOKEN (a rotated/revoked token 401s "
                    "while an unauthenticated probe like /version still 200s).",
                    "Then restart any long-lived process holding the old token "
                    "— MCP servers and editor sessions capture env at spawn, so "
                    "they keep failing after a rotation while a freshly sourced "
                    "shell succeeds, which misreads as intermittent.",
                ],
                fatal=True,
            )
        if code is not None:
            # The service answered, just not successfully. Surface the status
            # instead of laundering it into a reachability claim.
            _log.debug("vector_service_http_error", status=code, error=str(exc))
            return HealthResult(
                label="Vector service (/v1/vectors)",
                ok=False,
                detail=f"service returned HTTP {code}",
                fix_suggestions=[
                    "Check the service logs for the failing request — the "
                    "endpoint is reachable, so this is not a startup problem.",
                ],
                fatal=True,
            )
        _log.debug("vector_service_not_reachable", error=str(exc))
        # nexus-4m6i0.7: the service can crash-loop before answering any
        # request (a Liquibase VALIDATE failure on boot, GH #1390) — surface
        # the root cause from the local service log when one is available,
        # instead of leaving the operator to spelunk storage_service_native.log
        # by hand. Strictly best-effort/soft: any failure here degrades
        # silently back to the bare "not reachable" message. Only reached for
        # transport failures now, so a boot advisory can never be scraped from
        # a stale log while the service is actually up and answering.
        detail = "not reachable"
        boot_advisory = _boot_failure_advisory()
        if boot_advisory:
            detail = f"not reachable — {boot_advisory}"
        return HealthResult(
            label="Vector service (/v1/vectors)",
            ok=False,
            detail=detail,
            fix_suggestions=[
                "Start the nexus-service (pgvector backend) and export "
                "NX_SERVICE_URL / NX_SERVICE_TOKEN.",
            ],
            fatal=True,
        )


def _check_managed_service_probe() -> list[HealthResult]:
    """RDR-001 (nexus-o6fch): version-compatibility probe of a MANAGED endpoint.

    Runs ONLY when ``NX_SERVICE_URL`` is explicitly set — the unambiguous "I have
    pointed the client at a specific managed endpoint" signal. It deliberately
    NEVER defaults to ``https://api.conexus-nexus.com``: a local-service-in-cloud-
    mode user (``NX_SERVICE_URL`` unset, endpoint lease-discovered on localhost)
    must not be probed against the public managed endpoint.

    Complements :func:`_check_vector_service` (which probes
    ``/v1/vectors/collections`` for reachability + auth): this adds the
    unauthenticated ``/version`` handshake → release_version COMPATIBILITY, which
    reachability alone misses (a reachable-but-incompatible managed service). SOFT
    warn only — reachability fatals are ``_check_vector_service``'s domain, so this
    surfaces the version/remedy signal without a duplicate fatal on a down service.
    """
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    # env (NX_SERVICE_URL) FIRST, then config.yml — so a greenfield user who set
    # the endpoint with `nx config set service_url` (no shell export) still gets
    # the probe (RDR-166 nexus-v3p0x). Empty in BOTH → no explicit managed
    # endpoint, never default-probe the public one.
    base = (get_credential("service_url") or "").strip()
    if not base:
        return []

    from nexus.db.managed_endpoint import (  # noqa: PLC0415 — deferred to avoid circular import
        ManagedServiceError,
        ManagedServiceIncompatible,
        probe_managed_service,
    )

    try:
        caps = probe_managed_service(base_url=base)
    except ManagedServiceIncompatible as exc:
        return [HealthResult(
            label="Managed/remote service (version)",
            ok=False,
            warn=True,
            detail=str(exc),
            fix_suggestions=[
                "Align the managed-service and nx-client versions, or correct "
                "NX_SERVICE_URL.",
            ],
        )]
    except ManagedServiceError as exc:
        # Unreachable — _check_vector_service owns the fatal reachability signal;
        # stay soft here to avoid a double-report on a down endpoint.
        return [HealthResult(
            label="Managed/remote service (version)",
            ok=False,
            warn=True,
            detail=str(exc),
            fix_suggestions=["Confirm NX_SERVICE_URL is reachable (see the vector-service check)."],
        )]
    return [HealthResult(
        label="Managed/remote service (version)",
        ok=True,
        detail=f"{caps.base_url} — release_version {caps.release_version} (app_version {caps.app_version})",
    )]


def _check_t3_cloud() -> list[HealthResult]:
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []
    results.append(HealthResult(label="T3 mode", ok=True, detail="cloud"))
    results.append(_check_vector_service())
    results.extend(_check_managed_service_probe())

    # Credential lines are INFORMATIONAL, never fatal (nexus-nmw3i /
    # nexus-c7aj3): serving is the vector service in every mode (RDR-155
    # P4a.2 — make_t3() is service-backed unconditionally). The ChromaDB
    # migration-source credential rows died with the migration machinery
    # at P4b.

    # VOYAGE_API_KEY — server-side embedding on the service path. The
    # client key is no longer a client credential of any kind (nexus-sghyo,
    # Hal determination 2026-07-28: "we do no embedding on the client" —
    # RDR-188 already moved reranking server-side, so no client code path
    # consumed this key for rerank either). It remains an OPTIONAL
    # engine-bound setting: a locally-spawned engine plumbs it through
    # (daemon/storage_service_daemon.py) for voyage mode. Not a serving
    # requirement from the client's perspective either way.
    voyage_key = get_credential("voyage_api_key")
    results.append(HealthResult(
        label="Voyage AI (VOYAGE_API_KEY)",
        ok=True,
        detail="set" if voyage_key else "not set (enrichment/engine-bootstrap only, not for serving)",
    ))

    # Pipeline version sweep read Chroma COLLECTION metadata, which has no
    # pgvector equivalent — retired with the Chroma serving path, but the
    # line must still appear, not vanish (reviewer-c7aj3 Medium).
    results.append(HealthResult(
        label="pipeline versions",
        ok=True,
        detail="sweep retired with the Chroma serving path (RDR-155 P4a)",
    ))

    return results


def _check_tools() -> list[HealthResult]:
    results: list[HealthResult] = []

    # ripgrep
    rg_path = shutil.which("rg")
    # nexus-9xfx5 (fresh-install MVV finding #3): rg is an OPTIONAL system
    # accelerator that `pip install conexus` can never provide — its absence
    # is a degradation (hybrid search off), not a broken install. Render it
    # like an uninstalled git hook: ✓ with the detail + install suggestions,
    # never a red ✗ / non-zero doctor exit on a virgin box.
    r = HealthResult(
        label="ripgrep   (rg)",
        ok=True,
        detail=rg_path or "not installed — hybrid search disabled (optional)",
        fatal=False,
    )
    if not rg_path:
        # nexus-njmg (GH #622): winget --scope user avoids UAC-prompt
        # failures during unattended install on Windows.
        r.fix_suggestions = [
            "brew install ripgrep                                          (macOS)",
            "apt install ripgrep                                           (Ubuntu/Debian)",
            "winget install --id BurntSushi.ripgrep.MSVC --scope user      (Windows)",
            "https://github.com/BurntSushi/ripgrep#installation",
        ]
    results.append(r)

    # git
    git_path = shutil.which("git")
    r = HealthResult(
        label="git",
        ok=bool(git_path),
        detail=git_path or "not found on PATH",
        fatal=True,
    )
    if not git_path:
        r.fix_suggestions = [
            "brew install git                                              (macOS)",
            "apt install git                                               (Ubuntu/Debian)",
            "winget install --id Git.Git --scope user                      (Windows)",
            "https://git-scm.com/downloads",
        ]
    results.append(r)

    # bd (beads, optional)
    bd_path = shutil.which("bd")
    if bd_path:
        results.append(HealthResult(label="bd (beads, optional)", ok=True, detail=bd_path))
    else:
        # bd has no winget package (verified 2026-05-10); upstream releases
        # ship as a GitHub release zip operators install manually.
        results.append(HealthResult(
            label="bd (beads, optional)",
            ok=True,
            detail="not found — task tracking unavailable",
            fix_suggestions=[
                "https://github.com/BeadsProject/beads/releases   (download for your OS)",
            ],
        ))

    # npx (Node.js, plugin-only)
    # Required by the conexus Claude Code plugin, which spawns the
    # ``sequential-thinking`` and ``context7`` MCP servers via ``npx -y …``.
    # The CLI alone does not need it, so this is non-fatal — but a missing
    # ``npx`` causes silent MCP-server failures the moment a plugin tool is
    # invoked. Reported as informational so plugin users see the gap before
    # they hit it at runtime.
    npx_path = shutil.which("npx")
    if npx_path:
        results.append(HealthResult(label="npx (Node.js, plugin-only)", ok=True, detail=npx_path))
    else:
        results.append(HealthResult(
            label="npx (Node.js, plugin-only)",
            ok=True,
            detail="not found — plugin MCP servers (sequential-thinking, context7) will fail",
            fix_suggestions=[
                "brew install node                                              (macOS)",
                "apt install nodejs npm                                         (Ubuntu/Debian)",
                "winget install --id OpenJS.NodeJS.LTS --scope user             (Windows)",
                "https://nodejs.org/                                            (other platforms)",
            ],
        ))

    return results


# nexus-l2ku5: the (binary, expected serverInfo.name) pairs for the two
# published MCP entry points. Order matches ``[project.scripts]`` in
# pyproject.toml.
_MCP_ENTRY_POINTS: tuple[tuple[str, str], ...] = (
    ("nx-mcp", "nexus"),
    ("nx-mcp-catalog", "nexus-catalog"),
)

# The exact JSON-RPC ``initialize`` request that found nexus-l2ku5 by hand:
# mcp 2.0.0 (2026-07-28) removed ``mcp.server.fastmcp`` and the unbounded
# ``mcp>=1.0`` floor let it into every fresh install for 4 days, killing
# both servers at import with zero signal (Claude Code swallows stderr; no
# test gate ever booted the INSTALLED entry point — the dev venv is
# uv.lock-pinned to mcp 1.x).
_MCP_INITIALIZE_REQUEST = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
    '{"protocolVersion":"2024-11-05","capabilities":{},'
    '"clientInfo":{"name":"nx-doctor","version":"1"}}}\n'
)

# nexus-l2ku5 critique round 2: local subprocess handshake, not a network
# call — parity with this file's other probes (e.g. MinerU's 2.0s HTTP
# timeout). Two entry points probed serially, so a worst case of both
# hanging is 2 * 8s = 16s, not the 30s the prior 15.0 implied.
_MCP_PROBE_TIMEOUT_S = 8.0

# Bound both line COUNT and per-line LENGTH — a crashing binary controls
# its own stderr and could emit one arbitrarily long line (no newlines) to
# blow out doctor's output; truncate defensively either way.
_STDERR_EXCERPT_LINE_MAX_CHARS = 200


def _first_lines(text: str, n: int) -> str:
    """Join the first *n* non-blank lines of *text* with ' | ' separators,
    each truncated to :data:`_STDERR_EXCERPT_LINE_MAX_CHARS`."""
    lines = [ln[:_STDERR_EXCERPT_LINE_MAX_CHARS] for ln in text.splitlines() if ln.strip()]
    return " | ".join(lines[:n])


def _probe_mcp_server(
    binary_path: str, expected_name: str, *, timeout: float = _MCP_PROBE_TIMEOUT_S
) -> tuple[bool, str]:
    """Spawn *binary_path*, send a JSON-RPC ``initialize`` request on
    stdin, and verify the response's ``result.serverInfo.name`` matches
    *expected_name*.

    Returns ``(ok, detail)``. On failure, *detail* carries the first 3
    lines of stderr when available — that is where a ``ModuleNotFoundError``
    lives, and surfacing it (not "could not check") is the entire point of
    this probe (nexus-l2ku5).

    LOAD-BEARING ASSUMPTION: the MCP stdio server's read loop exits on
    stdin EOF. ``subprocess.run(input=...)`` writes the one request then
    closes stdin, which is what lets a healthy server finish this
    request/response and exit on its own within *timeout* instead of
    idling as a long-lived process — the same shape as a real MCP client
    session, just closed after one turn.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — binary_path resolved via shutil.which, not attacker input
            [binary_path],
            input=_MCP_INITIALIZE_REQUEST,
            capture_output=True,
            text=True,
            errors="replace",  # non-UTF8 crash output (e.g. a mangled traceback) must not raise UnicodeDecodeError out of a health check
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stderr_excerpt = _first_lines(
            exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace"),
            3,
        )
        detail = f"timed out after {timeout:.0f}s waiting for initialize response"
        if stderr_excerpt:
            detail += f" — stderr: {stderr_excerpt}"
        return False, detail
    except OSError as exc:
        return False, f"failed to spawn {binary_path}: {exc}"
    except Exception as exc:  # noqa: BLE001 — any other spawn/communicate failure must still report, not crash `nx doctor`
        return False, f"probe error: {exc!r}"

    stderr_excerpt = _first_lines(proc.stderr or "", 3)

    if proc.returncode != 0:
        detail = f"exited {proc.returncode}"
        if stderr_excerpt:
            detail += f" — stderr: {stderr_excerpt}"
        return False, detail

    response: dict | None = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("id") == 1:
            response = candidate
            break

    if response is None:
        detail = "no parseable JSON-RPC response on stdout"
        if stderr_excerpt:
            detail += f" — stderr: {stderr_excerpt}"
        return False, detail

    result = response.get("result")
    server_name = (
        result.get("serverInfo", {}).get("name") if isinstance(result, dict) else None
    )
    if server_name != expected_name:
        detail = f"serverInfo.name={server_name!r}, expected {expected_name!r}"
        if stderr_excerpt:
            detail += f" — stderr: {stderr_excerpt}"
        return False, detail

    return True, f"serverInfo.name={server_name!r}"


def _resolve_mcp_binary(binary_name: str) -> tuple[str | None, bool]:
    """Resolve *binary_name* on PATH, preferring an entry NOT under this
    running process's own ``sys.prefix``.

    nexus-l2ku5 critique round 2: a bare ``shutil.which(binary_name)`` is
    NOT sufficient — under ``uv run nx doctor`` (the routine maintainer
    invocation in this checkout), PATH is prefixed with THIS checkout's
    own ``.venv/bin``, so a plain ``which`` silently resolves the
    lock-pinned dev venv's own entry point and never reaches a separately
    installed tool later on PATH (e.g. ``~/.local/bin``) — exactly the
    substrate this check exists to get past.

    Resolution rule (by preference, not exclusion — a real binary is
    always probed, never skipped):
    1. Walk PATH left to right; the FIRST match found in a directory that
       is NOT under ``sys.prefix`` wins — but only among candidates the
       HOME-scoping rule below admits.
    2. If no such match exists but a match under ``sys.prefix`` does
       (e.g. this process's own venv is the ONLY thing on PATH — a valid
       shape when a user invokes that venv's own ``nx`` directly), that
       match is still returned and still probed for real.
    3. ``None`` only when the binary resolves nowhere on PATH at all.

    HOME-scoping (nexus-k0lk9 sibling finding, 7.4.0 cut): when this
    process's own prefix lives under the CURRENT ``$HOME``, a PATH hit
    OUTSIDE that ``$HOME`` is a FOREIGN install, not "the separately
    installed tool" l2ku5's preference exists to reach. The concrete
    incident: the release sandbox (``HOME=~/nexus-sandbox``) ran doctor
    from the sandbox's own tool venv; this resolver skipped the sandbox's
    nx-mcp as own-prefix and probed the REAL home's live install — the
    gate's verdict then tracked host load/health instead of the artifact
    under test. Foreign hits are demoted below the own-prefix fallback,
    never dropped: with own-prefix outside ``$HOME`` (system-wide
    installs) the pre-existing rule applies unchanged, and on a dev box
    both the checkout venv and ``~/.local/bin`` are under ``$HOME`` so
    the installed tool still wins exactly as before.

    ACCEPTED TRADE-OFF (substantive-critic, 7.4.0): a home-rooted process
    (e.g. ``uv run`` from a checkout) with the real install ONLY outside
    ``$HOME`` (e.g. ``/usr/local/bin``) now probes its own venv instead
    of that install — foreign-vs-genuine is undecidable from the path
    alone, the result is honestly labeled ``is_own_venv=True`` (never a
    silent lie), and the topology is pinned by
    ``test_home_scoping_tradeoff_outside_home_install_loses_to_own_venv``
    so a future change to this choice is deliberate, not accidental.

    Returns ``(path_or_none, is_own_venv)``.
    """
    own_prefix = str(Path(sys.prefix).resolve())
    try:
        home = str(Path.home().resolve())
    except (OSError, RuntimeError):
        # Path.home() raises RuntimeError (not OSError) when HOME is unset
        # with no passwd entry — the K8s arbitrary-UID / distroless shape
        # (nexus-262a7, critic-reproduced: an uncaught raise here crashes
        # `nx doctor` outright). No home → HOME-scoping disabled, the
        # pre-existing l2ku5 preference applies unchanged.
        home = ""
    own_prefix_under_home = bool(home) and (
        own_prefix == home or own_prefix.startswith(home + os.sep)
    )
    path_env = os.environ.get("PATH", os.defpath)
    own_prefix_hit: str | None = None
    foreign_hit: str | None = None
    for directory in path_env.split(os.pathsep):
        if not directory:
            continue
        hit = shutil.which(binary_name, path=directory)
        if not hit:
            continue
        try:
            resolved_dir = str(Path(hit).resolve().parent)
        except OSError:
            resolved_dir = str(Path(directory).resolve())
        if resolved_dir == own_prefix or resolved_dir.startswith(own_prefix + os.sep):
            if own_prefix_hit is None:
                own_prefix_hit = hit
            continue
        if own_prefix_under_home and not (
            resolved_dir == home or resolved_dir.startswith(home + os.sep)
        ):
            # Foreign install (outside the current $HOME while our own
            # prefix is home-rooted) — remember it only as a last resort.
            if foreign_hit is None:
                foreign_hit = hit
            continue
        return hit, False
    if own_prefix_hit is not None:
        return own_prefix_hit, True
    return foreign_hit, False


def _check_mcp_entry_points() -> list[HealthResult]:
    """nexus-l2ku5: probe the INSTALLED ``nx-mcp`` / ``nx-mcp-catalog``
    entry points with a real JSON-RPC ``initialize`` handshake — the layer
    test that was missing. Every other gate (unit/integration/MVV/sandbox)
    ran against the uv.lock-pinned dev venv and never booted the entry
    point a real install resolves fresh, so ``mcp>=1.0`` (no upper bound)
    let ``mcp`` 2.0.0 delete ``mcp.server.fastmcp`` under every fresh
    install for 4 days with zero signal.

    Resolution truthfully follows :func:`_resolve_mcp_binary`: it walks
    PATH and prefers the first hit that is NOT this running process's own
    ``sys.prefix`` — so under ``uv run nx doctor`` in this checkout it
    skips PAST this checkout's own ``.venv/bin`` to whatever separately
    installed tool (e.g. ``~/.local/bin``) is also on PATH. Only when the
    OWN venv is the sole match does it get probed, and the detail line
    says so explicitly rather than silently passing off a lock-pinned
    probe as proof of the installed artifact.

    CRITICAL POLICY: failure to probe is never rendered ✓.
    * Binary absent from PATH entirely → soft WARN (⚠) — expected in a
      dev checkout where no separately installed tool need be on PATH;
      never claimed OK.
    * Binary present but the handshake fails (crash / timeout / garbage /
      unexpected exception) → hard FAIL (✗), carrying the stderr excerpt
      where a ``ModuleNotFoundError`` would show up.
    """
    results: list[HealthResult] = []
    for binary_name, expected_server_name in _MCP_ENTRY_POINTS:
        label = f"MCP entry point ({binary_name})"
        binary_path, is_own_venv = _resolve_mcp_binary(binary_name)
        if not binary_path:
            results.append(HealthResult(
                label=label,
                ok=False,
                warn=True,
                detail="installed tool not found on PATH (dev-checkout edge)",
                fix_suggestions=["reinstall the conexus tool"],
            ))
            continue

        try:
            ok, detail = _probe_mcp_server(binary_path, expected_server_name)
        except Exception as exc:  # noqa: BLE001 — the probe itself must not crash `nx doctor`; an unexpected exception probing a PRESENT binary is at least as bad as a confirmed crash, so this is a hard FAIL, not a soft warn
            _log.warning(
                "doctor_mcp_entry_point_probe_failed",
                binary=binary_name,
                error=str(exc),
            )
            results.append(HealthResult(
                label=label,
                ok=False,
                fatal=True,
                detail=f"{binary_path} — probe raised {type(exc).__name__}: {exc}",
                fix_suggestions=["reinstall the conexus tool"],
            ))
            continue

        prefix_note = " (probing this process's own venv)" if is_own_venv else ""
        results.append(HealthResult(
            label=label,
            ok=ok,
            detail=f"{binary_path}{prefix_note} — {detail}",
            fatal=not ok,
            fix_suggestions=["reinstall the conexus tool"] if not ok else [],
        ))
    return results


def _check_git_hooks(repo_scope: str | Path | None = None) -> list[HealthResult]:
    """Walk registered repos' git hooks for stanza drift.

    ``repo_scope`` (nexus-jds59): the catalog + legacy registry this walk
    reads from are a SHARED, machine-wide store — not scoped to the
    caller's ``$HOME``/``NEXUS_CONFIG_DIR``. An automation harness that
    provisions its own throwaway repo (the release-sandbox shakedown's
    fixture checkout) still sees every OTHER repo ever registered on the
    same machine, including the live dev checkout the harness reinstalls
    from — so a deliberate hold on that repo's hook stanza (e.g. pinned
    behind an unreleased ``nx`` feature) reds a gate that has nothing to
    do with it. Passing a root here restricts the walk to repos at or
    under that root; repos outside it are silently excluded from the
    result (not even rendered as ``ok``), and the default (``None``)
    preserves the original walk-every-registered-repo behavior a
    developer running bare ``nx doctor`` relies on.
    """
    # nexus-8g79.10 (V2): import from the lower-layer module instead of
    # reaching up into commands/. Use module-attribute access so test
    # monkeypatches on ``nexus._git_hooks_meta.effective_hooks_dir``
    # reach the live binding at call time.
    import re  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
    from nexus import _git_hooks_meta as _ghm  # noqa: PLC0415 — deferred to avoid circular import
    from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END  # noqa: PLC0415 — deferred to avoid circular import
    _effective_hooks_dir = _ghm.effective_hooks_dir
    from nexus.config import catalog_path, nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.repos import list_repos_dual_with_catalog_roots  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []
    hook_names = ("post-commit", "post-merge", "post-rewrite")
    registry_path = nexus_config_dir() / "repos.json"

    # nexus-mkj6u shakeout: extract the canonical stanza from the
    # current template so we can detect drift in already-installed
    # hooks (e.g. the pre-pgrep-guard stanza). Done once per call;
    # the import is lazy because commands/hooks.py imports click
    # which we don't want to pay for at health-check time when no
    # repos are registered.
    def _canonical_stanza_body(hook_name: str) -> str | None:
        """Canonical body for *hook_name*.

        Resolved per hook by name (``_stanza_for``) so a hook-specific
        stanza, should one return, compares against its own template.
        """
        try:
            from nexus.commands.hooks import _stanza_for  # noqa: PLC0415 — deferred to avoid circular import
        except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
            return None
        m = re.search(
            rf"{re.escape(SENTINEL_BEGIN)}\n(.*?)\n{re.escape(SENTINEL_END)}",
            _stanza_for(hook_name), re.DOTALL,
        )
        return m.group(1) if m else None

    def _installed_stanza_body(content: str) -> str | None:
        m = re.search(
            rf"{re.escape(SENTINEL_BEGIN)}\n(.*?)\n{re.escape(SENTINEL_END)}",
            content, re.DOTALL,
        )
        return m.group(1) if m else None

    canonical_by_hook = {n: _canonical_stanza_body(n) for n in hook_names}

    def _stanza_state(repo: Path, name: str) -> str:
        try:
            from nexus.commands.hooks import hook_stanza_state  # noqa: PLC0415 — deferred to avoid circular import
        except Exception:  # noqa: BLE001 — boundary fallback
            return "unknown"
        return hook_stanza_state(repo, name)

    # RDR-137 Phase 3.1 (nexus-tts0d.6): catalog-backed enumeration with
    # legacy ``repos.json`` fallback via the dual-read shim. Catalog
    # paths come from ``owners WHERE owner_type='repo'``; the registry
    # provides legacy installs that have not yet been re-indexed.
    cat = None
    repos: list[str] = []
    # nexus-cw262 (round-2 critique, T2 21456 moderate finding): a single
    # list_repos_dual_with_catalog_roots call now serves BOTH the walk list
    # (``repos``, CATALOG ∪ registry union) and the attribution set
    # (``catalog_repo_roots``, catalog-only) from ONE
    # cat.list_owners_by_type("repo") round trip. Pre-fix this was two
    # independent calls (list_repos_dual, then a second list_owners_by_type
    # inline below); a transient failure of the SECOND call alone silently
    # degraded catalog_repo_roots to empty, misattributing a genuinely
    # catalog-owned dead owner as a legacy repos.json entry — a narrow,
    # self-healing (next doctor run) but real recurrence of the population-
    # mismatch class this same bead's round-1 fix closed for the common case.
    catalog_repo_roots: set[str] = set()
    # nexus-cw262 round-3 critique (T2 21467 Significant-2): the SAME call
    # also yields the owner-deactivate capability signal now — "unknown"
    # (not "available") is the safe default until proven otherwise, so a
    # failed/empty read never accidentally claims a route that may not
    # exist on the connected engine.
    deactivate_capability = "unknown"
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import
        cat = make_catalog_reader()
        repos, catalog_repo_roots, deactivate_capability = list_repos_dual_with_catalog_roots(
            cat=cat, registry_path=registry_path)
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        # RDR-137 followup IMP-20 (nexus-43qgm.20): exc_info=True so
        # the operator sees the traceback alongside the error message
        # (NameError / AttributeError otherwise appear only as the
        # rendered str(exc) with no source location).
        _log.warning(
            "doctor_registry_load_failed", error=str(exc), exc_info=True,
        )
        repos = []
        catalog_repo_roots = set()
        deactivate_capability = "unknown"

    # nexus-jds59: apply the scope filter (if any) BEFORE the "no repos"
    # branch below, so a scoped walk with nothing in scope reports an
    # honest "out of scope" reason rather than the unscoped "none
    # registered at all" message.
    _scope_root: Path | None = None
    _excluded_out_of_scope = 0
    if repo_scope is not None:
        _scope_root = Path(repo_scope).resolve()
        _scoped_repos: list[str] = []
        for repo_str in repos:
            try:
                _resolved = Path(repo_str).resolve()
            except OSError:
                # Unreadable path component -- treat as out of scope
                # rather than crashing the filter; the per-repo probe
                # below already degrades vanished/unreadable roots
                # honestly for the unscoped walk.
                _excluded_out_of_scope += 1
                continue
            if _resolved == _scope_root or _scope_root in _resolved.parents:
                _scoped_repos.append(repo_str)
            else:
                _excluded_out_of_scope += 1
        repos = _scoped_repos

    if not repos:
        if _scope_root is not None and _excluded_out_of_scope:
            results.append(HealthResult(
                label="git hooks", ok=True,
                detail=(
                    f"no repos registered under scope {_scope_root} "
                    f"({_excluded_out_of_scope} registered repo(s) outside "
                    "scope, skipped)"
                ),
            ))
        else:
            results.append(HealthResult(
                label="git hooks", ok=True,
                detail="no repos registered — run: nx index repo <path>",
            ))
    else:
        for repo_str in repos:
            repo_path = Path(repo_str)
            try:
                hdir = _effective_hooks_dir(repo_path)
                installed = [
                    n for n in hook_names
                    if (hdir / n).exists() and SENTINEL_BEGIN in (hdir / n).read_text()
                ]
                if installed:
                    # nexus-mkj6u: drift check — compare installed stanza
                    # body to the canonical template body. Different
                    # body means the user is running an old stanza
                    # (e.g. pre-pgrep-guard, vulnerable to the multi-
                    # indexer pile-up race).
                    # One comparison (commands/hooks.py hook_stanza_state,
                    # nexus-trwxr): a second copy of this selector drifted
                    # on arrival.
                    drifted: list[str] = []
                    for name in installed:
                        if canonical_by_hook.get(name) is None:
                            continue
                        if _stanza_state(repo_path, name) == "stale":
                            drifted.append(name)
                    if drifted:
                        results.append(HealthResult(
                            label="git hooks (stanza drift)",
                            ok=False,
                            detail=(
                                f"{repo_path} — installed stanza differs from "
                                f"current template ({', '.join(drifted)}). "
                                "May be missing pile-up guard or other fixes."
                            ),
                            fix_suggestions=[f"nx hooks update {repo_path}"],
                            fatal=False,
                        ))
                    else:
                        results.append(HealthResult(
                            label="git hooks", ok=True,
                            detail=f"{repo_path} ({', '.join(installed)})",
                        ))
                else:
                    results.append(HealthResult(
                        label="git hooks", ok=True,
                        detail=f"{repo_path} — not installed",
                        fix_suggestions=[f"nx hooks install {repo_path}"],
                    ))
            except Exception as exc:  # noqa: BLE001 — git-hook probe is best-effort; degrade to an HONEST signal, never a silent ok=True (nexus-9t86i / nexus-7kl32: a check that could not read state must never render ✓)
                # nexus-7kl32: the dominant cause of a probe failure here is
                # a dead owner — a registered repo whose root no longer
                # exists on disk (bench-index sandboxes, throwaway probe
                # checkouts, stale worktrees; the u8n4r-era debris
                # population). That case gets its own honest wording; any
                # other probe failure still degrades honestly, just without
                # the dead-owner framing. Either way this is now ok=False,
                # warn=True (soft warning ⚠, never fatal — RDR-129 B4) so a
                # dead owner never again renders as a signal-free green.
                try:
                    vanished = not repo_path.exists()
                except OSError:
                    # code-review IMPORTANT (nexus-7kl32): .exists() itself
                    # can raise (e.g. a permission-denied path component) —
                    # the sibling classifier
                    # (catalog_cmds.owners._classify_owner_root) guards this
                    # identical risk. Degrade to the generic could-not-check
                    # branch instead of letting it crash `nx doctor` — the
                    # whole point of this fix was to STOP creating new crash
                    # surfaces out of probe failures.
                    vanished = False

                if vanished:
                    if str(repo_path) in catalog_repo_roots:
                        # A catalog owner — nx catalog owners --census
                        # covers it (same list_repos_dual_with_catalog_roots
                        # round trip built this attribution set above).
                        #
                        # nexus-cw262 round-3 critique (T2 21467
                        # Significant-2): the mutation arm's ACTUAL
                        # availability depends on which engine build this
                        # tenant is connected to — the live cloud engine at
                        # authorship time genuinely predates the route. Name
                        # the census unconditionally (it always works, engine
                        # floor or not) but qualify the mutation arm honestly
                        # per the capability signal computed above, rather
                        # than claiming it unconditionally.
                        fix = ["nx catalog owners --census — inspects dead owners"]
                        if deactivate_capability == "available":
                            fix.append(
                                "--execute deactivate --no-dry-run --confirm "
                                "deregisters eligible ones (nexus-cw262)"
                            )
                        elif deactivate_capability == "unavailable":
                            fix.append(
                                "the --execute deactivate mutation arm requires "
                                "an engine build carrying the nexus-cw262 "
                                "owner-deactivate route (not yet deployed here); "
                                "re-run after the connected engine is upgraded"
                            )
                        else:  # "unknown" — no owners response to read the signal from
                            fix.append(
                                "whether --execute deactivate is available on the "
                                "connected engine could not be confirmed this run "
                                "(nexus-cw262)"
                            )
                        results.append(HealthResult(
                            label="git hooks", ok=False, warn=True,
                            detail=(
                                f"{repo_path} — owner root no longer exists "
                                "on disk (dead owner)"
                            ),
                            fix_suggestions=fix,
                        ))
                    else:
                        # code-review SIGNIFICANT (nexus-7kl32, critic
                        # finding 2): a legacy repos.json-only entry is NOT
                        # visible to the census (catalog owners only) —
                        # pointing at it here would be exactly the
                        # misleading-rendering class this bead exists to
                        # eliminate, just relocated. Its actual remedy also
                        # differs: repos.json is a local, directly editable
                        # file, not a catalog row.
                        results.append(HealthResult(
                            label="git hooks", ok=False, warn=True,
                            detail=(
                                f"{repo_path} — owner root no longer exists "
                                "on disk (dead owner; legacy repos.json "
                                "entry — not covered by `nx catalog owners "
                                "--census`, which classifies catalog owners "
                                "only)"
                            ),
                            fix_suggestions=[
                                f"remove the stale entry from {registry_path}"
                            ],
                        ))
                else:
                    results.append(HealthResult(
                        label="git hooks", ok=False, warn=True,
                        detail=f"{repo_path} — could not check ({exc})",
                    ))

    return results


def _check_index_log() -> list[HealthResult]:
    """Most-recent index activity across BOTH log surfaces.

    2026-07-15: this check watched only ``index.log`` (the git-HOOK append
    log, hooks.py) and reported "last write 460 hours ago" during a session
    with two live index runs — real runs write per-run rotated logs at
    ``logs/index-*.log``. Report the newest of either, saying which.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import

    def _age_str(mtime: float) -> str:
        age_s = time.time() - mtime
        if age_s < 60:
            return f"{int(age_s)}s ago"
        if age_s < 3600:
            return f"{int(age_s // 60)} minutes ago"
        return f"{int(age_s // 3600)} hours ago"

    candidates: list[tuple[float, str, str]] = []  # (mtime, path, kind)
    hook_log = nexus_config_dir() / "index.log"
    if hook_log.exists():
        candidates.append((hook_log.stat().st_mtime, str(hook_log), "hook log"))
    run_logs = sorted(
        (nexus_config_dir() / "logs").glob("index-*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if run_logs:
        newest = run_logs[0]
        candidates.append((newest.stat().st_mtime, str(newest), "run log"))
    if not candidates:
        return [HealthResult(
            label="index log", ok=True,
            detail="no index activity recorded yet (no run logs, hooks have not fired)",
        )]
    mtime, path, kind = max(candidates)
    detail = f"{path} ({kind}, last write: {_age_str(mtime)})"

    # This check reported RECENCY ONLY and returned ok=True unconditionally,
    # so it could not fail -- the nexus-moht0 vacuous-gate shape. The hook log
    # is the sole sink for a DETACHED background index run, which means it is
    # also the only place that run's warnings land; on a working box it had
    # accumulated 1528 aspect_source_path_uncanonical warnings and 49
    # manifest_write_many_failed events that nothing ever surfaced. Report the
    # MOST RECENT run's warnings only: a live recurring fault shows on every
    # doctor run, while historical noise cannot nag forever.
    warned = _recent_index_log_warnings(hook_log)
    if warned:
        top = ", ".join(f"{name} x{count}" for name, count in warned[:3])
        return [HealthResult(
            label="index log", ok=False, warn=True,
            detail=f"{detail}; last run emitted warnings: {top}",
            fix_suggestions=[
                f"read the last run: sed -n '/^=== nx index/,$p' {hook_log}",
            ],
        )]
    return [HealthResult(label="index log", ok=True, detail=detail)]


def _recent_index_log_warnings(hook_log: Path) -> list[tuple[str, int]]:
    """Warning events in the LAST stamped run of the hook log, most first.

    Scoped to the final ``=== nx index ...`` header so the count reflects the
    most recent run rather than the file's whole history. Best-effort: an
    unreadable or absent log yields no warnings rather than an error, because
    a doctor check must never fail on its own telemetry.
    """
    import collections as _collections  # noqa: PLC0415 — local, keeps CLI startup cost off this path
    import re as _re  # noqa: PLC0415 — local, same reason

    try:
        if not hook_log.exists():
            return []
        # Read a bounded tail; the hook rotates at 4MiB and a single run is
        # far smaller, so 2MiB always covers the last run in practice.
        size = hook_log.stat().st_size
        with hook_log.open("rb") as fh:
            if size > 2_097_152:
                fh.seek(size - 2_097_152)
            body = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    marker = body.rfind("=== nx index ")
    if marker != -1:
        body = body[marker:]
    counts: _collections.Counter[str] = _collections.Counter()
    for match in _re.finditer(r"event='([a-z_]+)'[^\n]*level='warning'", body):
        counts[match.group(1)] += 1
    return counts.most_common()


#: Fallback staleness window for a T1 session lease file this check cannot
#: parse (malformed JSON, missing ``expires_at``, or the pre-ngcpo bare-token
#: format). Mirrors the fail-safe style of health checks elsewhere in this
#: module: an unparseable file is reaped only once it is old enough that no
#: legitimate torn-write window could explain it (atomic temp-file +
#: os.replace publishes never leave a torn file mid-write), not on first
#: sight. One hour comfortably exceeds the default 24h lease TTL's refresh
#: cadence without risking a false reap of a lease mid-publish.
_ORPHAN_T1_LEASE_STALE_FALLBACK_SECONDS = 3600.0


def _check_orphan_t1_lease() -> list[HealthResult]:
    """Report on + reap stale T1 session lease files.

    Ported (nexus-8zfwv, 2026-08-07) off the RDR-149 P4 ``t1_addr.*``
    ``ServiceRegistry`` lease this check used to read -- ``T1LeasePublisher``,
    the only thing that ever published that format, is retired (deleted at
    ff744321). The live cross-process "session has a live T1 scope" signal
    is now the lease file ``nexus.db.t1.publish_t1_session_lease`` writes:
    JSON ``{token, expires_at}`` at ``t1_session_lease.<session_id>``.

    Unlike the retired check, THIS one actually reaps: nothing else sweeps
    an expired ``t1_session_lease.*`` file after an ungraceful owner death
    (only clean teardown -- ``clear_t1_session_lease`` -- removes one), so
    without this check stale lease files accumulate forever. A lease past
    its ``expires_at`` (plus the reader's freshness margin) is unlinked
    here, not merely reported.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.db.t1 import (  # noqa: PLC0415 — deferred to avoid circular import
        _T1_LEASE_FRESHNESS_MARGIN_SECONDS,
        _T1_SESSION_LEASE_PREFIX,
    )

    config_dir = nexus_config_dir()
    if not config_dir.exists():
        return [HealthResult(label="T1 sessions", ok=True, detail="no nexus config dir")]

    lease_files = list(config_dir.glob(f"{_T1_SESSION_LEASE_PREFIX}*"))
    if not lease_files:
        return [HealthResult(label="T1 sessions", ok=True, detail="no live T1 sessions")]

    now = time.time()
    fresh: list[str] = []
    reaped: list[str] = []
    for path in lease_files:
        session_id = path.name[len(_T1_SESSION_LEASE_PREFIX):]
        if not session_id:
            continue
        reapable: bool
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(data["expires_at"])
            reapable = now >= expires_at - _T1_LEASE_FRESHNESS_MARGIN_SECONDS
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            # Unparseable (pre-ngcpo bare-token format, or corruption):
            # fail-safe, not fail-open -- only reap if old enough that no
            # legitimate in-flight publish could explain it.
            _log.debug("t1_session_lease_unparseable", path=str(path))
            try:
                age_s = now - path.stat().st_mtime
                reapable = age_s > _ORPHAN_T1_LEASE_STALE_FALLBACK_SECONDS
            except OSError:
                reapable = False

        if reapable:
            try:
                path.unlink()
                reaped.append(session_id)
                _log.info("t1_session_lease_reaped", session_id=session_id, path=str(path))
            except OSError as exc:
                _log.debug("t1_session_lease_reap_failed", path=str(path), error=str(exc))
                fresh.append(session_id)  # best-effort: still there, report it
        else:
            fresh.append(session_id)

    parts: list[str] = []
    if fresh:
        parts.append(f"{len(fresh)} live T1 session(s): {', '.join(fresh)}")
    if reaped:
        parts.append(f"reaped {len(reaped)} expired lease(s): {', '.join(reaped)}")
    if not parts:
        parts.append("no live T1 sessions")

    return [HealthResult(label="T1 sessions", ok=True, detail="; ".join(parts))]


def _check_garbage() -> list[HealthResult]:
    """The garbage sweep (:mod:`nexus.garbage`, Sam 2026-09-05).

    Local litter (stale mint locks, rotated logs past 14 days, operator
    dispatch dumps past 7) is reaped here on every run, the same way the
    T1 lease and handoff-marker reapers above behave. Catalog litter
    (orphaned links, tombstones past the one-day window) is COUNTED here
    and reclaimed only by ``nx doctor --fix``, since each reclaim is an
    engine write. A non-zero catalog count is a warning that names the
    command; an unreachable engine is a warning too, never a clean row
    (nexus-moht0: a sweep that could not look is not a pass).
    """
    from nexus import config as _config  # noqa: PLC0415 — deferred to avoid circular import; module import so the by-value ratchet stays at its census
    from nexus.garbage import catalog_garbage, sweep_local_garbage  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []
    report = sweep_local_garbage(_config.nexus_config_dir())
    if report.failed_count:
        results.append(HealthResult(
            label="Local garbage",
            ok=False, warn=True,
            detail=(
                f"reaped {report.removed_count}, could not remove "
                f"{report.failed_count}: "
                + ", ".join(f"{k}={len(v)}" for k, v in report.failed.items())
            ),
        ))
    elif report.removed_count:
        results.append(HealthResult(
            label="Local garbage", ok=True,
            detail="reaped " + ", ".join(f"{len(v)} {k}" for k, v in report.removed.items()),
        ))
    else:
        results.append(HealthResult(label="Local garbage", ok=True, detail="none"))

    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import
        reader = make_catalog_reader()
    except Exception as exc:  # noqa: BLE001 - report, never crash doctor
        results.append(HealthResult(
            label="Catalog garbage", ok=False, warn=True,
            detail=f"could not open the catalog: {exc}",
        ))
        return results
    # Counting is all reads (the purge is a dry run), so the READ handle
    # serves it; the write-only proxy refuses ``orphaned_links``.
    garbage = catalog_garbage(reader)
    if garbage.error:
        results.append(HealthResult(
            label="Catalog garbage", ok=False, warn=True,
            detail=f"could not count: {garbage.error}",
        ))
    elif garbage.total:
        results.append(HealthResult(
            label="Catalog garbage", ok=False, warn=True,
            detail=(
                f"{garbage.orphaned_links} orphaned link(s), "
                f"{garbage.trash_documents} tombstoned document(s) and "
                f"{garbage.stranded_chunks} stranded chunk(s) past 1 day"
            ),
            fix_suggestions=["nx doctor --fix"],
        ))
    else:
        results.append(HealthResult(label="Catalog garbage", ok=True, detail="none"))
    return results


def _check_orphan_t1_handoff() -> list[HealthResult]:
    """Reap orphaned T1 session-handoff markers (nexus-9l147).

    nexus-d76vc's SessionStart hook writes a LIVE marker
    (``t1_handoff.<mcp_pid>``, :mod:`nexus.daemon.t1_handoff`) naming the
    transcript's new session id for a target MCP process; the MCP
    lifespan's handoff watcher normally claims it (atomically renaming it
    to the tick-private ``t1_handoff.claimed.<mcp_pid>``) and consumes it
    within one 5s tick. If the target ``mcp_pid`` dies (crash/OOM/SIGKILL)
    in the window between the marker being written and its next tick —
    either the live or the claimed variant, depending on exactly when it
    died — the marker is never consumed and persists on disk indefinitely.
    Functionally safe (the handoff watcher's own staleness check rejects a
    stale marker even after pid reuse), but it is unbounded disk litter
    with no other sweep touching it. Neither live T1 on-disk artifact
    self-reaps on read: ``read_t1_session_lease`` treats an expired lease
    as absent but does not unlink the file either, which is why
    :func:`_check_orphan_t1_lease` exists as an explicit sweep for THAT
    litter class. This check is the handoff-marker analogue of that same
    pattern — a distinct litter class, same "nothing else reaps it"
    problem. (The old claim this docstring used to make — that
    ``t1_addr.*`` readers self-reaped on discovery — died with
    ``T1LeasePublisher``/``ServiceRegistry(tier="t1")``, retired
    nexus-8zfwv 2026-08-07; that format is gone, not merely unreaped.)

    A marker is orphaned when the ``mcp_pid`` embedded in its filename
    names no live process (``nexus.session._is_pid_alive``, the same
    ``os.kill(pid, 0)`` idiom the shared daemon substrate uses elsewhere).
    Only orphans are reaped (unlinked); a marker whose ``mcp_pid`` IS alive
    is left completely untouched — the owner may simply be slow to tick.
    A filename whose suffix does not parse as a plain integer pid is never
    guessed at or deleted (fail-safe), only surfaced.

    PID-reuse tradeoff (same one ``sweep_dead_t1_holders`` /
    ``sweep_dead_t1_elect_locks`` document): a marker whose dead
    ``mcp_pid`` was recycled to an unrelated live process reads as
    "alive" and is never reaped here — accepted, since the watcher's
    staleness check makes such a marker inert and the litter is one file.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.daemon.t1_handoff import (  # noqa: PLC0415 — deferred to avoid circular import
        _CLAIMED_MARKER_PREFIX,
        _HANDOFF_MARKER_PREFIX,
    )
    from nexus.session import _is_pid_alive  # noqa: PLC0415 — deferred to avoid circular import

    config_dir = nexus_config_dir()
    if not config_dir.exists():
        return [HealthResult(
            label="T1 handoff markers", ok=True, detail="no nexus config dir",
        )]

    markers = list(config_dir.glob(f"{_HANDOFF_MARKER_PREFIX}*"))
    if not markers:
        return [HealthResult(
            label="T1 handoff markers", ok=True, detail="no handoff markers",
        )]

    reaped: list[str] = []
    live: list[str] = []
    unparseable: list[str] = []
    for path in markers:
        name = path.name
        # The claimed variant's prefix ("t1_handoff.claimed.") is itself
        # prefixed by the live variant's ("t1_handoff."), so it must be
        # checked FIRST or the pid slice below would include "claimed.".
        if name.startswith(_CLAIMED_MARKER_PREFIX):
            pid_str = name[len(_CLAIMED_MARKER_PREFIX):]
        else:
            pid_str = name[len(_HANDOFF_MARKER_PREFIX):]
        try:
            mcp_pid = int(pid_str)
        except ValueError:
            # Fail-safe: an unparseable suffix is never guessed at or
            # deleted, only surfaced as an oddity.
            unparseable.append(name)
            continue
        if _is_pid_alive(mcp_pid):
            live.append(name)
            continue
        try:
            path.unlink()
            reaped.append(name)
        except OSError as exc:
            _log.debug("t1_handoff_orphan_reap_failed", path=str(path), error=str(exc))
            unparseable.append(name)

    parts: list[str] = []
    if reaped:
        parts.append(f"reaped {len(reaped)} orphaned marker(s): {', '.join(sorted(reaped))}")
    if live:
        parts.append(f"{len(live)} live marker(s) untouched")
    if unparseable:
        parts.append(
            f"{len(unparseable)} unparseable/unreapable marker(s): "
            f"{', '.join(sorted(unparseable))}"
        )
    if not parts:
        parts.append("no handoff markers")

    return [HealthResult(
        label="T1 handoff markers",
        ok=not unparseable,
        detail="; ".join(parts),
        fix_suggestions=(
            ["Inspect/remove manually: ls ~/.config/nexus/t1_handoff.*"]
            if unparseable else []
        ),
    )]


def _check_orphan_checkpoints() -> list[HealthResult]:
    from nexus.checkpoint import CHECKPOINT_DIR, scan_orphaned_checkpoints  # noqa: PLC0415 — deferred to avoid circular import

    if not CHECKPOINT_DIR.exists():
        return [HealthResult(label="PDF checkpoints", ok=True, detail="no checkpoint directory")]

    try:
        orphans = scan_orphaned_checkpoints(delete=False)
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        _log.debug("orphan_checkpoint_scan_failed", error=str(exc))
        return [HealthResult(label="PDF checkpoints", ok=True, detail="scan failed — skipping")]

    total = len(list(CHECKPOINT_DIR.glob("*.json")))
    if orphans:
        return [HealthResult(
            label="PDF checkpoints",
            ok=False,
            detail=f"{len(orphans)} orphaned checkpoint(s) out of {total} total",
            fix_suggestions=["Remove stale checkpoints: nx doctor --clean-checkpoints"],
        )]

    return [HealthResult(
        label="PDF checkpoints", ok=True,
        detail=f"{total} checkpoint(s), none orphaned" if total else "no checkpoints",
    )]


def _check_orphan_pipelines() -> list[HealthResult]:
    from nexus.db.http_pipeline_client import HttpPipelineDB  # noqa: PLC0415 — deferred to avoid circular import

    try:
        with HttpPipelineDB() as db:
            orphans = db.scan_orphaned_pipelines(delete=False)
            total = db.count_pipelines()
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        _log.debug("orphan_pipeline_scan_failed", error=str(exc))
        return [HealthResult(label="PDF pipeline buffer", ok=True, detail="scan failed — skipping")]

    if orphans:
        return [HealthResult(
            label="PDF pipeline buffer",
            ok=False,
            detail=f"{len(orphans)} orphaned entry/entries out of {total} total",
            fix_suggestions=["Remove stale entries: nx doctor --clean-pipelines"],
        )]

    return [HealthResult(
        label="PDF pipeline buffer", ok=True,
        detail=f"{total} entry/entries, none orphaned" if total else "empty",
    )]


def _check_mineru_server() -> list[HealthResult]:
    """nexus-far1c: report whether math PDFs will GET a MinerU server —
    not whether one happens to be running at this instant.

    nexus-h1jk wrote this check against the pre-nexus-1qdb9 model, in
    which MinerU was a long-lived server the operator started by hand
    and kept up; "not reachable" therefore meant "the in-process
    subprocess fallback will run, and math PDFs will OOM". nexus-1qdb9
    made the server spawn ON DEMAND during extraction
    (``ensure_mineru_running``) and this check was never reconciled with
    it. A correctly-idle server rendered a red ✗ asserting a fallback
    that would never be taken. nexus-9xfx5 patched the fresh-install
    symptom (unprovisioned → no row) without touching the model, so the
    class survived on every box that had ever provisioned a server —
    including, via a pre-nexus-oa7r ephemeral port fossilised in
    ``pdf.mineru_server_url``, boxes whose on-demand path was healthy.

    What actually decides the OOM risk is whether a server will be there
    when a math PDF arrives: a live one, OR a spawn this box is
    permitted and equipped to perform. Only when NEITHER holds is the
    fallback claim true, and only then is this a failure.
    """
    from nexus.config import get_mineru_server_url, mineru_server_provisioned  # noqa: PLC0415 — heavy/optional dependency deferred to call time
    import httpx as _httpx  # noqa: PLC0415 — heavy/optional dependency deferred to call time

    try:
        provisioned = mineru_server_provisioned()
        url = get_mineru_server_url()
    except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return []
    if not url:
        return []

    # A provisioned URL is worth probing: either a live pid-file server or
    # explicit operator intent (RDR-148 Gap 1) points at it. An UP-but-sick
    # server is a real failure no on-demand spawn will paper over, because
    # the election short-circuits on a live pid.
    if provisioned:
        try:
            resp = _httpx.get(f"{url}/health", timeout=2.0)
        except (_httpx.ConnectError, _httpx.TimeoutException):
            pass  # not up — fall through to the on-demand assessment
        else:
            if resp.status_code == 200:
                return [HealthResult(
                    label="MinerU server", ok=True,
                    detail=f"reachable at {url}",
                )]
            return [HealthResult(
                label="MinerU server", ok=False,
                detail=f"{url} returned HTTP {resp.status_code}",
                fix_suggestions=["Restart the server: nx mineru stop && nx mineru start"],
            )]

    # Nothing is up. The question is no longer "is it reachable" but "can
    # one come up" — the two conditions ensure_mineru_running() itself
    # gates on, asked without spawning anything.
    try:
        from nexus._mineru_spawn import _resolve_mineru_api_bin  # noqa: PLC0415 — deferred; lower layer, per nexus-8g79.10
        from nexus.daemon.mineru_lifecycle import spawn_policy_allows  # noqa: PLC0415 — deferred

        binary = _resolve_mineru_api_bin()
        may_spawn = spawn_policy_allows(url)
    except Exception:  # noqa: BLE001 — never let the health probe raise
        return []

    if not provisioned:
        # Nothing up, nothing configured. Whether the spawn path is armed is
        # real information, but emitting it here would add a row to every
        # `conexus[local]` install and change nexus-9xfx5's fresh-install
        # contract (and the nexus-nolqs virgin-journey gate) for a case that
        # was never broken. Deliberately still silent; see nexus-far1c for
        # the "armed and idle is indistinguishable from absent" follow-up.
        return []

    if binary is not None and may_spawn:
        # The on-demand path is armed. Idle is the correct steady state for
        # this subsystem — NOT a failure, which is the whole of nexus-far1c.
        return [HealthResult(
            label="MinerU server", ok=True,
            detail="not running; spawns on demand during extraction",
        )]

    if binary is None:
        # Operator pointed at something and it is not answering, and no
        # local spawn can cover for it.
        return [HealthResult(
            label="MinerU server", ok=False,
            detail=(
                f"{url} unreachable and mineru-api is not installed here; "
                "math PDFs fall back to the in-process subprocess (OOM-risk)"
            ),
            fix_suggestions=[
                # nexus-pffc4: layout-aware — the uv form rebuilds over the
                # shims on a generation box; route through install_advice.
                *_install_advice().local_extra_advice(),
                f"Or point pdf.mineru_server_url at a reachable server "
                f"(currently: {url})",
            ],
        )]

    # Binary present, spawn refused by policy — the fallback claim is true,
    # and the operator asked for it. Name the reason rather than the symptom.
    from nexus.config import get_pdf_config  # noqa: PLC0415 — deferred

    env_override = os.environ.get("NX_MINERU_AUTOSTART", "").strip()
    if env_override:
        reason = f"autostart disabled by NX_MINERU_AUTOSTART={env_override!r}"
    elif not get_pdf_config().mineru_autostart:
        reason = "autostart disabled by pdf.mineru_autostart: false"
    else:
        reason = f"{url} is remote — a local spawn must not shadow it"
    return [HealthResult(
        label="MinerU server", ok=False,
        detail=(
            f"not running and will not autostart ({reason}); math PDFs "
            "fall back to the in-process subprocess (OOM-risk)"
        ),
        fix_suggestions=[
            "Start it explicitly: nx mineru start",
            "Or re-enable on-demand spawn: pdf.mineru_autostart: true",
        ],
    )]


#: Label for the ported T2 schema-fingerprint check (nexus-ay18d). Deliberately
#: DISTINCT from doctor.py's opt-in "T2 schema check" (`--check-schema`,
#: nexus-vl8lk) — same underlying probe (:func:`probe_t2_schema_fingerprint`),
#: different consumer: this one runs UNCONDITIONALLY in every `nx doctor`
#: sweep (cheap, single HTTP call), the flag is a deliberate, verbose,
#: exit-code-bearing report an operator asks for explicitly. Mirrors the
#: `_CHASH_CONFORMANCE_REPORT_LABEL` precedent (RDR-180, nexus-du2dw) for
#: keeping two call sites of the same probe honestly distinguishable.
_T2_SCHEMA_LABEL = "T2 schema applied"

#: Label for the frozen-migration-source advisory (nexus-ay18d). Purely
#: informational — never gates `nx doctor`'s exit code — emitted only when
#: the file is present (mirrors ``_check_engine_convergence``'s "return []
#: when not applicable" convention: silence, not a result, when absent).
_LEGACY_T2_SOURCE_LABEL = "legacy T2 migration source"


@dataclass(frozen=True)
class T2SchemaFingerprint:
    """Result of probing the engine's ``GET /version`` for the Liquibase
    changelog fingerprint (bead nexus-ay18d / nexus-vl8lk).

    ``reachable=False`` means the service endpoint could not be resolved or
    ``/version`` could not be reached/parsed — :attr:`unreachable_detail`
    carries the cause. ``reachable=True, reported=False`` means the endpoint
    answered but carried NONE of ``schema_latest_id`` / ``schema_changeset_count``
    / ``schema_error`` — the documented managed/cloud omission-by-design
    (``nexus.db.managed_endpoint`` module docstring: "schema_latest_id /
    schema_changeset_count / schema_error remain absent on the managed
    endpoint BY DESIGN"), or an engine predating the nexus-pebfx.4 fields.
    Distinguishing "key absent" from "key present but null" (rather than a
    blanket ``.get()``) is the same lesson nexus-vw594 F3 named for
    ``index_state_reported``: an absent key is UNKNOWN, not evidence of
    anything, and must never collapse into the same branch as a populated
    field reading zero.
    """

    reachable: bool
    reported: bool
    latest_id: str | None = None
    changeset_count: int | None = None
    schema_error: str | None = None
    unreachable_detail: str = ""


def probe_t2_schema_fingerprint(
    *,
    base_url: str | None = None,
    timeout: float = 5.0,
    http_get: Callable[[str, float], object] | None = None,
) -> T2SchemaFingerprint:
    """Ask the engine's already-existing ``GET /version`` (Java
    ``VersionHandler``) for the applied Liquibase changelog fingerprint.

    PORT decision (nexus-ay18d / nexus-vl8lk): both beads' own text names
    "Liquibase changeset state" as the honest Postgres-side answer to what
    the retired SQLite ``PRAGMA integrity_check`` used to ask. The route
    already ships on every engine at the current floor
    (``REQUIRED_ENGINE_VERSION``, well past nexus-pebfx.4, which introduced
    these fields) — no new engine route was added for this fix.

    Shared by TWO consumers, one probe: :func:`_check_t2_schema_applied`
    (this module's always-on ``nx doctor`` sweep, terse ``HealthResult``)
    and ``nexus.commands.doctor._run_check_schema`` (the opt-in
    ``--check-schema`` verbose report with exit codes). Keeping the HTTP
    call + absent-vs-null interpretation in ONE place means the two
    consumers can never independently drift on what "schema applied" means.

    Args:
        base_url: Override the resolved service base URL (test injection;
            mirrors :func:`nexus.db.managed_endpoint.probe_managed_service`'s
            own ``base_url`` param). ``None`` resolves via
            :func:`nexus.db.service_endpoint.resolve_service_endpoint`.
        timeout: HTTP timeout in seconds.
        http_get: Injectable ``(url, timeout) -> httpx.Response`` callable
            (test injection). ``None`` uses ``httpx.get``.

    Never raises — any resolution/transport/parse failure degrades to
    ``reachable=False`` so BOTH callers can render an honest warning instead
    of crashing `nx doctor` (the "WARN, never ok=True, for ANY exception
    here" lesson from nexus-du2dw's chash-conformance-report review).
    """
    try:
        import httpx  # noqa: PLC0415 — heavy/optional dependency deferred to call time

        resolved_base_url = base_url
        if resolved_base_url is None:
            from nexus.db.service_endpoint import resolve_service_endpoint  # noqa: PLC0415 — deferred to avoid circular import

            resolved_base_url, _token = resolve_service_endpoint()

        get = http_get if http_get is not None else (
            lambda u, t: httpx.get(u, timeout=t)
        )
        resp = get(f"{resolved_base_url.rstrip('/')}/version", timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — boundary fallback — must degrade, never crash `nx doctor`
        return T2SchemaFingerprint(reachable=False, reported=False, unreachable_detail=str(exc))

    if not isinstance(body, dict):
        return T2SchemaFingerprint(reachable=False, reported=False, unreachable_detail=f"/version returned non-object body: {body!r}")

    reported = any(k in body for k in ("schema_latest_id", "schema_changeset_count", "schema_error"))
    if not reported:
        return T2SchemaFingerprint(reachable=True, reported=False)

    count_raw = body.get("schema_changeset_count")
    count = int(count_raw) if isinstance(count_raw, (int, float)) and not isinstance(count_raw, bool) else None
    return T2SchemaFingerprint(
        reachable=True,
        reported=True,
        latest_id=body.get("schema_latest_id"),
        changeset_count=count,
        schema_error=body.get("schema_error"),
    )


def _check_t2_schema_applied() -> list[HealthResult]:
    """T2 schema-applied check (nexus-ay18d), PORTED off the retired SQLite
    ``PRAGMA integrity_check`` / FTS5-rebuild probe.

    WHY THE OLD CHECK DIED (nexus-ay18d): ``_check_t2_integrity`` opened
    ``default_db_path()`` — the frozen SQLite migration source, per RDR-158
    P4 never the live store in ANY mode since T2 moved to Postgres — with
    no service-mode branch. On a legacy file present it validated a fossil
    under the "T2 integrity" label; on a fresh PG-only box (the file never
    exists) it returned ``ok=True, detail="not created yet"`` having
    examined nothing — a clean pass that measured nothing, permanently
    vacuous on every supported install shape (WAVE-2 finding, bead notes).

    WHY THIS IS A PORT, NOT A RETIRE-WITH-NO-REPLACEMENT: Postgres has no
    lightweight client-observable equivalent to SQLite's ``PRAGMA
    integrity_check`` (that would need ``pg_amcheck`` wired through a NEW
    engine route — explicitly out of scope for this fix; adding a route is
    option (c) DEFER-LOUD, not needed here). But the bead's own suggested
    fix — "Postgres has its own answer - Liquibase changeset state" — is
    already served by the engine's existing ``GET /version`` handshake
    (:func:`probe_t2_schema_fingerprint`), so this check now asks THAT: is
    the schema Liquibase actually applied readable and non-empty. This is a
    narrower question than on-disk corruption, but it is a REAL,
    non-vacuous answer sourced from the live store, which is strictly more
    honest than the false attribution the old check produced.

    Capability-honest degrade: unreachable engine -> soft WARN (never
    silent ok=True); a managed/cloud endpoint that withholds the fingerprint
    by design -> ok=True, explicitly labelled "not exposed", never
    conflated with "checked and healthy".

    Legacy-file advisory: DECOUPLED from the verdict above. When
    ``default_db_path()`` still exists on disk (a migration-era install
    that has not been cleaned up), this reports a SEPARATE, purely
    informational entry under :data:`_LEGACY_T2_SOURCE_LABEL` — always
    ``ok=True`` — naming it a frozen rollback artifact, never claiming
    anything about the live store. No file present -> no entry at all
    (mirrors ``_check_engine_convergence``'s "not applicable" convention).
    """
    results: list[HealthResult] = []
    label = _T2_SCHEMA_LABEL

    fp = probe_t2_schema_fingerprint()
    if not fp.reachable:
        results.append(HealthResult(
            label=label,
            ok=False,
            warn=True,
            detail=(
                f"SKIPPED (T2 engine unreachable: {fp.unreachable_detail}) — "
                "see 'Storage service health' above for the underlying cause."
            ),
        ))
    elif not fp.reported:
        results.append(HealthResult(
            label=label,
            ok=True,
            detail=(
                "not exposed by this endpoint (managed/cloud service "
                "withholds the schema fingerprint by design, or the engine "
                "predates the /version schema fields)"
            ),
        ))
    elif fp.schema_error:
        results.append(HealthResult(
            label=label, ok=False,
            detail=f"engine reported schema_error: {fp.schema_error}",
        ))
    elif not fp.changeset_count:
        # Non-vacuity (nexus-kmo9h class): an engine that answers but
        # reports zero applied changesets is not a healthy schema state,
        # even though it is not a transport/read error either.
        results.append(HealthResult(
            label=label, ok=False,
            detail=f"schema_changeset_count={fp.changeset_count!r} — Liquibase applied nothing",
        ))
    else:
        results.append(HealthResult(
            label=label, ok=True,
            detail=f"{fp.changeset_count} changeset(s) applied, latest={fp.latest_id}",
        ))

    db_path = default_db_path()
    if db_path.exists():
        results.append(HealthResult(
            label=_LEGACY_T2_SOURCE_LABEL,
            ok=True,
            detail=(
                f"pre-migration SQLite file present at {db_path} — a relic: "
                "nothing reads it and there is no path back to it (Hal, "
                "2026-08-29); T2 is Postgres in every mode since RDR-158 P4. "
                "Delete it when convenient."
            ),
        ))

    return results


#: Hook names for LIVE producers of nexus.dropped_writes.record_drop as of
#: nexus-gjv9b PARTs 1/2 — a drop from one of these is CURRENT evidence a
#: best-effort write to the engine is failing, not RDR-187 chash-hook
#: history. Keep in lockstep with the ``hook=`` value each producer passes
#: (``_session_end_census._post_capability_census``,
#: ``routing/_lib.py``'s ``_record_dropped_routing_event``).
_LIVE_DROP_PRODUCER_HOOKS = frozenset({"capability_census", "routing_events"})


def _check_t2_dropped_writes() -> list[HealthResult]:
    """Surface the dropped-best-effort-write meter (RDR-129 B4, nexus-uq8a4).

    RDR-187 (nexus-piwya.4) retired the meter's FOUNDING producer — the
    chash dual-write hook — but nexus-gjv9b PARTs 1/2 gave it two LIVE
    ones (see :data:`_LIVE_DROP_PRODUCER_HOOKS`): the capability_census
    and routing_events writer swaps both degrade here on service-down.
    Framing keys on :attr:`DropSummary.recent_last_hook` — a WINDOWED
    field (:func:`nexus.dropped_writes.count_drops`'s ``recent_hours``,
    24h default), NOT the lifetime ``last_hook`` (review fold-in,
    critique CRITICAL 2): a live producer is expected to have OCCASIONAL
    drops during a real outage, and once the window passes with no new
    ones, this check must stop soft-WARNing — a decision keyed on the
    lifetime field would soft-WARN forever over one drop from months ago,
    exactly the "permanent false alarm" class nexus-piwya.9 already
    retired the founding chash-hook alarm to avoid re-introducing here.

    - ``recent_last_hook`` in :data:`_LIVE_DROP_PRODUCER_HOOKS`: soft-WARN
      (``ok=False``) — a live producer dropping WITHIN THE WINDOW IS
      current evidence of a best-effort write actually failing (service
      down, or an old engine missing the route), the exact posture
      RDR-129 B4 restores for a future adopter.
    - Anything else (empty, or the retired chash hook's historical
      value, or a live producer's drop that has AGED OUT of the window):
      HISTORICAL framing, ``ok=True`` — the lifetime ``total`` stays
      visible in the detail either way, audit visibility never shrinks.
    """
    from nexus.dropped_writes import count_drops  # noqa: PLC0415 — deferred to avoid circular import

    try:
        summary = count_drops()
    except Exception as exc:  # pragma: no cover — defensive  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return [HealthResult(
            label="T2 best-effort writes", ok=True, detail=f"meter unavailable: {exc}",
        )]

    if summary.total == 0:
        return [HealthResult(
            label="T2 best-effort writes", ok=True, detail="no drops recorded",
        )]

    if summary.recent_last_hook in _LIVE_DROP_PRODUCER_HOOKS:
        # nexus-gjv9b review fold-in round 3, code-review item 1: a window
        # made ENTIRELY of guard_refused drops is an un-opted-in dev
        # checkout's production-write guard correctly protecting itself
        # (nexus-a2qhz) on every SessionEnd — not evidence the engine or a
        # live producer is failing. This must never render as the same
        # "the engine is failing" WARN a real service-down/auth/timeout
        # episode gets; a SINGLE non-guard-refused drop in the window still
        # takes the WARN path below (recent_all_guard_refused is
        # deliberately all-or-nothing, not "mostly").
        if summary.recent_all_guard_refused:
            detail = (
                f"{summary.recent_total} drop(s) in the last 24h, all refused by "
                "the production-write guard (this process is an un-opted-in dev "
                "checkout — nexus-a2qhz; expected, not evidence of a failing "
                f"engine) ({summary.total} lifetime, {summary.rows} rows)"
            )
            if summary.last_ts:
                detail += f", last {summary.last_ts}"
            return [HealthResult(label="T2 best-effort writes", ok=True, detail=detail)]

        # nexus-gjv9b review fold-in round 4: a window whose OTHER causes
        # are all route_absent (a plugin cut shipping this hook ahead of
        # the paired engine tag -- every decision 404s until the engine
        # catches up), alone or mixed with guard_refused, is version skew,
        # not a failing service. recent_all_benign is a strict superset of
        # recent_all_guard_refused (checked above) -- reaching here means
        # the window is NOT all guard_refused, so an info framing here
        # implies at least one route_absent drop is present. A SINGLE
        # cause outside {guard_refused, route_absent} still falls through
        # to the real WARN below -- same all-or-nothing discipline.
        if summary.recent_all_benign:
            detail = (
                f"{summary.recent_total} drop(s) in the last 24h, most recently "
                f"from {summary.recent_last_hook!r} — engine behind the client; "
                f"{summary.recent_last_hook!r} not yet served "
                f"({summary.total} lifetime, {summary.rows} rows)"
            )
            if summary.last_ts:
                detail += f", last {summary.last_ts}"
            return [HealthResult(label="T2 best-effort writes", ok=True, detail=detail)]

        detail = (
            f"{summary.recent_total} drop(s) in the last 24h "
            f"({summary.total} lifetime, {summary.rows} rows), most recently "
            f"from {summary.recent_last_hook!r} — a best-effort write to the "
            f"engine is failing (service down, or an old engine missing the route)"
        )
        # nexus-gjv9b review fold-in round 3, critique CRITICAL 2: name the
        # DOMINANT cause and its share of the window, not just a bare count
        # -- "3 drops in the last 24h" reads identically whether that is
        # three unrelated connection blips (self-resolving, likely nothing
        # to do) or three consecutive 401s on the same broken credential
        # (structural, will keep recurring after this window ages out too).
        # An auth cause (401/403) never gets a softer word than "cause";
        # this is deliberately the same sentence shape as any other cause.
        if summary.recent_dominant_cause:
            detail += (
                f" — dominant cause: {summary.recent_dominant_cause!r} "
                f"({summary.recent_dominant_cause_count}/{summary.recent_total} in window)"
            )
        if summary.last_ts:
            detail += f", last {summary.last_ts}"
        return [HealthResult(label="T2 best-effort writes", ok=False, detail=detail)]

    detail = (
        f"{summary.total} historical drop(s) "
        f"({summary.rows} rows)"
        + (
            f" from the retired chash dual-write hook (writer retired by RDR-187; count frozen)"
            if summary.last_hook not in _LIVE_DROP_PRODUCER_HOOKS
            else f" from {summary.last_hook!r} (outside the 24h recency window — not currently failing)"
        )
    )
    if summary.last_ts:
        detail += f", last {summary.last_ts}"
    return [HealthResult(
        label="T2 best-effort writes",
        ok=True,
        detail=detail,
    )]


# NO _check_t2_daemon_singleton: RDR-129 A3 made a residual two-daemon
# violation observable, and its subject (the T2 daemon) is retired
# (nexus-i711w Stage 2 sub-stage B). With no daemon the census can only ever
# report zero, and its fix_suggestions named `nx daemon t2 stop` /
# `ensure-running`, both gone. The single-writer invariant it guarded now
# belongs to Postgres, not to a pid count.


def _check_catalog(cat: "CatalogReader | None", cat_path: "Path") -> list[HealthResult]:
    try:
        if cat is not None:
            # nexus-qnp5s: use cat.stats() which works on both SQLite Catalog
            # and HttpCatalogClient (GET /v1/catalog/stats).
            s = cat.stats()
            doc_count = s.get("doc_count", 0)
            link_count = s.get("link_count", 0)
            return [HealthResult(
                label="Catalog", ok=True,
                detail=f"{doc_count} documents, {link_count} links at {cat_path}",
            )]
        return [HealthResult(
            label="Catalog", ok=True,
            detail="not initialized (optional — run: nx catalog setup)",
        )]
    except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return [HealthResult(label="Catalog", ok=True, detail="check failed (non-critical)")]


# ── Orchestrator ──────────────────────────────────────────────────────────────


def _check_plugin_name() -> list[HealthResult]:
    """nexus-mkj6u: warn when the installed Claude Code plugin's name
    differs from what the CLI expects.

    The 2026-05-23 rename moved the plugin name from ``nx`` to
    ``conexus``. Migration is two Claude Code commands: ``/plugin
    install conexus@nexus-plugins`` to register the new plugin,
    then ``/reload-plugins`` to activate it. Until both run, the
    user is running the NEW conexus CLI under the OLD ``nx`` plugin
    install at ``~/.claude/plugins/cache/nexus-plugins/nx/...``.
    The MCP-server-startup check fires once per session; this
    doctor check is the explicit-invocation surface for users who
    run ``nx doctor`` to diagnose what's stale.

    Non-fatal. Returns an empty list when no ``CLAUDE_PLUGIN_ROOT``
    is set (CLI-only use; nothing to check) or when the plugin name
    matches.
    """
    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not plugin_root:
        return []
    manifest_path = Path(plugin_root) / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return []
    try:
        manifest = json.loads(manifest_path.read_text())
        plugin_name = manifest.get("name")
    except (OSError, json.JSONDecodeError):
        return []
    if not plugin_name:
        return []

    from nexus.mcp_infra import EXPECTED_PLUGIN_NAME  # noqa: PLC0415 — deferred to avoid circular import
    if plugin_name == EXPECTED_PLUGIN_NAME:
        return []

    return [
        HealthResult(
            label="Claude Code plugin name (renamed)",
            ok=False,
            detail=(
                f"installed plugin is '{plugin_name}@nexus-plugins'; CLI "
                f"expects '{EXPECTED_PLUGIN_NAME}@nexus-plugins' "
                "(renamed 2026-05-23, nexus-mkj6u)"
            ),
            fix_suggestions=[
                "/plugin install conexus@nexus-plugins",
                "/reload-plugins",
                "(both run in Claude Code; install registers the new plugin, reload activates it)",
            ],
            fatal=False,
        )
    ]


def _check_credential_persistence() -> list[HealthResult]:
    """nexus-m7evs: warn when cloud credentials live in shell env only.

    GUI-spawned ``nx-mcp`` (Claude Desktop, Cowork SDK bridge) inherits
    launchd's environment, NOT the user's interactive shell. If
    ``VOYAGE_API_KEY`` is in ``.zshrc`` exports but never persisted via
    ``nx config set``, the GUI-spawned subprocess sees it as absent.

    nexus-sghyo (2026-08-06) CORRECTED CLAIM: this docstring previously
    said the gap flips ``is_local_mode()`` to True and breaks T3
    dispatch — FALSE since RDR-155 (``is_local_mode()`` never reads this
    key; see nexus-nmw3i below, which already fixed the mode-detection
    half of this same false premise). The key is no longer a client
    embedding credential at all (Hal determination 2026-07-28: "we do no
    embedding on the client") — it is an OPTIONAL engine-bound setting:
    a locally-spawned engine plumbs it through
    (``daemon/storage_service_daemon.py``) to run in voyage mode instead
    of the local bge-768 default. The REAL consequence of the env-only
    gap: a GUI-spawned engine-provisioning path sees the key as absent
    and the locally-spawned engine silently defaults to bge-768 instead
    of the voyage mode the operator intended — never a mode misdetection.

    This check runs on the CLI side (where shell env IS visible) and
    surfaces the gap before the GUI-spawn path hits it. Non-fatal: a
    warning, not a blocker, because the CLI itself works fine.

    Returns an empty list when the configuration is consistent (both
    persisted, neither set, or no env exports).
    """
    from nexus.config import _global_config_path  # noqa: PLC0415 — deferred to avoid circular import

    cloud_keys = ("voyage_api_key",)
    env_names = {
        "voyage_api_key": "VOYAGE_API_KEY",
    }

    # Read config.yml directly; we want to see file state independent of env.
    file_creds: dict[str, str] = {}
    cfg_path = _global_config_path()
    if cfg_path.exists():
        try:
            import yaml  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
            data = yaml.safe_load(cfg_path.read_text()) or {}
            file_creds = data.get("credentials", {}) or {}
        except Exception:  # noqa: BLE001 — creds-file read is best-effort; fall back to empty mapping
            file_creds = {}

    # nexus-nmw3i (the "present as shell-env-only" false-flag, critic
    # Critical): the misdetection premise of this check is that a
    # GUI-spawned process, missing the shell-only cloud creds, flips
    # is_local_mode() to True. But is_local_mode() checks service_url
    # FIRST — when service_url is PERSISTED to config.yml (every migrated
    # install), the GUI spawn resolves the mode identically with or
    # without the shell creds, and shell-only legacy creds are
    # migration-source config, not a mode anchor. No gap to warn about.
    if str(file_creds.get("service_url", "")).strip():
        return []


    env_only: list[str] = []
    for key in cloud_keys:
        env_present = bool(os.environ.get(env_names[key], "").strip())
        file_present = bool(str(file_creds.get(key, "")).strip())
        if env_present and not file_present:
            env_only.append(key)

    if not env_only:
        return []

    suggestions = [f"nx config set {key} \"${env_names[key]}\"" for key in env_only]
    suggestions.append(
        "Then quit and relaunch Claude Desktop so the next nx-mcp "
        "spawn reads ~/.config/nexus/config.yml instead of empty env."
    )

    detail = (
        f"{len(env_only)} credential(s) in shell env only: {', '.join(env_only)}. "
        "GUI-spawned consumers (Claude Desktop, Cowork) cannot see "
        "shell env vars — a locally-spawned engine will silently default "
        "to bge-768 instead of voyage mode."
    )

    return [
        HealthResult(
            label="Credential persistence (GUI spawn)",
            ok=False,
            detail=detail,
            fix_suggestions=suggestions,
            fatal=False,
        )
    ]


def _check_mint_token() -> list[HealthResult]:
    """RDR-005 2a self-minting credential (nexus-wrwb7).

    Reports ``mint_token`` presence and, when configured, performs ONE live
    mint round trip via ``DataTokenManager`` to confirm the credential and
    endpoint are usable. Degrades cleanly -- never false-clean, never
    crashes ``nx doctor``:

      - unconfigured: a loud (visible, ``ok=True``) skip line -- self-
        minting is optional; the static ``service_token`` path runs
        unchanged for every install that has not opted in.
      - configured but the engine endpoint is not resolvable: soft warning
        (non-fatal), never silently "ok".
      - configured but the mint round trip itself fails
        (``DataTokenMintError``): soft warning naming the failure.
      - configured and reachable: reports whether the round trip MINTED a
        fresh token or REUSED the manager's cached live one, plus the
        granted TTL (critic S1/S3, nexus-ssqk9). IMPORTANT: on the managed
        CLOUD path, pre-cutover, the edge strips client ``Authorization``
        and injects its own credential (RDR-005 2a staged cutover -- T2
        ``conexus/conexus-06`` answer, 2026-08-15). A mint succeeding
        THROUGH the edge does not yet prove THIS credential's own
        authority, so the success line is worded neutrally rather than
        claiming credential-specific proof.
    """
    from nexus.config import get_credential  # noqa: PLC0415 — deferred to avoid circular import

    label = "Data-token self-minting (mint_token)"
    credential = (get_credential("mint_token") or "").strip()
    if not credential:
        return [HealthResult(
            label=label,
            ok=True,
            detail="not configured — self-minting inactive; the static "
                   "service_token path is used unchanged (optional, RDR-005 2a)",
        )]

    from nexus.db.data_token import DataTokenMintError, get_data_token_manager  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.db.service_endpoint import (  # noqa: PLC0415 — deferred to avoid circular import
        resolve_service_endpoint_with_evidence_gate,
    )

    try:
        base_url, _static_token = resolve_service_endpoint_with_evidence_gate()
    except Exception as exc:  # noqa: BLE001 — best-effort: endpoint unresolvable degrades to a warning
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"mint_token is configured but the engine endpoint is not resolvable: {exc}",
        )]

    import urllib.parse  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    host = urllib.parse.urlsplit(base_url).netloc or base_url
    tenant = "default"
    # critic S3 (nexus-ssqk9, residue class nexus-lgiqw): the PROCESS-WIDE
    # singleton, never a throwaway DataTokenManager() -- constructing a
    # fresh manager per invocation minted a NEW token every single `nx
    # doctor` run, defeating the manager's own residue discipline. Peek
    # BEFORE calling bearer_for so the success line can say which happened.
    manager = get_data_token_manager()
    was_live = manager.has_live_token(base_url, tenant)
    # nexus-9c7t9: a SEPARATE peek at the cross-process lease file -- only
    # meaningful when there is no in-process hit, since an in-process hit
    # never consults the lease file at all (see DataTokenManager.bearer_for).
    # A real subprocess (every `nx doctor` invocation) has an EMPTY
    # in-process cache by construction, so this is what makes "reused"
    # genuinely observable on the CLI now, not just inside one long-lived
    # process.
    had_fresh_lease = (not was_live) and manager.has_fresh_lease(base_url, tenant)
    try:
        manager.bearer_for(base_url, tenant)
    except DataTokenMintError as exc:
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"mint round trip FAILED against {host}: {exc}",
        )]

    if was_live:
        verb = "reused the cached (in-process)"
    elif had_fresh_lease:
        verb = "reused the cached (lease file)"
    else:
        verb = "minted a fresh"
    ttl = manager.granted_ttl_seconds(base_url, tenant)
    ttl_detail = f"granted TTL {ttl:.0f}s" if ttl is not None else "granted TTL unknown"
    return [HealthResult(
        label=label,
        ok=True,
        detail=(
            f"{verb} data token via {host} ({ttl_detail}). Pre-cutover on "
            "the managed cloud path this does NOT yet prove this "
            "credential's own authority (the edge still strips/injects "
            "Authorization until the RDR-005 2a cutover) — treat as "
            "reachability, not proof."
        ),
    )]


# ── RDR-152 / bead nexus-gmiaf.33: storage-service health checks ──────────────

# Authoritative set of tenant tables that MUST have RLS enabled, forced, and at
# least one policy.  Derived from every ``ALTER TABLE ... ENABLE ROW LEVEL
# SECURITY`` statement across all Liquibase changelog baseline files under
# service/src/main/resources/db/changelog/.
#
# STRUCTURAL GUARD: tests/test_health_service_checks.py::TestRlsTableCompleteness
# cross-walks this tuple against the actual XMLs at test time and fails loudly
# on any drift.  When adding a new changelog baseline, run that test to catch
# any newly RLS-protected table that needs to be added here.
_RLS_TENANT_TABLES: tuple[str, ...] = (
    "nexus.aspect_extraction_queue",
    "nexus.aspect_promotion_log",
    # nexus.capability_census: nexus-gjv9b PART 1 (telemetry-010-capability-
    # census.xml), replacing capability_census.jsonl. Per-session UPSERT
    # (tenant_id, session_id) with the usual ENABLE + FORCE + tenant_isolation
    # RLS shape.
    "nexus.capability_census",
    "nexus.catalog_collections",
    "nexus.catalog_document_chunks",
    "nexus.catalog_documents",
    "nexus.catalog_links",
    "nexus.catalog_meta",
    "nexus.catalog_owners",
    # "nexus.chash_index" REMOVED (RDR-187/nexus-piwya.9, .9 review High):
    # the table is dropped, and _check_rls_present LEFT-JOINs this list
    # against live pg_class — a listed-but-dropped table is a PERMANENT
    # false FATAL. (The earlier "likely permanent" note on the bead covered
    # only the XML cross-walk, which reads immutable history; the live
    # check is the consumer that matters. The completeness guard carries a
    # matching dropped-tables exemption.)
    # "nexus.chash_alias" REMOVED (nexus-lgdel.l1, legacy-001-drop-chash-
    # alias.xml): same chash_index precedent — the table is dropped, so it
    # must not be a permanent false FATAL here. The completeness guard's
    # dropped-tables exemption in tests/test_health_service_checks.py
    # carries the matching entry.
    "nexus.chash_remap",
    # nexus.chunks: RDR-191 Phase 4 unify (nexus-o8dil.51). Added in the SAME
    # engine release as vectors-004-unify-chunks.xml, which creates the
    # unified table WITH RLS in the same changeset that drops chunks_384/
    # chunks_768/chunks_1024 (never listed here — see the absent-table
    # handling in _check_rls_present, which is what makes this addition
    # safe rather than a future permanent false FATAL like the chash_index
    # episode above).
    "nexus.chunks",
    "nexus.claude_assisted_remediation_consents",
    "nexus.document_aspects",
    "nexus.document_highlights",
    "nexus.frecency",
    "nexus.gc_audit",
    "nexus.hook_failures",
    # nexus.index_failures: nexus-nukn3, durable per-file index-failure
    # record (telemetry-009-index-failures.xml). Event-log shape, same RLS
    # posture as hook_failures (ENABLE + FORCE + tenant_isolation).
    "nexus.index_failures",
    "nexus.ladder_completions",
    "nexus.memory",
    # "nexus.migration_jobs" REMOVED (nexus-tk070.p5b, reworked
    # 2026-08-20, migration-002-tenant-pk.xml): dead table dropped —
    # MigrationHandler.java / MigrationJobRepository.java deleted at
    # 7bcf29c67, zero producers/consumers, Sam disposition 2026-08-20.
    # Same chash_index/chash_alias precedent above: a listed-but-dropped
    # table is a PERMANENT false FATAL, so it must not stay listed. The
    # completeness guard's dropped-tables exemption in
    # tests/test_health_service_checks.py carries the matching entry.
    "nexus.nx_answer_runs",
    # nexus.nx_answer_steps: RDR-196 .p1c (nexus-nyry9.9,
    # telemetry-007-nx-answer-steps.xml) — per-step cost/quality
    # telemetry, child of nx_answer_runs (FK ON DELETE CASCADE), same
    # RLS shape as the parent (ENABLE + FORCE + tenant_isolation).
    "nexus.nx_answer_steps",
    "nexus.pdf_chunks",
    "nexus.pdf_pages",
    "nexus.pdf_pipeline",
    "nexus.plans",
    "nexus.relevance_log",
    "nexus.retention_markers",
    # nexus.routing_events: nexus-gjv9b PART 2 (telemetry-011-routing-
    # events.xml), replacing routing_log.jsonl. Append-only event log, same
    # RLS shape as relevance_log/search_telemetry.
    "nexus.routing_events",
    "nexus.search_telemetry",
    # nexus.taxonomy_centroids: RDR-191 Phase 4 unify (nexus-o8dil.51/.47 "one
    # era" ruling). Same rationale as nexus.chunks above: added alongside
    # taxonomy-007-unify-centroids.xml, which drops taxonomy_centroids_384/
    # _768/_1024 (never listed here) in the same changeset it grants RLS on
    # the unified table.
    "nexus.taxonomy_centroids",
    "nexus.taxonomy_meta",
    "nexus.tier_writes",
    "nexus.topic_assignments",
    "nexus.topic_links",
    "nexus.topics",
    "t1.scratch",
)

# Scope key published by the Java service supervisor (bead nexus-gmiaf.30).
# The supervisor writes a t2-tier lease record under this key; doctor reads it
# to resolve host:port without hard-coding or requiring env vars.
_STORAGE_SERVICE_SCOPE_KEY: str = "storage_service"

# Sentinel for distinguishing "caller passed None" from "use auto-discovery".
_ENDPOINT_AUTO: object = object()



def _resolve_service_endpoint(
    config_dir: Path,
) -> tuple[str, int] | None:
    """Return (host, port) for the Java storage service, or None.

    Resolution order:
    1. ServiceRegistry discover() — the supervisor (gmiaf.30) publishes a
       lease record under tier="storage_service", scope=str(os.getuid()).
       addr file = storage_service_addr.<uid>.  NOT the t2 tier.
    2. NX_SERVICE_HOST / NX_SERVICE_PORT environment variables (fallback).
    3. None — endpoint not discoverable (soft-warn, skip ping).
    """
    # 1. Registry discover.
    # IMPORTANT: tier="storage_service", scope=str(os.getuid()) — this matches
    # exactly what StorageServiceSupervisor._publish() writes (tier=_REGISTRY_TIER,
    # scope=str(os.getuid())).  The stale comment "t2 tier" drove a bug where
    # this used tier="t2" + scope_key="storage_service" (t2_addr.storage_service),
    # which never matched the supervisor's storage_service_addr.<uid> file.
    try:
        from nexus.daemon.service_registry import ServiceRegistry  # noqa: PLC0415 — deferred to avoid circular import
        registry = ServiceRegistry(dir=config_dir, tier="storage_service")
        scope = str(os.getuid())
        lease = registry.discover(scope)
        if lease is not None:
            ep = lease.endpoint
            host = str(ep.get("host", "127.0.0.1"))
            port = int(ep.get("port", 0))
            if port > 0:
                _log.debug(
                    "storage_service_endpoint_from_registry",
                    host=host, port=port,
                )
                return host, port
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        _log.debug("storage_service_registry_discover_failed", error=str(exc))

    # 2. Env var fallback.
    host = os.environ.get("NX_SERVICE_HOST", "127.0.0.1")
    port_str = os.environ.get("NX_SERVICE_PORT", "").strip()
    if port_str:
        try:
            port = int(port_str)
            if port > 0:
                _log.debug(
                    "storage_service_endpoint_from_env",
                    host=host, port=port,
                )
                return host, port
        except ValueError:
            pass

    return None


def _check_storage_service_health(
    creds_path: Path | None = None,
    endpoint: object = _ENDPOINT_AUTO,  # tuple[str,int] | None | _ENDPOINT_AUTO
    http_get=None,  # injectable for unit tests: (url, timeout) -> httpx.Response
) -> list[HealthResult]:
    """Ping the Java storage service /health endpoint.

    Gated on pg_credentials being present (service mode configured).
    Endpoint resolved via ServiceRegistry → NX_SERVICE_HOST/PORT env →
    soft-warn-and-skip if neither resolves.

    Down service -> fatal (no direct-mode fallback per RDR-152).
    """
    import httpx as _httpx  # noqa: PLC0415 — heavy/optional dependency deferred to call time

    # Resolve creds_path default.
    if creds_path is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred to avoid circular import
        creds_path = nexus_config_dir() / CREDENTIALS_FILENAME

    # Gate: service/PG mode configured?
    if not creds_path.exists():
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid circular import

        if not is_local_mode():
            # nexus-y3wuu: a managed deployment has no local pg_credentials
            # by design — the store operator holds those. Never claim "not
            # configured" for a box that IS configured, just not probeable
            # from here.
            detail = _MANAGED_DEPLOYMENT_SKIP_DETAIL
        else:
            detail = _LOCAL_MODE_NOT_CONFIGURED_DETAIL
        return [HealthResult(
            label="Storage service health",
            ok=False,
            detail=detail,
            warn=True,
        )]

    # Resolve endpoint.
    # _ENDPOINT_AUTO -> auto-discover via registry / env.
    # explicit tuple -> use directly (test injection or caller override).
    # explicit None -> endpoint not available, soft-warn.
    resolved_endpoint: tuple[str, int] | None
    if endpoint is _ENDPOINT_AUTO:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        resolved_endpoint = _resolve_service_endpoint(nexus_config_dir())
    else:
        resolved_endpoint = endpoint  # type: ignore[assignment]

    if resolved_endpoint is None:
        # Soft-warn (not fatal): the service supervisor (gmiaf.30) may not have
        # published its lease yet, or the user simply has not configured service
        # mode.  Either way there is no confirmed endpoint to blame — we cannot
        # distinguish "service not started" from "bead .30 not landed yet".
        # Once an endpoint IS known and the connection is refused, that changes
        # to fatal (we pinged a confirmed address and got nothing back).
        return [HealthResult(
            label="Storage service health",
            ok=False,
            detail=(
                "storage service endpoint not discoverable "
                "(no registry lease and NX_SERVICE_HOST/PORT not set); skipping"
            ),
            warn=True,
        )]

    host, port = resolved_endpoint
    url = f"http://{host}:{port}/health"

    try:
        if http_get is not None:
            resp = http_get(url, timeout=5.0)
        else:
            resp = _httpx.get(url, timeout=5.0)
    except (_httpx.ConnectError, _httpx.TimeoutException, OSError) as exc:
        # Fatal: we have a confirmed endpoint and it is not responding.
        # Unlike the undiscoverable case above, here we know the address and
        # can definitively say the service is down.
        return [HealthResult(
            label="Storage service health",
            ok=False,
            detail=f"Storage service at {url} unreachable: {exc}",
            fix_suggestions=[
                "Start the service: nx service start",
                f"Check that the service is listening on {host}:{port}",
            ],
            fatal=True,
        )]
    except Exception as exc:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return [HealthResult(
            label="Storage service health",
            ok=False,
            detail=f"Storage service health check failed unexpectedly: {exc}",
            fatal=True,
        )]

    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — health-body parse is best-effort; fall back to empty dict
        body = {}

    db_field = body.get("db", "")
    status_ok = resp.status_code == 200 and db_field == "up"

    if status_ok:
        return [HealthResult(
            label="Storage service health",
            ok=True,
            detail=f"Storage service: up (HTTP {resp.status_code}, db={db_field!r})",
        )]

    detail = (
        f"Storage service: DOWN "
        f"(HTTP {resp.status_code}, status={body.get('status','?')!r}, "
        f"db={db_field!r})"
    )
    if "detail" in body:
        detail += f" — {body['detail']}"

    return [HealthResult(
        label="Storage service health",
        ok=False,
        detail=detail,
        fix_suggestions=[
            "Start the service: nx service start",
            f"Check service logs; the DB probe at {host}:{port} is failing",
        ],
        fatal=True,
    )]


def _run_psql(
    psql_bin: Path,
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    sql: str,
    *,
    psql_runner=None,
) -> subprocess.CompletedProcess:
    """Run a single-statement psql query and return the CompletedProcess.

    ``-t -A`` gives unaligned, tuple-only output suitable for line-by-line
    parsing. ``-v ON_ERROR_STOP=1`` makes psql exit non-zero on SQL errors.
    ``psql_runner`` is injectable for unit tests (avoids shelling out).
    """
    cmd = [
        str(psql_bin),
        "-h", host,
        "-p", str(port),
        "-U", user,
        "-d", dbname,
        "-v", "ON_ERROR_STOP=1",
        "-t", "-A",
        "-c", sql,
    ]
    if psql_runner is not None:
        # Injected runner (unit tests) — does not accept env kwarg.
        return psql_runner(cmd, capture_output=True, text=True, check=False)
    # nexus-iytd3 loader guard (GH #1414 era-hop regression, 2026-07-21): the
    # published PG bundles ship psql without an RPATH, so on a minimal Linux
    # base a bare invocation exits 127 (libpq.so.5 unresolvable). pg_provision
    # wraps its own psql calls in _bundle_lib_env; this probe must get the
    # SAME guard — post-fc24123c a probe that cannot run reads as UNKNOWN to
    # the tri-state chash-poison gate and permanently DEFERS engine
    # convergence on exactly the era boxes the unattended upgrade serves.
    from nexus.db.pg_provision import _bundle_lib_env  # noqa: PLC0415 — deferred to avoid circular import

    env = _bundle_lib_env(cmd, None)
    env["PGPASSWORD"] = password
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)


def _check_engine_convergence(config_dir: Path | None = None) -> list[HealthResult]:
    """nexus-cfgo9: backstop for the automatic post-upgrade engine
    convergence pass (:func:`nexus.upgrade_finish.converge_engine`).

    The auto-trigger in :func:`nexus.upgrade_finish.check_version_transition`
    only fires on a conexus PACKAGE version transition; this check gives an
    operator a way to see (and be pointed at fixing) drift at any time via
    plain ``nx doctor``, without waiting for the next package upgrade.
    Framed as CONVERGENCE PENDING, never as a refusal/violation — per the
    ONE-engine model (GH #1402 postmortem), a local engine mismatch is
    something the product fixes, not something the user is blamed for.

    Delegates entirely to :func:`nexus.upgrade_finish.detect_engine_convergence`,
    which is itself internally gated on local service mode + pg_credentials
    being present — not applicable (cloud mode, no local service) yields no
    result, same convention as the other storage-service checks in this
    module. Any probe failure degrades to no result (best-effort, never
    breaks `nx doctor`).
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        config_dir = nexus_config_dir()

    try:
        from nexus.upgrade_finish import detect_engine_convergence  # noqa: PLC0415 — deferred to avoid circular import
        status = detect_engine_convergence(config_dir)
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_engine_convergence_check_failed", error=str(exc))
        return []

    if not status.applicable:
        return []

    req_s = ".".join(str(p) for p in status.required_version)
    if status.converged:
        return [HealthResult(
            label="Engine convergence",
            ok=True,
            detail=f"installed engine v{req_s} matches the release dependency",
        )]

    got_s = (
        ".".join(str(p) for p in status.installed_version)
        if status.installed_version else "unknown"
    )
    return [HealthResult(
        label="Engine convergence",
        ok=False,
        warn=True,
        detail=(
            f"engine convergence pending — installed v{got_s}, release "
            f"dependency v{req_s}"
        ),
        fix_suggestions=[
            "nx daemon restart-stale  # installs the pinned engine and "
            "cycles the service",
        ],
    )]


def _check_t2_launchagent_stray() -> list[HealthResult]:
    """nexus-c0vby (GH #1405 defect 2): backstop for the automatic
    ``unload_stale_t2_launchagent`` finish-pass leg
    (:func:`nexus.upgrade_finish.unload_stale_t2_launchagent`).

    The auto-trigger only fires on a conexus PACKAGE version transition;
    this gives an operator a way to SEE (and be pointed at fixing) a
    stray, endlessly-respawning T2 autostart unit at any time via plain
    ``nx doctor`` — same convention as ``_check_engine_convergence``
    above. Framed as a soft warning (this is benign log noise, not data
    loss), never a hard failure.

    NOT gated on storage mode (nexus-i711w Stage 2 sub-stage B). It used to
    return ``[]`` outside service mode, on the reasoning that in local mode
    "the T2 tier is the live substrate there — nothing stray to report".
    That reasoning died with the daemon: no box of ANY mode can start a T2
    daemon or reinstall the unit, so a surviving unit is stray EVERYWHERE.
    Keeping the gate would have given a SQLite-mode box — the one most
    likely to be carrying a unit — the silent auto-removal in
    :func:`~nexus.upgrade_finish.unload_stale_t2_launchagent` (un-gated in
    the same commit) and zero ``nx doctor`` visibility, which is the inverse
    of the argument that un-gated the removal. The two must agree on scope.

    Silent (``[]``) only when the probe itself fails — best-effort, must
    never break ``nx doctor``.
    """
    try:
        from nexus.commands.daemon import _autostart_unit_installed  # noqa: PLC0415 — deferred, CLI startup cost

        unit_path = _autostart_unit_installed()
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_t2_launchagent_check_failed", error=str(exc))
        return []

    if unit_path is None:
        return [HealthResult(
            label="T2 autostart unit",
            ok=True,
            detail="no stray T2 autostart unit installed",
        )]

    from nexus.upgrade_finish import _T2_AUTOSTART_UNIT_KIND  # noqa: PLC0415 — deferred to avoid circular import

    return [HealthResult(
        label="T2 autostart unit",
        ok=False,
        warn=True,
        detail=(
            f"a T2 autostart unit ({_T2_AUTOSTART_UNIT_KIND}) is installed "
            f"at {unit_path} but the T2 daemon it starts no longer exists — "
            "its OS-level restart policy respawns an immediately-failing "
            "`nx daemon t2 start` indefinitely (log noise)"
        ),
        # ONE suggestion, not two: `nx daemon t2 uninstall --autostart` was
        # the direct removal verb and died with the daemon (sub-stage B), so
        # naming it here sent the operator at a command that now exits
        # "No such command 't2'" — on precisely the pre-retirement-upgrade
        # box this check exists to help. Pinned by
        # test_every_fix_suggestion_names_a_LIVE_verb.
        fix_suggestions=[
            "nx daemon restart-stale  # removes the stray unit (GH #1405)",
        ],
    )]


def _check_service_launchagent_stray() -> list[HealthResult]:
    """nexus-6bmph (RDR-183 residual; GH #1405 defect-3 family): the c0vby
    sibling for the storage-SERVICE autostart unit.

    A ``com.nexus.service`` unit on a NON-local install (managed/cloud mode)
    launches the local engine against a config with no ``pg_credentials`` —
    the process exits immediately and launchd's restart policy respawns it
    every ``ThrottleInterval`` (30s) forever. Live evidence 2026-07-22: a
    cloud-mode box accumulated 810 error lines in one morning from exactly
    this loop. Soft warning naming the removal verb; silent on local mode
    (the unit is legitimate there) and on any probe failure (best-effort,
    must never break ``nx doctor``).
    """
    try:
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid circular import

        if is_local_mode():
            return []

        from nexus.commands.daemon import _service_autostart_unit_installed  # noqa: PLC0415 — deferred, CLI startup cost

        unit_path = _service_autostart_unit_installed()
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_service_launchagent_check_failed", error=str(exc))
        return []

    if unit_path is None:
        return [HealthResult(
            label="Service autostart unit (non-local mode)",
            ok=True,
            detail="no stray storage-service autostart unit installed",
        )]

    return [HealthResult(
        label="Service autostart unit (non-local mode)",
        ok=False,
        warn=True,
        detail=(
            f"a storage-service autostart unit is installed at {unit_path} but "
            "this install resolves to managed/cloud mode — the unit launches a "
            "local engine that exits immediately (no local pg_credentials) and "
            "the OS restart policy respawns it every ~30s indefinitely "
            "(log churn; GH #1405 defect-3 family)"
        ),
        fix_suggestions=[
            "nx daemon service uninstall --autostart  # removes the stray autostart unit",
        ],
    )]


def _check_service_autostart_drift() -> list[HealthResult]:
    """nexus-rlp0v (substantive-critic round 1, Significant): backstop for
    :func:`nexus.upgrade_finish.converge_service_autostart_unit`'s
    automatic-pass leg, the c0vby/6bmph launchagent-stray checks' sibling.

    That leg only fires on a conexus PACKAGE version transition, and even
    then it only NAMES the fix (``nx daemon restart-stale``) rather than
    running it -- it never bounces the service unattended (GH #1419 Issue
    3b restraint). A user who misses that one-shot NOTE and never runs
    ``nx daemon restart-stale`` keeps a stale unit (e.g. the
    ``ProcessType=Background`` 15x-slowdown vintage this bead fixed)
    installed forever with ZERO product signal otherwise -- exactly the
    field-report population nexus-rlp0v exists for. This check gives an
    operator a way to SEE it at any time via plain ``nx doctor``, same
    convention as ``_check_engine_convergence`` / the launchagent-stray
    checks above: report-only (never itself bounces the service -- that
    stays ``nx daemon restart-stale``'s job), silent on not-applicable
    (non-local mode, or no unit installed) AND on any probe failure
    (best-effort, must never break ``nx doctor`` -- unlike
    ``converge_service_autostart_unit``'s louder upgrade-path NEEDS-HUMAN
    contract, which fits a caller already mid-command; this is a passive
    surface for an operator who has run neither).

    Delegates entirely to
    :func:`nexus.upgrade_finish._probe_service_autostart_drift` for the
    applicability + comparison logic -- the SAME probe the convergence
    leg itself uses, so this check cannot silently drift from what a real
    convergence would actually act on.
    """
    try:
        from nexus.upgrade_finish import _probe_service_autostart_drift  # noqa: PLC0415 — deferred to avoid circular import

        probe = _probe_service_autostart_drift()
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_service_autostart_drift_check_failed", error=str(exc))
        return []

    if probe is None:
        return []  # not local mode, or no service-tier unit installed here

    dest, existing, rendered = probe
    if existing == rendered:
        return [HealthResult(
            label="Service autostart unit (local mode)",
            ok=True,
            detail=f"{dest} matches the current template",
        )]

    return [HealthResult(
        label="Service autostart unit (local mode)",
        ok=False,
        warn=True,
        detail=(
            f"the storage-service autostart unit at {dest} differs from "
            "the current template -- an installed unit is a rendered COPY "
            "that a package upgrade alone does not update (launchd/systemd "
            "only re-read it on bootout+bootstrap / reinstall); a fix that "
            "changed this template (e.g. nexus-rlp0v's ProcessType="
            "Background removal, a measured 15x local-mode indexing "
            "slowdown) needs an explicit convergence to take effect here"
        ),
        fix_suggestions=[
            "nx daemon restart-stale  # reinstalls the autostart unit and restarts the service",
        ],
    )]


def _check_migration_state(
    creds_path: Path | None = None,
    psql_bin: Path | None = None,
    psql_runner=None,  # injectable for unit tests
    diag_credentials=None,  # injectable: DiagCredentials | None
    diag_runner=None,  # injectable: run_diagnostic_sql psql_runner seam
) -> list[HealthResult]:
    """Verify Liquibase migration state on the nx-managed Postgres.

    What this check verifies (client-side psql queries against databasechangelog):

    1. The ``databasechangelog`` table exists and has at least one row.
       A running service implies Liquibase applied all changesets bundled in
       the JAR at startup (the JVM exits loudly on first-run migration failure),
       so the completeness of applied changesets is guaranteed by the service
       being up (/health).  This query confirms the table itself is reachable.

    2. No row has ``exectype = 'FAILED'``.  A FAILED changeset aborted
       mid-execution and left partial state, which can cause the service to
       refuse to start on the next boot. A ``RERAN`` exectype (a
       ``runOnChange`` changeset — e.g. GRANT statements — reapplied after
       its checksum changed) is Liquibase's normal, sanctioned behavior and
       is reported informationally, not as a failure.

    3. No EXECUTED row has a NULL md5sum.  Liquibase checksums every changeset
       on re-run; a NULL checksum on an applied changeset causes Liquibase to
       fail validation on next boot even though the row exists.

    Gated on pg_credentials being present (service/PG mode configured).
    """
    # Resolve creds_path default.
    if creds_path is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred to avoid circular import
        creds_path = nexus_config_dir() / CREDENTIALS_FILENAME

    if not creds_path.exists():
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid circular import

        if not is_local_mode():
            detail = _MANAGED_DEPLOYMENT_SKIP_DETAIL
        else:
            detail = _LOCAL_MODE_NOT_CONFIGURED_DETAIL
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=detail,
            warn=True,
        )]

    from nexus.db.pg_provision import (  # noqa: PLC0415 — deferred to avoid circular import
        _read_credentials,
        discover_pg_binaries,
        PgBinaryNotFoundError,
    )

    creds = _read_credentials(creds_path)
    host = "127.0.0.1"
    try:
        port = int(creds.get("PG_PORT", 0))
    except ValueError:
        port = 0
    if port <= 0:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail="pg_credentials missing PG_PORT; cannot connect",
            fatal=True,
        )]

    db_url = creds.get("NX_DB_ADMIN_URL", "")
    # Extract database name from JDBC URL: jdbc:postgresql://host:port/dbname
    dbname = "nexus"
    if "/" in db_url:
        dbname = db_url.rstrip("/").rsplit("/", 1)[-1] or "nexus"

    user = creds.get("NX_DB_ADMIN_USER", "nexus_admin")
    password = creds.get("NX_DB_ADMIN_PASS", "")

    # Resolve psql binary.
    if psql_bin is None:
        try:
            psql_bin = discover_pg_binaries().psql
        except PgBinaryNotFoundError as exc:
            return [HealthResult(
                label="Schema migrations",
                ok=False,
                detail=f"psql binary not found: {exc}",
                fatal=True,
            )]

    # Query 1: total row count (also verifies the table exists).
    total_sql = "SELECT COUNT(*) FROM databasechangelog;"
    proc = _run_psql(
        psql_bin, host, port, dbname, user, password, total_sql,
        psql_runner=psql_runner,
    )
    if proc.returncode != 0:
        stderr_snip = (proc.stderr or "").strip()[:200]
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=(
                f"Cannot query databasechangelog "
                f"(psql exit {proc.returncode}): {stderr_snip}"
            ),
            fix_suggestions=[
                "Run `nx init --service` to apply migrations",
                "Check that the Postgres cluster is running: nx service status",
            ],
            fatal=True,
        )]

    try:
        total = int(proc.stdout.strip())
    except ValueError:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=(
                f"Unexpected output from databasechangelog total-count query: "
                f"{proc.stdout!r}"
            ),
            fatal=True,
        )]

    if total == 0:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail="databasechangelog exists but has 0 rows — migrations never ran",
            fix_suggestions=["Run `nx init --service` to apply Liquibase migrations"],
            fatal=True,
        )]

    # Query 2: FAILED rows (real drift) vs RERAN/other non-EXECUTED rows.
    # nexus incident 2026-07-01: this used to treat ANY exectype != 'EXECUTED'
    # as fatal, but RERAN is Liquibase's own legitimate outcome for a
    # runOnChange changeset (e.g. GRANT statements reapplied after a checksum
    # change) — not evidence of a mid-run failure. A healthy DB with two
    # reapplied grant changesets was reported as a hard FAIL, indistinguishable
    # from real corruption. Only FAILED indicates a changeset that aborted
    # mid-execution and left partial state.
    drift_sql = (
        "SELECT COUNT(*) FILTER (WHERE exectype='FAILED'), "
        "COUNT(*) FILTER (WHERE exectype NOT IN ('EXECUTED','FAILED')) "
        "FROM databasechangelog;"
    )
    proc2 = _run_psql(
        psql_bin, host, port, dbname, user, password, drift_sql,
        psql_runner=psql_runner,
    )
    if proc2.returncode != 0:
        stderr_snip = (proc2.stderr or "").strip()[:200]
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=f"Migration drift query failed (psql exit {proc2.returncode}): {stderr_snip}",
            fatal=True,
        )]

    raw2 = proc2.stdout.strip()
    parts = raw2.split("|")
    if len(parts) != 2:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=(
                f"Migration drift query returned unexpected output: {raw2!r}"
            ),
            fatal=True,
        )]
    try:
        failed = int(parts[0])
        reran = int(parts[1])
    except ValueError:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=(
                f"Migration drift query returned unexpected output: {raw2!r}"
            ),
            fatal=True,
        )]

    if failed != 0:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=(
                f"Migration state mismatch: {failed} changeset(s) FAILED "
                "(mid-run failure, partial state)"
            ),
            fix_suggestions=[
                "Inspect: psql -c \"SELECT id,exectype FROM databasechangelog "
                "WHERE exectype='FAILED'\"",
                "Re-run: nx init --service to recover",
            ],
            fatal=True,
        )]

    reran_note = ""
    if reran != 0:
        reran_note = (
            f" ({reran} changeset(s) legitimately RERAN — e.g. a runOnChange "
            "grant reapplied after a checksum change; not a failure)"
        )

    # Query 3: NULL md5sum on EXECUTED rows.
    # A NULL checksum causes Liquibase validation to fail on next boot even
    # though the changeset row is present.
    null_md5_sql = (
        "SELECT COUNT(*) FROM databasechangelog "
        "WHERE exectype='EXECUTED' AND md5sum IS NULL;"
    )
    proc3 = _run_psql(
        psql_bin, host, port, dbname, user, password, null_md5_sql,
        psql_runner=psql_runner,
    )
    if proc3.returncode != 0:
        stderr_snip = (proc3.stderr or "").strip()[:200]
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=f"Migration md5sum query failed (psql exit {proc3.returncode}): {stderr_snip}",
            fatal=True,
        )]

    raw3 = proc3.stdout.strip()
    try:
        null_md5 = int(raw3)
    except ValueError:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=(
                f"Migration md5sum query returned unexpected output: {raw3!r}"
            ),
            fatal=True,
        )]

    if null_md5 != 0:
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail=(
                f"Migration checksum gap: {null_md5} EXECUTED changeset(s) with "
                "NULL md5sum — Liquibase will fail validation on next service boot"
            ),
            fix_suggestions=[
                "Inspect: psql -c \"SELECT id,md5sum FROM databasechangelog "
                "WHERE exectype='EXECUTED' AND md5sum IS NULL\"",
                "Re-run: nx init --service to re-apply and restore checksums",
            ],
            fatal=True,
        )]

    # Query 4 (nexus-pnwu0 / GH #1414): width-non-conformant chash rows
    # (octet_length <> 32, era-safe — see chash_tables.py) across the
    # chunk tables. A box that migrated legacy short ids pre-guard (or had
    # its chash CHECK constraints dropped out-of-band — the closed GH #1390
    # shape) serves FINE, and v0.1.48+ engines tolerate the rows at BOOT
    # too: rdr180-11 adds the octet-width CHECKs NOT VALID, and their
    # VALIDATE is the client chash-rekey rung's post-heal act — no boot
    # changeset VALIDATEs them (verified nexus-joima, T2 [21022]). The rows
    # are unhealed upgrade-ladder debt: surface a WARNING steering the
    # ladder heal (nexus-o513u ladder-first). Only a pre-v0.1.48 char-era
    # engine can still crash-loop on catalog-013-3's first VALIDATE (it
    # guards MISSING constraints, not VIOLATING rows). Never fatal on the
    # current box.
    #
    # nexus-vounk: this MUST run on the nexus_diag path, NOT as nexus_admin.
    # Every chash-bearing table is ENABLE+FORCE RLS with the fail-closed
    # tenant_isolation policy, so a nexus_admin session with no nexus.tenant
    # GUC counts ZERO rows (demonstrated 0-vs-9 on a real store) — the probe
    # would report clean on the exact poisoned store the install-binary gate
    # exists to block (the nexus-1wjmq asymmetry: any Liquibase VALIDATE that
    # DOES run sees every row — on a pre-v0.1.48 char-era engine that
    # crash-loops the boot). run_diagnostic_sql runs
    # as the SELECT-only BYPASSRLS nexus_diag role (no GUC), so integrity
    # counts see every tenant's rows — what VALIDATE sees. A missing
    # diagnostic role (pre-P2.1 install) or a probe failure degrades to a
    # WARN, never a false "clean".
    from nexus.db.chash_tables import (  # noqa: PLC0415 — deferred to avoid circular import
        CHASH_CONFORMANCE_LABEL,
        LEGACY_POISON_CHASH_TABLES,
        POISON_CHASH_TABLES,
        POISON_DETAIL_TOKEN,
        chash_conformance_statements,
        chash_era_probe_statement,
        debt_chash_conformance_statements,
        legacy_chash_conformance_statements,
        legacy_era_chash_conformance_statements,
        parse_conformance_sum,
    )
    from nexus.db.diag_connection import (  # noqa: PLC0415 — deferred to avoid circular import
        resolve_diag_credentials,
        run_diagnostic_sql,
    )
    from nexus.remediation.sql_lint import DiagnosticSqlViolation  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []
    view_era = False  # nexus-z5j0t: debt probe only runs where the view path proved live
    diag_creds = diag_credentials if diag_credentials is not None \
        else resolve_diag_credentials(creds_path)
    if diag_creds is None:
        results.append(HealthResult(
            label=CHASH_CONFORMANCE_LABEL,
            ok=False,
            detail=(
                "no nexus_diag diagnostic credentials (pre-P2.1 install) — "
                "the pre-upgrade poison check could NOT run. Re-run "
                "`nx init --service` to backfill the diagnostic role. Do NOT "
                "read this as a clean store."
            ),
            warn=True,
        ))
        nonconforming = -1
    else:
        def _run_diag(stmts: tuple[str, ...]) -> list[str]:
            return run_diagnostic_sql(
                stmts, diag_creds, psql_bin=psql_bin, psql_runner=diag_runner,
            )

        def _probe_unified_schema_exists() -> bool:
            """Existence-gate before reinterpreting a probe result as
            legacy-era (mirrors ``nexus.db.admin_sql``'s F14a pattern): a
            failure of the discriminator itself defaults to "assume
            unified" — the SAFEST default, since it refuses to reinterpret
            a genuinely poisoned current-era store as legacy-era just
            because the discriminator itself could not answer."""
            try:
                era_out = _run_diag((chash_era_probe_statement(),))
                return bool(era_out) and era_out[0].strip() == "1"
            except (RuntimeError, DiagnosticSqlViolation, ValueError) as era_exc:
                _log.warning(
                    "chash_probe_era_discriminator_failed",
                    error=str(era_exc)[:200],
                    note="could not determine unified-vs-legacy chash "
                         "schema — assuming unified (never reinterpret a "
                         "possibly-genuine poison result as legacy-era)",
                )
                return True

        try:
            poison_tables = POISON_CHASH_TABLES
            # Amendment A6 (nexus-9bufb): view-era statements first — counts
            # by construction via nexus.diag_chash_conformance. An engine one
            # generation behind (no view yet) fails the first set; the legacy
            # direct-table statements still work there because the legacy
            # grants era carries full-table SELECT — fall back LOUDLY (log),
            # never silently.
            try:
                counts = _run_diag(chash_conformance_statements())
                view_era = True
            except DiagnosticSqlViolation:
                # A LINT failure is a product defect, never an engine-
                # generation skew — re-raise to the outer handler (review
                # 47dcb65e Critical: DiagnosticSqlViolation subclasses
                # ValueError, so without this it would be silently retried
                # against the legacy statements and mislabeled as fallback).
                raise
            except (RuntimeError, ValueError) as view_exc:
                _log.warning(
                    "chash_probe_view_fallback_legacy",
                    error=str(view_exc)[:200],
                    # GH #1402: do NOT assert the cause here — the view path
                    # also fails on a live view when nexus_diag lacks the
                    # owner-granted view SELECT or the view owner lost table
                    # access (ownership fragmentation). The error field
                    # carries the real cause.
                    note="view-path probe failed — falling back to legacy "
                         "direct-table statements (view absent on pre-A6 "
                         "engines, or view/owner grant gap — see error)",
                )
                try:
                    counts = _run_diag(legacy_chash_conformance_statements())
                except DiagnosticSqlViolation:
                    raise
                except (RuntimeError, ValueError) as direct_exc:
                    # nexus-o8dil (2026-08-14): RDR-191 F14a mirror-direction
                    # straddle in the poison gate. The view is absent AND the
                    # unified-name direct fallback also failed — on a
                    # straddle-era box (past A6, before the RDR-191 unify)
                    # the unified nexus.chunks relation genuinely does not
                    # exist yet, so this direct COUNT fails the same way the
                    # view-path did. Existence-gate before retrying against
                    # the pre-unify per-dim direct statements; a genuinely
                    # broken current-era store re-raises the ORIGINAL error
                    # rather than being silently reinterpreted.
                    if _probe_unified_schema_exists():
                        raise
                    _log.warning(
                        "chash_probe_direct_fallback_retrying_legacy_era",
                        error=str(direct_exc)[:200],
                        note="unified-name direct fallback failed and "
                             "nexus.chunks does not exist yet — retrying "
                             "against the pre-RDR-191 per-dim table names",
                    )
                    counts = _run_diag(
                        legacy_chash_conformance_statements(LEGACY_POISON_CHASH_TABLES),
                    )
                    poison_tables = LEGACY_POISON_CHASH_TABLES

            # nexus-o8dil (2026-08-14): RDR-191 F14a mirror-direction
            # straddle in the poison gate (the confirmed shape, GH #1414
            # class recurrence). The unified view-path query above can
            # execute successfully yet return a NULL aggregate (blank psql
            # line) for a table whose relation was created by RDR-191 —
            # the deployed view was built by an OLDER engine's own
            # provisioning and still carries rows keyed by the pre-unify
            # per-dim names, so the unified WHERE filter matches no row.
            # Existence-gate before reinterpreting: consulted UNCONDITIONALLY
            # on a blank leg (never skipped), because the answer decides
            # which of TWO distinct straddle windows this is.
            if view_era and any(not c.strip() for c in counts):
                if _probe_unified_schema_exists():
                    # nexus-o8dil (2026-08-14, review round 2 SIGNIFICANT 1):
                    # nexus.chunks EXISTS — the engine has already migrated —
                    # yet the leg came back blank, so the deployed VIEW is
                    # stale (still per-dim shaped): view re-provisioning only
                    # happens via the chash-rekey rung's re-provision step or
                    # `nx init --service`, never automatically after a bare
                    # engine binary swap. This store IS measurable, just not
                    # through the stale view — fall through to the SAME
                    # direct unified-table statements used when the
                    # view-path raises outright, instead of surfacing a
                    # generic "could not probe" WARN for a store that is
                    # provably current-era.
                    try:
                        direct_counts = _run_diag(legacy_chash_conformance_statements())
                    except DiagnosticSqlViolation:
                        raise
                    except (RuntimeError, ValueError) as direct_exc:
                        _log.warning(
                            "chash_probe_direct_fallback_for_stale_view_failed",
                            error=str(direct_exc)[:200],
                            note="nexus.chunks exists but the view-path leg "
                                 "was blank (stale per-dim view) and the "
                                 "direct unified-table fallback also failed",
                        )
                    else:
                        _log.info(
                            "chash_probe_direct_fallback_for_stale_view",
                            note="the diag view is stale (still per-dim "
                                 "shaped) though the schema has already "
                                 "migrated; measured via direct "
                                 "unified-table counts instead",
                        )
                        counts = direct_counts
                else:
                    try:
                        legacy_view_counts = _run_diag(
                            legacy_era_chash_conformance_statements(),
                        )
                    except DiagnosticSqlViolation:
                        raise
                    except (RuntimeError, ValueError) as legacy_view_exc:
                        _log.warning(
                            "chash_probe_legacy_era_view_fallback_failed",
                            error=str(legacy_view_exc)[:200],
                        )
                        legacy_view_counts = None
                    if legacy_view_counts is not None and not any(
                        not c.strip() for c in legacy_view_counts
                    ):
                        _log.info(
                            "chash_probe_legacy_era_view_match",
                            note="the unified-name view query returned a "
                                 "NULL aggregate; the store verified via "
                                 "the pre-RDR-191 per-dim table names "
                                 "instead (RDR-191 straddle window)",
                        )
                        counts = legacy_view_counts
                        poison_tables = LEGACY_POISON_CHASH_TABLES

            nonconforming = parse_conformance_sum(poison_tables, counts)
        except (RuntimeError, DiagnosticSqlViolation, ValueError) as exc:
            # Probe failure (schema variant missing a table), lint refusal,
            # or non-numeric output — a WARN, never a false poison-clean.
            nonconforming = -1
            results.append(HealthResult(
                label=CHASH_CONFORMANCE_LABEL,
                ok=False,
                detail=(
                    "could not probe chash length across chunk tables via the "
                    f"nexus_diag path ({exc}) — the pre-upgrade poison check "
                    "did not run"
                ),
                warn=True,
            ))
    if nonconforming > 0:
        results.append(HealthResult(
            label=CHASH_CONFORMANCE_LABEL,
            ok=False,
            detail=(
                f"{nonconforming} chunk row(s) have a {POISON_DETAIL_TOKEN} "
                "(octet_length <> 32 — legacy pre-RDR-108 ids, or chash "
                "CHECK constraints were dropped out-of-band). The engine "
                "serves fine with these rows (the octet-width CHECKs stay "
                "NOT VALID for these rows), but "
                "they are unhealed upgrade-ladder debt (GH #1414 / "
                "nexus-pnwu0). Re-indexing affected content HEALS these "
                "rows in place and lowers this count (new conformant rows "
                "are written before stale rows are pruned — nexus-2hklz "
                "verified heal-by-replacement); deleting affected content "
                "also lowers it, so read a falling count as healing only "
                "where your content is intact."
            ),
            fix_suggestions=[
                "Step 1 — find each affected collection's repo: "
                "`nx catalog owners`",
                "Step 2 — re-index the file-backed legacy collections: "
                "`nx index repo <path>` (additive, per-collection). This "
                "is the ONLY remedy: re-indexing writes conformant rows "
                "before pruning stale ones. Content with no file behind "
                "it (store_put-only notes) cannot be healed this way — "
                "re-put it if the count matters.",
                "Step 3 — re-run `nx doctor`; upgrade the engine once "
                "this warning clears.",
                "Do NOT drop the chash length constraints to 'unblock' "
                "anything — that is what caused GH #1390.",
                "The will-not-boot class ONLY (service crash-looping at "
                "startup on a pre-v0.1.48 engine) recovers on the "
                "LAST_MIGRATION_CAPABLE pinned release (this version no "
                "longer ships the rollback tooling) — see \"If the "
                "service will not start\" in "
                "https://github.com/Hellblazer/nexus/blob/main/docs/"
                "migration-runbook.md",
            ],
            warn=True,
        ))

    # nexus-z5j0t: legacy-debt observability over the CHECK-less chash
    # bearers (topic_assignments.doc_id, frecency/relevance_log.chunk_id).
    # Non-gating BY DESIGN: no width CHECK exists on these tables, so a
    # non-32 value cannot crash-loop a VALIDATE — it silently degrades topic
    # membership / frecency ranking instead (converged by the remap cascade /
    # RDR-180 Item6 ETL). Only runs when the view path proved live; a stale
    # (pre-z5j0t 5-leg) view yields NULL sums (empty psql lines) → unknown,
    # logged at debug, never a WARN and never a false clean-or-poisoned.
    if view_era:
        try:
            debt_counts = run_diagnostic_sql(
                debt_chash_conformance_statements(), diag_creds,
                psql_bin=psql_bin, psql_runner=diag_runner,
            )
            debt = sum(int(c) for c in debt_counts)
        except (RuntimeError, DiagnosticSqlViolation, ValueError) as exc:
            _log.debug("chash_debt_probe_unavailable", error=str(exc)[:200])
            debt = -1
        if debt == -1:
            # critic-180-foundation finding 1: unknown must SURFACE, never
            # read as clean by omission. The common cause is a deployed view
            # predating the debt legs — the chash-rekey rung's re-provision
            # closes that window at the next nx upgrade.
            results.append(HealthResult(
                label="Chash legacy debt",
                ok=False,
                detail=(
                    "legacy-debt conformance UNKNOWN — the debt probe could "
                    "not run (deployed diag view predates the debt legs, or "
                    "probe failure). Do NOT read this as clean; `nx upgrade` "
                    "re-provisions the view."
                ),
                warn=True,
            ))
        if debt > 0:
            results.append(HealthResult(
                label="Chash legacy debt",
                ok=False,
                detail=(
                    f"{debt} hex-shaped chash reference(s) across "
                    "topic_assignments/frecency/relevance_log miss every "
                    "chunk-table join (dangling content references). "
                    "NON-GATING (no CHECK constraint exists on these tables); "
                    "alias-mapped rows converge via the RDR-180 rekey "
                    "cascade, and residual danglers are relic references "
                    "(title-keyed and other non-hex identities are excluded "
                    "— they are not chash debt)."
                ),
                warn=True,
            ))

    results.append(HealthResult(
        label="Schema migrations",
        ok=True,
        detail=(
            f"Schema migrations: {total} applied (0 FAILED, checksums present)"
            f"{reran_note}"
        ),
    ))
    return results


def _check_rls_present(
    creds_path: Path | None = None,
    psql_bin: Path | None = None,
    psql_runner=None,  # injectable for unit tests
) -> list[HealthResult]:
    """Structural RLS-presence check: verify every tenant table has RLS wired up.

    For each table in ``_RLS_TENANT_TABLES`` this checks:
    - ``pg_class.relrowsecurity = true`` (ENABLE ROW LEVEL SECURITY is set)
    - ``pg_class.relforcerowsecurity = true`` (FORCE ROW LEVEL SECURITY is set)
    - At least one row in ``pg_policies`` (a policy object exists)

    This is a structural presence check, NOT a policy-predicate correctness
    check — a policy of ``USING(true)`` would pass here.  Policy-predicate
    correctness (cross-tenant isolation) is covered by the RLS negative /
    cross-tenant integration tests in tests/db/test_http_*_integration.py.

    ANY table missing any of these structural conditions is a fatal result:
    the Liquibase changelogs must have failed to apply their RLS DDL, which
    indicates a serious schema regression.

    Gated on pg_credentials being present (service/PG mode configured).
    """

    # Resolve creds_path default.
    if creds_path is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        from nexus.db.pg_provision import CREDENTIALS_FILENAME  # noqa: PLC0415 — deferred to avoid circular import
        creds_path = nexus_config_dir() / CREDENTIALS_FILENAME

    if not creds_path.exists():
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid circular import

        if not is_local_mode():
            detail = _MANAGED_DEPLOYMENT_SKIP_DETAIL
        else:
            detail = _LOCAL_MODE_NOT_CONFIGURED_DETAIL
        return [HealthResult(
            label="RLS policies",
            ok=False,
            detail=detail,
            warn=True,
        )]

    from nexus.db.pg_provision import (  # noqa: PLC0415 — deferred to avoid circular import
        _read_credentials,
        discover_pg_binaries,
        PgBinaryNotFoundError,
    )

    creds = _read_credentials(creds_path)
    host = "127.0.0.1"
    try:
        port = int(creds.get("PG_PORT", 0))
    except ValueError:
        port = 0
    if port <= 0:
        return [HealthResult(
            label="RLS policies",
            ok=False,
            detail="pg_credentials missing PG_PORT; cannot connect",
            fatal=True,
        )]

    db_url = creds.get("NX_DB_ADMIN_URL", "")
    dbname = "nexus"
    if "/" in db_url:
        dbname = db_url.rstrip("/").rsplit("/", 1)[-1] or "nexus"

    user = creds.get("NX_DB_ADMIN_USER", "nexus_admin")
    password = creds.get("NX_DB_ADMIN_PASS", "")

    # Resolve psql binary.
    if psql_bin is None:
        try:
            psql_bin = discover_pg_binaries().psql
        except PgBinaryNotFoundError as exc:
            return [HealthResult(
                label="RLS policies",
                ok=False,
                detail=f"psql binary not found: {exc}",
                fatal=True,
            )]

    # Build a single query that returns one row per tenant table:
    #   schema_name | table_name | relrowsecurity | relforcerowsecurity | policy_count
    # Including schema_name + table_name in SELECT lets us match rows by identity
    # rather than by position (ORDER BY is alphabetical, not VALUES-list order).
    # Uses a VALUES list as the driving table so we get one output row per
    # expected table even if the table doesn't exist in pg_class (NULL row).
    table_values = ", ".join(
        f"('{schema}', '{tname}')"
        for schema, _, tname in (t.partition(".") for t in _RLS_TENANT_TABLES)
    )
    rls_sql = f"""
SELECT
    tbl.schema_name,
    tbl.table_name,
    c.relrowsecurity,
    c.relforcerowsecurity,
    COUNT(p.policyname) AS policy_count
FROM (VALUES {table_values}) AS tbl(schema_name, table_name)
LEFT JOIN pg_class c ON c.relname = tbl.table_name
    AND c.relnamespace = (
        SELECT oid FROM pg_namespace WHERE nspname = tbl.schema_name
    )
LEFT JOIN pg_policies p
    ON p.schemaname = tbl.schema_name AND p.tablename = tbl.table_name
GROUP BY tbl.schema_name, tbl.table_name, c.relrowsecurity, c.relforcerowsecurity
ORDER BY tbl.schema_name, tbl.table_name;
""".strip()

    proc = _run_psql(
        psql_bin, host, port, dbname, user, password, rls_sql,
        psql_runner=psql_runner,
    )
    if proc.returncode != 0:
        stderr_snip = (proc.stderr or "").strip()[:300]
        return [HealthResult(
            label="RLS policies",
            ok=False,
            detail=f"RLS introspection query failed (psql exit {proc.returncode}): {stderr_snip}",
            fatal=True,
        )]

    # Parse output: one pipe-separated line per table.
    # Format: schema_name|table_name|relrowsecurity|relforcerowsecurity|policy_count
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    if len(lines) != len(_RLS_TENANT_TABLES):
        return [HealthResult(
            label="RLS policies",
            ok=False,
            detail=(
                f"RLS query returned {len(lines)} rows "
                f"(expected {len(_RLS_TENANT_TABLES)}); schema mismatch"
            ),
            fatal=True,
        )]

    # Build a lookup dict keyed by "schema.table" for order-independent matching.
    rls_by_table: dict[str, tuple[str, str, int]] = {}
    for line in lines:
        parts = line.split("|")
        if len(parts) < 5:
            # Malformed row — mark as unknown failure.
            rls_by_table[line] = ("?", "?", 0)
            continue
        schema_name = parts[0].strip()
        table_name = parts[1].strip()
        key = f"{schema_name}.{table_name}"
        rls_on = parts[2].strip().lower()
        rls_force = parts[3].strip().lower()
        try:
            policy_count = int(parts[4].strip())
        except ValueError:
            policy_count = 0
        rls_by_table[key] = (rls_on, rls_force, policy_count)

    # nexus-o8dil.51: a listed table can legitimately be ABSENT from pg_class
    # (not yet migrated, or -- post RDR-191 Phase 4 -- a dropped per-dim
    # shard that was never listed in the first place but whose sibling
    # unified table might not have landed yet on an older box). The VALUES
    # driving table + LEFT JOIN means an absent table's relrowsecurity /
    # relforcerowsecurity come back NULL, which -t -A prints as an empty
    # string -- distinct from the 't'/'f' pg_class always assigns to a row
    # that actually exists (relrowsecurity is NOT NULL in every real pg_class
    # row). That is the ONLY signal absence has here; do not confuse it with
    # "RLS explicitly disabled" (rls_on == 'f'), which is a real table with
    # bad RLS and must stay FATAL.
    failed: list[str] = []
    absent: list[str] = []
    for table in _RLS_TENANT_TABLES:
        if table not in rls_by_table:
            failed.append(f"{table} (not in query output)")
            continue
        rls_on, rls_force, policy_count = rls_by_table[table]

        if rls_on == "" and rls_force == "":
            # LEFT JOIN produced no pg_class match: table does not exist.
            # Not fatal (chash_index precedent: a listed-but-dropped table
            # must never be a permanent false FATAL), but must not be
            # silently folded into the "all present and correct" ok result
            # either -- it is its own reported outcome, below.
            absent.append(table)
            continue

        if rls_on != "t" or rls_force != "t" or policy_count == 0:
            reasons = []
            if rls_on != "t":
                reasons.append("RLS not enabled")
            if rls_force != "t":
                reasons.append("RLS not forced")
            if policy_count == 0:
                reasons.append("no policies")
            failed.append(f"{table} ({', '.join(reasons)})")

    present_count = len(_RLS_TENANT_TABLES) - len(absent)

    if failed:
        absent_note = (
            f" ({len(absent)} listed table(s) not yet present, reported "
            f"separately: {', '.join(sorted(absent))})"
            if absent else ""
        )
        return [HealthResult(
            label="RLS policies",
            ok=False,
            detail=(
                f"RLS missing on {len(failed)}/{present_count} "
                f"present tenant table(s): {', '.join(failed)}{absent_note}"
            ),
            fix_suggestions=[
                "Re-run migrations: nx init --service",
                "Verify the Liquibase changeset applied RLS: "
                "check service/src/main/resources/db/changelog/",
            ],
            fatal=True,
        )]

    if absent:
        # Its own reported outcome (nexus-o8dil.51 acceptance): not FATAL
        # (the table may simply predate a migration that hasn't run yet, or
        # -- transiently, mid-upgrade -- postdate one), and not a silent
        # pass either (ok=False, warn=True) since a doctor run that reports
        # "RLS policies: OK" while N listed tables are actually missing
        # from the database would hide a real convergence gap.
        return [HealthResult(
            label="RLS policies",
            ok=False,
            warn=True,
            detail=(
                f"RLS policies: present and correct on {present_count}/"
                f"{present_count} migrated tenant table(s); "
                f"{len(absent)} listed table(s) not yet present in the "
                f"database (pre-migration or upgrade in progress): "
                f"{', '.join(sorted(absent))}"
            ),
        )]

    return [HealthResult(
        label="RLS policies",
        ok=True,
        detail=(
            f"RLS policies: present on {len(_RLS_TENANT_TABLES)}/"
            f"{len(_RLS_TENANT_TABLES)} tenant tables"
        ),
    )]


#: First conexus plugin release whose hooks.json carries the RDR-184
#: orchestration hook registrations (subagent-start-stamp + subagent-stop
#: landed ~78bb02b6/d613f2e7, ancestors of v6.14.0; nexus-3h0u6 then made
#: the plugin's hooks.json the ONLY registration surface). An installed
#: plugin below this floor has ZERO orchestration-hook coverage —
#: silently: no EXPECT/START rows, no stop guard (defeats the
#: nexus-ccs9v.15 default-ON directive). The plugin cannot warn about
#: this itself (a pre-floor plugin's hooks.json predates any warning hook
#: we could add), so the CLI — which upgrades via PyPI independently of
#: the plugin pin — carries the check (nexus-3xg21).
_ORCH_HOOKS_PLUGIN_FLOOR: tuple[int, int, int] = (6, 14, 0)


def _installed_conexus_plugin_versions(registry_path: Path | None = None) -> list[str] | None:
    """Versions of the installed conexus plugin per Claude Code's
    ``installed_plugins.json`` (v2 schema: ``"<plugin>@<marketplace>":
    [{"installPath": ..., "version": ...}, ...]``). ``None`` when the
    registry is absent/unreadable or carries no conexus entry — callers
    treat that as "not a plugin box", never a failure."""
    if registry_path is None:
        registry_path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(registry_path.read_text())
    except (OSError, ValueError):
        return None
    plugins = data.get("plugins") if isinstance(data.get("plugins"), dict) else data
    if not isinstance(plugins, dict):
        return None
    versions: list[str] = []
    for key, entries in plugins.items():
        if not (isinstance(key, str) and key.split("@")[0] == "conexus"):
            continue
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("version"), str):
                versions.append(entry["version"])
    return versions or None


def _check_orchestration_hook_floor(registry_path: Path | None = None) -> list[HealthResult]:
    """nexus-3xg21: warn when the installed conexus plugin predates the
    RDR-184 orchestration hook registrations. Soft WARN, never fatal —
    orchestration hooks are a multi-agent hygiene surface, and a box
    without the plugin at all is simply not in scope (ok row)."""
    label = "Orchestration hooks (plugin floor)"
    from nexus.engine_version import parse_engine_version  # noqa: PLC0415 — generic X.Y.Z parser, deferred import

    versions = _installed_conexus_plugin_versions(registry_path)
    if versions is None:
        return [HealthResult(
            label=label, ok=True,
            detail="no conexus plugin install detected — not applicable",
        )]
    parsed = [v for v in (parse_engine_version(s) for s in versions) if v is not None]
    if not parsed:
        return [HealthResult(
            label=label, ok=True,
            detail=f"plugin version unparseable ({versions[:3]}) — cannot verify",
        )]
    newest = max(parsed)
    floor_str = ".".join(str(p) for p in _ORCH_HOOKS_PLUGIN_FLOOR)
    if newest >= _ORCH_HOOKS_PLUGIN_FLOOR:
        return [HealthResult(
            label=label, ok=True,
            detail=f"plugin v{'.'.join(str(p) for p in newest)} >= v{floor_str} (hooks present)",
        )]
    return [HealthResult(
        label=label, ok=False, warn=True,
        detail=(
            f"installed conexus plugin v{'.'.join(str(p) for p in newest)} predates the "
            f"RDR-184 orchestration hooks (v{floor_str}+): NO stop-guard, NO "
            f"expectations ledger — multi-agent sessions run unguarded, silently"
        ),
        fix_suggestions=["/plugin update conexus (then restart the session)"],
    )]


def _check_catalog_legacy_file(*, config_dir: Path | None = None) -> list[HealthResult]:
    """nexus-aoqnb (GH #1419 Issue 4): name any legacy catalog SQLite file as
    a FROZEN MIGRATION SOURCE, never a live mirror.

    Steve Harris's backup held ``catalog.db`` with 532 docs / 13 links while
    the authoritative PG catalog held 592 / 52, and nothing in the product
    said which was real. The dangerous property is PLAUSIBILITY: a stale
    catalog opens, parses, and answers queries, so a recovery procedure
    reaches for it first. Copy-not-move migration leaves it behind
    deliberately (orphan-by-design, the Hal two-hop contract), which makes
    labelling it the product's job rather than the operator's.

    Two shapes both need naming — populated-but-stale (Steve's) and
    empty-but-present (observed on a dev box a month post-migration, 11
    tables and zero rows). The second is arguably worse for a restore: it
    succeeds and silently yields nothing.

    Not fatal: an orphaned source is the EXPECTED post-migration state. The
    failure being guarded is a human trusting it, so the row exists to be
    read, and the fix suggestions carry the actual instruction.
    """
    if config_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        config_dir = nexus_config_dir()

    results: list[HealthResult] = []

    # The stray: ~/.config/nexus/catalog.db, 0 bytes on real installs. Named
    # separately so nobody chases a file with nothing in it.
    stray = config_dir / "catalog.db"
    if stray.is_file():
        size = stray.stat().st_size
        if size == 0:
            results.append(HealthResult(
                label="Legacy catalog file",
                ok=False,
                warn=True,
                detail=(
                    f"{stray} is an EMPTY 0-byte stray — not a catalog, not a "
                    "migration source, no rows of any kind. Safe to ignore; "
                    "named only so it is not mistaken for a restore candidate."
                ),
                fix_suggestions=[
                    "Nothing to restore from this file — the catalog lives in "
                    "Postgres. Delete it only if you want the directory tidy.",
                ],
            ))

    legacy = config_dir / "catalog" / ".catalog.db"
    if legacy.is_file():
        import datetime as _dt  # noqa: PLC0415 — deferred, formatting only

        st = legacy.stat()
        mtime = _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        results.append(HealthResult(
            label="Legacy catalog file",
            ok=False,
            warn=True,
            detail=(
                f"{legacy} is a pre-migration relic ({st.st_size} bytes, last "
                f"written {mtime}). Nothing reads it and there is no path back "
                "to it (Hal, 2026-08-29); the authoritative catalog is Postgres."
            ),
            fix_suggestions=[
                "Nothing to restore or recover from this file. Use `nx catalog "
                "stats` for the real counts and delete the file when convenient. "
                "If this install was never migrated, the stranded-install row is "
                "the one to follow.",
            ],
        ))

    return results


def _check_stranded_install() -> list[HealthResult]:
    """nexus-gynt2: stranded-install detector (N+1 P4b prerequisite).

    Disarmed (``LAST_MIGRATION_CAPABLE is None``) on every
    migration-capable release — reported as an ok row so the check is
    visibly wired. At N+1 the stamped constant arms it: unmigrated pre-PG
    data (chroma.sqlite3 / t2.db / memory.db / .catalog.db present, no
    verified migration report) is a FATAL ✗ carrying the literal two-hop
    redirect. Pure file stats — see :mod:`nexus.stranded_install`.
    """
    label = "Stranded pre-PG install"
    from nexus.config import detect_stranded_install_default  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.stranded_install import LAST_MIGRATION_CAPABLE  # noqa: PLC0415 — leaf module, deferred for symmetry

    if LAST_MIGRATION_CAPABLE is None:
        return [HealthResult(
            label=label,
            ok=True,
            detail="detector disarmed — this release ships the migration tool",
        )]
    stranded = detect_stranded_install_default()
    if stranded is None:
        return [HealthResult(label=label, ok=True, detail="no unmigrated pre-PG data")]
    # THE PIN IS THE POINT. The detail used to come from stranded.message,
    # whose first hop is uv-shaped because that module is a stdlib-only leaf,
    # and the suggestion used to route the whole sentence through
    # _upgrade_advice — which on a generation box replaced it wholesale with
    # the generic installer, dropping both the pinned version and the "last
    # migration-capable release" framing, and so advising the NEWEST release:
    # the exact hop this procedure exists to avoid. doctor can ask which
    # layout the box has, so it asks, once, and the banner and the suggestion
    # now name the same command (nexus-utpuw.13).
    first_hop = _install_advice().pinned_install_command(
        stranded.pinned_release,
        legacy=f"uv tool install conexus=={stranded.pinned_release}",
    )
    return [HealthResult(
        label=label,
        ok=False,
        fatal=True,
        detail=stranded.message_for(first_hop),
        fix_suggestions=[
            f"Install the last migration-capable release: {first_hop}",
            "Run: nx upgrade (the ladder converges the pre-PG data migration)",
            "Then upgrade back to this version",
        ],
    )]


def _check_pending_rungs() -> list[HealthResult]:
    """RDR-185 P0.4 (nexus-n7u38.4): read-only upgrade-ladder surface.

    Reports pending ladder rungs from each rung's READ-ONLY ``detect()`` —
    zero writes, zero work, the completion store is never opened (the
    ``resolve_pending_steps`` dry-run-truth precedent). Pending rungs are a
    soft warning with `nx upgrade` (the single trigger) as the remedy.
    Crash-proof: any failure ABOVE the per-rung loop (deferred imports,
    ``default_registry()`` construction) degrades to a SOFT WARNING, never a
    silent ``ok=True`` — a check that could not even enumerate the ladder
    must not render as a clean row (nexus-v2mdd: this outer handler
    previously reported ``ok=True``, regressing the identical bug
    ``pending_rungs``' inner per-rung handling already fixed once). Never
    crashes ``nx doctor`` as a whole.
    """
    try:
        from nexus.upgrade_ladder import registry as _ladder_registry  # noqa: PLC0415 — deferred to avoid module-load cost
        from nexus.upgrade_ladder.runner import pending_rungs  # noqa: PLC0415 — deferred to avoid module-load cost

        statuses = pending_rungs(_ladder_registry.default_registry())
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.warning("doctor_pending_rungs_check_failed", error=str(exc))
        return [HealthResult(
            label="Upgrade ladder",
            ok=False,
            warn=True,
            detail=f"could not check pending rungs: {exc}",
        )]

    pending = [(name, status) for name, status in statuses if status.pending]
    if not pending:
        return [HealthResult(
            label="Upgrade ladder",
            ok=True,
            detail=f"no pending rungs ({len(statuses)} registered)",
        )]
    names = "; ".join(
        f"{name}: {status.pending_detail or 'pending'}" for name, status in pending[:6]
    )
    return [HealthResult(
        label="Upgrade ladder",
        ok=False,
        warn=True,
        detail=f"{len(pending)} pending upgrade rung(s) — {names}",
        fix_suggestions=["Run: nx upgrade"],
    )]


def _check_dimension_orphans() -> list[HealthResult]:
    """Name T3 collections whose declared embedding dim no longer matches
    the active serving embedder, and suggest the remedy (GH #1113 /
    nexus-9tsdf AC2).

    Such a collection (e.g. a minilm-l6-v2-384 leftover after the active
    embedder moved to 1024d voyage) can never be searched — every
    cross-corpus search skips it. Reuses the SAME finder ``nx collection
    prune`` lists from, so doctor and the remedy command can never
    disagree about what counts as an orphan. Degrades to a skip — never a
    crash, and never a guess: an unresolved active-embedder probe reports
    "skipped" rather than risk telling the operator to delete healthy
    collections.
    """
    label = "T3 dimension orphans"
    try:
        from nexus.commands.collection import _find_dimension_mismatched_collections  # noqa: PLC0415 — deferred to avoid circular import
        from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid circular import

        t3 = make_t3()
        mismatches, _skipped, active_label = _find_dimension_mismatched_collections(t3)
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_dimension_orphan_check_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (T3 unavailable)")]

    if active_label == "unknown":
        return [HealthResult(
            label=label, ok=True,
            detail="skipped (active embedder unresolved — cannot verify)",
        )]
    if not mismatches:
        return [HealthResult(
            label=label, ok=True,
            detail=f"none (active embedder: {active_label})",
        )]

    names = "; ".join(
        f"{m['name']} ({m['declared_dim']}d vs active {m['active_dim']}d, "
        f"{m['count']} chunk(s))"
        for m in mismatches
    )
    return [HealthResult(
        label=label,
        ok=False,
        warn=True,
        detail=(
            f"{len(mismatches)} collection(s) unsearchable under the active "
            f"embedder ({active_label}): {names}"
        ),
        fix_suggestions=[
            "nx collection prune          (list them)",
            "nx collection prune --yes    (delete them)",
        ],
    )]


# RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: manifest_orphan_report,
# _compact_position_ranges, _check_dangling_manifests, and their
# constants (_DANGLING_MANIFEST_NAME_THRESHOLD, _MANIFEST_ORPHANS_SAMPLE_
# LIMIT, _MANIFEST_ORPHANS_MAX_ROWS_PER_DIM, _DANGLING_MANIFEST_POPULATION_
# NOTE) are DELETED here — the manifest-chunk FK (catalog-029, VALIDATEd,
# deployed engine-service-v0.1.76) makes the dangling state they detected
# unreachable: a dangling manifest INSERT or a DELETE of a still-referenced
# chunk is now rejected at the source. _check_manifest_null_collection
# below is EXPLICITLY NOT RETIRED (RDR-191 Decision item 4's own
# carve-out) — see its own docstring.


_MANIFEST_NULL_COLLECTION_LABEL = "manifest pre-backfill rows (collection IS NULL)"


def _check_manifest_null_collection() -> list[HealthResult]:
    """T2 nexus/chroma-residue-plan-2026-08-10 §C2: read-only census of the
    manifest population the FK does not enforce — manifest rows with
    ``collection IS NULL``.

    EXPLICITLY NOT RETIRED (RDR-191 Decision item 4's own carve-out,
    2026-08-15): the manifest-chunk FK (catalog-029, VALIDATEd) enforces
    every NON-NULL-collection manifest row's referential integrity, making
    the former ``_check_dangling_manifests``/``manifest_orphans`` apparatus
    unreachable and RETIRED alongside their SQL functions
    (catalog-030-retire-manifest-verify.xml). But under PostgreSQL's default
    ``MATCH SIMPLE``, a row with a NULL in ANY foreign-key column is EXEMPT
    from enforcement entirely — the FK gives NULL-collection rows no
    guarantee at all, in either direction. This census remains the ONLY
    visibility into that permanently-unenforced population; retiring it
    alongside the FK-covered apparatus would delete the measurement while
    the thing it measures still exists (RDR gate Critical 1: this exact
    exclusion was lost in prose once already — see Decision item 4's own
    text).

    HISTORICAL CONTEXT (pre-Phase-6): this census was originally written to
    close a FALSE-CLEAN gap in the now-retired ``manifest_orphans``/
    ``manifest_verify_all`` — both filtered their working set to
    ``collection IS NOT NULL`` (catalog-004/catalog-020's own changeset
    comments), so a collection whose manifest rows were 100% NULL-collection
    never appeared in their output at all, and the now-retired
    ``_check_dangling_manifests`` read that absence as "nothing to report"
    indistinguishable from "verified clean." That mechanism is gone with
    those functions; this check's OWN job — reporting the NULL-collection
    population explicitly, honest 0 or not — is unaffected and continues
    unchanged.

    THE GHOST-DOCUMENT REFINEMENT (verified against source, not assumed,
    historical — the now-retired ``manifest_backfill()`` stamping function
    this originally described no longer exists, catalog-030): ghost/
    sourceless documents (registered with an empty ``physical_collection``
    — see ``CatalogRepository#register``'s ghost-element contract) could
    never have their manifest rows' NULL ``collection`` stamped by that
    function even while it existed — confirmed verbatim by
    catalog-014-manifest-collection-stamp.xml's own changeset comment:
    "ghost docs (physical_collection empty) ... are skipped, matching
    manifest_backfill()'s semantics." ``backfillable`` in this check's
    report therefore measures a population that, post catalog-025's NOT
    NULL promotion, is now structurally empty on any converged install —
    the split into ``backfillable``/permanently-excluded below is
    preserved for historical/legacy-population honesty, not because a live
    remedy still exists for either bucket.

    Best-effort, never crashes `nx doctor`: a catalog reader lacking
    ``manifest_null_collection_report`` (an older client, or a test double)
    degrades to the same honest "cannot determine" as an engine 404 —
    ``unavailable=True`` NEVER renders as a silent clean pass, but its
    severity is GATED ON THE ENGINE FLOOR (substantive critique finding 1,
    T2 nexus/chroma-residue-C2-durability-critique-2026-08-10), unlike
    ``_check_chash_conformance_report``'s unconditional loud-WARN-plus-
    allowlist-entry contract: while ``REQUIRED_ENGINE_VERSION`` is at or
    below ``(0,1,69)`` (the pin at the time this route was added — it has
    never shipped on any tag), "route absent" is the EXPECTED state on
    every engine a client is permitted to run, so it renders as
    informational (``ok=True``); once the floor advances past that point,
    the same "unavailable" reading becomes genuinely wrong and renders as a
    loud WARN. See the ``unavailable`` branch below for the full rationale
    — this self-corrects on the live constant with no allowlist entry or
    manual removal step needed.

    READ-ONLY: this function never calls ``manifest_backfill`` and never
    mutates the catalog.
    """
    label = _MANIFEST_NULL_COLLECTION_LABEL
    from nexus.engine_version import REQUIRED_ENGINE_VERSION  # noqa: PLC0415 — deferred; stdlib-only leaf, cheap either way

    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return [HealthResult(label=label, ok=True, detail="skipped (no catalog)")]
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_manifest_null_collection_check_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (catalog unavailable)")]

    try:
        report = cat.manifest_null_collection_report()
    except Exception as exc:  # noqa: BLE001 — best-effort: a reader without this method (older client,
        # test double) must degrade honestly, not crash `nx doctor`.
        _log.debug("doctor_manifest_null_collection_check_failed", error=str(exc))
        report = {"total": 0, "backfillable": 0, "unavailable": True}

    if report.get("unavailable"):
        # Severity-gated on the engine floor (substantive critique finding 1,
        # T2 nexus/chroma-residue-C2-durability-critique-2026-08-10) — the
        # SAME shape as TestDescendantsFallbackDoesNotOutliveItsRoute
        # (tests/test_engine_version.py) and http_catalog_client.py's own
        # descendants()/manifest_null_collection_report() docstrings:
        # GET /v1/catalog/manifest/null_collection ships in an engine tag
        # AFTER REQUIRED_ENGINE_VERSION==(0,1,69) (the pin at the time this
        # route was added), so "route absent" is the EXPECTED condition on
        # every engine a client is permitted to run today, not a defect.
        #
        # Rendering that expected gap as a loud WARN unconditionally (the
        # `_check_dangling_manifests`/`_check_chash_conformance_report`
        # precedent, which pairs an unconditional WARN with a temporary
        # fresh-install-mvv.sh ALLOWLIST_REGEX entry + a mechanized removal
        # trigger) would make `./tests/e2e/fresh-install-mvv.sh` fail on
        # EVERY virgin box today — the empty-by-design allowlist regex
        # matches nothing, and every install currently 404s this route.
        # Unlike those two checks (whose "engine predates the route" branch
        # was a genuinely transient historical gap already closed once the
        # floor moved), this route has never shipped on ANY tag — so an
        # unconditional WARN here would be pure alarm fatigue: every user
        # sees it, it names no action they can take, and it never resolves
        # until Hal cuts the next engine tag. Gating on the floor instead
        # means this check self-corrects the moment REQUIRED_ENGINE_VERSION
        # advances past (0,1,69) with NO manual removal step required (unlike
        # the allowlist-entry idiom) — the comparison re-evaluates the LIVE
        # constant on every call.
        route_predates_floor = REQUIRED_ENGINE_VERSION <= (0, 1, 69)
        if route_predates_floor:
            return [HealthResult(
                label=label,
                ok=True,
                detail=(
                    "informational — this engine predates GET "
                    "/v1/catalog/manifest/null_collection "
                    f"(REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} "
                    "pins a floor at or below (0,1,69), before the route "
                    "shipped), so orphan-check coverage for pre-backfill "
                    "(collection IS NULL) manifest rows cannot be "
                    "determined on this engine. This is EXPECTED, not a "
                    "defect — the 'dangling manifest chashes' check's clean "
                    "verdict still does not cover this population; re-run "
                    "after the next engine tag lands."
                ),
            )]
        return [HealthResult(
            label=label,
            ok=False,
            warn=True,
            detail=(
                "UNKNOWN — could not determine how many manifest rows are "
                "pre-backfill (collection IS NULL), even though "
                f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} should "
                "carry GET /v1/catalog/manifest/null_collection. The "
                "'dangling manifest chashes' check's clean verdict does NOT "
                "cover this population — investigate the read failure (this "
                "is no longer the expected pre-route-floor gap)."
            ),
        )]

    total = int(report.get("total", 0) or 0)
    if total == 0:
        return [HealthResult(label=label, ok=True, detail="none")]

    backfillable = int(report.get("backfillable", 0) or 0)
    ghost = total - backfillable
    detail = (
        f"{total} manifest row(s) have collection IS NULL. This is a "
        "legacy/pre-catalog-025 population — on a converged install "
        "(catalog_document_chunks.collection NOT NULL), no NEW row can ever "
        "land here — and it is not covered by any other orphan/damage "
        "check in this doctor sweep (RDR-191 Phase 6 retired those "
        "entirely; the manifest-chunk FK does not enforce NULL-collection "
        "rows either, under MATCH SIMPLE)."
    )
    fix_suggestions: list[str] = []
    if backfillable:
        detail += (
            f" {backfillable} of them belong to a document with a "
            "physical_collection. nexus.manifest_backfill() — the function "
            "that used to stamp this population — no longer exists "
            "(RDR-191 Phase 6); re-indexing the owning document instead "
            "(the normal write path now requires a non-blank collection on "
            "every manifest write) replaces the row with a correctly "
            "stamped one."
        )
        fix_suggestions.append(
            "nx index <path> --force       (re-index; the new write "
            "requires a real collection, replacing the NULL-collection row)"
        )
    if ghost:
        detail += (
            f" {ghost} belong to ghost/sourceless document(s) (no "
            "physical_collection) and have no automated remedy — "
            "re-`nx store put` the content, or `nx catalog reconcile` to "
            "rebuild the manifest from T3, if the content is still needed."
        )
    return [HealthResult(label=label, ok=False, warn=True, detail=detail, fix_suggestions=fix_suggestions)]


#: RDR-180 (bead nexus-du2dw): the label for the ENGINE-ROUTE chash
#: conformance check, deliberately DISTINCT from ``CHASH_CONFORMANCE_LABEL``
#: (``nexus.db.chash_tables``, "Chunk chash conformance") — that label is
#: substring-matched by the install-binary gate and the convergence gate
#: (``upgrade_finish.py``, ``commands/daemon.py``), both of which need the
#: LOCAL nexus_diag probe's cross-tenant BYPASSRLS visibility (nexus-vounk:
#: a tenant-scoped session undercounts to zero on a poisoned store). This
#: check's tenant-scoped count cannot honestly stand in for that decision,
#: so it reports under its own label and is never wired into those gates —
#: it exists purely so a managed/cloud install (no local psql access) gets
#: SOME observability instead of none.
_CHASH_CONFORMANCE_REPORT_LABEL = "Chunk chash conformance (tenant-scoped, engine route)"

#: Dims the ``chash_conformance_report`` stored function accepts (RDR-180).
_CHASH_CONFORMANCE_REPORT_DIMS: tuple[int, ...] = (384, 768, 1024)


def _check_chash_conformance_report() -> list[HealthResult]:
    """Managed/cloud-mode chash width-conformance check (RDR-180, bead
    nexus-du2dw) — the engine-route counterpart to the LOCAL-ONLY
    ``nexus_diag`` psql probe run by :func:`_check_migration_state`
    (``nexus.db.diag_connection`` — shells a local psql at 127.0.0.1 using a
    local ``pg_credentials`` file, LOCAL-ONLY BY DESIGN per the nexus-y3wuu
    Hal decision). A managed/cloud install has no local Postgres and no
    local credentials file, so that probe is PERMANENTLY BLIND there — the
    same blind-spot family as the nexus-55l58 shakedown's §3.3b
    substrate-direct anchor finding.

    SCOPING (read before comparing this check's count against the local
    'Chunk chash conformance' label): this check calls
    ``HttpCatalogClient.chash_conformance_report``, which invokes a
    SECURITY INVOKER stored function — tenant-scoped by FORCE RLS, NOT the
    cross-tenant BYPASSRLS view the local probe reads (nexus-vounk: a
    tenant-scoped session undercounts to zero on a poisoned store, which is
    exactly why the install-binary gate needs the cross-tenant view). This
    check gives a managed-mode tenant visibility into THEIR OWN data's
    conformance; it is a self-service observability surface, not a
    substitute for the local gate's whole-store decision — hence the
    distinct label (never fed into the install-binary/convergence gates,
    which filter on ``CHASH_CONFORMANCE_LABEL`` exactly).

    Covers the GATING ("poison") tables that are dim-routable —
    ``chunks_<dim>`` and ``catalog_document_chunks`` (filtered to that dim's
    model-token collections, same IN-list routing caveat the now-retired
    ``manifest_orphans`` used, RDR-191 Phase 6 nexus-o8dil.33). The
    LEGACY-DEBT tables (topic_assignments, frecency, relevance_log) are NOT
    covered — they are not dim-routable by construction (mixed identity
    space); this is a stated scope reduction relative to the local probe's
    four-table coverage, not a silent one.

    Engine-floor honesty (vw594 F3 precedent, formerly also demonstrated by
    the now-retired manifest_verify_all): a pre-route engine 404s
    ``/chash/conformance`` — this degrades to a LOUD WARN naming the gap
    explicitly, never a silent/false clean pass. Any other failure (engine
    down, catalog unavailable) also degrades to a WARN or a benign skip,
    matching the fail-open-but-loud contract used throughout this module —
    this check must never crash `nx doctor` and must never read "couldn't
    check" as "checked, clean" (nexus-kmo9h).
    """
    label = _CHASH_CONFORMANCE_REPORT_LABEL
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            # Benign by design (nexus-5h4ou re-examination): reader-returns-
            # None is the "no catalog configured" state — a configuration
            # fact, not a probe failure. There is genuinely nothing for this
            # check to examine, so a plain skip is honest, unlike the arms
            # below where something SHOULD have been examinable.
            return [HealthResult(label=label, ok=True, detail="skipped (no catalog)")]
    except Exception as exc:  # noqa: BLE001 — must not crash `nx doctor`, but must not lie either
        # nexus-5h4ou: the factory RAISING is "could not check" — a box with
        # a catalog whose reader failed to construct. That is
        # distinguishable-from-clean territory (nexus-kmo9h), never a bare
        # ok=True skip.
        _log.warning("doctor_chash_conformance_report_check_failed", error=str(exc))
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"SKIPPED (catalog reader unavailable: {exc}) — not a clean-store signal",
        )]

    import httpx  # noqa: PLC0415 — deferred to avoid a heavy/optional import at module load

    rows: list[dict] = []
    dims_checked = 0
    for dim in _CHASH_CONFORMANCE_REPORT_DIMS:
        try:
            result = cat.chash_conformance_report(dim)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status == 404:
                # Pre-route engine: every dim 404s identically, so stop at
                # the first one — the vw594 F3 / manifest_verify_all
                # precedent, applied here. Fail OPEN, but LOUD.
                _log.warning(
                    "doctor_chash_conformance_report_engine_floor",
                    status=status, dim=dim,
                )
                return [HealthResult(
                    label=label, ok=False, warn=True,
                    detail=(
                        "SKIPPED (engine predates the chash-conformance "
                        f"route — /chash/conformance 404'd on dim={dim}; "
                        "re-run after the next engine tag lands). This is "
                        "NOT a clean-store signal — if a local psql is "
                        "available, `nx doctor`'s local 'Chunk chash "
                        "conformance' check is the authoritative "
                        "cross-tenant probe until the engine is upgraded."
                    ),
                )]
            _log.warning(
                "doctor_chash_conformance_report_check_failed",
                error=str(exc), dim=dim,
            )
            return [HealthResult(
                label=label, ok=False, warn=True,
                detail=f"SKIPPED (chash_conformance_report failed for dim={dim}: {exc})",
            )]
        except Exception as exc:  # noqa: BLE001 — must not crash `nx doctor`, but must not lie either
            # substantive-critic (T2 nexus/critique-du2dw-2026-08-05 [21458]):
            # this branch used to return ok=True "skipped" — which silently
            # swallows HttpCatalogClient.chash_conformance_report's OWN
            # deliberate fail-closed RuntimeError (missing `tables` field —
            # see that method's docstring) into a false-benign pass,
            # contradicting this check's own "never a false clean" promise a
            # few lines above. WARN, never ok=True, for ANY exception here.
            _log.warning(
                "doctor_chash_conformance_report_check_failed",
                error=str(exc), dim=dim,
            )
            return [HealthResult(
                label=label, ok=False, warn=True,
                detail=f"SKIPPED (chash_conformance_report failed for dim={dim}: {exc})",
            )]
        dims_checked += 1
        rows.extend(result.get("tables") or [])

    if dims_checked == 0:
        # NON-VACUITY (nexus-kmo9h / nexus-5h4ou): zero dims actually
        # checked is not a clean bill of health — this arm used to return
        # ok=True directly under this very comment. RDR-191's shard-drop
        # window makes "no dim reachable" a real state, and it must render
        # distinguishable from genuinely-clean.
        #
        # SCOPE (nexus-5h4ou acceptance item 4, decided at review): warn,
        # NOT fatal — deliberately matching every OTHER could-not-check arm
        # of this same check (engine-predates-route 404, transport failure,
        # factory-raise above). Doctor's process exit code is therefore
        # unchanged (warns never mark the run failed, RDR-129 B4);
        # "distinguishable from clean" is served by the CLI warn glyph and
        # the --json ok=false/status field, which scripted consumers read.
        # Making only these two arms fatal while the 404 arm stays warn
        # would be an arbitrary split inside one check's uniform
        # degradation contract. Pinned by
        # test_could_not_check_arms_warn_but_do_not_fail_the_run.
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=(
                "SKIPPED (0 dims checked — no dim reachable); this check "
                "examined NOTHING and must not read as clean (nexus-5h4ou)"
            ),
        )]

    total_non_conformant = 0
    offenders: list[str] = []
    for row in rows:
        try:
            n = int(row.get("non_conformant", 0) or 0)
        except (TypeError, ValueError) as exc:
            _log.debug(
                "doctor_chash_conformance_report_row_skipped", row=row, error=str(exc),
            )
            continue
        total_non_conformant += n
        if n > 0:
            offenders.append(f"{row.get('table_name', '?')}={n}")

    # substantive-critic SIGNIFICANT (nexus-4ijv4, T2 [21458]): a collection
    # whose model token maps to no dim is INVISIBLE to the per-dim loop
    # above at every dim — same IN-list routing caveat the now-retired
    # manifest_orphans used (nexus-h1zu0, RDR-191 Phase 6 nexus-o8dil.33).
    # Left unstated, a tenant
    # with such content reads "clean" while those collections were never
    # counted or sampled at all — the exact false-clean-by-omission shape
    # nexus-kmo9h exists to catch. Best-effort: a probe failure here must
    # never crash this check or hide the primary (non_)conformant result.
    unroutable_collections: list[str] = []
    try:
        from nexus.corpus import is_conformant_collection_name, parse_conformant_collection_name  # noqa: PLC0415 — deferred to avoid import cycle
        from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid a heavy/optional import at module load
        from nexus.db.reconcile import dim_for_model_token  # noqa: PLC0415 — deferred to avoid import cycle; the canonical dim table (nexus-h1zu0)

        t3 = make_t3()
        for c in t3.list_collections():
            name = str(c.get("name", ""))
            if not name or not is_conformant_collection_name(name):
                continue
            token = parse_conformant_collection_name(name)["embedding_model"]
            if dim_for_model_token(token) is None:
                unroutable_collections.append(name)
        unroutable_collections = sorted(set(unroutable_collections))
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment only; never hides the primary result
        _log.debug("doctor_chash_conformance_report_unroutable_probe_failed", error=str(exc))
        unroutable_collections = []

    unroutable_suffix = ""
    if unroutable_collections:
        names = ", ".join(unroutable_collections[:10])
        more = (
            f" (+{len(unroutable_collections) - 10} more)"
            if len(unroutable_collections) > 10 else ""
        )
        unroutable_suffix = (
            f" NOT CHECKED: {len(unroutable_collections)} collection(s) use "
            "an embedding-model token this probe cannot route to any dim — "
            f"never counted, never sampled: {names}{more} (same IN-list "
            "routing caveat the now-retired manifest_orphans used; "
            "nexus-h1zu0)."
        )

    if total_non_conformant > 0:
        return [HealthResult(
            label=label,
            ok=False,
            warn=True,
            detail=(
                f"{total_non_conformant} chunk row(s) in YOUR tenant have a "
                "width-non-conformant chash (octet_length <> 32 — legacy "
                "pre-RDR-108 ids; the GH #1414 / nexus-pnwu0 class). Per "
                f"table: {', '.join(offenders)}. This is a TENANT-SCOPED "
                "count (see this check's docstring for why it differs from "
                "the local cross-tenant psql probe). Re-indexing affected "
                "content heals these rows in place." + unroutable_suffix
            ),
            fix_suggestions=[
                "nx catalog owners             (find affected collections' repos)",
                "nx index repo <path>          (re-index file-backed collections, additive)",
                "nx doctor                     (re-run; this warning clears once healed)",
            ],
        )]

    if unroutable_collections:
        # nexus-4ijv4: clean-with-unroutable must NEVER render as a plain
        # clean pass — the CHECKED tables/dims are genuinely clean, but the
        # tenant's store as a WHOLE was not fully checked. WARN, not ok=True.
        return [HealthResult(
            label=label,
            ok=False,
            warn=True,
            detail=(
                f"clean across {len(rows)} checked table(s), {dims_checked} "
                f"dim(s) (tenant-scoped) —{unroutable_suffix}"
            ),
        )]

    return [HealthResult(
        label=label,
        ok=True,
        detail=(
            f"clean — 0 width-non-conformant chash rows across {len(rows)} "
            f"table(s), {dims_checked} dim(s) checked (tenant-scoped)"
        ),
    )]


_GC_AUDIT_NON_EMPTY_LABEL = "gc_audit non-empty after purge"

#: Clock-drift tolerance when comparing a gc_audit ``purge_trash`` row's
#: ``created_at`` against the local purge marker's ``ts`` (crit-fix
#: critique 2026-08-19, nexus-0uuit/sybbh pile): the client and engine are
#: separate hosts, so a small allowance avoids a false warn on an
#: otherwise-correct same-purge audit row purely from clock skew. Generous
#: on purpose — this is a forensic freshness check, not a precise ordering
#: guarantee.
_GC_AUDIT_CLOCK_SKEW = timedelta(minutes=5)


#: Result-dict keys (per marker's ``result`` field, verbatim from
#: ``CatalogRepository#purgeTrash``'s response) that indicate the purge
#: actually reaped something. ``documents_purged`` and the three
#: ``chunks_<dim>_stranded`` counts are the only fields in that response
#: that reflect real deletions; ``dry_run``/``documents_eligible`` do not
#: (critique 2026-08-19 round 2, nexus-sybbh: ``nexus.purge_trash``'s own
#: audit INSERT is gated on ``v_chunk_count > 0 OR v_count > 0``, catalog-
#: 033-1 — a genuinely no-op real purge writes NO gc_audit row by design,
#: so this check must not expect one either).
_EFFECT_RESULT_KEY_RE = re.compile(r"^(documents_purged|chunks_\d+_stranded)$")


def _marker_has_effect(marker: dict) -> bool:
    """True if *marker*'s stored ``result`` shows a nonzero purge effect.

    Fail-open toward cross-checking, not toward silence: a missing or
    malformed ``result`` (unexpected shape, older marker format) is
    treated as "has effect" so the check still cross-references it,
    rather than risking a silent skip over a real writer-side defect
    (no-silent-fallback-for-correctness).
    """
    result = marker.get("result")
    if not isinstance(result, dict):
        return True
    for key, value in result.items():
        if _EFFECT_RESULT_KEY_RE.match(key) and isinstance(value, (int, float)) and value > 0:
            return True
    return False


def _parse_gc_audit_timestamp(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 parse for a gc_audit ``created_at`` / purge
    marker ``ts`` string.

    Returns ``None`` on any parse failure or missing value — callers MUST
    treat that as "cannot verify recency", never as "recent enough"
    (RDR-129 B4: honest degradation, not a false clean).
    """
    if not value or value == "?":
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _check_gc_audit_non_empty_after_purge() -> list[HealthResult]:
    """After any local reap/purge activity, ``nexus.gc_audit`` must carry a
    row (nexus-sybbh: found completely empty on the live store — 0 rows, all
    tenants, all time — despite real purges having run, so RDR-192-class
    chunk losses become unattributable after the fact).

    Client-side half of nexus-sybbh: the engine-side writer bug (why nothing
    lands in gc_audit) is a separate, concurrently-owned fix. This check
    never assumes that fix has shipped — it degrades honestly regardless
    (RDR-129 B4 house style, same shape as :func:`_check_chash_conformance_
    report`, its closest sibling in this module):

    - No local evidence a purge ran recently -> named skip (``ok=True``,
      "nothing to cross-check"). This is NOT the same claim as "gc_audit is
      populated correctly" (nexus-kmo9h) — it is honestly silent about
      gc_audit because there is nothing to compare it against.
    - Catalog reader unavailable, or the engine predates the
      ``/gc_audit/list`` route (404), or any other read failure -> warn
      (``ok=False, warn=True``), never a silent/false clean pass.
    - Local evidence exists but EVERY marker in the window shows zero
      effect (:func:`_marker_has_effect`) -> named skip (``ok=True``):
      ``nexus.purge_trash``'s own audit INSERT only fires on a nonzero
      effect (catalog-033-1), so a genuinely no-op real purge writes no
      gc_audit row by design — expecting one there is a false alarm, not
      a defect (critique 2026-08-19 round 2).
    - Local evidence of a purge WITH nonzero effect exists AND gc_audit has
      no ``purge_trash`` row at or after the NEWEST such marker -> warn:
      the defect this check exists to catch. Cross-referencing is by
      ``operation="purge_trash"`` (server-side exact-match filter,
      ``CatalogRepository#listGcAudit``) PLUS a ``created_at`` >= the
      newest effectful marker's ``ts`` (within :data:`_GC_AUDIT_CLOCK_SKEW`)
      — an unfiltered/untimed check would false-clean forever the moment
      ANY gc_audit row exists, from ANY of the 4 producers, at ANY point in
      the past (critique 2026-08-19); anchoring on the OLDEST marker
      instead of the newest would let a stale row from an earlier, working
      audit paper over a writer regression later in the same window
      (critique 2026-08-19 round 2 — both are now fixed).
    - Local evidence of a purge with nonzero effect exists AND a matching,
      sufficiently-recent ``purge_trash`` row exists -> clean.

    The independent "did a purge run" signal is
    :mod:`nexus.gc_purge_marker`'s local breadcrumb file, written by
    ``nx catalog purge-trash`` on every REAL (``--no-dry-run --confirm``)
    execution — see that module's docstring for why gc_audit itself cannot
    be used as its own "did something happen" signal.
    """
    from nexus.gc_purge_marker import read_recent_purge_markers  # noqa: PLC0415 — deferred; rarely-hit branch

    label = _GC_AUDIT_NON_EMPTY_LABEL
    markers = read_recent_purge_markers(within_days=7)
    if not markers:
        return [HealthResult(
            label=label, ok=True,
            detail=(
                "skipped (no local `nx catalog purge-trash` execution "
                "recorded in the last 7 days — nothing to cross-check)"
            ),
        )]

    # critique 2026-08-19 round 2: a marker records every REAL purge
    # invocation regardless of effect, but the engine's own audit INSERT
    # is gated on nonzero effect (catalog-033-1) — cross-checking a
    # zero-effect marker against gc_audit is a guaranteed false alarm on
    # every healthy no-op purge, not a defect. Only markers with real
    # effect are eligible for the cross-check below.
    effective_markers = [m for m in markers if _marker_has_effect(m)]
    if not effective_markers:
        return [HealthResult(
            label=label, ok=True,
            detail=(
                f"skipped ({len(markers)} local `nx catalog purge-trash` "
                "execution(s) in the last 7 days, all zero-effect no-ops "
                "per their own recorded result — nothing to cross-check)"
            ),
        )]

    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return [HealthResult(
                label=label, ok=False, warn=True,
                detail="SKIPPED (no catalog reader configured) — not a clean-store signal",
            )]
    except Exception as exc:  # noqa: BLE001 — must not crash `nx doctor`, but must not lie either
        _log.warning("doctor_gc_audit_check_failed", error=str(exc))
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"SKIPPED (catalog reader unavailable: {exc}) — not a clean-store signal",
        )]

    import httpx  # noqa: PLC0415 — deferred to avoid a heavy/optional import at module load

    # Anchor on the NEWEST effectful marker, not the oldest (critique
    # 2026-08-19 round 2): with multiple purges inside the 7-day lookback,
    # anchoring on the oldest let a stale gc_audit row covering only the
    # earliest purge satisfy the check even if the writer regressed after
    # that and every later purge went unaudited.
    anchor_ts = max(m.get("ts", "?") for m in effective_markers)
    anchor_dt = _parse_gc_audit_timestamp(anchor_ts)
    try:
        # operation="purge_trash" — CatalogRepository#listGcAudit applies
        # this as an exact-match WHERE clause server-side, so limit=1
        # returns the newest purge_trash row specifically, not the newest
        # row of ANY operation (critique 2026-08-19: the latter false-
        # cleans forever once the routine sweep_superseded_chunks producer
        # fires, now wired for all 4 gc_audit producers).
        entries = cat.gc_audit_list(limit=1, operation="purge_trash")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 404:
            _log.warning("doctor_gc_audit_engine_floor", status=status)
            return [HealthResult(
                label=label, ok=False, warn=True,
                detail=(
                    "SKIPPED (engine predates the gc_audit/list route — "
                    "404'd; re-run after the next engine tag lands). This "
                    "is NOT a clean signal: local evidence shows "
                    f"{len(effective_markers)} purge-trash execution(s) with "
                    f"real effect since {anchor_ts}, unauditable until the "
                    "engine is upgraded."
                ),
            )]
        _log.warning("doctor_gc_audit_check_failed", error=str(exc))
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"SKIPPED (gc_audit/list failed: {exc})",
        )]
    except Exception as exc:  # noqa: BLE001 — must not crash `nx doctor`, but must not lie either
        _log.warning("doctor_gc_audit_check_failed", error=str(exc))
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"SKIPPED (gc_audit/list failed: {exc})",
        )]

    if entries:
        audit_dt = _parse_gc_audit_timestamp(entries[0].get("created_at"))
        recent_enough = (
            audit_dt is not None
            and anchor_dt is not None
            and audit_dt >= anchor_dt - _GC_AUDIT_CLOCK_SKEW
        )
        if recent_enough:
            return [HealthResult(
                label=label, ok=True,
                detail=(
                    f"{len(effective_markers)} local purge-trash execution(s) "
                    f"with real effect since {anchor_ts}; gc_audit carries a "
                    f"purge_trash row at {entries[0].get('created_at')} — audited"
                ),
            )]
        # A purge_trash row exists, but either its created_at could not be
        # verified against the marker (missing/unparseable — degrade
        # honestly, never assume clean) or it predates the newest effectful
        # local evidence (a stale row from an earlier purge, not this one).
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=(
                f"{len(effective_markers)} local `nx catalog purge-trash` "
                f"execution(s) with real effect since {anchor_ts}, but the "
                f"newest nexus.gc_audit purge_trash row "
                f"({entries[0].get('created_at')!r}) does not cover it — "
                "the reap/purge is NOT confirmed audited (nexus-sybbh; "
                "RDR-192-class chunk losses become unattributable after "
                "the fact). If the deployed engine already carries the "
                "gc_audit writer fix, this should clear on the next purge; "
                "otherwise the writer-side bug is still live."
            ),
            fix_suggestions=[
                "nx catalog purge-trash --no-dry-run --confirm   (re-run once the engine writer fix is deployed)",
                "nx doctor                                       (re-run; clears once gc_audit carries a row)",
            ],
        )]

    return [HealthResult(
        label=label, ok=False, warn=True,
        detail=(
            f"{len(effective_markers)} local `nx catalog purge-trash` "
            f"execution(s) with real effect since {anchor_ts}, but "
            "nexus.gc_audit is EMPTY — the reap/purge is NOT being audited "
            "(nexus-sybbh; RDR-192-class chunk losses become unattributable "
            "after the fact). If the deployed engine already carries the "
            "gc_audit writer fix, this should clear on the next purge; "
            "otherwise the writer-side bug is still live."
        ),
        fix_suggestions=[
            "nx catalog purge-trash --no-dry-run --confirm   (re-run once the engine writer fix is deployed)",
            "nx doctor                                       (re-run; clears once gc_audit carries a row)",
        ],
    )]


#: Threshold beyond which a document stranded in ``index_state='indexing'``
#: is worth flagging (nexus-5xn3k.6, bead-text amendment 2026-08-02 —
#: substantive-critic on .3's client diff). Generous by design: 'indexing'
#: is the SAFE state (nexus-lcmbp non-goal — it always means re-index, never
#: skip), so this check exists purely to bound how long a stuck/rolling-
#: deploy-split run sits unnoticed, not to police normal in-flight runs.
#:
#: PROVISIONAL (substantive-critic OBSERVATION, 2026-08-02, T2
#: nexus/5xn3k6-critique-2026-08-02 [21355]): 6h is not tied to any
#: measured p95/p99 MinerU extraction ceiling — no such data exists yet —
#: and the design memo does not name a number at all; this was picked ad
#: hoc from the bead-text amendment's "e.g. N hours" placeholder. Low
#: blast radius if wrong: this is a WARN-level doctor advisory, never a
#: gate, and its remedy (``nx index <path> --force``) is the same
#: idempotent, safe-to-over-run operation the fence design relies on
#: elsewhere — a false positive on a genuinely slow extraction costs one
#: unnecessary WARNING, not a wrong action taken automatically. Revisit
#: once a real extraction-time ceiling is observed. Not made
#: env-overridable: every existing numeric env-override in this codebase
#: (``NX_GC_FLOOR_FRACTION``, ``NX_INDEX_CONCURRENCY``, ...) is a
#: multi-line parse/clamp/log-on-invalid function, never a bare one-liner
#: cast — matching that idiom here would be new machinery for a
#: low-stakes advisory threshold, not a one-liner, so it's deferred with
#: this comment instead.
_STALE_INDEXING_THRESHOLD_HOURS = 6.0

#: v7.3.0 tag time (UTC) — the first PUBLIC release in which EVERY producer
#: stamps the index-run fence (nexus-vw594 F1, commit f55435eb). Used ONLY to
#: separate a document whose producer was legitimately unfenced when it was
#: written (permanent no-backfill debt — expected, needs no action) from a
#: genuine producer regression (a document indexed AFTER full coverage
#: shipped that still carries no stamp).
#:
#: DERIVATION — re-verify with git, never from memory or a bead's prose
#: (nexus-apig6 got this wrong TWICE from prose, in opposite directions):
#:     git tag --contains f55435eb --sort=creatordate | grep '^v' | head -1
#:     git log -1 --format=%cI v7.3.0        # -> 2026-08-07T09:00:35-07:00
#: and confirm the release BEFORE it does not contain the commit:
#:     git merge-base --is-ancestor f55435eb v7.2.0   # -> non-zero
#:
#: DO NOT move this back to v7.1.0's 2026-08-02T22:26Z. That is
#: the tag time of the FIRST client fence (nexus-5xn3k.3, commit 4b0c5fb5),
#: which called begin/complete at exactly 4 PDF/md/dt ingest sites covering
#: ~105 of 10,544 documents. The producers of the other 96% — the repo
#: indexer's ``_batch_flush``, ``store_put``, the code/prose indexers, memory,
#: MCP — were only fenced by f55435eb (2026-08-04), public at v7.3.0. Keyed on
#: v7.1.0 this check reported every document indexed in the intervening five
#: days as an unfenced-producer regression and prescribed a whole-corpus
#: re-embed to fix it; that false positive reached a downstream install, which
#: filed it as an open upstream bug (460 of 462 documents flagged). See the
#: investigation memo, T2
#: nx memory get -p nexus -t "vw594-investigation-2026-08-04".
#:
#: Nor forward to v7.5.0's 2026-08-09T22:45:30Z — apig6's first fix attempt
#: used that date on the unverified claim that coverage shipped there. It
#: does not: f55435eb is an ancestor of v7.3.0, two releases earlier
#: (code-review-expert, 2026-08-11). That anchor silently absorbed any real
#: regression in the 08-07..08-09 window into the "legacy, no action" bucket —
#: the same defect class this constant exists to close, merely narrower.
#:
#: vw594 ruled NO LEGACY BACKFILL as a design decision, so a corpus indexed
#: before this date reporting index_state=NULL on every row is the EXPECTED
#: permanent steady state, not drift.
_PRODUCER_FENCE_RELEASE_DT = datetime(2026, 8, 7, 16, 0, 35, tzinfo=UTC)

#: NOT the anchor on its own — a LOWER BOUND on it (nexus-oiu1t). No client
#: can have had full producer coverage before the release that carried it, so
#: no evidence-derived anchor may ever fall below this. See
#: ``_check_stale_indexing_runs`` for the install-local anchor this floors.

#: How many post-anchor unstamped identifiers are NAMED in the WARN detail.
_MAX_NAMED_UNSTAMPED = 10
#: Bound on the run-id ledger built during the walk (nexus-2sa6w).
#:
#: WHAT IT ACTUALLY COSTS (substantive-critic, 2026-08-11 — an earlier version
#: of this comment claimed the cap "only bites on a corpus stamped entirely
#: one-document-at-a-time", which understates it): the ledger fills in WALK
#: order, so ANY 5000 distinct run ids seen before a genuine multi-document
#: batch — including solo ids from interactive `nx store put` / `nx memory
#: put`, each minting its own uuid4 — cause that batch's proof to be dropped.
#: A heavily dogfooded install is a plausible way to hit this, not a
#: hypothetical one.
#:
#: It fails SAFE: losing the proof falls back to the weak anchor and its
#: hedged wording, never to a false accusation. It is no longer silent —
#: `ledger_note` below says so when the cap filled and nothing was proven.
_MAX_TRACKED_RUN_IDS = 5000

#: How many candidates are RETAINED during the corpus walk. The walk cannot
#: know the anchor until it finishes (the anchor is derived from the stamped
#: population), so candidates are gathered against the floor and filtered
#: after. Bounded so a fully-unstamped 10k-document corpus cannot grow a list
#: proportional to the corpus inside a diagnostic-only check; truncation is
#: reported explicitly, never silently.
_MAX_TRACKED_UNSTAMPED = 1000


def _check_stale_indexing_runs() -> list[HealthResult]:
    """Name documents stranded in ``index_state='indexing'`` beyond a
    threshold (nexus-5xn3k.6, bead-text amendment 2026-08-02 —
    substantive-critic on .3's client diff, T2 nexus/5xn3k3-critique-2026-08-02).

    DISTINCT AXIS from the now-retired ``_check_dangling_manifests``
    (memo §3.2/§4, ``manifest_verify_all`` — both RETIRED RDR-191 Phase 6,
    nexus-o8dil.33): that check found MISSING-CHUNK aggregates — it said
    nothing about a document whose fence was never cleared, and this check
    is independent of it either way. ``'indexing'`` is the correct, SAFE
    state for an in-flight or crashed run
    (memo §3.5 / nexus-lcmbp non-goal: a document in ``'indexing'`` always
    re-indexes, never silently skips) but nothing bounds how LONG it can sit
    there. A rolling engine deploy that straddles one multi-batch run's
    begin/complete pair (begin lands on an upgraded pod, complete 404s
    against a not-yet-upgraded pod) strands a document in ``'indexing'``
    until a FUTURE full re-index happens to route both calls through
    upgraded pods; every intervening ``nx index`` pass re-chunks and
    re-embeds it at full cost with no signal distinguishing "still catching
    up" from "stuck."

    Formerly surfaced ALONGSIDE the manifest_verify_all check, never folded
    into it — they detected different failure classes (missing chunks vs. a
    fence that never cleared). That sibling check no longer runs; this one
    is unaffected.

    Walks the full corpus once (``all_documents(limit=0)``) — the same cost
    class doctor already pays in ``_check_next_seq_drift`` (nexus-ohxzu).
    Read-only; degrades to a skip; never crashes the command it diagnoses.
    """
    label = "stale index-run fences"
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return [HealthResult(label=label, ok=True, detail="skipped (no catalog)")]
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_stale_indexing_check_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (catalog unavailable)")]

    now = datetime.now(UTC)
    stale: list[tuple[str, str]] = []  # (identifier, age)
    checked = 0
    # nexus-vw594 F3 (root cause of nexus-biq4x): a THIRD population,
    # distinct from both "checked" (real index_state, non-null) and the old
    # binary's silent skip. A row where the wire reported the
    # ``index_state`` key at all (``index_state_reported``) but its value
    # is NULL is NOT the same evidence as a key genuinely absent — see
    # CatalogEntry.index_state_reported's docstring.
    reported_null = 0
    not_reported = 0
    # nexus-apig6/oiu1t: candidate regressions — reported-but-NULL documents
    # indexed after the coverage FLOOR. Gathered against the floor because the
    # real anchor is not known until the walk ends; filtered against it after.
    candidates: list[tuple[str, datetime]] = []
    candidates_truncated = 0
    # nexus-oiu1t: the install's OWN evidence that it has run a fenced
    # producer — the earliest document carrying a real index_state. A fact
    # about THIS install, unlike a release-tag date, which assumes the user
    # upgraded the day the release shipped.
    #
    # A BARE stamp proves SOME producer was fenced, NOT that all were
    # (code-review-expert + substantive-critic, 2026-08-11; verified against
    # git). v7.1.0's fence (4b0c5fb5) covered 4 PDF/md/DEVONthink ingest
    # sites; the rest arrived at v7.3.0 (f55435eb), and no column records
    # which producer or client version stamped a row. So a client still on
    # v7.1.0/v7.2.x that runs one PDF ingest establishes a WEAK anchor while
    # its repo-index and store_put writes stay legitimately unfenced.
    #
    # nexus-2sa6w — the STRONG discriminator, read off the data rather than
    # inferred from a naming convention: `_fence_begin` mints uuid4() PER
    # DOCUMENT, while `_fence_begin_many` mints ONE run id shared across an
    # entire flush. A run id appearing on 2+ documents therefore proves the
    # writing client had the begin-many route, which exists ONLY from
    # f55435eb — i.e. FULL producer coverage. Evidence-positive only: a
    # single-document flush shares nothing either, so ABSENCE proves nothing
    # and falls back to the weak anchor and its hedged wording. Chosen over
    # mapping content_type to a producer family because THIS degrades safely —
    # if begin-many ever changes shape the evidence stops appearing and the
    # check gets more cautious, where a stale content_type map would keep
    # mis-attributing silently.
    earliest_stamped_dt: datetime | None = None
    # nexus-2sa6w: run id -> [count, earliest indexed_at seen for that run].
    run_ids: dict[str, list] = {}
    run_ids_dropped = 0
    # Reported-but-NULL with NO usable indexed_at. Cannot be attributed to
    # either side of the boundary — counted separately so the ok=True summary
    # never claims they predate coverage when nothing establishes that
    # (code-review-expert, 2026-08-11).
    undated_reported_null = 0
    try:
        # nexus-ft7eg: share this walk with _check_next_seq_drift
        # (_highest_child_seqs' identical `all_documents(limit=0)` scan) —
        # doctor currently pays for the full-corpus walk TWICE per run.
        for entry in cat.all_documents(limit=0):
            reported = bool(getattr(entry, "index_state_reported", True))
            state = getattr(entry, "index_state", None)
            if not reported:
                # Genuinely pre-fence engine — the wire never carried the
                # key. Unknown, not evidence either way.
                not_reported += 1
                continue
            if state is None:
                # Fence-aware engine, but this document has never been
                # stamped (unfenced producer, or simply not re-indexed
                # since full producer coverage shipped —
                # §_PRODUCER_FENCE_RELEASE_DT below tells these two apart).
                reported_null += 1
                indexed_at = str(getattr(entry, "indexed_at", "") or "")
                ia_dt = None
                if indexed_at:
                    try:
                        ia_dt = datetime.fromisoformat(indexed_at.replace("Z", "+00:00"))
                    except ValueError:
                        ia_dt = None
                if ia_dt is None:
                    undated_reported_null += 1
                elif ia_dt > _PRODUCER_FENCE_RELEASE_DT:
                    if len(candidates) < _MAX_TRACKED_UNSTAMPED:
                        ident = (
                            str(getattr(entry, "source_uri", "") or "")
                            or str(getattr(entry, "tumbler", "") or "")
                            or "?"
                        )
                        candidates.append((ident, ia_dt))
                    else:
                        candidates_truncated += 1
                continue
            checked += 1
            # nexus-oiu1t: a real index_state is proof that a fence ran on
            # this document.
            #
            # ONLY 'complete' rows may date that proof (code-review-expert,
            # 2026-08-11; verified in service/.../db/CatalogRepository.java).
            # beginIndexRun writes INDEX_STATE/INDEX_RUN_ID/INDEX_STARTED_AT
            # and completeIndexRun writes INDEX_STATE/INDEX_CONTENT_HASH/
            # CHUNK_COUNT — NEITHER writes INDEXED_AT, which only the
            # manifest-write/register path sets. So on a row stuck at
            # 'indexing' or 'failed' (a crashed or in-flight run), indexed_at
            # is a leftover from that document's PRIOR, unrelated successful
            # index. Dating either anchor from it puts the anchor earlier than
            # any evidence supports — and for the ledger it would let a
            # CRASHED begin-many batch assert a regression, unhedged, against
            # documents written before coverage was ever proven. Reopening the
            # over-claiming class through a new door.
            #
            # NOTE the guard is an `if`, NOT an early `continue`: the
            # stale-'indexing' detection below is this function's ORIGINAL
            # purpose and must still see every non-complete row. A `continue`
            # here silently disabled it (caught by
            # TestCheckStaleIndexingRuns::test_stale_indexing_document_is_reported).
            if state == "complete":
                stamped_at = str(getattr(entry, "indexed_at", "") or "")
                if stamped_at:
                    try:
                        st_dt = datetime.fromisoformat(stamped_at.replace("Z", "+00:00"))
                    except ValueError:
                        st_dt = None
                    if st_dt is not None:
                        if (earliest_stamped_dt is None
                                or st_dt < earliest_stamped_dt):
                            earliest_stamped_dt = st_dt
                        # nexus-2sa6w: ledger the run id so a SHARED one can
                        # prove begin-many, hence a full-coverage client.
                        rid = str(getattr(entry, "index_run_id", "") or "")
                        if rid:
                            slot = run_ids.get(rid)
                            if slot is not None:
                                slot[0] += 1
                                if st_dt < slot[1]:
                                    slot[1] = st_dt
                            elif len(run_ids) < _MAX_TRACKED_RUN_IDS:
                                run_ids[rid] = [1, st_dt]
                            else:
                                run_ids_dropped += 1
            if state != "indexing":
                continue
            started = getattr(entry, "index_started_at", "") or ""
            if not started:
                continue
            try:
                started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            except ValueError:
                continue
            age_hours = (now - started_dt).total_seconds() / 3600.0
            if age_hours >= _STALE_INDEXING_THRESHOLD_HOURS:
                ident = (
                    str(getattr(entry, "source_uri", "") or "")
                    or str(getattr(entry, "tumbler", "") or "")
                    or "?"
                )
                stale.append((ident, f"{age_hours:.1f}h"))
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_stale_indexing_scan_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (corpus scan failed)")]

    # nexus-vw594 F3 fix-round CRITICAL (substantive-critic, T2
    # nexus/vw594-critique-2026-08-05 [21445]): this check MUST run
    # UNCONDITIONALLY — never nested inside `if checked == 0`. The prior
    # shape nested the reported_null/vw594-signature detection inside
    # that guard, so a mixed corpus where ONE document has a genuine
    # fenced run (checked > 0 — e.g. the very first `nx index repo` post-
    # deploy) fell through to the generic "none (N checked)" ok=True
    # branch WITHOUT EVER INSPECTING reported_null, even when a SECOND
    # document in the same corpus was reported-but-NULL and indexed
    # after the fence shipped. That is nexus-biq4x's silent-green bug
    # reborn under a new trigger (checked > 0 instead of "engine predates
    # the fence"); the critic reproduced it against this function with a
    # real 2-doc mixed fixture. `results` collects every WARN-worthy
    # finding independently; the summary branches below only run when
    # NONE fired.
    results: list[HealthResult] = []

    # nexus-oiu1t: derive the anchor from THIS INSTALL's evidence, floored by
    # the release that first carried full coverage. A release-tag date alone
    # assumes the user upgraded the day it shipped; an install that upgraded
    # late wrote legitimately-unfenced documents well after the tag, and
    # anchoring on the tag reports every one of them as a producer regression
    # — the identical false positive nexus-apig6 was filed for, merely on a
    # later population. The floor still applies because no client can have had
    # full coverage before the release existed, so a stamp claiming to predate
    # it is clock skew, not evidence.
    # No floor is applied HERE: candidates were already gathered against
    # _PRODUCER_FENCE_RELEASE_DT during the walk, so every candidate is above
    # the floor by construction and an anchor below it cannot change any
    # outcome. (A max() here was written first and proven dead by its own kill
    # control — the walk-level floor subsumes it. The floor is pinned by
    # test_pre_coverage_document_is_never_a_candidate.)
    # nexus-2sa6w: prefer the PROVEN anchor — the earliest run that stamped 2+
    # documents, which only begin-many (v7.3.0+) can produce. It is always at
    # or after the weak anchor, so preferring it is strictly more conservative:
    # fewer documents are flagged, and the ones that are cannot be explained by
    # a partial-coverage client.
    proven_anchor: datetime | None = None
    for _count, _first_dt in run_ids.values():
        if _count >= 2 and (proven_anchor is None or _first_dt < proven_anchor):
            proven_anchor = _first_dt
    anchor = proven_anchor if proven_anchor is not None else earliest_stamped_dt
    post_anchor = [c for c in candidates if anchor is not None and c[1] > anchor]

    if post_anchor:
        # A document landed AFTER this install demonstrably had a fully-fenced
        # client and still carries no stamp — a NEW producer regression. Never
        # ok=True for this (nexus-biq4x's misdiagnosis was exactly rendering
        # this case as a green pre-fence skip).
        #
        # nexus-apig6: scoped to the post-anchor population and NAMED. The old
        # shape reported the whole ``reported_null`` count and prescribed an
        # unqualified `nx index <path> --force`, which on a corpus indexed
        # before v7.3.0 reads as "re-embed all 462 of your documents" —
        # contradicting vw594's own no-backfill decision.
        post_anchor_count = len(post_anchor)
        names = "; ".join(
            f"{ident} ({at.isoformat()})"
            for ident, at in post_anchor[:_MAX_NAMED_UNSTAMPED]
        )
        if post_anchor_count > _MAX_NAMED_UNSTAMPED:
            names += f"; +{post_anchor_count - _MAX_NAMED_UNSTAMPED} more"
        truncation_note = (
            f" (plus {candidates_truncated} candidate(s) beyond the "
            f"{_MAX_TRACKED_UNSTAMPED}-document tracking cap, not individually "
            "attributed)"
            if candidates_truncated else ""
        )
        # Truncated candidates were never anchor-filtered, so they are
        # UNATTRIBUTED — excluded from `legacy` rather than folded into
        # "predate the anchor, need no action", which would contradict the
        # truncation note in the same message (code-review-expert, 2026-08-11).
        legacy = (
            reported_null - post_anchor_count - undated_reported_null
            - candidates_truncated
        )
        legacy_note = (
            f" The other {legacy} reported-but-NULL document(s) predate "
            f"{anchor.isoformat()} and need no action (no legacy backfill by "
            "design)."
            if legacy > 0 else ""
        )
        undated_note = (
            f" {undated_reported_null} further reported-but-NULL document(s) "
            "carry no usable indexed_at and could fall on either side of that "
            "boundary — this check cannot attribute them."
            if undated_reported_null > 0 else ""
        )
        results.append(HealthResult(
            label=label, ok=False, warn=True,
            detail=(
                f"{post_anchor_count} document(s) report index_state but "
                "carry no stamp, despite being indexed after this install's "
                f"fence baseline ({anchor.isoformat()}, "
                + (
                    "a batched index run, which only a client with FULL "
                    "producer coverage can produce"
                    if proven_anchor is not None else
                    "its earliest stamped document"
                )
                + f"): {names}{truncation_note}. "
                + (
                    "A producer wrote these without calling index-run "
                    "begin/complete — a coverage regression worth finding. "
                    "(The one alternative left: a SECOND, older client also "
                    "writing to this corpus.)"
                    if proven_anchor is not None else
                    "Two explanations fit, and a bare stamp cannot "
                    "distinguish them: a producer is unfenced (a regression "
                    "worth finding), OR this install was running a client "
                    "whose fence covered only PDF/md/DEVONthink ingest "
                    "(v7.1.0-v7.2.x), leaving its repo-index and store_put "
                    "writes legitimately unstamped. Check the client version "
                    "first — it is the cheaper of the two."
                )
                + (
                    f" (Note: {run_ids_dropped} index-run id(s) exceeded this "
                    f"check's {_MAX_TRACKED_RUN_IDS}-run ledger and were not "
                    "examined, so proof of full coverage may have been missed "
                    "— this reading is the cautious one.)"
                    if run_ids_dropped and proven_anchor is None else ""
                )
                + f"{legacy_note}{undated_note}"
            ),
            fix_suggestions=(
                [
                    "nx index <path> --force   (re-index ONLY the named "
                    "document(s) above — this clears the symptom, not the "
                    "unfenced producer)",
                ] if proven_anchor is not None else [
                    "nx --version   (a client below 7.3.0 explains this with "
                    "no regression at all)",
                    "nx index <path> --force   (on a 7.3.0+ client, re-index "
                    "ONLY the named document(s) above — this clears the "
                    "symptom, not the unfenced producer)",
                ]
            ),
        ))
    elif anchor is None and (candidates or candidates_truncated):
        # nexus-oiu1t: post-floor unstamped documents exist, but NO document in
        # this corpus has ever been stamped — nothing establishes whether this
        # install has run a fully-fenced client at all. A late upgrader and a
        # genuinely unfenced producer are indistinguishable from here.
        #
        # This still WARNS. Declining to warn would hide a real producer
        # regression on an install that has never once stamped — the exact
        # shape the vw594 incident had. What nexus-apig6 was filed for was not
        # that a warning existed, but that it ASSERTED a producer bug it could
        # not prove and prescribed re-embedding the whole corpus; so this arm
        # states the ambiguity and prescribes ONE document.
        ambiguous_total = len(candidates) + candidates_truncated
        results.append(HealthResult(
            label=label, ok=False, warn=True,
            detail=(
                f"cannot attribute: {ambiguous_total} document(s) indexed "
                "after full producer coverage shipped "
                f"{_PRODUCER_FENCE_RELEASE_DT.date().isoformat()} carry no "
                "index-run stamp, and "
                + (
                    "no document in this corpus has ever been stamped"
                    if checked == 0 else
                    f"none of the {checked} stamped document(s) has a "
                    "COMPLETED run carrying a usable indexed_at (an "
                    "'indexing'/'failed' row's indexed_at belongs to its "
                    "previous index, not this run)"
                )
                + " — so no fence baseline can be established for this "
                "install. Either a producer is unfenced, or this install "
                "upgraded late and these are its own pre-coverage writes. "
                "Re-indexing ONE document tells them apart."
            ),
            fix_suggestions=[
                "nx index <path> --force   (re-index any ONE document to "
                "establish this install's fence baseline, then re-run doctor)",
            ],
        ))

    if stale:
        names = "; ".join(f"{ident} ({age})" for ident, age in stale[:10])
        if len(stale) > 10:
            names += f"; +{len(stale) - 10} more"
        results.append(HealthResult(
            label=label,
            ok=False,
            warn=True,
            detail=(
                f"{len(stale)} document(s) stranded in index_state='indexing' "
                f"beyond {_STALE_INDEXING_THRESHOLD_HOURS:.0f}h: {names}. A "
                "normal re-index run now clears this automatically, even "
                "when the document's content is unchanged (nexus-cp46b) — "
                "for a repo document that means its own repo's next "
                "`nx index repo` pass, no --force needed. If it keeps "
                "recurring, check for a stuck run or a rolling deploy that "
                "split a begin/complete pair across engine versions."
            ),
            fix_suggestions=[
                "nx index <path>   (a normal re-index clears the fence; "
                "--force is not required)",
            ],
        ))

    if results:
        return results

    # Nothing WARN-worthy found — build the single honest ok=True summary.
    if checked > 0:
        # nexus-oiu1t (code-review-expert, 2026-08-11): do NOT collapse to a
        # bare "none (N checked)" here. On a real corpus this is the COMMON
        # branch — a few stamped documents alongside a large pre-coverage
        # population — and dropping the reported_null/undated counts hides
        # exactly the state a downstream install misread as an upstream bug.
        extra = ""
        if reported_null:
            attributed = reported_null - undated_reported_null
            extra = (
                f"; {attributed} unstamped document(s) predate this install's "
                "fence baseline and need no action (no legacy backfill by "
                "design)"
                if attributed else ""
            )
            if undated_reported_null:
                extra += (
                    f"; {undated_reported_null} unstamped document(s) carry no "
                    "usable indexed_at and cannot be attributed"
                )
        return [HealthResult(
            label=label, ok=True,
            detail=f"none ({checked} fenced document(s) checked){extra}",
        )]
    if reported_null > 0:
        # Quiescent: the fence is live but nothing has run through it yet
        # (fresh install, or a stable corpus untouched since the fence
        # shipped — catalog-020 does not retro-populate by design).
        return [HealthResult(
            label=label, ok=True,
            detail=(
                f"fence live, 0 stale runs ({reported_null} document(s) "
                "report index_state but none has run through the fence yet — "
                f"{reported_null - undated_reported_null} indexed before full "
                "producer coverage shipped "
                f"{_PRODUCER_FENCE_RELEASE_DT.date().isoformat()}, not "
                "backfilled by design, no action needed"
                + (
                    f"; {undated_reported_null} carry no usable indexed_at "
                    "and cannot be attributed to either side of that date"
                    if undated_reported_null else ""
                )
                + ")"
            ),
        )]
    # NON-VACUITY: a genuinely pre-fence engine omits index_state entirely
    # on every row (not_reported > 0 and nothing else), or the corpus
    # scanned had nothing to say at all. Either way there is nothing this
    # check can assess — say so rather than render an all-clear it cannot
    # actually support.
    return [HealthResult(
        label=label, ok=True,
        detail="skipped (engine does not report index_state — predates "
               "the index-run fence)",
    )]


def _check_next_seq_drift() -> list[HealthResult]:
    """Name owners whose tumbler allocator has fallen BEHIND its own children
    (nexus-0ehwe item 4).

    THE WEDGE THIS SURFACES. ``registerDocument`` claims
    ``catalog_owners.next_seq`` and inserts; the INSERT's only ON CONFLICT
    arbiter is ``(tenant_id, source_uri)``, but the only unique key on
    ``catalog_documents`` is ``(tenant_id, tumbler)`` — so a tumbler collision
    has no arm and escapes as a bare 409. Pre-fix the counter's increment shared
    the failing transaction and rolled back WITH it, making one drifted owner a
    PERMANENT, TOTAL outage for that owner (nexus-pbawi, owner 1.12, fixed by
    hand).

    The engine now floors the claim past any drift, so a drifted owner
    SELF-HEALS on its next registration rather than wedging. This check exists
    because self-healing is silent: it reports which owners are ALREADY below
    their high-water mark, so the blast radius is known rather than guessed, and
    so an owner that never gets written to again does not sit drifted forever.

    Counts TOMBSTONED children: the ``(tenant_id, tumbler)`` PK does not exclude
    soft-deleted rows the way the partial ``source_uri`` index does, so a
    deleted document's tumbler is still taken.

    Read-only, per the RDR-185 rung shape. Degrades to a skip — a doctor check
    must never crash the command it is diagnosing.
    """
    label = "tumbler allocator drift"
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return [HealthResult(label=label, ok=True, detail="skipped (no catalog)")]
        # nexus-cw262 (round-3 critique, T2 21467 Significant-3): list_owners()
        # defaults to excluding deactivated owners now that the flag exists —
        # include_deactivated=True preserves this check's PRIOR coverage
        # (every owner, always) rather than silently narrowing it. Mirrors
        # CatalogRepository.sweepNextSeqDrift's own deliberate choice to keep
        # covering deactivated owners: "a deactivated owner can still receive
        # late-arriving writes via an explicit tumbler_prefix (e.g. ETL
        # replay)" — next_seq drift-floor visibility is orthogonal to owner
        # liveness, same as the engine-side sweep this check watches.
        owners = cat.list_owners(include_deactivated=True)
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_next_seq_check_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (catalog unavailable)")]

    if not owners:
        return [HealthResult(label=label, ok=True, detail="skipped (no owners)")]
    if not any("next_seq" in o for o in owners):
        # NON-VACUITY: an engine that predates nexus-0ehwe item 3 omits the
        # field entirely, and every owner would then read as drift-free. Say so
        # rather than render an all-clear this check cannot actually support.
        return [HealthResult(
            label=label, ok=True,
            detail="skipped (engine does not report next_seq — needs the "
                   "nexus-0ehwe engine change)",
        )]

    # ONE corpus pass for every owner at once. This loop called
    # _highest_child_seq (a full all_documents walk) PER OWNER — 65 owners x
    # ~22k documents = ~1.4M records over the managed API per doctor run,
    # measured at 218s of a 224s doctor (nexus-ohxzu). The max-seq for every
    # prefix falls out of a single walk.
    # One shared walk serves all owners, which also means one mid-walk
    # failure would blank drift visibility for EVERY owner (the old
    # per-owner walks failed independently). Retry once before conceding,
    # and say so at WARNING — a check with a total-outage history
    # (nexus-pbawi) must not vanish at debug level.
    highs: dict[str, int] | None = None
    for attempt in (1, 2):
        try:
            highs = _highest_child_seqs(cat)
            break
        except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
            _log.warning(
                "doctor_next_seq_scan_failed", attempt=attempt, error=str(exc),
            )
    if highs is None:
        return [HealthResult(
            label=label, ok=True,
            detail="skipped (corpus scan failed twice — drift not assessed "
                   "for ANY owner this run)",
        )]

    drifted: list[tuple[str, int, int]] = []
    checked = 0
    for owner in owners:
        prefix = str(owner.get("tumbler_prefix", ""))
        if not prefix or "next_seq" not in owner:
            continue
        try:
            next_seq = int(owner.get("next_seq") or 0)
            high = highs.get(prefix, 0)
        except Exception as exc:  # noqa: BLE001 — one unreadable owner must not end the sweep
            _log.debug("doctor_next_seq_owner_skipped", owner=prefix, error=str(exc))
            continue
        checked += 1
        # STRICTLY less-than. ``next_seq`` holds the LAST CLAIMED sequence, not
        # the next one to hand out: CatalogRepository.claimNextSeq computes
        # ``claim = max(next_seq, high_water) + 1`` and stores ``claim``, so
        # after every successful registration ``next_seq == highest child`` by
        # construction. Equality is the healthy steady state of every owner
        # that has ever been written to; only a counter that has fallen BELOW
        # its own high-water mark is drift, which is what this docstring says
        # and what the engine's own ``next_seq_drift_healed`` log keys on
        # (it fires when ``claim != next_seq + 1``, i.e. high_water > next_seq).
        #
        # This was ``<=`` (nexus-k5sdi), which flagged every healthy owner. It
        # went unnoticed because the check skipped entirely against engines that
        # did not report next_seq, and no test covered the equality boundary.
        if high and next_seq < high:
            drifted.append((prefix, next_seq, high))

    if checked == 0:
        return [HealthResult(label=label, ok=True, detail="skipped (no owner was readable)")]
    if not drifted:
        return [HealthResult(
            label=label, ok=True, detail=f"none ({checked} owner(s) checked)",
        )]

    names = "; ".join(
        f"{p} (next_seq={ns}, highest child={hi})" for p, ns, hi in drifted
    )
    return [HealthResult(
        label=label,
        ok=False,
        warn=True,
        detail=(
            f"{len(drifted)} owner(s) whose allocator is at or below their "
            f"highest existing tumbler: {names}. The engine floors past this on "
            "the next registration, so these self-heal when next written to."
        ),
        fix_suggestions=[
            "nx index <path>   (any registration into the owner floors it)",
        ],
    )]


def _highest_child_seqs(cat: Any) -> dict[str, int]:
    """Highest numeric child sequence per owner prefix, tombstones INCLUDED.

    One ``all_documents`` walk for ALL owners. The predecessor
    (``_highest_child_seq(cat, prefix)``) re-walked the full corpus per
    owner — O(owners x documents) over the managed API (nexus-ohxzu:
    218s of a 224s doctor on 65 owners x ~22k docs).
    """
    best: dict[str, int] = {}
    for entry in cat.all_documents(limit=0):
        tumbler = str(getattr(entry, "tumbler", "") or "")
        prefix, dot, tail = tumbler.rpartition(".")
        if not dot or not tail.isdigit():
            continue
        seq = int(tail)
        if seq > best.get(prefix, 0):
            best[prefix] = seq
    return best


def run_health_checks(git_hooks_scope: str | Path | None = None) -> tuple[list[HealthResult], bool]:
    """Run all health checks.

    ``git_hooks_scope``: forwarded to :func:`_check_git_hooks` (nexus-jds59)
    to restrict the git-hooks stanza-drift walk to repos at or under the
    given root. ``None`` (default) preserves the original behavior of
    walking every repo registered on the machine.

    Returns (results, is_local_mode).
    """
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []

    results.extend(_check_python())
    results.extend(_check_cli_version())
    results.extend(_check_generation_layout())
    results.extend(_check_process_skew())
    results.extend(_check_plugin_name())
    results.extend(_check_credential_persistence())
    results.extend(_check_mint_token())

    _local = is_local_mode()
    if _local:
        results.extend(_check_t3_local())
        results.extend(_check_service_bge_model())
        results.extend(_check_service_crossencoder_model())
    else:
        results.extend(_check_t3_cloud())

    # nexus-9tsdf (GH #1113 AC2): name dimension-orphaned collections and
    # point at `nx collection prune`. Applies in both modes; degrades
    # internally.
    results.extend(_check_dimension_orphans())
    # _check_dangling_manifests (nexus-5xn3k AC5) RETIRED here (RDR-191
    # Phase 6, nexus-o8dil.33) — the manifest-chunk FK makes the dangling
    # state it detected (a POPULATED manifest whose chashes no longer
    # resolve in T3) unreachable by construction.
    # T2 nexus/chroma-residue-plan-2026-08-10 §C2: the NULL-collection
    # population the FK does not cover (MATCH SIMPLE exempts it) — reported
    # explicitly, EXPLICITLY NOT RETIRED (RDR-191 Decision item 4's own
    # carve-out; see the check's own docstring).
    results.extend(_check_manifest_null_collection())
    # RDR-180 (bead nexus-du2dw): managed/cloud-mode chash width-conformance
    # coverage via the engine route — the local nexus_diag psql probe inside
    # _check_migration_state (above) is LOCAL-ONLY by design (nexus-y3wuu)
    # and permanently blind on installs with no direct substrate access.
    # Runs ALONGSIDE the local check (distinct label, tenant-scoped, never
    # fed into the install-binary/convergence gates — see the check's own
    # docstring for the scoping rationale).
    results.extend(_check_chash_conformance_report())
    # nexus-sybbh: independent local evidence a purge-trash ran recently,
    # cross-referenced against the engine's gc_audit table — degrades to a
    # named skip when there is no local evidence, never a false clean pass.
    results.extend(_check_gc_audit_non_empty_after_purge())
    # nexus-5xn3k.6 (bead-text amendment): a document's fence never
    # cleared — a different failure class from the missing-chunk aggregates
    # above (surfaced ALONGSIDE, not folded in).
    results.extend(_check_stale_indexing_runs())
    # nexus-0ehwe item 4: owners whose tumbler allocator has fallen behind
    # their own children. Self-healing is silent, so the blast radius must
    # be reportable rather than guessed.
    results.extend(_check_next_seq_drift())

    results.extend(_check_tools())
    results.extend(_check_mcp_entry_points())
    results.extend(_check_git_hooks(repo_scope=git_hooks_scope))
    results.extend(_check_index_log())
    results.extend(_check_orphan_t1_lease())
    results.extend(_check_orphan_t1_handoff())
    results.extend(_check_garbage())
    results.extend(_check_orphan_checkpoints())
    results.extend(_check_orphan_pipelines())
    results.extend(_check_mineru_server())
    results.extend(_check_t2_schema_applied())
    results.extend(_check_t2_dropped_writes())

    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.config import catalog_path  # noqa: PLC0415 — deferred to avoid circular import
    _cat_path = catalog_path()
    try:
        _cat = make_catalog_reader()
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        # Discovered via upgrade-shakeout.sh (10/12 FAIL) during the 6.1.0
        # release gate: unlike every sibling check in this function (chroma
        # pagination, storage-service health, migration state, RLS — all
        # explicitly "gated internally... always safe to run"), this call was
        # unguarded. In service mode with no reachable nexus-service (e.g. a
        # bare `nx doctor` before `nx daemon service start`),
        # resolve_service_config() raises RuntimeError uncaught, crashing the
        # entire doctor command instead of degrading like _check_catalog
        # already knows how to (cat=None -> "not initialized").
        _log.warning("doctor_catalog_reader_unavailable", error=str(exc))
        _cat = None
    results.extend(_check_catalog(_cat, _cat_path))

    # RDR-152 / bead nexus-gmiaf.33: storage-service checks.
    # All three are gated internally on pg_credentials being present; they emit
    # a single soft-warn-and-skip result when service/PG mode is not configured,
    # so they are always safe to run.
    results.extend(_check_storage_service_health())
    results.extend(_check_engine_convergence())
    results.extend(_check_t2_launchagent_stray())
    results.extend(_check_service_launchagent_stray())
    results.extend(_check_service_autostart_drift())
    results.extend(_check_migration_state())
    results.extend(_check_rls_present())
    # RDR-185 P0.4: read-only pending-rungs surface (degrades internally).
    results.extend(_check_pending_rungs())

    # RDR-155 P4b: the legacy chunk-id census, migration-report, and
    # migration-divergence doctor rows died with the migration machinery;
    # reports on disk remain as inert artifacts.

    # nexus-gynt2: stranded-install detector (disarmed no-op until the N+1
    # cut stamps LAST_MIGRATION_CAPABLE). A crash here must not take down
    # `nx doctor` — but unlike the best-effort checks above, a check
    # failure surfaces as a WARN, not a silent ok: this is the
    # data-loss-shaped class (no silent fallbacks for correctness).
    try:
        results.extend(_check_stranded_install())
    except Exception as exc:  # noqa: BLE001 — must not crash `nx doctor`; degraded to WARN, never silent-ok
        _log.warning("doctor_stranded_install_check_failed", error=str(exc))
        results.append(HealthResult(
            label="Stranded pre-PG install", ok=False, warn=True,
            detail=f"check failed ({exc}) — could not verify pre-PG data state",
        ))

    # nexus-aoqnb (GH #1419 Issue 4): label any orphaned catalog SQLite file
    # as a frozen migration source. Same WARN-on-failure posture as the
    # stranded check above and for the same reason — the guarded failure is
    # a human restoring from a stale-but-plausible store, so "could not
    # check" must never render as "nothing to see".
    try:
        results.extend(_check_catalog_legacy_file())
    except Exception as exc:  # noqa: BLE001 — must not crash `nx doctor`; degraded to WARN, never silent-ok
        _log.warning("doctor_catalog_legacy_check_failed", error=str(exc))
        results.append(HealthResult(
            label="Legacy catalog file", ok=False, warn=True,
            detail=f"check failed ({exc}) — could not verify legacy catalog state",
        ))

    # nexus-3xg21: plugin-floor check for the RDR-184 orchestration hooks —
    # the CLI is the only surface that can warn (a pre-floor plugin's own
    # hooks.json predates any warning hook). Best-effort.
    try:
        results.extend(_check_orchestration_hook_floor())
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.warning("doctor_orch_hook_floor_check_failed", error=str(exc))
        results.append(HealthResult(
            label="Orchestration hooks (plugin floor)", ok=True,
            detail="check failed (non-critical)",
        ))

    return results, _local
