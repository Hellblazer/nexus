#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Cut a plugin-only release: branch, import, move refs, verify (nexus-a2wmi.8).

The cut is a script with tests, never a checklist a human follows under
pressure. Invariants R and W and the anchoring rule live in
``scripts/plugin_channel.py``'s docstring; this script cites them and
produces exactly the state they admit:

- The sequence number n comes from git's tag list at cut time
  (``next_plugin_tag_number`` — the channel is COUNTER-LESS; no file
  records the number, so there is nothing to reset, nothing to own, and
  nothing to read from the wrong place). Belt and suspenders: after
  deriving, the cut refuses if the derived tag already exists locally or
  on origin.
- The branch name ``plugin-release/{version}-{n}`` is LOAD-BEARING, not
  cosmetic: condition (d) of invariant W grants the anchored release
  window only on a branch whose name matches the ref's own version and
  number.
- The import is a wholesale allowlist DIFF-AND-APPLY off origin/main
  (``git checkout`` of a pathspec stages no deletions), excluding the
  denied wheel-data prefixes, which are then asserted byte-identical to
  main.
- Ref movement keys on ``PLUGIN_BY_ALLOWLIST_PREFIX`` — the allowlist is
  what the cut ships; ``SURFACE_BY_PLUGIN`` is what the loader reads and
  is narrower. A cut carrying only ``conexus/evals/`` ships real content
  and must move the ref.
- No version field moves, and no counter file is created.
- The script pushes nothing and tags nothing: it prints the PR, merge,
  tag and back-merge commands, and the human cuts the release.

MAIN-READINESS: every piece of channel machinery is outside the
allowlist, so a cut cannot install it. Until a client release carries
Phases 1 and 2 to main, this script refuses, naming every missing piece.

The atomic-split check (bead .9) runs BEFORE the import and refusal
aborts before any branch or file is written. Deferral via the ledger is
the only path past a straddling entry; there is no flag.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from plugin_channel import (  # noqa: E402
    ALLOWED_EXACT,
    ALLOWED_PREFIXES,
    DENIED_PREFIXES,
    PLUGIN_BY_ALLOWLIST_PREFIX,
    format_plugin_tag,
    next_plugin_tag_number,
    path_has_prefix,
)

MARKETPLACE = ".claude-plugin/marketplace.json"
LEDGER = "conexus/PENDING_RELEASE.md"

#: What origin/main must carry before a cut is possible, path → content
#: markers proving the RULE is present, not merely the file. A cut PR
#: lands on main, where main's own gates judge it: pre-machinery main
#: demands the client ref shape and has no plugin-release workflow, so a
#: cut would be red there and a pushed tag would fire nothing.
REQUIRED_ON_MAIN: dict[str, tuple[str, ...]] = {
    # The per-plugin parity rule (.3).
    "tests/test_plugin_structure.py": ("_assert_ref_valid_for_plugin",),
    # Per-plugin drift scoping (.14) and the python window site (.4).
    "tests/test_plugin_release_drift_ledger.py": (
        "def _drifted_paths(plugin",
        "plugin_in_release_window",
    ),
    # The bash window site (.4).
    ".github/workflows/plugin-drift-ledger.yml": (
        'grep -qE "^plugin-v${version_re}-[1-9][0-9]*$"',
    ),
    # The channel primitives (.1).
    "scripts/plugin_channel.py": ("INVARIANT W",),
    # This script itself.
    "scripts/cut_plugin_release.py": ("def perform_cut",),
    # The verify-only tag workflow (.7).
    ".github/workflows/plugin-release.yml": ("plugin-v*",),
}

_VERSION_RE = re.compile(r"^v(\d+\.\d+\.\d+)\Z")


class CutRefused(RuntimeError):
    """The cut cannot be performed correctly; nothing was mutated."""


