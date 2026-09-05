# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8hdg9 phase 2 critique remediation — cross-language default ordering.

T2 ``critique-nexus-8hdg9-p2-5ce59b36d`` [24651] finding 3: the Java engine's
``RequestDeadline.DEFAULT_DEADLINE_MS`` (the request embed deadline AuthFilter
mints when ``NX_EMBED_DEADLINE_MS`` is unset) must stay strictly BELOW the
Python client's ``_UPSERT_CHUNKS_TIMEOUT_S`` socket timeout
(``nexus.db.http_vector_client``). If it did not, the client's own socket
read would time out BEFORE the server's deadline ever fires, and the honest
503 the server was built to send instead of a silent hang would never be
reachable in practice.

There is no shared Java/Python constant file for this pair, so this gate
reads the JAVA SOURCE directly (regex on the constant declaration) rather
than hand-duplicating the number as a second literal that could silently
drift out of sync — mirrors ``tests/test_rdr155_p4b_deletion_gate.py``'s own
read-the-source-file-as-text convention for cross-language invariants.
"""
from __future__ import annotations

import re
from pathlib import Path

from nexus.db.http_vector_client import _UPSERT_CHUNKS_TIMEOUT_S

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUEST_DEADLINE_JAVA = (
    REPO_ROOT
    / "service"
    / "src"
    / "main"
    / "java"
    / "dev"
    / "nexus"
    / "service"
    / "http"
    / "RequestDeadline.java"
)


def _java_default_deadline_ms() -> int:
    assert REQUEST_DEADLINE_JAVA.is_file(), (
        f"expected {REQUEST_DEADLINE_JAVA} to exist -- if RequestDeadline.java "
        "moved, update this gate's path, don't delete the gate"
    )
    src = REQUEST_DEADLINE_JAVA.read_text()
    match = re.search(r"DEFAULT_DEADLINE_MS\s*=\s*(\d[\d_]*)L", src)
    assert match is not None, (
        "RequestDeadline.java's DEFAULT_DEADLINE_MS declaration shape changed "
        "-- update this gate's regex, don't silently skip the assertion"
    )
    return int(match.group(1).replace("_", ""))


def test_java_default_embed_deadline_stays_below_python_upsert_socket_timeout():
    java_default_ms = _java_default_deadline_ms()
    python_timeout_ms = _UPSERT_CHUNKS_TIMEOUT_S * 1000
    assert java_default_ms < python_timeout_ms, (
        f"RequestDeadline.DEFAULT_DEADLINE_MS ({java_default_ms}ms) must stay "
        f"strictly below the Python client's upsert-chunks socket timeout "
        f"({python_timeout_ms}ms) -- otherwise the client's own socket read "
        "would time out first, and the server's honest 503 would never be "
        "reachable"
    )
