# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Consolidated config/env-resolution contract for the T2 HTTP stores
(test-suite-compression P1b, nexus-test-cleanup 2026-08-05).

``RefreshableHttpStoreMixin.__init__`` (``src/nexus/db/t2/_refreshable_client.py``)
resolves ``base_url`` from ``NX_SERVICE_URL`` / ``NX_SERVICE_HOST`` +
``NX_SERVICE_PORT`` and the bearer token from ``NX_SERVICE_TOKEN`` when
neither is passed explicitly to the constructor — every adopting store
raises the SAME ``RuntimeError`` shape when either is unresolvable.
``test_http_memory_store.py`` and ``test_http_telemetry_store.py``
independently pinned this identical behaviour (``TestAuthAndConfig`` /
``TestConfigErrors``); consolidated here as one parametrized pair.

nexus-aqbrk (see the per-file docstrings this superseded): NX_SERVICE_URL
/ NX_SERVICE_HOST are higher-priority resolution tiers than
NX_SERVICE_PORT, so leaving either set makes the endpoint resolve and the
missing-port assertion unreachable under the engine substrate, where
``t2_service_env`` re-sets URL + TOKEN after the conftest scrub. All four
vars are explicitly scrubbed before the missing-port assertion for that
reason.

Not folded in here: the three ``wrong-token raises RuntimeError(match=
"cannot self-heal")`` tests in ``test_http_telemetry_store.py``,
``test_http_plan_library.py``, ``test_http_chash_index.py``. They are the
same shape but each needs its own fake HTTP server fixture (a different
one per store); moving them would mean importing three sibling test
modules' private fake-handler classes for a ~15-line net reduction — not
worth the cross-file coupling. Left in place, one per store, as documented
in the P1b write-up (T1 scratch, tag test-cleanup,p1b).
"""
from __future__ import annotations

import pytest

from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

#: (label, store_cls) — every T2 Http*Store adopting RefreshableHttpStoreMixin
#: shares this constructor-time env-resolution contract. Sorted by label for
#: deterministic xdist collection order.
_ENV_RESOLUTION_CASES = sorted(
    [
        ("memory", HttpMemoryStore),
        ("telemetry", HttpTelemetryStore),
    ],
    key=lambda case: case[0],
)


@pytest.mark.parametrize(
    "store_cls", [cls for _label, cls in _ENV_RESOLUTION_CASES],
    ids=[label for label, _cls in _ENV_RESOLUTION_CASES],
)
def test_missing_port_raises(store_cls, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NX_SERVICE_URL", "NX_SERVICE_HOST",
                "NX_SERVICE_PORT", "NX_SERVICE_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(RuntimeError, match="NX_SERVICE_PORT"):
        store_cls()


@pytest.mark.parametrize(
    "store_cls", [cls for _label, cls in _ENV_RESOLUTION_CASES],
    ids=[label for label, _cls in _ENV_RESOLUTION_CASES],
)
def test_missing_token_raises(store_cls, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NX_SERVICE_PORT", "19999")
    monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="NX_SERVICE_TOKEN"):
        store_cls()
