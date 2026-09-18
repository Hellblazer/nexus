# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-211 nexus-rplay.14: the Monitor-driven ``nx tuple watch`` CLI loop,
its SessionStart arming and the 30-minute re-arm rule were deleted
outright (Sam's decision of 2026-09-16, T2 nexus_rdr/211-decision-channel-
delivery-2026-09-16). Push delivery is now the session's own nexus MCP
server pushing over the Claude Code channel, with the ``UserPromptSubmit``
drain hook as the unconditional floor beneath it.

A straggler mention of the deleted surface reads as still-live: a docstring
or a skill instructing an agent to arm a command that no longer exists is
worse than silence, because it sends the reader chasing a dead end instead
of the real mechanism (``tuple_subscribe`` + the channel). This census
holds every one of the eight identifying literal strings of that surface
to zero, everywhere except a fixed allowlist of historical records where
the deleted shape is the whole point of the record.

Deliberately EXACT-SUBSTRING, not a regex with word boundaries or import
resolution: the point is textual absence, not whether some clever
respelling would still parse as Python. A false negative here is a
respelling nobody would plausibly write by accident; a false positive
costs one line in the allowlist, named and justified.

Non-vacuity (the nexus-moht0 doctrine): a scan that walked zero files would
report a hollow, unfalsifiable "clean" — the SCANNED_FILE_FLOOR assertion
below fails loud if the walk itself is broken, before the banned-string
assertion ever gets a chance to pass by finding nothing to check.

RDR-213 (amends RDR-211, bead nexus-tk2cz) reuses this same walk and
non-vacuity floor for a second, independent census: the proof gate and
claim-at-delivery machinery RDR-213 deletes outright (a claim needs a
proof the client cannot give, so the gate can only be a heuristic — see
RDR-213 Gap 1). See :data:`RDR_213_BANNED_STRINGS` below.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The eight literal strings that identify the deleted surface. Exact
#: substrings, checked independently per line.
BANNED_STRINGS: tuple[str, ...] = (
    "nexus.tuple_watch",
    "tuple_watch.py",
    "nx tuple watch",
    "tuple watch --instance",
    "MAILBOX WATCH",
    "watcher_alive",
    "tuple_watch_cmd",
    "_check_tuple_watch_permission",
)

#: RDR-213 (amends RDR-211 Phase 1 Step 3, bead nexus-tk2cz): the proof
#: gate and claim-at-delivery machinery deleted outright, not kept as a
#: fallback -- a claim needs a proof the client cannot give (RDR-213
#: Gap 1), so RDR-213 deletes the gate along with the claim itself.
#: Deliberately excludes `tuple_channel_probe`: that name is also a doc
#: PROSE mention (the tool's own catalog row) this census does not own --
#: `tests/test_mcp_package.py`'s exact-set pin and
#: `tests/test_mcp_wire_snapshot.py` are the tool-registration guards for
#: that deletion.
#: Also deliberately excludes `claimant` and `lease_s`: the RDR's Deleted
#: list names them as the WAITER's own attributes (its claimant identity
#: and lease/renew loop), but both are also live `tuple_in`/`tuple_renew`
#: PARAMETER names on the MCP tool surface (RDR-205/206), so banning them
#: tree-wide would flag every unrelated caller of those tools. Their
#: deletion from the waiter is proven by absence from `ChannelWaiter`'s
#: own `__init__` signature (see `tests/test_mcp_channel.py`), not by
#: this exact-substring census.
#: `renew_interval_s` and `max_resends` are NOT excluded (code-review
#: round-2 fix): a grep of the live tree (`src`, `tests`, `conexus`,
#: `scripts`, excluding this file and `docs/rdr/`) found zero hits for
#: either, so there is no unrelated caller they could false-positive
#: against, and the earlier "common enough English" exclusion reason
#: forwent free defensive coverage against a future reintroduction with
#: no offsetting risk.
#: Round 3-5 identity tracking, replaced by the cursor (T2
#: `nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
#: 2026-09-17`): the mailbox path tracked EACH row's identity and
#: claim-state across reconciles for three fix rounds running, each
#: round's own fix making the next round's defect harder to see. The
#: replacement cursor design (a position past the last referenced row,
#: plus a bare re-send budget) needs none of it -- these names are
#: deleted outright, not kept as dead code or a fallback.
_ROUND_3_5_IDENTITY_TRACKING_BANNED_STRINGS: tuple[str, ...] = (
    "_pending_subspaces",
    "_reconcile_pending_mailboxes",
    "_reconcile_tracked_row",
    "_pending_key",
    "_scan_forward",
    "_ScanOutcome",
    "_select_live_head",
    "_dead_cursor",
    "_advance_dead_cursor",
    "_last_reconciled_at",
    "_is_cadence_due",
    "_Announced",
    "in_hand",
    "_RECONCILE_HEAD_SCAN_N",
    "_RECONCILE_MAX_PASSES",
)

