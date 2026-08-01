#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Blocking engine-deploy gate: are the client commits this engine REQUIRES
already in a RELEASED conexus version? (nexus-9ssih deploy order)

The mirror image of ``check_engine_release_floor.py``. That script stops a
PyPI release from shipping on a stale engine; nothing stopped an ENGINE from
deploying ahead of the client change it depends on. The 9ssih dangling-link
validation is exactly that shape: the engine starts returning a 400 with
``code=dangling_endpoint`` that only clients carrying ``a62649ef``
(``_post_link`` translating it to ``ValueError``) handle gracefully — which
is why the original implementation was REMOVED from the tagged tree by
``6714e70e`` and held until the client half existed. The re-apply
(``1b3962aa``) stated the deploy gate in prose; the 2026-08-01 critique
(T2 [21340]) verified the precondition was UNSATISFIED (``a62649ef`` absent
from every released tag) with nothing mechanized to notice. This script is
the mechanization: prose deploy-gates get skipped, exit codes do not.

Add a row to :data:`ENGINE_CLIENT_PRECONDITIONS` whenever an engine change
breaks clients that predate a specific client commit. Delete rows once the
floor moves past the engine version that carried the requirement.

Usage::

    uv run python scripts/check_client_release_precondition.py
    uv run python scripts/check_client_release_precondition.py --engine-tag engine-service-v0.1.61

Exit codes: ``0`` all preconditions satisfied (or none registered for the
tag), ``1`` a required client commit is missing from the latest released
``v*`` tag, ``2`` git state could not be interrogated ("could not verify" is
never "must be fine").
"""
from __future__ import annotations

import argparse
import subprocess
import sys

#: engine tag (or the literal "next" for the tag about to be cut) -> the
#: client commits that must be in a RELEASED conexus version before that
#: engine may DEPLOY. Commits, not branches: a branch can move, a commit
#: either is or is not an ancestor of the release tag.
ENGINE_CLIENT_PRECONDITIONS: dict[str, dict[str, str]] = {
    "engine-service-v0.1.61": {
        "a62649ef": "nexus-9ssih client half: _post_link translates the "
        "engine's 400 code=dangling_endpoint into ValueError; older "
        "clients surface it as an unhandled HTTP error on every "
        "auto-link against a missing endpoint",
    },
}

_REMEDY = (
    "Remedy: cut the conexus PyPI release carrying the listed commit(s) "
    "(release skill; AGENTS.md § Cutting a release) BEFORE tagging/deploying "
    "this engine. Then re-run this check."
)


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def latest_release_tag() -> str:
    """The most recent conexus release tag (vX.Y.Z, not engine-service-*)."""
    tags = _git(
        "tag", "-l", "v[0-9]*", "--sort=-v:refname"
    ).splitlines()
    if not tags:
        raise RuntimeError("no v* release tags found")
    return tags[0]


def is_ancestor(commit: str, tag: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, tag],
        capture_output=True, text=True, timeout=60,
    )
    if proc.returncode in (0, 1):
        return proc.returncode == 0
    raise RuntimeError(
        f"git merge-base --is-ancestor {commit} {tag}: {proc.stderr.strip()}"
    )


def check(engine_tag: str) -> int:
    required = ENGINE_CLIENT_PRECONDITIONS.get(engine_tag, {})
    if not required:
        print(f"OK: no client-release preconditions registered for {engine_tag}")
        return 0
    try:
        release = latest_release_tag()
    except RuntimeError as e:
        print(f"CANNOT VERIFY: {e}", file=sys.stderr)
        return 2
    missing = []
    for commit, why in required.items():
        try:
            ok = is_ancestor(commit, release)
        except RuntimeError as e:
            print(f"CANNOT VERIFY {commit}: {e}", file=sys.stderr)
            return 2
        status = "in" if ok else "MISSING FROM"
        print(f"  {commit}  {status} {release}  ({why.splitlines()[0]}...)")
        if not ok:
            missing.append((commit, why))
    if missing:
        print(
            f"\nBLOCKED: {engine_tag} must not deploy — "
            f"{len(missing)} required client commit(s) absent from the "
            f"latest release {release}:",
            file=sys.stderr,
        )
        for commit, why in missing:
            print(f"  {commit}: {why}", file=sys.stderr)
        print(f"\n{_REMEDY}", file=sys.stderr)
        return 1
    print(f"OK: all client preconditions for {engine_tag} are in {release}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine-tag",
        default="engine-service-v0.1.61",
        help="engine tag whose client preconditions to verify",
    )
    args = ap.parse_args()
    return check(args.engine_tag)


if __name__ == "__main__":
    sys.exit(main())
