# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-b2eaw — attribute a `last_seen_version` rewrite by content, not
just by mtime/size, so the real-config-dir mutation guard stops false-failing
on a PEER session's `nx` invocation from a differently-versioned tree.

Measured twice during the 7.55.3 release (2026-09-21) on a three-session
box: `src/nexus/upgrade_finish.check_version_transition` rewrites
`last_seen_version` to the RUNNING `nx` version on every invocation. On a
box where more than one session runs `nx` from checkouts at different
versions, the guard's before/after (mtime_ns, size) diff cannot tell a
peer's write from a test in THIS session writing to the real config dir --
both look like "the file changed". Two lint runs six minutes apart, both
red, over the same unmodified tree.

The fix is attribution: compare the stamp's post-session CONTENT to this
session's own resolved conexus version
(`_resolve_this_session_conexus_version`, exactly what
`upgrade_finish.install_dist_info` reads). A write landing on a version
this session's own `nx` invocation could not have produced is a peer's,
not this session's -- exempt it (report, do not fail). A write landing on
this session's own version is still exactly what the guard exists to
catch, and it still fails.

`_split_appends_from_state`'s two new keyword-only params default to
`None`, which reproduces the PRE-nexus-b2eaw behaviour exactly (always
state) -- every existing caller (including every test in
`tests/test_pfuns_ambient_daemon_logs.py`, which feeds the function
synthetic (mtime, size) data with no real file behind it) keeps working
unchanged. Attribution activates only when both values are supplied, which
only the real caller (`_check_real_config_dir_mutations`) does.
"""

from __future__ import annotations

from tests.conftest import (  # type: ignore[attr-defined]
    _split_appends_from_state,
)

REL = "last_seen_version"
BEFORE = {REL: (1, 6)}
AFTER = {REL: (2, 6)}
CHANGED = [("MODIFIED", REL)]


def test_peer_write_is_not_flagged() -> None:
    """A rewrite to a version this session's own `nx` could not have
    produced -- the exact 7.55.2-vs-7.55.3 shape from the incident -- is
    exempted, not failed."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.55.2",
        this_session_version="7.55.3",
    )
    assert state == []
    assert appends == [("MODIFIED", REL)]


def test_in_session_write_still_flagged() -> None:
    """A rewrite to EXACTLY this session's own version is still what the
    guard exists to catch -- attribution must not swallow a real leak."""
    state, appends = _split_appends_from_state(
        CHANGED, BEFORE, AFTER,
        last_seen_version_content="7.55.3",
        this_session_version="7.55.3",
    )
    assert state == [("MODIFIED", REL)]
    assert appends == []


def test_no_attribution_data_keeps_pre_existing_strict_behaviour() -> None:
    """Neither kwarg supplied (every caller before nexus-b2eaw, and every
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
    returned None here, attribution would never activate for any real
    session on this box."""
    from tests.conftest import _resolve_this_session_conexus_version  # type: ignore[attr-defined]

    version = _resolve_this_session_conexus_version()
    assert version, "could not resolve this session's own conexus version"
