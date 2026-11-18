#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Assertions on the NATIVE engine's boot log for the RDR-246 data-move code (nexus-f3yqd.2.22).

The vendored ddl_utils and dml_utils changelogs use Liquibase ``sqlFile`` and ``createProcedure``
changesets that no other nexus changelog uses, and no native build had loaded them before the
0.1.195 candidate. The JVM tests cannot see a native-only failure (a missing reflection or resource
entry), so the release workflow's ``smoke-native-data-move`` job boots the native binary on a
provisioned PostgreSQL and hands the log to this script.

Two boot modes, one per PostgreSQL the engine meets:

``with-pg-background``
    A bundle that carries pg_background (the 0.1.194+ bundle). The engine must log BOTH
    ``event=data_move_library_applied`` lines, ``library=ddl_utils`` and ``library=dml_utils``, and
    neither ``event=data_move_libraries_skipped`` nor ``event=data_move_libraries_failed``.

``without-pg-background``
    A PostgreSQL with no pg_background (Sam's 8.4 ruling: 0.1.195 ships the move code with none on
    the cloud DB host). The engine must log ``event=data_move_libraries_skipped
    reason="pg_background is not installed"`` at WARN (the engine has a separate admin role, so the
    skip is not the expected single-role one), complete the nexus schema walk
    (``event=schema_migration_complete``), reach ``event=service_ready``, and apply no library.

Both modes also check the log masking of nexus-f3yqd.2.18: ``event=log_masking_installed`` with
``masking`` of at least 1, no ``event=log_masking_inactive``, and a MASKED line per secret. The
smoke makes the engine log a line that would carry each database password (a request to
``/v1/_whoami/<label>-<password>``, which the auth filter logs as ``event=request_failed
path=...``), and the log must show ``path=/v1/_whoami/<label>-<redacted>`` for it and must hold the
raw password nowhere. A log that holds a secret anywhere fails, whatever line carries it.

Every check reports how many lines it examined. A log with no lines at all, an unknown mode and a
run with no secret to look for are failures, never passes (the nexus-moht0 vacuous-gate rule).

Usage::

    NX_DB_PASS=... NX_DB_ADMIN_PASS=... \\
      check_native_data_move_boot.py --mode with-pg-background --log svc.log \\
        --secret svc=NX_DB_PASS --secret admin=NX_DB_ADMIN_PASS

Exit 0 all checks pass, 1 a check failed, 2 unusable input (unreadable or empty log, bad
arguments, an empty or unset secret variable). The secrets are read from the named environment
variables and are never printed.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

MODES: tuple[str, ...] = ("with-pg-background", "without-pg-background")

#: The libraries DmlUtilsInstaller walks, in its apply order.
LIBRARIES: tuple[str, ...] = ("ddl_utils", "dml_utils")

#: The route the masking probe requests. The auth filter logs ``path=<this><label>-<secret>`` at
#: WARN (status 401 on a bad token); the smoke script sends the same prefix.
PROBE_PATH_PREFIX = "/v1/_whoami/"

REDACTED = "<redacted>"

#: The skip reason DmlUtilsInstaller logs when the extension is absent.
NO_PG_BACKGROUND_REASON = "pg_background is not installed"

_APPLIED = re.compile(r"event=data_move_library_applied library=(\w+) changesets=(\d+) master=(\S+)")
_SKIPPED = re.compile(r'event=data_move_libraries_skipped reason="([^"]*)"')
_MASKING = re.compile(r"event=log_masking_installed masking=(\d+) unmasked=(\d+)")
_WARN = re.compile(r"(?:^|\s)WARN(?:\s|$)")


@dataclass(frozen=True)
class CheckResult:
    """One assertion: its name, whether it held, how many log lines it looked at, and why not."""

    name: str
    ok: bool
    examined: int
    detail: str = ""


@dataclass
class Report:
    """Every check of one run, in order."""

    mode: str
    lines: int
    results: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    def render(self) -> str:
        out = [f"native data-move boot check: mode={self.mode} log_lines={self.lines}"]
        for r in self.results:
            out.append(f"  {'ok  ' if r.ok else 'FAIL'} {r.name} (examined {r.examined} lines)"
                       + ("" if r.ok else f": {r.detail}"))
        out.append("PASS" if self.ok else "FAIL")
        return "\n".join(out)


def _first_line_no(lines: list[str], hits: list[str]) -> int:
    """The 1-based number of the first hit, cited instead of its text: nothing printed is taken from a log line,
    so a secret a broken mask left in one can never reach the report (CodeQL alert 62)."""
    return lines.index(hits[0]) + 1


def _lines(text: str) -> list[str]:
    return text.splitlines()


