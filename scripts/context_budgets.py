#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Context-size ceilings for the markdown a plugin loads into a model's context (nexus-3ceda.12).

    context_budgets.py check                              # exit 0 within every ceiling, 10 otherwise
    context_budgets.py update-exceptions [--allow-raise]  # rewrite the ceilings to today's sizes

Every tracked ``.md`` under a plugin's ``skills/``, ``agents/``, ``commands/`` or ``resources/``
(conexus, sn, prose-edit, tool-spill; resources hold what agents read at dispatch, such as
``agent-shared/CONTEXT_PROTOCOL.md``) is loaded into a model's context when it is used, so its size is tokens
spent per use. Each one has a byte ceiling in ``tests/fixtures/context-budgets.txt``
(``<path> <bytes>``; under ``tests/`` so a plugin growth commit that also raises its ceiling stays
eligible for the plugin-only fast lane, which refuses ``scripts/``). With ``slack = max(256 bytes,
5% of the file)``, a ceiling is written at the file's size plus one slack of headroom, so small edits
(a path fix, a reworded line) touch nothing:

- a file larger than its ceiling fails: cut it, move detail into a file loaded on demand (a net drop,
  since that file is counted too), or record a raise;
- a file with no ceiling fails: add it with ``update-exceptions --allow-raise`` (a new skill or agent
  is a deliberate cost, so it is recorded);
- a ceiling more than two slacks above its file fails, so the commit that really shrinks a file
  lowers its ceiling and the ceiling falls;
- an entry for a file that is gone fails.

``update-exceptions`` lowers and drops entries freely; raising one or adding a file needs
``--allow-raise`` and a reason in the commit message. The SubagentStart payload has its own byte
budgets in ``tests/hooks/test_subagent_start_byte_budget.py`` (it depends on runtime state, not on a
file's size).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PLUGINS: tuple[str, ...] = ("conexus", "sn", "prose-edit", "tool-spill")
KINDS: tuple[str, ...] = ("skills", "agents", "commands", "resources")
CEILINGS: str = "tests/fixtures/context-budgets.txt"
SLACK_MIN: int = 256
SLACK_FRACTION: float = 0.05

EXIT_OK: int = 0
EXIT_OVER: int = 10

_HEADER = """\
# Context-size ceilings in bytes (scripts/context_budgets.py, nexus-3ceda.12). One `<path> <bytes>`
# per line, for every tracked .md under a plugin's skills/, agents/, commands/ or resources/, at size plus
# max(256, 5%) of headroom. Ceilings only fall: a shrink of more than two headrooms lowers its entry in
# the same commit (`python3 scripts/context_budgets.py update-exceptions`); raising one or adding a
# file needs `--allow-raise` and a reason in the commit message.
"""


def measure(root: Path) -> dict[str, int]:
    """Byte size of every tracked in-scope markdown file, keyed by repo-relative path."""
    globs = [f"{p}/{k}/**.md" for p in PLUGINS for k in KINDS]
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *globs], check=True, capture_output=True
    ).stdout.decode()
    # Sized with LF line ends, as git stores them, so a CRLF checkout (core.autocrlf) measures the same.
    return {
        p: len((root / p).read_bytes().replace(b"\r\n", b"\n"))
        for p in sorted(out.split("\0"))
        if p and (root / p).is_file()
    }


def read_ceilings(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path.exists():
        return out
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise SystemExit(f"{path}:{n}: expected '<path> <bytes>', got {raw!r}")
        if parts[0] in out:
            raise SystemExit(f"{path}:{n}: {parts[0]} is listed twice")
        out[parts[0]] = int(parts[1])
    return out


def slack_allowed(size: int) -> int:
    return max(SLACK_MIN, int(SLACK_FRACTION * size))


def ceiling_for(size: int) -> int:
    """The ceiling written for a file of this size: one slack of headroom above it."""
    return size + slack_allowed(size)


def accepted(size: int, ceiling: int) -> bool:
    """Whether check accepts this ceiling: not exceeded, and at most two slacks above the file.
    The slack is taken from the ceiling, so a shrink by a whole headroom passes at every size."""
    return size <= ceiling <= size + 2 * slack_allowed(ceiling)


def check(root: Path) -> list[str]:
    sizes = measure(root)
    ceilings = read_ceilings(root / CEILINGS)
    errors: list[str] = []
    if not sizes:
        errors.append("counted no skill, agent or command markdown; the globs reach nothing")
    for p, n in sizes.items():
        c = ceilings.get(p)
        if c is None:
            errors.append(
                f"{p}: {n} bytes and no ceiling; a new context file is a recorded cost: "
                "python3 scripts/context_budgets.py update-exceptions --allow-raise"
            )
        elif n > c:
            errors.append(
                f"{p}: {n} bytes, over its ceiling of {c}; cut it or move detail into a file "
                "loaded on demand (a legitimate raise: update-exceptions --allow-raise, with the "
                "reason in the commit message)"
            )
        elif not accepted(n, c):
            errors.append(
                f"{p}: {n} bytes, {c - n} below its ceiling of {c} (more than {2 * slack_allowed(c)}); "
                "the shrink is real, so lower it in this commit: "
                "python3 scripts/context_budgets.py update-exceptions"
            )
    errors.extend(
        f"{p}: listed but not a tracked context file present in this checkout; remove it"
        for p in sorted(set(ceilings) - set(sizes))
    )
    return errors


def update_exceptions(root: Path, allow_raise: bool) -> list[str]:
    sizes = measure(root)
    old = read_ceilings(root / CEILINGS)
    refusals: list[str] = []
    if not allow_raise:
        for p, n in sizes.items():
            if p not in old:
                refusals.append(f"{p}: new entry at {n} bytes")
            elif n > old[p]:
                refusals.append(f"{p}: raise {old[p]} -> {n}")
    if refusals:
        return refusals
    new: dict[str, int] = {}
    for p, n in sizes.items():
        c = old.get(p)
        # An entry check accepts stays as it is (no churn on a shared file); a new, raised or
        # too-slack one is rewritten at the file's size plus its headroom.
        new[p] = c if c is not None and accepted(n, c) else ceiling_for(n)
    (root / CEILINGS).write_text(
        _HEADER + "".join(f"{p} {c}\n" for p, c in new.items()), encoding="utf-8", newline="\n"
    )
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    up = sub.add_parser("update-exceptions")
    up.add_argument("--allow-raise", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "update-exceptions":
        refusals = update_exceptions(args.root, args.allow_raise)
        for r in refusals:
            sys.stderr.write(f"refused: {r}\n")
        if refusals:
            sys.stderr.write("ceilings only fall; pass --allow-raise for a legitimate raise\n")
            return EXIT_OVER
        sys.stdout.write(f"wrote {CEILINGS}\n")
        return EXIT_OK
    errors = check(args.root)
    for e in errors:
        sys.stderr.write(f"error: {e}\n")
    sizes = measure(args.root)
    sys.stdout.write(f"context_budgets: {len(sizes)} files, {sum(sizes.values())} bytes, {len(errors)} errors\n")
    return EXIT_OVER if errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
