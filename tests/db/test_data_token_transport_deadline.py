# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mint's transport timeout is bounded by the caller's deadline (nexus-wfmfi).

A hook that passes ``bearer_for(deadline_s=...)`` (the SubagentStart T2 scan,
the dispatch recorder) used to bound every sleep of the mint but not the
attempt itself: ``_default_poster`` always waited ``_MINT_TIMEOUT_S`` (10 s).
The timeout of an attempt is now the shorter of the two.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from nexus.db import data_token
from nexus.db.data_token import DataTokenManager, DataTokenMintError

TENANT = "acme"


@contextmanager
def _silent_server() -> Iterator[str]:
    """A real local socket that accepts a connection and never answers (port 0)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                continue
            held.append(conn)

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    finally:
        stop.set()
        t.join(2.0)
        for c in held:
            c.close()
        srv.close()


def _real_manager(cfg: Path) -> DataTokenManager:
    return DataTokenManager(
        mint_credential=lambda: "mintcred", mint_tenant=lambda: "", config_dir=cfg, sleep=lambda s: None,
    )


def test_a_mint_against_a_server_that_never_answers_stops_at_the_callers_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real socket, real ``_default_poster``: deadline_s=1.0 against a silent server ends in about
    a second, not after the 10 s transport timeout."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    with _silent_server() as url:
        started = time.monotonic()
        with pytest.raises(DataTokenMintError, match="failed"):
            _real_manager(tmp_path).bearer_for(url, TENANT, deadline_s=1.0)
        elapsed = time.monotonic() - started
    assert elapsed < 4.0, elapsed  # the deadline (1 s), not the 10 s transport timeout
    assert elapsed >= 0.5, elapsed  # it did wait on the server: the pin is not vacuous


@pytest.mark.parametrize(
    ("remaining", "expected"),
    [
        (None, data_token._MINT_TIMEOUT_S),  # no deadline: unchanged
        (100.0, data_token._MINT_TIMEOUT_S),  # a deadline further than the timeout: unchanged
        (3.0, 3.0),
        (0.0, data_token._MINT_TRANSPORT_FLOOR_S),  # spent: a real round trip, never a zero timeout
    ],
)
def test_the_transport_timeout_is_the_shorter_of_the_default_and_the_time_left(
    remaining: float | None, expected: float,
) -> None:
    assert data_token._transport_timeout(remaining) == expected


def test_the_poster_sees_the_attempts_timeout_and_the_default_is_restored() -> None:
    """The timeout rides a context variable around each poster call and is reset after it."""
    seen: list[float] = []

    def poster(url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        seen.append(data_token._mint_transport_timeout.get())
        return 200, {"data_token": "tok", "expires_in_seconds": 300}, {}

    mgr = DataTokenManager(
        poster=poster, mint_credential=lambda: "mintcred", mint_tenant=lambda: "",
        sleep=lambda s: None, config_dir=Path("/nonexistent-nexus-wfmfi"),
    )
    with data_token.mint_deadline(2.0):
        mgr._mint("http://127.0.0.1:1", TENANT, "mintcred")
    mgr._mint("http://127.0.0.1:1", TENANT, "mintcred")
    assert 1.0 < seen[0] <= 2.0  # cut to what the deadline left
    assert seen[1] == data_token._MINT_TIMEOUT_S  # no deadline: the default
    assert data_token._mint_transport_timeout.get() == data_token._MINT_TIMEOUT_S  # reset after the call
