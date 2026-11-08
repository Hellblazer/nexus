#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Changelog fragments, folded into ``## [Unreleased]`` at release time (nexus-3ceda.2).

    changelog_fold.py check    # every fragment is well formed and no [Unreleased] holds a hand entry
    changelog_fold.py fold     # move every fragment into its changelog's [Unreleased], then delete it

A change adds a fragment instead of editing a changelog's top lines, so concurrent branches stop
colliding on them:

    changelog/unreleased/<bead-id>[-<slug>].md          -> CHANGELOG.md
    conexus/changelog/unreleased/<bead-id>[-<slug>].md  -> conexus/CHANGELOG.md

A fragment is one ``### <Section>`` line (Added, Changed, Deprecated, Removed, Fixed, Security,
Performance or Docs) followed by one or more ``- `` bullets; a bullet may wrap onto lines indented
by two spaces. ``fold`` groups the bullets by section in that order and, inside a section, by the
commit that first added each fragment (oldest first; a fragment git has no history for sorts last,
by name). Each fragment directory keeps a ``README.md``, which is not a fragment.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SECTIONS: tuple[str, ...] = (
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
    "Security",
    "Performance",
    "Docs",
)
TARGETS: tuple[tuple[str, str], ...] = (
    ("changelog/unreleased", "CHANGELOG.md"),
    ("conexus/changelog/unreleased", "conexus/CHANGELOG.md"),
)
FRAGMENT_NAME = re.compile(
    r"^[a-z][a-z0-9]*-[a-z0-9]+(?:\.[0-9]+)*(?:-[a-z0-9]+)*\.md$"
)
UNRELEASED = "## [Unreleased]"
DIR_README = "README.md"

EXIT_OK: int = 0
EXIT_BAD: int = 10


@dataclass(frozen=True)
class Fragment:
    path: Path
    section: str
    bullets: tuple[str, ...]


def parse_fragment(path: Path) -> tuple[Fragment | None, list[str]]:
    """Parse one fragment; return (fragment, errors). A fragment with errors is None."""
    errors: list[str] = []
    if not FRAGMENT_NAME.match(path.name):
        errors.append(
            f"{path}: name must be <bead-id>[-<slug>].md, lower case (e.g. nexus-3ceda.2.md)"
        )
    lines = [ln.rstrip() for ln in path.read_text(encoding="utf-8").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    if not lines:
        return None, [*errors, f"{path}: empty"]
    head = re.fullmatch(r"### (\S.*)", lines[0])
    if not head or head.group(1) not in SECTIONS:
        errors.append(
            f"{path}: first line must be '### <Section>', one of {', '.join(SECTIONS)}"
        )
    bullets: list[str] = []
    for n, ln in enumerate(lines[1:], 2):
        if not ln:
            continue
        if ln.startswith("- ") and len(ln) > 2:
            bullets.append(ln)
        elif ln.startswith("  ") and ln.strip() and bullets:
            bullets[-1] += "\n" + ln
        else:
            errors.append(
                f"{path}:{n}: expected a '- ' bullet or a two-space continuation, got {ln[:60]!r}"
            )
    if not bullets:
        errors.append(f"{path}: no '- ' bullet")
    if errors:
        return None, errors
    assert head is not None
    return Fragment(path, head.group(1), tuple(bullets)), []


def fragment_paths(root: Path, frag_dir: str) -> list[Path]:
    d = root / frag_dir
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.name != DIR_README)


def unreleased_body(text: str) -> tuple[int, int]:
    """Return the [start, end) character span of the body under ``## [Unreleased]``."""
    start = text.find(UNRELEASED + "\n")
    if start < 0:
        raise SystemExit(f"no '{UNRELEASED}' heading")
    body_start = start + len(UNRELEASED) + 1
    nxt = re.search(r"^## \[", text[body_start:], re.MULTILINE)
    return body_start, body_start + nxt.start() if nxt else len(text)


def check(root: Path) -> list[str]:
    errors: list[str] = []
    for frag_dir, changelog in TARGETS:
        d = root / frag_dir
        if not (d / DIR_README).is_file():
            errors.append(
                f"{frag_dir}/{DIR_README}: missing (it keeps the directory and says the format)"
            )
        for p in fragment_paths(root, frag_dir):
            errors.extend(parse_fragment(p)[1])
        text = (root / changelog).read_text(encoding="utf-8")
        s, e = unreleased_body(text)
        # Bullets are entries; prose such as an engine pairing line ("Pairs with ...") may stay.
        hand = [ln for ln in text[s:e].splitlines() if ln.startswith("- ")]
        if hand:
            errors.append(
                f"{changelog}: [Unreleased] holds {len(hand)} hand-written line(s); "
                f"put the entry in {frag_dir}/<bead-id>.md instead (first: {hand[0][:60]!r})"
            )
    return errors


def _first_added(root: Path, path: Path) -> int | None:
    out = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "log",
            "--diff-filter=A",
            "--format=%ct",
            "--",
            str(path.relative_to(root)),
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    return int(out[-1]) if out else None


def render(fragments: list[tuple[int | None, Fragment]]) -> str:
    """The ``### Section`` blocks for these fragments, each block ending in a blank line."""
    out = ""
    for section in SECTIONS:
        chosen = [f for f in fragments if f[1].section == section]
        if not chosen:
            continue
        chosen.sort(key=lambda f: (f[0] is None, f[0] or 0, f[1].path.name))
        out += (
            f"### {section}\n\n"
            + "".join(b + "\n" for _, f in chosen for b in f.bullets)
            + "\n"
        )
    return out


def fold(root: Path) -> list[str]:
    """Fold every target's fragments; return errors (nothing written if any)."""
    errors = check(root)
    if errors:
        return errors
    for frag_dir, changelog in TARGETS:
        paths = fragment_paths(root, frag_dir)
        if not paths:
            continue
        frags = [parse_fragment(p)[0] for p in paths]
        dated = [(_first_added(root, f.path), f) for f in frags if f is not None]
        cl = root / changelog
        text = cl.read_text(encoding="utf-8")
        s, e = unreleased_body(text)
        kept = text[s:e].strip("\n")
        kept = re.sub(r"^### .*\n?", "", kept, flags=re.MULTILINE).strip(
            "\n"
        )  # empty headings only remain
        body = "\n" + (kept + "\n\n" if kept else "") + render(dated)
        cl.write_text(text[:s] + body + text[e:].lstrip("\n"), encoding="utf-8")
        for p in paths:
            p.unlink()
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("cmd", choices=("check", "fold"))
    args = ap.parse_args(argv)
    errors = check(args.root) if args.cmd == "check" else fold(args.root)
    for e in errors:
        sys.stderr.write(f"error: {e}\n")
    if errors:
        return EXIT_BAD
    sys.stdout.write(f"changelog_fold {args.cmd}: ok\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
