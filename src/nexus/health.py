# SPDX-License-Identifier: AGPL-3.0-or-later
"""Health check data model and runner for nx doctor / nx console."""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import chromadb
import structlog

from nexus.config import default_db_path

if TYPE_CHECKING:
    from nexus.catalog import Catalog

_log = structlog.get_logger(__name__)

_CHECK = "✓"
_WARN = "✗"
# RDR-129 B4 (nexus-uq8a4): a third, soft state — the check could not complete
# but the condition is benign/transient (e.g. a healthy-but-busy database), so
# it renders distinctly from both a pass (✓) and a hard fail (✗) and never
# marks the run as failed.
_SOFT_WARN = "⚠"


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
            install_source,
        )

        report = detect_stale_processes()
    except Exception:  # noqa: BLE001 — probe failure must not fail doctor; skip silently
        return []
    if not report.stale:
        return [HealthResult(
            label="Process freshness",
            ok=True,
            detail=(
                f"all running conexus processes match the installed "
                f"{report.installed_version} (install source: "
                f"{install_source().split(' — ')[0]})"
            ),
        )]
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
        f"uv tool upgrade conexus    # → {latest}",
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
            fix_suggestions=[
                "Install the local extra and provision bge-768: nx init",
                "Or directly: pip install 'conexus[local]'",
            ],
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
    from nexus.config import _default_local_path  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []
    results.append(HealthResult(label="T3 mode", ok=True, detail="local (no API keys needed)"))
    # RDR-155 P4a.2 (nexus-1k8s1): the nexus-service serves T3 in local mode
    # too — probe it unconditionally (critique finding 2: a pgvector-only
    # install with the service down must not doctor all-green).
    results.append(_check_vector_service())

    local_path = _default_local_path()
    path_exists = local_path.exists()
    if path_exists:
        try:
            test_file = local_path / ".doctor_test"
            test_file.touch()
            test_file.unlink()
            results.append(HealthResult(label="Local ChromaDB path", ok=True, detail=str(local_path)))
        except OSError:
            results.append(HealthResult(
                label="Local ChromaDB path",
                ok=False,
                detail=f"{local_path} — not writable",
                fix_suggestions=[f"Check permissions on {local_path}"],
                fatal=True,
            ))
    else:
        results.append(HealthResult(
            label="Local ChromaDB path",
            ok=True,
            detail=f"{local_path} (will be created on first index)",
        ))

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

    # Collection count + legacy-store disk usage.
    #
    # RDR-155 P4a.2 (nexus-1k8s1): the T3-daemon probe is retired with the
    # Chroma serving path — T3 serving routes through the pgvector-backed
    # nexus-service, so the collection census queries it via ``make_t3()``.
    # The on-disk Chroma directory is reported as the LEGACY store awaiting
    # the Phase-5 ETL (its deletion is Phase 4b, gated on P5.G).
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
        detail = f"{col_count} collections (pgvector service)"
        if path_exists:
            total_bytes = sum(f.stat().st_size for f in local_path.rglob("*") if f.is_file())
            if total_bytes < 1024 * 1024:
                size_str = f"{total_bytes / 1024:.1f} KB"
            else:
                size_str = f"{total_bytes / (1024 * 1024):.1f} MB"
            detail += f"; legacy Chroma store {size_str} on disk (awaiting P5 ETL)"
        results.append(HealthResult(
            label="T3 collections", ok=True,
            detail=detail,
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
        _log.debug("vector_service_not_reachable", error=str(exc))
        # nexus-4m6i0.7: the service can crash-loop before answering any
        # request (a Liquibase VALIDATE failure on boot, GH #1390) — surface
        # the root cause from the local service log when one is available,
        # instead of leaving the operator to spelunk storage_service_native.log
        # by hand. Strictly best-effort/soft: any failure here degrades
        # silently back to the bare "not reachable" message.
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
    # P4a.2 — make_t3() is service-backed unconditionally), so these
    # ChromaDB/Voyage client credentials are MIGRATION-SOURCE config.
    # DELIBERATE TRADEOFF, disclosed: a pre-/mid-migration install with
    # absent source creds doctors clean here and learns about the gap from
    # the migration command itself (the ETL open_read_legs checks fail
    # loud) — a wrong-exit-1 on every migrated install was the worse
    # failure mode.
    _absent_detail = (
        "not set (migration-source only — needed by the legacy-store "
        "migration ETL, not for serving)"
    )

    # CHROMA_API_KEY
    chroma_key = get_credential("chroma_api_key")
    results.append(HealthResult(
        label="ChromaDB  (CHROMA_API_KEY)",
        ok=True,
        detail="set" if chroma_key else _absent_detail,
    ))

    # CHROMA_TENANT (optional)
    chroma_tenant = get_credential("chroma_tenant")
    results.append(HealthResult(
        label="ChromaDB  (CHROMA_TENANT)",
        ok=True,
        detail=chroma_tenant if chroma_tenant
        else "not set (auto-inferred from API key — set explicitly only for multi-workspace)",
    ))

    # CHROMA_DATABASE
    chroma_database = get_credential("chroma_database")
    results.append(HealthResult(
        label="ChromaDB  (CHROMA_DATABASE)",
        ok=True,
        detail=chroma_database if chroma_database else _absent_detail,
    ))

    # Vector-serving reachability is probed UNCONDITIONALLY at the top of
    # this function via _check_vector_service() (RDR-155 P4a.2): the direct
    # ChromaDB Cloud probe retired with the serving path, and the service
    # probe must not be gated on legacy ChromaCloud credential presence.
    # The ChromaCloud credentials above still matter to ONE consumer: the
    # Phase-5 migration ETL reads the legacy store through them. Their
    # absence is deliberately non-fatal here (see the tradeoff note above).

    # VOYAGE_API_KEY — server-side embedding on the service path; the
    # client key is migration/enrichment config (e.g. rerank soft-degrades
    # without it), not a serving requirement.
    voyage_key = get_credential("voyage_api_key")
    results.append(HealthResult(
        label="Voyage AI (VOYAGE_API_KEY)",
        ok=True,
        detail="set" if voyage_key else _absent_detail,
    ))

    # Pipeline version check. Without the legacy source creds the line
    # must still appear — as "retired", not vanish (reviewer-c7aj3 Medium).
    if not (chroma_key and chroma_database and voyage_key):
        results.append(HealthResult(
            label="pipeline versions",
            ok=True,
            detail="sweep retired with the Chroma serving path (RDR-155 P4a)",
        ))
    elif chroma_key and chroma_database and voyage_key:
        from nexus.indexer import PIPELINE_VERSION, get_collection_pipeline_version  # noqa: PLC0415 — deferred to avoid circular import

        # RDR-155 P4a.2 (nexus-1k8s1): the sweep reads Chroma COLLECTION
        # metadata (the pipeline_version stamp), which has no pgvector
        # equivalent — collection is a column, not an object with metadata.
        # On the service-backed handle the sweep is retired, not "failed";
        # pgvector-side staleness tracking is a P5 ETL concern.
        from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid circular import
        from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred to avoid circular import

        # nexus-b6qlf regression: make_t3() (via get_http_vector_client())
        # now runs a cloud-mode engine-version probe -- every OTHER make_t3()
        # call site in this module already degrades gracefully on failure
        # (_check_t3_local's "T3 collections" census, _check_managed_service_
        # probe); this was the one unguarded call, so a probe failure used to
        # crash the entire `nx doctor` run instead of reporting a soft-fail
        # line like its siblings.
        try:
            t3_handle = make_t3()
        except Exception as exc:  # noqa: BLE001 — diagnostic: report unavailability, never crash doctor
            _log.debug("doctor_pipeline_version_t3_unavailable", error=str(exc))
            results.append(HealthResult(
                label="pipeline versions", ok=False, warn=True,
                detail=f"T3 unavailable ({exc}) — see the Managed/remote service check above.",
            ))
            return results
        if is_service_backed(t3_handle):
            results.append(HealthResult(
                label="pipeline versions", ok=True,
                detail="sweep retired with the Chroma serving path (RDR-155 P4a)",
            ))
            return results

        stale_count = 0
        pipeline_results: list[HealthResult] = []
        try:
            client = t3_handle._client
            cols = client.list_collections()
            for col in cols:
                # taxonomy__* collections are BERTopic aggregates (RDR-070),
                # not indexer outputs — PIPELINE_VERSION does not apply.
                if col.name.startswith("taxonomy__"):
                    continue
                stored = get_collection_pipeline_version(col)
                if stored is None:
                    pipeline_results.append(HealthResult(
                        label=f"pipeline ({col.name})", ok=True,
                        detail="no version stamp (next 'nx index repo' will stamp)",
                    ))
                elif stored != PIPELINE_VERSION:
                    stale_count += 1
                    pipeline_results.append(HealthResult(
                        label=f"pipeline ({col.name})", ok=False,
                        detail=f"v{stored} (current: v{PIPELINE_VERSION})",
                    ))
                else:
                    pipeline_results.append(HealthResult(
                        label=f"pipeline ({col.name})", ok=True, detail=f"v{stored}",
                    ))
        except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
            _log.debug("doctor_pipeline_check_failed", db=chroma_database, error=str(exc))
            pipeline_results.append(HealthResult(
                label=f"pipeline ({chroma_database})", ok=False, detail="check failed",
            ))

        # Add fix suggestions to the last pipeline result if stale
        if stale_count and pipeline_results:
            pipeline_results[-1].fix_suggestions = [
                "nx index repo <path> --force-stale  (re-index outdated collections)",
                "nx index repo <path> --force        (re-index all collections)",
            ]
            pipeline_results[-1].fatal = True

        results.extend(pipeline_results)

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


def _check_git_hooks() -> list[HealthResult]:
    # nexus-8g79.10 (V2): import from the lower-layer module instead of
    # reaching up into commands/. Use module-attribute access so test
    # monkeypatches on ``nexus._git_hooks_meta.effective_hooks_dir``
    # reach the live binding at call time.
    import re  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
    from nexus import _git_hooks_meta as _ghm  # noqa: PLC0415 — deferred to avoid circular import
    from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END  # noqa: PLC0415 — deferred to avoid circular import
    _effective_hooks_dir = _ghm.effective_hooks_dir
    from nexus.catalog.catalog import Catalog  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.config import catalog_path, nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.repos import list_repos_dual  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []
    hook_names = ("post-commit", "post-merge", "post-rewrite")
    registry_path = nexus_config_dir() / "repos.json"

    # nexus-mkj6u shakeout: extract the canonical stanza from the
    # current template so we can detect drift in already-installed
    # hooks (e.g. the pre-pgrep-guard stanza). Done once per call;
    # the import is lazy because commands/hooks.py imports click
    # which we don't want to pay for at health-check time when no
    # repos are registered.
    def _canonical_stanza_body() -> str | None:
        try:
            from nexus.commands.hooks import _STANZA  # noqa: PLC0415 — deferred to avoid circular import
        except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
            return None
        m = re.search(
            rf"{re.escape(SENTINEL_BEGIN)}\n(.*?)\n{re.escape(SENTINEL_END)}",
            _STANZA, re.DOTALL,
        )
        return m.group(1) if m else None

    def _installed_stanza_body(content: str) -> str | None:
        m = re.search(
            rf"{re.escape(SENTINEL_BEGIN)}\n(.*?)\n{re.escape(SENTINEL_END)}",
            content, re.DOTALL,
        )
        return m.group(1) if m else None

    canonical = _canonical_stanza_body()

    # RDR-137 Phase 3.1 (nexus-tts0d.6): catalog-backed enumeration with
    # legacy ``repos.json`` fallback via the dual-read shim. Catalog
    # paths come from ``owners WHERE owner_type='repo'``; the registry
    # provides legacy installs that have not yet been re-indexed.
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import
        cat = make_catalog_reader()
        repos = list_repos_dual(cat=cat, registry_path=registry_path)
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
        # RDR-137 followup IMP-20 (nexus-43qgm.20): exc_info=True so
        # the operator sees the traceback alongside the error message
        # (NameError / AttributeError otherwise appear only as the
        # rendered str(exc) with no source location).
        _log.warning(
            "doctor_registry_load_failed", error=str(exc), exc_info=True,
        )
        repos = []

    if not repos:
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
                    drifted: list[str] = []
                    if canonical is not None:
                        for name in installed:
                            installed_body = _installed_stanza_body(
                                (hdir / name).read_text()
                            )
                            if installed_body is not None and installed_body != canonical:
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
            except Exception:  # noqa: BLE001 — git-hook probe is best-effort; degrade to 'could not check'
                results.append(HealthResult(
                    label="git hooks", ok=True,
                    detail=f"{repo_path} — could not check",
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
    return [HealthResult(
        label="index log", ok=True,
        detail=f"{path} ({kind}, last write: {_age_str(mtime)})",
    )]


def _check_orphan_t1() -> list[HealthResult]:
    """Report on T1 lease records on disk (RDR-149 P4 leased registry).

    T1 publishes a leased registry record at
    ``~/.config/nexus/t1_addr.<session_id>`` (re-keyed from a transient
    ``server_pid`` key at cold start). Liveness is lease freshness (TTL),
    not pid: a dead owner's lease ages out on its own, so there is no
    bespoke orphan sweep (RDR-149 P5 removed it). This check surfaces any
    stale (expired) lease record still on disk; such records are inert
    (readers reap them on discovery), so removal is cosmetic.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
    from nexus.daemon.service_registry import LeaseRecord  # noqa: PLC0415 — deferred to avoid circular import

    config_dir = nexus_config_dir()
    if not config_dir.exists():
        return [HealthResult(label="T1 sessions", ok=True, detail="no nexus config dir")]

    addr_files = list(config_dir.glob("t1_addr.*"))
    if not addr_files:
        return [HealthResult(label="T1 sessions", ok=True, detail="no live T1 sessions")]

    now = time.time()
    fresh: list[str] = []
    stale: list[str] = []
    legacy: list[str] = []
    for path in addr_files:
        try:
            record = LeaseRecord.from_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            # Not a lease record: a pre-P4 ``host:port`` addr file left on
            # disk by an older version (RDR-149 P4 changed the format). Inert
            # -- nothing reads it -- but surfaced so it is not silently
            # invisible after a "no bespoke copies" audit.
            _log.debug("t1_lease_unparseable", path=str(path))
            legacy.append(path.name)
            continue
        if record.is_fresh(now):
            age_s = max(0, int(now - record.heartbeat_epoch))
            fresh.append(f"{path.name} (fresh, last heartbeat {age_s}s ago)")
        else:
            stale.append(path.name)

    if stale:
        return [HealthResult(
            label="T1 sessions",
            ok=False,
            detail=f"{len(stale)} stale T1 lease(s) (expired past TTL): {', '.join(stale)}",
            fix_suggestions=[
                "Stale leases are inert (readers reap on discovery); removal is cosmetic.",
                "Remove them anyway: rm ~/.config/nexus/t1_addr.*",
            ],
        )]

    if legacy and not fresh:
        return [HealthResult(
            label="T1 sessions", ok=True,
            detail=f"no live T1 sessions ({len(legacy)} inert pre-P4 addr file(s) on disk)",
            fix_suggestions=["Remove inert legacy files: rm ~/.config/nexus/t1_addr.*"],
        )]

    if not fresh:
        return [HealthResult(label="T1 sessions", ok=True, detail="no live T1 sessions")]

    detail = f"{len(fresh)} live T1 lease(s): {', '.join(fresh)}"
    if legacy:
        detail += f" (+{len(legacy)} inert pre-P4 addr file(s))"
    return [HealthResult(label="T1 sessions", ok=True, detail=detail)]


def _check_t3_daemon_version() -> list[HealthResult]:
    """Flag a CLI-vs-T3-daemon version mismatch (RDR-149, nexus-ymn76).

    The supervised T3 daemon stamps its conexus version in its lease record
    (RDR-149 P3). After a CLI upgrade the version-skew cycle restarts it (the
    #1112 fix), but until that fires — or if it failed — the daemon keeps
    serving the old binary. This is the operator-visible counterpart to that
    structural fix: it surfaces the mismatch as a soft warning (the daemon
    still works, it is merely stale). Local mode only; cloud T3 has no daemon.
    """
    from importlib.metadata import version as _pkg_version  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    from nexus.daemon.discovery import find_t3_daemon  # noqa: PLC0415 — deferred to avoid circular import

    try:
        cli_version = _pkg_version("conexus")
    except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return []  # installed version unknown — nothing to compare against

    daemon = find_t3_daemon()
    if daemon is None:
        return [HealthResult(
            label="T3 daemon version", ok=True, detail="no T3 daemon running"
        )]
    daemon_version = daemon.get("version")
    if not daemon_version:
        return [HealthResult(
            label="T3 daemon version", ok=True,
            detail="T3 daemon lease carries no version (pre-RDR-149 daemon?)",
        )]
    if daemon_version == cli_version:
        return [HealthResult(
            label="T3 daemon version", ok=True,
            detail=f"{daemon_version} (matches CLI)",
        )]
    return [HealthResult(
        label="T3 daemon version", ok=False, warn=True,
        detail=(
            f"T3 daemon is running {daemon_version} but the CLI is "
            f"{cli_version} (stale daemon — serving the old binary)"
        ),
        fix_suggestions=[
            "Restart the T3 daemon to pick up the new version: "
            "nx daemon t3 stop && nx daemon t3 start",
            "nx upgrade cycles supervised daemons automatically; a mismatch "
            "here means the version-skew cycle has not run yet or failed.",
        ],
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
    """nexus-h1jk: surface MinerU server reachability in the default
    doctor flow.

    Math-heavy PDFs (papers with dense formula notation) accumulate per-
    page tensor state in MinerU's formula-detection pass and routinely
    OOM-kill the in-process subprocess fallback. The HTTP server avoids
    that by running MinerU as a long-lived dedicated worker. The
    configured URL silently goes stale: ``_restart_mineru_server`` in
    ``pdf_extractor.py`` writes the live port to
    ``~/.config/nexus/config.yml`` after a mid-run recovery, but if
    that server later dies the URL points at a dead port across every
    subsequent session. ``nx doctor`` is the natural place to surface
    that drift.
    """
    from nexus.config import get_mineru_server_url, mineru_server_provisioned  # noqa: PLC0415 — heavy/optional dependency deferred to call time
    import httpx as _httpx  # noqa: PLC0415 — heavy/optional dependency deferred to call time

    try:
        # nexus-9xfx5 (reviewer-3modes H1): never probe the built-in default
        # URL on a box where no server was ever provisioned — every fresh
        # install rendered a red ✗ ("unreachable ... OOM-risk") in the
        # DEFAULT doctor flow. Unprovisioned → no result row (MinerU is
        # opt-in); a ✗ now means a PROVISIONED server went stale — exactly
        # the drift this check exists to surface.
        if not mineru_server_provisioned():
            return []
        url = get_mineru_server_url()
    except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return []
    if not url:
        return []

    health_url = f"{url}/health"
    try:
        resp = _httpx.get(health_url, timeout=2.0)
    except (_httpx.ConnectError, _httpx.TimeoutException) as exc:
        return [HealthResult(
            label="MinerU server",
            ok=False,
            detail=(
                f"{url} unreachable ({type(exc).__name__}); falling back to "
                "in-process subprocess on math PDFs (OOM-risk)"
            ),
            fix_suggestions=[
                "Start the server: nx mineru start",
                f"Or confirm the URL in ~/.config/nexus/config.yml "
                f"(currently: {url})",
            ],
        )]
    if resp.status_code != 200:
        return [HealthResult(
            label="MinerU server",
            ok=False,
            detail=f"{url} returned HTTP {resp.status_code}",
            fix_suggestions=["Restart the server: nx mineru stop && nx mineru start"],
        )]
    return [HealthResult(
        label="MinerU server",
        ok=True,
        detail=f"reachable at {url}",
    )]


# RDR-129 B4 (nexus-uq8a4): the FTS5 integrity probe
# (``INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')``) is a
# *write* — it needs ``memory.db``'s single WAL writer slot. A legitimate
# concurrent writer (typically an active ``nx index repo``) holds that slot,
# and the probe would block to ``busy_timeout`` and then report a hard red X
# for a database that is perfectly healthy, just busy. We give each attempt a
# bounded ``busy_timeout`` and retry briefly; on continued contention we emit a
# SOFT WARN (the DB is fine) rather than a hard failure. A genuine corruption
# (a non-lock error, or a failing ``PRAGMA integrity_check``) still hard-fails.
_INTEGRITY_BUSY_TIMEOUT_MS: int = 2000
_INTEGRITY_RETRY_SLEEPS_BETWEEN: tuple[float, ...] = (0.25, 0.5)


def _is_lock_error(exc: BaseException) -> bool:
    """True when *exc* is transient writer-slot contention, not corruption.

    Mirrors the discriminator in ``nexus.db.t2._apply_pending_with_lock_retry``:
    ``database is locked`` / ``database is busy`` (including the
    ``SQLITE_BUSY_SNAPSHOT`` variant, whose message also contains "locked").
    """
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _check_t2_integrity() -> list[HealthResult]:
    import time  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    db_path = default_db_path()
    if not db_path.exists():
        return [HealthResult(label="T2 integrity", ok=True, detail="not created yet")]

    try:
        conn = sqlite3.connect(str(db_path))  # epsilon-allow: health PRAGMA integrity_check diagnostic — must operate when daemon offline; read-only
        try:
            conn.execute(f"PRAGMA busy_timeout = {_INTEGRITY_BUSY_TIMEOUT_MS}")
            rows = conn.execute("PRAGMA integrity_check").fetchall()
            pragma_ok = len(rows) == 1 and rows[0][0] == "ok"
            if not pragma_ok:
                issues = "; ".join(r[0] for r in rows[:3])
                return [HealthResult(label="T2 integrity", ok=False, detail=f"PRAGMA: {issues}")]

            # FTS5 integrity probe — a write that takes the WAL writer slot.
            # Retry on transient lock contention; a non-lock error is genuine
            # FTS5 corruption and must hard-fail immediately.
            sleeps = _INTEGRITY_RETRY_SLEEPS_BETWEEN
            max_attempts = len(sleeps) + 1
            fts_ok = False
            for attempt in range(1, max_attempts + 1):
                try:
                    conn.execute(
                        "INSERT INTO memory_fts(memory_fts) VALUES('integrity-check')"
                    )
                    fts_ok = True
                    break
                except sqlite3.OperationalError as exc:
                    if not _is_lock_error(exc):
                        return [HealthResult(label="T2 integrity", ok=False, detail=f"FTS5: {exc}")]
                    # Clear any partial transaction so the retry re-reads a
                    # fresh snapshot (handles SQLITE_BUSY_SNAPSHOT too).
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    if attempt == max_attempts:
                        # Transient writer-lock contention, not corruption.
                        # Stays SOFT by design — and *stays* soft even after
                        # RDR-129's single-daemon enforcement ships: a lock
                        # here post-P2 indicates a single-daemon invariant
                        # violation (a second daemon, or a direct writer
                        # bypassing the daemon), which the A3 daemon census
                        # reports as a hard error. The two are complementary;
                        # keeping B4 soft means the drop metric is never lost
                        # to a hard fail. Do NOT flip this to a hard failure
                        # without understanding that relationship (RDR-129 §B4).
                        return [HealthResult(
                            label="T2 integrity",
                            ok=False,
                            warn=True,
                            detail="FTS5: busy (write in progress, retry)",
                        )]
                    time.sleep(sleeps[attempt - 1])
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return [HealthResult(label="T2 integrity", ok=False, detail=f"could not open: {exc}")]

    if pragma_ok and fts_ok:
        return [HealthResult(label="T2 integrity", ok=True, detail="PRAGMA ok, FTS5 ok")]
    return [HealthResult(label="T2 integrity", ok=False, detail="check failed")]


def _check_t2_dropped_writes() -> list[HealthResult]:
    """Surface the dropped-best-effort-write meter (RDR-129 B4, nexus-uq8a4).

    RDR-187 (nexus-piwya.4): the meter's only-ever producer — the chash
    dual-write hook — is retired, so the count can no longer grow. A
    nonzero count is therefore HISTORICAL evidence (drops that happened
    before the writer was retired), reported ok=True with the number
    visible: a frozen soft-WARN whose last_ts can never advance would
    nag forever about a writer that no longer exists, and a permanently
    green "no drops" would silently hide the history. If a future
    best-effort writer adopts record_drop(), restore the soft-WARN
    posture for its records.
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

    detail = (
        f"{summary.total} historical drop(s) under lock contention "
        f"({summary.rows} rows) from the retired chash dual-write hook "
        f"(writer retired by RDR-187; count frozen)"
    )
    if summary.last_ts:
        detail += f", last {summary.last_ts}"
    return [HealthResult(
        label="T2 best-effort writes",
        ok=True,
        detail=detail,
    )]


def _check_t2_daemon_singleton() -> list[HealthResult]:
    """Fail loud when more than one T2 daemon serves the same db (RDR-129 A3,
    nexus-exa2p). Exactly one daemon per ``memory.db`` is the single-writer
    invariant; two daemons contend on the WAL and produce the ``FTS5: database
    is locked`` flicker. This is the **hard** census error, complementary to
    the soft live-contention signal in ``_check_t2_integrity``: A1/A2 enforce
    single occupancy, A3 makes a residual violation observable instead of
    silent. Names the offending pids so an operator can act without reading
    code.
    """
    db_path = default_db_path()
    if not db_path.exists():
        return [HealthResult(
            label="T2 daemon singleton", ok=True, detail="no T2 database yet",
        )]
    try:
        from nexus.daemon.t2_daemon import _enumerate_t2_daemon_pids_for_db  # noqa: PLC0415 — deferred to avoid circular import

        pids = sorted(set(_enumerate_t2_daemon_pids_for_db(db_path)))
    except Exception as exc:  # pragma: no cover — defensive  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        # Absence of evidence is not evidence of multiplicity: a failed probe
        # must not flip doctor red.
        return [HealthResult(
            label="T2 daemon singleton", ok=True, detail=f"probe unavailable: {exc}",
        )]

    if len(pids) <= 1:
        detail = "1 daemon" if pids else "no daemon running"
        return [HealthResult(label="T2 daemon singleton", ok=True, detail=detail)]

    pid_list = ", ".join(str(p) for p in pids)
    return [HealthResult(
        label="T2 daemon singleton",
        ok=False,
        fatal=True,
        detail=(
            f"{len(pids)} daemons for {db_path.name} (pids: {pid_list}); "
            f"single-writer invariant violated"
        ),
        fix_suggestions=[
            "two T2 daemons are contending on the same memory.db (RDR-129 A3). "
            "Stop the extras: `nx daemon t2 stop`, then "
            "`nx daemon t2 ensure-running` to leave exactly one",
        ],
    )]


def _check_chroma_pagination(client: object, db_name: str) -> list[HealthResult]:
    try:
        cols = client.list_collections()  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return [HealthResult(
            label=f"ChromaDB pagination ({db_name})", ok=False, detail=f"list failed: {exc}",
        )]

    target_col = None
    for col in cols:
        try:
            if col.count() > 0:
                target_col = col
                break
        except Exception:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
            continue

    if target_col is None:
        return [HealthResult(
            label=f"ChromaDB pagination ({db_name})", ok=True,
            detail="no non-empty collections to audit",
        )]

    try:
        expected = target_col.count()
        retrieved = 0
        offset = 0
        page_size = 300
        while True:
            batch = target_col.get(limit=page_size, offset=offset, include=[])
            ids = batch.get("ids", [])
            retrieved += len(ids)
            if len(ids) < page_size:
                break
            offset += page_size

        ok = retrieved == expected
        detail = f"{target_col.name}: count={expected}, paginated={retrieved}"
        return [HealthResult(label=f"ChromaDB pagination ({db_name})", ok=ok, detail=detail)]
    except Exception as exc:  # noqa: BLE001 — boundary fallback — degrade gracefully on unexpected error
        return [HealthResult(
            label=f"ChromaDB pagination ({db_name})", ok=False, detail=f"audit failed: {exc}",
        )]


def _check_catalog(cat: "Catalog | None", cat_path: "Path") -> list[HealthResult]:
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
    ``CHROMA_API_KEY`` / ``VOYAGE_API_KEY`` are in ``.zshrc`` exports
    but never persisted via ``nx config set``, the GUI-spawned
    subprocess sees them as absent, ``is_local_mode()`` flips to True,
    and T3 dispatch goes to the daemon path that fails opaquely.

    This check runs on the CLI side (where shell env IS visible) and
    surfaces the gap before the GUI-spawn path hits it. Non-fatal: a
    warning, not a blocker, because the CLI itself works fine.

    Returns an empty list when the configuration is consistent (both
    persisted, neither set, or no env exports).
    """
    from nexus.config import _global_config_path  # noqa: PLC0415 — deferred to avoid circular import

    cloud_keys = ("chroma_api_key", "voyage_api_key", "chroma_tenant", "chroma_database")
    env_names = {
        "chroma_api_key": "CHROMA_API_KEY",
        "voyage_api_key": "VOYAGE_API_KEY",
        "chroma_tenant": "CHROMA_TENANT",
        "chroma_database": "CHROMA_DATABASE",
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

    # Surface the most-load-bearing pair first; chroma_tenant /
    # chroma_database are derived/configuration rather than identity.
    suggestions = [f"nx config set {key} \"${env_names[key]}\"" for key in env_only]
    suggestions.append(
        "Then quit and relaunch Claude Desktop so the next nx-mcp "
        "spawn reads ~/.config/nexus/config.yml instead of empty env."
    )

    detail = (
        f"{len(env_only)} credential(s) in shell env only: {', '.join(env_only)}. "
        "GUI-spawned consumers (Claude Desktop, Cowork) cannot see "
        "shell env vars and will misdetect cloud mode as local mode."
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
    "nexus.catalog_collections",
    "nexus.catalog_document_chunks",
    "nexus.catalog_documents",
    "nexus.catalog_links",
    "nexus.catalog_meta",
    "nexus.catalog_owners",
    "nexus.chash_alias",
    # "nexus.chash_index" REMOVED (RDR-187/nexus-piwya.9, .9 review High):
    # the table is dropped, and _check_rls_present LEFT-JOINs this list
    # against live pg_class — a listed-but-dropped table is a PERMANENT
    # false FATAL. (The earlier "likely permanent" note on the bead covered
    # only the XML cross-walk, which reads immutable history; the live
    # check is the consumer that matters. The completeness guard carries a
    # matching dropped-tables exemption.)
    "nexus.chash_remap",
    "nexus.claude_assisted_remediation_consents",
    "nexus.document_aspects",
    "nexus.document_highlights",
    "nexus.frecency",
    "nexus.hook_failures",
    "nexus.ladder_completions",
    "nexus.memory",
    "nexus.migration_jobs",
    "nexus.nx_answer_runs",
    "nexus.pdf_chunks",
    "nexus.pdf_pages",
    "nexus.pdf_pipeline",
    "nexus.plans",
    "nexus.relevance_log",
    "nexus.retention_markers",
    "nexus.search_telemetry",
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
        return [HealthResult(
            label="Storage service health",
            ok=False,
            detail="service mode not configured (pg_credentials absent); skipping",
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

    Silent (``[]``) on the common cases: local mode (the T2 tier is the
    live substrate there — nothing stray to report), or service mode with
    no autostart unit installed. Any probe failure ALSO degrades
    silently — best-effort, must never break ``nx doctor``.
    """
    try:
        from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — deferred to avoid circular import

        if storage_backend_for("memory") != StorageBackend.SERVICE:
            return []

        from nexus.commands.daemon import _autostart_unit_installed  # noqa: PLC0415 — deferred, CLI startup cost

        unit_path = _autostart_unit_installed()
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_t2_launchagent_check_failed", error=str(exc))
        return []

    if unit_path is None:
        return [HealthResult(
            label="T2 autostart unit (service mode)",
            ok=True,
            detail="no stray T2 autostart unit installed",
        )]

    from nexus.upgrade_finish import _T2_AUTOSTART_UNIT_KIND  # noqa: PLC0415 — deferred to avoid circular import

    return [HealthResult(
        label="T2 autostart unit (service mode)",
        ok=False,
        warn=True,
        detail=(
            f"a T2 autostart unit ({_T2_AUTOSTART_UNIT_KIND}) is installed "
            f"at {unit_path} but service mode never starts the T2 daemon — "
            "its OS-level restart policy respawns an immediately-exiting "
            "process indefinitely (log noise)"
        ),
        fix_suggestions=[
            "nx daemon restart-stale  # removes the stray unit (GH #1405)",
            "nx daemon t2 uninstall --autostart  # removes it directly",
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
        return [HealthResult(
            label="Schema migrations",
            ok=False,
            detail="service mode not configured (pg_credentials absent); skipping",
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
        POISON_DETAIL_TOKEN,
        chash_conformance_statements,
        debt_chash_conformance_statements,
        legacy_chash_conformance_statements,
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
        try:
            # Amendment A6 (nexus-9bufb): view-era statements first — counts
            # by construction via nexus.diag_chash_conformance. An engine one
            # generation behind (no view yet) fails the first set; the legacy
            # direct-table statements still work there because the legacy
            # grants era carries full-table SELECT — fall back LOUDLY (log),
            # never silently.
            try:
                counts = run_diagnostic_sql(
                    chash_conformance_statements(), diag_creds,
                    psql_bin=psql_bin, psql_runner=diag_runner,
                )
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
                counts = run_diagnostic_sql(
                    legacy_chash_conformance_statements(), diag_creds,
                    psql_bin=psql_bin, psql_runner=diag_runner,
                )
            nonconforming = sum(int(c) for c in counts)
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
                "NOT VALID until the chash-rekey rung heals them), but "
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
                "`nx catalog owners list`",
                "Step 2 — re-index the file-backed legacy collections: "
                "`nx index repo <path>` (additive, per-collection; "
                "store_put-only notes need nothing — the rekey rung "
                "heals those from stored text)",
                "Step 3 — run the ladder: `nx upgrade` (the chash-rekey "
                "rung recomputes correct ids from stored chunk text)",
                "Step 4 — re-run `nx doctor`; upgrade the engine once "
                "this warning clears.",
                "Do NOT drop the chash length constraints to 'unblock' "
                "anything — that is what caused GH #1390.",
                "Rollback (`nx storage migrate vectors --rollback`) is "
                "for the will-not-boot class ONLY (service crash-looping "
                "at startup on a pre-v0.1.48 engine) — see §8.1 of "
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
        return [HealthResult(
            label="RLS policies",
            ok=False,
            detail="service mode not configured (pg_credentials absent); skipping",
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

    failed: list[str] = []
    for table in _RLS_TENANT_TABLES:
        if table not in rls_by_table:
            failed.append(f"{table} (not in query output)")
            continue
        rls_on, rls_force, policy_count = rls_by_table[table]

        if rls_on != "t" or rls_force != "t" or policy_count == 0:
            reasons = []
            if rls_on != "t":
                reasons.append("RLS not enabled")
            if rls_force != "t":
                reasons.append("RLS not forced")
            if policy_count == 0:
                reasons.append("no policies")
            failed.append(f"{table} ({', '.join(reasons)})")

    if failed:
        return [HealthResult(
            label="RLS policies",
            ok=False,
            detail=(
                f"RLS missing on {len(failed)}/{len(_RLS_TENANT_TABLES)} "
                f"tenant table(s): {', '.join(failed)}"
            ),
            fix_suggestions=[
                "Re-run migrations: nx init --service",
                "Verify the Liquibase changeset applied RLS: "
                "check service/src/main/resources/db/changelog/",
            ],
            fatal=True,
        )]

    return [HealthResult(
        label="RLS policies",
        ok=True,
        detail=(
            f"RLS policies: present on {len(_RLS_TENANT_TABLES)}/"
            f"{len(_RLS_TENANT_TABLES)} tenant tables"
        ),
    )]


# ── RDR-178 Pillar A: migration-report doctor checks ────────────────────────
#
# 2026-06-30 ``migrate all`` crashed 6/8 stores (report migration-9141ebaf,
# summary.total_failed=120, verification=indeterminate). Nothing read the
# report for a month; it was found only by manually opening the JSON
# (nexus-aigpt). ``_check_migration_reports`` closes Gap 1 (fail loud on a
# bad report); ``_check_migration_divergence`` closes Gap 2 (warn when local
# SQLite kept accepting writes after its store "moved" to a cloud target).


def _newest_migration_report_path(reports_dir: Path) -> Path | None:
    """Most-recently-modified ``migration-*.json`` in *reports_dir*, or
    ``None`` when the directory is absent or has no report files.

    Migration IDs are random UUIDs (see ``build_report``), not time-ordered,
    so filename sort cannot establish recency — mtime is the only signal
    available without parsing every report on disk.
    """
    if not reports_dir.exists():
        return None
    candidates = sorted(
        reports_dir.glob("migration-*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    return candidates[-1] if candidates else None


def _is_local_service_url(url: str) -> bool:
    """True when *url* is empty, the pre-lease placeholder, or points at
    loopback — i.e. NOT a remote/cloud migration target."""
    url = url.strip()
    if not url or url == "(lease)":
        return True
    from urllib.parse import urlparse  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    host = urlparse(url).hostname or url
    return host in ("localhost", "127.0.0.1", "::1")


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
    return [HealthResult(
        label=label,
        ok=False,
        fatal=True,
        detail=stranded.message,
        fix_suggestions=[
            f"Install the last migration-capable release: uv tool install conexus=={stranded.pinned_release}",
            "Run: nx guided-upgrade",
            "Then upgrade back to this version",
        ],
    )]


def _check_migration_reports(reports_dir: Path | None = None) -> list[HealthResult]:
    """RDR-178 Gap 1 (nexus-aigpt): read the newest migration report and
    fail loud when it recorded failures or an unverified run.

    ``verification`` present but anything other than ``"verified"``
    (``"indeterminate"``, ``"mismatch"``) counts as failure — the
    nexus-r0esi precedent: never SKIP-then-report-all-passed. The vocabulary
    is the orchestrator's: ``verify_counts()`` emits exactly
    ``"verified" | "mismatch" | "indeterminate"`` (never "passed").

    ``verification`` KEY entirely absent + zero failures = a legacy report
    from pre-6.2 tooling that never recorded verdicts → non-fatal WARN with
    a one-time re-verify suggestion, never a fatal alarm (the false-positive
    split, 2026-07-02). Absent + failures recorded stays fatal.
    """
    label = "Migration reports"
    if reports_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        reports_dir = nexus_config_dir() / "migration-reports"

    report_path = _newest_migration_report_path(reports_dir)
    if report_path is None:
        return [HealthResult(label=label, ok=True, detail="no migrations recorded")]

    try:
        from nexus.migration.migration_report import load_report  # noqa: PLC0415 — deferred to avoid circular import
        report = load_report(report_path)
    except (OSError, ValueError) as exc:
        # A report that exists but cannot be read must never be silently
        # treated as "no migrations recorded" — that recreates the
        # month-of-silence class with a different failure mode.
        _log.warning("doctor_migration_report_unreadable", path=str(report_path), error=str(exc))
        return [HealthResult(
            label=label,
            ok=False,
            fatal=True,
            detail=f"{report_path}: could not read report ({exc})",
            fix_suggestions=[f"Inspect: nx storage migration-report show {report_path}"],
        )]

    summary = report.get("summary") or {}
    try:
        total_failed = int(summary.get("total_failed", 0))
    except (TypeError, ValueError):
        total_failed = 1  # unparseable -- treat as a failure signal, never silence
    verification = report.get("verification")

    failed = total_failed > 0
    unverified = verification != "verified"

    if not failed and not unverified:
        return [HealthResult(
            label=label,
            ok=True,
            detail=f"clean ({report_path.name}): total_failed=0, verification=verified",
        )]

    # Legacy-artifact split (2026-07-02, Hal): a report with ZERO failures
    # whose ``verification`` KEY is entirely absent was written by pre-6.2
    # tooling that never recorded a verdict — a benign, knowable artifact
    # (modern writers ALWAYS record one; see _emit_store_report). Reporting
    # it FATAL is a crying-wolf alarm on day one of every upgrade — the
    # false-positive class that trains operators to ignore doctor. It
    # warns (non-fatal) with a one-time-actionable fix instead. A MODERN
    # report saying mismatch/indeterminate, or any total_failed > 0, stays
    # fatal — the nexus-r0esi never-silently-pass rule keeps its teeth
    # where the signal is real. (A modern report can never hit this
    # branch: its verification key is always present.)
    if not failed and "verification" not in report:
        return [HealthResult(
            label=label,
            ok=False,
            warn=True,
            fatal=False,
            detail=(
                f"{report_path.name}: clean (total_failed=0) but predates "
                f"verification recording (pre-6.2 tooling) — unverified, not failed"
            ),
            fix_suggestions=[
                "Re-verify once with the current tooling (near-no-op on an already-migrated system):",
                "  nx storage migrate all --verify-fill",
                "This writes a fresh report with a real verification verdict and clears this warning.",
            ],
        )]

    per_store_failures: list[str] = []
    for store in report.get("stores") or []:
        store_name = str(store.get("store", "?"))
        store_failed = sum(
            int(table.get("failed") or 0) for table in (store.get("tables") or [])
        )
        if store_failed:
            per_store_failures.append(f"{store_name}={store_failed}")

    reasons = []
    if failed:
        reasons.append(f"total_failed={total_failed}")
    if unverified:
        reasons.append(f"verification={verification!r}")

    detail = f"{report_path}: {', '.join(reasons)}"
    if per_store_failures:
        detail += f"; per-store failures: {', '.join(per_store_failures)}"

    fix_suggestions = ["Re-run the failed store migrations:"]
    if per_store_failures:
        for entry in per_store_failures:
            fix_suggestions.append(f"  nx storage migrate {entry.split('=')[0]}")
    else:
        fix_suggestions.append("  nx storage migrate all")
    fix_suggestions.append(f"Inspect: nx storage migration-report show {report_path}")

    return [HealthResult(
        label=label,
        ok=False,
        fatal=True,
        detail=detail,
        fix_suggestions=fix_suggestions,
    )]


def _check_migration_divergence(
    reports_dir: Path | None = None,
    memory_db_path: Path | None = None,
) -> list[HealthResult]:
    """RDR-178 Gap 2 (nexus-14ndm): warn when local SQLite ``memory.db`` kept
    accepting writes after the newest migration report recorded a cloud
    target for the memory store.

    Incident: after the 2026-06-30 cloud migration, ``memory.db`` kept
    receiving writes from old-venv MCP daemons still resolving the local
    backend — 68 rows accumulated with no warning anywhere. Non-fatal
    (``warn=True``): a stale local copy is recoverable, not catastrophic.
    """
    label = "Migration divergence (memory)"
    if reports_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to avoid circular import
        reports_dir = nexus_config_dir() / "migration-reports"
    if memory_db_path is None:
        memory_db_path = default_db_path()

    report_path = _newest_migration_report_path(reports_dir)
    if report_path is None:
        return [HealthResult(label=label, ok=True, detail="no migrations recorded")]

    try:
        from nexus.migration.migration_report import load_report  # noqa: PLC0415 — deferred to avoid circular import
        report = load_report(report_path)
    except (OSError, ValueError) as exc:
        # Gap 1's check already fails loud on an unreadable report; this
        # check degrades quietly rather than double-reporting the same fault.
        _log.warning("doctor_migration_divergence_report_unreadable", path=str(report_path), error=str(exc))
        return [HealthResult(label=label, ok=True, detail=f"{report_path.name}: could not read report; skipping")]

    target = report.get("target") or {}
    service_url = str(target.get("service_url") or "")
    if _is_local_service_url(service_url):
        return [HealthResult(
            label=label, ok=True,
            detail=f"migration target is local ({service_url or '(none)'}); skipping",
        )]

    completed_at_raw = report.get("completed_at")
    if not completed_at_raw:
        return [HealthResult(label=label, ok=True, detail="report missing completed_at; skipping")]
    try:
        cutoff = datetime.fromisoformat(str(completed_at_raw).replace("Z", "+00:00"))
    except ValueError:
        return [HealthResult(
            label=label, ok=True,
            detail=f"report has unparseable completed_at {completed_at_raw!r}; skipping",
        )]
    # memory.db stores timestamps as "%Y-%m-%dT%H:%M:%SZ" (fixed-width UTC,
    # no fractional seconds) — format the cutoff the same way so a plain SQL
    # string comparison is valid.
    cutoff_str = cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if not memory_db_path.exists():
        return [HealthResult(label=label, ok=True, detail="memory.db not present; skipping")]

    try:
        conn = sqlite3.connect(f"file:{memory_db_path}?mode=ro", uri=True)  # epsilon-allow: health divergence check — read-only, must not contend for the WAL writer slot
        try:
            divergent_count, max_ts = conn.execute(
                "SELECT COUNT(*), MAX(timestamp) FROM memory WHERE timestamp > ?",
                (cutoff_str,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return [HealthResult(label=label, ok=True, detail=f"could not query memory.db: {exc}")]

    if not divergent_count:
        return [HealthResult(
            label=label, ok=True,
            detail=f"no local writes after migration to {service_url} ({report_path.name})",
        )]

    return [HealthResult(
        label=label,
        ok=False,
        warn=True,
        detail=(
            f"{divergent_count} local memory write(s) landed after migration to "
            f"{service_url} completed at {completed_at_raw} "
            f"(latest local write {max_ts}); report={report_path}"
        ),
        fix_suggestions=["Re-run: nx storage migrate memory"],
    )]


def _check_pending_rungs() -> list[HealthResult]:
    """RDR-185 P0.4 (nexus-n7u38.4): read-only upgrade-ladder surface.

    Reports pending ladder rungs from each rung's READ-ONLY ``detect()`` —
    zero writes, zero work, the completion store is never opened (the
    ``resolve_pending_steps`` dry-run-truth precedent). Pending rungs are a
    soft warning with `nx upgrade` (the single trigger) as the remedy.
    Crash-proof: any failure degrades to a non-critical pass — every check
    in ``run_health_checks`` must never crash ``nx doctor`` as a whole.
    """
    try:
        from nexus.upgrade_ladder import registry as _ladder_registry  # noqa: PLC0415 — deferred to avoid module-load cost
        from nexus.upgrade_ladder.runner import pending_rungs  # noqa: PLC0415 — deferred to avoid module-load cost

        statuses = pending_rungs(_ladder_registry.default_registry())
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.warning("doctor_pending_rungs_check_failed", error=str(exc))
        return [HealthResult(
            label="Upgrade ladder", ok=True, detail="check failed (non-critical)",
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


def _check_legacy_id_census() -> list[HealthResult]:
    """RDR-185 P1.2 (nexus-n7u38.9): legacy chunk-id era census (Gap-5).

    Chroma-mode installs holding OUTSTANDING pre-RDR-108 (non-32-char)
    chunk ids see the debt HERE — months before migration day (the
    GH #1408 incident class: 18 legacy collections invisible until they
    blocked the migration). Soft warning, and never a directive: the
    substrate rung's wire re-id converges this in flight at migration,
    and the upgrade-ladder row — not this one — is the authority on
    pending work (Gap-4). Non-Chroma / fresh installs skip silently (the
    census's cheap file-level gate never opens the store); a CONVERGED
    install prints the clean row, because the debt is gone even though
    the immutable RDR-176 source still holds its legacy ids and always
    will (nexus-6or3m). Crash-proof.

    Reads the census row ONLY. It pairs with ``_check_pending_rungs``:
    count equality cannot see an unreflected remap cascade, so a clean
    row here plus a pending ladder row is a coherent pair, not a
    contradiction — the ladder row carries the half this cannot.
    """
    try:
        from nexus.upgrade_ladder import census as _census  # noqa: PLC0415 — deferred to avoid module-load cost

        result = _census.legacy_id_census()
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.warning("doctor_legacy_id_census_failed", error=str(exc))
        return [HealthResult(
            label="Chunk-id era (upgrade ladder)", ok=True,
            detail="check failed (non-critical)",
        )]
    if result is None:
        return []  # not applicable: no legacy Chroma footprint to census
    if not result:
        # NOT "all collections hold conformant ids" (nexus-6or3m): on a
        # converged install the immutable RDR-176 source still holds its legacy
        # ids forever, and always will. What is true in both worlds — never
        # migrated + conformant, and migrated + converged — is that no debt is
        # outstanding.
        return [HealthResult(
            label="Chunk-id era (upgrade ladder)",
            ok=True,
            detail="no outstanding legacy chunk-id debt",
        )]
    names = ", ".join(
        f"{c.collection} ({c.source_count} chunks, {c.leg})" for c in result[:6]
    )
    more = f" (+{len(result) - 6} more)" if len(result) > 6 else ""
    # A collection the upgrade CANNOT converge gets the remedy that actually
    # works, named (bead nexus-mq42b). Without this the row's silence reads as
    # "the upgrade will handle it", and for a keyless voyage collection it never
    # will — the planner skips it and the ladder then reports converged. That is
    # the impossible-remedy shape RDR-185 exists to end, so the one case that
    # needs a user action is the one case this row must speak up about.
    blocked = [c for c in result if c.blocked_reason]
    blocked_more = f" (+{len(blocked) - 3} more)" if len(blocked) > 3 else ""
    blocked_suggestions = (
        [
            f"{len(blocked)} of these cannot be converged by the upgrade: "
            + "; ".join(f"{c.collection} — {c.blocked_reason}" for c in blocked[:3])
            + blocked_more
            + ". Configure the Voyage key and re-run `nx upgrade` to include "
            "them, or re-index them from source against the current embedder."
        ]
        if blocked
        else []
    )
    return [HealthResult(
        label="Chunk-id era (upgrade ladder)",
        ok=False,
        warn=True,
        detail=(
            f"{len(result)} collection(s) hold pre-RDR-108 legacy chunk ids — "
            f"pending upgrade-ladder debt: {names}{more}"
        ),
        # This row deliberately issues NO directive, and that is a contract, not
        # an omission (RDR-185 Gap-4: the census must not become a THIRD
        # mechanism answering "how far from current" — the upgrade-ladder row is
        # the authority on pending work; this row is visibility).
        #
        # An earlier draft of this fix said "Run: nx upgrade" here. Both
        # reviewers caught it and it was wrong twice over: the census's whole
        # reason to exist is keeping visible the collections that CANNOT
        # migrate, and for the sharpest of those — a voyage-named legacy
        # collection with no key — `nx upgrade` provably no-ops (the planner
        # skips it at the credential gate, the rung then reports converged, and
        # this row fires again forever). Directing a user to a verb the product
        # will not honour rebuilds the impossible-remedy shape RDR-185 exists to
        # end. The credential gate's own silent skip is bead nexus-mq42b; this
        # row's silence is the message-layer workaround, not the fix.
        fix_suggestions=[
            # Conditional, or the row contradicts itself: "No action needed
            # here" cannot print directly above "N of these cannot be converged
            # by the upgrade" (code review, 2026-07-17). When anything is
            # blocked, the blocked line IS the message.
            *(
                []
                if blocked
                else [
                    "No action needed here: the substrate migration (nx "
                    "upgrade, wire re-id) converges legacy chunk ids in flight "
                    "— no re-index, no re-embed. This row is visibility (the "
                    "GH #1408 class: era debt that used to surface only ON "
                    "migration day); the upgrade-ladder row is what reports "
                    "pending work."
                ]
            ),
            *blocked_suggestions,
        ],
    )]


def run_health_checks() -> tuple[list[HealthResult], bool]:
    """Run all health checks.

    Returns (results, is_local_mode).
    """
    from nexus.config import is_local_mode, get_credential  # noqa: PLC0415 — deferred to avoid circular import

    results: list[HealthResult] = []

    results.extend(_check_python())
    results.extend(_check_cli_version())
    results.extend(_check_process_skew())
    results.extend(_check_plugin_name())
    results.extend(_check_credential_persistence())

    _local = is_local_mode()
    if _local:
        results.extend(_check_t3_local())
        results.extend(_check_service_bge_model())
        results.extend(_check_t3_daemon_version())
    else:
        results.extend(_check_t3_cloud())

    results.extend(_check_tools())
    results.extend(_check_git_hooks())
    results.extend(_check_index_log())
    results.extend(_check_orphan_t1())
    results.extend(_check_orphan_checkpoints())
    results.extend(_check_orphan_pipelines())
    results.extend(_check_mineru_server())
    results.extend(_check_t2_integrity())
    results.extend(_check_t2_dropped_writes())
    results.extend(_check_t2_daemon_singleton())

    # ChromaDB pagination audit (cloud only)
    if not _local:
        chroma_key = get_credential("chroma_api_key")
        chroma_database = get_credential("chroma_database")
        chroma_tenant = get_credential("chroma_tenant")
        if chroma_key and chroma_database:
            try:
                # RDR-120 P2: route through make_t3. Cloud-only branch
                # (gated by ``not _local``); daemon does not apply.
                from nexus.db import make_t3  # noqa: PLC0415 — deferred to avoid circular import
                client = make_t3()._client
                results.extend(_check_chroma_pagination(client, chroma_database))
            except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash caller
                _log.debug(
                    "doctor_pagination_check_client_failed",
                    db=chroma_database, error=str(exc),
                )
                results.append(HealthResult(
                    label=f"ChromaDB pagination ({chroma_database})", ok=True,
                    detail="skipped (client unavailable)",
                ))

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
    results.extend(_check_migration_state())
    results.extend(_check_rls_present())
    # RDR-185 P0.4: read-only pending-rungs surface (degrades internally).
    results.extend(_check_pending_rungs())
    # RDR-185 P1.2: legacy chunk-id era census, Gap-5 (degrades internally).
    results.extend(_check_legacy_id_census())

    # RDR-178 Pillar A (nexus-aigpt, nexus-14ndm): migration-report checks.
    # Both degrade internally (missing dir / unreadable report / absent
    # memory.db all resolve to an ok=True HealthResult), but every check in
    # this function must be crash-proof for `nx doctor` as a whole (see the
    # doctor_catalog_reader_unavailable precedent above) — guard anyway.
    try:
        results.extend(_check_migration_reports())
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.warning("doctor_migration_reports_check_failed", error=str(exc))
        results.append(HealthResult(
            label="Migration reports", ok=True, detail="check failed (non-critical)",
        ))
    try:
        results.extend(_check_migration_divergence())
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.warning("doctor_migration_divergence_check_failed", error=str(exc))
        results.append(HealthResult(
            label="Migration divergence (memory)", ok=True, detail="check failed (non-critical)",
        ))

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

    return results, _local
