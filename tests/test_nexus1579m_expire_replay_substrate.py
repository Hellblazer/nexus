# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-222 Amendment A1 (nexus-1579m): the nexus-ll31n replay for the BOUNDED, KEYED expire, on a REAL engine
substrate. Phase 1a's MVV, a ship gate for the engine tag that carries ``vectors-035``.

The pattern is ``tests/test_yye8l13_ll31n_replay_substrate.py`` (same fixture, same poll spy, same holding
session): a keyed sweep page is held past the client's socket timeout by a lock the test's own Postgres
session takes, and the client must resolve the lost response through ``GET /v1/requests/<key>``.

The expire form takes the RDR-191 sweep gate on its quarantine SIBLING (``sweepgate:<tenant>/<sibling>``) and
waits for it up to 2 s (``lock_timeout`` in ``nexus.gc_expire_quarantine_bounded``). Two replays:

* held 1.5 s: the page waits, takes the gate and commits after the client timed out at 0.5 s. The client polls,
  sees ``in_flight`` then ``committed``, returns the stored count, and does NOT resend: one POST, one audit row.
* held 3.5 s: the page's gate wait times out at 2 s, the engine answers 503 ``sweep_gate_busy`` (to a socket the
  client has closed) and marks the request ``rolled_back``. The client's poll sees that and resends under the
  SAME key; the resend meets the still-held gate, takes it at 3.5 s and commits. Two POSTs, one key, one audit
  row, the true count.

