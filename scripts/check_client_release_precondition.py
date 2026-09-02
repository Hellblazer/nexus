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

**Gap 1 fix (protocol-audit [22511], 2026-08-14).** The hand-filled table
above is only HALF the deploy-order precondition. The AUTOMATED detector for
the same bug class -- :mod:`check_wire_contract_pairing` /
``docs/wire-contract-pending.md``, which flags commits touching both the
engine tree and the client wire surface in one changeset -- used to be
consulted ONLY from ``check_engine_release_floor.py``'s ``--paired-deploy``
branch. An ORDINARY unpaired engine deploy (an operator running this script
with no paired client release in flight -- exactly the shape of the
2026-08-13 ``engine-service-v0.1.73`` deploy that caused the 910-doc
manifest-400 outage) read only the empty hand table above and never the
ledger. :func:`check_wire_contract_ledger` closes that: every run of
:func:`check` also asks the ledger whether it has ANY unacknowledged
``## Unshipped`` entry, and blocks (exit 1) if so, with the same named-bead
``--ack-client-lag`` escape :func:`check_engine_release_floor.check_client_lag_ledger`
already offers on the paired-deploy path. Unlike the hand table (keyed to one
specific *engine_tag*), the ledger check is NOT tag-scoped -- a non-empty
``## Unshipped`` section means some client half is missing from every
released version, and an unpaired deploy of ANY engine tag risks carrying
that gap live, so it blocks regardless of which tag is being checked.

Usage::

    uv run python scripts/check_client_release_precondition.py
    uv run python scripts/check_client_release_precondition.py --engine-tag engine-service-v0.1.61
    uv run python scripts/check_client_release_precondition.py --ack-client-lag nexus-1234

