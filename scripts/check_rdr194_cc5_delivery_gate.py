#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Pre-tag release gate: RDR-194 D4's cloud-count-5 delivery precondition
(nexus-tk070.p5a substantive-critic CRITICAL, T2 [22965]).

Root cause this closes: ``taxonomy-014-topics-tenant-unique.xml``
(nexus-tk070.p5a) repoints four tenant-blind FKs onto tenant-scoped
composites. The RDR's own D0 rule 4 ("each count is recorded in T2 before
its phase ships. A phase whose count is unavailable does not ship") and the
file's own header both say cloud-count-5 must be recorded ZERO in T2 before
any engine tag carrying this file is cut for cloud deployment — but until
this script, that requirement lived ONLY in prose (the file header, the RDR
text, a P7 human checklist item). Nothing MECHANICAL enforced it. This is
exactly the failure shape nexus-i5c2u already burned the project on once:
an eyeball pre-tag check ("is the drift non-trivial and cloud-relevant?")
was silently skipped in practice, and the cloud engine sat 9+ days stale
across multiple releases before ``check_engine_release_floor.py`` replaced
the eyeball with a mechanical probe. This script is that same replacement
for cloud-count-5: mold and fail-closed doctrine copied from
``check_engine_release_floor.py`` (this repo, same directory).

## What the gate checks

Exits 0 iff EITHER:

  (a) ``taxonomy-014-topics-tenant-unique.xml`` (:data:`FK_FILE`) is ABSENT
      from the tree at ``--ref`` (default ``HEAD``) — nothing to gate,
      cloud-count-5 is not this tag's problem; or
  (b) a T2 record proving cloud-count-5 was MEASURED zero exists — see
      "Record contract" below.

Otherwise the gate is RED (exit 1), with a message naming the bead
(nexus-tk070.cc5, the measurement; nexus-tk070.p7, the phase that must
verify it) and the remedy. ``nx`` being unreachable is UNVERIFIABLE (exit
2), never a silent pass — "could not check" is never "must be fine",
identical doctrine to every fail-closed gate in this directory.

## Record contract (deliberately strict — the measured-vs-vacuous gap)

