#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Mechanical eligibility gate for the plugin-only fast lane (nexus-rbmja.1).

Design of record: T2 nexus/design-plugin-only-fast-lane.md, "Eligibility".
A patch release off the last client tag may skip the heavy battery legs
only when its diff from that tag touches nothing the nx code under test
is built from.

    uv run python scripts/check_plugin_only_eligibility.py --base vX.Y.Z [--head HEAD]

Diffs ``base..head`` (name-status, renames off, so a move out of ``src/``
shows the ``src/`` deletion). ELIGIBLE (exit 0, last line
``PLUGIN-ONLY ELIGIBILITY PASSED``) only if every changed path is one of:

  a. a channel path per :func:`plugin_channel.is_channel_path` (imported,
     never copied);
  b. under ``tests/`` or ``docs/``;
  c. ``CHANGELOG.md`` or ``conexus/PENDING_RELEASE.md``;
  d. a version surface whose CONTENT change is the version and nothing
     else: ``pyproject.toml`` (the ``[project]`` version line),
     ``mcpb/pyproject.toml`` (that line and the ``conexus>=`` pin),
     ``mcpb/manifest.json`` (the top-level ``version``), ``uv.lock``
     (the ``conexus`` package stanza's ``version`` line).

Everything else is INELIGIBLE (exit 1, each offender with its reason):
``src/`` (the engine pin lives there), ``service/``, any other line of the
four surfaces, ``scripts/``, ``.github/``, the rest of ``mcpb/``.

Could-not-check is never a pass: an unresolvable ``--base`` or ``--head``
(a blind checkout, a non-repo) exits 2 with ``PLUGIN-ONLY ELIGIBILITY
UNVERIFIED``. An EMPTY diff exits 1: there is nothing to release, and a
vacuous pass would let an empty cut through.
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

_VERSION_VALUE = r'"[0-9][^"]*"'
_VERSION_LINE = re.compile(rf"^version\s*=\s*{_VERSION_VALUE}\s*$")
_CONEXUS_PIN = re.compile(rf'^(\s*)"conexus>=[0-9][^"]*"(,?)\s*$')
_TABLE_HEADER = re.compile(r"^\s*\[+\s*([^\]]*?)\s*\]+\s*$")
_NAME_LINE = re.compile(r'^name\s*=\s*"([^"]*)"\s*$')

_SENTINEL = "<VERSION>"


class Unverifiable(RuntimeError):
    """The check could not be performed; never a pass."""


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


def changed_paths(base: str, head: str, cwd: Path) -> list[tuple[str, str]]:
    """(status, path) for every path differing between *base* and *head*."""
    out = _git(["diff", "--name-status", "--no-renames", "-z", base, head], cwd)
    if out.returncode != 0:
        raise Unverifiable(
            f"git diff {base} {head} failed (rc {out.returncode}): "
            f"{out.stderr.decode(errors='replace').strip()}"
        )
    fields = out.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2:
        raise Unverifiable("unparsable git diff --name-status output")
    return [(fields[i], fields[i + 1]) for i in range(0, len(fields), 2)]


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


# ---- classification ----------------------------------------------------------


def classify(
    status: str, path: str, base: str, head: str, cwd: Path
) -> tuple[str, str | None]:
    """(category, reason). reason is None when the path is eligible."""
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
    if is_channel_path(path):
        return "channel", None
    if any(path_has_prefix(path, p) for p in FREE_PREFIXES):
        return "tests-docs", None
    if path in FREE_EXACT:
        return "changelog", None
    return "other", "outside the plugin channel, tests/, docs/ and the version surfaces"


def evaluate(base: str, head: str, cwd: Path) -> int:
    _resolve(base, cwd, "base")
    _resolve(head, cwd, "head")
    changes = changed_paths(base, head, cwd)
    if not changes:
        sys.stdout.write(
            f"INELIGIBLE: empty diff {base}..{head}, nothing to release\n{FAIL_LINE}\n"
        )
        return 1
    categories: Counter[str] = Counter()
    offenders: list[str] = []
    for status, path in changes:
        category, reason = classify(status, path, base, head, cwd)
        categories[category] += 1
        if reason is not None:
            offenders.append(f"INELIGIBLE: {path} [{status}] {reason}")
    summary = ", ".join(f"{n} {name}" for name, n in sorted(categories.items()))
    sys.stdout.write(
        f"examined {base}..{head}: {len(changes)} changed paths ({summary})\n"
    )
    if offenders:
        sys.stdout.write("\n".join(offenders) + "\n")
        sys.stdout.write(f"{FAIL_LINE}: {len(offenders)} of {len(changes)} paths\n")
        return 1
    sys.stdout.write(f"{PASS_LINE}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", required=True, help="last client tag, e.g. v7.86.3")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args(argv)
    try:
        return evaluate(args.base, args.head, Path.cwd())
    except Unverifiable as exc:
        sys.stdout.write(f"cannot verify: {exc}\n{UNVERIFIED_LINE}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
