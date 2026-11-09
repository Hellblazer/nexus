#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""File-size and folder-shape ratchet: ceilings that only go down (nexus-3ceda.1).

    file_size_ratchet.py check                  # exit 0 within every cap and ceiling, 10 otherwise
    file_size_ratchet.py update-exceptions      # rewrite the exceptions files to today's sizes;
                                                # refuses to raise a ceiling or add an entry
    file_size_ratchet.py update-exceptions --allow-raise   # the rare legitimate raise or new entry

Size rule. Every tracked source file is counted in physical lines:

    source  src/nexus/**/*.py, service/src/main/**/*.java   warn over 800, fail over 1500
    test    tests/**/*.py, service/src/test/**/*.java        fail over 1000

A file over its fail cap passes only when ``scripts/file-size-exceptions.txt`` lists it as
``<path> <ceiling>`` and its size is at or below that ceiling. With ``slack = max(100 lines, 5%)``,
a ceiling is written at the file's size plus one slack of headroom, so an ordinary edit never touches
the exceptions file. A ceiling more than two slacks above its file fails, so the commit that splits
a file lowers its ceiling (``update-exceptions``) and the ceiling really falls. An entry for a file
now under its cap, or gone, fails until it is removed.

Shape rule, ``src/nexus`` only (``__init__.py`` counts as a module). A directory may hold at most 40
Python modules directly, and a module ``x.py`` may not sit beside a ``x_*.py`` family (make it a
package ``x/``). Exceptions live in ``scripts/folder-shape-exceptions.txt`` as
``dir <path> <ceiling>`` and ``family <path/x>``. A new directory over the cap, or a new family,
fails; a listed directory that grows past its ceiling only warns, because the repo's own recipes
(``.claude/rules/adding-a-cli-command.md``) still add modules to ``commands/``.

Out of scope by decision: ``scripts/`` and ``conexus/`` are not counted.

Before adding to a file over its cap, move the area you touch into its own module first.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SOURCE_SOFT_CAP: int = 800
SOURCE_HARD_CAP: int = 1500
TEST_CAP: int = 1000
DIR_MODULE_CAP: int = 40
#: One slack is max(SLACK_MIN, SLACK_FRACTION * size): a ceiling is seeded one slack above its file
#: and check fails it once it sits more than two slacks above (Sam, ask ratchet-rollout: headroom).
SLACK_MIN: int = 100
SLACK_FRACTION: float = 0.05

SOURCE_GLOBS: tuple[str, ...] = ("src/nexus/**.py", "service/src/main/**.java")
TEST_GLOBS: tuple[str, ...] = ("tests/**.py", "service/src/test/**.java")
SHAPE_ROOT: str = "src/nexus"

SIZE_EXCEPTIONS: str = "scripts/file-size-exceptions.txt"
SHAPE_EXCEPTIONS: str = "scripts/folder-shape-exceptions.txt"

EXIT_OK: int = 0
EXIT_OVER: int = 10

_SIZE_HEADER = """\
# File-size ceilings (scripts/file_size_ratchet.py, nexus-3ceda.1). One `<path> <ceiling>` per line,
# at the file's size plus max(100 lines, 5%) of headroom. A listed file may not grow past its
# ceiling. Ceilings only fall: a shrink of more than two headrooms lowers its entry in the commit that
# shrinks the file (`python3 scripts/file_size_ratchet.py update-exceptions`); raising one or adding
# a file needs `--allow-raise` and a reason in the commit message. Before adding to a file over its
# cap, move the area you touch into its own module first.
"""

_SHAPE_HEADER = """\
# Folder-shape exceptions (scripts/file_size_ratchet.py, nexus-3ceda.1).
#   dir <path> <ceiling>   a src/nexus directory allowed more than 40 direct modules, up to <ceiling>
#   family <path/x>        a module x.py allowed beside its x_*.py family
# Entries only shrink; adding or raising one needs `update-exceptions --allow-raise`.
"""


@dataclass(frozen=True)
class Findings:
    errors: list[str]
    warnings: list[str]
    notes: list[str]


def _git_files(root: Path, globs: tuple[str, ...]) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", *globs],
        check=True,
        capture_output=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p and (root / p).is_file())


def _count_lines(path: Path) -> int:
    data = path.read_bytes()
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def measure_sizes(root: Path) -> tuple[dict[str, int], dict[str, int]]:
    """Return (source sizes, test sizes) keyed by repo-relative path."""
    source = {p: _count_lines(root / p) for p in _git_files(root, SOURCE_GLOBS)}
    tests = {p: _count_lines(root / p) for p in _git_files(root, TEST_GLOBS)}
    return source, tests


