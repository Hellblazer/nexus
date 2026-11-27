#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report the flake quarantine, and add to it (RDR-258 Phase 5, nexus-2jomg.6).

    scripts/quarantine_report.py [--registry P] [--today D] [--json] [--results F]
    scripts/quarantine_report.py count [--registry P]
    scripts/quarantine_report.py add --candidates F --bead B --reason R [--since D] [--registry P] NODEID...

The report prints, for each entry in ``tests/quarantine.toml``, its age in days, its deadline and the days left,
and flags an entry past its deadline (Sam, 2026-11-27: a quarantine lasts 14 days, then the test is fixed or
deleted). It exits 1 when any entry is overdue, 2 on a registry or usage error, 3 under ``--expect-all`` when a
listed test has no result (a run that never examined it), 0 otherwise. ``--json`` prints the
same for machines. ``--results F`` joins the outcomes a ``NX_QUARANTINE_RESULTS`` run wrote (tests/_quarantine.py);
a test that passed is not released by this script or by any job: remove its entry by hand.

``count`` prints the number of entries and nothing else (the nightly workflow's first job reads it).

``add`` writes an entry for each NODEID that the candidates file (``red_run_report.py candidates`` output) lists
under ``flake_infra``. It refuses a nodeid the file does not name, and one found only under ``unattributed``, which
is a class Sam decides. The registry's ``source`` is the sha256 of the candidates file.

Standard library only; the registry format and the candidates reader are in ``tests/_quarantine_registry.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests import _quarantine_registry as reg  # noqa: E402

_PASSED_NOTE = "passed: release by hand (remove the entry)"


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date (YYYY-MM-DD)") from exc


def _load(path: Path) -> list[reg.Entry]:
    try:
        return reg.load(path)
    except OSError as exc:
        raise reg.RegistryError(f"cannot read the registry {path}: {exc}") from exc


def _results(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        results = doc["results"]
        if not isinstance(results, dict):
            raise TypeError("results is not an object")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise reg.RegistryError(f"cannot read the results file {path}: {exc}") from exc
    return results


def _row(e: reg.Entry, today: date, results: dict[str, str] | None) -> dict[str, object]:
    row: dict[str, object] = {
        "nodeid": e.nodeid, "since": e.since.isoformat(), "deadline": e.deadline.isoformat(), "bead": e.bead,
        "reason": e.reason, "source": e.source, "age_days": e.age_days(today), "days_left": e.days_left(today),
        "overdue": e.overdue(today),
    }
    if results is not None:
        row["result"] = results.get(e.nodeid, "not run")
    return row


def _text(rows: list[dict[str, object]], today: date) -> str:
    if not rows:
        return "0 quarantined\n"
    overdue = sum(1 for r in rows if r["overdue"])
    lines = [f"{len(rows)} quarantined, {overdue} overdue (today {today.isoformat()})"]
    for r in rows:
        left = int(r["days_left"])  # type: ignore[call-overload]
        when = f"{-left} days overdue" if left < 0 else f"{left} days left"
        flag = "OVERDUE" if r["overdue"] else "ok     "
        line = f"  {flag} {r['nodeid']}  age {r['age_days']}d  deadline {r['deadline']}  {when}  {r['bead']}"
        if "result" in r:
            line += f"  [{r['result']}" + (f" -- {_PASSED_NOTE}" if r["result"] == "passed" else "") + "]"
        lines.append(line)
    if overdue:
        lines.append(f"{overdue} past the 14-day deadline: fix or delete each test, then remove its entry")
    return "\n".join(lines) + "\n"


def _report(args: argparse.Namespace) -> int:
    if args.expect_all and args.results is None:
        raise reg.RegistryError("--expect-all needs --results")
    today = args.today or date.today()
    entries = _load(args.registry)
    results = _results(args.results)
    rows = [_row(e, today, results) for e in entries]
    overdue = sum(1 for r in rows if r["overdue"])
    unrun = [r["nodeid"] for r in rows if r.get("result") == "not run"] if args.expect_all else []
    if args.json:
        print(json.dumps({"today": today.isoformat(), "count": len(rows), "overdue": overdue, "entries": rows}, indent=2))
    else:
        sys.stdout.write(_text(rows, today))
    if unrun:
        print(f"{len(unrun)} listed tests did not run (the run examined nothing for them): " + ", ".join(map(str, unrun)))
    return 1 if overdue else 3 if unrun else 0


def _count(args: argparse.Namespace) -> int:
    print(len(_load(args.registry)))
    return 0


def _add(args: argparse.Namespace) -> int:
    added = reg.add_entries(
        args.registry, args.candidates, args.nodeids, bead=args.bead, reason=args.reason, since=args.since or date.today()
    )
    for e in added:
        print(f"quarantined {e.nodeid} since {e.since} until {e.deadline} ({e.bead})")
    return 0


def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", type=Path, default=reg.REGISTRY_PATH, help="the registry file (default tests/quarantine.toml)")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], parents=[common])
    p.add_argument("--today", type=_day, help="the date to age against (default: today)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--results", type=Path, help="the NX_QUARANTINE_RESULTS file of a run")
    p.add_argument("--expect-all", action="store_true", help="with --results: exit 3 when a listed test has no result")
    p.set_defaults(func=_report)
    sub = p.add_subparsers(dest="command")
    sub.add_parser("count", parents=[common], help="print the number of entries").set_defaults(func=_count)
    add = sub.add_parser("add", parents=[common], help="quarantine nodeids the candidates file names under flake_infra")
    add.add_argument("--candidates", type=Path, required=True)
    add.add_argument("--bead", required=True)
    add.add_argument("--reason", required=True)
    add.add_argument("--since", type=_day, help="default: today")
    add.add_argument("nodeids", nargs="+", metavar="NODEID")
    add.set_defaults(func=_add)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except reg.RegistryError as exc:
        print(f"quarantine_report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
