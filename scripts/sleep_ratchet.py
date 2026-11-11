#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sleep-count ratchet: the test tree's ``time.sleep`` calls stay under a ceiling that only falls
(nexus-3ceda.11).

    sleep_ratchet.py check                  # exit 0 within the ceiling, 10 otherwise
    sleep_ratchet.py update                 # rewrite the ceiling for today's count; refuses a raise
    sleep_ratchet.py update --allow-raise   # the rare legitimate raise

A test that sleeps is guessing how long the work takes, and under load it guesses wrong. Wait on the
work instead: ``settle()`` on what is still pending, or a virtual clock (``tests/_settle.py``
``FakeClock``) for code that takes ``clock=``.

Counted: every call of ``time.sleep`` in a tracked ``tests/**/*.py`` file, through ``import time``
(any alias) or ``from time import sleep`` (any alias), read from the syntax tree. Not counted: a
sleep inside a string (a child process's script), ``asyncio.sleep``, a clock object's ``sleep``,
``time.sleep`` held as a value and called by another name, ``Event().wait(n)``, and a file that does
not parse. The count is a floor that guards against drift, not a census of every wait.

``scripts/sleep-ceiling.txt`` holds one ``tests <ceiling>`` line. With
``slack = max(10 calls, 5%)``, the ceiling is written at the count plus one slack, so an ordinary
edit never touches it; a ceiling more than two slacks above the count fails, so the commit that
removes more than about one slack of sleeps lowers it (``update``) and the ceiling really falls.
``check`` reads only the committed ceiling; a raise is refused by ``update`` and seen in review. Headroom, like the file-size
ratchet: Sam 2026-11-10, relayed by halcyon.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

TEST_GLOBS: tuple[str, ...] = ("tests/**.py",)
CEILING_FILE: str = "scripts/sleep-ceiling.txt"
#: One slack is max(SLACK_MIN, SLACK_PERCENT% of the count), rounded up.
SLACK_MIN: int = 10
SLACK_PERCENT: int = 5

EXIT_OK: int = 0
EXIT_OVER: int = 10

HEADER: str = """\
# Sleep-count ceiling (scripts/sleep_ratchet.py, nexus-3ceda.11): the most time.sleep calls the
# tracked tests/**/*.py files may hold, at today's count plus max(10 calls, 5%) of headroom. It only
# falls: removing more than one headroom of sleeps lowers it in the same commit
# (`python3 scripts/sleep_ratchet.py update`); raising it needs `update --allow-raise` and a reason.
"""

_HINT = "wait on the work instead: settle() or FakeClock in tests/_settle.py"


@dataclass(frozen=True)
class Findings:
    errors: list[str]
    count: int
    ceiling: int | None


def _git_files(root: Path, globs: tuple[str, ...]) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *globs],
        check=True,
        capture_output=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p and (root / p).is_file())


def _sleeps_in(source: str) -> int:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter(
                "ignore", SyntaxWarning
            )  # a test file's own escape-sequence slips
            tree = ast.parse(source)
    except SyntaxError:
        return 0
    modules: set[str] = set()  # names bound to the time module
    funcs: set[str] = set()  # names bound to time.sleep
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            funcs.update(a.asname or a.name for a in node.names if a.name == "sleep")
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "sleep"
            and isinstance(f.value, ast.Name)
            and f.value.id in modules
        ):
            count += 1
        elif isinstance(f, ast.Name) and f.id in funcs:
            count += 1
    return count


def count_sleeps(root: Path) -> dict[str, int]:
    """``{path: calls}`` for every tracked test file holding at least one ``time.sleep`` call."""
    counts: dict[str, int] = {}
    for rel in _git_files(root, TEST_GLOBS):
        n = _sleeps_in((root / rel).read_text(encoding="utf-8", errors="replace"))
        if n:
            counts[rel] = n
    return counts


def slack(count: int) -> int:
    return max(SLACK_MIN, -(-count * SLACK_PERCENT // 100))


def ceiling_for(count: int) -> int:
    """The ceiling written for this count: one slack of headroom above it."""
    return count + slack(count)


def read_ceiling(root: Path) -> int | None:
    path = root / CEILING_FILE
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) == 2 and parts[0] == "tests":
            return int(parts[1])
    return None


def check(root: Path) -> Findings:
    files = _git_files(root, TEST_GLOBS)
    count = sum(count_sleeps(root).values())
    ceiling = read_ceiling(root)
    errors: list[str] = []
    if not files:
        errors.append(
            "counted no test files: the glob reached nothing, so a pass would mean nothing"
        )
    if ceiling is None:
        errors.append(
            f"no ceiling: {CEILING_FILE} needs a `tests <ceiling>` line (`sleep_ratchet.py update`)"
        )
    elif count > ceiling:
        errors.append(
            f"{count} time.sleep calls in tests/, over the ceiling {ceiling}: {_HINT}"
        )
    elif ceiling > count + 2 * slack(count):
        errors.append(
            f"the ceiling {ceiling} is more than two headrooms above the {count} time.sleep calls in tests/: "
            f"lower it to {ceiling_for(count)} (`python3 scripts/sleep_ratchet.py update`)"
        )
    return Findings(errors=errors, count=count, ceiling=ceiling)


def update(root: Path, *, allow_raise: bool) -> int:
    if not _git_files(root, TEST_GLOBS):
        sys.stderr.write(
            "refused: counted no test files, so a ceiling written now would mean nothing\n"
        )
        return EXIT_OVER
    count = sum(count_sleeps(root).values())
    new = ceiling_for(count)
    old = read_ceiling(root)
    if old is not None and new > old and not allow_raise:
        sys.stderr.write(
            f"refused: {count} time.sleep calls would raise the ceiling {old} to {new}; {_HINT} "
            "(a legitimate raise: update --allow-raise, with the reason in the commit message)\n"
        )
        return EXIT_OVER
    (root / CEILING_FILE).write_text(f"{HEADER}tests {new}\n", encoding="utf-8")
    sys.stdout.write(f"{CEILING_FILE}: tests {new} ({count} calls)\n")
    return EXIT_OK


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sleep-count ratchet for tests/ (nexus-3ceda.11)."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    up = sub.add_parser("update")
    up.add_argument("--allow-raise", action="store_true")
    args = parser.parse_args(argv)
    root = root or Path(__file__).resolve().parents[1]
    if args.cmd == "update":
        return update(root, allow_raise=args.allow_raise)
    f = check(root)
    for e in f.errors:
        sys.stderr.write(f"{e}\n")
    if not f.errors:
        sys.stdout.write(f"{f.count} time.sleep calls in tests/, ceiling {f.ceiling}\n")
    return EXIT_OVER if f.errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
