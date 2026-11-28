#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a pass/fail pytest check in report mode (RDR-258 Phase 1, nexus-2jomg.2).

    python3 scripts/class_a_report.py --name <check> --min-tests <n> [--require PREFIX=N ...] [--items-out FILE]
        [--summary FILE] -- <pytest command...>

A class-A check with no value and no alarm line (the docs-reference-rot test, the mode-declarations census,
module_seam) never fails develop. This wraps the pytest command that runs it: the command's output streams as
usual, then every failing test is listed with its node id and its message (a census names its offenders in the
message), once as an Actions ``::warning::`` annotation and once in the job summary, and the exit is 0.

Items. Each failing test yields one ``CLASS_A_ITEM <check> <id>[<TAB><detail>]`` line on stdout and one row of the
``--items-out`` JSON (the job uploads it: the 2-week review and the 60-day guard-catch rule read it). ``<check>`` is
the test file's stem. ``<id>`` is what the test's message lists, one line each: a ``CLASS_A_ITEM <id>[<TAB><detail>]`` line the
test wrote itself (the census lists every offender this way, uncapped), else a ``path:line: detail`` line (the
docs-reference-rot and module_seam messages); a failing test whose message lists none is its own item, by node id.

It exits 1 only when the report examined too little to mean anything: pytest crashed or was interrupted (a
collection error, a usage error), exited 1 with no failing test (a crash is not a report), the command could not
start, the junit file is missing, fewer than ``--min-tests`` tests passed or failed, or a ``--require PREFIX=N``
is not met: at least N tests whose node id starts with PREFIX must have passed or failed. A PREFIX is written without the ``.py``
(``tests/test_x::test_name``): a CI step that names ``tests/test_x.py`` reads as a script the step runs, to the lab-entry sweep. ``--require`` is the
floor of one check, so a check that vanishes cannot hide behind another's tests. A pass over nothing is not a
report (the nexus-moht0 doctrine); a skipped test and a test that errored at setup examined nothing, so neither
counts. The first line of the report always says how many tests passed, failed and skipped (and errored).

The summary goes to ``--summary``, else to ``$GITHUB_STEP_SUMMARY`` when set, else nowhere.

STANDARD LIBRARY ONLY, python3 >= 3.11: it runs before any project environment exists.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

#: Lines of one item's message kept in the log and the summary; the rest is counted ("... N more lines").
MAX_MESSAGE_LINES = 60
MAX_MESSAGE_CHARS = 6000
#: One annotation: the Actions UI cuts a longer message anyway, and the full text is in the log and the summary.
ANNOTATION_CHARS = 1200
ITEM_TOKEN = "CLASS_A_ITEM"
#: A line the test wrote for the reporter: ``CLASS_A_ITEM <id>`` or ``CLASS_A_ITEM <id><TAB><detail>`` (an id may hold spaces:
#: a parametrized node id does).
_EXPLICIT = re.compile(rf"^\s*{ITEM_TOKEN} ([^\t]+?)(?:\t(.*))?$")
#: The exception name pytest puts in front of a message's first line (``AssertionError: ...``, ``Failed: ...``).
_EXCEPTION_PREFIX = re.compile(r"^[\w.]+: ")
#: ``path:line: detail``, the shape of the docs-reference-rot and module_seam messages.
_LOCATION = re.compile(r"^\s*([\w./-]+\.\w+:\d+): (.+)$")


@dataclass(frozen=True)
class Item:
    nodeid: str
    kind: str       # "failure" or "error"
    message: str    # the failure text shown to a reader
    summary: str = ""   # junit's message attribute: the assertion message alone, which items are read from


@dataclass(frozen=True)
class Counts:
    passed: int
    failed: int
    skipped: int
    errored: int = 0


def node_id(classname: str, name: str) -> str:
    """A pytest node id from a junit ``classname`` and ``name``: the dotted module path up to its ``test_*`` segment
    is a file, what follows is a class chain. A classname with no such segment is kept dotted."""
    if not classname:
        return name
    parts = classname.split(".")
    for i, part in enumerate(parts):
        if part.startswith("test_") or part.endswith("_test"):
            path = "/".join(parts[: i + 1]) + ".py"
            return "::".join([path, *parts[i + 1:], name])
    return f"{classname}::{name}"


