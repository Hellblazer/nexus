# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-b2eaw — the real-config-dir mutation guard's `last_seen_version`
handling, across two rounds.

ROUND 1. Measured twice during the 7.55.3 release (2026-09-21) on a
three-session box: `src/nexus/upgrade_finish.check_version_transition`
rewrites `last_seen_version` to the RUNNING `nx` version on every
invocation. On a box where more than one session runs `nx` from checkouts
at different versions, the guard's before/after (mtime_ns, size) diff
cannot tell a peer's write from a test in THIS session writing to the real
config dir -- both look like "the file changed". Two lint runs six minutes
apart, both red, over the same unmodified tree.

ROUND 1's FIX (now REVERTED -- see round 2): exempt a `last_seen_version`
change whenever its content did not match this session's own resolved
conexus version, reasoning that a different version could only be a
peer's write.

ROUND 2 (review finding, CRITICAL). That reasoning is FALSE: a test IN
THIS SESSION can spawn an INSTALLED `nx` from a stale/different generation
on PATH without isolating `NEXUS_CONFIG_DIR` -- exactly the documented
2026-08-24 incident shape (`tests/test_gate_fences_the_real_config_dir.py`:
"last_seen_version stamped 7.16.3 -- the INSTALLED tool's version, not the
tree under test"). Content alone cannot tell that shape apart from a
genuine peer process, because BOTH produce a version different from this
session's own resolved version. Exempting on version mismatch was
therefore a FALSE NEGATIVE: it could silently swallow the exact defect
this guard exists to catch, and round 1 shipped with ZERO test coverage of
that direction -- every round-1 test fed synthetic content that only ever
exercised the "genuine peer" story, never the "this session's own
misbehaving nx" story, because both produce identical inputs to the
function under test.

ROUND 2's FIX: remove the version-mismatch exemption entirely.
`last_seen_version` is now classified as state (fails the run) on ANY
genuine content change, exactly like round 0 (pre-nexus-b2eaw) -- a false
POSITIVE with an honest diagnostic beats a silent false negative. The ONE
content-driven exemption kept is when the baseline and post-session
content are BYTE-IDENTICAL: a stat touch with unchanged bytes cannot be a
real state mutation regardless of anyone's version, in-session or not, so
that direction stays safe to exempt. `this_session_version` remains a
parameter, but now feeds ONLY `_format_diff_entry`'s diagnostic, never the
classification.

Every kwarg defaults to `None`, which reproduces the PRE-nexus-b2eaw
behaviour exactly (`last_seen_version` always classified as state) --
every existing caller, including every test in
`tests/test_pfuns_ambient_daemon_logs.py` (synthetic (mtime, size) data,
no real file behind it), keeps working unchanged.
"""

from __future__ import annotations

import tests.conftest as conftest_module
from tests.conftest import (  # type: ignore[attr-defined]
    _split_appends_from_state,
)

REL = "last_seen_version"
BEFORE = {REL: (1, 6)}
AFTER = {REL: (2, 6)}
CHANGED = [("MODIFIED", REL)]


def test_same_version_write_still_flagged() -> None:
    """A rewrite to EXACTLY this session's own version is still what the
    guard exists to catch -- unchanged from round 1."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.55.3",
        this_session_version="7.55.3",
    )
    assert state == [("MODIFIED", REL)]
    assert appends == []


def test_different_version_write_is_ALSO_still_flagged() -> None:
    """ROUND 2, the false-negative close. A rewrite to a version different
    from this session's own is NO LONGER exempted -- round 1's reasoning
    ("must be a peer") cannot distinguish that shape from THIS session's
    own test hitting a stale installed `nx` (the 2026-08-24 incident
    shape), so both must fail. This is the exact input round 1's
    `test_peer_write_is_not_flagged` asserted the OPPOSITE for; that
    assertion was the bug."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.55.2",
        this_session_version="7.55.3",
    )
    assert state == [("MODIFIED", REL)]
    assert appends == []


def test_in_session_misbehaving_nx_shape_is_caught_not_swallowed() -> None:
    """The literal 2026-08-24 incident shape, reproduced: THIS session's
    own test spawns an installed `nx` from a DIFFERENT (stale) generation
    -- 7.16.3 -- against the real config dir, while this checkout's own
    resolved version is something else entirely (7.55.3, a live dev
    checkout far ahead of any installed generation). Content-based
    attribution has no way to tell this apart from a genuine peer's write
    -- both produce "new content != this session's version" -- so the only
    safe answer is to fail, honestly, every time this shape occurs."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.16.3",
        this_session_version="7.55.3",
    )
    assert state == [("MODIFIED", REL)], (
        "a same-session write through a stale installed `nx` was silently "
        "exempted -- this is the false negative round 2 exists to close"
    )
    assert appends == []


