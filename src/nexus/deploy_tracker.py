# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``deployed-engine-version`` tracker, written from conexus's STEP-6 report.

nexus-nx3l5 (shape c, adopted 2026-08-28). The tracker used to be written by
hand: ``nx service record-deploy TAG --gate PASSED`` guarded the VALUE (a live
``/version`` read, nexus-dz6b1) but ``--gate`` was verbatim operator text. Two
failure modes followed from that, both observed in production:

* OMISSION — the step was simply not run (v0.1.17 stale across three deploys;
  v0.1.41 vs a cloud on 0.1.55, nexus-6igii).
* PREMATURE WRITE — ``gate PASSED`` written at 02:41:10Z on 2026-08-28 while
  the cloud gate it names had ~10 minutes left to run; run 1 reported RED 17 s
  later (T2 ``release-7.22.0-ship-2026-08-28``).

This module makes the gate's own report the source of the ``gate`` field. The
post-deploy cloud gate (conexus's STEP-6, ``deploy/gate/run-step6-regate.sh``
in that repo) writes one durable JSON report per run, never mutates one, and
records the engine version it gated FROM THE LIVE EDGE during the run. The
consumer here reads that report and writes the tracker; conexus stays
side-effect-free (their gate is test-only and must remain so).

Report contract (conexus-a4, 2026-08-28, read off the real files; recorded
verbatim on nexus-nx3l5):

* Discovery: ``<conexus checkout>/deploy/gate-report-<compactUTC>-v011.json``,
  glob :data:`GATE_REPORT_GLOB`. The files are GITIGNORED in conexus, so they
  are operator-local: a clone or CI cannot see them. The consumer takes the
  directory explicitly (or :data:`GATE_REPORT_DIR_ENV`) and a missing
  directory is a loud, named failure, never an empty set.
* ``schema_version`` (:data:`GATE_REPORT_SCHEMA_VERSION`, it has moved before —
  the real directory holds 51 schema-1 and 33 schema-2 files next to the
  schema-3 ones) and ``run_timestamp`` (ISO-8601, tz-aware, AUTHORITATIVE).
* The engine version the run gated is
  ``sections.preconditions.version_visibility.observed.release_version``,
  read from the live edge, so the invoker cannot assert it wrongly. NEVER
  ``identity.jar_version`` — that is the control-plane jar (``1.0-SNAPSHOT``).
* Verdict: ``overall.pass``. ``overall.advisories`` is always populated on a
  schema-3 report and is READ on green, never inferred empty — that is where
  a self-normalising latency regression hides.
* Authority when several reports exist for one version: the LATEST by
  ``run_timestamp`` (sort the field, not the filename). Any-green and
  first-wins are both wrong on real files: ``024127Z`` (red) and ``030235Z``
  (green) coexist for 0.1.88.

Discovery is lenient (a file that cannot name a version and a timestamp is
skipped and counted); the SELECTED report is validated strictly. Every refusal
is a named :class:`DeployTrackerError`; nothing is written on any of them.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: The report schema this consumer understands. Gate on it: it has moved before.
GATE_REPORT_SCHEMA_VERSION: int = 3
#: Report filenames in the conexus ``deploy/`` directory.
GATE_REPORT_GLOB: str = "gate-report-*.json"
#: Environment variable naming the conexus ``deploy/`` directory, for the
#: operator's box where both checkouts coexist.
GATE_REPORT_DIR_ENV: str = "NX_GATE_REPORT_DIR"

TRACKER_PROJECT: str = "nexus"
TRACKER_TITLE: str = "deployed-engine-version"
TRACKER_TAGS: str = "engine,deploy,tracker,rdr-179"


class DeployTrackerError(Exception):
    """Base: a named reason the tracker was NOT written."""


class GateReportDirectoryError(DeployTrackerError):
    """The report directory is missing or unreadable (operator-local files)."""


class GateReportSchemaError(DeployTrackerError):
    """The selected report is not a schema this consumer understands."""


class NoGateReportForVersion(DeployTrackerError):
    """No report in the directory gated the live engine version."""


class GateReportRed(DeployTrackerError):
    """The authoritative report for the live version did not pass."""


class GateReportVersionMismatch(DeployTrackerError):
    """An explicitly named report gated a different version than is live."""


class LiveVersionMismatch(DeployTrackerError):
    """The operator-named tag disagrees with the live ``/version`` read."""


@dataclass(frozen=True)
class GateReport:
    """One STEP-6 report, strictly validated (schema 3)."""

    path: Path
    schema_version: int
    run_timestamp: datetime
    release_version: str
    passed: bool
    failures: tuple[str, ...]
    advisories: tuple[Any, ...]

    @property
    def basename(self) -> str:
        return self.path.name


@dataclass(frozen=True)
class _Candidate:
    """What discovery can read off ANY report: enough to pick, not to trust."""

    path: Path
    release_version: str
    run_timestamp: datetime
    schema_version: Any


@dataclass(frozen=True)
class TrackerWrite:
    """What :func:`record_deploy_from_gate_report` wrote, for the caller to echo."""

    content: str
    live_version: str
    base_url: str
    report: GateReport