def parse_junit(path: Path) -> tuple[Counts, list[Item], list[str]]:
    """The counts, the failing or erroring items, and the node ids of the tests that were examined (passed or failed).

    A test that errored (at setup, in a fixture, at teardown) is an item and is not examined. Raises ``ValueError`` (or
    ``OSError``) when the file cannot be read. Streamed, one test case at a time: the census job's file holds about
    63,000 skipped cases."""
    passed = failed = skipped = errored = 0
    items: list[Item] = []
    examined: list[str] = []
    try:
        for _event, case in ET.iterparse(path, events=("end",)):
            if case.tag != "testcase":
                continue
            nodeid = node_id(case.get("classname", ""), case.get("name", ""))
            bad = next((child for child in case if child.tag in ("failure", "error")), None)
            if bad is not None:
                if bad.tag == "failure":
                    failed += 1
                    examined.append(nodeid)
                else:
                    errored += 1
                text = (bad.text or "").strip() or bad.get("message", "")
                # The message attribute is the line pytest prints in its own summary; the body repeats it with the traceback.
                message = bad.get("message", "")
                if message and message not in text:
                    text = f"{message}\n{text}"
                items.append(Item(nodeid, bad.tag, text, message))
            elif any(child.tag == "skipped" for child in case):
                skipped += 1
            else:
                passed += 1
                examined.append(nodeid)
            case.clear()
    except ET.ParseError as exc:
        raise ValueError(f"cannot parse {path}: {exc}") from exc
    return Counts(passed, failed, skipped, errored), items, examined


def check_of(nodeid: str) -> str:
    """The check a node id belongs to: its test file's stem."""
    return Path(nodeid.split("::", 1)[0]).stem


def item_rows(item: Item) -> list[tuple[str, str]]:
    """``(id, detail)`` of what a failing test lists in its message; the test's own node id when it lists nothing."""
    rows: list[tuple[str, str]] = []
    for n, line in enumerate(item.summary.splitlines()):
        if n == 0:
            line = _EXCEPTION_PREFIX.sub("", line, count=1)
        m = _EXPLICIT.match(line) or _LOCATION.match(line)
        if m:
            rows.append((m.group(1), (m.group(2) or "").strip()))
    return rows or [(item.nodeid, "")]


def _cut(message: str) -> tuple[str, int]:
    """*message* limited to :data:`MAX_MESSAGE_LINES` lines and :data:`MAX_MESSAGE_CHARS` characters, and how many lines it lost."""
    lines = message.splitlines()
    kept = lines[:MAX_MESSAGE_LINES]
    text = "\n".join(kept)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS]
        kept = text.splitlines()
    return text, len(lines) - len(kept)


