# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The PENDING_RELEASE drift-ledger parsers and the RDR-228 Phase 2 guard.

Moved out of the anchored-tag cut script (nexus-rbmja.2), since retired
(nexus-rbmja.8), so the drift-ledger test does not depend on it.

Under the plugin-only fast lane the ledger is the cherry-pick list, and an
entry under "Deferred to the next client release" still means "do not
cherry-pick this one". The Phase 2 guard tests the tree against the
marketplace pin, not the cut, so it travels with the ledger test.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from plugin_channel import is_channel_path

LEDGER = "conexus/PENDING_RELEASE.md"

_is_allowlisted = is_channel_path


class LedgerRefused(RuntimeError):
    """A ledger or Phase 2 check refused; nothing was mutated."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise LedgerRefused(
            f"git {' '.join(args[:3])}... failed (rc {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc


def _is_continuation(line: str) -> bool:
    return line.startswith("  ") or line.startswith("\t")


def _ledger_blocks(ledger_text: str) -> list[tuple[bool, list[str]]]:
    """Group the ledger into (is_bullet_block, lines) runs.

    A blank line INSIDE a bullet stays with it when the next non-blank
    line is an indented continuation — a CommonMark paragraph break
    within one list item (R2 finding: treating the blank as an
    unconditional boundary split a two-paragraph entry, judged its first
    half covered, erased it, and orphaned the second half's wheel-half
    warning and `bead:` marker).
    """
    lines = ledger_text.splitlines(keepends=True)
    blocks: list[tuple[bool, list[str]]] = []
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        if not line.startswith("- "):
            blocks.append((False, [line]))
            i += 1
            continue
        entry = [line]
        i += 1
        while i < total:
            nxt = lines[i]
            if _is_continuation(nxt):
                entry.append(nxt)
                i += 1
            elif nxt.strip() == "":
                j = i
                while j < total and lines[j].strip() == "":
                    j += 1
                if j < total and _is_continuation(lines[j]):
                    entry.extend(lines[i:j])
                    i = j
                else:
                    break
            else:
                break
        blocks.append((True, entry))
    return blocks


#: The ledger heading that marks the deferral section (nexus-2x3qy).
#: Membership is by the LAST "## " heading seen before an entry -- any
#: other heading, or none yet, means the entry is not deferred. Matched
#: on the heading's own text (after "## "), so a bead-scoped suffix a
#: human appends does not break recognition.
DEFERRED_SECTION_HEADING = "Deferred to the next client release"

_HEADING_RE = re.compile(r"^##\s+(.*\S)\s*$")


def _deferred_entries(ledger_text: str) -> list[str]:
    """Path-carrying entries sitting under :data:`DEFERRED_SECTION_HEADING`.

    A deferred entry's straddling bead is exempt from
    :func:`atomic_split_check` -- that is the whole point of deferring
    it -- and :func:`deferred_paths` reads the same set to hold its
    channel path(s) back from the cut's import. Reuses
    ``_ledger_blocks`` so section membership and entry boundaries come
    from the one grouping.
    """
    deferred: list[str] = []
    in_section = False
    for is_bullet, lines in _ledger_blocks(ledger_text):
        if not is_bullet:
            for line in lines:
                heading = _HEADING_RE.match(line.rstrip("\n"))
                if heading:
                    in_section = heading.group(1).strip() == DEFERRED_SECTION_HEADING
            continue
        text = "".join(lines)
        has_path = any("/" in span for span in re.findall(r"`([^`]+)`", text))
        if in_section and has_path:
            deferred.append(text)
    return deferred


def deferred_paths(ledger_text: str) -> set[str]:
    """Channel paths a DEFERRED entry says must not ship on this cut.

    Only allowlisted (channel) spans count -- a deferred entry's wheel
    half was never going to ship regardless of deferral, and a bare
    prefix root in prose (``conexus/``) is not a path, mirroring the
    same filter :func:`_rewrite_ledger` applies to its own spans.
    """
    paths: set[str] = set()
    for entry in _deferred_entries(ledger_text):
        for span in re.findall(r"`([^`]+)`", entry):
            span = span.strip()
            if "/" in span.strip("/") and _is_allowlisted(span):
                paths.add(span)
    return paths


#: RDR-228 Phase 2 guard (nexus-sv2wx.13). Phase 2's plugin parts never go
#: out in a plugin-only cut ahead of the client release that carries
#: `--bootstrap` and the real server's setup tools. Phase 2 has SHIPPED to
#: installs when the reference tree carries this file. Phase 1 never adds it,
#: so its presence tells Phase 2 content from Phase 1 content in the files
#: both phases change.
PHASE2_MARKER = "conexus/launch/nx_setup.py"
#: Phase 1's launcher. A reference tree without it is one Phase 1 has not
#: reached, which widens the rule (see :func:`phase2_deferral_check`).
PHASE1_LAUNCHER = "conexus/launch/nx_launch.py"
#: Phase 1's whole footprint under ``conexus/launch/``. Any OTHER file there,
#: or the setup command, arms the guard even before the marker lands, so a
#: Phase 2 part that arrives ahead of ``nx_setup.py`` is still held to it.
PHASE1_LAUNCH_FILES = frozenset({PHASE1_LAUNCHER, "conexus/launch/nx_standin.py"})
#: Keyed on the DIRECTORY, not on file names: the stand-in's file and the
#: credentials page's module were not named when this guard was written, and
#: a list of guessed names would pass without covering them.
#:
#: Every other ``conexus/`` file Phase 2 changes is listed by name, because a
#: partial list is a hole: the hook shim calls ``_cli_lookup.cannot_start``,
#: so a cut that ships the shim without the lookup raises ``AttributeError``
#: on every hook it runs, and the lockstep hook skips the upgrade dispatch
#: for a CLI that cannot start with no setup process to repair it. The
#: README and the command registry describe ``/conexus:setup``, which a cut
#: that holds the command back does not ship. A path that is safe alone is
#: not listed here but in :data:`PHASE2_SAFE_ALONE`, with a reason, and
#: ``tests/test_plugin_release_drift_ledger.py`` fails on a changed
#: ``conexus/`` path that is in neither. ``conexus/hooks/``, ``conexus/launch/``,
#: ``conexus/commands/``, ``registry.yaml``, ``README.md``, ``hooks.json`` and
#: ``.mcp.json`` keep the every-path rule: they are where a caller can ship
#: without its callee. ``conexus/skills/``, ``agents/`` and ``resources/`` are
#: safe alone by prefix, backed by a content scan in the drift-ledger test.
PHASE2_GUARDED = (
    "conexus/launch/",
    "conexus/hooks/scripts/nx_setup_notice.py",
    "conexus/hooks/scripts/_endpoint_resolve.py",
    "conexus/hooks/scripts/_cli_lookup.py",
    "conexus/hooks/scripts/_exec_path.py",
    "conexus/hooks/scripts/nx_hook_shim.py",
    "conexus/hooks/scripts/version_lockstep_hook.py",
    "conexus/commands/setup.md",
    "conexus/registry.yaml",
    "conexus/README.md",
)
#: Added while the reference lacks the launcher: these call it (the two
#: server entries and the hook entries), so they cannot ship ahead of it.
PHASE2_GUARDED_WITHOUT_LAUNCHER = (
    "conexus/hooks/hooks.json",
    "conexus/.mcp.json",
)
#: Changed on the Phase 2 branch against the release reference, yet safe to
#: ship in a plugin-only cut ahead of the client release: path (exact, or a
#: ``/``-ending prefix) -> why. Each reason is a claim about the file, and
#: the drift-ledger test checks the one it can: that a Python file here
#: calls no name of a guarded module that the reference lacks. An entry is
#: never a way to make the coverage test pass; it is a decision that the
#: path stands alone.
PHASE2_SAFE_ALONE: dict[str, str] = {
    "conexus/PENDING_RELEASE.md": (
        "the ledger declares what ships; it runs nothing, and a cut rewrites "
        "it for the cut itself"
    ),
    "conexus/.mcp.json": (
        "its change is nexus-fbtyl's (dropping the server-level alwaysLoad "
        "flag), not RDR-228's; it names no Phase 2 file. Held back by "
        "PHASE2_GUARDED_WITHOUT_LAUNCHER while the reference lacks the "
        "launcher, and by its own ledger deferral to fbtyl's client release"
    ),
    "conexus/skills/": (
        "skill text is read by the model, runs nothing, and calls no Phase 2 "
        "file; the drift-ledger test scans every file under it for the names "
        "of the Phase 2 files and the setup command, and fails on a hit"
    ),
    "conexus/agents/": (
        "agent text is read by the model, runs nothing, and calls no Phase 2 "
        "file; the drift-ledger test scans every file under it for the names "
        "of the Phase 2 files and the setup command, and fails on a hit"
    ),
    "conexus/resources/": (
        "shared reference text, read by the model, runs nothing, and calls no "
        "Phase 2 file; the drift-ledger test scans every file under it for "
        "the names of the Phase 2 files and the setup command, and fails on a hit"
    ),
    "conexus/hooks/scripts/routing/subagent_git_write_requires_orchestrator.py": (
        "a stand-alone PreToolUse guard (nexus-yc7fs, nexus-ikoqj); it "
        "imports no Phase 2 module"
    ),
    "conexus/hooks/scripts/version_lockstep_action.py": (
        "UTF-8 text IO only (nexus-3fme8, nexus-ikoqj); it imports "
        "_cli_lookup, _endpoint_resolve and _exec_path but calls only names "
        "the reference already has, which the test checks"
    ),
}


def phase2_in_play(repo: Path, reference: str, source_ref: str) -> bool:
    """True when Phase 2 is in *source_ref* and *reference* has not shipped it.

    The state :func:`phase2_deferral_check` guards; the drift-ledger test
    uses it to know when the coverage rule applies.
    """
    return _phase2_armed_by(repo, source_ref) is not None and not _exists_at(
        repo, reference, PHASE2_MARKER
    )


def phase2_guarded_paths(repo: Path, reference: str) -> list[str]:
    """The paths :func:`phase2_deferral_check` holds to deferral for *reference*."""
    guarded = list(PHASE2_GUARDED)
    if not _exists_at(repo, reference, PHASE1_LAUNCHER):
        guarded += PHASE2_GUARDED_WITHOUT_LAUNCHER
    return guarded


def _exists_at(repo: Path, ref: str, path: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{ref}:{path}", check=False).returncode == 0


def _phase2_armed_by(repo: Path, source_ref: str) -> str | None:
    """The path in *source_ref* that shows Phase 2 is present, or None."""
    for path in (PHASE2_MARKER, "conexus/commands/setup.md"):
        if _exists_at(repo, source_ref, path):
            return path
    launch = _git(
        repo, "ls-tree", "-r", "--name-only", source_ref, "--", "conexus/launch/"
    ).stdout.splitlines()
    for path in sorted(p.strip() for p in launch if p.strip()):
        if path not in PHASE1_LAUNCH_FILES:
            return path
    return None


def phase2_deferral_check(
    repo: Path, reference: str, source_ref: str, ledger_text: str
) -> str:
    """Every guarded path that differs from *reference* must be DEFERRED.

    *reference* is what installs already have: ``origin/main`` in a cut
    (the tree the cut branch starts from and holds deferred paths back
    to), the marketplace pin in the drift-ledger test. NOT the client tag:
    after one plugin cut ships Phase 1, the client tag still lacks it, and
    measuring from there would demand deferral entries for files that no
    longer differ from the pin, which the stale-entry test then rejects.
    *source_ref* is the tree the cut ships from; *ledger_text* is that
    tree's ``conexus/PENDING_RELEASE.md``.

    Returns a one-line account: that it had nothing to check (Phase 2 is
    not in the tree, or *reference* already carries it), or how many paths
    it confirmed deferred. Raises :class:`LedgerRefused` naming each path that
    is not under "Deferred to the next client release", or either ref when
    it does not resolve.

    The case the refusal states: Phase 2 changes files Phase 1 adds. When
    Phase 1 has not shipped, a file cannot be "Awaiting" for Phase 1 and
    "Deferred" for Phase 2, because deferring holds the whole file back.
    Phase 1 and Phase 2 then ship in the SAME client release and every path
    of both sits under "Deferred". The guard widens to the launcher's
    callers (``hooks.json``, ``.mcp.json``, the lockstep hook) in that
    case, so a plugin-only cut cannot ship entries that call a launcher
    the cut holds back.
    """
    for ref in (reference, source_ref):
        if _git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}",
                check=False).returncode != 0:
            raise LedgerRefused(
                f"phase 2 guard: {ref} does not resolve, so whether Phase 2 "
                "is present or shipped is unknown"
            )
    armed_by = _phase2_armed_by(repo, source_ref)
    if armed_by is None:
        return (
            f"phase 2 guard: nothing to check -- no Phase 2 part is in "
            f"{source_ref}"
        )
    if _exists_at(repo, reference, PHASE2_MARKER):
        return (
            f"phase 2 guard: nothing to check -- {reference} carries "
            f"{PHASE2_MARKER}, so Phase 2 has shipped"
        )
    launcher_shipped = _exists_at(repo, reference, PHASE1_LAUNCHER)
    guarded = phase2_guarded_paths(repo, reference)
    # --no-renames: a rename lists only its new name by default, and the
    # old name's deletion reaches installs through a cut like any edit.
    differing = [
        line.strip()
        for line in _git(
            repo, "diff", "--name-only", "--no-renames", reference, source_ref,
            "--", *guarded,
        ).stdout.splitlines()
        if line.strip()
    ]
    deferred = deferred_paths(ledger_text)
    missing = sorted(set(differing) - deferred)
    if missing:
        message = (
            f"RDR-228 Phase 2 is in {source_ref} ({armed_by}) and {reference} "
            f"does not carry {PHASE2_MARKER}, so every Phase 2 plugin path "
            f"that differs from {reference} must sit under "
            f"'## {DEFERRED_SECTION_HEADING}' in {LEDGER}. Not under that "
            f"heading:\n  " + "\n  ".join(missing)
            + "\nMove each entry under the 'Deferred to the next client "
            "release' heading; a plugin-only cut ahead of the client release "
            "that carries --bootstrap and the real setup tools would ship "
            "setup parts that cannot work. Do not take a shared file off the "
            "guarded list to make this pass. An unrelated fix to a guarded "
            "file waits for that client release too."
        )
        if not launcher_shipped:
            message += (
                f"\nPhase 1 has not shipped ({reference} lacks "
                f"{PHASE1_LAUNCHER}), so a file cannot be 'Awaiting' for "
                "Phase 1 and 'Deferred' for Phase 2: deferring holds the "
                "whole file back. Phase 1 and Phase 2 then ship in the same "
                "client release, and every path of both -- including "
                "hooks.json and .mcp.json -- sits under 'Deferred'."
            )
        raise LedgerRefused(message)
    return (
        f"phase 2 guard: {len(differing)} guarded path(s) differing from "
        f"{reference}, all under '{DEFERRED_SECTION_HEADING}'"
    )
