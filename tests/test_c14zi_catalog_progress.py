# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-c14zi: ``_catalog_hook``'s ``"  Catalog: ..."`` progress writes
used to go through a raw ``sys.stderr.write`` bound directly
(``_progress = sys.stderr.write``), un-gated on TTY. Its ``\\r``
in-place-repaint writes are harmless on an interactive terminal but a
literal control character in a redirected (non-TTY) log — two such writes
used to land on the SAME physical line with no separator (observed on a
real run: "Catalog: linking 251 new entries...  Catalog:
housekeeping..." mashed together).

The fix extracts the branching into ``nexus.indexer._catalog_progress``, a
pure module-level function of ``(msg, is_tty, on_phase, write)`` — this
lets it be pinned directly without needing the T2 engine substrate
``_catalog_hook`` itself requires for a real catalog round-trip (unit
tests exercising the FULL hook live in tests/test_catalog_indexer_hook.py
and tests/test_indexer_linking_phases.py; those need
``ActiveCatalog()``/the real engine, this file's tests do not).
"""
from __future__ import annotations

from nexus.indexer import _catalog_progress


def test_nontty_two_r_terminated_messages_arrive_as_two_separate_phase_lines():
    """The exact reported mashed-line scenario: two \\r-terminated
    'Catalog: ...' messages, non-TTY. Must arrive as two SEPARATE on_phase
    calls, neither carrying a literal \\r -- a test that only greps for
    the absence of \\r would pass even if the two were still concatenated
    into one on_phase call, so both properties are asserted."""
    phases: list[str] = []
    writes: list[str] = []
    _catalog_progress(
        "  Catalog: linking 251 new entries…\r",
        is_tty=False, on_phase=phases.append, write=writes.append,
    )
    _catalog_progress(
        "  Catalog: housekeeping…\r",
        is_tty=False, on_phase=phases.append, write=writes.append,
    )
    assert phases == [
        "  Catalog: linking 251 new entries…",
        "  Catalog: housekeeping…",
    ]
    for line in phases:
        assert "\r" not in line
    # Routed entirely through on_phase -- the raw write channel is untouched.
    assert writes == []


def test_nontty_without_on_phase_falls_back_to_plain_terminated_write():
    """No on_phase supplied (some _catalog_hook callers, e.g. tests, omit
    it): non-TTY still must never emit a bare \\r -- fall back to a plain,
    newline-terminated write instead."""
    writes: list[str] = []
    _catalog_progress(
        "  Catalog: done (3 new, 1 links)\r",
        is_tty=False, on_phase=None, write=writes.append,
    )
    assert writes == ["  Catalog: done (3 new, 1 links)\n"]
    assert "\r" not in writes[0]


def test_nontty_empty_after_strip_emits_nothing():
    """A message that is only a bare \\r/\\n strips to empty and must not
    produce a phantom blank on_phase call or write."""
    phases: list[str] = []
    writes: list[str] = []
    _catalog_progress("\r", is_tty=False, on_phase=phases.append, write=writes.append)
    assert phases == []
    assert writes == []


def test_tty_path_still_repaints_in_place():
    """Non-vacuity guard against the gate deleting the feature outright:
    on a TTY, the message reaches `write` completely UNCHANGED -- \\r
    included -- and on_phase is never consulted."""
    phases: list[str] = []
    writes: list[str] = []
    msg = "  Catalog: registering 3 files…\r"
    _catalog_progress(msg, is_tty=True, on_phase=phases.append, write=writes.append)
    assert writes == [msg]
    assert phases == []