def measure_shape(root: Path) -> tuple[dict[str, int], list[str]]:
    """Return (direct module count per directory, families as ``dir/x``) under SHAPE_ROOT."""
    per_dir: dict[str, list[str]] = {}
    for p in _git_files(root, (f"{SHAPE_ROOT}/**.py",)):
        parent, _, name = p.rpartition("/")
        per_dir.setdefault(parent, []).append(name[:-3])
    counts = {d: len(mods) for d, mods in per_dir.items()}
    families: list[str] = []
    for d, mods in per_dir.items():
        names = set(mods)
        for m in sorted(names):
            if m.startswith("_") or m == "__init__":
                continue
            if any(o.startswith(m + "_") for o in names):
                families.append(f"{d}/{m}")
    return counts, sorted(families)


def _read_pairs(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path.exists():
        return out
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise SystemExit(f"{path}:{n}: expected '<path> <ceiling>', got {raw!r}")
        if parts[0] in out:
            raise SystemExit(f"{path}:{n}: {parts[0]} is listed twice")
        out[parts[0]] = int(parts[1])
    return out


def _read_shape(path: Path) -> tuple[dict[str, int], set[str]]:
    dirs: dict[str, int] = {}
    fams: set[str] = set()
    if not path.exists():
        return dirs, fams
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if (
            parts[0] == "dir"
            and len(parts) == 3
            and parts[2].isdigit()
            and parts[1] not in dirs
        ):
            dirs[parts[1]] = int(parts[2])
        elif parts[0] == "family" and len(parts) == 2 and parts[1] not in fams:
            fams.add(parts[1])
        else:
            raise SystemExit(
                f"{path}:{n}: expected one 'dir <path> <ceiling>' or 'family <path/x>' per entry, got {raw!r}"
            )
    return dirs, fams


def _cap_for(is_test: bool) -> int:
    return TEST_CAP if is_test else SOURCE_HARD_CAP


def slack_allowed(size: int) -> int:
    return max(SLACK_MIN, int(SLACK_FRACTION * size))


def ceiling_for(size: int) -> int:
    """The ceiling written for a file of this size: one slack of headroom above it."""
    return size + slack_allowed(size)


def accepted(size: int, ceiling: int) -> bool:
    """Whether check accepts this ceiling: not exceeded, and at most two slacks above the file.
    The slack is taken from the ceiling, so a shrink by a whole headroom passes at every size."""
    return size <= ceiling <= size + 2 * slack_allowed(ceiling)


_RAISE_HINT = "(a legitimate raise: update-exceptions --allow-raise, with the reason in the commit message)"


def check(root: Path) -> Findings:
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    source, tests = measure_sizes(root)
    if not source or not tests:
        # Non-vacuity: a pathspec or checkout problem that counts nothing must not read as a pass.
        errors.append(
            f"counted {len(source)} source and {len(tests)} test files; expected both trees"
        )
    ceilings = _read_pairs(root / SIZE_EXCEPTIONS)
    sizes = {
        **{p: (n, False) for p, n in source.items()},
        **{p: (n, True) for p, n in tests.items()},
    }

    for p, (n, is_test) in sorted(sizes.items()):
        cap = _cap_for(is_test)
        ceiling = ceilings.get(p)
        if n > cap:
            if ceiling is None:
                errors.append(
                    f"{p}: {n} lines, over the {'test' if is_test else 'source'} cap of {cap}; "
                    f"move the area you touched into its own module first {_RAISE_HINT}"
                )
            elif n > ceiling:
                errors.append(
                    f"{p}: {n} lines, over its ceiling of {ceiling}; ceilings only fall, "
                    f"so move the area you touched into its own module first {_RAISE_HINT}"
                )
            elif not accepted(n, ceiling):
                errors.append(
                    f"{p}: {n} lines, {ceiling - n} below its ceiling of {ceiling} (more than "
                    f"{2 * slack_allowed(ceiling)}); the shrink is real, so lower it in this commit: "
                    "python3 scripts/file_size_ratchet.py update-exceptions"
                )
        elif not is_test and n > SOURCE_SOFT_CAP:
            warnings.append(f"{p}: {n} lines, over the soft cap of {SOURCE_SOFT_CAP}")
    for p, ceiling in sorted(ceilings.items()):
        # A stale entry fails: left in place, it would let the file regrow to the old ceiling.
        if p not in sizes:
            errors.append(
                f"{p}: listed with ceiling {ceiling} but not a tracked source or test file; remove it: "
                "python3 scripts/file_size_ratchet.py update-exceptions"
            )
        elif sizes[p][0] <= _cap_for(sizes[p][1]):
            errors.append(
                f"{p}: now within its cap; remove its exception: "
                "python3 scripts/file_size_ratchet.py update-exceptions"
            )

    counts, families = measure_shape(root)
    dir_ceilings, fam_allowed = _read_shape(root / SHAPE_EXCEPTIONS)
    for d, n in sorted(counts.items()):
        ceiling = dir_ceilings.get(d)
        if n > DIR_MODULE_CAP:
            if ceiling is None:
                errors.append(
                    f"{d}/: {n} modules, over the cap of {DIR_MODULE_CAP}; put new modules in a "
                    f"subpackage of {d}/ {_RAISE_HINT}"
                )
            elif n > ceiling:
                # A listed directory is already too broad and its recipes (adding-a-cli-command)
                # still add modules to it: growth warns rather than turning develop red.
                warnings.append(
                    f"{d}/: {n} modules, over its ceiling of {ceiling}; prefer a subpackage of {d}/"
                )
            elif n < ceiling:
                notes.append(
                    f"{d}/: {n} modules, below its ceiling of {ceiling}: lower it (update-exceptions)"
                )
    for d in sorted(dir_ceilings):
        if counts.get(d, 0) <= DIR_MODULE_CAP:
            notes.append(f"{d}/: within the module cap: remove its exception")
    for f in families:
        if f not in fam_allowed:
            errors.append(
                f"{f}.py sits beside {f}_*.py; make {f.rsplit('/', 1)[1]} a package {_RAISE_HINT}"
            )
    for f in sorted(fam_allowed - set(families)):
        notes.append(f"family {f}: no longer a family: remove its exception")
    return Findings(errors, warnings, notes)


def update_exceptions(root: Path, allow_raise: bool) -> list[str]:
    """Rewrite both exceptions files to today's state; return refusals (nothing written if any)."""
    source, tests = measure_sizes(root)
    old = _read_pairs(root / SIZE_EXCEPTIONS)
    over = {
        **{p: n for p, n in source.items() if n > SOURCE_HARD_CAP},
        **{p: n for p, n in tests.items() if n > TEST_CAP},
    }
    counts, families = measure_shape(root)
    old_dirs, old_fams = _read_shape(root / SHAPE_EXCEPTIONS)
    new_dirs = {d: n for d, n in counts.items() if n > DIR_MODULE_CAP}
    if not allow_raise:
        # check only warns when a listed directory grows, so an update that lowers file ceilings
        # keeps that directory's entry as it was rather than refusing the whole rewrite.
        new_dirs = {
            d: min(n, old_dirs[d]) if d in old_dirs else n for d, n in new_dirs.items()
        }
    new_fams = set(families)

    refusals: list[str] = []
    if not allow_raise:
        for p, n in sorted(over.items()):
            if p not in old:
                refusals.append(f"{p}: new exception at {n} lines")
            elif n > old[p]:
                refusals.append(f"{p}: raise {old[p]} -> {n}")
        for d, n in sorted(new_dirs.items()):
            if d not in old_dirs:
                refusals.append(f"{d}/: new directory exception at {n} modules")
        for f in sorted(new_fams - old_fams):
            refusals.append(f"family {f}: new family exception")
    if refusals:
        return refusals

    # An entry check accepts stays as it is (no churn on a shared file); a new, raised or too-slack
    # one is rewritten at the file's size plus its headroom.
    new = {
        p: old[p] if p in old and accepted(n, old[p]) else ceiling_for(n)
        for p, n in over.items()
    }
    body = "".join(f"{p} {n}\n" for p, n in sorted(new.items()))
    (root / SIZE_EXCEPTIONS).write_text(_SIZE_HEADER + body)
    shape = "".join(f"dir {d} {n}\n" for d, n in sorted(new_dirs.items()))
    shape += "".join(f"family {f}\n" for f in sorted(new_fams))
    (root / SHAPE_EXCEPTIONS).write_text(_SHAPE_HEADER + shape)
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    up = sub.add_parser("update-exceptions")
    up.add_argument("--allow-raise", action="store_true")
    args = ap.parse_args(argv)
    root: Path = args.root

    if args.cmd == "update-exceptions":
        refusals = update_exceptions(root, args.allow_raise)
        for r in refusals:
            sys.stderr.write(f"refused: {r}\n")
        if refusals:
            sys.stderr.write(
                "ceilings only fall; pass --allow-raise for a legitimate raise\n"
            )
            return EXIT_OVER
        sys.stdout.write(f"wrote {SIZE_EXCEPTIONS} and {SHAPE_EXCEPTIONS}\n")
        return EXIT_OK

    f = check(root)
    for w in f.warnings:
        sys.stdout.write(f"warning: {w}\n")
    for n in f.notes:
        sys.stdout.write(f"note: {n}\n")
    for e in f.errors:
        sys.stderr.write(f"error: {e}\n")
    sys.stdout.write(
        f"file_size_ratchet: {len(f.errors)} errors, {len(f.warnings)} warnings, {len(f.notes)} notes\n"
    )
    return EXIT_OVER if f.errors else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
