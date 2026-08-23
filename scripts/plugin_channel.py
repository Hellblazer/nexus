# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Primitives for the plugin-only release channel (RDR-197, nexus-a2wmi.1).

Design of record: docs/rdr/rdr-197-plugin-only-release-channel.md as
amended by 2a3e265fc and 874bd681c. The channel is STATELESS: there is
no counter file; git's tag list is the only record of cuts. This
docstring is the single home of the two invariants and the anchoring
rule — beads .3, .4, .8 and .14 cite them, none restates them. The
principle: every check is per plugin and anchored to a tag, never
global and never against a working tree.

INVARIANT R — per-plugin ref validation. Every pinned ref is judged
under its OWN shape: the client form ``v{version}``, or the anchored
form ``plugin-v{version}-{n}`` for any positive integer n. Never a
quantifier across plugins. Mixed shapes are the channel's normal state
after any cut.

INVARIANT W — per-plugin window, WITH ITS CLOSER. A plugin is in its
release window only when ALL of the following hold:

  (a) its ref is valid for the CURRENT pyproject version under
      invariant R;
  (b) for the anchored form, n equals ``next_plugin_tag_number(version)``,
      so the ref names the NEXT tag rather than any nonexistent one;
  (c) the tag's absence is confirmed upstream, reusing the existing
      remote probe rather than trusting local unresolvability, AND tag
      visibility is established by ``assert_tag_visibility`` so that
      conditions (b) and (c) are answered from a checkout that can
      actually see tags;
  (d) for the anchored form, the checkout is the cut branch itself,
      ``plugin-release/{version}-{n}``, with n matching the ref's own n.

Outside all four, an anchored ref naming a missing tag is RED, not
exempt. Condition (d) is the closer: without it, a cut that merges and
whose tag is never pushed keeps its plugin exempt from the real-tag
check with its drift contract disabled, and nothing bounds that but the
next client release. Branch name comes from ``GITHUB_HEAD_REF`` when
set, else ``git rev-parse --abbrev-ref HEAD``; when neither yields a
name, no window is granted. No ancestry walk: depth-1 clones cannot
compute ancestry, and this check needs none.

Why (b) and (d) and not a "later tag exists" rule: (b) already subsumes
it. If ``plugin-v7.15.0-4`` exists then ``next_plugin_tag_number`` is 5,
so a ref naming ``-3`` fails (b) without any separate rule. What (b)
cannot see is the case where the ref names exactly the next number and
the tag was simply never pushed; (d) is what closes that, and it closes
it within one back-merge to develop rather than waiting on a client
release.

ANCHORING — the wheel-surface proof's target, stated exactly. The
proof's range is: base client tag (the range's base) to the TARGET,
where the target is the plugin's anchored tag when it resolves, and
otherwise the cut PR's HEAD COMMIT. A merge base is only ever a range
base, never a target: the merge base is the fork point and carries none
of the cut's content, so a merge-base target makes the proof vacuous.
Never HEAD of a branch that tracks ongoing development either: on
post-cut develop, HEAD carries every unreleased wheel change, so such a
target reports offenders for a cut that shipped none.

This module lives under ``scripts/`` deliberately: pyproject's pytest
``pythonpath = ["scripts"]`` makes it importable from tests with no
packaging, and ``scripts/`` is in neither the wheel force-include nor
the sdist include list, so the channel's machinery is off the wheel
surface by construction (pinned by tests/test_plugin_channel.py).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

__all__ = [
    "ALLOWED_EXACT",
    "ALLOWED_PREFIXES",
    "DENIED_PREFIXES",
    "PLUGIN_BY_ALLOWLIST_PREFIX",
    "GitCommandError",
    "TagVisibilityError",
    "assert_tag_visibility",
    "current_branch_name",
    "format_plugin_tag",
    "is_channel_path",
    "is_cut_branch_for",
    "next_plugin_tag_number",
    "parse_plugin_tag",
    "path_has_prefix",
    "wheel_surface_offenders",
]


class TagVisibilityError(RuntimeError):
    """The base client tag does not resolve: the checkout is blind to tags."""


class GitCommandError(RuntimeError):
    """A git invocation failed; the answer is unknown, not empty."""


