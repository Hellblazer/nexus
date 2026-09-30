# SPDX-License-Identifier: AGPL-3.0-or-later
"""``tests/_chunk_seed.py`` (RDR-223 P3.1, bead nexus-z0o2p.23): the one place
a test builds a ``nexus.chunks`` row with no manifest row, now that the engine
refuses that write on ``/v1/vectors/upsert-chunks``, ``/store-put`` and
``/upsert-reference-only`` from Phase 3 on.

Real engine substrate (``t2_service_env``). What is pinned here is what every
moved test leans on: the row is physically stored, it has no live owner, it
lands in the tenant the ambient token names and no other, a repeat write merges
metadata, and the vector is the one the caller asked for.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import nexus.db.http_vector_client as hvc
from tests._chunk_seed import ambient_tenant, seed_chunks_direct
from tests._engine_substrate import ensure_engine, mint_test_tenant

_COLL = "knowledge__chunkseed__bge-base-en-v15-768__v1"


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _stored_row(tenant: str, chash: str) -> tuple[str, dict, list[float]]:
    """``(chunk_text, metadata, embedding)`` of one stored row, read as the
    substrate superuser: the engine's own reads return neither text nor vector
    for a chunk with no live owner."""
    state = ensure_engine()
    sql = (
        "SELECT json_build_object('t', chunk_text, 'm', metadata, 'e', embedding_768::text) "
        f"FROM nexus.chunks WHERE tenant_id = '{tenant}' AND collection = '{_COLL}' "
        f"AND chash = decode('{chash}', 'hex')"
    )
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    row = json.loads(proc.stdout.strip())
    return row["t"], row["m"], json.loads(row["e"])


def test_seeded_chunk_is_stored_but_has_no_live_owner(t2_service_env: str) -> None:
    text = "chunk seed: stored, ownerless"
    chash = _chash(text)
    seed_chunks_direct(_COLL, [chash], [text], [{"title": "seed"}])

    client = hvc.HttpVectorClient(tenant=t2_service_env)
    assert set(client.existing_ids(_COLL, [chash])) == {chash}, "physically stored"
    # live(c): a chunk with no live manifest row is hidden from content reads.
    assert client.get_collection(_COLL).get(ids=[chash], include=[])["ids"] == []
    assert client.get_collection(_COLL).get(ids=[chash], include=["metadatas"], include_non_live=True)["ids"] == [chash]


def test_seed_lands_in_the_ambient_tenant_only(t2_service_env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "chunk seed: tenant scoped"
    chash = _chash(text)
    seed_chunks_direct(_COLL, [chash], [text])
    assert ambient_tenant() == t2_service_env

    _tenant_b, token_b = mint_test_tenant(ensure_engine())
    monkeypatch.setenv("NX_SERVICE_TOKEN", token_b)
    # Registration is per-tenant and cached per process by name; a test that
    # switches tenants mid-body clears the cache, as tests/conftest.py does
    # between tests.
    import nexus.corpus as _corpus

    _corpus._REGISTERED_COLLECTIONS.clear()
    _corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    seed_chunks_direct(_COLL, [_chash("b only")], ["b only"])
    other = hvc.HttpVectorClient(tenant=_tenant_b)
    assert set(other.existing_ids(_COLL, [chash])) == set(), "tenant A's chunk is invisible to tenant B"
    assert set(other.existing_ids(_COLL, [_chash("b only")])) == {_chash("b only")}


def test_reseed_merges_metadata_and_replaces_text(t2_service_env: str) -> None:
    chash = _chash("v1")
    seed_chunks_direct(_COLL, [chash], ["v1"], [{"a": 1, "keep": "x"}])
    seed_chunks_direct(_COLL, [chash], ["v2"], [{"a": 2, "b": 3}])

    text, meta, _vec = _stored_row(t2_service_env, chash)
    assert text == "v2"
    assert (meta["a"], meta["b"], meta["keep"]) == (2, 3, "x"), meta


def test_embed_true_stores_the_engines_own_vector(t2_service_env: str) -> None:
    text = "chunk seed: a real vector"
    chash = _chash(text)
    seed_chunks_direct(_COLL, [chash], [text], embed=True)

    expected = hvc.HttpVectorClient(tenant=t2_service_env).embed_for_collection(_COLL, [text])[0]
    _text, _meta, stored = _stored_row(t2_service_env, chash)
    assert len(stored) == len(expected) == 768
    assert max(abs(a - b) for a, b in zip(stored, expected, strict=True)) < 1e-4
    assert any(abs(x) > 0 for x in stored), "not the zero vector"


def test_default_vector_is_zero_and_explicit_vectors_are_stored_verbatim(t2_service_env: str) -> None:
    zero, given = _chash("zero"), _chash("given")
    vec = [0.5] + [0.0] * 767
    seed_chunks_direct(_COLL, [zero], ["zero"])
    seed_chunks_direct(_COLL, [given], ["given"], embeddings=[vec])
    assert set(_stored_row(t2_service_env, zero)[2]) == {0.0}
    assert _stored_row(t2_service_env, given)[2] == vec


def test_a_malformed_chash_is_refused_before_any_sql() -> None:
    with pytest.raises(ValueError, match="64-char lowercase hex"):
        seed_chunks_direct(_COLL, ["abc"], ["x"], tenant="unused")