def _git(repo: Path, *args: str, check: bool = True, input_text: str | None = None
         ) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, input=input_text
    )
    if check and proc.returncode != 0:
        raise CutRefused(
            f"git {' '.join(args[:3])}... failed (rc {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc


def check_main_readiness(repo: Path) -> list[str]:
    """Every machinery miss on origin/main, described. Empty means ready.

    One scan collects ALL misses (never abort-on-first: one refusal
    yields the whole fix list — the release-preflight lesson).
    """
    misses: list[str] = []
    for path, markers in REQUIRED_ON_MAIN.items():
        shown = _git(repo, "show", f"origin/main:{path}", check=False)
        if shown.returncode != 0:
            misses.append(f"{path}: absent from origin/main")
            continue
        for marker in markers:
            if marker not in shown.stdout:
                misses.append(
                    f"{path}: present on origin/main but lacks the rule "
                    f"marker {marker!r}"
                )
    return misses


def _allowlist_pathspec() -> list[str]:
    spec = [prefix.rstrip("/") for prefix in ALLOWED_PREFIXES]
    spec += list(ALLOWED_EXACT)
    spec += [f":(exclude){denied.rstrip('/')}" for denied in DENIED_PREFIXES]
    return spec


def _is_allowlisted(path: str) -> bool:
    if any(path_has_prefix(path, denied) for denied in DENIED_PREFIXES):
        return False
    if path in ALLOWED_EXACT:
        return True
    return any(path_has_prefix(path, allowed) for allowed in ALLOWED_PREFIXES)


_BEAD_ID_RE = re.compile(r"\bnexus-[a-z0-9]+(?:\.[a-z0-9]+)*\b")
_BEAD_MARKER_RE = re.compile(
    r"^\s*(?:\*\*)?bead:\s*(nexus-[a-z0-9]+(?:\.[a-z0-9]+)*)",
    re.IGNORECASE | re.MULTILINE,
)

#: Paths that land in a SHIPPED artifact — the wheel (src/, the denied
#: wheel-data prefixes, mcpb/, dt/scripts as force-include package data).
#: The straddle predicate is scoped to these: an entry whose bead also
#: touched tests/ or docs/ strands nothing, because those ship nowhere.
#: RECORDED DEVIATION (judged at R2, bead .10): the plan's literal "any
#: path outside the allowlist" would refuse every real cut — real beads'
#: commits always carry test ride-alongs (today's live nexus-2v0v7 entry
#: does) — while the stated rationale is coupling to content the import
#: must hold back. The refusal-and-defer semantics are unchanged.
_SHIPPED_SURFACE_PREFIXES: tuple[str, ...] = (
    "src/",
    "conexus/plans/",
    "conexus/daemon/",
    "mcpb/",
    "dt/",
)


def path_entries(ledger_text: str) -> list[str]:
    """The ledger's ENTRIES: bullet groups carrying a path-shaped span.

    Bullets with no backtick path (the file's header contract prose) are
    not entries and are never attributed.
    """
    blocks: list[str] = []
    entry: list[str] = []

    def flush() -> None:
        if entry:
            blocks.append("".join(entry))
            entry.clear()

    for line in ledger_text.splitlines(keepends=True):
        if line.startswith("- "):
            flush()
            entry.append(line)
        elif entry and (line.startswith("  ") or line.startswith("\t")):
            entry.append(line)
        else:
            flush()
    flush()
    return [
        block
        for block in blocks
        if any("/" in span for span in re.findall(r"`([^`]+)`", block))
    ]


def attribute_entry(entry: str) -> str:
    """One bead per entry, by the rule that needs no interpreting.

    1. A structured ``bead: nexus-xxxxx`` marker on any line wins and
       nothing else is read (mirrors the requires-commit convention
       scripts/check_remediation_commits_ride_release.py reads).
    2. Otherwise the FIRST bead id token on the entry's FIRST line only.
    3. Bead ids anywhere else are prose, never attribution.
    4. No marker and no first-line id: refused by name.
    """
    marker = _BEAD_MARKER_RE.search(entry)
    if marker:
        return marker.group(1)
    first_line = entry.splitlines()[0] if entry.splitlines() else ""
    token = _BEAD_ID_RE.search(first_line)
    if token:
        return token.group(0)
    raise CutRefused(
        f"ledger entry has no bead attribution — add a 'bead: nexus-xxxxx' "
        f"marker line or a bead id on its first line: {first_line.strip()!r}"
    )


def atomic_split_check(repo: Path, base_tag: str, allowlisted: list[str]) -> None:
    """Refuse when a ledger entry's bead straddles into shipped content.

    The import is a wholesale allowlist diff-and-apply with no per-entry
    granularity: it cannot ship an entry's allowlisted paths while
    holding back the same entry's wheel-surface paths, so a straddling
    entry makes the cut impossible to perform correctly — not risky,
    impossible. The only path forward is editing PENDING_RELEASE.md to
    DEFER that entry, a reviewable change a human makes deliberately.
    There is no override flag and no warn mode. Runs BEFORE the branch
    exists; a raise leaves the repository untouched (.8's seam contract).
    """
    shown = _git(repo, "show", f"origin/develop:{LEDGER}", check=False)
    if shown.returncode != 0:
        return  # no ledger on develop; the drift contract owns that state
    problems: list[str] = []
    for entry in path_entries(shown.stdout):
        bead = attribute_entry(entry)
        shas = _git(
            repo, "log", f"{base_tag}..origin/develop",
            f"--grep={bead}", "--format=%H",
        ).stdout.split()
        offending: set[str] = set()
        for sha in shas:
            for path in _git(
                repo, "diff-tree", "--no-commit-id", "--name-only", "-r", sha
            ).stdout.splitlines():
                path = path.strip()
                if (
                    path
                    and not _is_allowlisted(path)
                    and any(
                        path_has_prefix(path, prefix)
                        for prefix in _SHIPPED_SURFACE_PREFIXES
                    )
                ):
                    offending.add(path)
        if offending:
            problems.append(f"{bead}: {sorted(offending)}")
    if problems:
        raise CutRefused(
            "straddling ledger entries — the wholesale import cannot ship "
            "an entry's plugin half while holding back its wheel half. "
            "DEFER these entries in conexus/PENDING_RELEASE.md; there is "
            "no flag:\n  " + "\n  ".join(problems)
        )


def assert_branch_agreement(repo: Path, version: str, n: int) -> None:
    """The branch name, the written ref and n must agree (condition d)."""
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    expected_branch = f"plugin-release/{version}-{n}"
    tag = format_plugin_tag(version, n)
    problems: list[str] = []
    if branch != expected_branch:
        problems.append(f"branch is {branch!r}, expected {expected_branch!r}")
    data = json.loads((repo / MARKETPLACE).read_text(encoding="utf-8"))
    for plugin in data.get("plugins", []):
        ref = plugin.get("source", {}).get("ref", "")
        if ref.startswith("plugin-v") and ref != tag:
            problems.append(
                f"plugin {plugin.get('name')} ref {ref!r} does not name {tag!r}"
            )
    if problems:
        raise CutRefused(
            "branch name, written ref and n do not agree: " + "; ".join(problems)
        )


def _rewrite_ledger(repo: Path) -> None:
    """Empty the entries this cut covers; keep the rest verbatim.

    Covered: every path-shaped backtick span in the entry lies inside
    the channel allowlist — the wholesale import ships it. An entry
    naming any path outside the allowlist (a split-delivery wheel half)
    survives untouched, as does an entry naming no path at all.
    """
    ledger = repo / LEDGER
    lines = ledger.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    entry: list[str] = []

    def flush() -> None:
        if not entry:
            return
        text = "".join(entry)
        spans = [s for s in re.findall(r"`([^`]+)`", text) if "/" in s]
        covered = bool(spans) and all(_is_allowlisted(s) for s in spans)
        if not covered:
            kept.extend(entry)
        entry.clear()

    for line in lines:
        if line.startswith("- "):
            flush()
            entry.append(line)
        elif entry and (line.startswith("  ") or line.startswith("\t")):
            entry.append(line)
        else:
            flush()
            kept.append(line)
    flush()
    ledger.write_text("".join(kept), encoding="utf-8")


def _run_real_battery(repo: Path) -> None:
    """The minimal battery, against the branch's own mixed state."""
    commands = [
        ["uv", "run", "pytest", "-m", "lint", "-q"],
        ["uv", "run", "pytest", "tests/test_plugin_release_drift_ledger.py", "-q"],
        ["uv", "run", "pytest", "tests/hooks/", "-q"],
        ["./tests/e2e/release-sandbox.sh", "smoke"],
    ]
    for command in commands:
        proc = subprocess.run(command, cwd=repo, text=True)
        if proc.returncode != 0:
            raise CutRefused(
                f"battery failed: {' '.join(command)} (rc {proc.returncode})"
            )


def perform_cut(
    repo: Path,
    base_tag: str,
    *,
    split_check: Callable[[Path, str, list[str]], None] = atomic_split_check,
    battery: Callable[[Path], None] = _run_real_battery,
) -> dict:
    """Produce the cut branch. Refusal raises :class:`CutRefused`.

    Takes NO sequence-number argument: a hand-passed number is how a cut
    re-mints an existing tag.
    """
    match = _VERSION_RE.match(base_tag)
    if match is None:
        raise CutRefused(f"base tag {base_tag!r} is not a client tag vX.Y.Z")
    version = match.group(1)

    misses = check_main_readiness(repo)
    if misses:
        raise CutRefused(
            "origin/main does not carry the channel machinery; a cut PR "
            "would be red there and a pushed tag would fire nothing:\n  "
            + "\n  ".join(misses)
        )

    _git(repo, "fetch", "--tags", "--force", "origin")
    n = next_plugin_tag_number(version, cwd=repo)
    tag = format_plugin_tag(version, n)

    if _git(repo, "rev-parse", "--verify", "--quiet", f"{tag}^{{commit}}",
            check=False).returncode == 0:
        raise CutRefused(f"derived tag {tag} already exists locally")
    remote = _git(repo, "ls-remote", "--tags", "origin", f"refs/tags/{tag}")
    if remote.stdout.strip():
        raise CutRefused(f"derived tag {tag} already exists on origin")

    pathspec = _allowlist_pathspec()
    changed = [
        line.strip()
        for line in _git(
            repo, "diff", "--name-only", "origin/main..origin/develop",
            "--", *pathspec,
        ).stdout.splitlines()
        if line.strip()
    ]
    if not changed:
        raise CutRefused(
            "no allowlisted path differs between origin/main and "
            "origin/develop: there is nothing to ship"
        )

    # BEFORE the branch exists: a refusal here leaves nothing behind.
    split_check(repo, base_tag, changed)

    branch = f"plugin-release/{version}-{n}"
    _git(repo, "switch", "-q", "-c", branch, "origin/main")

    diff = _git(
        repo, "diff", "--binary", "origin/main..origin/develop", "--", *pathspec
    ).stdout
    if diff:
        _git(repo, "apply", "--index", "-", input_text=diff)

    # Wheel package data never rides a cut: restore and PROVE.
    _git(repo, "checkout", "-q", "origin/main", "--",
         *[d.rstrip("/") for d in DENIED_PREFIXES])
    denied_diff = _git(repo, "diff", "--quiet", "origin/main", "--",
                       *[d.rstrip("/") for d in DENIED_PREFIXES], check=False)
    if denied_diff.returncode != 0:
        raise CutRefused(
            "denied prefixes differ from origin/main after restore — wheel "
            "package data must never ride a plugin cut"
        )

    moved = sorted(
        {
            plugin
            for prefix, plugin in PLUGIN_BY_ALLOWLIST_PREFIX.items()
            if any(path_has_prefix(path, prefix) for path in changed)
        }
    )
    data = json.loads((repo / MARKETPLACE).read_text(encoding="utf-8"))
    main_marketplace = json.loads(
        _git(repo, "show", f"origin/main:{MARKETPLACE}").stdout
    )
    for plugin in data.get("plugins", []):
        if plugin.get("name") in moved:
            plugin["source"]["ref"] = tag
    (repo / MARKETPLACE).write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )

    if (repo / LEDGER).exists():
        _rewrite_ledger(repo)

    # No version field moves, ever, and nothing outside the allowlist.
    if data.get("metadata", {}).get("version") != main_marketplace.get(
        "metadata", {}
    ).get("version"):
        raise CutRefused("marketplace metadata.version moved; a cut moves no version field")
    main_versions = {p.get("name"): p.get("version") for p in main_marketplace.get("plugins", [])}
    for plugin in data.get("plugins", []):
        if plugin.get("version") != main_versions.get(plugin.get("name")):
            raise CutRefused(
                f"plugin {plugin.get('name')} version field moved; a cut "
                f"moves no version field"
            )
    stray = [
        line.strip()
        for line in _git(repo, "diff", "--name-only", "origin/main").stdout.splitlines()
        if line.strip() and not _is_allowlisted(line.strip())
    ]
    if stray:
        raise CutRefused(
            f"the cut touched paths outside the channel allowlist: {stray}"
        )

    assert_branch_agreement(repo, version, n)

    _git(repo, "add", "--", MARKETPLACE)
    if (repo / LEDGER).exists():
        _git(repo, "add", "--", LEDGER)
    _git(repo, "commit", "-q", "-m", f"chore(plugin): cut {tag}")

    battery(repo)

    print(f"cut branch {branch} ready; nothing pushed, nothing tagged.")
    print("the human cuts the release:")
    print(f"  git push -u origin {branch}")
    print(f"  gh pr create --base main --head {branch} --title 'plugin release: {tag}'")
    print("  gh pr merge <N> --merge")
    print(f"  git tag -a {tag} -m '{tag}' <merge-commit> && git push origin {tag}")
    print("  git checkout develop && git merge origin/main --no-edit && git push origin develop")
    return {"n": n, "tag": tag, "branch": branch, "moved_plugins": moved}


def _build_parser() -> argparse.ArgumentParser:
    """No override flag and no warn mode exist, by design (pinned by test)."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base_tag", help="the released client tag the cut anchors to, e.g. v7.15.0")
    parser.add_argument("--repo", default=".", help="repository checkout to cut from")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = perform_cut(Path(args.repo).resolve(), args.base_tag)
    except CutRefused as refusal:
        print(f"CUT REFUSED: {refusal}", file=sys.stderr)
        return 1
    print(f"cut: {result['tag']} on {result['branch']} (moved: {', '.join(result['moved_plugins'])})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
