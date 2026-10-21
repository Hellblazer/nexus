# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-222 Phase 1 (nexus-yye8l.13): the nexus-ll31n replay on a REAL engine substrate.

The incident: a bounded sweep committed server-side, the response was lost to the client's timeout,
and the caller read a completed 41,032-row move as a failure (and, before the auto-retry was
removed, resent it into a second sweep). The replay holds a keyed sweep past the client's socket
timeout and requires the client to report it committed, with the true count, after ONE execution.

How the sweep is held, with no engine seam: the test's own Postgres session takes the lock the sweep
waits on and keeps it for 1.5 s. For the quarantine form that is the RDR-191 sweep gate advisory lock
(the engine waits for it up to ``SWEEP_GATE_LOCK_TIMEOUT_MS`` = 2 s, so the sweep waits, proceeds and
commits after the client has timed out at 0.5 s). Restore takes no gate, so the restore variant holds a
row lock on one candidate quarantine row (its ``lock_timeout`` is 2 s). The client under test is fresh
(the first keyed call of the process: no echo seen, nothing cached).

Non-vacuity: the status polls are recorded, and the test fails unless one of them saw ``in_flight``
(the sweep really was still running after the client gave up) and the last saw ``committed``.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import nexus.db.http_vector_client as hvc
from nexus.catalog.chunk_quarantine import quarantine_collection_name
from nexus.catalog.http_catalog_client import HttpCatalogClient
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import _lit, _pg_state, _psql_superuser, ambient_tenant
from tests._engine_substrate import ensure_engine
from tests._reapable_age import age_chunks_past_grace
from tests._reapable_cli_fixture import _doc, chash_of, collection_name, write_chunks

SEEDED = 5
HOLD_S = 1.5
CLIENT_READ_TIMEOUT_S = 0.5
_QUARANTINED_AT = "2026-10-20T00:00:00Z"


@pytest.fixture
def env(t2_service_env, monkeypatch):
    monkeypatch.setattr(hvc, "_SWEEP_POST_TIMEOUT_S", CLIENT_READ_TIMEOUT_S)
    monkeypatch.setattr(hvc, "_STATUS_POLL_INTERVAL_S", 0.3)
    return hvc.HttpVectorClient(tenant=t2_service_env), ActiveCatalog()


class _PollSpy:
    """Records every status poll the client makes (and what the engine answered), delegating to the real ``_get``."""

    def __init__(self, monkeypatch) -> None:
        self.states: list[str] = []
        self.paths: list[str] = []
        real = hvc._get

        def spy(path, *, tenant="default"):
            result = real(path, tenant=tenant)
            if path.startswith("/v1/requests/"):
                self.paths.append(path)
                self.states.append(result.get("state"))
            return result

        monkeypatch.setattr(hvc, "_get", spy)


