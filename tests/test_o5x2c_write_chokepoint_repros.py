# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o5x2c (nexus-35ok4 round 4 SHIP-BLOCKER): regression tests for
the two substantive-critic live-repro'd crashes and the shared
resolve_write_embedding_model() chokepoint they exposed.

Repro shapes (local mode, local.embed_model voyage-shaped, NO
voyage_api_key configured, a pre-existing bge collection for the
target):

1. ``nx index repo`` on an already catalog-registered repo — hit
   ``catalog/http_catalog_client.py``'s ``collection_for_repo``
   directly, no ``t3_collection_name`` involved at all.
2. ``nx index md``/``pdf`` without ``--collection`` — hit
   ``corpus.docs_leaf_fallback_collection_name`` directly.

Both must grandfather onto the pre-existing bge collection instead of
raising ``LocalVoyageCredentialMissingError`` — the SAME truth table
``t3_collection_name`` already honored (round 2/3), now honored by
EVERY write entry point via the shared
:func:`nexus.corpus.resolve_write_embedding_model` chokepoint.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.corpus import (
    LOCAL_EMBEDDING_MODELS,
    LocalVoyageCredentialMissingError,
    resolve_write_embedding_model,
)


def _voyage_keyless_local(monkeypatch) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    # Selective: only voyage_api_key is pinned empty. Every OTHER
    # credential name (service_token, mint_token, ...) delegates to the
    # real resolver — a blanket non-empty string for every name would
    # ALSO satisfy HttpCatalogClient's mint_token check and trigger a
    # REAL outbound data-token mint attempt against the mock host.
    import nexus.config as _config_mod

    real_get_credential = _config_mod.get_credential

    def _get_credential(name: str) -> str:
        if name == "voyage_api_key":
            return ""
        return real_get_credential(name)

    monkeypatch.setattr("nexus.config.get_credential", _get_credential)


# ── resolve_write_embedding_model: the chokepoint itself ────────────────


def test_chokepoint_grandfathers_when_probe_finds_bge(monkeypatch) -> None:
    _voyage_keyless_local(monkeypatch)
    assert (
        resolve_write_embedding_model(
            "code", collection_exists=lambda token: token == "bge-base-en-v15-768",
        )
        == "bge-base-en-v15-768"
    )


def test_chokepoint_raises_when_probe_finds_nothing(monkeypatch) -> None:
    _voyage_keyless_local(monkeypatch)
    with pytest.raises(LocalVoyageCredentialMissingError, match="voyage-code-3"):
        resolve_write_embedding_model("code", collection_exists=lambda token: False)


def test_chokepoint_raises_when_no_probe_supplied(monkeypatch) -> None:
    _voyage_keyless_local(monkeypatch)
    with pytest.raises(LocalVoyageCredentialMissingError):
        resolve_write_embedding_model("code")


def test_chokepoint_probe_exception_degrades_to_strict_not_crash(monkeypatch) -> None:
    """A broken probe (network error, missing method on a test double)
    must degrade to the strict/raising fallback, never propagate its own
    exception type out of the chokepoint."""
    _voyage_keyless_local(monkeypatch)

    def _broken(token: str) -> bool:
        raise RuntimeError("probe substrate unavailable")

    with pytest.raises(LocalVoyageCredentialMissingError):
        resolve_write_embedding_model("code", collection_exists=_broken)


# ── Repro 1: nx index repo on an already-registered repo ────────────────
# catalog/http_catalog_client.py's collection_for_repo, hit directly —
# no t3_collection_name / T3 vector client involved at all.


