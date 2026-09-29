# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-kp5q3: a descending tuple read, so ``nx tuple rd --newest`` returns the
true newest rows however large the subspace, past ``--max-rows``.

Before this the engine could only order ``(created_at, id)`` ascending, so
``--newest`` paged oldest-first up to ``--max-rows`` and kept the tail, then
exited 3 when rows remained: a subspace past the bound could not return its
newest rows at all. The engine now takes ``order: "desc"`` on ``/rd`` and
``/rdp`` and echoes ``"order": "desc"`` in the response. The echo is the
capability signal: an engine that predates the field ignores the unknown key
and answers ascending without it, which the client must not mistake for a
descending read.

Store and CLI behaviour is proved here with a stubbed transport (no engine);
the real-engine round trip is in ``tests/test_tuple_cmd.py``.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands.tuple_cmd import tuple_group
from nexus.db.t2.http_tuple_store import DescendingReadUnsupportedError, HttpTupleStore
from nexus.db.t2.records import TupleRow


def _wire_row(i: int) -> dict[str, Any]:
    return {
        "id": f"{i:064x}",
        "subspace": "mailbox/a",
        "template": "mailbox/<address>",
        "keys": {"to": "a"},
        "dims": {},
        "body": f"m{i}",
        "claim_state": None,
        "claimant": None,
        "created_at": f"2026-09-29T10:00:{i:02d}Z",
    }


def _store_with_transport(monkeypatch, responder) -> tuple[HttpTupleStore, list[tuple[str, dict]]]:
    """A store whose ``_post`` records ``(path, payload)`` and answers via *responder*."""
    calls: list[tuple[str, dict]] = []

    def _post(self, path, payload, **kwargs):
        calls.append((path, payload))
        return responder(path, payload)

    monkeypatch.setattr(HttpTupleStore, "_post", _post)
    store = HttpTupleStore.__new__(HttpTupleStore)
    return store, calls


class TestStoreOrderArgument:
    @pytest.mark.parametrize("method", ["rd", "rdp"])
    def test_descending_sends_order_and_returns_the_rows_as_the_engine_ordered_them(
        self, monkeypatch, method,
    ) -> None:
        store, calls = _store_with_transport(
            monkeypatch,
            lambda p, b: {"tuples": [_wire_row(3), _wire_row(2)], "order": "desc"},
        )
        rows = getattr(store, method)("mailbox/a", {"to": "a"}, n=2, order="desc")
        assert [r.body for r in rows] == ["m3", "m2"]
        assert calls[0][1]["order"] == "desc"

    @pytest.mark.parametrize("method", ["rd", "rdp"])
    def test_ascending_default_sends_no_order_field(self, monkeypatch, method) -> None:
        store, calls = _store_with_transport(monkeypatch, lambda p, b: {"tuples": [_wire_row(1)]})
        getattr(store, method)("mailbox/a", {"to": "a"}, n=1)
        assert "order" not in calls[0][1], "an old engine and every existing caller see the same bytes"

    @pytest.mark.parametrize("method", ["rd", "rdp"])
    def test_an_engine_that_did_not_echo_the_order_is_refused_not_trusted(
        self, monkeypatch, method,
    ) -> None:
        # An engine predating the field ignores it and answers ASCENDING with no echo.
        store, _ = _store_with_transport(
            monkeypatch, lambda p, b: {"tuples": [_wire_row(1), _wire_row(2)]},
        )
        with pytest.raises(DescendingReadUnsupportedError):
            getattr(store, method)("mailbox/a", {"to": "a"}, n=2, order="desc")

    def test_an_order_that_is_not_asc_or_desc_is_refused_before_sending(self, monkeypatch) -> None:
        store, calls = _store_with_transport(monkeypatch, lambda p, b: {"tuples": []})
        with pytest.raises(ValueError, match="order"):
            store.rd("mailbox/a", None, n=1, order="sideways")
        assert calls == []

    def test_the_unsupported_error_is_not_a_tuple_error(self) -> None:
        # Like ReplyNotWrittenError: a client-detected condition, so a broad
        # ``except TupleError`` (the engine's refusal codes) must not swallow it.
        from nexus.db.t2.http_tuple_store import TupleError  # noqa: PLC0415 — test-local import

        assert not issubclass(DescendingReadUnsupportedError, TupleError)