def check_libraries_applied(text: str) -> list[CheckResult]:
    """BOTH library-applied lines, one per library, and no skipped or failed line (a bundle with pg_background)."""
    lines = _lines(text)
    # The line is logged after the walk even when nothing was pending, so on the smoke's fresh cluster the line
    # alone shows the walk ran, not that it applied the library: require a changeset count above zero.
    applied: dict[str, int] = {}
    for ln in lines:
        m = _APPLIED.search(ln)
        if m:
            applied[m.group(1)] = max(applied.get(m.group(1), 0), int(m.group(2)))
    results = []
    for lib in LIBRARIES:
        if lib not in applied:
            known = [name for name in LIBRARIES if name in applied]
            others = len([name for name in applied if name not in LIBRARIES])
            ok, detail = False, f"no such line; applied: {known or 'none'}" + (f", plus {others} other name(s)" if others else "")
        elif applied[lib] < 1:
            ok, detail = False, "the line says changesets=0: on a fresh cluster the walk applied nothing"
        else:
            ok, detail = True, ""
        results.append(CheckResult(f"event=data_move_library_applied library={lib}", ok, len(lines), detail))
    skipped = [ln for ln in lines if _SKIPPED.search(ln)]
    results.append(CheckResult("no event=data_move_libraries_skipped on a bundle with pg_background", not skipped,
                               len(lines), "" if not skipped else f"the engine skipped the libraries (line {_first_line_no(lines, skipped)})"))
    failed = [ln for ln in lines if "event=data_move_libraries_failed" in ln]
    results.append(CheckResult("no event=data_move_libraries_failed", not failed, len(lines),
                               "" if not failed else f"a library changelog failed (line {_first_line_no(lines, failed)})"))
    return results


def _other_skips(lines: list[str]) -> str:
    """The skip lines with another reason, by count and first line number; the reason is free text the engine can
    fill with exception text, so it is never printed (review of 372016a55)."""
    hits = [ln for ln in lines if _SKIPPED.search(ln)]
    if not hits:
        return "no such line; no skip line at all"
    return f"no such line; {len(hits)} skip line(s) with another reason (first at line {_first_line_no(lines, hits)})"


def check_libraries_skipped_without_pg_background(text: str) -> list[CheckResult]:
    """The WARN skip, the completed nexus walk, a serving engine, and no library applied (a PostgreSQL without pg_background)."""
    lines = _lines(text)
    results = []
    skip = [ln for ln in lines if (m := _SKIPPED.search(ln)) and m.group(1) == NO_PG_BACKGROUND_REASON]
    results.append(CheckResult(
        f'event=data_move_libraries_skipped reason="{NO_PG_BACKGROUND_REASON}"', bool(skip), len(lines),
        "" if skip else _other_skips(lines)))
    results.append(CheckResult(
        "the skip is a WARN (a separate admin role, so not the expected single-role skip)",
        bool(skip) and all(_WARN.search(ln) for ln in skip), len(lines),
        "" if skip and all(_WARN.search(ln) for ln in skip) else "no skip line, or one not at WARN"))
    complete = [ln for ln in lines if "event=schema_migration_complete" in ln]
    results.append(CheckResult("event=schema_migration_complete (the nexus schema walk finished)", bool(complete), len(lines),
                               "" if complete else "no such line"))
    ready = [ln for ln in lines if "event=service_ready" in ln]
    results.append(CheckResult("event=service_ready (the engine serves)", bool(ready), len(lines),
                               "" if ready else "no such line"))
    applied = [ln for ln in lines if _APPLIED.search(ln)]
    results.append(CheckResult("no event=data_move_library_applied without pg_background", not applied, len(lines),
                               "" if not applied else f"a library applied (line {_first_line_no(lines, applied)})"))
    failed = [ln for ln in lines if "event=data_move_libraries_failed" in ln]
    results.append(CheckResult("no event=data_move_libraries_failed", not failed, len(lines),
                               "" if not failed else f"a library changelog failed (line {_first_line_no(lines, failed)})"))
    return results


def check_log_masking_installed(text: str) -> CheckResult:
    """``event=log_masking_installed`` with masking of at least 1, and no ``event=log_masking_inactive``."""
    lines = _lines(text)
    counts = [int(m.group(1)) for ln in lines if (m := _MASKING.search(ln))]
    if not counts:
        return CheckResult("event=log_masking_installed masking>=1", False, len(lines), "no such line")
    if max(counts) < 1:
        return CheckResult("event=log_masking_installed masking>=1", False, len(lines),
                           f"masking={max(counts)}: no appender masks")
    inactive = [ln for ln in lines if "event=log_masking_inactive" in ln]
    if inactive:
        return CheckResult("event=log_masking_installed masking>=1", False, len(lines),
                           f"the engine also logged log_masking_inactive (line {_first_line_no(lines, inactive)})")
    return CheckResult("event=log_masking_installed masking>=1", True, len(lines))


