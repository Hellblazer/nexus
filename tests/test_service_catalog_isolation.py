# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-1vt0b: the SERVICE-era catalog isolation regression suite.

History: RDR-060 (2026-04-08) added conftest's local-catalog isolation
after one integration test leaked 64 orphan ``int-cce-*`` owners into the
operator's REAL catalog; ``tests/test_catalog_isolation.py`` was its
regression suite. nexus-i711w deleted the local catalog AND that file, and
nothing replaced it — while the defect class had already fired a second
time in service form (2026-07-29: a badly isolated probe wrote a junk
entry into the live CLOUD store).

The service-era isolation stack is three autouse fixtures
(``_isolate_config_dir``, ``_isolate_service_endpoint_env``,
``_pin_t2_substrate``/``t2_service_env``) whose COMBINED effect is: every
test's catalog traffic lands on the self-provisioned local test engine,
under a per-test minted tenant, with the operator's real endpoint
unreachable by env AND by credential file. This suite asserts each layer
directly, so a future fixture edit cannot silently un-guard the class the
way the i711w deletion did.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_config_dir_is_never_the_operators_real_one() -> None:
    """Layer 1: credential-file resolution cannot reach ~/.config/nexus.

    ``get_credential("service_url")`` reads under ``nexus_config_dir()``;
    if that ever resolves to the real home during a test, the operator's
    cloud endpoint becomes reachable and every write becomes a potential
    64-orphan-owners incident."""
    from nexus.config import nexus_config_dir

    resolved = nexus_config_dir().resolve()
    real = (Path.home() / ".config" / "nexus").resolve()
    assert resolved != real, (
        "nexus_config_dir() resolved to the operator's REAL config dir "
        "inside a test — the _isolate_config_dir autouse fixture is no "
        "longer guarding (nexus-1vt0b / RDR-060 incident class)"
    )


def test_ambient_operator_endpoint_env_is_stripped_or_repinned(
    t2_service_env: str,
) -> None:
    """Layer 2: NX_SERVICE_URL/TOKEN inside a test are the SUBSTRATE's,
    never whatever the developer's shell had exported (the documented
    ``activate.sh`` workflow exports the REAL managed-service token)."""
    url = os.environ.get("NX_SERVICE_URL", "")
    assert url.startswith("http://127.0.0.1") or url.startswith("http://localhost"), (
        f"NX_SERVICE_URL inside a test is {url!r} — not the loopback test "
        "engine. Either _isolate_service_endpoint_env stopped stripping the "
        "ambient value or the substrate pin stopped re-pinning it."
    )
    # The bearer is the per-test minted tenant token — the engine binds
    # tenant to bearer server-side, so this IS the isolation boundary.
    assert os.environ.get("NX_SERVICE_TOKEN"), "substrate pin left no tenant token"


def test_catalog_write_lands_in_this_tests_tenant_only(t2_service_env: str) -> None:
    """Layer 3, the historical leak pattern run for real: a catalog write
    from a test must land under THIS test's minted tenant on the test
    engine — invisible to any other tenant, the operator included (the
    operator is just another tenant the bearer cannot reach)."""
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer

    w = make_catalog_writer()
    try:
        owner = w.register_owner("int-cce-isolation-probe", "curator")
        tumbler = w.register(owner, "nexus-1vt0b isolation probe")
    finally:
        w.close()
    assert tumbler is not None

    r = make_catalog_reader()
    try:
        entry = r.resolve(tumbler)
    finally:
        r.close()
    assert entry is not None and entry.title == "nexus-1vt0b isolation probe", (
        "the write did not land where the reader looks"
    )


def test_a_second_tenant_cannot_see_this_tenants_writes(
    t2_service_env: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-tenant invisibility half: junk written by one test is
    unreachable from a different tenant's bearer — which is exactly why
    the operator's catalog cannot accumulate test junk on this stack."""
    from tests._engine_substrate import ensure_engine, mint_test_tenant

    from nexus.catalog.factory import make_catalog_writer, make_catalog_reader

    w = make_catalog_writer()
    try:
        owner = w.register_owner("int-cce-crosstenant-probe", "curator")
        tumbler = w.register(owner, "nexus-1vt0b cross-tenant probe")
    finally:
        w.close()

    other_tenant, other_token = mint_test_tenant(ensure_engine())
    monkeypatch.setenv("NX_SERVICE_TOKEN", other_token)
    # The service catalog client is a shared singleton keyed at build time;
    # drop it so the reader below is rebuilt with the OTHER tenant's bearer.
    from nexus.catalog.factory import reset_shared_service_catalog_client_for_tests
    reset_shared_service_catalog_client_for_tests()
    r = make_catalog_reader()
    try:
        leaked = r.resolve(tumbler)
    finally:
        r.close()
    assert leaked is None, (
        f"tenant {other_tenant!r} can read another tenant's catalog write — "
        "the per-test-token isolation boundary is broken (nexus-1vt0b)"
    )
