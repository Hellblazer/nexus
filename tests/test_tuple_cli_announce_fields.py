# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx tuple rd/in/list`` shows a row's announce budget, as the MCP tools do (GitHub #1567).

``announced_at`` and ``announce_count`` (RDR-213, bead nexus-vsipz) were added to the MCP
serializer (``nexus.mcp.core._tuple_row_to_dict``) and not to the CLI's near-copy
(``nexus.commands.tuple_cmd._row_dict``), so ``nx tuple rd --json`` left them out. The two must
render the same keys for the same row, so they cannot drift again.
"""

from __future__ import annotations

import pytest

from nexus.commands import tuple_cmd
from nexus.db.t2.http_tuple_store import _body_to_tuple_row
from nexus.mcp.core import _tuple_row_to_dict

_BODY = {
    "id": "a" * 64, "subspace": "mailbox/x", "template": "mailbox/<address>",
    "keys": {"to": "x"}, "dims": {"from": "y"}, "body": "hi",
    "claim_state": None, "claimant": None, "lease_until": None,
    "attempts": 0, "consumed_at": None, "consumed_by": None,
    "expires_at": None, "created_at": "2026-11-10T00:00:00Z",
    "announced_at": "2026-11-10T00:00:05Z", "announce_count": 2,
}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(_BODY, id="announced"),
        pytest.param({**_BODY, "announced_at": None, "announce_count": 0}, id="never-announced"),
        pytest.param({**_BODY, "sender_session": "S1"}, id="stamped-sender"),
    ],
)
def test_the_cli_json_row_has_the_mcp_rows_keys_and_values(body: dict) -> None:
    row = _body_to_tuple_row(dict(body))
    assert tuple_cmd._row_dict(row) == _tuple_row_to_dict(row)


def test_the_cli_json_row_carries_the_announce_budget() -> None:
    out = tuple_cmd._row_dict(_body_to_tuple_row(dict(_BODY)))
    assert out["announced_at"] == "2026-11-10T00:00:05Z"
    assert out["announce_count"] == 2


def test_the_cli_text_row_prints_the_announce_budget(capsys: pytest.CaptureFixture[str]) -> None:
    tuple_cmd._print_row(_body_to_tuple_row(dict(_BODY)))
    text = capsys.readouterr().out
    assert "  announced_at: 2026-11-10T00:00:05Z" in text
    assert "  announce_count: 2" in text
