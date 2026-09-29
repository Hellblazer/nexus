# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-kp5q3: a descending tuple read, so ``nx tuple rd --newest`` returns the
true newest rows however large the subspace, past ``--max-rows``.

Before this the engine could only order ``(created_at, id)`` ascending, so
``--newest`` paged oldest-first up to ``--max-rows`` and kept the tail, then
exited 3 when rows remained: a subspace past the bound could not return its
newest rows at all. The engine now takes ``order: "desc"`` on ``/rd`` and
``/rdp`` and echoes ``"order": "desc"`` plus the ``"limit"`` it ran with. The
echo is the capability signal: an engine that predates the field ignores the
unknown key and answers ascending with neither, which the client must not
mistake for a descending read. The limit (n clamped to the engine's read cap)
settles a short page without a second read: below the limit the subspace ran
out, exactly at it while n asked for more the cap trimmed it.

Store and CLI behaviour is proved here with a stubbed transport (no engine);
the real-engine round trip is in ``tests/test_tuple_cmd.py``.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.commands.tuple_cmd import tuple_group
from nexus.db.t2.http_tuple_store import (
    DescendingReadUnsupportedError,
    HttpTupleStore,
    NewestRead,
)
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
            lambda p, b: {"tuples": [_wire_row(3), _wire_row(2)], "order": "desc", "limit": 2},
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
    @pytest.mark.parametrize(
        "echo",
        [{}, {"order": "desc"}, {"limit": 2}, {"order": "asc", "limit": 2}, {"order": "desc", "limit": "2"}],
        ids=["no-echo", "order-only", "limit-only", "wrong-order", "non-int-limit"],
    )
    def test_a_response_without_the_full_echo_is_refused_not_trusted(
        self, monkeypatch, method, echo,
    ) -> None:
        # An engine predating the field ignores it and answers ASCENDING with no echo.
        store, _ = _store_with_transport(
            monkeypatch, lambda p, b: {"tuples": [_wire_row(1), _wire_row(2)], **echo},
        )
        with pytest.raises(DescendingReadUnsupportedError):
            getattr(store, method)("mailbox/a", {"to": "a"}, n=2, order="desc")

    def test_rd_newest_returns_the_page_with_the_engines_limit(self, monkeypatch) -> None:
        store, calls = _store_with_transport(
            monkeypatch,
            lambda p, b: {"tuples": [_wire_row(3), _wire_row(2)], "order": "desc", "limit": 300},
        )
        read = store.rd_newest("mailbox/a", {"to": "a"}, n=500, timeout_s=5)
        assert isinstance(read, NewestRead)
        assert [r.body for r in read.rows] == ["m3", "m2"]
        assert read.limit == 300
        assert calls[0][0] == "/rd" and calls[0][1]["order"] == "desc" and calls[0][1]["timeout_s"] == 5

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
    """Answers from a fixed ascending list, modelling the engine: ``rd_newest``
    is a descending read clamped to *cap* that echoes the limit it ran with,
    and on an engine without it (``supports_desc=False``) parks first, then
    answers with no echo, which the real store turns into the unsupported error."""

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
        #: ``(verb, n, timeout_s)`` per engine call, in order.
        self.calls: list[tuple[str, int, int]] = []

    def rd_newest(self, subspace, pattern, *, n=1, since=None, timeout_s=0):
        self.calls.append(("newest", n, timeout_s))
        if not self.supports_desc:
            raise DescendingReadUnsupportedError("engine predates order")
        pool = [r for r in self.rows if not since or r.id > since[1]]
        limit = min(n, self.cap)
        return NewestRead(rows=list(reversed(pool))[:limit], limit=limit)

    def rd(self, subspace, pattern, *, n=1, since=None, timeout_s=0):
        self.calls.append(("asc", n, timeout_s))
        pool = [r for r in self.rows if not since or r.id > since[1]]
        return pool[: min(n, self.cap)]

    def close(self) -> None:
        pass


def _run_newest(monkeypatch, store: _FakeStore, *args: str):
    import nexus.commands.tuple_cmd as tc  # noqa: PLC0415 — test-local import

    monkeypatch.setattr(tc, "_store", lambda: store)
    return CliRunner().invoke(tuple_group, ["rd", "mailbox/a", "--json", "--newest", *args])


def _bodies(res) -> list[str]:
    return [r["body"] for r in json.loads(res.stdout.strip().splitlines()[-1])]


class TestNewestCli:
    def test_one_descending_read_returns_the_newest_rows_oldest_first(self, monkeypatch) -> None:
        store = _FakeStore(20_000)  # far past the default --max-rows of 10000
        res = _run_newest(monkeypatch, store, "-n", "5")
        assert res.exit_code == 0, res.output
        assert _bodies(res) == [f"m{i:04d}" for i in range(19_995, 20_000)], "the real newest, oldest to newest"
        assert store.calls == [("newest", 5, 0)], "one descending read, no paging pass over the subspace"
        assert "truncated" not in res.stderr

    def test_max_rows_no_longer_hides_the_newest(self, monkeypatch) -> None:
        store = _FakeStore(500)
        res = _run_newest(monkeypatch, store, "-n", "3", "--max-rows", "100")
        assert res.exit_code == 0, res.output
        assert _bodies(res) == ["m0497", "m0498", "m0499"]

    def test_a_short_page_below_the_limit_is_the_whole_subspace_and_is_not_re_read(
        self, monkeypatch,
    ) -> None:
        # 7 rows, -n 20: the engine ran with limit 20 and returned 7, so the
        # subspace ran out. Trusting that must cost exactly ONE engine call; a
        # regression to "short means page again" shows as a second, asc call.
        store = _FakeStore(7)
        res = _run_newest(monkeypatch, store, "-n", "20")
        assert res.exit_code == 0, res.output
        assert _bodies(res) == [f"m{i:04d}" for i in range(7)]
        assert store.calls == [("newest", 20, 0)]
        assert "truncated" not in res.stderr

    def test_n_above_the_read_cap_keeps_the_capped_page_and_says_so(self, monkeypatch) -> None:
        # 600 rows, cap 300, -n 400: the engine ran with limit 300 and the page
        # is full at it. Those ARE the newest 300; the old behaviour paged up to
        # --max-rows and exited 3 here on a big subspace.
        store = _FakeStore(600, cap=300)
        res = _run_newest(monkeypatch, store, "-n", "400")
        assert res.exit_code == 0, res.output
        assert _bodies(res) == [f"m{i:04d}" for i in range(300, 600)]
        assert store.calls == [("newest", 400, 0)]
        assert "nx tuple rd: truncated" in res.stderr
        assert "asked for 400" in res.stderr and "read cap is 300" in res.stderr, res.stderr

    def test_n_equal_to_the_cap_is_not_reported_capped(self, monkeypatch) -> None:
        store = _FakeStore(600, cap=300)
        res = _run_newest(monkeypatch, store, "-n", "300")
        assert res.exit_code == 0, res.output
        assert len(_bodies(res)) == 300
        assert "truncated" not in res.stderr

    def test_an_engine_without_descending_reads_falls_back_to_paging_and_still_exits_3(
        self, monkeypatch,
    ) -> None:
        store = _FakeStore(500, supports_desc=False)
        res = _run_newest(monkeypatch, store, "-n", "3", "--max-rows", "100")
        assert res.exit_code == 3, res.output
        assert "not the newest" in res.stderr
        assert store.calls[0][0] == "newest", "descending is tried first"
        assert all(c[0] == "asc" for c in store.calls[1:]) and len(store.calls) > 1

    @pytest.mark.parametrize("supports_desc", [True, False], ids=["new-engine", "old-engine"])
    def test_the_park_timeout_is_spent_once_on_an_empty_subspace(
        self, monkeypatch, supports_desc: bool,
    ) -> None:
        # An engine without the read parks for the timeout BEFORE answering with
        # no echo, so the fallback must not park again on an empty subspace.
        store = _FakeStore(0, supports_desc=supports_desc)
        res = _run_newest(monkeypatch, store, "-n", "1", "--timeout-s", "5")
        assert res.exit_code == 0, res.output
        parked = [c for c in store.calls if c[2]]
        assert len(parked) == 1, f"the park must be spent once: {store.calls}"