class _FakeStore:
    """Answers ``rd`` from a fixed ascending list, honouring ``order`` unless told not to."""

    def __init__(self, total: int, *, supports_desc: bool = True, cap: int = 300) -> None:
        self.rows = [
            TupleRow(id=f"{i:064x}", subspace="mailbox/a", template="mailbox/<address>",
                     keys={}, dims={}, body=f"m{i:04d}", claim_state=None, claimant=None,
                     lease_until=None, attempts=0, consumed_at=None, consumed_by=None,
                     expires_at=None, created_at=f"2026-09-29T10:00:{i % 60:02d}Z")
            for i in range(total)
        ]
        self.supports_desc = supports_desc
        self.cap = cap
        self.calls: list[dict[str, Any]] = []

    def rd(self, subspace, pattern, *, n=1, since=None, timeout_s=0, order="asc"):
        self.calls.append({"n": n, "since": since, "timeout_s": timeout_s, "order": order})
        pool = self.rows
        if since:
            pool = [r for r in pool if r.id > since[1]]
        n = min(n, self.cap)
        if order == "desc":
            if not self.supports_desc:
                raise DescendingReadUnsupportedError("engine predates order")
            return list(reversed(pool))[:n]
        return pool[:n]

    def close(self) -> None:
        pass


def _run_newest(monkeypatch, store: _FakeStore, *args: str):
    import nexus.commands.tuple_cmd as tc  # noqa: PLC0415 — test-local import

    monkeypatch.setattr(tc, "_store", lambda: store)
    res = CliRunner().invoke(tuple_group, ["rd", "mailbox/a", "--json", "--newest", *args])
    return res


class TestNewestCli:
    def test_one_descending_read_returns_the_newest_rows_oldest_first(self, monkeypatch) -> None:
        store = _FakeStore(20_000)  # far past the default --max-rows of 10000
        res = _run_newest(monkeypatch, store, "-n", "5")
        assert res.exit_code == 0, res.output
        bodies = [r["body"] for r in json.loads(res.stdout.strip().splitlines()[-1])]
        assert bodies == [f"m{i:04d}" for i in range(19_995, 20_000)], "the real newest, printed oldest to newest"
        assert [c["order"] for c in store.calls] == ["desc"], "no paging pass over the subspace"
        assert "truncated" not in res.stderr

    def test_max_rows_no_longer_hides_the_newest(self, monkeypatch) -> None:
        store = _FakeStore(500)
        res = _run_newest(monkeypatch, store, "-n", "3", "--max-rows", "100")
        assert res.exit_code == 0, res.output
        bodies = [r["body"] for r in json.loads(res.stdout.strip().splitlines()[-1])]
        assert bodies == ["m0497", "m0498", "m0499"]

    def test_an_engine_without_descending_reads_falls_back_to_paging_and_still_exits_3(
        self, monkeypatch,
    ) -> None:
        store = _FakeStore(500, supports_desc=False)
        res = _run_newest(monkeypatch, store, "-n", "3", "--max-rows", "100")
        assert res.exit_code == 3, res.output
        assert "not the newest" in res.stderr
        assert store.calls[0]["order"] == "desc", "descending is tried first"
        assert all(c["order"] == "asc" for c in store.calls[1:])

    def test_a_short_descending_page_is_rechecked_by_paging_not_trusted(self, monkeypatch) -> None:
        # Fewer rows than -n could mean the subspace is small OR that the engine's
        # read cap hid older rows; paging answers both, and is cheap when small.
        store = _FakeStore(7)
        res = _run_newest(monkeypatch, store, "-n", "20")
        assert res.exit_code == 0, res.output
        bodies = [r["body"] for r in json.loads(res.stdout.strip().splitlines()[-1])]
        assert bodies == [f"m{i:04d}" for i in range(7)]

    def test_n_above_the_read_cap_pages_when_the_cap_hides_rows(self, monkeypatch) -> None:
        store = _FakeStore(600, cap=300)
        res = _run_newest(monkeypatch, store, "-n", "400")
        assert res.exit_code == 0, res.output
        bodies = [r["body"] for r in json.loads(res.stdout.strip().splitlines()[-1])]
        assert bodies == [f"m{i:04d}" for i in range(200, 600)]

    def test_the_park_timeout_is_spent_once(self, monkeypatch) -> None:
        store = _FakeStore(0)
        res = _run_newest(monkeypatch, store, "-n", "1", "--timeout-s", "5")
        assert res.exit_code == 0, res.output
        parked = [c for c in store.calls if c["timeout_s"]]
        assert len(parked) == 1, f"a descending read that waited must not be followed by a second wait: {store.calls}"
