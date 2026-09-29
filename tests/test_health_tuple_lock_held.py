# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``tuples.lock_held`` doctor row (nexus-sis0m.7): a lock claim held, by
renewal, far past its lease.

The unit classes fake the HTTP store and the psql runner. The real-engine
class claims a real lock through the engine and backdates its claim-log row,
so the row's SQL is proved against the real tables and the real lint.
"""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path


import nexus.health as h
from nexus.db.diag_connection import DiagCredentials

_LOCK = "lock/<resource>"
_DIAG = DiagCredentials(port=54321, user="nexus_diag", password="x")


class _Census:
    def __init__(self, subspace: str, claimed: int) -> None:
        self.subspace = subspace
        self.claimed = claimed


class _Row:
    def __init__(self, claimant: str | None, lease_until: str | None, claim_state: str | None = "claimed") -> None:
        self.claimant = claimant
        self.lease_until = lease_until
        self.claim_state = claim_state


class _Store:
    def __init__(self, subspaces, rows=None, templates=None) -> None:
        self._subspaces = subspaces
        self._rows = rows or {}
        self._templates = templates if templates is not None else [
            {"name": _LOCK, "take": {"enabled": True, "max_lease_seconds": 900}, "lock": True},
            {"name": "queue/<topic>", "take": {"enabled": True, "max_lease_seconds": 900}, "lock": False},
        ]
        self.rd_calls: list[str] = []

    def subspace_list(self, prefix=None):
        return self._subspaces

    def registry(self):
        return {"templates": self._templates}

    def rd(self, subspace, keys_pattern, n=1, since=None, timeout_s=0):
        self.rd_calls.append(subspace)
        return self._rows.get(subspace, [])


def _future() -> str:
    return (datetime.now(UTC) + timedelta(minutes=10)).isoformat()


def _ago(**kw) -> str:
    return (datetime.now(UTC) - timedelta(**kw)).isoformat(sep=" ")


def _runner(stdout: str, calls: list[str] | None = None, returncode: int = 0):
    def run(argv, env):
        if calls is not None:
            calls.append(argv[-1])
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="boom" if returncode else "")
    return run


def _run(monkeypatch, store, **kw) -> h.HealthResult:
    monkeypatch.setattr(
        "nexus.db.t2.http_tuple_store.HttpTupleStore", lambda *a, **k: store, raising=False,
    )
    kw.setdefault("psql_bin", Path("/nonexistent/psql"))
    return h._check_tuple_lock_held(**kw)[0]


class TestLockHeldNotApplicable:
    def test_virgin_box_never_reaches_psql(self, monkeypatch, tmp_path) -> None:
        calls: list[str] = []
        r = _run(
            monkeypatch, _Store([]),
            creds_path=tmp_path / "absent", diag_runner=_runner("", calls),
        )
        assert r.ok is True and r.warn is not True
        assert "not applicable" in r.detail
        assert calls == []

    def test_claimed_non_lock_subspace_is_ignored(self, monkeypatch, tmp_path) -> None:
        store = _Store([_Census("queue/jobs", 3)])
        r = _run(monkeypatch, store, creds_path=tmp_path / "absent")
        assert r.ok is True and "not applicable" in r.detail
        assert store.rd_calls == []

    def test_lapsed_lease_is_not_held(self, monkeypatch, tmp_path) -> None:
        store = _Store(
            [_Census("lock/push", 1)],
            rows={"lock/push": [_Row("sess-a", _ago(minutes=1))]},
        )
        calls: list[str] = []
        r = _run(monkeypatch, store, diag_credentials=_DIAG, diag_runner=_runner("", calls))
        assert r.ok is True and "not applicable" in r.detail
        assert calls == []


class TestLockHeldMeasured:
    def test_hold_past_the_renewal_bound_is_a_finding(self, monkeypatch) -> None:
        store = _Store(
            [_Census("lock/push", 1)],
            rows={"lock/push": [_Row("sess-a", _future())]},
        )
        r = _run(
            monkeypatch, store,
            diag_credentials=_DIAG, diag_runner=_runner(_ago(hours=2)),
        )
        assert r.ok is False and r.warn is True
        assert "lock/push by sess-a" in r.detail
        assert "over 60m" in r.detail  # 4 x 900s

    def test_hold_inside_the_bound_is_ok_and_names_the_holder(self, monkeypatch) -> None:
        store = _Store(
            [_Census("lock/push", 1)],
            rows={"lock/push": [_Row("sess-a", _future())]},
        )
        r = _run(
            monkeypatch, store,
            diag_credentials=_DIAG, diag_runner=_runner(_ago(minutes=5)),
        )
        assert r.ok is True
        assert r.detail == "lock/push held 5m by sess-a"

    def test_missing_claim_row_is_a_finding_not_clean(self, monkeypatch) -> None:
        store = _Store(
            [_Census("lock/push", 1)],
            rows={"lock/push": [_Row("sess-a", _future())]},
        )
        r = _run(monkeypatch, store, diag_credentials=_DIAG, diag_runner=_runner(""))
        assert r.ok is False and r.warn is True
        assert "no claim row" in r.detail

    def test_diag_failure_is_reported_unmeasured(self, monkeypatch) -> None:
        store = _Store(
            [_Census("lock/push", 1)],
            rows={"lock/push": [_Row("sess-a", _future())]},
        )
        r = _run(
            monkeypatch, store,
            diag_credentials=_DIAG, diag_runner=_runner("", returncode=2),
        )
        assert r.ok is False and r.warn is True
        assert "not measured" in r.detail and "sess-a" in r.detail

    def test_no_diag_credentials_names_holder_and_does_not_claim_clean(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("nexus.config.is_local_mode", lambda: False)
        store = _Store(
            [_Census("lock/push", 1)],
            rows={"lock/push": [_Row("sess-a", _future())]},
        )
        r = _run(monkeypatch, store, creds_path=tmp_path / "absent")
        assert r.ok is False and r.warn is True
        assert "lock/push by sess-a" in r.detail
        assert "managed deployment" in r.detail

    def test_keyword_resource_and_quote_pass_the_lint_hex_encoded(self, monkeypatch) -> None:
        """A resource named ``do`` would trip the lint's mutating-word scan
        and a quote would break a quoted literal; neither reaches the SQL as
        text."""
        store = _Store(
            [_Census("lock/do", 1)],
            rows={"lock/do": [_Row("it's-me", _future())]},
        )
        calls: list[str] = []
        r = _run(
            monkeypatch, store,
            diag_credentials=_DIAG, diag_runner=_runner(_ago(minutes=1), calls),
        )
        assert r.ok is True, r.detail
        assert len(calls) == 1
        assert "lock/do" not in calls[0] and "it's-me" not in calls[0]
        assert "lock/do".encode().hex() in calls[0]


def test_row_is_registered_in_run_health_checks() -> None:
    import inspect

    assert "_check_tuple_lock_held()" in inspect.getsource(h.run_health_checks)


def test_row_absent_from_fresh_install_mvv_allowlist() -> None:
    mvv = Path(__file__).resolve().parent / "e2e" / "fresh-install-mvv.sh"
    assert "lock_held" not in mvv.read_text(encoding="utf-8")


class TestLockHeldRealEngine:
    """The row's SQL against the real tables: a real lock claim through the
    engine, its claim-log row backdated as the cluster superuser."""

    def _psql(self, state, sql: str) -> str:
        proc = subprocess.run(
            [str(state["pg_bin"] / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
             "-U", state["pg_user"], "-d", "nexus_t2_substrate", "-v", "ON_ERROR_STOP=1",
             "-tAc", sql],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    def test_backdated_claim_is_a_finding_and_a_fresh_one_is_not(
        self, t2_service_env, tmp_path,
    ) -> None:
        from nexus.db.t2.http_tuple_store import HttpTupleStore
        from tests._engine_substrate import ensure_engine

        state = ensure_engine()
        resource = f"sis0m7-{tmp_path.name}"
        subspace = f"lock/{resource}"
        claimant = f"holder-{tmp_path.name}"
        store = HttpTupleStore()
        store.out(subspace, {"resource": resource})
        claim = store.in_(subspace, {"resource": resource}, claimant=claimant, lease_s=600)
        assert claim is not None
        diag = DiagCredentials(
            port=state["pg_port"], user=state["pg_user"], password="",
            dbname="nexus_t2_substrate",
        )
        psql_bin = state["pg_bin"] / "psql"

        fresh = h._check_tuple_lock_held(diag_credentials=diag, psql_bin=psql_bin)[0]
        assert fresh.ok is True, fresh.detail
        assert f"{subspace} held" in fresh.detail and claimant in fresh.detail

        backdated = self._psql(
            state,
            "UPDATE nexus.tuple_claim_log SET at = now() - interval '3 hours' "
            f"WHERE claim_id = '{claim[1]}' AND transition = 'claim' RETURNING log_id;",
        )
        assert backdated, "the claim's claim-log row must exist to backdate"

        stale = h._check_tuple_lock_held(diag_credentials=diag, psql_bin=psql_bin)[0]
        assert stale.ok is False and stale.warn is True, stale.detail
        assert f"{subspace} by {claimant}" in stale.detail
        assert "held 3.0h" in stale.detail
