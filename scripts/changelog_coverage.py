#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The missing-fragment detector: a commit that changes shipped code and has no changelog fragment naming its bead
(RDR-258 Phase 4, nexus-2jomg.5).

    changelog_coverage.py [--repo DIR] [--base REF] [--head REF] [--mode block|report]

``scripts/changelog_fold.py check`` sees a fragment's FORMAT; nothing saw a MISSING fragment. This lists each commit in
``<base>..<head>`` that changes shipped code and whose bead has no ``changelog/unreleased/<bead>[-<slug>].md``.

**Range.** ``--base`` defaults to ``git merge-base origin/main <head>``, the release target, so the default is "everything this
release carries"; ``--head`` defaults to ``HEAD``. An empty range (nothing to examine) is an error in both modes.

**Shipped code** is what the changelog (``CHANGELOG.md``, folded from the fragments) reports on and a user's install runs: the
Python package ``src/nexus/``, the engine ``service/src/main/`` (its migrations under ``resources/db/changelog/`` included), the
Desktop bundle's ``mcpb/src/`` and the two install programs ``web/install.sh`` and ``web/install.ps1``. Not shipped for this
purpose: ``tests/``, ``docs/``, ``scripts/``, ``.github/``, ``changelog/`` itself, and the plugin trees (``conexus/``, ``sn/``,
``tool-spill/``, ``prose-edit/``), whose entries come from ``conexus/PENDING_RELEASE.md`` at release and are checked by the
plugin ledger, not by a root fragment (``changelog_fold.py``'s docstring). A markdown file under a shipped tree is
documentation, not code.

**Names its bead** is what the repo already does: the commit subject ends ``(<bead-id>)`` (``nexus-3ceda.2``,
``nexus-clfse.2 step 1``), and a fragment is named ``<bead-id>.md`` or ``<bead-id>-<slug>.md``. A commit whose subject names no
bead falls back to the first bead id in its body; one with none at all is a finding ("names no bead"). A fragment counts when
it is in ``<head>``'s tree or was ADDED anywhere in the range: a release branch folds and deletes the fragments, so the range's
own adds are what keep this true on the release PR. Fragment names are matched the way ``changelog_fold.py`` names them
(``nexus-abc`` matches ``nexus-abc.md`` and ``nexus-abc-cli.md``, never ``nexus-abc.1.md``).

**Exempt, and why.** A merge commit (it changes nothing of its own; the commits it brings are examined). A revert (``Revert "..."``:
the change it undoes carries the entry). A release-preparation commit (subject starting ``chore(release)`` or ``release``: it moves
versions and folds the fragments this check looks for).

``NX_LEDGER_MODE`` (scripts/ledger_mode.py): ``block`` (default) exits 10 on a finding; ``report`` lists each as
``LEDGER-FINDING changelog-coverage: <sha> <subject>: <why>``, prints the summary line and exits 0, and still exits 10 when it
examined no commit. Nothing runs this yet: wiring it is RDR-258 Phase 4 step 2 (Sam).

Standard library only.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import ledger_mode

LEDGER = "changelog-coverage"
SHIPPED_PREFIXES: tuple[str, ...] = ("src/nexus/", "service/src/main/", "mcpb/src/")
SHIPPED_FILES: tuple[str, ...] = ("web/install.sh", "web/install.ps1")
FRAGMENT_DIR = "changelog/unreleased"
RELEASE_TARGET = "origin/main"
BEAD = re.compile(r"\b(nexus-[a-z0-9]+(?:\.[0-9]+)*)\b")
EXEMPT_SUBJECT = re.compile(r'^(Revert "|chore\(release\)|release\b)')
EXIT_OK = 0
EXIT_BAD = 10


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str
    paths: tuple[str, ...]


def shipped(path: str) -> bool:
    """A path the changelog reports on: shipped code, not a markdown file inside a shipped tree."""
    if path.endswith(".md"):
        return False
    return path in SHIPPED_FILES or path.startswith(SHIPPED_PREFIXES)


def beads(commit: Commit) -> list[str]:
    """The bead ids the commit names: its subject's, else its body's, in order, without repeats."""
    found = BEAD.findall(commit.subject) or BEAD.findall(commit.body)
    return list(dict.fromkeys(found))


def has_fragment(bead: str, fragment_stems: set[str]) -> bool:
    return any(stem == bead or stem.startswith(bead + "-") for stem in fragment_stems)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout


def default_base(repo: Path, head: str) -> str:
    return _git(repo, "merge-base", RELEASE_TARGET, head).strip()


def commits_in(repo: Path, base: str, head: str) -> list[Commit]:
    """Every non-merge commit of ``base..head``, oldest first, with the paths it changes."""
    out = _git(repo, "-c", "core.quotepath=false", "log", "--no-merges", "--reverse", "--no-renames", "--name-only",
               "--format=%x1e%H%x1f%s%x1f%b%x1f", f"{base}..{head}")
    commits: list[Commit] = []
    for record in out.split("\x1e")[1:]:
        sha, subject, body, tail = record.split("\x1f", 3)
        commits.append(Commit(sha, subject, body, tuple(p for p in tail.splitlines() if p.strip())))
    return commits


def fragment_stems(repo: Path, base: str, head: str) -> set[str]:
    """Fragment names (without ``.md``) in ``head``'s tree, plus every one ADDED in ``base..head``."""
    stems: set[str] = set()
    tree = _git(repo, "ls-tree", "--name-only", head, f"{FRAGMENT_DIR}/")
    added = _git(repo, "log", "--diff-filter=A", "--name-only", "--format=", f"{base}..{head}", "--", f"{FRAGMENT_DIR}/")
    for line in [*tree.splitlines(), *added.splitlines()]:
        name = Path(line.strip()).name
        if name.endswith(".md") and name != "README.md":
            stems.add(name[:-3])
    return stems


def examine(repo: Path, base: str, head: str) -> tuple[list[str], int]:
    """(findings, commits examined). A commit is examined unless it is exempt; the range's merges are not commits here."""
    stems = fragment_stems(repo, base, head)
    findings: list[str] = []
    examined = 0
    for c in commits_in(repo, base, head):
        if EXEMPT_SUBJECT.match(c.subject):
            continue
        examined += 1
        touched = [p for p in c.paths if shipped(p)]
        if not touched:
            continue
        named = beads(c)
        label = f"{c.sha[:10]} {c.subject}"
        if not named:
            findings.append(f"{label}: changes shipped code ({touched[0]}) and names no bead, so no fragment can name it")
        elif not any(has_fragment(b, stems) for b in named):
            want = " or ".join(f"{FRAGMENT_DIR}/{b}.md" for b in named)
            findings.append(f"{label}: changes shipped code ({touched[0]}) and has no fragment: add {want}")
    return findings, examined


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    ap.add_argument("--base", default=None, help=f"default: git merge-base {RELEASE_TARGET} <head>")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--mode", choices=ledger_mode.MODES, default=None, help=f"overrides ${ledger_mode.ENV}")
    args = ap.parse_args(argv)
    try:
        mode = args.mode or ledger_mode.mode()
        base = args.base or default_base(args.repo, args.head)
        findings, examined = examine(args.repo, base, args.head)
    except (ledger_mode.LedgerModeError, RuntimeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_BAD
    env = {**os.environ, ledger_mode.ENV: mode}
    if mode == ledger_mode.REPORT:
        return EXIT_OK if ledger_mode.settle(LEDGER, findings, examined, environ=env) else EXIT_BAD
    if examined == 0:
        sys.stderr.write(f"error: {base}..{args.head} holds no commit to examine: the check would prove nothing\n")
        return EXIT_BAD
    for finding in findings:
        sys.stderr.write(f"error: {finding}\n")
    if findings:
        return EXIT_BAD
    sys.stdout.write(f"changelog_coverage: ok ({examined} commit(s) examined)\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
