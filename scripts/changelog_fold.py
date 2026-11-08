#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Changelog fragments, folded into ``## [Unreleased]`` at release time (nexus-3ceda.2).

    changelog_fold.py check    # every fragment is well formed and no [Unreleased] holds a hand entry
    changelog_fold.py fold     # move every fragment into its changelog's [Unreleased], then delete it

A change adds a fragment instead of editing a changelog's top lines, so concurrent branches stop
colliding on them:

    changelog/unreleased/<bead-id>[-<slug>].md  -> CHANGELOG.md

The plugin changelog (``conexus/CHANGELOG.md``) has no ``[Unreleased]`` section: its entries come
from ``conexus/PENDING_RELEASE.md`` and are written by the release itself, so it takes no fragments.

A fragment is one ``### <Section>`` line (Added, Changed, Deprecated, Removed, Fixed, Security,
Performance or Docs) followed by one or more ``- `` bullets; a bullet may wrap onto lines indented
by two spaces. ``fold`` groups the bullets by section in that order and, inside a section, by the
commit that added each fragment (its latest add, oldest first; a fragment git has no history for
sorts last, by name). The directory keeps a ``README.md``, which is not a fragment; dotfiles (``.DS_Store``,
editor swap files) are ignored and a subdirectory is refused.
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
TARGETS: tuple[tuple[str, str], ...] = (("changelog/unreleased", "CHANGELOG.md"),)
#: A hand-written entry under [Unreleased]: a ``-``, ``*`` or ``+`` list item at any indent.
HAND_BULLET = re.compile(r"^\s*[-*+] ")
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
    return sorted(
        p
        for p in d.iterdir()
        if p.is_file() and p.name != DIR_README and not p.name.startswith(".")
    )


def unreleased_body(text: str) -> tuple[int, int]:
    """Return the [start, end) span of the body under ``## [Unreleased]``, which must be the first
    ``## [`` heading: an old empty [Unreleased] further down the history is never the target."""
    first = re.search(r"^## \[.*$", text, re.MULTILINE)
    if not first or first.group(0) != UNRELEASED:
        raise ValueError(f"the first '## [' heading is not '{UNRELEASED}'")
    body_start = first.end() + 1
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
        if d.is_dir():
            errors.extend(
                f"{frag_dir}/{p.name}/: a subdirectory is never folded; put fragments directly in {frag_dir}/"
                for p in sorted(d.iterdir())
                if p.is_dir()
            )
        for p in fragment_paths(root, frag_dir):
            errors.extend(parse_fragment(p)[1])
        cl = root / changelog
        if not cl.is_file():
            errors.append(f"{changelog}: missing")
            continue
        text = cl.read_text(encoding="utf-8")
        try:
            s, e = unreleased_body(text)
        except ValueError as exc:
            errors.append(f"{changelog}: {exc}")
            continue
        # Bullets are entries; prose such as an engine pairing line ("Pairs with ...") may stay.
        hand = [ln for ln in text[s:e].splitlines() if HAND_BULLET.match(ln)]
        if hand:
            errors.append(
                f"{changelog}: [Unreleased] holds {len(hand)} hand-written line(s); "
                f"put the entry in {frag_dir}/<bead-id>.md instead, as '### Added' (or Changed, "
                f"Fixed, ...), a blank line, then '- **...**' (first: {hand[0][:60]!r})"
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
    # git log lists newest first: the latest add is this fragment's own; an older add of the same
    # name belongs to an earlier round that was folded and deleted.
    return int(out[0]) if out else None


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
    # Only what fold needs: well-formed fragments and a top [Unreleased]. Not check's hand-bullet
    # rule (a second fold after a late fragment would read the first fold's bullets as hand
    # entries) and not the README (a release branch off an older tag may not carry it).
    errors: list[str] = []
    for frag_dir, changelog in TARGETS:
        for p in fragment_paths(root, frag_dir):
            errors.extend(parse_fragment(p)[1])
        cl = root / changelog
        if not cl.is_file():
            errors.append(f"{changelog}: missing")
            continue
        try:
            unreleased_body(cl.read_text(encoding="utf-8"))
        except ValueError as exc:
            errors.append(f"{changelog}: {exc}")
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
