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
``nexus-clfse.2 step 1``), or ``(GH #1689)`` for a GitHub issue, whose fragment is ``gh-1689.md``. A commit whose subject names no bead falls back to the first bead id in its body; one with none at all
is a finding ("names no bead"). **Covered** means the range holds an entry for the bead, in one of three ways:

* a fragment named ``<bead-id>.md`` or ``<bead-id>-<slug>.md`` (``nexus-abc`` matches ``nexus-abc.md`` and ``nexus-abc-cli.md``,
  never ``nexus-abc.1.md``), in ``<head>``'s tree or added in the range;
* an entry that CITES the bead in parentheses, the repo's ``(nexus-...)`` convention: a fragment in ``<head>``'s tree, or a line
  the range ADDS to a fragment or to ``CHANGELOG.md`` (what a release fold writes). A bead cited only in an older release's
  ``CHANGELOG.md`` section is NOT covered: a later commit for it is a new change and wants its own entry;
* an entry ADDED IN THE RANGE that cites a SIBLING under the same epic (``nexus-sby49.8`` leans on a range entry citing
  ``nexus-sby49.3``). One entry for an epic's children is how the repo writes them: 8.5.9 shipped the plugin settings work
  (``nexus-sby49.3``, ``.4``, ``.20`` cited) with four commits on ``.8`` and ``.11``, described by those entries and cited by none.
  The cost is that an epic's entry covers every child commit in the range, so a child that deserved its own line is not listed.

**Exempt, and why.** A merge commit (it changes nothing of its own; the commits it brings are examined). A revert (``Revert "..."``:
the change it undoes carries the entry). A release-preparation commit (subject ``chore(release)...``, ``release: ...`` or
``release(scope): ...``; ``release.yml: ...`` and ``release-foo: ...`` are not: it moves versions and folds the fragments this
check looks for). A commit that changes no behaviour, narrowly: either (a) every shipped file it touches is a ``.py`` or ``.java``
file whose only difference is comments or, for Python, docstrings (compared as the parsed tree with docstrings removed, and for
Java as the text with comments removed), so a string literal, an annotation or a signature change is NOT exempt; the docstrings of
``src/nexus/mcp/`` and ``src/nexus/commands/`` are tool descriptions and CLI help a user reads, so there only comments are
exempt; or (b) its message carries a ``Changelog: none`` trailer line, the author's explicit statement that nothing a user sees
changed (give the reason after it). A user reads the changelog for behaviour; a comment or docstring edit has none to report, and
(b) is the one escape for what (a) cannot prove, visible in the commit message for review.

A range whose commits are all release preparation has nothing to check: in report mode that is a warning and a pass. A range with
no commit at all, or one that examined nothing in block mode, is an error (the check would prove nothing).

``NX_LEDGER_MODE`` (scripts/ledger_mode.py): ``block`` (default) exits 10 on a finding; ``report`` lists each as
``LEDGER-FINDING changelog-coverage: <sha> <subject>: <why>``, prints the summary line and exits 0, and still exits 10 when it
examined no commit (bar the release-prep-only range above). Nothing runs this yet: wiring it is RDR-258 Phase 4 step 2 (Sam).

Standard library only.
"""
from __future__ import annotations

import ast
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
#: A bead id, or a GitHub issue the way the repo cites one ("GH #1689", "Closes #1689"), which names the fragment ``gh-1689.md``.
BEAD = re.compile(r"\b(nexus-[a-z0-9]+(?:\.[0-9]+)*)\b|\b(?:GH|Closes|Fixes) #([0-9]+)\b")


def item_ids(text: str) -> list[str]:
    """The ids ``text`` names, in order: ``nexus-abc.1`` for a bead and ``gh-1689`` for ``GH #1689``."""
    return [bead or f"gh-{issue}" for bead, issue in BEAD.findall(text)]
EXEMPT_SUBJECT = re.compile(r'^(Revert "|chore\(release\)|release(\([^)]*\))?:)')
#: A trailer line in the commit message: the author's statement that no behaviour changed.
NO_CHANGELOG = re.compile(r"^Changelog:\s*none\b", re.IGNORECASE | re.MULTILINE)
#: A bead cited in a parenthetical, the repo's "(nexus-xyz)" convention: "(nexus-a)", "(CodeQL, nexus-b)", "(nexus-c; nexus-d)".
PAREN = re.compile(r"\(([^()]*)\)")
_JAVA_TOKENS = re.compile(r'"""[\s\S]*?"""|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'|//[^\n]*|/\*[\s\S]*?\*/')
#: Python trees whose docstrings are text a user reads (MCP tool descriptions, CLI help): a docstring edit there is behaviour.
DOCSTRING_IS_TEXT: tuple[str, ...] = ("src/nexus/mcp/", "src/nexus/commands/")
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
    """The ids the commit names (beads, and GitHub issues as ``gh-N``): its subject's, else its body's, in order, without repeats."""
    found = item_ids(commit.subject) or item_ids(commit.body)
    return list(dict.fromkeys(found))


def has_fragment(bead: str, fragment_stems: set[str]) -> bool:
    return any(stem == bead or stem.startswith(bead + "-") for stem in fragment_stems)


def family(bead: str) -> str:
    """The bead's epic: ``nexus-sby49.8`` and ``nexus-sby49.3`` are one family, ``nexus-sby49``."""
    return bead.rsplit(".", 1)[0]


def covered(bead: str, stems: set[str], cites: set[str], range_cites: set[str]) -> bool:
    """A fragment is named for the bead, or an entry cites it, or an entry ADDED IN THE RANGE cites a sibling under the same epic
    (one entry for an epic's children; the module docstring says what that covers)."""
    return has_fragment(bead, stems) or bead in cites or family(bead) in {family(c) for c in range_cites}


def cited(text: str) -> set[str]:
    """Bead ids that ``text`` cites in parentheses."""
    return {b for group in PAREN.findall(text) for b in item_ids(group)}


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _java_code(text: str) -> str:
    """The Java text with comments removed (string and char literals kept) and whitespace collapsed."""
    kept = _JAVA_TOKENS.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else " ", text)
    return " ".join(kept.split())


def code_equal(path: str, old: str, new: str) -> bool:
    """True when ``old`` and ``new`` differ only in comments (and, for Python outside ``DOCSTRING_IS_TEXT``, docstrings). False
    for any other file type."""
    if path.endswith(".py"):
        strip = _strip_docstrings if not path.startswith(DOCSTRING_IS_TEXT) else (lambda tree: tree)
        try:
            return ast.dump(strip(ast.parse(old))) == ast.dump(strip(ast.parse(new)))
        except SyntaxError:
            return False
    if path.endswith(".java"):
        return _java_code(old) == _java_code(new)
    return False


def behaviour_free(repo: Path, c: Commit, touched: list[str]) -> bool:
    """Every shipped file the commit touches exists before and after and differs only in comments or docstrings."""
    if NO_CHANGELOG.search(c.body):
        return True
    for path in touched:
        old = subprocess.run(["git", "-C", str(repo), "show", f"{c.sha}^:{path}"], capture_output=True, text=True, check=False)
        new = subprocess.run(["git", "-C", str(repo), "show", f"{c.sha}:{path}"], capture_output=True, text=True, check=False)
        if old.returncode != 0 or new.returncode != 0 or not code_equal(path, old.stdout, new.stdout):
            return False
    return True


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


def cited_beads(repo: Path, base: str, head: str) -> tuple[set[str], set[str]]:
    """(the beads an entry cites, the beads an entry ADDED IN THE RANGE cites).

    The first set is what a bead itself may lean on: fragments in ``head``'s tree plus every line the range adds to a fragment or to
    ``CHANGELOG.md`` (what the release fold writes). The second, the lines the range adds, is all a sibling bead may lean on. A
    ``CHANGELOG.md`` section from before the range cites nothing here."""
    head_fragments = []
    for line in _git(repo, "ls-tree", "-r", "--name-only", head, f"{FRAGMENT_DIR}/").splitlines():
        if line.endswith(".md") and Path(line).name != "README.md":
            head_fragments.append(_git(repo, "show", f"{head}:{line}"))
    patch = _git(repo, "log", "--no-merges", "--format=", "-p", "-U0", "--no-color", f"{base}..{head}", "--", f"{FRAGMENT_DIR}/",
                 "CHANGELOG.md")
    added = cited("\n".join(ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")))
    return added | cited("\n".join(head_fragments)), added


def examine(repo: Path, base: str, head: str) -> tuple[list[str], int, int]:
    """(findings, commits examined, release-preparation commits skipped). A release-prep or revert commit is not examined; the
    range's merges are not commits here."""
    stems = fragment_stems(repo, base, head)
    cites, range_cites = cited_beads(repo, base, head)
    findings: list[str] = []
    examined = prep = 0
    for c in commits_in(repo, base, head):
        if EXEMPT_SUBJECT.match(c.subject):
            prep += 1
            continue
        examined += 1
        touched = [p for p in c.paths if shipped(p)]
        if not touched:
            continue
        if behaviour_free(repo, c, touched):
            continue
        named = beads(c)
        label = f"{c.sha[:10]} {c.subject}"
        if not named:
            findings.append(f"{label}: changes shipped code ({touched[0]}) and names no bead, so no fragment can name it")
        elif not any(covered(b, stems, cites, range_cites) for b in named):
            want = " or ".join(f"{FRAGMENT_DIR}/{b}.md" for b in named)
            findings.append(f"{label}: changes shipped code ({touched[0]}) and has no fragment: add {want}, "
                            "or cite the bead in another entry, or add a 'Changelog: none' trailer if no behaviour changed")
    return findings, examined, prep


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
        findings, examined, prep = examine(args.repo, base, args.head)
    except (ledger_mode.LedgerModeError, RuntimeError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_BAD
    env = {**os.environ, ledger_mode.ENV: mode}
    if mode == ledger_mode.REPORT and examined == 0 and prep > 0:
        sys.stdout.write(f"{ledger_mode.SUMMARY_PREFIX} {LEDGER}: 0 finding(s), 0 item(s) examined (mode=report): the range holds "
                         f"only {prep} release-preparation or revert commit(s), so there is nothing to check\n")
        if env.get("GITHUB_ACTIONS") == "true":
            sys.stdout.write(f"::warning title={LEDGER}::the range holds only release-preparation or revert commits, so nothing was checked\n")
        return EXIT_OK
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
