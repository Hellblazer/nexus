# SPDX-License-Identifier: AGPL-3.0-or-later
"""``tests/_chunk_seed.py`` (RDR-223 P3.1, bead nexus-z0o2p.23): the one place
a test builds a ``nexus.chunks`` row with no manifest row, now that the engine
refuses that write on ``/v1/vectors/upsert-chunks``, ``/store-put`` and
``/upsert-reference-only`` from Phase 3 on.

Real engine substrate (``t2_service_env``). What is pinned here is what every
moved test leans on: the row is physically stored, it has no live owner, it
lands in the tenant the ambient token names and no other, a repeat write merges
metadata, and the vector is the one the caller asked for.

The parity tests at the bottom drive the SAME input through ``upsert-chunks`` and
through the helper and compare every non-timestamp column. They can only run
while the route still accepts an ownerless write, which is the point: they pin
the helper to the route BEFORE nexus-z0o2p.24 (P3.2) makes the route refuse.
When P3.2 lands their route leg (``_both``) is refused by design; convert the
route leg to a write of owned chashes or delete the parity tests, keeping the
helper-only ones above them.
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


# ── parity with the route, pinned while the route still accepts ownerless writes ──

_ROUTE = "knowledge__chunkseed-route__bge-base-en-v15-768__v1"
_SEED = "knowledge__chunkseed-seed__bge-base-en-v15-768__v1"


def _psql_rows(sql: str) -> list:
    state = ensure_engine()
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _exec(sql: str) -> None:
    state = ensure_engine()
    proc = subprocess.run(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def _dump(tenant: str, collection: str) -> list[dict]:
    """Every column of every row in *collection* except the ones that name the
    collection or carry a wall-clock stamp, ordered by chash."""
    return _psql_rows(
        "SELECT to_jsonb(c) - 'collection' - 'created_at' - 'last_written_at' "
        f"FROM nexus.chunks c WHERE tenant_id = '{tenant}' AND collection = '{collection}' "
        "ORDER BY chash"
    )


def _last_written(tenant: str, collection: str, chash: str):
    [row] = _psql_rows(
        f"SELECT to_jsonb(c.last_written_at) FROM nexus.chunks c WHERE tenant_id = '{tenant}' "
        f"AND collection = '{collection}' AND chash = decode('{chash}', 'hex')"
    )
    return row


def _vec(x: float) -> list[float]:
    return [x] + [0.0] * 767


def _both(tenant: str, ids, docs, metas, vecs) -> None:
    """The same write through the route and through the helper."""
    hvc.HttpVectorClient(tenant=tenant).upsert_chunks(_ROUTE, list(ids), list(docs), list(metas), embeddings=vecs)
    seed_chunks_direct(_SEED, ids, docs, metas, embeddings=vecs)


def test_helper_rows_equal_the_routes_on_first_write_and_on_conflict(t2_service_env: str) -> None:
    a, b = _chash("parity a"), _chash("parity b")
    _both(t2_service_env, [a, b], ["parity a v1", "parity b v1"],
          [{"title": "a", "n": 1, "keep": "x"}, {}], [_vec(0.5), _vec(0.25)])
    first = _dump(t2_service_env, _ROUTE)
    assert first and first == _dump(t2_service_env, _SEED), "first write"

    # Conflict on `a`: text and vector replaced, metadata merged, `b` untouched.
    _both(t2_service_env, [a], ["parity a v2"], [{"title": "a2", "extra": True}], [_vec(0.75)])
    second = _dump(t2_service_env, _ROUTE)
    assert second != first, "non-vacuity: the conflict write changed something"
    assert second == _dump(t2_service_env, _SEED), "conflict write"
    [row_a] = [r for r in second if r["chunk_text"] == "parity a v2"]
    assert row_a["metadata"] == {"title": "a2", "n": 1, "keep": "x", "extra": True}


def test_duplicate_ids_in_one_call_collapse_first_wins_like_the_route(t2_service_env: str) -> None:
    a = _chash("dup a")
    _both(t2_service_env, [a, a], ["dup first", "dup second"], [{"w": 1}, {"w": 2}], [_vec(0.5), _vec(0.25)])
    seeded = _dump(t2_service_env, _SEED)
    assert seeded == _dump(t2_service_env, _ROUTE)
    assert [r["chunk_text"] for r in seeded] == ["dup first"]
    assert seeded[0]["metadata"] == {"w": 1}


def test_a_reseed_restamps_last_written_at_like_the_route(t2_service_env: str) -> None:
    a = _chash("stamp a")
    _both(t2_service_env, [a], ["stamp v1"], [{}], [_vec(0.5)])
    before = (_last_written(t2_service_env, _ROUTE, a), _last_written(t2_service_env, _SEED, a))
    _both(t2_service_env, [a], ["stamp v2"], [{}], [_vec(0.5)])
    after = (_last_written(t2_service_env, _ROUTE, a), _last_written(t2_service_env, _SEED, a))
    assert after[0] > before[0], "route restamps (the reference behavior)"
    assert after[1] > before[1], "helper restamps"


def test_a_conflict_resets_retention_to_full_like_the_route(t2_service_env: str) -> None:
    a = _chash("retention a")
    _both(t2_service_env, [a], ["retention v1"], [{}], [_vec(0.5)])
    for coll in (_ROUTE, _SEED):
        _exec(
            "UPDATE nexus.chunks SET retention = 'reference-only', chunk_text = NULL "
            f"WHERE tenant_id = '{t2_service_env}' AND collection = '{coll}' "
            f"AND chash = decode('{a}', 'hex')"
        )
    assert _dump(t2_service_env, _SEED)[0]["retention"] == "reference-only", "non-vacuity"
    _both(t2_service_env, [a], ["retention v2"], [{}], [_vec(0.5)])
    route_row, seed_row = _dump(t2_service_env, _ROUTE), _dump(t2_service_env, _SEED)
    assert seed_row == route_row
    assert seed_row[0]["retention"] == "full"


@pytest.mark.parametrize("width", [384, 1024])
def test_a_vector_of_the_wrong_width_is_refused_and_writes_nothing(t2_service_env: str, width: int) -> None:
    a = _chash(f"dim {width}")
    with pytest.raises(ValueError, match=f"width {width}.*routes to 768"):
        seed_chunks_direct(_SEED, [a], ["x"], embeddings=[[0.5] * width])
    assert _dump(t2_service_env, _SEED) == []


def test_an_explicit_tenant_must_be_the_ambient_one(t2_service_env: str) -> None:
    with pytest.raises(ValueError, match="NX_SERVICE_TOKEN is bound to"):
        seed_chunks_direct(_SEED, [_chash("t")], ["x"], tenant="not-" + t2_service_env)
    seed_chunks_direct(_SEED, [_chash("t")], ["x"], tenant=t2_service_env)
