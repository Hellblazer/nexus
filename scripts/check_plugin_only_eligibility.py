#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Mechanical eligibility gate for the plugin-only fast lane (nexus-rbmja.1).

Design of record: T2 nexus/design-plugin-only-fast-lane.md, "Eligibility".
A patch release off the last client tag may skip the heavy battery legs
only when its diff from that tag touches nothing the nx code under test
is built from.

    uv run python scripts/check_plugin_only_eligibility.py --base vX.Y.Z [--head HEAD] \\
        [--develop-ref origin/develop]

``--base`` must be a client tag ``vX.Y.Z`` and ``--head`` must descend from it.
Diffs ``base..head`` (raw, renames off, so a move out of ``src/`` shows the
``src/`` deletion). ELIGIBLE (exit 0, last line ``PLUGIN-ONLY ELIGIBILITY
PASSED``) only if the head's ``pyproject.toml`` version is the base's with the
patch number plus one (a plugin diff that does not move the version is never
delivered, nexus-konsk) and every changed path is one of:

  a. a channel path per :func:`plugin_channel.is_channel_path` (imported,
     never copied);
  b. under ``tests/`` or ``docs/``, except the release machinery that gates
     this lane (:data:`HARNESS_EXACT`, :data:`HARNESS_PREFIXES`): the battery,
     the lockstep wrapper and tests, the skew harness, the delivery proof;
  c. ``CHANGELOG.md`` or ``conexus/PENDING_RELEASE.md``;
  d. a version surface whose CONTENT change is the version and nothing
     else: ``pyproject.toml`` (the ``[project]`` version line),
     ``mcpb/pyproject.toml`` (that line and the ``conexus>=`` pin),
     ``mcpb/manifest.json`` (the top-level ``version``), ``uv.lock``
     (the ``conexus`` package stanza's ``version`` line).

A bead must also not straddle the lane (nexus-rbmja.10). Each bead id named
in the commit messages of ``base..head`` (``cherry-pick -x`` keeps the original
message) is looked up on ``--develop-ref`` (default ``origin/develop``): every
non-merge commit in ``base..develop-ref`` whose message names that bead is
classified by the rules above, and one that touches a path outside the eligible
set makes the lane INELIGIBLE, naming the bead and the commit. A cherry-pick
that takes the plugin half of a bead and leaves its ``src/`` half behind is
exactly what this catches. Attribution is by commit message, matched on the
whole bead token, so a straddling commit that does not name its bead escapes
this check: the per-path rules above bind only what was picked, and an unpicked
half that names no bead is not seen. Any bead token in a picked commit's
message counts, a prose mention of an unrelated bead included, so the check can
refuse more than it must and never less. An unreadable
``--develop-ref`` is UNVERIFIED, never a pass.

Everything else is INELIGIBLE (exit 1, each offender with its reason):
``src/`` (the engine pin lives there), ``service/``, any other line of the
four surfaces, ``scripts/``, ``.github/``, the rest of ``mcpb/``, the release
machinery under ``tests/``, and any type change or symlink (mode 120000).

Could-not-check is never a pass: an unresolvable ``--base`` or ``--head``
(a blind checkout, a non-repo, a ``--base`` that is not ``vX.Y.Z``) exits 2
with ``PLUGIN-ONLY ELIGIBILITY UNVERIFIED``. An EMPTY diff exits 1: there is
nothing to release, and a vacuous pass would let an empty cut through.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from plugin_channel import (
    GitCommandError,
    TagVisibilityError,
    assert_tag_visibility,
    client_version_of,
    is_channel_path,
    path_has_prefix,
)

PASS_LINE = "PLUGIN-ONLY ELIGIBILITY PASSED"
FAIL_LINE = "PLUGIN-ONLY ELIGIBILITY FAILED"
UNVERIFIED_LINE = "PLUGIN-ONLY ELIGIBILITY UNVERIFIED"

#: Prefixes whose content never reaches the nx code under test.
FREE_PREFIXES: tuple[str, ...] = ("tests/", "docs/")
#: Exact paths released alongside a plugin-only cut.
FREE_EXACT: tuple[str, ...] = ("CHANGELOG.md", "conexus/PENDING_RELEASE.md")
#: The machinery that gates this lane. It lives under ``tests/`` but is not free:
#: an eligible diff that edited the eligibility call or the lockstep leg would
#: change its own gate. That covers the scripts the battery runs or sources in
#: this mode (preflight, tests/e2e/lib) and the tests that pin this script, the
#: channel allowlist it imports, and the battery mode.
HARNESS_EXACT: tuple[str, ...] = (
    "tests/e2e/release-battery.sh",
    "tests/e2e/release-preflight.sh",
    "tests/e2e/plugin-lockstep-tests.sh",
    "tests/e2e/plugin-delivery-proof.sh",
    "tests/test_plugin_lockstep.py",
    "tests/hooks/test_lockstep_survives_cli_skew.py",
    "tests/test_plugin_channel.py",
    "tests/scripts/test_check_plugin_only_eligibility.py",
    "tests/scripts/test_release_battery_end_to_end.py",
    "tests/scripts/test_plugin_lockstep_wrapper.py",
)
HARNESS_PREFIXES: tuple[str, ...] = ("tests/e2e/hook-cli-skew/", "tests/e2e/lib/")
#: Git modes that are not a regular file: symlink, submodule.
SPECIAL_MODES: frozenset[str] = frozenset({"120000", "160000"})

_CLIENT_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")
_SEMVER = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)$")
_PROJECT_VERSION = re.compile(r'^version\s*=\s*"([^"]*)"\s*$')