#: Bead nexus-vsipz (RDR-213 engine half): the mailbox path's own cursor
#: and per-mailbox re-send bookkeeping -- the ROUND-3-5-successor design
#: T2 `nexus_rdr/213-decision-announcements-rate-limited-not-ack-gated-
#: 2026-09-17` landed and this bead itself supersedes -- replaced by the
#: engine's own `announced_at`/`announce_count` stamp
#: (`TupleRepository.WaitSpec.Announce`, service-side): the engine now
#: decides cadence and cap, and this waiter tracks no position of its
#: own for a mailbox at all. `._cursor` (not the bare word `cursor`,
#: which boards' own position-cursor dict key and `ReadCursor` both still
#: use legitimately) is the exact attribute-access shape the deleted
#: `self._cursor`/`waiter._cursor` dict used and nothing else in the live
#: tree ever wrote.
_NEXUS_VSIPZ_ENGINE_STAMP_BANNED_STRINGS: tuple[str, ...] = (
    "_LastRef",
    "_last_ref",
    "_resend_due_references",
    "_seconds_to_nearest_resend_s",
    "just_referenced",
    "._cursor",
)

RDR_213_BANNED_STRINGS: tuple[str, ...] = (
    "detect_channel_argv",
    "_CHANNEL_ARGV_FLAGS",
    "_read_parent_command",
    "_PROBE_CONTENT",
    "_probe_until_live",
    "_send_probe",
    "note_probe_ack",
    "_maybe_claim_mail",
    "_renew_or_release",
    "_adopt_persisted_outstanding",
    "_Outstanding",
    "note_credit",
    "renew_interval_s",
    "max_resends",
    *_ROUND_3_5_IDENTITY_TRACKING_BANNED_STRINGS,
    *_NEXUS_VSIPZ_ENGINE_STAMP_BANNED_STRINGS,
)

#: Every directory this census walks, relative to the repo root.
_SCAN_ROOTS: tuple[str, ...] = ("src", "tests", "conexus", "docs", "scripts", "web")

#: Directories never descended into: VCS internals, caches, and build output.
_SKIP_DIR_NAMES = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache",
    "dist", "build",
})

#: Historical records where the deleted surface's exact shape is the
#: record, not a straggler: RDR files narrate what was designed and later
#: removed, and changelogs narrate what shipped and what was deleted in a
#: past release -- both are supposed to say the old name.
_ALLOW_PREFIXES: tuple[str, ...] = ("docs/rdr/",)
_ALLOW_EXACT: frozenset[str] = frozenset({
    "docs/wire-contract-pending.md",
    "conexus/CHANGELOG.md",
})


def _is_allowlisted(rel_posix: str) -> bool:
    if rel_posix.startswith(_ALLOW_PREFIXES):
        return True
    if rel_posix in _ALLOW_EXACT:
        return True
    # CHANGELOG*.md anywhere in the tree (root CHANGELOG.md, and any other
    # changelog a subtree might carry) -- same historical-record reasoning
    # as the two exact entries above, generalised to the naming pattern.
    name = rel_posix.rsplit("/", 1)[-1]
    return name.startswith("CHANGELOG") and name.endswith(".md")


def _scanned_files() -> list[Path]:
    # This test's own file is excluded deliberately: it names every banned
    # string itself, as the very lists it checks other files against, and
    # self-matching there is not a straggler.
    self_path = Path(__file__).resolve()
    out: list[Path] = []
    for root_name in _SCAN_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                entries = list(current.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir():
                    if entry.name not in _SKIP_DIR_NAMES:
                        stack.append(entry)
                    continue
                if entry.resolve() == self_path:
                    continue
                out.append(entry)
    return out


def _offenders(banned: tuple[str, ...] = BANNED_STRINGS) -> list[str]:
    offenders: list[str] = []
    for path in _scanned_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if _is_allowlisted(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle in banned:
                if needle in line:
                    offenders.append(f"{rel}:{lineno}: {needle!r} in {line.strip()[:160]!r}")
    return offenders


#: A scan that walked fewer files than this is broken, not clean --
#: src/nexus alone carries several hundred Python files.
SCANNED_FILE_FLOOR = 500


def test_the_scan_sees_real_files() -> None:
    """Non-vacuity: a scan of zero (or a suspiciously small number of)
    files would make the assertion below pass by finding nothing to check,
    which is the false-clean failure this whole census exists to avoid."""
    count = len(_scanned_files())
    assert count >= SCANNED_FILE_FLOOR, (
        f"scanned only {count} files across {_SCAN_ROOTS}; expected at least "
        f"{SCANNED_FILE_FLOOR} -- the walk itself looks broken, not the tree clean"
    )


def test_no_straggler_references_to_the_deleted_watcher() -> None:
    offenders = _offenders(BANNED_STRINGS)
    assert offenders == [], (
        "RDR-211 nexus-rplay.14 deleted the CLI mailbox-watch loop, its "
        "SessionStart arming and the 30-minute re-arm rule outright. A "
        "straggler mention of the deleted surface below reads as still "
        "live -- fix the reference (paraphrase, past tense, or point at "
        "the replacement: tuple_subscribe + the Claude Code channel), or "
        "add the file to this test's allowlist if it is a genuine "
        "historical record:\n  " + "\n  ".join(offenders)
    )


def test_no_straggler_references_to_the_rdr213_deleted_proof_gate() -> None:
    offenders = _offenders(RDR_213_BANNED_STRINGS)
    assert offenders == [], (
        "RDR-213 deleted the RDR-211 proof gate and claim-at-delivery "
        "machinery outright: the waiter never claims mail, so there is "
        "nothing left needing a proof it is safe to claim. A straggler "
        "reference to the deleted names below reads as still-live -- fix "
        "the reference or add the file to this test's allowlist if it is "
        "a genuine historical record:\n  " + "\n  ".join(offenders)
    )