def _nested_release_version(doc: dict[str, Any]) -> str | None:
    try:
        value = doc["sections"]["preconditions"]["version_visibility"]["observed"]["release_version"]
    except (KeyError, TypeError):
        return None
    return value if isinstance(value, str) and value else None


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # The contract says tz-aware; a naive stamp cannot be ordered against
        # aware ones and must not be guessed at.
        return None
    return parsed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GateReportSchemaError(f"{path.name}: unreadable or not JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise GateReportSchemaError(f"{path.name}: top level is not an object")
    return doc


def _candidate(path: Path) -> _Candidate | None:
    """Lenient read: the version and timestamp, or ``None`` when absent."""
    try:
        doc = _read_json(path)
    except GateReportSchemaError:
        return None
    version = _nested_release_version(doc)
    stamp = _parse_timestamp(doc.get("run_timestamp"))
    if version is None or stamp is None:
        return None
    return _Candidate(
        path=path, release_version=version, run_timestamp=stamp,
        schema_version=doc.get("schema_version"),
    )


def load_gate_report(path: Path) -> GateReport:
    """Strict read of one report. Raises :class:`GateReportSchemaError` on any drift."""
    doc = _read_json(path)
    schema = doc.get("schema_version")
    if schema != GATE_REPORT_SCHEMA_VERSION:
        raise GateReportSchemaError(
            f"{path.name}: schema_version {schema!r}, this consumer understands "
            f"{GATE_REPORT_SCHEMA_VERSION}. The report format moved; update "
            "nexus.deploy_tracker against the new contract before trusting it."
        )
    stamp = _parse_timestamp(doc.get("run_timestamp"))
    if stamp is None:
        raise GateReportSchemaError(
            f"{path.name}: run_timestamp {doc.get('run_timestamp')!r} is missing or not a tz-aware ISO-8601 stamp"
        )
    version = _nested_release_version(doc)
    if version is None:
        raise GateReportSchemaError(
            f"{path.name}: sections.preconditions.version_visibility.observed.release_version is missing"
        )
    overall = doc.get("overall")
    if not isinstance(overall, dict) or not isinstance(overall.get("pass"), bool):
        raise GateReportSchemaError(f"{path.name}: overall.pass is missing or not a boolean")
    advisories = overall.get("advisories")
    if not isinstance(advisories, list):
        raise GateReportSchemaError(
            f"{path.name}: overall.advisories is missing — it is always populated on a "
            "schema-3 report, and a green run's advisories are read, never inferred empty"
        )
    failures = overall.get("failures")
    if failures is None:
        failures = []
    if not isinstance(failures, list):
        raise GateReportSchemaError(f"{path.name}: overall.failures is not a list")
    return GateReport(
        path=path,
        schema_version=schema,
        run_timestamp=stamp,
        release_version=version,
        passed=overall["pass"],
        failures=tuple(str(f) for f in failures),
        advisories=tuple(advisories),
    )


def discover_gate_reports(directory: Path) -> list[Path]:
    """Every report file in *directory*. A missing directory is a named refusal."""
    if not directory.is_dir():
        raise GateReportDirectoryError(
            f"gate-report directory {directory} does not exist or is not a directory. "
            "STEP-6 reports are gitignored in conexus (operator-local): point at the "
            f"conexus checkout's deploy/ directory on the box that ran the gate, or set {GATE_REPORT_DIR_ENV}."
        )
    try:
        return sorted(p for p in directory.glob(GATE_REPORT_GLOB) if p.is_file())
    except OSError as exc:
        raise GateReportDirectoryError(f"gate-report directory {directory} is unreadable ({exc})") from exc


def select_authoritative_report(paths: list[Path], release_version: str) -> GateReport:
    """The LATEST report (by ``run_timestamp``) that gated *release_version*.

    Discovery is lenient: files that cannot name a version and a tz-aware
    timestamp are skipped and counted. The selected report is then loaded
    strictly, so a latest-for-this-version report on an older schema REFUSES
    rather than letting an older schema-3 report vouch in its place.
    """
    candidates: list[_Candidate] = []
    skipped = 0
    seen_versions: set[str] = set()
    for path in paths:
        cand = _candidate(path)
        if cand is None:
            skipped += 1
            continue
        seen_versions.add(cand.release_version)
        if cand.release_version == release_version:
            candidates.append(cand)
    if not candidates:
        raise NoGateReportForVersion(
            f"no STEP-6 report gated release_version {release_version!r} "
            f"({len(paths)} report(s) scanned, {skipped} could not name a version/timestamp; "
            f"versions seen: {sorted(seen_versions) or 'none'}). The cloud gate has not "
            "reported on this deploy — the tracker is NOT written."
        )
    # Sort on the FIELD, not the filename; ties (microsecond stamps) break on name.
    latest = max(candidates, key=lambda c: (c.run_timestamp, c.path.name))
    _log.info(
        "deploy_tracker.report_selected",
        report=latest.path.name, release_version=release_version,
        candidates=len(candidates), skipped=skipped,
    )
    return load_gate_report(latest.path)


def require_green(report: GateReport) -> None:
    """Raise :class:`GateReportRed` unless the report passed."""
    if report.passed:
        return
    shown = "; ".join(report.failures[:3]) or "<no failure text>"
    more = f" (+{len(report.failures) - 3} more)" if len(report.failures) > 3 else ""
    raise GateReportRed(
        f"the authoritative STEP-6 report for release_version {report.release_version!r} "
        f"is RED: {report.basename} ({shown}{more}). A red gate is not a deploy to record."
    )


def gate_provenance(report: GateReport) -> str:
    """The tracker's ``gate`` field: derived from the report, never typed."""
    return f"PASSED {report.basename} (advisories: {len(report.advisories)})"


def format_advisory(advisory: Any) -> str:
    return advisory if isinstance(advisory, str) else json.dumps(advisory, sort_keys=True)


def tracker_content(*, live_version: str, base_url: str, commit: str, gate: str) -> str:
    parts = [f"engine-service-v{live_version} @ {commit or '<commit unrecorded>'}"]
    parts.append(f"recorded {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    parts.append(f"gate {gate or '<gate result unrecorded>'}")
    parts.append(f"verified live at {base_url}/version")
    return "; ".join(parts)


def write_deployed_engine_tracker(*, live_version: str, base_url: str, commit: str, gate: str) -> str:
    """The ONE writer of the tracker. Returns the content written.

    Callers have already asserted *live_version* against a live ``/version``
    read; this function never re-checks it and must not be reached otherwise.
    """
    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 — circular-dep avoidance; _helpers imports command surfaces

    content = tracker_content(live_version=live_version, base_url=base_url, commit=commit, gate=gate)
    with t2_handle() as handle:
        handle.memory.put(
            project=TRACKER_PROJECT,
            title=TRACKER_TITLE,
            content=content,
            tags=TRACKER_TAGS,
            # PERMANENT IS None, NOT 0 (nexus-6igii recurrence, 2026-07-26): the
            # engine rejects ttl<=0 loudly now (RDR-194 D5), and None is the only
            # value MemoryRepository.expire() reads as "never expires".
            ttl=None,
        )
    _log.info("deploy_tracker.written", live=live_version, base_url=base_url, gate=gate)
    return content


def record_deploy_from_gate_report(
    *,
    report_dir: Path | None = None,
    report_path: Path | None = None,
    url: str | None = None,
    commit: str = "",
    expected_version: str | None = None,
    commit_resolver: Callable[[str], str] | None = None,
) -> TrackerWrite:
    """Probe the live engine, find its authoritative STEP-6 report, write the tracker.

    Exactly one of *report_dir* (discover + select) or *report_path* (one named
    report, which must have gated the live version) is given. *expected_version*
    (``X.Y.Z``), when set, is asserted against the live read first — the
    operator-named-tag guard ``nx service record-deploy`` has always had.
    *commit_resolver*, when given, is called with the LIVE version after the
    probe and its result replaces *commit*: provenance must name the commit of
    what is actually running, which the caller cannot know before the probe
    (the floor tag is only a lower bound on the live version).

    Raises a :class:`DeployTrackerError` subclass (named reason, nothing
    written) or lets the probe's ``ManagedServiceError`` propagate.
    """
    if (report_dir is None) == (report_path is None):
        raise ValueError("record_deploy_from_gate_report: exactly one of report_dir / report_path")
    from nexus.db.managed_endpoint import (  # noqa: PLC0415 — circular-dep avoidance; managed_endpoint imports config
        probe_managed_service,
        resolve_managed_endpoint,
    )

    base = url or resolve_managed_endpoint(require_token=False)[0]
    caps = probe_managed_service(base_url=base)
    live = caps.release_version
    if commit_resolver is not None:
        commit = commit_resolver(live)
    if expected_version is not None and live != expected_version:
        raise LiveVersionMismatch(
            f"Refusing to record {expected_version!r}: the service at {caps.base_url} is running "
            f"release_version {live!r}, not {expected_version!r}. Deploy the tag first, then re-run "
            "(the tracker only records verified deploys)."
        )
    if report_path is not None:
        report = load_gate_report(report_path)
        if report.release_version != live:
            raise GateReportVersionMismatch(
                f"{report.basename} gated release_version {report.release_version!r}, but the "
                f"service at {caps.base_url} is running {live!r}. Name the report for the live "
                "version, or point --gate-report-dir at the directory and let selection pick it."
            )
    else:
        assert report_dir is not None
        report = select_authoritative_report(discover_gate_reports(report_dir), live)
    require_green(report)
    content = write_deployed_engine_tracker(
        live_version=live, base_url=caps.base_url, commit=commit, gate=gate_provenance(report),
    )
    return TrackerWrite(content=content, live_version=live, base_url=caps.base_url, report=report)


def gate_report_dir_from_env() -> Path | None:
    raw = os.environ.get(GATE_REPORT_DIR_ENV, "").strip()
    return Path(raw) if raw else None