_VERSION_VALUE = r'"[0-9][^"]*"'
_VERSION_LINE = re.compile(rf"^version\s*=\s*{_VERSION_VALUE}\s*$")
_CONEXUS_PIN = re.compile(rf'^(\s*)"conexus>=[0-9][^"]*"(,?)\s*$')
_TABLE_HEADER = re.compile(r"^\s*\[+\s*([^\]]*?)\s*\]+\s*$")
_NAME_LINE = re.compile(r'^name\s*=\s*"([^"]*)"\s*$')

_SENTINEL = "<VERSION>"


class Unverifiable(RuntimeError):
    """The check could not be performed; never a pass."""


class Change(NamedTuple):
    status: str
    path: str
    old_mode: str
    new_mode: str


# ---- git ---------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    except OSError as exc:  # git itself missing
        raise Unverifiable(f"cannot run git: {exc}") from exc


def _resolve(ref: str, cwd: Path, label: str) -> None:
    version = client_version_of(ref)
    if version is not None:
        try:
            assert_tag_visibility(version, cwd=cwd)
        except (TagVisibilityError, GitCommandError) as exc:
            raise Unverifiable(f"--{label} {ref}: {exc}") from exc
        return
    probe = _git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd)
    if probe.returncode != 0:
        raise Unverifiable(f"--{label} {ref} does not resolve to a commit")


def is_ancestor(base: str, head: str, cwd: Path) -> bool:
    """Whether *head* descends from *base*; could-not-check raises."""
    out = _git(["merge-base", "--is-ancestor", base, head], cwd)
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    raise Unverifiable(
        f"git merge-base --is-ancestor {base} {head} failed (rc {out.returncode}): "
        f"{out.stderr.decode(errors='replace').strip()}"
    )


def changed_paths(base: str, head: str, cwd: Path) -> list[Change]:
    """Every path differing between *base* and *head*, with git's own modes."""
    out = _git(["diff", "--raw", "--no-renames", "-z", base, head], cwd)
    if out.returncode != 0:
        raise Unverifiable(
            f"git diff {base} {head} failed (rc {out.returncode}): "
            f"{out.stderr.decode(errors='replace').strip()}"
        )
    fields = out.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise Unverifiable("unparsable git diff --raw output")
    changes: list[Change] = []
    for i in range(0, len(fields), 2):
        meta = fields[i].lstrip(":").split()
        if len(meta) != 5:
            raise Unverifiable(f"unparsable git diff --raw entry: {fields[i]!r}")
        changes.append(Change(meta[4], fields[i + 1], meta[0], meta[1]))
    return changes


def _blob(rev: str, path: str, cwd: Path) -> str | None:
    out = _git(["show", f"{rev}:{path}"], cwd)
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", errors="surrogateescape")


# ---- version-surface normalisers ---------------------------------------------
# Each maps file text to text with the permitted version bytes replaced by a
# sentinel; base and head must then be byte-equal.


def _normalise_lines(text: str, rewrite: Callable[[str, str, str], str | None]) -> str:
    """Run *rewrite(line, table, package)* over each line; a returned string
    replaces the line. ``table`` is the current TOML table header; ``package``
    is the ``name`` of the current ``[[package]]`` stanza (uv.lock)."""
    table = ""
    package = ""
    out: list[str] = []
    for line in text.split("\n"):
        header = _TABLE_HEADER.match(line)
        if header:
            table = header.group(1)
            package = ""
        elif table == "package" and (name := _NAME_LINE.match(line)):
            package = name.group(1)
        replaced = rewrite(line, table, package)
        out.append(line if replaced is None else replaced)
    return "\n".join(out)


def _project_version(line: str, table: str, _package: str) -> str | None:
    if table == "project" and _VERSION_LINE.match(line):
        return f"version = {_SENTINEL}"
    return None


def _norm_pyproject(text: str) -> str:
    return _normalise_lines(text, _project_version)


