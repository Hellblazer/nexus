# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plugin changes that are not live yet must be DECLARED, not assumed active.

THE INCIDENT (2026-07-25). A subagent ran ``git stash -u`` in a shared working
tree. The routing guard for that is explicit about ``stash`` and treats the bare
form as destructive -- and it did not fire. Cause: ``marketplace.json`` pins
``plugins[].source.ref`` to an immutable release tag, so Claude Code loads hooks,
commands, skills, and agents from THAT TAG, never from the working tree. The
stash coverage had merged hours earlier. The installed plugin was still v6.18.1.

Three guards had been merged, closed as "mechanized", and were protecting
nothing. A guard BELIEVED live but actually inert is worse than a known-absent
one, because nobody compensates for it.

WHY A LEDGER AND NOT A PLAIN DRIFT FAILURE. The obvious tripwire -- fail when
the surface differs from the pinned tag -- would be RED CONTINUOUSLY between
releases. This repo has documented, more than once, what happens to a check that
cries wolf: it gets blessed reflexively and stops meaning anything. So drift is
not itself a failure. UNDECLARED drift is. Declaring costs one line; the ledger
then doubles as the "what goes live next release" list, which is information
nobody had before.

THE SYMMETRY IS LOAD-BEARING. A stale entry fails too. Once a release ships and
the pin advances, drift returns to zero and the ledger must be emptied -- so it
cannot quietly decay into fiction that everything is pending forever.

WHY THE SURFACE IS BROADER THAN HOOKS. commands/ and skills/ are pinned by the
same mechanism and are equally inert when edited. The same 2026-07-25 session
edited continuation.md and orchestration/SKILL.md believing the corrections took
effect immediately; they did not.
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent
LEDGER = REPO_ROOT / "conexus" / "PENDING_RELEASE.md"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

#: The plugin's BEHAVIOURAL surface -- what a session actually executes or reads.
#: Deliberately excludes CHANGELOG.md / README.md: stale docs in a shipped
#: plugin are harmless, whereas a stale hook is a guard that is not guarding.
SURFACE: tuple[str, ...] = (
    "conexus/hooks/",
    "conexus/commands/",
    "conexus/skills/",
    "conexus/agents/",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )


def _pinned_ref() -> str:
    data = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    for plugin in data.get("plugins", []):
        if plugin.get("name") == "conexus":
            source = plugin.get("source")
            if isinstance(source, dict) and source.get("ref"):
                return str(source["ref"])
    raise AssertionError(
        "marketplace.json has no conexus plugin with a source.ref -- the pinned "
        "release model this test depends on has changed shape. Do not delete "
        "this test; update it to the new shape."
    )


def _has_any_tags() -> bool:
    return bool(_git("tag", "-l").stdout.strip())


def _drifted_paths(ref: str) -> list[str]:
    proc = _git("diff", "--name-only", f"{ref}..HEAD", "--", *SURFACE)
    # A FAILED diff must never read as "no drift". That silent-zero is the whole
    # bug class this file exists for.
    assert proc.returncode == 0, (
        f"git diff against {ref} failed, so drift is UNKNOWN, not zero:\n"
        f"{proc.stderr}"
    )
    return sorted(p for p in proc.stdout.splitlines() if p.strip())


def _ledger_text() -> str:
    return LEDGER.read_text(encoding="utf-8") if LEDGER.exists() else ""


# --- non-vacuity: this file must not pass by checking nothing ----------------


def test_the_surface_prefixes_all_exist() -> None:
    """A renamed or deleted directory would silently shrink coverage to zero.

    Without this, moving conexus/hooks/ elsewhere leaves every other test here
    passing forever while watching an empty set.
    """
    missing = [p for p in SURFACE if not (REPO_ROOT / p).is_dir()]
    assert not missing, (
        f"SURFACE names directories that do not exist: {missing}. Either the "
        "plugin layout moved (update SURFACE) or a directory was deleted. Until "
        "this is corrected the drift check is watching nothing."
    )


def test_the_ledger_file_exists() -> None:
    assert LEDGER.exists(), (
        f"{LEDGER.relative_to(REPO_ROOT)} is missing. It is the acknowledgement "
        "ledger for plugin changes that are not live yet; deleting it does not "
        "make the gap go away, it makes it invisible again."
    )


# --- the pin itself ----------------------------------------------------------


def test_the_pinned_ref_is_a_real_tag() -> None:
    """marketplace.json must not name a tag that does not exist.

    Distinguishes two very different situations that look alike:
      * no tags at all  -> shallow CI checkout, skip (and say so)
      * tags, but not THIS one -> a real defect: users would install from a ref
        that cannot be resolved.
    """
    ref = _pinned_ref()
    if not _has_any_tags():
        pytest.skip(
            "no tags in this checkout (shallow clone) -- cannot resolve the "
            "pinned ref. Verified genuinely tagless rather than assumed."
        )
    proc = _git("rev-parse", "--verify", f"{ref}^{{commit}}")
    assert proc.returncode == 0, (
        f"marketplace.json pins source.ref={ref!r}, but that tag does not exist "
        f"in this repository. Installed users resolve the plugin from that ref, "
        f"so it must be a real, pushed tag."
    )


# --- the ledger contract, both directions -----------------------------------


def test_every_drifted_file_is_declared_in_the_ledger() -> None:
    """Undeclared drift is the failure -- drift itself is not.

    A guard merged without a ledger entry is a guard someone will believe is
    protecting them. One line here is the whole cost of saying otherwise.
    """
    if not _has_any_tags():
        pytest.skip("no tags in this checkout (shallow clone); drift unresolvable")
    ref = _pinned_ref()
    drifted = _drifted_paths(ref)
    if not drifted:
        return  # nothing pending; the stale-entry test covers the other side

    ledger = _ledger_text()
    undeclared = [p for p in drifted if p not in ledger]
    assert not undeclared, (
        "These plugin files differ from the pinned release tag "
        f"({ref}) but are NOT declared in conexus/PENDING_RELEASE.md:\n"
        + "\n".join(f"  {p}" for p in undeclared)
        + "\n\nThey are INERT: Claude Code loads this plugin from the pinned "
        "tag, so these changes do not run in any session until the next "
        "release ships.\n"
        "FIX: add each path to conexus/PENDING_RELEASE.md with one line saying "
        "what it changes and which bead. Do NOT weaken this test instead -- the "
        "entry is the honest statement that the change is not yet live."
    )


def test_the_ledger_has_no_stale_entries() -> None:
    """After a release the pin advances, drift goes to zero, and this empties.

    Without this half the ledger rots into a permanent list of things that
    already shipped, which reads exactly like a list of things that have not --
    the same believed-vs-actual confusion, one level up.
    """
    if not _has_any_tags():
        pytest.skip("no tags in this checkout (shallow clone); drift unresolvable")
    ref = _pinned_ref()
    drifted = set(_drifted_paths(ref))

    stale = [
        line.split("`")[1]
        for line in _ledger_text().splitlines()
        if line.lstrip().startswith("- `") and "`" in line.lstrip()[3:]
    ]
    stale = [p for p in stale if p.startswith("conexus/") and p not in drifted]
    assert not stale, (
        f"conexus/PENDING_RELEASE.md lists files that NO LONGER differ from the "
        f"pinned tag ({ref}) -- they have shipped:\n"
        + "\n".join(f"  {p}" for p in stale)
        + "\n\nRemove them. A ledger of already-shipped changes is "
        "indistinguishable from a ledger of pending ones."
    )
