# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-d76vc: T1 handoff marker primitives (nexus.daemon.t1_handoff).

Pure file-I/O + structural-validation coverage. Ancestry verification is
NOT this module's job (see its docstring) -- that is exercised separately
against the writer (tests/test_hooks.py) and the watcher
(tests/mcp/test_t1_handoff_release.py).
"""
from __future__ import annotations

from pathlib import Path

from nexus.daemon.t1_handoff import (
    HandoffMarker,
    claim_handoff_marker,
    claimed_marker_path,
    consume_claimed_marker,
    consume_handoff_marker,
    handoff_marker_path,
    read_claimed_marker,
    read_handoff_marker,
    write_handoff_marker,
    write_handoff_marker_if_absent,
)


def test_marker_path_is_keyed_on_mcp_pid(tmp_path: Path) -> None:
    p1 = handoff_marker_path(111, tmp_path)
    p2 = handoff_marker_path(222, tmp_path)
    assert p1 != p2
    assert p1.name == "t1_handoff.111"
    assert p2.name == "t1_handoff.222"


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    write_handoff_marker(
        111, new_session_id="new-sess", claude_pid=999, config_dir=tmp_path,
        clock=lambda: 1000.0,
    )
    marker = read_handoff_marker(111, tmp_path)
    assert marker == HandoffMarker(
        new_session_id="new-sess", claude_pid=999, written_at=1000.0
    )


def test_write_is_mode_0600(tmp_path: Path) -> None:
    write_handoff_marker(
        111, new_session_id="s", claude_pid=1, config_dir=tmp_path,
    )
    path = handoff_marker_path(111, tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600


def test_write_creates_config_dir(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist"
    write_handoff_marker(
        111, new_session_id="s", claude_pid=1, config_dir=nested,
    )
    assert (nested / "t1_handoff.111").exists()


def test_read_missing_marker_returns_none(tmp_path: Path) -> None:
    assert read_handoff_marker(999, tmp_path) is None


def test_read_malformed_json_returns_none(tmp_path: Path) -> None:
    path = handoff_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text("not json {{", encoding="utf-8")
    assert read_handoff_marker(111, tmp_path) is None


def test_read_missing_field_returns_none(tmp_path: Path) -> None:
    path = handoff_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text('{"new_session_id": "s"}', encoding="utf-8")
    assert read_handoff_marker(111, tmp_path) is None


def test_read_empty_session_id_returns_none(tmp_path: Path) -> None:
    path = handoff_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"new_session_id": "  ", "claude_pid": 1, "written_at": 1.0}',
        encoding="utf-8",
    )
    assert read_handoff_marker(111, tmp_path) is None


def test_read_wrong_type_claude_pid_returns_none(tmp_path: Path) -> None:
    path = handoff_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"new_session_id": "s", "claude_pid": "not-an-int", "written_at": 1.0}',
        encoding="utf-8",
    )
    assert read_handoff_marker(111, tmp_path) is None


def test_read_bool_claude_pid_rejected(tmp_path: Path) -> None:
    """``bool`` is an ``int`` subclass in Python -- must not silently pass
    the isinstance(int) check as a claude_pid."""
    path = handoff_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"new_session_id": "s", "claude_pid": true, "written_at": 1.0}',
        encoding="utf-8",
    )
    assert read_handoff_marker(111, tmp_path) is None


def test_write_strips_session_id_whitespace(tmp_path: Path) -> None:
    path = handoff_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"new_session_id": "  padded-sess  ", "claude_pid": 1, "written_at": 1.0}',
        encoding="utf-8",
    )
    marker = read_handoff_marker(111, tmp_path)
    assert marker is not None
    assert marker.new_session_id == "padded-sess"


def test_consume_deletes_marker(tmp_path: Path) -> None:
    write_handoff_marker(111, new_session_id="s", claude_pid=1, config_dir=tmp_path)
    assert read_handoff_marker(111, tmp_path) is not None
    consume_handoff_marker(111, tmp_path)
    assert read_handoff_marker(111, tmp_path) is None


def test_consume_missing_marker_is_idempotent_noop(tmp_path: Path) -> None:
    consume_handoff_marker(999, tmp_path)  # must not raise


def test_rewrite_overwrites_prior_unconsumed_marker(tmp_path: Path) -> None:
    """A rapid clear-then-resume before the watcher's next tick must hand
    off to the LATEST session id, not queue/preserve a stale one."""
    write_handoff_marker(
        111, new_session_id="first", claude_pid=1, config_dir=tmp_path,
        clock=lambda: 1.0,
    )
    write_handoff_marker(
        111, new_session_id="second", claude_pid=1, config_dir=tmp_path,
        clock=lambda: 2.0,
    )
    marker = read_handoff_marker(111, tmp_path)
    assert marker is not None
    assert marker.new_session_id == "second"
    assert marker.written_at == 2.0


# ── claim/read-claimed/consume-claimed (fix-round TOCTOU fix) ──────────────


def test_claimed_marker_path_is_disjoint_from_live_path(tmp_path: Path) -> None:
    live = handoff_marker_path(111, tmp_path)
    claimed = claimed_marker_path(111, tmp_path)
    assert live != claimed
    assert claimed.name == "t1_handoff.claimed.111"


def test_claim_renames_live_marker_to_claimed_path(tmp_path: Path) -> None:
    write_handoff_marker(111, new_session_id="s", claude_pid=1, config_dir=tmp_path)
    live_path = handoff_marker_path(111, tmp_path)

    claimed_path = claim_handoff_marker(111, tmp_path)

    assert claimed_path == claimed_marker_path(111, tmp_path)
    assert not live_path.exists()  # gone from the live path
    assert claimed_path.exists()  # present at the claimed path


def test_claim_with_no_live_marker_returns_none_and_is_a_noop(tmp_path: Path) -> None:
    result = claim_handoff_marker(999, tmp_path)
    assert result is None
    assert not claimed_marker_path(999, tmp_path).exists()


def test_claim_overwrites_a_prior_unconsumed_claimed_file(tmp_path: Path) -> None:
    """A leftover claimed file from a crashed prior tick is overwritten
    by a fresh claim -- it was already in an indeterminate state."""
    claimed_path = claimed_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    claimed_path.write_text("leftover-from-a-crashed-tick", encoding="utf-8")

    write_handoff_marker(111, new_session_id="fresh", claude_pid=1, config_dir=tmp_path)
    result = claim_handoff_marker(111, tmp_path)

    assert result == claimed_path
    marker = read_claimed_marker(claimed_path)
    assert marker is not None
    assert marker.new_session_id == "fresh"


def test_read_claimed_marker_round_trips(tmp_path: Path) -> None:
    write_handoff_marker(
        111, new_session_id="s", claude_pid=1, config_dir=tmp_path,
        clock=lambda: 5.0,
    )
    claimed_path = claim_handoff_marker(111, tmp_path)
    assert claimed_path is not None

    marker = read_claimed_marker(claimed_path)
    assert marker == HandoffMarker(new_session_id="s", claude_pid=1, written_at=5.0)


def test_read_claimed_marker_missing_returns_none(tmp_path: Path) -> None:
    assert read_claimed_marker(tmp_path / "does-not-exist") is None


def test_read_claimed_marker_malformed_returns_none(tmp_path: Path) -> None:
    claimed_path = claimed_marker_path(111, tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    claimed_path.write_text("not json {{", encoding="utf-8")
    assert read_claimed_marker(claimed_path) is None


def test_consume_claimed_marker_deletes_it(tmp_path: Path) -> None:
    write_handoff_marker(111, new_session_id="s", claude_pid=1, config_dir=tmp_path)
    claimed_path = claim_handoff_marker(111, tmp_path)
    assert claimed_path is not None
    assert claimed_path.exists()

    consume_claimed_marker(claimed_path)

    assert not claimed_path.exists()


def test_consume_claimed_marker_missing_is_idempotent_noop(tmp_path: Path) -> None:
    consume_claimed_marker(tmp_path / "does-not-exist")  # must not raise


def test_claim_then_fresh_write_to_live_path_survives_untouched(tmp_path: Path) -> None:
    """The exact TOCTOU scenario the claim step exists to close: after
    claiming marker A, a fresh write of marker B to the LIVE path must
    remain completely untouched -- it is a DIFFERENT file from the one
    the claim step already moved aside."""
    write_handoff_marker(
        111, new_session_id="A", claude_pid=1, config_dir=tmp_path, clock=lambda: 1.0,
    )
    claimed_path = claim_handoff_marker(111, tmp_path)
    assert claimed_path is not None

    # A second /clear lands while "processing" is still in flight.
    write_handoff_marker(
        111, new_session_id="B", claude_pid=1, config_dir=tmp_path, clock=lambda: 2.0,
    )

    # A is safely isolated at the claimed path...
    marker_a = read_claimed_marker(claimed_path)
    assert marker_a is not None
    assert marker_a.new_session_id == "A"

    # ...and B is untouched at the live path, ready for its own claim.
    marker_b = read_handoff_marker(111, tmp_path)
    assert marker_b is not None
    assert marker_b.new_session_id == "B"

    # Consuming the claimed A does not touch the live B.
    consume_claimed_marker(claimed_path)
    assert read_handoff_marker(111, tmp_path) is not None
    assert read_handoff_marker(111, tmp_path).new_session_id == "B"


# ── write_handoff_marker_if_absent (fix-round 2: no-clobber reinstate) ─────


def test_write_if_absent_succeeds_when_live_path_is_empty(tmp_path: Path) -> None:
    result = write_handoff_marker_if_absent(
        111, new_session_id="s", claude_pid=1, config_dir=tmp_path, clock=lambda: 5.0,
    )
    assert result is True
    marker = read_handoff_marker(111, tmp_path)
    assert marker == HandoffMarker(new_session_id="s", claude_pid=1, written_at=5.0)


def test_write_if_absent_returns_false_and_does_not_clobber_existing(
    tmp_path: Path,
) -> None:
    write_handoff_marker(
        111, new_session_id="newer", claude_pid=1, config_dir=tmp_path,
        clock=lambda: 20.0,
    )

    result = write_handoff_marker_if_absent(
        111, new_session_id="stale", claude_pid=1, config_dir=tmp_path,
        clock=lambda: 10.0,
    )

    assert result is False
    # Completely untouched -- still the "newer" marker, not the stale one.
    marker = read_handoff_marker(111, tmp_path)
    assert marker is not None
    assert marker.new_session_id == "newer"
    assert marker.written_at == 20.0


def test_write_if_absent_creates_config_dir(tmp_path: Path) -> None:
    nested = tmp_path / "does" / "not" / "exist"
    result = write_handoff_marker_if_absent(
        111, new_session_id="s", claude_pid=1, config_dir=nested,
    )
    assert result is True
    assert (nested / "t1_handoff.111").exists()


def test_write_if_absent_leaves_no_temp_file_on_success(tmp_path: Path) -> None:
    write_handoff_marker_if_absent(111, new_session_id="s", claude_pid=1, config_dir=tmp_path)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "t1_handoff.111"]
    assert leftovers == []


def test_write_if_absent_leaves_no_temp_file_on_supersede(tmp_path: Path) -> None:
    write_handoff_marker(111, new_session_id="newer", claude_pid=1, config_dir=tmp_path)
    write_handoff_marker_if_absent(111, new_session_id="stale", claude_pid=1, config_dir=tmp_path)
    leftovers = [p for p in tmp_path.iterdir() if p.name != "t1_handoff.111"]
    assert leftovers == []


def test_write_if_absent_mode_0600_on_success(tmp_path: Path) -> None:
    write_handoff_marker_if_absent(111, new_session_id="s", claude_pid=1, config_dir=tmp_path)
    path = handoff_marker_path(111, tmp_path)
    assert (path.stat().st_mode & 0o777) == 0o600