def _mcpb_rewrite(line: str, table: str, package: str) -> str | None:
    if (replaced := _project_version(line, table, package)) is not None:
        return replaced
    if table == "project" and (pin := _CONEXUS_PIN.match(line)):
        return f'{pin.group(1)}"conexus>={_SENTINEL}"{pin.group(2)}'
    return None


def _norm_mcpb_pyproject(text: str) -> str:
    return _normalise_lines(text, _mcpb_rewrite)


def _lock_rewrite(line: str, table: str, package: str) -> str | None:
    if table == "package" and package == "conexus" and _VERSION_LINE.match(line):
        return f"version = {_SENTINEL}"
    return None


def _norm_uv_lock(text: str) -> str:
    return _normalise_lines(text, _lock_rewrite)


def _norm_manifest(text: str) -> str:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("manifest is not a JSON object")
    if not isinstance(data.get("version"), str):
        raise ValueError("manifest has no string version")
    data["version"] = _SENTINEL
    return json.dumps(data, sort_keys=True)


VERSION_SURFACES: dict[str, Callable[[str], str]] = {
    "pyproject.toml": _norm_pyproject,
    "mcpb/pyproject.toml": _norm_mcpb_pyproject,
    "mcpb/manifest.json": _norm_manifest,
    "uv.lock": _norm_uv_lock,
}

_SURFACE_ALLOWS = {
    "pyproject.toml": "only the [project] version line may change",
    "mcpb/pyproject.toml": "only the [project] version line and the conexus>= pin may change",
    "mcpb/manifest.json": "only the top-level version may change",
    "uv.lock": "only the conexus package stanza's version line may change",
}


# ---- the version bump --------------------------------------------------------


def project_version_of(text: str) -> str | None:
    """The ``[project]`` table's ``version`` value, or None when absent."""
    table = ""
    for line in text.split("\n"):
        header = _TABLE_HEADER.match(line)
        if header:
            table = header.group(1)
        elif table == "project" and (m := _PROJECT_VERSION.match(line)):
            return m.group(1)
    return None


def version_bump_problem(base: str, head: str, cwd: Path) -> str | None:
    """None when head's ``[project]`` version is base's with patch + 1."""
    before = _blob(base, "pyproject.toml", cwd)
    after = _blob(head, "pyproject.toml", cwd)
    if before is None or after is None:
        return "pyproject.toml must exist on both sides to check the version bump"
    old, new = project_version_of(before), project_version_of(after)
    old_m, new_m = _SEMVER.match(old or ""), _SEMVER.match(new or "")
    if old_m is None or new_m is None:
        return (
            f"pyproject.toml version is not X.Y.Z on both sides "
            f"(base {old!r}, head {new!r})"
        )
    major, minor, patch = (int(g) for g in old_m.groups())
    if tuple(int(g) for g in new_m.groups()) != (major, minor, patch + 1):
        return (
            f"version {old} -> {new} is not a patch bump; this lane ships "
            f"{major}.{minor}.{patch + 1} exactly (a diff that does not move "
            f"the version is never delivered)"
        )
    return None


# ---- the bead-level straddle check -------------------------------------------

_BEAD_TOKEN = re.compile(r"nexus-[a-z0-9]+(?:\.[0-9]+)*")


def bead_ids_in(text: str) -> set[str]:
    """Whole bead tokens in *text* (``nexus-abc.2`` is not ``nexus-abc``)."""
    return set(_BEAD_TOKEN.findall(text))


def _log_messages(range_spec: str, cwd: Path) -> list[tuple[str, str]]:
    """(sha, full message) of each non-merge commit in *range_spec*."""
    out = _git(
        ["log", "--no-merges", "--format=%H%x00%B%x01", range_spec], cwd
    )
    if out.returncode != 0:
        raise Unverifiable(
            f"git log {range_spec} failed (rc {out.returncode}): "
            f"{out.stderr.decode(errors='replace').strip()}"
        )
    text = out.stdout.decode("utf-8", errors="replace")
    commits: list[tuple[str, str]] = []
    for record in text.split("\x01"):
        record = record.strip("\n")
        if not record:
            continue
        sha, _, message = record.partition("\x00")
        commits.append((sha.strip(), message))
    return commits


class Straddle(NamedTuple):
    bead: str
    sha: str
    path: str
    reason: str


