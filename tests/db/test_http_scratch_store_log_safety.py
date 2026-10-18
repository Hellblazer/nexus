# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpScratchStore`` construction logs ``host[:port]``, never the userinfo or
query of its base URL (nexus-q26w1).

``http_scratch_store.init`` is an INFO event on every T1 store construction,
and INFO is the default level of the MCP, console and hook log modes, so a
``service_url`` of ``https://user:password@host`` reached ``logs/mcp.log``
verbatim. Only this one default-path site is covered here; the other sites that
log a base URL are a separate bead.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
import structlog

from nexus.db.http_scratch_store import HttpScratchStore

PW = "SECRETPW-5d21"
QUERY = "SECRETQUERY-c3e8"


@pytest.fixture(autouse=True)
def info_logging() -> Iterator[None]:
    prior = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    try:
        yield
    finally:
        structlog.configure(wrapper_class=prior)


@pytest.mark.parametrize(
    "url",
    [
        pytest.param(f"http://user:{PW}@127.0.0.1:9/p?token={QUERY}", id="plain"),
        pytest.param("http://user:p%40ssSECRETPW@127.0.0.1:9", id="percent-encoded"),
        pytest.param("http://user:a:b:SECRETPW@127.0.0.1:9", id="colons"),
    ],
)
def test_store_construction_logs_host_and_port_only(url: str) -> None:
    with structlog.testing.capture_logs() as captured:
        HttpScratchStore(url, session_id="s", _token="tok-pinned")
    init = [e for e in captured if e["event"] == "http_scratch_store.init"]
    assert len(init) == 1
    assert init[0]["base_url"] == "127.0.0.1:9"
    rendered = repr(captured)
    for secret in ("SECRET", "user:", "p%40ss", "p@ss"):
        assert secret not in rendered, f"{secret!r} reached the log"
