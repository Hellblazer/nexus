# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-run list collectors in ``nexus.mcp_infra`` record only inside a CLI run.

nexus-wbfpw.29 round 2 gated the manifest-write-failure and identity-drop
collectors behind ``_identity_drop_collectors_active`` so a long-lived process
(the MCP server, which never resets and never reads them) does not grow them for
its whole life. Three sibling list collectors still appended unconditionally:
completion refusals, superseded-sweep skips and ephemeral-registration skips.
The first two are reached from the MCP store_put path
(``note_write._stamp_refused``, ``MetadataMergingCatalog._note``).

The autouse ``_isolate_index_run_collectors`` fixture (tests/conftest.py) starts
every test disarmed, so each "long-lived process" test below begins from the
state an MCP server is always in.
"""

from __future__ import annotations

import pytest

from nexus import mcp_infra

_REPEATS = 500

# (recorder call, reader returning the collected entries, reset)
_RECORDERS = {
    "complete_refusal": (
        lambda i: mcp_infra._record_complete_refusal(f"1.1.{i}"),
        mcp_infra.get_complete_refusals,
        mcp_infra.reset_complete_refusals,
    ),
    "superseded_sweep_skip": (
        lambda i: mcp_infra._record_superseded_sweep_skip(f"1.1.{i}", "col", "x"),
        lambda: mcp_infra.get_superseded_sweep_stats()["skipped"],
        mcp_infra.reset_superseded_sweep_stats,
    ),
    "ephemeral_registration_skip": (
        lambda i: mcp_infra._record_ephemeral_registration_skip(f"/tmp/f{i}", "own"),
        mcp_infra.get_ephemeral_registration_skips,
        mcp_infra.reset_ephemeral_registration_skips,
    ),
}


@pytest.mark.parametrize("name", sorted(_RECORDERS))
def test_long_lived_process_never_accumulates(name):
    """No CLI run has reset anything: repeated recording must retain nothing."""
    record, read, _reset = _RECORDERS[name]
    assert mcp_infra._identity_drop_collectors_active is False  # the premise

    for i in range(_REPEATS):
        record(i)

    assert read() == []


@pytest.mark.parametrize("name", sorted(_RECORDERS))
def test_cli_shaped_run_still_reports(name):
    """Reset, then record: the end-of-run reader sees every entry."""
    record, read, _reset = _RECORDERS[name]

    from nexus.commands._helpers import reset_identity_drop_collectors

    reset_identity_drop_collectors()
    for i in range(3):
        record(i)

    assert len(read()) == 3


@pytest.mark.parametrize("name", sorted(_RECORDERS))
def test_each_collectors_own_reset_arms_it(name):
    """A caller that resets only this collector (``nx index repo`` resets the
    ephemeral skips on its own line) must still get it recording."""
    record, read, reset = _RECORDERS[name]
    assert mcp_infra._identity_drop_collectors_active is False

    reset()
    record(0)

    assert len(read()) == 1


def test_mcp_store_put_refused_stamp_records_nothing():
    """The real MCP-reachable caller of ``_record_complete_refusal``."""
    from nexus.catalog.note_write import _stamp_refused

    for i in range(_REPEATS):
        _stamp_refused(f"1.1.{i}", "knowledge__x", RuntimeError("refused"))

    assert mcp_infra.get_complete_refusals() == []


def test_sweep_response_from_a_store_records_no_skip_when_disarmed():
    """The real MCP-reachable caller of ``_record_superseded_sweep_skip``."""
    from nexus.catalog.metadata_merging_catalog import MetadataMergingCatalog

    merging = MetadataMergingCatalog(object(), "knowledge__x", [])
    resp = {"swept": 3, "sweep_detail": [{"doc_id": "1.1.1", "errored": True, "reason": "r"}]}

    for _ in range(_REPEATS):
        merging._note(resp)

    assert mcp_infra.get_superseded_sweep_stats()["skipped"] == []