def _mock_catalog_client(monkeypatch, *, registered_model: str | None):
    """A real HttpCatalogClient wired to a MockTransport. Serves an owner
    for /owners/by_repo, and answers /collections/for_tuple with a
    registered v1 collection when embedding_model == *registered_model*,
    404 otherwise (nexus-o5x2c: the catalog-tier's OWN existence-probe
    substrate — collection_for_repo has no T3 vector client to ask)."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    requests: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append((request.url.path, params))
        if request.url.path == "/v1/catalog/owners/by_repo":
            return httpx.Response(200, json={"tumbler_prefix": "1.1"})
        if request.url.path == "/v1/catalog/collections/for_tuple":
            model = params.get("embedding_model")
            if registered_model is not None and model == registered_model:
                return httpx.Response(
                    200,
                    json={"name": f"code__1-1__{model}__v1"},
                )
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={})

    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    client = HttpCatalogClient(base_url="http://mock.test")
    client._client = httpx.Client(
        base_url="http://mock.test",
        transport=httpx.MockTransport(handler),
    )
    return client, requests


def test_collection_for_repo_grandfathers_onto_registered_bge(
    tmp_path, monkeypatch,
) -> None:
    """THE repro: `nx index repo` on a repo whose catalog already has a
    registered bge-base-en-v15-768 collection, local.embed_model
    switched to voyage-code-3, no key configured. Must return the
    EXISTING bge collection, never raise."""
    _voyage_keyless_local(monkeypatch)
    client, _ = _mock_catalog_client(
        monkeypatch, registered_model="bge-base-en-v15-768",
    )

    result = client.collection_for_repo(tmp_path, "code")

    assert result.embedding_model == "bge-base-en-v15-768"
    assert result.render() == "code__1-1__bge-base-en-v15-768__v1"


def test_collection_for_repo_raises_when_nothing_registered(
    tmp_path, monkeypatch,
) -> None:
    """Same keyless-voyage config, but nothing at all is registered for
    this repo yet — a genuine new mint, correctly fails loud."""
    _voyage_keyless_local(monkeypatch)
    client, _ = _mock_catalog_client(monkeypatch, registered_model=None)

    with pytest.raises(LocalVoyageCredentialMissingError, match="voyage-code-3"):
        client.collection_for_repo(tmp_path, "code")


def test_collection_for_repo_mints_voyage_when_key_present(
    tmp_path, monkeypatch,
) -> None:
    """Symmetric with the docs table: key PRESENT + a registered bge
    collection -> targets voyage (new sibling), does NOT grandfather —
    round 3's gate applies here too, via the same chokepoint."""
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)
    monkeypatch.setattr("nexus.config.local_embed_model_choice", lambda: "voyage-code-3")
    monkeypatch.setattr("nexus.config.local_embed_model_is_voyage", lambda: True)
    import nexus.config as _config_mod
    real_get_credential = _config_mod.get_credential
    monkeypatch.setattr(
        "nexus.config.get_credential",
        lambda name: "configured-key" if name == "voyage_api_key" else real_get_credential(name),
    )
    client, _ = _mock_catalog_client(
        monkeypatch, registered_model="bge-base-en-v15-768",
    )

    result = client.collection_for_repo(tmp_path, "code")

    assert result.embedding_model == "voyage-code-3"


# ── Repro 2: nx index md/pdf without --collection ────────────────────────
# corpus.docs_leaf_fallback_collection_name, hit directly.


def test_docs_leaf_fallback_grandfathers_onto_bge(monkeypatch) -> None:
    """THE repro: `nx index md <file>` with no --collection, a
    pre-existing docs__art__bge-base-en-v15-768__v1 collection,
    local.embed_model=voyage-code-3, no key. Must grandfather, not
    raise."""
    from nexus.corpus import docs_leaf_fallback_collection_name

    _voyage_keyless_local(monkeypatch)
    existing = {"docs__art__bge-base-en-v15-768__v1"}

    name = docs_leaf_fallback_collection_name(
        "art", collection_exists=lambda n: n in existing,
    )

    assert name == "docs__art__bge-base-en-v15-768__v1"


def test_docs_leaf_fallback_raises_when_nothing_preexisting(monkeypatch) -> None:
    from nexus.corpus import docs_leaf_fallback_collection_name

    _voyage_keyless_local(monkeypatch)
    with pytest.raises(LocalVoyageCredentialMissingError, match="voyage-code-3"):
        docs_leaf_fallback_collection_name("art", collection_exists=lambda n: False)


def test_docs_leaf_fallback_diagnostic_call_stays_strict(monkeypatch) -> None:
    """The nexus-2t63u diagnostic comparison (no collection_exists passed
    at all) must NOT grandfather — it needs the STRICT "expected" name
    to detect a genuine mismatch, so it keeps raising here by design."""
    from nexus.corpus import docs_leaf_fallback_collection_name

    _voyage_keyless_local(monkeypatch)
    with pytest.raises(LocalVoyageCredentialMissingError):
        docs_leaf_fallback_collection_name("art")


# ── Shared truth table applies uniformly across both repro sites ────────


def test_both_repro_sites_and_t3_collection_name_agree(monkeypatch, tmp_path) -> None:
    """Both round-4 fixes and t3_collection_name's own write path
    (round 2/3) resolve to the SAME model token for an equivalent
    keyless-voyage + pre-existing-bge scenario — proving ONE shared
    truth table, not three independently-agreeing copies."""
    from nexus.corpus import docs_leaf_fallback_collection_name, t3_collection_name

    _voyage_keyless_local(monkeypatch)

    class _FakeT3:
        def collection_exists(self, name: str) -> bool:
            return name == "knowledge__art__bge-base-en-v15-768__v1"

    t3_name = t3_collection_name("art", t3=_FakeT3(), for_write=True)
    docs_name = docs_leaf_fallback_collection_name(
        "art",
        collection_exists=lambda n: n == "docs__art__bge-base-en-v15-768__v1",
    )
    catalog_client, _ = _mock_catalog_client(
        monkeypatch, registered_model="bge-base-en-v15-768",
    )
    catalog_model = catalog_client.collection_for_repo(tmp_path, "code").embedding_model

    assert t3_name.split("__")[2] == "bge-base-en-v15-768"
    assert docs_name.split("__")[2] == "bge-base-en-v15-768"
    assert catalog_model == "bge-base-en-v15-768"
    assert LOCAL_EMBEDDING_MODELS == {"minilm-l6-v2-384", "bge-base-en-v15-768"}
