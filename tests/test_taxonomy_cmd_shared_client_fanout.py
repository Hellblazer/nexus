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
1 ``httpx.Client``` for its ``T2Database``, not one per domain store.

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
from nexus.db.storage_mode import T2_FACADE_STORES


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
    same-tick snapshot at construction time has no such problem.

    Store names come from ``nexus.db.storage_mode.T2_FACADE_STORES``, not
    hand-typed here -- a hand-typed list is exactly how ``tuples``
    (HttpTupleStore, RDR-205) went missing from this fan-out check for as
    long as it did."""
    from nexus.db.t2 import T2Database

    store_names = T2_FACADE_STORES
    snapshots: list[dict[str, int]] = []
    orig_init = T2Database.__init__

    def _capturing_init(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        snapshots.append({name: id(getattr(self, name)._client) for name in store_names})

    monkeypatch.setattr(T2Database, "__init__", _capturing_init)
    return snapshots


def test_real_taxonomy_status_command_shares_one_httpx_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real ``nx taxonomy status`` invocation (via the actual ``taxonomy``
    Click group, not a bare function call) against the engine substrate:
    BEFORE this wiring, one ``_T2Database(...)`` call built one
    independent ``httpx.Client`` per domain store; AFTER, the group's one
    shared client backs all of them. Table: T2Database constructions ->
    1, httpx.Client constructions -> 1 (not one-per-domain-store)."""
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
    store_count = len(t2_instances[0])
    assert len(client_tally) == 1, (
        f"BEFORE this wiring this would have been {store_count} (one "
        f"httpx.Client per domain store); AFTER, the taxonomy group's one "
        f"shared client must back the whole facade. Got "
        f"{len(client_tally)} httpx.Client construction(s) for one `nx "
        f"taxonomy status` run."
    )

    stores_clients = set(t2_instances[0].values())
    assert len(stores_clients) == 1, (
        f"expected all {store_count} domain stores to share the identical "
        f"shared client; found {len(stores_clients)} distinct client "
        f"objects: {t2_instances[0]}"
    )


def test_default_taxonomy_construction_outside_a_command_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling ``_T2Database`` OUTSIDE a live ``taxonomy`` Click context
    (e.g. a helper imported and called directly, or any caller that isn't
    a CLI invocation) must still build one independent httpx.Client() per
    domain store (``len(T2_FACADE_STORES)``) -- the additive contract:
    nothing changes for callers this fix does not touch."""
    from nexus.commands.taxonomy_cmd import _T2Database

    client_tally = _instrument_httpx_clients(monkeypatch)
    expected = len(T2_FACADE_STORES)

    db = _T2Database(tmp_path / "memory.db")
    try:
        assert len(client_tally) == expected, (
            f"expected _T2Database() called with no active Click context "
            f"to build {expected} independent httpx.Client()s, one per "
            f"domain store (default, unwired behavior); got "
            f"{len(client_tally)}"
        )
    finally:
        db.close()
