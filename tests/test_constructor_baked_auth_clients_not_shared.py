# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-198 guard: the two constructor-baked-auth stores must never share a client.

WHY THIS EXISTS. Twelve of the fourteen httpx-backed stores build their
``Authorization`` header PER CALL (``RefreshableHttpStoreMixin._auth_headers``),
so their ``httpx.Client`` carries no credential and is safe to share. TWO do
not: :class:`HttpTokenStore` and :class:`HttpScratchStore` bake the header into
the ``httpx.Client`` CONSTRUCTOR and rebuild the client object whenever the
token changes.

Sharing one client between those two would send one domain's credential on the
other domain's request. Nothing does that today — each owns its own client — so
this is a TRAP, not a live bug, and this test exists to keep it that way.

RDR-198 investigated collapsing all fourteen onto one pooled transport and
DECIDED NOT TO (`docs/rdr/rdr-198-collapse-duplicated-client-transport.md`,
closed). Measured: the shared-pool benefit was 0.07% of an nx_answer call, the
indexer turned out not to construct stores per file at all (T2
`nexus_rdr/198-research-2`), and rewriting these two auth paths carried outage
risk for no measured gain (`198-research-3`). This ~40-line guard is what
shipped instead of that rewrite — it captures the only argument that survived.

If you are here because you want to share a transport: read the RDR first. The
conversion is possible, it is just not free, and the reason it was declined is
recorded rather than forgotten.
"""
from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.lint

_CONSTRUCTOR_BAKED = (
    ("nexus.db.t2.http_token_store", "HttpTokenStore"),
    ("nexus.db.http_scratch_store", "HttpScratchStore"),
)


def _build_client_source(module_name: str, cls_name: str) -> str:
    mod = __import__(module_name, fromlist=[cls_name])
    cls = getattr(mod, cls_name)
    return inspect.getsource(cls._build_client)


@pytest.mark.parametrize(("module_name", "cls_name"), _CONSTRUCTOR_BAKED)
def test_store_still_bakes_auth_into_its_own_client(module_name: str, cls_name: str):
    """Pins the PREMISE. If this fails, the store was converted to per-call
    auth — good — and this whole guard can be deleted for that store.

    Written this way deliberately: a guard whose premise has silently gone
    stale is worse than no guard, because it keeps asserting a shape nobody
    maintains any more.
    """
    src = _build_client_source(module_name, cls_name)
    assert "httpx.Client(" in src, f"{cls_name}._build_client no longer constructs a client"
    assert "Authorization" in src or "headers" in src, (
        f"{cls_name}._build_client no longer bakes auth into the constructor. "
        f"If it was converted to per-call auth, delete this store from "
        f"_CONSTRUCTOR_BAKED and from the RDR-198 guard entirely."
    )


def test_the_two_stores_do_not_share_a_client_factory():
    """The actual guard: these two must not be routed onto a common client.

    Detects the realistic regression — someone introduces a shared/singleton
    accessor and points both ``_build_client`` implementations at it. Today
    each constructs its own ``httpx.Client`` inline, which is what keeps the
    credentials separated.
    """
    sources = {
        cls_name: _build_client_source(module_name, cls_name)
        for module_name, cls_name in _CONSTRUCTOR_BAKED
    }
    for cls_name, src in sources.items():
        assert "httpx.Client(" in src, (
            f"{cls_name}._build_client no longer constructs its OWN client. "
            f"If it now calls a shared accessor, that shared client carries a "
            f"baked Authorization header and will leak this store's credential "
            f"onto the other store's requests. See RDR-198 and convert to "
            f"per-call auth BEFORE sharing, never after."
        )
        for shared_marker in ("shared_transport", "get_shared", "_SHARED", "singleton"):
            assert shared_marker not in src, (
                f"{cls_name}._build_client references {shared_marker!r}, which "
                f"suggests it was pointed at a shared client. That is the exact "
                f"cross-domain credential bleed RDR-198 declined to risk. "
                f"Convert to per-call auth first."
            )