Substantive-critic's own dev-notes for this phase already wrote a T2 entry
STATING cloud-count-5 is unmeasured/blocked ("every conexus-reachable role
is NOBYPASSRLS ... cc5 is still unrecorded/blocked, no BYPASSRLS path") —
exactly the shape of record that must NOT satisfy this gate. A record whose
TITLE resolves is not proof of anything; only its CONTENT is. So the gate
requires the T2 entry at (``CC5_PROJECT``, ``CC5_TITLE``) to carry, in its
body:

  1. a STRUCTURED status line, exactly ``STATUS: MEASURED`` on its own
     line (round-2 hardening, critic false-accept (i), T2 [22965]: a bare
     word-boundary search for ``MEASURED`` anywhere in the text accepted
     "cloud-count-5 is **NOT MEASURED** yet" whenever that same prose also
     happened to quote the record's own template — e.g. an operator
     copy-pasting this docstring's example while explaining cc5 is STILL
     unmeasured. Anchoring to a dedicated, unambiguous status line closes
     that gap: prose describing the target shape no longer collides with
     an actual assertion of it);
  2. NO occurrence, anywhere in the text, of a negative/blocked status
     marker — ``NOT MEASURED``, ``UNMEASURED``, or ``BLOCKED``
     (case-insensitive) — checked UNCONDITIONALLY, independent of (1): a
     record could in principle carry both a genuine ``STATUS: MEASURED``
     line AND leftover negative prose from an earlier draft, and this
     rejects that combination outright rather than trusting whichever
     signal happens to be checked last;
  3. all three named sub-population counts from the RDR's own cloud-count-5
     definition (RDR-194 Gate-preconditions table: "topic_assignments rows
     whose topic_id belongs to another tenant; same for topic_links and
     topics.parent_id"), each spelled EXACTLY as one of the
     :data:`_REQUIRED_COUNT_KEYS`, each with an explicit value of ``0`` --
     and, if a key is spelled more than once anywhere in the text (round-2
     hardening, critic false-accept (ii)), EVERY occurrence must agree: a
     stale ``key=0`` left in place alongside a corrected ``key=3`` (in
     either order) is a CONFLICT, rejected outright, never resolved by
     silently preferring the first or the last match.

Missing ANY of the three required keys, a nonzero or internally-conflicting
value on any of them, a missing ``STATUS: MEASURED`` line, or the presence
of any negative-status marker — ANY of these — means the record is REJECTED
as not-yet-measured, the identical exit 1 as a wholly absent record (with a
message distinguishing "no record at all" from "a record exists but does
not carry a valid MEASURED-zero reading", so an operator knows whether to
CREATE the record or CORRECT it). This is the mechanical form of the
"measured vs vacuous" distinction — a gate that accepted any record merely
because a title exists, or because the word MEASURED appears anywhere in
its prose, would have passed the exact already-written vacuous entry above.

## Writing the record (the human/operator side, once cc5 is genuinely
measured zero — needs BYPASSRLS/superuser, see nexus-tk070.cc5)

    nx memory put -p nexus -t cloud-count-5-measured --ttl 0 <<'EOF'
    STATUS: MEASURED
    cloud-count-5 (RDR-194 D4, nexus-tk070.cc5): <ISO8601 timestamp>
    topic_assignments_cross_tenant=0
    topic_links_cross_tenant=0
    topics_parent_cross_tenant=0
    EOF

(``--ttl 0`` / "permanent" — this fact must not silently expire and let the
gate revert to red for no operational reason; confirm the CLI's permanent-TTL
flag at write time, this script does not police TTL.)

Usage::

    uv run python scripts/check_rdr194_cc5_delivery_gate.py
    uv run python scripts/check_rdr194_cc5_delivery_gate.py --ref v7.12.0
    uv run python scripts/check_rdr194_cc5_delivery_gate.py --repo-root /path/to/nexus

Exit codes: ``0`` clean (file absent from the ref, or a valid MEASURED-zero
record exists), ``1`` blocked (file present, no valid record — remedy named),
``2`` unverifiable (``nx`` or ``git`` could not be consulted — never a pass).
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

#: The file this gate exists for. Relative to repo root, forward-slash form
#: (matches every ``git cat-file -e <ref>:<path>`` idiom in this directory).
FK_FILE = "service/src/main/resources/db/changelog/taxonomy-014-topics-tenant-unique.xml"

#: T2 record identity the gate greps for (see module docstring, "Record
#: contract"). A caller may override for testing via --project/--title;
#: production callers should never need to.
CC5_PROJECT = "nexus"
CC5_TITLE = "cloud-count-5-measured"

#: Sentinel for "git could not resolve the ref / consult the tree at all".
#: Distinct from False ("git resolved cleanly; the path does not exist at
#: that ref") -- an unresolvable ref is UNVERIFIABLE, never "file absent".
_GIT_UNAVAILABLE = object()

#: Sentinel for "nx could not be consulted at all" (binary missing, timeout,
#: or an exit code that is neither 0 (found) nor the specific "not found"
#: shape). Distinct from None ("nx ran fine and reported no such entry") --
#: an unreachable nx is UNVERIFIABLE, never "record absent".
_NX_UNAVAILABLE = object()

#: The three sub-populations RDR-194's own cloud-count-5 definition names
#: (Gate-preconditions table: "topic_assignments rows whose topic_id
#: belongs to another tenant; same for topic_links and topics.parent_id").
#: Order is the order stated in the RDR; the parser does not care about
#: order, only that all three keys are present with value 0.
_REQUIRED_COUNT_KEYS = (
    "topic_assignments_cross_tenant",
    "topic_links_cross_tenant",
    "topics_parent_cross_tenant",
)

#: The ONLY shape that asserts "this record is a genuine measurement", round-2
#: hardening (critic false-accept (i), T2 [22965]): a bare word-boundary
#: search for ``MEASURED`` anywhere in the text (the pre-hardening approach)
#: accepted prose like "cc5 is NOT MEASURED yet -- once measured, record it
#: like: MEASURED cloud-count-5: ..." whenever that same prose also quoted
#: the record's own template -- the word appeared, just not as an assertion.
#: Anchoring to a dedicated, whole-line status marker closes that gap:
#: multiline mode, the line must contain ONLY "STATUS: MEASURED" (whitespace
#: around the colon tolerated, trailing whitespace tolerated), never merely
#: a substring of a longer sentence.
_STATUS_MEASURED_RE = re.compile(r"(?m)^[ \t]*STATUS[ \t]*:[ \t]*MEASURED[ \t]*$")

#: Negative/blocked status markers, checked UNCONDITIONALLY and
#: independently of :data:`_STATUS_MEASURED_RE` (round-2 hardening, critic
#: false-accept (i)): a record carrying ANY of these anywhere is rejected
#: outright, even if it ALSO carries a genuine STATUS: MEASURED line
#: elsewhere (e.g. leftover prose from an earlier, unmeasured draft that
#: was edited in place rather than replaced). Case-insensitive; "unmeasured"
#: is matched as one word so "not measured" (two words) needs its own
#: alternative, which the pattern provides.
_NEGATIVE_STATUS_RE = re.compile(r"(?i)\b(NOT\s+MEASURED|UNMEASURED|BLOCKED)\b")

#: ``key=0`` / ``key = 0`` / ``key: 0`` -- one regex per required key,
#: built at import time so a typo in _REQUIRED_COUNT_KEYS surfaces as an
#: obvious KeyError-shaped failure rather than a silently-never-matching
#: pattern. Matched with ``finditer`` (ALL occurrences, not just the
#: first -- round-2 hardening, critic false-accept (ii): a first-match-wins
#: read of "key=0 ... key=3" silently accepted the stale 0 and never even
#: saw the real, conflicting 3).
_COUNT_LINE_RE = {
    key: re.compile(rf"\b{re.escape(key)}\b\s*[:=]\s*(-?\d+)")
    for key in _REQUIRED_COUNT_KEYS
}

_REMEDY = (
    "Remedy: measure cloud-count-5 (nexus-tk070.cc5 -- requires BYPASSRLS/"
    "superuser access, currently blocked: every conexus-reachable role is "
    "NOBYPASSRLS), then record it via:\n"
    "    nx memory put -p nexus -t cloud-count-5-measured --ttl 0 <<'EOF'\n"
    "    STATUS: MEASURED\n"
    "    cloud-count-5 (RDR-194 D4, nexus-tk070.cc5): <ISO8601 timestamp>\n"
    "    topic_assignments_cross_tenant=0\n"
    "    topic_links_cross_tenant=0\n"
    "    topics_parent_cross_tenant=0\n"
    "    EOF\n"
    "A nonzero measurement is DATA CORRUPTION, not a value to force through "
    "this gate -- quarantine the offending rows and re-run taxonomy "
    "assignment for the affected tenant(s) (RDR-194 D1/D4) before "
    "re-measuring. See nexus-tk070.p7 for the close-out verification step "
    "this gate mechanizes."
)


# ---------------------------------------------------------------------------
# Pure logic (unit-tested directly, no subprocess) -- record validation
# ---------------------------------------------------------------------------


def validate_measured_record(text: str) -> tuple[bool, list[str]]:
    """Does *text* satisfy the "measured-zero" record contract?

    Returns ``(True, [])`` when the record carries a dedicated
    ``STATUS: MEASURED`` line, no negative/blocked status marker anywhere,
    AND all three :data:`_REQUIRED_COUNT_KEYS`, each spelled with a SINGLE,
    consistent value of ``0`` (multiple occurrences of the same key are
    allowed only if they all agree). Otherwise returns ``(False, problems)``
    where *problems* names EVERY thing wrong (not just the first) -- missing
    keys, nonzero values, conflicting values, a missing status line, and a
    negative-status marker are all independently reported so a correcting
    edit does not have to re-run this script per fix.

    Round-2 hardening (critic false-accepts (i)/(ii), T2 [22965]): see
    :data:`_STATUS_MEASURED_RE`, :data:`_NEGATIVE_STATUS_RE`, and this
    function's own conflicting-count handling below for what changed and
    why -- a bare substring search for "MEASURED" and a first-match-wins
    count read both silently accepted records that should have been
    rejected.
    """
    problems: list[str] = []

    # (i) Status assertion: BOTH checks run unconditionally, independent of
    # each other -- a record can fail one, the other, or both, and every
    # failure is reported.
    if not _STATUS_MEASURED_RE.search(text):
        problems.append(
            "no 'STATUS: MEASURED' line found -- record does not assert an "
            "actual measurement (a template/example quoting the word "
            "MEASURED in surrounding prose does not count; the record must "
            "carry the status as its own dedicated line)"
        )
    negative_hits = sorted({m.group(1).upper() for m in _NEGATIVE_STATUS_RE.finditer(text)})
    if negative_hits:
        problems.append(
            f"record contains a negative/blocked status marker ({', '.join(negative_hits)}) "
            "-- rejected outright, even if a STATUS: MEASURED line is also present elsewhere"
        )

    # (ii) Counts: collect EVERY occurrence per key, not just the first.
    for key in _REQUIRED_COUNT_KEYS:
        matches = _COUNT_LINE_RE[key].findall(text)
        if not matches:
            problems.append(f"missing required count '{key}=0'")
            continue
        parsed: list[int] = []
        unparseable: list[str] = []
        for raw in matches:
            try:
                parsed.append(int(raw))
            except ValueError:
                unparseable.append(raw)
        if unparseable:
            problems.append(f"'{key}' has a non-integer value {unparseable[0]!r}")
            continue
        distinct = sorted(set(parsed))
        if len(distinct) > 1:
            problems.append(
                f"'{key}' has CONFLICTING values in the same record ({distinct}) -- "
                "never silently resolved by preferring the first or last occurrence; "
                "remove the stale line(s) so exactly one value remains"
            )
            continue
        value = distinct[0]
        if value != 0:
            problems.append(f"'{key}' is {value}, not 0 -- a nonzero count is corruption, not a pass")
    return (not problems, problems)


# ---------------------------------------------------------------------------
# IO boundary (subprocess wrappers) -- monkeypatched in tests, not
# unit-tested directly (mirrors check_inbound_relay_acks.py's split)
# ---------------------------------------------------------------------------


def file_present_at_ref(
    ref: str, path: str = FK_FILE, repo_root: pathlib.Path | None = None
) -> object:
    """``True``/``False``, or :data:`_GIT_UNAVAILABLE` if git could not
    resolve *ref* or consult the tree at all.

    Two-step, deliberately: ``git cat-file -e <ref>:<path>`` alone cannot
    distinguish "the ref itself does not resolve" from "the ref resolves
    fine but this path does not exist in it" -- BOTH exit 128 with a
    ``fatal: ...`` message (verified directly, 2026-08-20: an unresolvable
    ref and a resolvable ref with a missing path give the IDENTICAL exit
    code from this one command). Those two cases need different answers
    here (unverifiable vs. "nothing to gate"), unlike
    ``check_remediation_commits_ride_release.py``'s
    ``is_path_committed_at_ref``, whose caller treats every non-zero exit
    as the same "not proven" outcome. So *ref* is verified to resolve
    FIRST (``git rev-parse --verify``); only once that succeeds does a
    non-zero ``cat-file -e`` mean "genuinely absent" rather than
    "unverifiable"."""
    root = repo_root or pathlib.Path(__file__).resolve().parent.parent
    try:
        resolved = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _GIT_UNAVAILABLE
    if resolved.returncode != 0:
        return _GIT_UNAVAILABLE
    try:
        out = subprocess.run(
            ["git", "cat-file", "-e", f"{ref}:{path}"],
            cwd=root, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _GIT_UNAVAILABLE
    return out.returncode == 0


def fetch_cc5_record(project: str = CC5_PROJECT, title: str = CC5_TITLE) -> object:
    """The T2 record's raw body via the ``nx`` CLI, or ``None`` if no such
    entry exists, or :data:`_NX_UNAVAILABLE` if ``nx`` could not be
    consulted at all.

    ``nx memory get -p <project> -t <title>`` prints the entry's raw content
    to stdout on success (exit 0); on a missing entry it exits 1 with
    ``Error: entry not found`` on stderr (verified against the live CLI,
    2026-08-20) -- that specific shape is "record absent", every OTHER
    nonzero exit (auth failure, a T2 substrate that is down, a changed CLI
    error shape) is UNVERIFIABLE, never silently folded into "absent".
    """
    try:
        out = subprocess.run(
            ["nx", "memory", "get", "-p", project, "-t", title],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _NX_UNAVAILABLE
    if out.returncode == 0:
        return out.stdout
    if out.returncode == 1 and "not found" in out.stderr.lower():
        return None
    return _NX_UNAVAILABLE


# ---------------------------------------------------------------------------
# Decision logic -- pure over already-fetched IO results, unit-tested
# directly with injected values (no subprocess, no monkeypatch needed here)
# ---------------------------------------------------------------------------


def run_gate(file_present: object, record_text: object) -> int:
    """Combine the two IO results into the gate's exit code.

    ``file_present``: ``True``/``False``/:data:`_GIT_UNAVAILABLE`.
    ``record_text``: ``str``/``None``/:data:`_NX_UNAVAILABLE`.

    Order matters: an absent file is checked FIRST and short-circuits --
    nothing to gate means nx never needs to be consulted at all, so a tag
    with no D4 changeset in it is never blocked by an nx outage (the gate
    must not become a tax on every unrelated release).
    """
    if file_present is _GIT_UNAVAILABLE:
        print(
            f"RDR-194 CC5 DELIVERY GATE UNVERIFIABLE: could not resolve whether "
            f"{FK_FILE} exists at the given ref -- git could not be consulted. "
            "Cannot determine whether this gate even applies to this tag; treat "
            "as a failed gate, not a pass.",
            file=sys.stderr,
        )
        return 2
    if file_present is False:
        print(
            f"RDR-194 CC5 delivery gate: {FK_FILE} is not present at this ref -- "
            "nothing to gate, cloud-count-5 is not this tag's problem."
        )
        return 0

    # file_present is True from here on.
    if record_text is _NX_UNAVAILABLE:
        print(
            f"RDR-194 CC5 DELIVERY GATE UNVERIFIABLE: {FK_FILE} is present at this "
            f"ref, but `nx memory get -p {CC5_PROJECT} -t {CC5_TITLE}` could not be "
            "consulted (nx missing, timeout, or an unrecognized error shape). "
            "Cannot verify cloud-count-5 was measured -- treat as a failed gate, "
            f"not a pass.\n{_REMEDY}",
            file=sys.stderr,
        )
        return 2
    if record_text is None:
        print(
            f"RDR-194 CC5 DELIVERY GATE BLOCKED: {FK_FILE} is present at this ref, "
            f"but no T2 record exists at ({CC5_PROJECT}, {CC5_TITLE}). RDR-194 D0 "
            "rule 4: \"each count is recorded in T2 before its phase ships. A "
            f"phase whose count is unavailable does not ship.\"\n{_REMEDY}",
            file=sys.stderr,
        )
        return 1

    ok, problems = validate_measured_record(record_text)
    if not ok:
        print(
            f"RDR-194 CC5 DELIVERY GATE BLOCKED: {FK_FILE} is present at this ref, "
            f"and a T2 record exists at ({CC5_PROJECT}, {CC5_TITLE}), but it does "
            "NOT carry a valid MEASURED-zero reading -- a record merely existing "
            "is not proof of anything (this is the exact vacuous-record gap the "
            "gate exists to close). Problems found:\n"
            + "\n".join(f"  - {p}" for p in problems)
            + f"\n{_REMEDY}",
            file=sys.stderr,
        )
        return 1

    print(
        f"RDR-194 CC5 delivery gate: {FK_FILE} is present at this ref, and "
        f"({CC5_PROJECT}, {CC5_TITLE}) carries a valid MEASURED-zero cloud-count-5 "
        "record -- clear to tag."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--ref", default="HEAD", metavar="REF",
        help="Tree ref to check for the presence of taxonomy-014-topics-tenant-unique.xml "
        "(default: HEAD -- the tag's own commit when run right before pushing it).",
    )
    parser.add_argument(
        "--repo-root", default=None, type=pathlib.Path, metavar="PATH",
        help="Repo root for the `git cat-file` invocation (default: this script's parent repo).",
    )
    parser.add_argument(
        "--project", default=CC5_PROJECT, metavar="PROJECT",
        help=f"T2 project namespace to read the record from (default: {CC5_PROJECT!r}).",
    )
    parser.add_argument(
        "--title", default=CC5_TITLE, metavar="TITLE",
        help=f"T2 entry title to read (default: {CC5_TITLE!r}).",
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root or pathlib.Path(__file__).resolve().parent.parent
    file_present = file_present_at_ref(args.ref, repo_root=repo_root)
    # Short-circuit: an absent file means nx is never consulted at all (see
    # run_gate's own docstring for why this ordering is load-bearing).
    if file_present is not True:
        return run_gate(file_present, None)
    record_text = fetch_cc5_record(args.project, args.title)
    return run_gate(file_present, record_text)


if __name__ == "__main__":
    raise SystemExit(main())
