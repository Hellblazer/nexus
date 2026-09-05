# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-m20mf P3 (fold-in): a real `nx taxonomy` CLI invocation actually
uses the shared-client mechanism -- not just the T2Database-level unit
proof in tests/db/test_t2_facade_shared_client_fanout.py.

Coordinator directive (2026-09-05): P3 delivers nothing to any command
until taxonomy_cmd and index.py are wired to build one shared client per
command/run and pass it through. This file proves the taxonomy half: the
``taxonomy`` Click group callback (src/nexus/commands/taxonomy_cmd.py)
builds ONE ``httpx.Client`` via ``build_shared_t2_client()`` per process
invocation, stashes it on ``ctx.obj``, and ``_T2Database`` (the single
factory backing every ``nx taxonomy`` subcommand) picks it up -- so a real
``nx taxonomy status`` run against the engine substrate constructs exactly
1 ``httpx.Client``` for its ``T2Database``, not 8.

Runs against the self-provisioned engine TEST substrate (autouse
``_pin_t2_substrate``) -- never production, never a mocked transport.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from click.testing import CliRunner

from nexus.commands.taxonomy_cmd import taxonomy


def _instrument_httpx_clients(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    tally: list[int] = []
    orig_init = httpx.Client.__init__

    def _counting_init(self: httpx.Client, *args: Any, **kwargs: Any) -> None:
        tally.append(1)
        orig_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _counting_init)
    return tally


def _instrument_t2database(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, int]]:
    """For every ``T2Database`` constructed, snapshot each domain store's
    ``id(_client)`` IMMEDIATELY (not the instance itself) -- the command
    under test closes its own ``T2Database`` inside a ``with`` block
    before this test ever gets to inspect it, and ``close()`` nulls
    ``self._taxonomy`` (see ``T2Database.close``'s lazy-taxonomy comment),
    so anything read post-hoc from a stale reference is unreliable. A
    same-tick snapshot at construction time has no such problem."""
    from nexus.db.t2 import T2Database

    snapshots: list[dict[str, int]] = []
    orig_init = T2Database.__init__

    def _capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        snapshots.append(
            {
                "memory": id(self.memory._client),
                "plans": id(self.plans._client),
                "taxonomy": id(self.taxonomy._client),
                "telemetry": id(self.telemetry._client),
                "chash_index": id(self.chash_index._client),
                "document_aspects": id(self.document_aspects._client),
                "aspect_queue": id(self.aspect_queue._client),
                "document_highlights": id(self.document_highlights._client),
            }
        )

    monkeypatch.setattr(T2Database, "__init__", _capturing_init)
    return snapshots


def test_real_taxonomy_status_command_shares_one_httpx_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``nx taxonomy status`` invocation (via the actual ``taxonomy``
    Click group, not a bare function call) against the engine substrate:
    BEFORE this wiring, one ``_T2Database(...)`` call built 8 independent
    ``httpx.Client``s; AFTER, the group's one shared client backs all 8
    domain stores. Table: T2Database constructions -> 1, httpx.Client
    constructions -> 1 (not 8)."""
    db_path = tmp_path / "memory.db"
    client_tally = _instrument_httpx_clients(monkeypatch)
    t2_instances = _instrument_t2database(monkeypatch)

    runner = CliRunner()
    with patch("nexus.commands.taxonomy_cmd._default_db_path", return_value=db_path):
        result = runner.invoke(taxonomy, ["status"])

    assert result.exit_code == 0, result.output

    assert len(t2_instances) == 1, (
        f"expected exactly 1 T2Database construction for `nx taxonomy "
        f"status`; got {len(t2_instances)}"
    )
    assert len(client_tally) == 1, (
        f"BEFORE this wiring this would have been 8 (one httpx.Client per "
        f"domain store); AFTER, the taxonomy group's one shared client "
        f"must back the whole facade. Got {len(client_tally)} httpx.Client "
        f"construction(s) for one `nx taxonomy status` run."
    )

    stores_clients = set(t2_instances[0].values())
    assert len(stores_clients) == 1, (
        f"expected all 8 domain stores to share the identical shared "
        f"client; found {len(stores_clients)} distinct client objects: "
        f"{t2_instances[0]}"
    )


def test_default_taxonomy_construction_outside_a_command_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling ``_T2Database`` OUTSIDE a live ``taxonomy`` Click context
    (e.g. a helper imported and called directly, or any caller that isn't
    a CLI invocation) must still build 8 independent httpx.Client()s --
    the additive contract: nothing changes for callers this fix does not
    touch."""
    from nexus.commands.taxonomy_cmd import _T2Database

    client_tally = _instrument_httpx_clients(monkeypatch)

    db = _T2Database(tmp_path / "memory.db")
    try:
        assert len(client_tally) == 8, (
            f"expected _T2Database() called with no active Click context "
            f"to build 8 independent httpx.Client()s (default, unwired "
            f"behavior); got {len(client_tally)}"
        )
    finally:
        db.close()
