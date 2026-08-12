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
#:
#: Rows exist only for engines AHEAD of ``REQUIRED_ENGINE_VERSION`` —
#: once the floor reaches a row's engine, the deploy it gated has happened
#: and every subsequent release supersets the required client commits, so
#: the row is dead weight (mechanized:
#: ``TestStalePreconditionRowsDoNotOutliveTheFloor``). Pruned rows, for
#: the historical record:
#:   engine-service-v0.1.61 / a62649ef (nexus-9ssih dangling-endpoint 400)
#:   engine-service-v0.1.62 / 9ba82a3b (nexus-lcmbp 409 conflict_running)
ENGINE_CLIENT_PRECONDITIONS: dict[str, dict[str, str]] = {}

_REMEDY = (
    "Remedy: this blocks the DEPLOY only — the engine tag cuts whenever the "
    "tree is green (a tag gates delivery, not work; Hal directive "
    "2026-08-02). Pair the deploy with the conexus release that carries the "
    "listed commit(s) AND bumps the floor to this tag: deploy fires at "
    "client-tag push, in parallel with the PyPI publish (AGENTS.md "
    "§ Engine-service release, paired-release choreography). Then re-run "
    "this check."
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
        # nexus-f9z84: an empty table is a LEGITIMATE state (most engine
        # tags carry no client-breaking coupling), but it is INDISTINGUISHABLE
        # from "nobody filled the row in" unless the wording says so out
        # loud. This prints "OK" because exit 0 IS still correct here --
        # justification: this script gates the DEPLOY-ORDER precondition
        # specifically (9ssih dangling-endpoint class), not release safety in
        # general; an empty registry means "no KNOWN engine/client coupling
        # for this tag", which is a fact about the registry, not a verified
        # safety property. The engine-release skill's Step 3b is where a
        # human is expected to add a row (or consciously leave none) when
        # cutting the tag -- this script cannot force that decision, only
        # report honestly that it verified NOTHING when the table is empty.
        print(
            f"OK (VACUOUS -- 0 preconditions registered for {engine_tag}): "
            "this run verified NOTHING. An empty registry means either "
            "'no known client coupling for this engine tag' or 'nobody "
            "added the row' -- this script cannot tell the two apart. "
            "See ENGINE_CLIENT_PRECONDITIONS's module docstring before "
            "treating this as evidence the deploy is safe."
        )
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


def stale_precondition_rows(
    table: dict[str, dict[str, str]] | None = None,
    floor: tuple[int, ...] | None = None,
) -> list[str]:
    """Rows at or behind the floor -- dead weight per the module's own
    contract ("Delete rows once the floor moves past the engine version that
    carried the requirement").

    nexus-f9z84 non-vacuity companion: extracted to a pure function that
    accepts an INJECTABLE table/floor rather than closing over the live
    (almost always empty) :data:`ENGINE_CLIENT_PRECONDITIONS` directly. The
    original inline loop in the test suite iterated the real, empty dict --
    zero iterations, unconditional pass, no signal that the comparison logic
    ever actually ran. A test can now plant a deliberately stale row and
    observe this function actually return it, which the old shape could
    never demonstrate.
    """
    from nexus.engine_version import REQUIRED_ENGINE_VERSION

    rows = ENGINE_CLIENT_PRECONDITIONS if table is None else table
    active_floor = REQUIRED_ENGINE_VERSION if floor is None else floor
    stale: list[str] = []
    for tag in rows:
        if tag == "next":  # the about-to-be-cut sentinel is always ahead
            continue
        version = tuple(int(n) for n in tag.removeprefix("engine-service-v").split("."))
        if version <= active_floor:
            stale.append(tag)
    return stale


def _pinned_engine_tag() -> str:
    """Default to the pinned engine identity — derived, so it cannot rot."""
    from nexus.engine_version import REQUIRED_ENGINE_VERSION

    return "engine-service-v" + ".".join(str(n) for n in REQUIRED_ENGINE_VERSION)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine-tag",
        default=None,
        help="engine tag whose client preconditions to verify "
        "(default: the pinned REQUIRED_ENGINE_VERSION tag)",
    )
    args = ap.parse_args()
    return check(args.engine_tag or _pinned_engine_tag())


if __name__ == "__main__":
    sys.exit(main())