def _say(line: str = "") -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _annotation(text: str) -> str:
    """One workflow-command line: percent, CR and LF are escaped as the Actions runner reads them."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _counts_text(counts: Counts) -> str:
    text = f"{counts.passed} passed, {counts.failed} failed, {counts.skipped} skipped"
    return text + (f", {counts.errored} errored" if counts.errored else "")


def render(name: str, counts: Counts, items: list[Item]) -> tuple[str, str]:
    """(the log text, the markdown summary) of one report. The item lines come last in the log, one per listed item."""
    head = f"class-A report [{name}]: {_counts_text(counts)}"
    log = [head]
    md = [f"### {name}", "", f"{_counts_text(counts)}. Report mode: nothing here fails develop."]
    for item in items:
        text, lost = _cut(item.message)
        more = f"\n... {lost} more line{'s' if lost != 1 else ''}" if lost else ""
        log += ["", f"::warning title=class-A {name}::" + _annotation(f"{item.nodeid} ({item.kind})\n{text[:ANNOTATION_CHARS]}"),
                f"{item.nodeid} ({item.kind})", *(f"  {ln}" for ln in (text + more).splitlines())]
        md += ["", f"#### `{item.nodeid}` ({item.kind})", "", "```text", text + more, "```"]
    if not items:
        md += ["", "No failing items."]
    return "\n".join(log), "\n".join(md) + "\n"


def item_lines(items: list[Item]) -> list[str]:
    """The fixed-format lines, ``CLASS_A_ITEM <check> <id> [<detail>]``, for every item of every failing test."""
    out = []
    for item in items:
        check = check_of(item.nodeid)
        out += [f"{ITEM_TOKEN} {check} {i}" + (f"\t{d}" if d else "") for i, d in item_rows(item)]
    return out


def parse_require(text: str) -> tuple[str, int]:
    prefix, sep, number = text.rpartition("=")
    if not sep or not prefix or not number.isdigit() or int(number) < 1:
        raise argparse.ArgumentTypeError(f"--require takes PREFIX=N with N a whole number >= 1, got {text!r}")
    return prefix, int(number)


def run_report(name: str, min_tests: int, command: list[str], summary: Path | None,
               require: list[tuple[str, int]] = (), items_out: Path | None = None) -> int:
    with tempfile.TemporaryDirectory(prefix="class-a-report.") as tmp:
        junit = Path(tmp) / "junit.xml"
        try:
            returncode = subprocess.run([*command, f"--junitxml={junit}"], check=False).returncode
        except OSError as exc:
            _say(f"::error title=class-A {name}::the check could not start ({type(exc).__name__}: {exc})")
            _say(f"class-A report [{name}]: could not start {command[0]}: {exc}")
            return 1
        try:
            counts, items, examined_ids = parse_junit(junit)
        except (OSError, ValueError) as exc:
            _say(f"::error title=class-A {name}::no readable junit file after the run (pytest exit {returncode}): {exc}")
            return 1
    log, md = render(name, counts, items)
    lines = item_lines(items)
    _say()
    _say(log)
    for line in lines:
        _say(line)
    if summary is not None:
        try:
            with summary.open("a", encoding="utf-8") as fh:
                fh.write(md)
        except OSError as exc:
            _say(f"class-A report [{name}]: could not write the summary {summary}: {exc}")
    plain = [i.replace(".py::", "::", 1) if ".py::" in i else (i[:-3] if i.endswith(".py") else i) for i in examined_ids]
    required = [{"prefix": p, "min": n, "examined": sum(1 for i in plain if i.startswith(p))} for p, n in require]
    if items_out is not None:
        rows = [{"check": check_of(it.nodeid), "test": it.nodeid, "kind": it.kind, "id": i, "detail": d}
                for it in items for i, d in item_rows(it)]
        payload = {"name": name, "pytest_exit": returncode, "passed": counts.passed, "failed": counts.failed,
                   "skipped": counts.skipped, "errored": counts.errored, "required": required, "items": rows}
        try:
            items_out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        except OSError as exc:
            _say(f"class-A report [{name}]: could not write the items file {items_out}: {exc}")
    examined = counts.passed + counts.failed
    if examined == 0:
        _say(f"::error title=class-A {name}::examined 0 tests ({counts.skipped} skipped, {counts.errored} errored, pytest exit {returncode}): a report over nothing is not a report")
        return 1
    if returncode not in (0, 1):
        _say(f"::error title=class-A {name}::pytest exited {returncode} (not a pass or a test failure): the check did not run to the end, so this report is partial")
        return 1
    if returncode == 1 and counts.failed + counts.errored == 0:
        _say(f"::error title=class-A {name}::pytest exited 1 although no test failed or errored: a session-level failure of the run (a plugin's verdict, a fence), and a crash is not a report")
        return 1
    if examined < min_tests:
        _say(f"::error title=class-A {name}::examined {examined} tests, under the floor of {min_tests}: the selection or the tests moved")
        return 1
    unmet = [r for r in required if r["examined"] < r["min"]]
    for r in unmet:
        _say(f"::error title=class-A {name}::examined {r['examined']} test(s) under {r['prefix']}, under its floor of {r['min']}: that check's tests are skipped, errored or gone")
    return 1 if unmet else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0],
                                     usage="%(prog)s --name NAME --min-tests N [--require PREFIX=N ...] [--items-out FILE] [--summary FILE] -- COMMAND...")
    parser.add_argument("--name", required=True, help="the report's name, as the log and the annotations print it")
    parser.add_argument("--min-tests", required=True, type=int, help="fewer tests than this passing or failing is a failed report, not a pass")
    parser.add_argument("--require", action="append", default=[], type=parse_require, metavar="PREFIX=N",
                        help="at least N examined tests whose node id starts with PREFIX (one check's floor; repeatable)")
    parser.add_argument("--items-out", type=Path, default=None, help="write the failing items, counts and floors as JSON here")
    parser.add_argument("--summary", type=Path, default=None, help="append the markdown summary here (default: $GITHUB_STEP_SUMMARY)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the pytest command, after --; --junitxml is added to it")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("no pytest command after --")
    summary = args.summary or (Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None)
    return run_report(args.name, args.min_tests, command, summary, args.require, args.items_out)


if __name__ == "__main__":
    sys.exit(main())
