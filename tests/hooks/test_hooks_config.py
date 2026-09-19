# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``nexus._hook_runtime._config`` — one resolver for the orchestration
stop guard (RDR-215 Phase 1, bead nexus-q02nx.1).

Four bash scripts read ``NX_ORCH_STOP_GUARD`` inline today with no shared
helper, which is a place their defaults could silently drift. Measured at
develop ``9c730421f`` before consolidating, because the bead requires preserving
each script's *current* effective default rather than unifying on an assumption:

    agent-dispatch-expect.sh:63   MODE="${NX_ORCH_STOP_GUARD:-block}"
    subagent-start-stamp.sh:25    MODE="${NX_ORCH_STOP_GUARD:-block}"
    subagent-stop.sh:74           MODE="${NX_ORCH_STOP_GUARD:-block}"
    stop_verification_hook.sh:49  GUARD_MODE="${NX_ORCH_STOP_GUARD:-block}"

All four resolve identically to ``block``, and all four treat exactly
``observe`` and ``block`` as active — three spell the check as a negated guard
(``!= observe && != block`` -> stand down) and the fourth as the positive form
(``== observe || == block`` -> proceed), which is the same predicate. So the
consolidation preserves behaviour rather than choosing between rival defaults.
The contract map (T2 ``nexus_rdr/215-hook-contract-map``) hedges that some
script might default to ``off``; it does not. These tests pin the measurement.
"""
from __future__ import annotations

import pytest

from nexus._hook_runtime import _config


def test_unset_defaults_to_block(monkeypatch):
    """DEFAULT-ON since P1.G (subagent-stop.sh:16). The guard is not opt-in."""
    monkeypatch.delenv("NX_ORCH_STOP_GUARD", raising=False)
    assert _config.stop_guard_mode() == "block"
    assert _config.stop_guard_active() is True


def test_empty_string_also_defaults_to_block(monkeypatch):
    """``${VAR:-default}`` (colon form) substitutes on empty, not just on unset."""
    monkeypatch.setenv("NX_ORCH_STOP_GUARD", "")
    assert _config.stop_guard_mode() == "block"
    assert _config.stop_guard_active() is True


@pytest.mark.parametrize("value", ["observe", "block"])
def test_the_two_active_modes_are_returned_verbatim(monkeypatch, value):
    monkeypatch.setenv("NX_ORCH_STOP_GUARD", value)
    assert _config.stop_guard_mode() == value
    assert _config.stop_guard_active() is True


@pytest.mark.parametrize(
    "value",
    ["off", "OFF", "Block", "observe ", "yes", "1", "true"],
    ids=["off", "off-upper", "block-mixed-case", "trailing-space", "yes", "one", "true"],
)
def test_every_other_value_stands_the_guard_down(monkeypatch, value):
    """The bash comparison is exact and case-sensitive: ``off/unknown -> exit 0``
    (subagent-stop.sh:15). A mis-spelled mode disables the guard rather than
    erroring, and the port must not quietly become lenient about case or
    whitespace — that would silently re-enable a guard an operator turned off.
    """
    monkeypatch.setenv("NX_ORCH_STOP_GUARD", value)
    assert _config.stop_guard_mode() == value
    assert _config.stop_guard_active() is False


def test_the_mode_is_read_at_call_time_not_import_time(monkeypatch):
    """Hooks are long-lived in the tool tier: the server imports once and serves
    many events, so a cached module-level read would pin whatever the server's
    environment was at boot. Each call re-reads.
    """
    monkeypatch.setenv("NX_ORCH_STOP_GUARD", "off")
    assert _config.stop_guard_active() is False
    monkeypatch.setenv("NX_ORCH_STOP_GUARD", "block")
    assert _config.stop_guard_active() is True
