#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The engine release's wire-pairing check (RDR-258 Phase 2 step 1, nexus-2jomg.3).

``scripts/check_wire_contract_pairing.py`` scans ``<newest v* tag>..HEAD`` by default. That
follows the CLIENT's tags, and ``v[0-9]*`` does not match ``engine-service-v*``, so an engine
tag cut from develop was never scanned for an undeclared both-halves change. This script gives
the engine release its own range::

    <previous engine-service-v* tag>..<the commit being tagged>

and hands it to the pairing check's ``--rev-range``, so the ledger, the detector and the exit
codes are the pairing check's own (0 clean, 1 undeclared or stale entries, 2 could not verify).

The previous tag is the highest version BELOW the tag being cut, by numeric version and not by
date or position: a rerun for a tag that already exists, and a tag cut for an older commit
while a newer one exists, both find the same base. Tags that are not exactly
``engine-service-vX.Y.Z`` cannot be ordered and are ignored.

Fails closed (exit 2) where it cannot name the range: the tag is not ``engine-service-vX.Y.Z``,
the commit does not resolve, the checkout is shallow (a shallow boundary reads as a root commit,
so a scan would pass over history it never saw), or no lower engine tag is visible (a checkout
without tags, which this repository's 200 tags rule out as a real first release).

Usage::

    uv run python scripts/check_engine_release_pairing.py --tag engine-service-v0.1.205
    uv run python scripts/check_engine_release_pairing.py --tag engine-service-vX.Y.Z --commit <TAG_SHA>

``--commit`` defaults to what the tag points at; give it before the tag exists (the pre-tag run
on the frozen ``TAG_SHA``).
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import check_wire_contract_pairing as _pairing

_TAG_RE = re.compile(r"^engine-service-v(\d+)\.(\d+)\.(\d+)$")

Version = tuple[int, int, int]


def _version(tag: str) -> Version | None:
    m = _TAG_RE.match(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


class CannotVerify(Exception):
    """The range cannot be named. Never read as clean."""


def previous_engine_tag(tag: str, repo_root: pathlib.Path | None = None) -> str:
    """The highest ``engine-service-vX.Y.Z`` tag below ``tag``'s version.

    Raises :class:`CannotVerify` when ``tag`` is malformed, git cannot answer, or no lower tag is
    visible.
    """
    this = _version(tag)
    if this is None:
        raise CannotVerify(f"{tag!r} is not an engine-service-vX.Y.Z tag")
    proc = _pairing._git("tag", "-l", "engine-service-v*", repo_root=repo_root)
    if proc.returncode != 0:
        raise CannotVerify(f"git tag -l failed: {proc.stderr.strip()}")
    lower = [(v, t) for t in proc.stdout.split() if (v := _version(t)) is not None and v < this]
    if not lower:
        raise CannotVerify(
            f"no engine-service-v* tag below {tag} is visible. The checkout has no tags "
            "(CI: fetch-depth: 0), or this is the first engine release"
        )
    return max(lower)[1]


def _is_shallow(repo_root: pathlib.Path | None) -> bool:
    proc = _pairing._git("rev-parse", "--is-shallow-repository", repo_root=repo_root)
    if proc.returncode != 0:
        raise CannotVerify(f"git rev-parse --is-shallow-repository failed: {proc.stderr.strip()}")
    return proc.stdout.strip() == "true"


def _resolve_commit(tag: str, commit: str | None, repo_root: pathlib.Path | None) -> str:
    spec = commit if commit else f"refs/tags/{tag}"
    proc = _pairing._git("rev-parse", "--verify", f"{spec}^{{commit}}", repo_root=repo_root)
    if proc.returncode != 0:
        raise CannotVerify(
            f"cannot resolve {spec!r} to a commit: {proc.stderr.strip()}. "
            "Before the tag exists, pass --commit <the commit you will tag>"
        )
    return proc.stdout.strip()


def engine_release_range(
    tag: str, commit: str | None = None, repo_root: pathlib.Path | None = None
) -> str:
    """``<previous engine tag>..<commit being tagged>``, or raise :class:`CannotVerify`."""
    if _is_shallow(repo_root):
        raise CannotVerify(
            "the checkout is shallow, so the range would silently stop at the shallow boundary "
            "(CI: fetch-depth: 0)"
        )
    return f"{previous_engine_tag(tag, repo_root)}..{_resolve_commit(tag, commit, repo_root)}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="The engine tag being cut: engine-service-vX.Y.Z.")
    parser.add_argument("--commit", default=None, help="The commit being tagged (default: what --tag points at).")
    parser.add_argument("--ledger", type=pathlib.Path, default=None, help="Ledger path (default: the pairing check's).")
    parser.add_argument(
        "--repo-root", type=pathlib.Path, default=None, help="Repository to interrogate (default: this script's repository)."
    )
    args = parser.parse_args(argv)
    try:
        rev_range = engine_release_range(args.tag, args.commit, args.repo_root)
    except CannotVerify as exc:
        print(f"ENGINE-RELEASE WIRE-PAIRING CHECK FAILED: {exc}. Cannot verify: a failed gate, not a pass.", file=sys.stderr)
        return 2
    print(f"engine-release wire pairing: {args.tag} scans {rev_range}")
    return _pairing.check(rev_range=rev_range, ledger_path=args.ledger, repo_root=args.repo_root)


if __name__ == "__main__":
    sys.exit(main())