Non-vacuity: the status polls are recorded, and each test fails unless the states it needs were really seen.
"""
from __future__ import annotations

import time

import nexus.db.http_vector_client as hvc
from nexus.catalog.chunk_quarantine import expire_quarantine_bounded_serverside, quarantine_collection_name
from tests._chunk_seed import _lit, ambient_tenant
from tests._reapable_age import age_chunks_past_grace
from tests._reapable_cli_fixture import collection_name, write_chunks
from tests.test_yye8l13_ll31n_replay_substrate import (  # noqa: F401 -- `env` is a fixture used by name
    CLIENT_READ_TIMEOUT_S,
    HOLD_S,
    SEEDED,
    _audit,
    _count,
    _engine_errors_since,
    _engine_log_size,
    _hold,
    _PollSpy,
    _QUARANTINED_AT,
    env,
)

EXPIRE_PATH = "/v1/vectors/gc/expire-quarantine"
#: Past every quarantined_at the fixtures use, so every client-moved row is eligible.
CUTOFF = "2099-01-01T00:00:00Z"
#: Long enough that the page's own 2 s gate wait expires while the gate is still held.
LONG_HOLD_S = 3.5


class _PostSpy:
    """Records every POST the client makes to the expire route, delegating to the real ``_post``."""

    def __init__(self, monkeypatch) -> None:
        self.paths: list[str] = []
        real = hvc._post

        def spy(path, body, **kwargs):
            if path == EXPIRE_PATH:
                self.paths.append(path)
            return real(path, body, **kwargs)

        monkeypatch.setattr(hvc, "_post", spy)


def _quarantined(client, label: str) -> tuple[str, str]:
    """``(origin, sibling)`` with ``SEEDED`` client-moved rows in the sibling, tagged for the origin,
    quarantined at ``_QUARANTINED_AT`` and named by no manifest row: every one is eligible for the expire.
    The move runs on a separate client, so the client under test makes its first keyed call in the held expire."""
    coll = collection_name(label)
    quarantine = quarantine_collection_name(coll)
    write_chunks(coll, [f"{label} quarantined orphan {i}: no document names it." for i in range(SEEDED)])
    age_chunks_past_grace(coll)
    setup = hvc.HttpVectorClient(tenant=client._tenant).gc_quarantine_orphans_bounded(
        coll, quarantine, _QUARANTINED_AT, 20, 100,
    )
    assert setup["moved"] == SEEDED, setup
    assert _count(quarantine) == SEEDED
    return coll, quarantine


def _hold_the_gate(tenant: str, sibling: str, hold_s: float):
    """Take the RDR-191 sweep gate the expire page waits on (keyed on the SIBLING) for *hold_s* seconds."""
    key = _lit(f"sweepgate:{tenant}/{sibling}")
    return _hold(
        f"SELECT pg_advisory_lock(hashtext({key}));\n"
        f"SELECT pg_sleep({hold_s});\n"
        f"SELECT pg_advisory_unlock(hashtext({key}));\n",
        hold_s,
    )


def _expire_audit_rows(origin: str, sibling: str) -> list[dict]:
    return [r for r in _audit(origin, sibling) if r["operation"] == "gc_expire_quarantine_bounded"]


def test_an_expire_page_held_past_the_clients_timeout_reports_committed_after_one_execution(env, monkeypatch):
    client, _cat = env
    origin, sibling = _quarantined(client, "1579m-e1")
    tenant = ambient_tenant()
    spy = _PollSpy(monkeypatch)
    posts = _PostSpy(monkeypatch)
    log_offset = _engine_log_size()

    holder = _hold_the_gate(tenant, sibling, HOLD_S)
    started = time.monotonic()
    try:
        outcome = expire_quarantine_bounded_serverside(client, sibling, origin, CUTOFF, 100, strict=True)
    finally:
        holder.wait(timeout=30)
    elapsed = time.monotonic() - started

    print(  # noqa: T201 -- the first live run's counts, for the bead (a test, not library code)
        f"ll31n replay (expire, committed): seeded={SEEDED} outcome={outcome} posts={len(posts.paths)} "
        f"polls={len(spy.paths)} states={spy.states} elapsed={elapsed:.2f}s"
    )
    assert spy.paths, "the client must have asked GET /v1/requests/<id> at least once"
    assert "in_flight" in spy.states, (
        f"non-vacuity: the page was still running after the client's timeout (states seen {spy.states})"
    )
    assert spy.states[-1] == "committed", spy.states
    assert elapsed > CLIENT_READ_TIMEOUT_S, "the client's read timeout really expired"
    assert outcome == (SEEDED, 0, True), "the stored page body: every seeded row expired, none refused, counts whole"
    assert len(posts.paths) == 1, "the client did not re-run the page"
    assert _count(sibling) == 0, "the sibling holds nothing: the page deleted the rows once"
    rows = _expire_audit_rows(origin, sibling)
    assert len(rows) == 1, f"one execution, one gc_audit row: {rows}"
    assert rows[0]["chash_count"] == SEEDED
    assert _engine_errors_since(log_offset) == [], "the engine logged a handler error (a 500) for the request"


def test_an_expire_page_that_rolled_back_behind_a_held_gate_is_resent_under_its_key_and_commits_once(env, monkeypatch):
    client, _cat = env
    origin, sibling = _quarantined(client, "1579m-e2")
    tenant = ambient_tenant()
    spy = _PollSpy(monkeypatch)
    posts = _PostSpy(monkeypatch)
    log_offset = _engine_log_size()

    holder = _hold_the_gate(tenant, sibling, LONG_HOLD_S)
    started = time.monotonic()
    try:
        outcome = expire_quarantine_bounded_serverside(client, sibling, origin, CUTOFF, 100, strict=True)
    finally:
        holder.wait(timeout=30)
    elapsed = time.monotonic() - started

    print(  # noqa: T201 -- the first live run's counts, for the bead (a test, not library code)
        f"ll31n replay (expire, rolled back then resent): seeded={SEEDED} outcome={outcome} "
        f"posts={len(posts.paths)} polls={len(spy.paths)} states={spy.states} elapsed={elapsed:.2f}s"
    )
    assert "in_flight" in spy.states, f"non-vacuity: the first attempt was running after the timeout ({spy.states})"
    assert "rolled_back" in spy.states, (
        f"non-vacuity: the first attempt's gate wait timed out and the ledger said so ({spy.states})"
    )
    assert spy.states.index("in_flight") < spy.states.index("rolled_back")
    assert spy.states[-1] == "committed", spy.states
    assert elapsed > 2.0, "the first attempt waited out the engine's 2 s gate bound before it rolled back"
    assert len(posts.paths) == 2, "the original POST and exactly one resend (under the same key)"
    assert len(set(spy.paths)) == 1, f"one request key throughout: {set(spy.paths)}"
    assert outcome == (SEEDED, 0, True), "the resend's committed page, counted once"
    assert _count(sibling) == 0
    rows = _expire_audit_rows(origin, sibling)
    assert len(rows) == 1, f"the rolled-back attempt wrote no audit row; the resend wrote one: {rows}"
    assert rows[0]["chash_count"] == SEEDED
    assert _engine_errors_since(log_offset) == [], "the engine logged a handler error (a 500) for the request"