def test_byte_identical_restamp_is_not_a_state_mutation() -> None:
    """The ONE content-driven exemption kept in round 2: the mtime/size
    diff fired, but the baseline and post-session content are IDENTICAL --
    no real event happened, independent of anyone's version. This cannot
    be a genuine state mutation in either direction (in-session or peer),
    unlike a version mismatch."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.55.3",
        last_seen_version_baseline_content="7.55.3",
        this_session_version="7.55.1",  # irrelevant to this exemption
    )
    assert state == []
    assert appends == [("MODIFIED", REL)]


def test_genuine_change_is_not_swallowed_by_the_baseline_check() -> None:
    """The byte-identical exemption must not fire on a REAL change --
    baseline and post-session content differ here, so this must fall
    through to the (still-strict) state classification."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.55.3",
        last_seen_version_baseline_content="7.16.3",
        this_session_version="7.55.3",
    )
    assert state == [("MODIFIED", REL)]
    assert appends == []


def test_no_attribution_data_keeps_pre_existing_strict_behaviour() -> None:
    """No kwargs supplied (every caller before nexus-b2eaw, and every
    synthetic-data unit test) -- always state, unchanged."""
    state, appends = _split_appends_from_state(CHANGED, BEFORE, AFTER)
    assert state == [("MODIFIED", REL)]
    assert appends == []


def test_unresolvable_session_version_falls_back_to_state() -> None:
    """A failure to decide (this session's own version could not be
    resolved) must never silently exempt a real mutation."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.55.2",
        this_session_version=None,
    )
    assert state == [("MODIFIED", REL)]
    assert appends == []


def test_unreadable_stamp_content_falls_back_to_state() -> None:
    """A failure to read the post-session stamp (race, permissions) must
    never silently exempt a real mutation either."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content=None,
        this_session_version="7.55.3",
    )
    assert state == [("MODIFIED", REL)]
    assert appends == []


def test_resolve_this_session_conexus_version_reads_a_real_version() -> None:
    """Sanity: in this venv (an editable install of THIS checkout),
    resolution must succeed and return a non-empty string -- if it silently
    returned None here, the diagnostic would never carry a comparison
    value for any real session on this box."""
    from tests.conftest import _resolve_this_session_conexus_version  # type: ignore[attr-defined]

    version = _resolve_this_session_conexus_version()
    assert version, "could not resolve this session's own conexus version"


class TestFormatDiffEntryIsFactualNotAccusatory:
    """`_format_diff_entry` must present old content, new content, this
    session's version, and whether they match -- as observations, never as
    a claim about who wrote the file (round 2 review: the round-1 shape
    read as an accusation the guard cannot actually prove)."""

    def _render(self, monkeypatch, tmp_path, *, now: str, baseline: str | None, session_version: str | None) -> str:
        (tmp_path / REL).write_text(now)
        monkeypatch.setattr(conftest_module, "_real_config_dir_for_guard", lambda: tmp_path)
        monkeypatch.setattr(conftest_module, "_last_seen_version_baseline_content", baseline)
        monkeypatch.setattr(conftest_module, "_this_session_conexus_version", session_version)
        return conftest_module._format_diff_entry(("MODIFIED", REL))

    def test_shows_old_and_new_content_and_this_session_version(self, monkeypatch, tmp_path) -> None:
        rendered = self._render(monkeypatch, tmp_path, now="7.55.3", baseline="7.16.3", session_version="7.55.3")
        assert "7.16.3" in rendered
        assert "7.55.3" in rendered

    def test_never_asserts_who_wrote_it(self, monkeypatch, tmp_path) -> None:
        rendered = self._render(monkeypatch, tmp_path, now="7.55.3", baseline="7.16.3", session_version="7.55.3")
        lowered = rendered.lower()
        for accusatory_phrase in ("this session wrote", "a unit test wrote", "you wrote"):
            assert accusatory_phrase not in lowered, f"diagnostic asserts authorship: {rendered!r}"

    def test_names_a_matching_version_explicitly(self, monkeypatch, tmp_path) -> None:
        rendered = self._render(monkeypatch, tmp_path, now="7.55.3", baseline="7.16.3", session_version="7.55.3")
        assert "matches this session" in rendered

    def test_names_a_mismatched_version_explicitly(self, monkeypatch, tmp_path) -> None:
        rendered = self._render(monkeypatch, tmp_path, now="7.16.3", baseline="7.10.0", session_version="7.55.3")
        assert "does NOT match" in rendered
