#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Report the flake quarantine, and add to it (RDR-258 Phase 5, nexus-2jomg.6).

    quarantine_report.py [report] [--registry P] [--today D] [--json] [--junit PATH]... [--expect-all]
    quarantine_report.py select --kind pytest|java [--format lines|surefire|surefire-exclude] [--registry P]
    quarantine_report.py classify --rc N [--junit PATH]...
    quarantine_report.py add --candidates F --bead B --reason R [--since D] [--review REF] [--registry P] NODEID...

``report`` prints, for each entry in ``tests/quarantine.toml``, its age in days, its deadline and the days left,
and flags an entry past its deadline (Sam, 2026-11-27: a quarantine lasts 14 days, then the test is fixed or
deleted). With ``--junit`` (a junit file, or a directory searched for ``*.xml``; repeatable) it joins each entry's
runs, "passed N of N", "failed k of N". Exit 1 when any entry is overdue; 3 under ``--expect-all`` when an entry
has no run that examined it (no testcase, or only skips); 2 on a registry or usage error; 0 otherwise. ``--json``
prints the same for machines.

``select`` prints the entries of one kind: one nodeid per line, or the ``-Dtest=`` value surefire takes
(``surefire``: only those tests; ``surefire-exclude``: everything but them).

``classify`` says whether a runner's exit code is acceptable for one quarantine pass, from the pass's junit: 0 is,
1 is only with a failed or errored testcase, anything else is not (exit 1 here means not acceptable).

``add`` writes an entry for each NODEID that the candidates file (``red_run_report.py candidates`` output) lists
under ``candidates``. It refuses a nodeid the file does not name. A nodeid found only under ``unattributed`` is
accepted only with ``--review <ref>`` (the 2-week review's decision, stored in the entry's reason), one nodeid per
call. ``--since`` may be today or yesterday (UTC), never later or earlier. The registry's ``source`` is the sha256
of the candidates file.

Standard library only; the registry format, the candidates reader and the junit tally are in
``tests/_quarantine_registry.py``.
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


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date (YYYY-MM-DD)") from exc


def _load(args: argparse.Namespace) -> list[reg.Entry]:
    try:
        return reg.load(args.registry)
    except OSError as exc:
        raise reg.RegistryError(f"cannot read the registry {args.registry}: {exc}") from exc


def _row(e: reg.Entry, today: date, cases: list[reg.Case] | None) -> dict[str, object]:
    row: dict[str, object] = {
        "nodeid": e.nodeid, "kind": e.kind, "since": e.since.isoformat(), "deadline": e.deadline.isoformat(),
        "bead": e.bead, "reason": e.reason, "source": e.source, "age_days": e.age_days(today),
        "days_left": e.days_left(today), "overdue": e.overdue(today),
    }
    if cases is not None:
        t = reg.tally(e, cases)
        row.update(result=t.text(), passed=t.passed, failed=t.failed, skipped=t.skipped, runs=t.runs, examined=t.examined)
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
        line = f"  {flag} {r['nodeid']} ({r['kind']})  age {r['age_days']}d  deadline {r['deadline']}  {when}  {r['bead']}"
        if "result" in r:
            line += f"  [{r['result']}]"
        lines.append(line)
    if overdue:
        lines.append(f"{overdue} past the 14-day deadline: fix or delete each test, then remove its entry")
    return "\n".join(lines) + "\n"


def _report(args: argparse.Namespace) -> int:
    if args.expect_all and not args.junit:
        raise reg.RegistryError("--expect-all needs --junit")
    today = args.today or reg.today_utc()
    entries = _load(args)
    cases = reg.read_junit(args.junit) if args.junit else None
    rows = [_row(e, today, cases) for e in entries]
    overdue = sum(1 for r in rows if r["overdue"])
    unexamined = [str(r["nodeid"]) for r in rows if args.expect_all and not r["examined"]]
    if args.json:
        print(json.dumps({"today": today.isoformat(), "count": len(rows), "overdue": overdue, "entries": rows}, indent=2))
    else:
        sys.stdout.write(_text(rows, today))
    if unexamined:
        print(f"{len(unexamined)} listed tests were not examined (no run, or only skips): " + ", ".join(unexamined))
    return 1 if overdue else 3 if unexamined else 0


def _select(args: argparse.Namespace) -> int:
    entries = [e for e in _load(args) if e.kind == args.kind]
    if args.format == "surefire":
        print(reg.surefire_selector(entries))
    elif args.format == "surefire-exclude":
        print(reg.surefire_exclusion(entries))
    else:
        for e in entries:
            print(e.nodeid)
    return 0


def _classify(args: argparse.Namespace) -> int:
    present = [p for p in args.junit if p.exists()]
    ok, why = reg.classify_exit(args.rc, reg.read_junit(present))
    print(f"quarantine pass: {why}")
    return 0 if ok else 1


def _add(args: argparse.Namespace) -> int:
    added = reg.add_entries(
        args.registry, args.candidates, args.nodeids, bead=args.bead, reason=args.reason,
        today=args.today or reg.today_utc(), since=args.since, review=args.review,
    )
    for e in added:
        print(f"quarantined {e.nodeid} ({e.kind}) since {e.since} until {e.deadline} ({e.bead})")
    return 0


def _parser() -> argparse.ArgumentParser:
    # --registry has no default on any parser (SUPPRESS), so a value given before or after the subcommand
    # survives; main() fills in the real default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", type=Path, default=argparse.SUPPRESS, help="the registry file (default tests/quarantine.toml)")
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0], parents=[common])
    p.add_argument("--today", type=_day, help="the date to age against (default: today, UTC)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--junit", type=Path, action="append", default=[], help="a junit file or a directory of them; repeatable")
    p.add_argument("--expect-all", action="store_true", help="with --junit: exit 3 when an entry was never examined")
    p.set_defaults(func=_report)
    sub = p.add_subparsers(dest="command")
    sel = sub.add_parser("select", parents=[common], help="print the entries of one kind")
    sel.add_argument("--kind", choices=reg.KINDS, required=True)
    sel.add_argument("--format", choices=("lines", "surefire", "surefire-exclude"), default="lines")
    sel.set_defaults(func=_select)
    cls = sub.add_parser("classify", help="is this runner exit code acceptable for a quarantine pass")
    cls.add_argument("--rc", type=int, required=True)
    cls.add_argument("--junit", type=Path, action="append", default=[])
    cls.set_defaults(func=_classify)
    add = sub.add_parser("add", parents=[common], help="quarantine nodeids the candidates file names")
    add.add_argument("--candidates", type=Path, required=True)
    add.add_argument("--bead", required=True)
    add.add_argument("--reason", required=True)
    add.add_argument("--since", type=_day, help="default: today (UTC); today or yesterday only")
    add.add_argument("--today", type=_day, help="the date to treat as today (tests)")
    add.add_argument("--review", help="the 2-week review's reference; required for a test only in `unattributed`")
    add.add_argument("nodeids", nargs="+", metavar="NODEID")
    add.set_defaults(func=_add)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not hasattr(args, "registry"):
        args.registry = reg.REGISTRY_PATH
    try:
        return args.func(args)
    except reg.RegistryError as exc:
        print(f"quarantine_report: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
