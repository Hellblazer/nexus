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

    # VOYAGE_API_KEY — server-side embedding on the service path; the
    # client key is enrichment + engine-bootstrap material only (RDR-188
    # moved reranking server-side — no client code path consumes this key
    # for rerank), not a serving requirement.
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
       is NOT under ``sys.prefix`` wins.
    2. If no such match exists but a match under ``sys.prefix`` does
       (e.g. this process's own venv is the ONLY thing on PATH — a valid
       shape when a user invokes that venv's own ``nx`` directly), that
       match is still returned and still probed for real.
    3. ``None`` only when the binary resolves nowhere on PATH at all.

    Returns ``(path_or_none, is_own_venv)``.
    """
    own_prefix = str(Path(sys.prefix).resolve())
    path_env = os.environ.get("PATH", os.defpath)
    own_prefix_hit: str | None = None
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
        return hit, False
    return own_prefix_hit, True


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


def _check_git_hooks() -> list[HealthResult]:
    # nexus-8g79.10 (V2): import from the lower-layer module instead of
    # reaching up into commands/. Use module-attribute access so test
    # monkeypatches on ``nexus._git_hooks_meta.effective_hooks_dir``
    # reach the live binding at call time.
    import re  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost
    from nexus import _git_hooks_meta as _ghm  # noqa: PLC0415 — deferred to avoid circular import
    from nexus._git_hooks_meta import SENTINEL_BEGIN, SENTINEL_END  # noqa: PLC0415 — deferred to avoid circular import
    _effective_hooks_dir = _ghm.effective_hooks_dir
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
    cat = None
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

    # nexus-7kl32 code-review finding 3 (population mismatch): the loop
    # below walks the CATALOG ∪ registry union (``repos``), but
    # ``nx catalog owners --census`` classifies catalog owners only — a
    # dead owner whose ONLY registration is the legacy ``repos.json`` file
    # would warn here yet never appear in the census output the warning
    # points at. Attribute each dead path to its source (once, up front —
    # not per-repo) so the vanished-owner branch below can say which verb
    # actually covers it.
    catalog_repo_roots: set[str] = set()
    if cat is not None:
        try:
            catalog_repo_roots = {
                o.get("repo_root") for o in cat.list_owners_by_type("repo")
                if o.get("repo_root")
            }
        except Exception:  # noqa: BLE001 — attribution is best-effort; the primary check must survive its failure
            _log.debug("doctor_git_hooks_catalog_attribution_failed", exc_info=True)
            catalog_repo_roots = set()

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
                        # covers it (same list_owners_by_type("repo") read).
                        results.append(HealthResult(
                            label="git hooks", ok=False, warn=True,
                            detail=(
                                f"{repo_path} — owner root no longer exists "
                                "on disk (dead owner)"
                            ),
                            fix_suggestions=[
                                "nx catalog owners --census — inspects dead "
                                "owners (read-only; deregistration not yet "
                                "available, tracked as nexus-cw262)"
                            ],
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

    Mirrors the discriminator the retired T2 bootstrap-migration retry used
    (``database is locked`` / ``database is busy``, including the
    ``SQLITE_BUSY_SNAPSHOT`` variant, whose message also contains "locked");
    the retry died with ``db/migrations.py`` (RDR-158 P4 Stage 4,
    nexus-i711w) but the lock-vs-corruption distinction is still the point.
    """
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _check_t2_integrity() -> list[HealthResult]:
    import time  # noqa: PLC0415 — deferred import — branch-local, avoids module-load cost

    db_path = default_db_path()
    if not db_path.exists():
        return [HealthResult(label="T2 integrity", ok=True, detail="not created yet")]

    try:
        conn = sqlite3.connect(str(db_path))  # frozen-source-integrity-write: NOT mode=ro — the FTS5 integrity-check pseudo-command below requires a writable connection (checking command, no content change); named in SQLITE_CONNECT_ALLOWLIST with the write-shaped exception documented
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
    ``nx config set``, the GUI-spawned subprocess sees it as absent,
    ``is_local_mode()`` flips to True, and T3 dispatch goes to the
    daemon path that fails opaquely. (RDR-155 P4b: the CHROMA_* keys
    died with the migration machinery.)

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
    "nexus.gc_audit",
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
                "The will-not-boot class ONLY (service crash-looping at "
                "startup on a pre-v0.1.48 engine) recovers on the "
                "LAST_MIGRATION_CAPABLE pinned release (this version no "
                "longer ships the rollback tooling) — see §8.1 of "
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
        from nexus.db import (  # noqa: PLC0415 — deferred to avoid circular import
            read_legacy_catalog_counts,
        )

        docs, links = read_legacy_catalog_counts(legacy)

        import datetime as _dt  # noqa: PLC0415 — deferred, formatting only

        mtime = _dt.datetime.fromtimestamp(legacy.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M"
        )
        results.append(HealthResult(
            label="Legacy catalog file",
            ok=False,
            warn=True,
            detail=(
                f"{legacy} is a FROZEN migration source, not a live mirror of "
                f"the catalog. It holds {docs} document rows / {links} link "
                f"rows, last written {mtime}. The authoritative catalog is in "
                "Postgres and has moved on independently — these numbers will "
                "drift further apart over time and that is expected."
            ),
            fix_suggestions=[
                "Do NOT restore or recover from this file — it is a "
                "pre-migration snapshot kept only as a rollback source. "
                "Use `nx catalog stats` for the real counts.",
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
    return [HealthResult(
        label=label,
        ok=False,
        fatal=True,
        detail=stranded.message,
        fix_suggestions=[
            f"Install the last migration-capable release: uv tool install conexus=={stranded.pinned_release}",
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


#: nexus-heizf: above this many distinct damaged DOCUMENTS, doctor stops
#: naming tumblers inline and points at the enumeration command instead.
_DANGLING_MANIFEST_NAME_THRESHOLD = 10

#: nexus-heizf part 1: a single ``manifest_orphans`` call's sample cap.
#: Chosen generously so the common case (a few dozen to a couple hundred
#: dangling rows, per the 2026-08-04 nexus-55l58 shakedown's 188) needs no
#: second round trip; :func:`manifest_orphan_report` still re-fetches on the
#: rare miss, bounded by ``_MANIFEST_ORPHANS_MAX_ROWS_PER_DIM`` below.
_MANIFEST_ORPHANS_SAMPLE_LIMIT = 2000

#: nexus-heizf code-review fix round (2026-08-05): the ceiling on the
#: refetch below. ``manifest_orphans(dim)``'s ``limit`` is an ENGINE-WIDE
#: cap on that dim's orphan rows (not scoped to the damaged collections
#: under investigation here) with no upper bound enforced server-side and
#: — load-bearing for why this is a single bounded fetch, not a paged loop
#: — NO ``offset`` parameter (CatalogRepository.manifestOrphanReport /
#: CatalogHandler.handleManifestOrphans both take only ``limit``), so
#: repeated calls cannot walk pages the way the ``MAX_QUERY_RESULTS=300``
#: convention elsewhere in this codebase does. Un-bounding the refetch
#: (the pre-fix-round shape: ``limit=count``) would let one damaged dim
#: with tens of thousands of rows drive an unbounded response. 10k is
#: ~50x the 2026-08-04 nexus-55l58 shakedown's 188-row observation —
#: generous headroom for real data, still a hard stop. Exceeding it (or a
#: refetch failure) is never silently treated as complete: see the
#: per-collection ``incomplete_collections`` cross-check against the
#: census's own ``missing`` counts at the end of
#: :func:`manifest_orphan_report`.
_MANIFEST_ORPHANS_MAX_ROWS_PER_DIM = 10_000

#: nexus-h1zu0 / nexus-heizf: the one-line runtime disjointness caveat —
#: appears in `nx doctor`'s dangling-manifest detail AND
#: `nx catalog manifest-verify --list`'s output (both text and --json),
#: never docstring/help-only (substantive-critic Significant-1, 2026-08-05:
#: the 2026-08-04 nexus-55l58 shakedown was mislead by an instrument's
#: *docstring*, which nobody reads mid-incident — the live numeric output
#: itself must carry the warning). See ``purge_trash.py``'s matching line.
_DANGLING_MANIFEST_POPULATION_NOTE = (
    "population: live-doc manifest rows missing a T3 chunk — disjoint "
    "from `nx catalog purge-trash`'s 'stranded' chunks (tombstoned-doc "
    "chunks with no live parent); one reading clean says nothing about "
    "the other"
)


def _compact_position_ranges(positions: list[int]) -> str:
    """Compact a list of manifest positions into ``"0-3,7,9-12"`` form.

    Pure display helper — a damaged document with hundreds of contiguous
    dangling positions (a whole-file re-chunk gone wrong) renders as one
    short range instead of a wall of individual numbers.
    """
    xs = sorted(set(positions))
    if not xs:
        return ""
    ranges: list[str] = []
    start = prev = xs[0]
    for x in xs[1:]:
        if x == prev + 1:
            prev = x
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = x
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def manifest_orphan_report(cat: object, damaged_collections: list[dict]) -> dict:
    """Enumerate the dangling manifest ROWS behind a ``manifest_verify_all``
    census, grouped by collection then by document (nexus-heizf part 1).

    Wires the previously-dead ``manifest_orphans(dim)`` client method
    (RDR-159 P-1b, zero callers before this bead — T2 nexus/55l58-
    instrument-map-2026-08-04) to a consumer: given the damaged rows
    ``manifest_verify_all()`` already returned (``{"collection",
    "referenced", "present", "missing"}`` dicts with ``missing > 0``),
    resolves each collection's dim via :func:`nexus.db.reconcile.
    dim_for_model_token` and enumerates the actual ``(doc_id, position,
    chash)`` rows for every resolvable dim.

    ROUTING PARITY (nexus-h1zu0): ``manifest_verify_all`` does not route by
    model token at all — it ORs presence across all three ``chunks_<dim>``
    tables per manifest row, so it never drops a collection for having an
    unrecognized token. ``manifest_orphans(dim)``'s SQL, by contrast, only
    returns rows for collections whose ``__<model>__`` segment is in that
    dim's hardcoded IN-list (rdr180-002-hex-boundary-functions.xml). A
    collection with a token outside ALL three IN-lists is therefore
    invisible to ``manifest_orphans`` at every dim while still being
    counted (and possibly flagged ``missing``) by ``manifest_verify_all`` —
    a SILENT per-collection coverage gap with no error. This function
    closes the "silent, no error" half of that gap (not the underlying SQL
    routing itself — see :func:`nexus.db.reconcile.dim_for_model_token`'s
    docstring for why a client-side fix was chosen over an engine-side SQL
    change): any damaged collection this function cannot route, OR whose
    ``manifest_orphans`` call itself fails, is reported by NAME in
    ``unroutable_collections`` rather than being dropped without a trace.
    On 2026-08-04 production data every damaged collection routed cleanly
    (188 census rows = 188 enumerated rows) — the gap is latent, not
    active; this function makes it loud if it ever isn't.

    COMPLETENESS (code-review-expert fix round, 2026-08-05): a dim's
    orphan population can exceed what a single bounded fetch returns (see
    ``_MANIFEST_ORPHANS_MAX_ROWS_PER_DIM``), and the bounded refetch
    itself can fail. NEITHER case is silently treated as a complete
    result — the row count actually enumerated for each collection is
    cross-checked against the census's own ``missing`` count from
    *damaged_collections*; a shortfall lands the collection in
    ``incomplete_collections`` with both numbers, regardless of which of
    the two causes produced it (uniform handling, not a special case per
    failure mode).

    Returns::

        {
            "collections": {
                "<collection>": {
                    "<doc_id>": {"positions": [0, 1, ...], "chashes": {"..."}},
                    ...
                },
                ...
            },
            "unroutable_collections": [...],  # sorted, deduped names
            "incomplete_collections": {
                "<collection>": {"enumerated": <int>, "expected": <int>},
                ...
            },
            "total_rows": <int>,              # sum of enumerated rows
        }

    Best-effort: never raises. A ``manifest_orphans`` failure for one dim
    folds that dim's collections into ``unroutable_collections`` and moves
    on to the next dim rather than aborting the whole report. Per-row
    field parsing is guarded too (a malformed ``position`` from the wire
    is logged and skipped, never an unhandled exception).
    """
    from nexus.corpus import (  # noqa: PLC0415 — deferred to avoid a module-load-time import cycle
        is_conformant_collection_name,
        parse_conformant_collection_name,
    )
    from nexus.db.reconcile import dim_for_model_token  # noqa: PLC0415 — see above

    damaged_names = {str(row.get("collection", "?")) for row in damaged_collections}
    expected_missing: dict[str, int] = {
        str(row.get("collection", "?")): int(row.get("missing", 0) or 0)
        for row in damaged_collections
    }
    dims_needed: dict[int, list[str]] = {}
    unroutable: list[str] = []
    for name in sorted(damaged_names):
        if not is_conformant_collection_name(name):
            unroutable.append(name)
            continue
        token = parse_conformant_collection_name(name)["embedding_model"]
        dim = dim_for_model_token(token)
        if dim is None:
            unroutable.append(name)
            continue
        dims_needed.setdefault(dim, []).append(name)

    collections: dict[str, dict[str, dict]] = {}
    total_rows = 0
    for dim in sorted(dims_needed):
        try:
            first = cat.manifest_orphans(dim, limit=_MANIFEST_ORPHANS_SAMPLE_LIMIT)
        except Exception as exc:  # noqa: BLE001 — isolated per dim: reported, not swallowed
            _log.warning("doctor_manifest_orphans_failed", dim=dim, error=str(exc))
            unroutable.extend(dims_needed[dim])
            continue
        count = int(first.get("count", 0) or 0)
        orphans = first.get("orphans") or []
        if count > len(orphans):
            # Bounded refetch (nexus-heizf fix round): never request more
            # than the hard ceiling, and a refetch failure is left to fall
            # through to the completeness cross-check below rather than
            # silently standing in for a complete result.
            fetch_limit = min(count, _MANIFEST_ORPHANS_MAX_ROWS_PER_DIM)
            if fetch_limit > len(orphans):
                try:
                    second = cat.manifest_orphans(dim, limit=fetch_limit)
                    orphans = second.get("orphans") or orphans
                except Exception as exc:  # noqa: BLE001 — best-effort refetch; a shortfall is still caught below
                    _log.warning("doctor_manifest_orphans_refetch_failed", dim=dim, error=str(exc))
        for row in orphans:
            coll = str(row.get("collection", "?"))
            if coll not in damaged_names:
                continue
            doc_id = str(row.get("doc_id", "?"))
            bucket = collections.setdefault(coll, {})
            doc_bucket = bucket.setdefault(doc_id, {"positions": [], "chashes": set()})
            position = row.get("position")
            if position is not None:
                try:
                    doc_bucket["positions"].append(int(position))
                except (TypeError, ValueError) as exc:
                    _log.warning(
                        "doctor_manifest_orphans_row_position_unparseable",
                        row=row, error=str(exc),
                    )
            chash = row.get("chash")
            if chash:
                doc_bucket["chashes"].add(str(chash))
            total_rows += 1

    incomplete: dict[str, dict[str, int]] = {}
    for coll, expected in expected_missing.items():
        if coll in unroutable or expected <= 0:
            continue
        enumerated = sum(
            len(info["positions"]) for info in collections.get(coll, {}).values()
        )
        if enumerated < expected:
            incomplete[coll] = {"enumerated": enumerated, "expected": expected}

    return {
        "collections": collections,
        "unroutable_collections": sorted(set(unroutable)),
        "incomplete_collections": incomplete,
        "total_rows": total_rows,
    }


def _check_dangling_manifests() -> list[HealthResult]:
    """Name collections whose manifest references chashes that no longer
    exist in T3 (nexus-5xn3k AC5, RE-ARMED by nexus-5xn3k.6 on
    ``manifest_verify_all()`` — closes nexus-ac4id).

    THE UNDETECTABLE CLASS. A partial index commits chunks and a manifest
    without a transaction spanning them, so an interrupted run can leave a
    catalog row that LOOKS healthy: ``nx catalog show`` reports a chunk_count,
    the manifest lists chashes, and every one of them hydrates to nothing.
    Nothing in the product reports it. ``nx catalog reconcile`` covers the
    adjacent shape — ``chunk_count > 0`` with an EMPTY manifest (GH #1397) —
    but NOT a POPULATED manifest full of dead chashes, which is the state a
    failed index actually leaves behind.

    PRE-nexus-5xn3k.6, this check paged T3 chunk metadata client-side per
    collection (``t3.list_collections()`` + ``list_chunks_with_metadata``) —
    a multi-minute scan on managed boxes, deliberately left DISABLED via an
    unfixed dict-shape crash (nexus-ac4id) rather than revived at that cost.
    ``manifest_verify_all()`` (design memo §3.2/§4) replaces both: ONE
    engine-side SQL anti-join, tenant-scoped, no chunk metadata crosses the
    wire. The dict-shape off-switch is gone because there is no more
    client-side T3 enumeration to accidentally revive — ac4id part (1)
    becomes moot, not fixed.

    Fence unreadable (pre-fence engine, 404) => fail open, but LOUD: renders
    SKIPPED with a WARNING (⚠), never a clean pass — a check that silently
    stops working recreates the exact ac4id bug this re-arm closes (memo
    §3.4's fail-open+WARNING contract, applied here to the doctor sweep).

    Degrades to a skip — a doctor check must never crash the command it is
    diagnosing, and never guess.

    POPULATION (nexus-heizf part 3 — read this before comparing this
    check's count against ``nx catalog purge-trash``'s "stranded chunks"
    preview; they are DISJOINT, not two views of the same rows, and one
    reading clean says NOTHING about the other):

    * THIS check: manifest ROWS of LIVE documents
      (``catalog_documents.deleted_at IS NULL``) whose ``(collection,
      chash)`` has NO backing row in any ``chunks_<dim>`` table. Direction:
      manifest -> chunk. A chash can be counted here more than once (one
      per manifest row/position that references it) — see the wording
      note below.
    * ``purge-trash``'s stranded-chunk preview (``nexus.purge_trash``'s
      dry-run count, ``commands/catalog_cmds/purge_trash.py``): EXISTING
      ``chunks_<dim>`` rows that ARE manifest-backed but have NO LIVE
      parent document (every referencing manifest row's document is
      tombstoned). Direction: chunk -> parent.

    A chash cannot be in both populations at once (this check requires a
    LIVE parent by construction; purge-trash requires the opposite). Zero
    stranded chunks says nothing about this check's count, and vice versa
    — the 2026-08-04 nexus-55l58 shakedown mistook one instrument's zero
    for evidence against the other's non-zero finding on the SAME data.

    WORDING: ``manifest_verify_all()``'s ``missing`` count is manifest ROWS,
    not distinct chashes — the same chash referenced from two positions in
    one document's manifest counts twice. The detail text below says
    "row(s)"; a cheap distinct-chash count is layered on top via
    :func:`manifest_orphan_report` when the enumeration succeeds (nexus-
    heizf part 2 — the 2026-08-04 shakedown's 188 rows were 186 distinct
    chashes).
    """
    label = "dangling manifest chashes"
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return [HealthResult(label=label, ok=True, detail="skipped (no catalog)")]
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_dangling_manifest_check_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (catalog unavailable)")]

    import httpx  # noqa: PLC0415 — deferred to avoid a heavy/optional import at module load

    try:
        result = cat.manifest_verify_all() or {}
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status == 404:
            # memo §3.4 / §6: a pre-fence engine 404s every route this arc
            # added. Fail OPEN (never crash `nx doctor`) but LOUD — this is
            # the exact ac4id lesson, applied to the fence's own read.
            #
            # HISTORY (nexus-5xn3k.6 code-review-expert CRITICAL, 2026-08-02):
            # from REQUIRED_ENGINE_VERSION v0.1.61 (which predates 3cf64d48)
            # until the nexus-koms3 v0.1.62 floor bump, this detail string was
            # allowlisted VERBATIM by tests/e2e/fresh-install-mvv.sh's
            # doctor-warnings check, because this branch fired on every
            # virgin box. As of the v0.1.62 floor, the bundled engine ships
            # the fence routes, so a virgin box no longer 404s here and that
            # allowlist entry was removed (nexus-koms3, same change). This
            # fail-open branch itself stays — a FOREIGN or otherwise
            # below-floor engine can still 404 this route — and
            # tests/test_health_service_checks.py exercises it directly.
            _log.warning("doctor_dangling_manifest_engine_floor", status=status)
            return [HealthResult(
                label=label, ok=False, warn=True,
                detail="SKIPPED (engine predates the index-run fence — "
                       "manifest_verify_all 404'd; re-run after the next "
                       "engine tag lands)",
            )]
        _log.warning("doctor_dangling_manifest_check_failed", error=str(exc))
        return [HealthResult(
            label=label, ok=False, warn=True,
            detail=f"SKIPPED (manifest_verify_all failed: {exc})",
        )]
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_dangling_manifest_check_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (catalog or engine unavailable)")]

    collections = result.get("collections") or []
    checked = len(collections)
    if checked == 0:
        # NON-VACUITY: zero collections actually compared is not a clean bill
        # of health, and must not render as one.
        return [HealthResult(
            label=label, ok=True,
            detail="skipped (no collection had a readable manifest to compare)",
        )]

    dangling: list[tuple[str, int, int]] = []  # (collection, n_dangling, n_referenced)
    for row in collections:
        try:
            name = str(row.get("collection", "?"))
            referenced = int(row.get("referenced", 0) or 0)
            missing = int(row.get("missing", 0) or 0)
        except (AttributeError, TypeError, ValueError) as exc:
            _log.debug(
                "doctor_dangling_manifest_row_skipped", row=row, error=str(exc),
            )
            continue
        if missing:
            dangling.append((name, missing, referenced))

    if not dangling:
        return [HealthResult(
            label=label, ok=True,
            detail=f"none ({checked} collection(s) checked)",
        )]

    names = "; ".join(
        f"{name} ({n_missing} of {n_ref} manifest row(s) missing a T3 chunk)"
        for name, n_missing, n_ref in dangling
    )
    detail = (
        f"{len(dangling)} collection(s) have manifest rows referencing chunks "
        f"that do not exist: {names}. A document in this state reports a "
        "chunk_count and returns nothing; re-indexing may silently no-op "
        f"(nexus-5xn3k). [{_DANGLING_MANIFEST_POPULATION_NOTE}]"
    )
    fix_suggestions = [
        "nx catalog reconcile          (rebuild manifests from T3)",
        "nx index <path> --force       (discard the staleness decision and re-index)",
    ]

    # nexus-heizf part 1: best-effort enrichment — name the actual damaged
    # documents (small count) or point at the enumeration command (large
    # count). A failure here must never downgrade or hide the
    # collection-level warning already established above.
    try:
        report = manifest_orphan_report(cat, [
            {"collection": name, "missing": n_missing, "referenced": n_ref}
            for name, n_missing, n_ref in dangling
        ])
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment only
        _log.debug("doctor_dangling_manifest_enumeration_failed", error=str(exc))
        report = None

    unroutable = report["unroutable_collections"] if report is not None else []
    incomplete = report["incomplete_collections"] if report is not None else {}

    if report is not None and report["total_rows"]:
        doc_ids: set[str] = set()
        chashes: set[str] = set()
        for coll_docs in report["collections"].values():
            for doc_id, info in coll_docs.items():
                doc_ids.add(doc_id)
                chashes.update(info["chashes"])
        n_docs = len(doc_ids)
        detail += (
            f" ({report['total_rows']} manifest row(s), {len(chashes)} "
            f"distinct chash(es), {n_docs} document(s))"
        )
    else:
        n_docs = 0

    if unroutable:
        detail += (
            f"; {len(unroutable)} collection(s) could not be enumerated "
            f"(unrecognized model routing or an enumeration error): "
            f"{', '.join(unroutable)}"
        )
    if incomplete:
        partials = "; ".join(
            f"{coll} ({info['enumerated']} of {info['expected']})"
            for coll, info in sorted(incomplete.items())
        )
        detail += (
            f"; {len(incomplete)} collection(s) only PARTIALLY enumerated "
            f"(row cap or an enumeration error) — counts are a LOWER "
            f"BOUND for them: {partials}"
        )

    if 0 < n_docs <= _DANGLING_MANIFEST_NAME_THRESHOLD and not unroutable and not incomplete:
        detail += f". Damaged document(s): {', '.join(sorted(doc_ids))}"
    else:
        detail += ". Run: nx catalog manifest-verify --list"

    return [HealthResult(
        label=label,
        ok=False,
        warn=True,
        detail=detail,
        fix_suggestions=fix_suggestions,
    )]


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
    model-token collections, same IN-list routing caveat as
    ``manifest_orphans``/``_check_dangling_manifests``). The LEGACY-DEBT
    tables (topic_assignments, frecency, relevance_log) are NOT covered —
    they are not dim-routable by construction (mixed identity space); this
    is a stated scope reduction relative to the local probe's four-table
    coverage, not a silent one.

    Engine-floor honesty (vw594 F3 / manifest_verify_all precedent): a
    pre-route engine 404s ``/chash/conformance`` — this degrades to a LOUD
    WARN naming the gap explicitly, never a silent/false clean pass. Any
    other failure (engine down, catalog unavailable) also degrades to a
    WARN or a benign skip, matching ``_check_dangling_manifests``'s
    fail-open-but-loud contract — this check must never crash `nx doctor`
    and must never read "couldn't check" as "checked, clean" (nexus-kmo9h).
    """
    label = _CHASH_CONFORMANCE_REPORT_LABEL
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return [HealthResult(label=label, ok=True, detail="skipped (no catalog)")]
    except Exception as exc:  # noqa: BLE001 — best-effort: failure logged, must not crash `nx doctor`
        _log.debug("doctor_chash_conformance_report_check_failed", error=str(exc))
        return [HealthResult(label=label, ok=True, detail="skipped (catalog unavailable)")]

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
        # NON-VACUITY (nexus-kmo9h): zero dims actually checked is not a
        # clean bill of health.
        return [HealthResult(label=label, ok=True, detail="skipped (no dim reachable)")]

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
    # above at every dim — same IN-list routing caveat as manifest_orphans
    # / _check_dangling_manifests (nexus-h1zu0). Left unstated, a tenant
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
            "routing caveat as manifest_orphans; nexus-h1zu0)."
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
                "nx catalog owners list        (find affected collections' repos)",
                "nx index repo <path>          (re-index file-backed collections, additive)",
                "nx upgrade                    (the chash-rekey rung recomputes conformant ids)",
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

#: v7.1.0 tag time (UTC) — the first PUBLIC release carrying the CLIENT-side
#: index-run fence (nexus-5xn3k.3, commit 4b0c5fb5). nexus-vw594 F3 / root
#: cause of nexus-biq4x: used ONLY to distinguish a quiescent-but-fence-aware
#: corpus (nothing indexed since the fence shipped — expected, still
#: ok=True) from the nexus-vw594 coverage-gap signature (a document indexed
#: AFTER this date carries no fence stamp, because its producer never calls
#: begin/complete at all — see the investigation memo, T2
#: nx memory get -p nexus -t "vw594-investigation-2026-08-04").
_FENCE_RELEASE_DT = datetime(2026, 8, 2, 22, 26, 0, tzinfo=UTC)


def _check_stale_indexing_runs() -> list[HealthResult]:
    """Name documents stranded in ``index_state='indexing'`` beyond a
    threshold (nexus-5xn3k.6, bead-text amendment 2026-08-02 —
    substantive-critic on .3's client diff, T2 nexus/5xn3k3-critique-2026-08-02).

    DISTINCT AXIS from :func:`_check_dangling_manifests`. That check
    (memo §3.2/§4, ``manifest_verify_all``) finds MISSING-CHUNK aggregates —
    it says nothing about a document whose fence was never cleared.
    ``'indexing'`` is the correct, SAFE state for an in-flight or crashed run
    (memo §3.5 / nexus-lcmbp non-goal: a document in ``'indexing'`` always
    re-indexes, never silently skips) but nothing bounds how LONG it can sit
    there. A rolling engine deploy that straddles one multi-batch run's
    begin/complete pair (begin lands on an upgraded pod, complete 404s
    against a not-yet-upgraded pod) strands a document in ``'indexing'``
    until a FUTURE full re-index happens to route both calls through
    upgraded pods; every intervening ``nx index`` pass re-chunks and
    re-embeds it at full cost with no signal distinguishing "still catching
    up" from "stuck."

    Surfaced ALONGSIDE the manifest_verify_all check, not folded into it —
    they detect different failure classes (missing chunks vs. a fence that
    never cleared).

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
    newest_reported_null_dt: datetime | None = None
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
                # since the fence shipped — §_FENCE_RELEASE_DT below tells
                # these two apart).
                reported_null += 1
                indexed_at = str(getattr(entry, "indexed_at", "") or "")
                if indexed_at:
                    try:
                        ia_dt = datetime.fromisoformat(indexed_at.replace("Z", "+00:00"))
                    except ValueError:
                        ia_dt = None
                    if ia_dt is not None and (
                        newest_reported_null_dt is None or ia_dt > newest_reported_null_dt
                    ):
                        newest_reported_null_dt = ia_dt
                continue
            checked += 1
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

    if (
        reported_null > 0
        and newest_reported_null_dt is not None
        and newest_reported_null_dt > _FENCE_RELEASE_DT
    ):
        # The vw594 signature: a document landed AFTER the fence existed
        # with no stamp at all — an unfenced producer wrote it. Never
        # ok=True for this (nexus-biq4x's misdiagnosis was exactly
        # rendering this case as a green pre-fence skip).
        results.append(HealthResult(
            label=label, ok=False, warn=True,
            detail=(
                f"{reported_null} document(s) report index_state but it "
                "is NULL on every one of them, and at least one was "
                f"indexed {newest_reported_null_dt.isoformat()} — after "
                f"the fence's {_FENCE_RELEASE_DT.isoformat()} release. "
                "The fence engine is live; a producer wrote this document "
                "without ever calling index-run begin/complete "
                "(nexus-vw594 coverage gap), not a pre-fence engine."
            ),
            fix_suggestions=[
                "nx index <path> --force   (re-index through a fenced producer)",
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
                f"beyond {_STALE_INDEXING_THRESHOLD_HOURS:.0f}h: {names}. This is "
                "SAFE (re-indexing never skips an 'indexing' document, "
                "nexus-lcmbp) but wastes a full re-chunk/re-embed on every "
                "intervening run — check for a stuck run or a rolling deploy "
                "that split a begin/complete pair across engine versions."
            ),
            fix_suggestions=[
                "nx index <path> --force   (clears the fence on a clean run)",
            ],
        ))

    if results:
        return results

    # Nothing WARN-worthy found — build the single honest ok=True summary.
    if checked > 0:
        return [HealthResult(
            label=label, ok=True,
            detail=f"none ({checked} fenced document(s) checked)",
        )]
    if reported_null > 0:
        # Quiescent: the fence is live but nothing has run through it yet
        # (fresh install, or a stable corpus untouched since the fence
        # shipped — catalog-020 does not retro-populate by design).
        return [HealthResult(
            label=label, ok=True,
            detail=f"fence live, 0 stale runs ({reported_null} document(s) "
                   "report index_state but none has run through the fence yet)",
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
        owners = cat.list_owners()
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


def run_health_checks() -> tuple[list[HealthResult], bool]:
    """Run all health checks.

    Returns (results, is_local_mode).
    """
    from nexus.config import is_local_mode  # noqa: PLC0415 — deferred to avoid circular import

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
        results.extend(_check_service_crossencoder_model())
    else:
        results.extend(_check_t3_cloud())

    # nexus-9tsdf (GH #1113 AC2): name dimension-orphaned collections and
    # point at `nx collection prune`. Applies in both modes; degrades
    # internally.
    results.extend(_check_dimension_orphans())
    # nexus-5xn3k AC5: a POPULATED manifest whose chashes no longer
    # resolve in T3 — the class `nx catalog reconcile` does not cover
    # (it handles chunk_count>0 with an EMPTY manifest, GH #1397).
    results.extend(_check_dangling_manifests())
    # RDR-180 (bead nexus-du2dw): managed/cloud-mode chash width-conformance
    # coverage via the engine route — the local nexus_diag psql probe inside
    # _check_migration_state (above) is LOCAL-ONLY by design (nexus-y3wuu)
    # and permanently blind on installs with no direct substrate access.
    # Runs ALONGSIDE the local check (distinct label, tenant-scoped, never
    # fed into the install-binary/convergence gates — see the check's own
    # docstring for the scoping rationale).
    results.extend(_check_chash_conformance_report())
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
    results.extend(_check_git_hooks())
    results.extend(_check_index_log())
    results.extend(_check_orphan_t1())
    results.extend(_check_orphan_checkpoints())
    results.extend(_check_orphan_pipelines())
    results.extend(_check_mineru_server())
    results.extend(_check_t2_integrity())
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
