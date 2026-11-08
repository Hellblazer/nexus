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
``<path> <ceiling>`` and its size is at or below that ceiling. A ceiling above the file's size is
slack and is reported so the commit that shrank the file can lower it (``update-exceptions``); an
entry for a file now under its cap, or gone, is reported for removal.

Shape rule, ``src/nexus`` only. A directory may hold at most 40 Python modules directly, and a
module ``x.py`` may not sit beside a ``x_*.py`` family (make it a package ``x/``). Exceptions live
in ``scripts/folder-shape-exceptions.txt`` as ``dir <path> <ceiling>`` and ``family <path/x>``.

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

SOURCE_GLOBS: tuple[str, ...] = ("src/nexus/**.py", "service/src/main/**.java")
TEST_GLOBS: tuple[str, ...] = ("tests/**.py", "service/src/test/**.java")
SHAPE_ROOT: str = "src/nexus"

SIZE_EXCEPTIONS: str = "scripts/file-size-exceptions.txt"
SHAPE_EXCEPTIONS: str = "scripts/folder-shape-exceptions.txt"

EXIT_OK: int = 0
EXIT_OVER: int = 10

_SIZE_HEADER = """\
# File-size ceilings (scripts/file_size_ratchet.py, nexus-3ceda.1). One `<path> <ceiling>` per line.
# A listed file may not grow past its ceiling. Ceilings only fall: lower one in the commit that
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
        if parts[0] == "dir" and len(parts) == 3 and parts[2].isdigit():
            dirs[parts[1]] = int(parts[2])
        elif parts[0] == "family" and len(parts) == 2:
            fams.add(parts[1])
        else:
            raise SystemExit(
                f"{path}:{n}: expected 'dir <path> <ceiling>' or 'family <path/x>', got {raw!r}"
            )
    return dirs, fams


def _cap_for(is_test: bool) -> int:
    return TEST_CAP if is_test else SOURCE_HARD_CAP


def check(root: Path) -> Findings:
    errors: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    source, tests = measure_sizes(root)
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
                    "move the area you touched into its own module"
                )
            elif n > ceiling:
                errors.append(
                    f"{p}: {n} lines, over its ceiling of {ceiling}; ceilings only fall, "
                    "so move the area you touched into its own module"
                )
            elif n < ceiling:
                notes.append(
                    f"{p}: {n} lines, below its ceiling of {ceiling}: lower it (update-exceptions)"
                )
        elif not is_test and n > SOURCE_SOFT_CAP:
            warnings.append(f"{p}: {n} lines, over the soft cap of {SOURCE_SOFT_CAP}")
    for p, ceiling in sorted(ceilings.items()):
        if p not in sizes:
            notes.append(
                f"{p}: listed with ceiling {ceiling} but not a tracked source or test file: remove it"
            )
        elif sizes[p][0] <= _cap_for(sizes[p][1]):
            notes.append(f"{p}: now within its cap: remove its exception")

    counts, families = measure_shape(root)
    dir_ceilings, fam_allowed = _read_shape(root / SHAPE_EXCEPTIONS)
    for d, n in sorted(counts.items()):
        ceiling = dir_ceilings.get(d)
        if n > DIR_MODULE_CAP:
            if ceiling is None:
                errors.append(
                    f"{d}/: {n} modules, over the cap of {DIR_MODULE_CAP}; add the module to a subpackage"
                )
            elif n > ceiling:
                errors.append(
                    f"{d}/: {n} modules, over its ceiling of {ceiling}; add the module to a subpackage"
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
                f"{f}.py sits beside {f}_*.py; make {f.rsplit('/', 1)[1]} a package"
            )
    for f in sorted(fam_allowed - set(families)):
        notes.append(f"family {f}: no longer a family: remove its exception")
    return Findings(errors, warnings, notes)


def update_exceptions(root: Path, allow_raise: bool) -> list[str]:
    """Rewrite both exceptions files to today's state; return refusals (nothing written if any)."""
    source, tests = measure_sizes(root)
    old = _read_pairs(root / SIZE_EXCEPTIONS)
    new: dict[str, int] = {}
    for p, n in source.items():
        if n > SOURCE_HARD_CAP:
            new[p] = n
    for p, n in tests.items():
        if n > TEST_CAP:
            new[p] = n
    counts, families = measure_shape(root)
    old_dirs, old_fams = _read_shape(root / SHAPE_EXCEPTIONS)
    new_dirs = {d: n for d, n in counts.items() if n > DIR_MODULE_CAP}
    new_fams = set(families)

    refusals: list[str] = []
    if not allow_raise:
        for p, n in sorted(new.items()):
            if p not in old:
                refusals.append(f"{p}: new exception at {n} lines")
            elif n > old[p]:
                refusals.append(f"{p}: raise {old[p]} -> {n}")
        for d, n in sorted(new_dirs.items()):
            if d not in old_dirs:
                refusals.append(f"{d}/: new directory exception at {n} modules")
            elif n > old_dirs[d]:
                refusals.append(f"{d}/: raise {old_dirs[d]} -> {n}")
        for f in sorted(new_fams - old_fams):
            refusals.append(f"family {f}: new family exception")
    if refusals:
        return refusals

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