def check_masked_lines(text: str, labels: Sequence[str], values: Sequence[str]) -> list[CheckResult]:
    """Per probe: its line shows ``<redacted>`` in place of the value, and the raw value is in no line.

    ``labels[i]`` names the probe whose value is ``values[i]``; the smoke provoked its line with
    ``PROBE_PATH_PREFIX + label + "-" + value``. No probe, or an empty value, is a failure. The report is built
    from the labels and from line numbers only: a value is compared against lines and never copied into a
    result, so nothing printed flows from one (CodeQL alert 62).
    """
    lines = _lines(text)
    if not labels or len(labels) != len(values) or any(not v.strip() for v in values):
        return [CheckResult("masked line", False, len(lines), "no secret to look for (an empty or missing secret is not a pass)")]
    results = []
    for i, label in enumerate(labels):
        expected = f"path={PROBE_PATH_PREFIX}{label}-{REDACTED}"
        results.append(CheckResult(
            f"masked line: {expected}", any(expected in ln for ln in lines), len(lines),
            "no logged line shows the probe with its secret masked"))
        leaked = _lines_holding(lines, values[i])
        results.append(CheckResult(
            f"the {label} secret appears in no line", not leaked, len(lines),
            "" if not leaked else f"the raw secret is in line(s) {leaked[:5]} (a log line carries it unmasked)"))
    return results


def _lines_holding(lines: list[str], value: str) -> list[int]:
    """1-based numbers of the lines that contain *value*; only the numbers leave this function."""
    return [n for n, ln in enumerate(lines, start=1) if value in ln]


def run_checks(text: str, mode: str, probes: Mapping[str, str]) -> Report:
    """Every check for ``mode`` on the log ``text``, with ``probes`` as ``{label: value}`` (the tests' form)."""
    return run_checks_for(text, mode, list(probes), list(probes.values()))


def run_checks_for(text: str, mode: str, labels: Sequence[str], values: Sequence[str]) -> Report:
    """Every check for ``mode`` on the log ``text``. An unknown mode or a log with no lines fails."""
    n = len(_lines(text))
    report = Report(mode=mode, lines=n)
    if mode not in MODES:
        report.results.append(CheckResult("mode", False, n, f"unknown mode {mode!r}; one of {', '.join(MODES)}"))
        return report
    if n == 0 or not text.strip():
        report.results.append(CheckResult("the log has lines", False, n, "the log is empty: nothing was examined"))
        return report
    if mode == "with-pg-background":
        report.results.extend(check_libraries_applied(text))
    else:
        report.results.extend(check_libraries_skipped_without_pg_background(text))
    report.results.append(check_log_masking_installed(text))
    report.results.extend(check_masked_lines(text, labels, values))
    return report


def _probe_specs(specs: list[str]) -> tuple[list[str], list[str]]:
    """``label=ENV_NAME`` specs to (labels, variable names); raises ``ValueError`` on a bad spec."""
    labels: list[str] = []
    names: list[str] = []
    for spec in specs:
        label, sep, var = spec.partition("=")
        if not sep or not re.fullmatch(r"[a-z][a-z0-9]*", label) or not var:
            raise ValueError(f"--secret wants label=ENV_NAME with a lowercase alphanumeric label, got {spec!r}")
        labels.append(label)
        names.append(var)
    return labels, names


def _probe_values(labels: list[str], names: list[str]) -> list[str]:
    """The value of each named environment variable; raises ``ValueError`` naming the label and variable, never the value."""
    values = [os.environ.get(var, "") for var in names]
    for label, var, value in zip(labels, names, values, strict=True):
        if not value.strip():
            raise ValueError(f"--secret {label}: the environment variable {var} is unset or empty")
    return values


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--mode", required=True, choices=MODES)
    ap.add_argument("--log", required=True, type=Path)
    ap.add_argument("--secret", dest="probes", action="append", default=[], metavar="LABEL=ENV_NAME",
                    help="a secret to look for, as label=NAME of the environment variable holding it (repeatable, at least one)")
    args = ap.parse_args(argv)
    if not args.probes:
        print("check_native_data_move_boot: at least one --secret is required (the masking check is not optional)", file=sys.stderr)
        return 2
    try:
        labels, names = _probe_specs(args.probes)
        values = _probe_values(labels, names)
        text = args.log.read_text(encoding="utf-8", errors="replace")
    except (ValueError, OSError) as e:
        print(f"check_native_data_move_boot: {e}", file=sys.stderr)
        return 2
    if not text.strip():
        print(f"check_native_data_move_boot: {args.log} is empty", file=sys.stderr)
        return 2
    report = run_checks_for(text, args.mode, labels, values)
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