def straddling_beads(
    base: str, head: str, develop_ref: str, cwd: Path
) -> tuple[list[Straddle], int, int]:
    """(findings, beads named in base..head, develop commits examined).

    A bead straddles when a commit on *develop_ref* that names it touches a
    path :func:`classify` refuses, judged against that commit's own parent.
    """
    probe = _git(["rev-parse", "--verify", "--quiet", f"{develop_ref}^{{commit}}"], cwd)
    if probe.returncode != 0:
        raise Unverifiable(f"--develop-ref {develop_ref} does not resolve to a commit")
    beads: set[str] = set()
    for _sha, message in _log_messages(f"{base}..{head}", cwd):
        beads |= bead_ids_in(message)
    findings: list[Straddle] = []
    examined = 0
    if not beads:
        return findings, 0, 0
    for sha, message in _log_messages(f"{base}..{develop_ref}", cwd):
        named = sorted(beads & bead_ids_in(message))
        if not named:
            continue
        examined += 1
        for change in changed_paths(f"{sha}^", sha, cwd):
            _category, reason = classify(change, f"{sha}^", sha, cwd)
            if reason is not None:
                findings.extend(
                    Straddle(bead, sha, change.path, reason) for bead in named
                )
    return findings, len(beads), examined


# ---- classification ----------------------------------------------------------


def classify(
    change: Change, base: str, head: str, cwd: Path
) -> tuple[str, str | None]:
    """(category, reason). reason is None when the path is eligible."""
    status, path = change.status, change.path
    if status.startswith("T") or SPECIAL_MODES & {change.old_mode, change.new_mode}:
        return "special", (
            f"type change or symlink/submodule (mode {change.old_mode} -> "
            f"{change.new_mode}); a link can point anywhere"
        )
    if path in VERSION_SURFACES:
        before = _blob(base, path, cwd)
        after = _blob(head, path, cwd)
        if before is None or after is None:
            return "version-surface", f"status {status}: must exist on both sides"
        normalise = VERSION_SURFACES[path]
        try:
            same = normalise(before) == normalise(after)
        except ValueError as exc:
            return "version-surface", f"unparsable: {exc}"
        if same:
            return "version-surface", None
        return "version-surface", (
            f"changes more than the version ({_SURFACE_ALLOWS[path]})"
        )
    if path in HARNESS_EXACT or any(path_has_prefix(path, p) for p in HARNESS_PREFIXES):
        return "harness", (
            "release machinery that gates this lane; it ships through a "
            "normal release"
        )
    if is_channel_path(path):
        return "channel", None
    if any(path_has_prefix(path, p) for p in FREE_PREFIXES):
        return "tests-docs", None
    if path in FREE_EXACT:
        return "changelog", None
    return "other", "outside the plugin channel, tests/, docs/ and the version surfaces"


def evaluate(
    base: str, head: str, cwd: Path, develop_ref: str = "origin/develop"
) -> int:
    if not _CLIENT_TAG.match(base):
        raise Unverifiable(f"--base {base!r} must be the last client tag, vX.Y.Z")
    _resolve(base, cwd, "base")
    _resolve(head, cwd, "head")
    if not is_ancestor(base, head, cwd):
        sys.stdout.write(
            f"INELIGIBLE: {head} does not descend from {base}\n{FAIL_LINE}\n"
        )
        return 1
    changes = changed_paths(base, head, cwd)
    if not changes:
        sys.stdout.write(
            f"INELIGIBLE: empty diff {base}..{head}, nothing to release\n{FAIL_LINE}\n"
        )
        return 1
    categories: Counter[str] = Counter()
    offenders: list[str] = []
    for change in changes:
        category, reason = classify(change, base, head, cwd)
        categories[category] += 1
        if reason is not None:
            offenders.append(f"INELIGIBLE: {change.path} [{change.status}] {reason}")
    if (bump := version_bump_problem(base, head, cwd)) is not None:
        offenders.append(f"INELIGIBLE: version bump: {bump}")
    straddles, bead_count, develop_commits = straddling_beads(
        base, head, develop_ref, cwd
    )
    for item in straddles:
        offenders.append(
            f"INELIGIBLE: bead {item.bead} straddles the lane: commit "
            f"{item.sha[:10]} on {develop_ref} touches {item.path} ({item.reason}); "
            f"ship the whole bead in a normal release"
        )
    summary = ", ".join(f"{n} {name}" for name, n in sorted(categories.items()))
    sys.stdout.write(
        f"examined {base}..{head}: {len(changes)} changed paths ({summary})\n"
        f"straddle check against {develop_ref}: {bead_count} bead(s) named in the "
        f"commits, {develop_commits} commit(s) of theirs examined\n"
    )
    if offenders:
        sys.stdout.write("\n".join(offenders) + "\n")
        sys.stdout.write(
            f"{FAIL_LINE}: {len(offenders)} problem(s) in {len(changes)} paths\n"
        )
        return 1
    sys.stdout.write(f"{PASS_LINE}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", required=True, help="last client tag, e.g. v7.86.3")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument(
        "--develop-ref",
        default="origin/develop",
        help="where the beads' other commits live (default origin/develop)",
    )
    args = parser.parse_args(argv)
    try:
        return evaluate(args.base, args.head, Path.cwd(), args.develop_ref)
    except Unverifiable as exc:
        sys.stdout.write(f"cannot verify: {exc}\n{UNVERIFIED_LINE}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
