# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-167 Step 4, the Minimum Viable Validation (nexus-dngtz.1.10): one ``nx`` command the engine
answers with a 4xx leaves the SAME correlation id in the client log and in the engine log.

The trigger is a 401 from a bearer the engine never minted (``AuthFilter`` ``unresolved_token``).
The bead's first candidate, a malformed document tumbler that reaches
``CatalogHandler.rejectIfBadDocumentTumbler`` (400), is not reachable from ``nx``: ``catalog show``
and ``catalog update`` parse or resolve the argument first (a bad tumbler becomes a title search and a
200 with no rows), ``--alias-of`` is parsed client-side, ``register`` sends no document tumbler
(the engine mints it), and the batch routes take only tumblers the client parsed. So the fallback applies. ``nx memory list`` goes through
``RefreshableHttpStoreMixin``, the class that sends ``client_identity_headers()``. The client retries the
401 once and then fails; both attempts carry the same id. The client line is DEBUG, so the command runs
with ``-v``.

Each half is asserted on its own, and the expected id is read from the CLIENT's stderr only, never from
the engine log, so a missing client line cannot be papered over by whatever id the engine minted itself
(``nxe-`` plus 32 hex when the request carries no usable header). The command runs in a subprocess so the
stderr is the real one and no logging configuration leaks into the test process.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._engine_substrate import ensure_engine

#: The client's ``engine_request_failed`` line as the CLI's structlog ``KeyValueRenderer`` writes it:
#: ``event='engine_request_failed' ... status=401 path='/v1/...' correlation_id='<32 hex>'`` (the id
#: ``nexus.correlation.new_correlation_id`` mints). The client retries a 401 once, so the line appears
#: twice, under the same id.
_CLIENT_LINE = re.compile(
    r"^event='engine_request_failed'(?=[^\n]*\bstatus=401\b)(?=[^\n]*\bcorrelation_id='(?P<cid>[0-9a-f]{32})')",
    re.MULTILINE,
)

#: The engine's wait is bounded, not a guess: logback is asynchronous, so the line can trail the
#: response by a few milliseconds. The loop returns the moment the line is there.
_ENGINE_LOG_DEADLINE_S = 20.0


def _engine_log_has(path: Path, needle: re.Pattern[str]) -> re.Match[str] | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return needle.search(text)


def _run_nx(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "from nexus.cli import main; main()", *args],
        env=env, capture_output=True, text=True, timeout=120, check=False,
    )


def test_one_4xx_leaves_one_correlation_id_in_both_logs() -> None:
    # No tenant is minted (the storage-touch ceiling counts tests that mint one): the bearer below never
    # resolves to a tenant, so the test needs only the engine, which ensure_engine() boots, and its URL.
    state = ensure_engine()
    engine_log = Path(state.get("engine_log") or os.path.join(state["pgdata"], "engine.log"))

    env = dict(os.environ)
    env["NX_SERVICE_URL"] = state["base_url"]
    env["NX_LOCAL"] = "1"
    # A well-formed bearer the engine never minted: AuthFilter answers 401 "unresolved_token".
    env["NX_SERVICE_TOKEN"] = "nx_" + "0" * 40

    result = _run_nx("-v", "memory", "list", "--project", "rdr167-mvv", env=env)

    # The command must have failed on the 401, not succeeded and not died elsewhere.
    assert result.returncode != 0, (
        f"the unknown-token command succeeded; the trigger is not firing.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    # Half 1: the client log carries a correlation id on the failed-request line.
    client = _CLIENT_LINE.search(result.stderr)
    assert client is not None, (
        "the client's stderr has no `event='engine_request_failed' status=401 correlation_id='<32 hex>'` line; "
        f"the client side of the pair is missing.\nstderr:\n{result.stderr}"
    )
    cid = client.group("cid")

    # Half 2: the engine log has request_failed for THAT id with the 4xx status. The cid comes from the
    # client's stderr above; the engine log is only searched for it. The log is shared by every test in
    # the worker, so the unique cid is the key, never the last line.
    needle = re.compile(
        rf"\[cid={cid} [^\]]*\][^\n]*event=request_failed[^\n]*\bstatus=401\b"
    )
    deadline = time.monotonic() + _ENGINE_LOG_DEADLINE_S
    found = _engine_log_has(engine_log, needle)
    while found is None and time.monotonic() < deadline:
        time.sleep(0.05)
        found = _engine_log_has(engine_log, needle)

    if found is None:
        engine_text = engine_log.read_text(encoding="utf-8", errors="replace") if engine_log.exists() else ""
        failed_lines = [ln for ln in engine_text.splitlines() if "event=request_failed" in ln][-5:]
        pytest.fail(
            f"the engine log {engine_log} has no `event=request_failed ... status=401` line under "
            f"cid={cid} (the id the client logged). Last request_failed lines:\n" + "\n".join(failed_lines)
        )