#: The channel allowlist — a strict SUPERSET of the loader-visible
#: SURFACE_BY_PLUGIN in tests/test_plugin_release_drift_ledger.py: it
#: also admits conexus/CHANGELOG.md, conexus/registry.yaml,
#: conexus/evals/, conexus/PENDING_RELEASE.md and every path under sn/.
#: Do not narrow this to SURFACE_BY_PLUGIN and do not widen
#: SURFACE_BY_PLUGIN to this.
ALLOWED_PREFIXES: tuple[str, ...] = ("conexus/", "sn/")

ALLOWED_EXACT: tuple[str, ...] = (".claude-plugin/marketplace.json",)

#: Wheel content living inside the plugin trees (hatch force-include
#: ships them as package data), carved OUT of the channel: a plugin cut
#: may never alter wheel behaviour.
DENIED_PREFIXES: tuple[str, ...] = ("conexus/plans/", "conexus/daemon/")

#: Ref movement keys on THIS mapping, not on SURFACE_BY_PLUGIN: a cut
#: carrying only conexus/evals/ or conexus/registry.yaml ships real
#: content the loader-visible surface does not cover, and keying on
#: SURFACE_BY_PLUGIN would move no ref and ship nothing.
#: .claude-plugin/marketplace.json belongs to no plugin and moves no ref.
PLUGIN_BY_ALLOWLIST_PREFIX: dict[str, str] = {
    "conexus/": "conexus",
    "sn/": "sn",
}

#: \Z, not $: a bare $ also matches before a trailing newline, so
#: "plugin-v7.15.0-1\n" would parse (review finding, a2wmi.6).
_PLUGIN_TAG_RE = re.compile(r"^plugin-v(\d+\.\d+\.\d+)-([1-9]\d*)\Z")


def format_plugin_tag(version: str, n: int) -> str:
    """Render the anchored tag form for *version*'s *n*-th cut."""
    return f"plugin-v{version}-{n}"


def parse_plugin_tag(ref: str) -> tuple[str, int] | None:
    """Parse an anchored-form ref into ``(version, n)``.

    Matches exactly ``plugin-v`` + a three-part dotted version + ``-`` +
    an integer of 1 or greater with no leading zero. Anything else —
    the client form, a truncated version, a zero or zero-padded n, an
    extra segment — returns ``None``.
    """
    match = _PLUGIN_TAG_RE.match(ref)
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def path_has_prefix(path: str, prefix: str) -> bool:
    """Whether *path* equals *prefix* or sits under it, component-wise.

    The hatch force-include keys are written WITHOUT a trailing slash
    ("conexus/plans") while :data:`DENIED_PREFIXES` carries one, so a
    bare ``startswith`` would report a carved-out path as uncovered —
    and would match "conexus/plansible" against "conexus/plans". Both
    sides are normalised and the match is on a path-component boundary.
    """
    normalised_path = path.rstrip("/")
    normalised_prefix = prefix.rstrip("/")
    return normalised_path == normalised_prefix or normalised_path.startswith(
        normalised_prefix + "/"
    )


def _run_git(
    args: list[str], cwd: str | Path | None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    )


def assert_tag_visibility(version: str, *, cwd: str | Path | None = None) -> None:
    """The BLIND-CHECKOUT SENTINEL: raise unless ``v{version}`` resolves.

    A shallow checkout that fetched only some refs enumerates ZERO
    plugin tags and is indistinguishable from a world with no cuts,
    which would let :func:`next_plugin_tag_number` return 1 while
    ``plugin-v{version}-3`` exists and would silently defeat window
    condition (b). The base client tag must exist in any state where an
    anchored ref is evaluated, because a cut is built on a RELEASED
    client version, so its absence proves the checkout is blind rather
    than the world empty. The single state where ``v{version}``
    legitimately does not exist is the client release window, and there
    every ref is the client form, so the anchored evaluator never runs.
    """
    probe = _run_git(
        ["rev-parse", "--verify", "--quiet", f"v{version}^{{commit}}"], cwd
    )
    if probe.returncode != 0:
        raise TagVisibilityError(
            f"base client tag v{version} does not resolve: this checkout is "
            f"blind to tags, so the plugin tag list cannot be trusted. Fetch "
            f"tags before evaluating an anchored ref."
        )