def _hold(sql: str, hold_s: float = HOLD_S) -> subprocess.Popen:
    """Run *sql* in a separate Postgres session as the cluster owner, and return once it is inside
    its ``pg_sleep(hold_s)`` (``HOLD_S`` unless given) -- the lock before it is then held."""
    state = _pg_state()
    proc = subprocess.Popen(
        [str(Path(state["pg_bin"]) / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", state["pg_user"], "-d", state["pg_dbname"], "-v", "ON_ERROR_STOP=1", "-A", "-t", "-f", "-"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdin is not None
    proc.stdin.write(sql)
    proc.stdin.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert proc.poll() is None, f"the holding session ended early: {proc.stderr.read() if proc.stderr else ''}"
        if _psql_superuser(
            state,
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' AND pid <> pg_backend_pid() "
            f"AND query LIKE '%pg_sleep({hold_s})%'",
        ) != "0":
            return proc
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("the holding session never reached its sleep: the lock was not taken")


def _engine_errors_since(offset: int) -> list[str]:
    log = Path(ensure_engine()["engine_log"])
    with open(log, "rb") as fh:
        fh.seek(offset)
        text = fh.read().decode("utf-8", errors="replace")
    return [ln for ln in text.splitlines() if "event=vector_handler_error" in ln]


def _engine_log_size() -> int:
    return Path(ensure_engine()["engine_log"]).stat().st_size


def _audit(*collections: str) -> list[dict]:
    cat = HttpCatalogClient()
    seen: dict[object, dict] = {}
    for coll in collections:
        for row in cat.gc_audit_list(collection=coll, limit=50):
            seen[row.get("id", id(row))] = row
    return list(seen.values())


def _count(coll: str) -> int:
    """Rows of *coll* in this tenant, by substrate SQL (the client's handle refuses a collection with no rows)."""
    return int(_psql_superuser(
        _pg_state(),
        f"SELECT count(*) FROM nexus.chunks WHERE tenant_id = {_lit(ambient_tenant())} AND collection = {_lit(coll)}",
    ))


def _assert_held_then_committed(spy: _PollSpy, elapsed: float) -> None:
    assert spy.paths, "the client must have asked GET /v1/requests/<id> at least once"
    assert "in_flight" in spy.states, (
        f"non-vacuity: the sweep was still running after the client's timeout (states seen {spy.states})"
    )
    assert spy.states[-1] == "committed", spy.states
    assert elapsed > CLIENT_READ_TIMEOUT_S, "the client's read timeout really expired"


def test_a_quarantine_sweep_held_past_the_clients_timeout_reports_committed_after_one_execution(env, monkeypatch):
    client, _cat = env
    coll = collection_name("yye8l13-q")
    quarantine = quarantine_collection_name(coll)
    hashes = write_chunks(coll, [f"yye8l13 quarantine orphan {i}: no document names it." for i in range(SEEDED)])
    assert len(set(hashes)) == SEEDED
    age_chunks_past_grace(coll)
    tenant = ambient_tenant()
    spy = _PollSpy(monkeypatch)
    log_offset = _engine_log_size()

    holder = _hold(
        f"SELECT pg_advisory_lock(hashtext({_lit(f'sweepgate:{tenant}/{coll}')}));\n"
        f"SELECT pg_sleep({HOLD_S});\n"
        f"SELECT pg_advisory_unlock(hashtext({_lit(f'sweepgate:{tenant}/{coll}')}));\n"
    )
    started = time.monotonic()
    try:
        result = client.gc_quarantine_orphans_bounded(coll, quarantine, _QUARANTINED_AT, 20, 100)
    finally:
        holder.wait(timeout=30)
    elapsed = time.monotonic() - started

    print(  # noqa: T201 -- the first live run's counts, for the bead (a test, not library code)
        f"ll31n replay (quarantine): seeded={SEEDED} moved={result.get('moved')} "
        f"polls={len(spy.paths)} states={spy.states} elapsed={elapsed:.2f}s"
    )
    _assert_held_then_committed(spy, elapsed)
    assert result["moved"] == SEEDED, result
    assert result.get("remaining") == 0
    assert _count(quarantine) == SEEDED, "every seeded row is in the quarantine collection exactly once"
    assert _count(coll) == 0
    rows = [r for r in _audit(coll, quarantine) if r["operation"] == "gc_quarantine_orphans_bounded"]
    assert len(rows) == 1, f"one execution, one gc_audit row: {rows}"
    assert rows[0]["chash_count"] == result["moved"]
    assert _engine_errors_since(log_offset) == [], "the engine logged a handler error (a 500) for the request"


def test_a_restore_sweep_held_past_the_clients_timeout_reports_committed_after_one_execution(env, monkeypatch):
    client, cat = env
    coll = collection_name("yye8l13-r")
    quarantine = quarantine_collection_name(coll)
    texts = [f"yye8l13 restore chunk {i}: quarantined, then named again." for i in range(SEEDED)]
    hashes = write_chunks(coll, texts)
    age_chunks_past_grace(coll)
    # The setup sweep (not under test, not held) runs on a separate client, so the client under test
    # makes its first keyed call in the held restore: the chunks leave the origin for the quarantine.
    setup_client = hvc.HttpVectorClient(tenant=client._tenant)
    setup = setup_client.gc_quarantine_orphans_bounded(coll, quarantine, _QUARANTINED_AT, 20, 100)
    assert setup["moved"] == SEEDED
    # The same text arrives again and a document names it: the quarantined copies are re-referenced.
    assert write_chunks(coll, texts) == hashes
    owner = cat.register_owner("yye8l13-owner", "curator")
    doc = cat.register(
        owner, "yye8l13-doc", content_type="knowledge", physical_collection=coll, meta={"doc_id": hashes[0]},
    )
    cat.append_manifest_chunks(str(doc), [{"chash": h, "position": i} for i, h in enumerate(hashes)], collection=coll)
    tenant = ambient_tenant()
    spy = _PollSpy(monkeypatch)
    log_offset = _engine_log_size()

    holder = _hold(
        "BEGIN;\n"
        f"SELECT 1 FROM nexus.chunks WHERE tenant_id = {_lit(tenant)} AND collection = {_lit(quarantine)} "
        f"AND chash = decode({_lit(hashes[0])}, 'hex') FOR UPDATE;\n"
        f"SELECT pg_sleep({HOLD_S});\n"
        "COMMIT;\n"
    )
    started = time.monotonic()
    try:
        result = client.gc_restore_rereferenced_bounded(quarantine, coll, 100)
    finally:
        holder.wait(timeout=30)
    elapsed = time.monotonic() - started

    print(  # noqa: T201 -- the first live run's counts, for the bead (a test, not library code)
        f"ll31n replay (restore): seeded={SEEDED} restored={result.get('restored')} "
        f"polls={len(spy.paths)} states={spy.states} elapsed={elapsed:.2f}s"
    )
    _assert_held_then_committed(spy, elapsed)
    assert result["restored"] == SEEDED, result
    assert result.get("remaining") == 0
    assert _count(quarantine) == 0, "the restore consumed the quarantine copies once"
    assert _count(coll) == SEEDED
    assert chash_of(texts[0]) == hashes[0]
    rows = [r for r in _audit(coll, quarantine) if "restore" in r["operation"]]
    assert len(rows) == 1, f"one execution, one gc_audit row: {rows}"
    assert rows[0]["chash_count"] == result["restored"]
    assert _engine_errors_since(log_offset) == [], "the engine logged a handler error (a 500) for the request"