Exit codes: ``0`` all preconditions satisfied (or none registered for the
tag, and the wire-contract ledger is clean), ``1`` a required client commit
is missing from the latest released ``v*`` tag, OR the wire-contract ledger
has an unacknowledged ``## Unshipped`` entry, ``2`` git state could not be
interrogated ("could not verify" is never "must be fine").
"""
from __future__ import annotations

import argparse
import subprocess
import sys

import check_wire_contract_pairing as _wire_ledger
import release_choreography as _choreo

# RDR-201 P2.5/P2.6 (nexus-j9z30.15/.16): the decision path. The sensors
# (git, the ledger parse) stay imperative; every branch below that decides
# an exit code resolves docs/tables/release-choreography.toml through the
# SAME scripts/release_choreography.py module check_engine_release_floor.py
# uses -- one table, one cache -- so the two gates cannot disagree about a
# ledger entry again (the O1 class). A DELEGATING branch (check() returning
# check_wire_contract_ledger's own verdict; main() returning check()'s)
# emits nothing itself: the sub-call's row is the decision.

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

_LEDGER_REMEDY = (
    "Remedy: this engine deploy carries an engine-side wire change "
    "(docs/wire-contract-pending.md ## Unshipped) whose client half is not "
    "yet in a published release (nexus-1vogq class -- the 08-13 v0.1.73 "
    "manifest-400 outage shape). Either pair this deploy with the client "
    "release carrying the listed commit(s) (see AGENTS.md § Engine-service "
    "release, paired-release choreography), or pass --ack-client-lag "
    "<bead-id> for each entry below to deploy anyway."
)


def check_wire_contract_ledger(ack_beads: list[str] | None = None) -> tuple[int, bool]:
    """Gap 1 fix: consult the mechanized both-halves wire-contract ledger,
    not only :data:`ENGINE_CLIENT_PRECONDITIONS` above. See the module
    docstring's "Gap 1 fix" section for why the hand table alone left the
    unpaired-deploy path blind to exactly the bug class that caused the
    2026-08-13 outage.

    Not tag-scoped (unlike :func:`check`'s hand-table half): a non-empty
    ``## Unshipped`` section means SOME client half is missing from every
    released version, so it blocks an unpaired deploy of any engine tag,
    mirroring :func:`check_engine_release_floor.check_client_lag_ledger`'s
    unconditional-block-unless-acknowledged shape on the paired-deploy path.

    Returns ``(rc, is_vacuous)``. ``is_vacuous`` is True iff the ledger's
    ``## Unshipped`` section is entirely empty -- kept distinct from "checked
    and found clean" so :func:`check` can print an honest combined message
    when BOTH this and the hand table above verified nothing.
    """
    ledger = _wire_ledger.parse_ledger(_wire_ledger.DEFAULT_LEDGER_PATH)
    if not ledger.unshipped:
        return _choreo.emit_choreography(
            "check_wire_contract_ledger", {"ledger": "empty"},
            {"ledger_path": str(_wire_ledger.DEFAULT_LEDGER_PATH)},
        ), True

    # nexus-1emxn choreography (a), classified by the ONE shared interpreter
    # of the [additive] token (nexus-hcdk3) so this gate and the floor gate
    # cannot return opposite verdicts on the same ledger.
    verdict = _wire_ledger.classify_unshipped(ledger, ack_beads)
    acked = {e.bead for e in verdict.acked}
    missing = list(verdict.blocking) + list(verdict.additive)
    blocking = list(verdict.blocking)
    if blocking:
        entries = "\n".join(
            f"  {e.sha}  bead {e.bead}  engine tag {e.engine_tag}  ({e.note})"
            for e in blocking
        )
        return _choreo.emit_choreography(
            "check_wire_contract_ledger", {"ledger": "blocking"},
            {
                "n": str(len(blocking)), "entries": entries,
                "ledger_path": str(_wire_ledger.DEFAULT_LEDGER_PATH),
            },
        ), False
    if missing:
        acked_count = len(ledger.unshipped) - len(missing)
        acked_suffix = (
            f" ({acked_count} further entr{'y' if acked_count == 1 else 'ies'} "
            "acknowledged via --ack-client-lag)"
            if acked_count else ""
        )
        return _choreo.emit_choreography(
            "check_wire_contract_ledger", {"ledger": "additive"},
            {
                "n": str(len(missing)),
                "beads": ", ".join(sorted({e.bead for e in missing})),
                "acked_suffix": acked_suffix,
            },
        ), False

    acked_beads = ", ".join(sorted(acked & {e.bead for e in ledger.unshipped.values()}))
    return _choreo.emit_choreography(
        "check_wire_contract_ledger", {"ledger": "acked_only"},
        {"n": str(len(ledger.unshipped)), "beads": acked_beads},
    ), False


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


def check(engine_tag: str, ack_client_lag: list[str] | None = None) -> int:
    required = ENGINE_CLIENT_PRECONDITIONS.get(engine_tag, {})
    hand_table_vacuous = not required
    if not required:
        # nexus-f9z84: an empty table is a LEGITIMATE state (most engine
        # tags carry no client-breaking coupling), but it is INDISTINGUISHABLE
        # from "nobody filled the row in" unless the wording says so out
        # loud. Exit 0 IS still correct here -- justification: this script
        # gates the DEPLOY-ORDER precondition specifically (9ssih
        # dangling-endpoint class), not release safety in general; an empty
        # registry means "no KNOWN engine/client coupling for this tag",
        # which is a fact about the registry, not a verified safety
        # property. The engine-release skill's Step 3b is where a human is
        # expected to add a row (or consciously leave none) when cutting the
        # tag -- this script cannot force that decision, only report
        # honestly that it verified NOTHING when the table is empty.
        #
        # Gap 1 fix: the hand table's own vacuity is no longer printed
        # unconditionally here -- the mechanized wire-contract ledger below
        # is a SECOND, independent source, and the combined "verified
        # nothing" claim is only true when BOTH are empty. See the combined
        # message at the bottom of this function.
        pass
    else:
        try:
            release = latest_release_tag()
        except RuntimeError as e:
            return _choreo.emit_choreography(
                "check_composite", {"hand_table": "latest_release_tag_error"}, {"exc": str(e)},
            )
        missing = []
        for commit, why in required.items():
            try:
                ok = is_ancestor(commit, release)
            except RuntimeError as e:
                return _choreo.emit_choreography(
                    "check_composite", {"hand_table": "is_ancestor_error"},
                    {"commit": commit, "exc": str(e)},
                )
            status = "in" if ok else "MISSING FROM"
            print(f"  {commit}  {status} {release}  ({why.splitlines()[0]}...)")
            if not ok:
                missing.append((commit, why))
        if missing:
            return _choreo.emit_choreography(
                "check_composite", {"hand_table": "missing_commit"},
                {
                    "engine_tag": engine_tag, "n": str(len(missing)), "release": release,
                    "commits": "\n".join(f"  {commit}: {why}" for commit, why in missing),
                },
            )
        print(f"OK: all client preconditions for {engine_tag} are in {release}")

    ledger_rc, ledger_vacuous = check_wire_contract_ledger(ack_client_lag)
    if ledger_rc != 0:
        return ledger_rc

    if hand_table_vacuous and ledger_vacuous:
        return _choreo.emit_choreography(
            "check_composite", {"hand_table": "vacuous", "ledger": "empty"},
            {"engine_tag": engine_tag, "ledger_path": str(_wire_ledger.DEFAULT_LEDGER_PATH)},
        )
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--engine-tag",
        default=None,
        help="engine tag whose client preconditions to verify "
        "(default: the pinned REQUIRED_ENGINE_VERSION tag)",
    )
    ap.add_argument(
        "--ack-client-lag",
        action="append",
        default=None,
        metavar="BEAD-ID",
        help="Gap 1 fix. Explicit acknowledgment that a both-halves "
        "commit's client half is not yet in a published release -- names "
        "the OWNING BEAD of a docs/wire-contract-pending.md ## Unshipped "
        "entry. Repeat for each entry. Without this, a non-empty ledger "
        "blocks the deploy regardless of --engine-tag.",
    )
    args = ap.parse_args(argv)
    return check(args.engine_tag or _pinned_engine_tag(), args.ack_client_lag)


if __name__ == "__main__":
    sys.exit(main())