def next_plugin_tag_number(version: str, *, cwd: str | Path | None = None) -> int:
    """One more than the highest existing cut number for *version*.

    Calls :func:`assert_tag_visibility` FIRST: an empty enumeration is
    trusted only after the sentinel passes — unreadable and unfetched
    must never read as empty. Unparsable tags are ignored rather than
    crashed on; a failing git command raises :class:`GitCommandError`.
    With no matching tag and visibility established, returns 1.
    """
    assert_tag_visibility(version, cwd=cwd)
    listing = _run_git(["tag", "-l", f"plugin-v{version}-*"], cwd)
    if listing.returncode != 0:
        raise GitCommandError(
            f"git tag -l failed (rc {listing.returncode}): "
            f"{listing.stderr.strip()}"
        )
    numbers = [
        parsed[1]
        for tag in listing.stdout.splitlines()
        if (parsed := parse_plugin_tag(tag.strip())) is not None
        and parsed[0] == version
    ]
    return max(numbers, default=0) + 1


def current_branch_name(*, cwd: str | Path | None = None) -> str | None:
    """The checkout's branch name, or ``None`` when none resolves.

    ``GITHUB_HEAD_REF`` wins when set and non-empty (on pull_request
    events the checkout is a detached merge ref, so git cannot name the
    branch). Otherwise ``git rev-parse --abbrev-ref HEAD``. A detached
    HEAD with no environment hint returns ``None`` — never guess; no
    name means no window.
    """
    env_hint = os.environ.get("GITHUB_HEAD_REF", "").strip()
    if env_hint:
        return env_hint
    probe = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    if probe.returncode != 0:
        return None
    name = probe.stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


def is_cut_branch_for(
    version: str, n: int, *, cwd: str | Path | None = None
) -> bool:
    """Window condition (d): the checkout IS the cut branch for (version, n)."""
    return current_branch_name(cwd=cwd) == f"plugin-release/{version}-{n}"


def is_channel_path(path: str) -> bool:
    """Whether *path* is content a plugin cut may ship: inside the
    allowlist and not carved out by :data:`DENIED_PREFIXES`. The single
    predicate behind the wheel-surface proof and the cut script's own
    allowlist decisions (R2 finding: two verbatim copies drift)."""
    if any(path_has_prefix(path, denied) for denied in DENIED_PREFIXES):
        return False
    if path in ALLOWED_EXACT:
        return True
    return any(path_has_prefix(path, allowed) for allowed in ALLOWED_PREFIXES)


def wheel_surface_offenders(
    base_ref: str, target_ref: str, *, cwd: str | Path | None = None
) -> list[str]:
    """Paths changed in ``base_ref..target_ref`` that a cut may not touch.

    *target_ref* is required, with no default, and the ANCHORING rule in
    the module docstring names it: the plugin's anchored tag when it
    resolves, else the cut PR's head commit. Passing a merge base (the
    fork point carries none of the cut's content — the proof goes
    vacuous), or the HEAD of a branch that tracks ongoing development
    (reports offenders the cut never shipped), is a caller bug.

    Raises :class:`GitCommandError` when the git command exits non-zero
    or either ref does not resolve — this function never returns an
    empty list because the comparison failed.

    ACCEPTED FLATNESS (R1 review, a2wmi.6): the allowlist is judged
    across BOTH plugin trees, not scoped to the plugin whose ref moved —
    the proof guarantees "no wheel content ships", not "this plugin's
    cut touches only its own tree". Cross-plugin content isolation was
    never in the design of record; ref movement is what keys on
    :data:`PLUGIN_BY_ALLOWLIST_PREFIX`, and a cut carrying both trees
    still ships only plugin surface.
    """
    diff = _run_git(["diff", "--name-only", f"{base_ref}..{target_ref}"], cwd)
    if diff.returncode != 0:
        raise GitCommandError(
            f"git diff --name-only {base_ref}..{target_ref} failed "
            f"(rc {diff.returncode}): {diff.stderr.strip()}"
        )
    changed = [line.strip() for line in diff.stdout.splitlines() if line.strip()]
    return sorted(path for path in changed if not is_channel_path(path))
