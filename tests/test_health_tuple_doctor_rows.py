# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for the three RDR-205 tuple-space doctor rows (bead
nexus-em75s.10): ``_check_tuple_unclaimed_age``, ``_check_tuple_table_bloat``,
``_check_tuple_sweep_freshness``.

Fast, no subprocesses, no real PG, no network — the HTTP store (row 1) and
psql runner (rows 2/3) are injected/monkeypatched, mirroring
``TestCheckTopicsDocCountDrift`` and ``TestCheckMigrationState`` in
``tests/test_health_service_checks.py``.

Every row resolves severity through the SAME route_predates_floor gate as
``_check_manifest_null_collection`` (``TestCheckManifestNullCollection`` in
that file is the template these tests follow): below/at the frozen
``_TUPLE_ROUTE_FIRST_ENGINE_VERSION`` anchor, a route/table-absent result is
informational (ok=True); above it, the identical absence is a loud WARN.
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import nexus.engine_version as ev
import nexus.health as h


# ── row 1: _check_tuple_unclaimed_age ────────────────────────────────────────


class _FakeSubspace:
    def __init__(
        self, subspace: str, available: int, oldest_created_at: str | None = None,
        dead: int = 0,
    ) -> None:
        self.subspace = subspace
        self.available = available
        self.oldest_created_at = oldest_created_at
        self.dead = dead


class _FakeTupleRow:
    def __init__(self, id_: str, created_at: str | None, claim_state: str | None) -> None:
        self.id = id_
        self.created_at = created_at
        self.claim_state = claim_state


def _fake_template(name: str, *, take_enabled: bool = True) -> dict:
    return {"name": name, "take": {"enabled": take_enabled}}


class _FakeParkStats:
    def __init__(
        self, max_global: int, max_per_claimant: int, global_in_use: int,
        refused_global: int = 0, refused_claimant: int = 0, per_claimant: dict | None = None,
    ) -> None:
        self.max_global = max_global
        self.max_per_claimant = max_per_claimant
        self.global_in_use = global_in_use
        self.refused_global = refused_global
        self.refused_claimant = refused_claimant
        self.per_claimant = per_claimant or {}


class _FakeTupleStore:
    closed = False

    def __init__(
        self, subspaces=None, rd_by_subspace=None, list_exc=None,
        templates=None, rd_calls: list[str] | None = None, registry_exc=None,
        park_stats_result=None, park_stats_exc=None,
    ) -> None:
        self._subspaces = subspaces or []
        self._rd_by_subspace = rd_by_subspace or {}
        self._list_exc = list_exc
        self._templates = templates if templates is not None else []
        self._rd_calls = rd_calls
        self._registry_exc = registry_exc
        self._park_stats_result = park_stats_result
        self._park_stats_exc = park_stats_exc

    def subspace_list(self, prefix=None):
        if self._list_exc is not None:
            raise self._list_exc
        return self._subspaces

    def registry(self):
        if self._registry_exc is not None:
            raise self._registry_exc
        return {"templates": self._templates}

    def rd(self, subspace, keys_pattern, n=1, since=None, timeout_s=0):
        if self._rd_calls is not None:
            self._rd_calls.append(subspace)
        return self._rd_by_subspace.get(subspace, [])

    def park_stats(self):
        if self._park_stats_exc is not None:
            raise self._park_stats_exc
        return self._park_stats_result


def _run_unclaimed(monkeypatch, store) -> h.HealthResult:
    monkeypatch.setattr(
        "nexus.db.t2.http_tuple_store.HttpTupleStore",
        lambda *a, **k: store, raising=False,
    )
    return h._check_tuple_unclaimed_age()[0]


def _run_park_slots(monkeypatch, store) -> h.HealthResult:
    monkeypatch.setattr(
        "nexus.db.t2.http_tuple_store.HttpTupleStore",
        lambda *a, **k: store, raising=False,
    )
    return h._check_tuple_park_slots()[0]


def _run_queue_depth(monkeypatch, store) -> h.HealthResult:
    monkeypatch.setattr(
        "nexus.db.t2.http_tuple_store.HttpTupleStore",
        lambda *a, **k: store, raising=False,
    )
    return h._check_tuple_queue_depth()[0]


class TestCheckTupleUnclaimedAgeFloorGate:
    def test_route_missing_at_floor_is_loud_warn(self, monkeypatch) -> None:
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", h._TUPLE_ROUTE_FIRST_ENGINE_VERSION)
        exc = httpx.HTTPStatusError(
            "404", request=httpx.Request("POST", "http://x"),
            response=httpx.Response(404, request=httpx.Request("POST", "http://x")),
        )
        r = _run_unclaimed(monkeypatch, _FakeTupleStore(list_exc=exc))
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail

    def test_route_missing_below_floor_is_informational(self, monkeypatch) -> None:
        below = tuple(list(h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[:-1]) + [h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[-1] - 1])
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", below)
        exc = httpx.HTTPStatusError(
            "404", request=httpx.Request("POST", "http://x"),
            response=httpx.Response(404, request=httpx.Request("POST", "http://x")),
        )
        r = _run_unclaimed(monkeypatch, _FakeTupleStore(list_exc=exc))
        assert r.ok is True
        assert "informational" in r.detail

    def test_route_missing_above_floor_is_loud_warn(self, monkeypatch) -> None:
        above = tuple(list(h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[:-1]) + [h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[-1] + 1])
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", above)
        exc = httpx.HTTPStatusError(
            "404", request=httpx.Request("POST", "http://x"),
            response=httpx.Response(404, request=httpx.Request("POST", "http://x")),
        )
        r = _run_unclaimed(monkeypatch, _FakeTupleStore(list_exc=exc))
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail


class TestCheckTupleUnclaimedAgeBehavior:
    def test_engine_unreachable_at_construction(self, monkeypatch) -> None:
        def _raise(*a, **k):
            raise RuntimeError("no service registered")

        monkeypatch.setattr(
            "nexus.db.t2.http_tuple_store.HttpTupleStore", _raise, raising=False,
        )
        r = h._check_tuple_unclaimed_age()[0]
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail

    def test_engine_unreachable_on_list_call(self, monkeypatch) -> None:
        r = _run_unclaimed(monkeypatch, _FakeTupleStore(list_exc=ConnectionError("refused")))
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail

    def test_no_subspaces_is_ok(self, monkeypatch) -> None:
        r = _run_unclaimed(monkeypatch, _FakeTupleStore(subspaces=[]))
        assert r.ok is True
        assert r.detail == "no subspaces"

    def test_fresh_unclaimed_tuple_is_ok(self, monkeypatch) -> None:
        import datetime as _dt
        now = _dt.datetime.now(_dt.UTC).isoformat().replace("+00:00", "Z")
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("mailbox/a", available=1)],
            rd_by_subspace={"mailbox/a": [_FakeTupleRow("id1", now, None)]},
        )
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is True
        assert "mailbox/a=" in r.detail

    def test_stale_unclaimed_tuple_is_a_hard_finding(self, monkeypatch) -> None:
        import datetime as _dt
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("mailbox/a", available=1)],
            rd_by_subspace={"mailbox/a": [_FakeTupleRow("deadbeef" * 8, old, None)]},
        )
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is False and r.warn is not True
        assert "mailbox/a" in r.detail

    def test_claimed_rows_are_excluded(self, monkeypatch) -> None:
        import datetime as _dt
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=5)).isoformat().replace("+00:00", "Z")
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("mailbox/a", available=1)],
            rd_by_subspace={"mailbox/a": [_FakeTupleRow("id1", old, "claimed")]},
        )
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is True
        assert r.detail == "none"

    def test_zero_available_subspace_is_skipped(self, monkeypatch) -> None:
        store = _FakeTupleStore(subspaces=[_FakeSubspace("mailbox/a", available=0)])
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is True
        assert r.detail == "none"

    def test_take_disabled_ledger_subspace_with_day_old_rows_is_ok(self, monkeypatch) -> None:
        """nexus-em75s.12 review fix: a take.enabled=false template (e.g.
        ledger/<session_id>) is read-only by design -- rows are never
        claimed, so a day-old row must not trip the staleness finding.
        rd() must never even be called for this subspace."""
        import datetime as _dt
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        rd_calls: list[str] = []
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("ledger/sess1", available=1, oldest_created_at=old)],
            rd_by_subspace={"ledger/sess1": [_FakeTupleRow("id1", old, None)]},
            templates=[_fake_template("ledger/<session_id>", take_enabled=False)],
            rd_calls=rd_calls,
        )
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is True
        assert r.warn is not True
        assert "ledger/sess1" not in r.detail
        assert rd_calls == []

    def test_take_disabled_directory_subspace_with_day_old_rows_is_ok(self, monkeypatch) -> None:
        """RDR-208 Phase 1 Step 1 (bead nexus-galkv.1): directory/<name> is
        also take.enabled=false (a lease, re-sent by its holder, never
        claimed) -- same doctrine as ledger/<session_id> above. A day-old
        row must not trip the staleness finding, and rd() must never even
        be called for this subspace."""
        import datetime as _dt
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        rd_calls: list[str] = []
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("directory/agent-a", available=1, oldest_created_at=old)],
            rd_by_subspace={"directory/agent-a": [_FakeTupleRow("id1", old, None)]},
            templates=[_fake_template("directory/<name>", take_enabled=False)],
            rd_calls=rd_calls,
        )
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is True
        assert r.warn is not True
        assert "directory/agent-a" not in r.detail
        assert rd_calls == []

    def test_mailbox_subspace_with_hour_old_available_row_yields_existing_severity(self, monkeypatch) -> None:
        """The take-enabled path (mailbox/<address>) and the new census
        pre-filter must not change behavior for a row old enough to
        matter -- rd() is still called and the outcome matches the
        pre-fix severity (a hard finding, same as
        test_stale_unclaimed_tuple_is_a_hard_finding)."""
        import datetime as _dt
        hour_old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=1, seconds=5)).isoformat().replace("+00:00", "Z")
        rd_calls: list[str] = []
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("mailbox/a", available=1, oldest_created_at=hour_old)],
            rd_by_subspace={"mailbox/a": [_FakeTupleRow("id1", hour_old, None)]},
            templates=[_fake_template("mailbox/<address>", take_enabled=True)],
            rd_calls=rd_calls,
        )
        r = _run_unclaimed(monkeypatch, store)
        assert rd_calls == ["mailbox/a"]
        assert r.ok is False and r.warn is not True
        assert "mailbox/a" in r.detail

    def test_fresh_census_oldest_created_at_skips_the_rd_fetch(self, monkeypatch) -> None:
        """nexus-em75s.12 review fix: when the census's own oldest_created_at
        (spans ALL rows) is already younger than the staleness threshold,
        nothing in the subspace -- unclaimed included -- can possibly be
        stale, so the per-subspace rd(n=300) fetch is skipped entirely.

        nexus-em75s.42 review fix: this census-derived figure is an
        upper BOUND on every row's age (all rows, not verified to be the
        oldest UNCLAIMED row specifically -- computeCensus's
        oldest_created_at is a min() over live+claimed+dead+consumed
        rows), so it must be labelled differently from the verified
        per-row computation the rd() path reports (``=``,
        e.g. test_fresh_unclaimed_tuple_is_ok) -- ``<`` here, never
        ``=``, so a reader cannot mistake a census bound for a verified
        oldest-unclaimed age."""
        import datetime as _dt
        fresh = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        rd_calls: list[str] = []
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("mailbox/a", available=1, oldest_created_at=fresh)],
            rd_by_subspace={"mailbox/a": [_FakeTupleRow("id1", fresh, None)]},
            templates=[_fake_template("mailbox/<address>", take_enabled=True)],
            rd_calls=rd_calls,
        )
        r = _run_unclaimed(monkeypatch, store)
        assert rd_calls == []
        assert r.ok is True
        assert "mailbox/a<" in r.detail
        assert "mailbox/a=" not in r.detail

    def test_unmatched_subspace_defaults_to_checked(self, monkeypatch) -> None:
        """A subspace with no resolving template (registry unavailable or
        genuinely unmatched) must default to claimable/checked -- never
        silently skipped, matching TemplateRegistry.resolve()'s own
        "return null when nothing matches" being a caller-decides case,
        not an implicit take.enabled=false."""
        import datetime as _dt
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("mailbox/a", available=1, oldest_created_at=old)],
            rd_by_subspace={"mailbox/a": [_FakeTupleRow("deadbeef" * 8, old, None)]},
            templates=[],  # registry has no matching template
        )
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is False and r.warn is not True
        assert "mailbox/a" in r.detail

    def test_registry_exception_is_a_soft_warn_not_a_hard_fail(self, monkeypatch) -> None:
        """nexus-em75s.42 review fix: a registry() exception (a transient
        registry blip) must not fall through to checking every subspace as
        claimable -- that risks misreporting a take.enabled=false subspace
        (e.g. ledger/<session_id>, read-only by design, rows never
        claimed) as a stale-unclaimed HARD finding for a run where the
        registry merely blipped. A day-old ledger row with a live registry
        would never even be checked (test_take_disabled_ledger_subspace_
        with_day_old_rows_is_ok); here the registry raises, so the
        pre-fix code fell back to templates=[] and defaulted this
        subspace to checked, hard-failing the row on a transient blip.
        Must report warn=True instead, and never call rd()."""
        import datetime as _dt
        old = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=1)).isoformat().replace("+00:00", "Z")
        rd_calls: list[str] = []
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("ledger/sess1", available=1, oldest_created_at=old)],
            rd_by_subspace={"ledger/sess1": [_FakeTupleRow("id1", old, None)]},
            registry_exc=RuntimeError("registry temporarily unavailable"),
            rd_calls=rd_calls,
        )
        r = _run_unclaimed(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "templates could not be resolved" in r.detail
        assert rd_calls == []


# ── rows 2 and 3: psql-backed ────────────────────────────────────────────────


def _make_creds_file(tmp_path: Path, **overrides) -> Path:
    defaults = {
        "PG_PORT": "54321",
        "NX_DB_ADMIN_URL": "jdbc:postgresql://127.0.0.1:54321/nexus",
        "NX_DB_ADMIN_USER": "nexus_admin",
        "NX_DB_ADMIN_PASS": "testpass",
    }
    defaults.update(overrides)
    content = "\n".join(f"{k}={v}" for k, v in defaults.items() if v is not None) + "\n"
    p = tmp_path / "pg_credentials"
    p.write_text(content)
    return p


def _psql_runner(responder):
    def runner(cmd: list[str], *, capture_output: bool, text: bool, check: bool):
        sql = " ".join(cmd)
        return responder(sql, cmd)
    return runner


class TestCheckTupleTableBloat:
    def test_no_pg_credentials_local_mode(self, tmp_path) -> None:
        missing = tmp_path / "pg_credentials"
        with patch("nexus.config.is_local_mode", return_value=True):
            r = h._check_tuple_table_bloat(creds_path=missing)[0]
        assert r.ok is False and r.warn is True
        assert "pg_credentials absent" in r.detail

    def test_no_pg_credentials_managed_mode(self, tmp_path) -> None:
        missing = tmp_path / "pg_credentials"
        with patch("nexus.config.is_local_mode", return_value=False):
            r = h._check_tuple_table_bloat(creds_path=missing)[0]
        assert r.ok is False and r.warn is True
        assert "server-side" in r.detail

    def test_tables_absent_at_floor_is_loud_warn(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", h._TUPLE_ROUTE_FIRST_ENGINE_VERSION)
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        r = h._check_tuple_table_bloat(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail

    def test_tables_absent_above_floor_is_loud_warn(self, tmp_path, monkeypatch) -> None:
        above = tuple(list(h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[:-1]) + [h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[-1] + 1])
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", above)
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        r = h._check_tuple_table_bloat(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail

    def test_healthy_ratio_is_ok(self, tmp_path) -> None:
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="tuples|1000|5||\ntuple_claim_log|1000|10||\n", stderr="",
            )

        r = h._check_tuple_table_bloat(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is True
        assert "tuples" in r.detail

    def test_high_dead_ratio_is_a_hard_finding(self, tmp_path) -> None:
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0,
                stdout="tuples|100|900|2026-01-01 00:00:00|\n", stderr="",
            )

        r = h._check_tuple_table_bloat(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is not True
        assert "exceeds" in r.detail

    def test_engine_unreachable(self, tmp_path) -> None:
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=2, stdout="", stderr="connection refused")

        r = h._check_tuple_table_bloat(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail


class TestCheckTupleSweepFreshness:
    def test_no_pg_credentials_local_mode(self, tmp_path) -> None:
        missing = tmp_path / "pg_credentials"
        with patch("nexus.config.is_local_mode", return_value=True):
            r = h._check_tuple_sweep_freshness(creds_path=missing)[0]
        assert r.ok is False and r.warn is True
        assert "pg_credentials absent" in r.detail

    def test_table_absent_at_floor_is_loud_warn(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", h._TUPLE_ROUTE_FIRST_ENGINE_VERSION)
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="f\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail

    def test_table_absent_above_floor_is_loud_warn(self, tmp_path, monkeypatch) -> None:
        above = tuple(list(h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[:-1]) + [h._TUPLE_ROUTE_FIRST_ENGINE_VERSION[-1] + 1])
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", above)
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="f\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail

    def test_no_tenants_is_ok(self, tmp_path) -> None:
        creds = _make_creds_file(tmp_path)
        calls = {"n": 0}

        def responder(sql, cmd):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="t\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0|||\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is True
        assert "no tuple tenants" in r.detail

    def test_recent_sweep_is_ok(self, tmp_path) -> None:
        import datetime as _dt
        creds = _make_creds_file(tmp_path)
        recent = _dt.datetime.now(_dt.UTC).isoformat(sep=" ")
        calls = {"n": 0}

        def responder(sql, cmd):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="t\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"3|0|{recent}|0\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is True
        assert "3 tenant(s)" in r.detail

    def test_never_swept_tenants_are_informational(self, tmp_path) -> None:
        creds = _make_creds_file(tmp_path)
        calls = {"n": 0}

        def responder(sql, cmd):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="t\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="2|2||0\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is True
        assert "none swept yet" in r.detail
        assert "not persisted" in r.detail

    def test_stale_sweep_is_a_hard_finding(self, tmp_path) -> None:
        import datetime as _dt
        creds = _make_creds_file(tmp_path)
        stale = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=24)).isoformat(sep=" ")
        calls = {"n": 0}

        def responder(sql, cmd):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="t\n", stderr="")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"1|0|{stale}|1\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is not True
        assert "over the" in r.detail

    def test_one_fresh_and_one_stale_tenant_warns_on_the_laggard(self, tmp_path) -> None:
        """nexus-xapt8: the defect this row existed to catch. Before the
        MIN-vs-MAX fix, a query aggregating on MAX(last_swept_at) would
        report the FRESH tenant's recent sweep and the row would pass --
        masking the stale tenant entirely. The SQL under test is faked here
        (the responder does the MIN/stale-count arithmetic the real
        query would do server-side), so this pins the ROW's interpretation
        of that output, not Postgres's own aggregate behaviour."""
        import datetime as _dt
        creds = _make_creds_file(tmp_path)
        laggard = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=24)).isoformat(sep=" ")
        calls = {"n": 0}

        def responder(sql, cmd):
            calls["n"] += 1
            if calls["n"] == 1:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="t\n", stderr="")
            # total=2, never=0, MIN(last_swept_at)=the 24h-stale laggard
            # (not the fresh tenant's timestamp), stale_count=1.
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"2|0|{laggard}|1\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is not True
        assert "over the" in r.detail
        assert "1 stale" in r.detail

    def test_engine_unreachable(self, tmp_path) -> None:
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=2, stdout="", stderr="connection refused")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail


class TestCheckTupleSweepFreshnessRealPostgres:
    """Code review finding 8 / test-validator gap (nexus-xapt8 fix round):
    every test above fakes the ``psql_runner`` and hands the row a pre-baked
    SQL response string, so none of them proves the ACTUAL SQL text in
    ``_check_tuple_sweep_freshness`` computes ``MIN`` (versus, if someone
    reverted it, ``MAX``) correctly against a real ``nexus.tuple_tenants``
    table -- the exact defect class the row exists to catch. This class
    closes that gap: it runs the row's REAL SQL, through a REAL ``psql``
    binary, against the REAL Postgres the Python unit suite's own engine
    substrate boots for every test (``tests/_engine_substrate.py`` --
    the same cluster ``t2_service_env`` points the T2 HTTP client at, not a
    separate fixture). No ``psql_runner`` override anywhere in this class.

    Deliberately NOT folded into ``TestCheckTupleSweepFreshness`` above:
    that class's own docstring (this file's module docstring too) promises
    "no subprocesses, no real PG" for the row's LOGIC tests -- true of every
    test there, and worth keeping true. This is a second, explicitly-scoped
    layer for the SQL itself.
    """

    def test_min_based_staleness_against_a_real_tuple_tenants_table(
        self, t2_service_env, tmp_path,
    ) -> None:
        import datetime as _dt
        import subprocess as _subprocess

        from tests._engine_substrate import ensure_engine

        state = ensure_engine()
        psql_bin = state["pg_bin"] / "psql"
        pg_port = state["pg_port"]
        pg_user = state["pg_user"]
        dbname = "nexus_t2_substrate"  # tests/_engine_substrate.py's _DBNAME

        fresh_tenant = f"xapt8-fresh-{tmp_path.name}"
        stale_tenant = f"xapt8-stale-{tmp_path.name}"
        now = _dt.datetime.now(_dt.UTC)
        fresh_swept_at = now.isoformat(sep=" ")
        stale_swept_at = (now - _dt.timedelta(hours=24)).isoformat(sep=" ")

        # No RLS on nexus.tuple_tenants (tuples-001-3's own header: "this
        # table names tenants and holds no tenant-owned data of its own"),
        # so a plain INSERT as the cluster's own admin/superuser role needs
        # no tenant GUC. first_seen/last_seen are NOT NULL with no default.
        insert_sql = (
            "INSERT INTO nexus.tuple_tenants (tenant_id, first_seen, last_seen, last_swept_at) VALUES "
            f"('{fresh_tenant}', now(), now(), '{fresh_swept_at}'), "
            f"('{stale_tenant}', now(), now(), '{stale_swept_at}') "
            "ON CONFLICT (tenant_id) DO UPDATE SET last_swept_at = EXCLUDED.last_swept_at;"
        )
        proc = _subprocess.run(
            [str(psql_bin), "-h", "127.0.0.1", "-p", str(pg_port), "-U", pg_user,
             "-d", dbname, "-v", "ON_ERROR_STOP=1", "-c", insert_sql],
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"seeding nexus.tuple_tenants failed: {proc.stderr}"

        creds = _make_creds_file(
            tmp_path,
            PG_PORT=str(pg_port),
            NX_DB_ADMIN_URL=f"jdbc:postgresql://127.0.0.1:{pg_port}/{dbname}",
            NX_DB_ADMIN_USER=pg_user,
            NX_DB_ADMIN_PASS="",
        )

        # No psql_runner override: this is the row's REAL SQL running
        # through a REAL psql subprocess against the REAL table.
        r = h._check_tuple_sweep_freshness(creds_path=creds, psql_bin=psql_bin)[0]

        assert r.ok is False, (
            "the stale_tenant row (24h old) must make this row a hard finding via the "
            f"real MIN(last_swept_at) SQL, not the fresh_tenant's own recent sweep: {r.detail!r}"
        )
        assert r.warn is not True
        assert "over the" in r.detail
        assert stale_tenant not in r.detail  # detail carries counts, not tenant ids -- non-vacuity on the message shape
        assert "stale" in r.detail

    def test_engine_unreachable_bad_port_is_a_real_connection_failure(
        self, t2_service_env, tmp_path,
    ) -> None:
        """Companion non-vacuity check: the REAL psql binary against a port
        nothing listens on must produce the SAME 'engine unreachable' warn
        this file's mocked test asserts -- proving the mocked responder's
        shape (returncode != 0, stderr populated) is not a fiction of the
        test double."""
        from tests._engine_substrate import ensure_engine

        state = ensure_engine()
        psql_bin = state["pg_bin"] / "psql"
        dead_port = 1  # privileged, nothing listens; refused immediately, no timeout wait

        creds = _make_creds_file(
            tmp_path,
            PG_PORT=str(dead_port),
            NX_DB_ADMIN_URL=f"jdbc:postgresql://127.0.0.1:{dead_port}/nexus_t2_substrate",
            NX_DB_ADMIN_USER=state["pg_user"],
            NX_DB_ADMIN_PASS="",
        )
        r = h._check_tuple_sweep_freshness(creds_path=creds, psql_bin=psql_bin)[0]
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail


# ── registration ─────────────────────────────────────────────────────────────


def test_all_three_rows_are_registered_in_run_health_checks() -> None:
    import inspect

    source = inspect.getsource(h.run_health_checks)
    for fn_name in (
        "_check_tuple_unclaimed_age", "_check_tuple_table_bloat", "_check_tuple_sweep_freshness",
    ):
        assert f"{fn_name}()" in source, f"nx doctor must invoke {fn_name}()"


# ── floor-constant pin (nexus-em75s.12 review fix) ───────────────────────────


def test_tuple_route_first_engine_version_pin() -> None:
    """``_TUPLE_ROUTE_FIRST_ENGINE_VERSION`` must never sit ABOVE the newest
    published ``engine-service-v*`` tag this repo's git history knows about
    (that would name a tag that does not exist yet). This is the mechanical
    half of the comment above the constant's definition in
    ``src/nexus/health.py``.
    """
    # No lower bound against REQUIRED_ENGINE_VERSION: the constant names the
    # engine that FIRST carried the route (engine-service-v0.1.114) and stays
    # truthful as the floor moves past it (7.42.0 pinned v0.1.115). Below the
    # floor it means every reachable engine has the route and the doctor's
    # informational-skip branch is dead by construction, which is the intended
    # end state, not drift.

    import check_engine_release_floor as gate

    newest = gate.newest_published_engine()
    if newest is gate._TAGS_UNAVAILABLE:
        pytest.skip("git tags unavailable in this checkout (shallow clone with no tags fetched)")
    if newest is None:
        pytest.skip("no engine-service-v* tags found in this checkout's git history")
    assert h._TUPLE_ROUTE_FIRST_ENGINE_VERSION <= newest, (
        f"_TUPLE_ROUTE_FIRST_ENGINE_VERSION {h._TUPLE_ROUTE_FIRST_ENGINE_VERSION} names a "
        f"tag NEWER than any published engine-service-v* tag this repo knows about "
        f"({newest}) -- update it only once that tag actually exists and actually carries "
        "/v1/tuples."
    )


# ── RDR-211 Phase 1 Step 3 (bead nexus-rplay.12): park-slot use and queue ────
# depth ───────────────────────────────────────────────────────────────────────
#
# _TUPLE_PARK_STATS_FIRST_ENGINE_VERSION names engine-service-v0.1.127, the
# first tag serving GET /v1/tuples/park_stats. It was written one tag ahead
# while that cut was pending; the 7.51.0 release commit bumped
# REQUIRED_ENGINE_VERSION to the same value, and the pin test below now
# holds it to the same rule as _TUPLE_ROUTE_FIRST_ENGINE_VERSION: never
# above the newest published tag this checkout knows about.


def test_tuple_park_stats_first_engine_version_pin() -> None:
    """``_TUPLE_PARK_STATS_FIRST_ENGINE_VERSION`` must never sit ABOVE the
    newest published ``engine-service-v*`` tag this repo's git history knows
    about, and must equal the tag that first served the route (v0.1.127).
    """
    import check_engine_release_floor as gate

    assert h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION == (0, 1, 127), (
        "park_stats first shipped in engine-service-v0.1.127; the anchor is a fact about "
        "history, not a knob"
    )
    newest = gate.newest_published_engine()
    if newest is gate._TAGS_UNAVAILABLE:
        pytest.skip("git tags unavailable in this checkout (shallow clone with no tags fetched)")
    if newest is None:
        pytest.skip("no engine-service-v* tags found in this checkout's git history")
    assert h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION <= newest, (
        f"_TUPLE_PARK_STATS_FIRST_ENGINE_VERSION {h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION} "
        f"names a tag NEWER than any published engine-service-v* tag this repo knows about "
        f"({newest})."
    )


class TestCheckTupleParkSlots:
    def test_route_missing_below_floor_is_informational(self, monkeypatch) -> None:
        below = tuple(
            list(h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION[:-1])
            + [h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION[-1] - 1],
        )
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", below)
        exc = httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
        )
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_exc=exc))
        assert r.ok is True
        assert "predates the park report" in r.detail

    def test_route_missing_at_floor_is_loud_warn(self, monkeypatch) -> None:
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION)
        exc = httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
        )
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_exc=exc))
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail

    def test_route_missing_above_floor_is_loud_warn(self, monkeypatch) -> None:
        above = tuple(
            list(h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION[:-1])
            + [h._TUPLE_PARK_STATS_FIRST_ENGINE_VERSION[-1] + 1],
        )
        monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", above)
        exc = httpx.HTTPStatusError(
            "404", request=httpx.Request("GET", "http://x"),
            response=httpx.Response(404, request=httpx.Request("GET", "http://x")),
        )
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_exc=exc))
        assert r.ok is False and r.warn is True
        assert "UNKNOWN" in r.detail

    def test_engine_unreachable_at_construction(self, monkeypatch) -> None:
        def _raise(*a, **k):
            raise RuntimeError("no service registered")

        monkeypatch.setattr(
            "nexus.db.t2.http_tuple_store.HttpTupleStore", _raise, raising=False,
        )
        r = h._check_tuple_park_slots()[0]
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail

    def test_engine_unreachable_on_park_stats_call(self, monkeypatch) -> None:
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_exc=ConnectionError("refused")))
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail

    def test_below_75_percent_is_ok(self, monkeypatch) -> None:
        stats = _FakeParkStats(max_global=16, max_per_claimant=4, global_in_use=11)
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_result=stats))
        assert r.ok is True
        assert "11/16" in r.detail

    def test_at_75_percent_is_warn(self, monkeypatch) -> None:
        stats = _FakeParkStats(max_global=16, max_per_claimant=4, global_in_use=12)
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_result=stats))
        assert r.ok is False and r.warn is True
        assert "12/16" in r.detail

    def test_above_75_percent_is_warn(self, monkeypatch) -> None:
        stats = _FakeParkStats(max_global=16, max_per_claimant=4, global_in_use=15)
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_result=stats))
        assert r.ok is False and r.warn is True
        assert "15/16" in r.detail

    def test_never_hardcodes_16_as_the_cap(self, monkeypatch) -> None:
        """The cap is whatever the engine reports (max_global), never a
        client-side literal -- a differently-configured engine (e.g.
        max_global=32) must be judged against ITS OWN cap."""
        stats = _FakeParkStats(max_global=32, max_per_claimant=4, global_in_use=23)
        r = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_result=stats))
        assert r.ok is True  # 23/32 == 71.875%, below 75% of ITS OWN cap
        stats2 = _FakeParkStats(max_global=32, max_per_claimant=4, global_in_use=24)
        r2 = _run_park_slots(monkeypatch, _FakeTupleStore(park_stats_result=stats2))
        assert r2.ok is False and r2.warn is True  # 24/32 == 75%


def _queue_template(name: str, *, take_enabled: bool = True) -> dict:
    return {"name": name, "take": {"enabled": take_enabled}}


_ALL_TEMPLATE_KINDS = [
    _queue_template("board/<topic>", take_enabled=False),
    _queue_template("queue/<name>", take_enabled=True),
    _queue_template("lock/<resource>", take_enabled=True),
    _queue_template("mailbox/<address>", take_enabled=True),
    _queue_template("ledger/<session_id>", take_enabled=False),
    _queue_template("directory/<name>", take_enabled=False),
]


class TestCheckTupleQueueDepth:
    def test_no_queue_subspaces_is_not_applicable(self, monkeypatch) -> None:
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("mailbox/a", available=5)],
            templates=_ALL_TEMPLATE_KINDS,
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is True
        assert "informational" in r.detail
        assert r.ok is not False

    def test_healthy_queue_is_ok(self, monkeypatch) -> None:
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("queue/work", available=999, dead=0)],
            templates=_ALL_TEMPLATE_KINDS,
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is True
        assert "queue/work" in r.detail

    def test_over_1000_available_is_warn(self, monkeypatch) -> None:
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("queue/work", available=1001, dead=0)],
            templates=_ALL_TEMPLATE_KINDS,
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "queue/work" in r.detail

    def test_one_dead_task_is_warn_even_with_low_available(self, monkeypatch) -> None:
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("queue/work", available=999, dead=1)],
            templates=_ALL_TEMPLATE_KINDS,
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "queue/work" in r.detail

    def test_zero_dead_and_999_available_is_ok(self, monkeypatch) -> None:
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("queue/work", available=999, dead=0)],
            templates=_ALL_TEMPLATE_KINDS,
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is True

    def test_board_and_lock_subspaces_never_count(self, monkeypatch) -> None:
        """A board or lock subspace with a huge row count must never trip
        this row -- it answers a different question (RDR-211 Scale and
        Limits item 3 is about QUEUES specifically)."""
        store = _FakeTupleStore(
            subspaces=[
                _FakeSubspace("board/announcements", available=99999, dead=99999),
                _FakeSubspace("lock/resource-a", available=99999, dead=99999),
            ],
            templates=_ALL_TEMPLATE_KINDS,
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is True
        assert "informational" in r.detail

    def test_mixed_subspaces_only_queue_counts(self, monkeypatch) -> None:
        store = _FakeTupleStore(
            subspaces=[
                _FakeSubspace("board/announcements", available=99999, dead=99999),
                _FakeSubspace("queue/work", available=1500, dead=0),
            ],
            templates=_ALL_TEMPLATE_KINDS,
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is False and r.warn is True
        assert "queue/work" in r.detail
        assert "board/announcements" not in r.detail

    def test_engine_unreachable_at_construction(self, monkeypatch) -> None:
        def _raise(*a, **k):
            raise RuntimeError("no service registered")

        monkeypatch.setattr(
            "nexus.db.t2.http_tuple_store.HttpTupleStore", _raise, raising=False,
        )
        r = h._check_tuple_queue_depth()[0]
        assert r.ok is False and r.warn is True
        assert "engine unreachable" in r.detail

    def test_registry_failure_is_soft_warn(self, monkeypatch) -> None:
        store = _FakeTupleStore(
            subspaces=[_FakeSubspace("queue/work", available=5)],
            registry_exc=RuntimeError("registry blip"),
        )
        r = _run_queue_depth(monkeypatch, store)
        assert r.ok is False and r.warn is True


def test_rdr211_park_slots_and_queue_depth_rows_are_registered_in_run_health_checks() -> None:
    import inspect

    source = inspect.getsource(h.run_health_checks)
    for fn_name in ("_check_tuple_park_slots", "_check_tuple_queue_depth"):
        assert f"{fn_name}()" in source, f"nx doctor must invoke {fn_name}()"


def test_new_doctor_rows_absent_from_fresh_install_mvv_allowlist() -> None:
    """RDR-211 Phase 1 Step 3: these are new doctor rows, so per the
    nexus-7zhag doctrine (see the standing rule in the project's memory),
    they resolve not-applicable on a virgin box and must NEVER be added to
    ``tests/e2e/fresh-install-mvv.sh``'s doctor warnings allowlist -- a
    virgin box has no queue subspace and (today) an engine below the
    park-stats floor, so both rows are informational there already, with
    nothing to allowlist.
    """
    import re as _re
    from pathlib import Path as _Path

    mvv_path = _Path(__file__).resolve().parent.parent / "tests" / "e2e" / "fresh-install-mvv.sh"
    source = mvv_path.read_text(encoding="utf-8")
    match = _re.search(r"ALLOWLIST_REGEX='([^']*)'", source)
    assert match is not None, "fresh-install-mvv.sh must still define ALLOWLIST_REGEX"
    allowlist_regex = match.group(1)
    assert "park_slots" not in allowlist_regex
    assert "queue_depth" not in allowlist_regex
    assert "tuples.park_slots" not in source
    assert "tuples.queue_depth" not in source


# ── row 3: _check_tuple_channel_delivery (bead nexus-rplay.13, rewritten
# under RDR-213 -- the proof gate and claim-at-delivery facts are gone;
# `alive`/`last_wake`/`announced`/`pending`/`oldest_pending_age_s` replace
# `proof`/`unacked`/`released`) ─────────────────────────────────────────


def _write_status(config_dir: Path, session_id: str, **fields) -> None:
    from nexus.mcp.channel import write_channel_status

    base = {"alive": False, "last_wake": None, "announced": 0, "pending": 0, "oldest_pending_age_s": None}
    base.update(fields)
    write_channel_status(config_dir, session_id, base)


class TestCheckTupleChannelDelivery:
    def test_no_active_session_is_not_applicable(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: None)
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is True
        assert "informational" in r.detail
        assert "no active session" in r.detail

    def test_no_status_record_is_not_applicable(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is True
        assert "informational" in r.detail
        assert "no channel-waiter status recorded" in r.detail

    def test_alive_fresh_wake_is_ok(self, monkeypatch, tmp_path: Path) -> None:
        """RDR-213 deleted the proof gate: there is no "declared but
        unproven" state left -- an alive waiter with a fresh wake is
        simply OK."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=True, last_wake=now, announced=2, pending=1)
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is True
        assert r.warn is False
        assert "waiter alive" in r.detail
        assert "announced=2" in r.detail
        assert "pending=1" in r.detail

    def test_alive_nothing_announced_is_ok_the_flagless_launch_residue(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """bead nexus-gomuo.1: the only observable residue this unit test
        can prove for "session launched without the channel flag" (RDR-213
        Test Plan's ninth scenario). With the argv gate deleted, the
        waiter cannot itself tell a flagless launch apart from a live one
        that simply has no mail -- `send_channel_notification` sends
        unconditionally once the stdio write stream is up, and a flagless
        Claude Code drops the push silently. What IS observable here is
        the doctor row's state for a waiter that is alive and has ticked
        (`last_wake` fresh) but has announced nothing (`announced=0,
        pending=0`): informational, ok=True, never a WARN, because a
        session with no mail waiting looks identical whether or not the
        channel is heard. The host-layer half -- that a flagless real
        session gets no push at all and the drain hook renders at the
        next prompt -- belongs to the MVV (nexus-gomuo.3 part 2), not a
        unit test: asserting "nothing is pushed" here would either
        re-introduce the gate RDR-213 deletes or pass vacuously."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=True, last_wake=now, announced=0, pending=0)
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is True
        assert r.warn is False
        assert "waiter alive" in r.detail
        assert "announced=0" in r.detail
        assert "pending=0" in r.detail

    def test_pending_oldest_age_stated_when_pending(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(
            tmp_path, "sess-1", alive=True, last_wake=now, announced=1, pending=1,
            oldest_pending_age_s=42.0,
        )
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is True
        assert "pending=1" in r.detail
        assert "42s" in r.detail

    def test_zero_pending_never_states_an_age(self, monkeypatch, tmp_path: Path) -> None:
        """Mutation-check target: rendering an age when `pending` is 0
        (or `oldest_pending_age_s` is `None`) must fail this test."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=True, last_wake=now, announced=3, pending=0)
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is True
        assert "pending=0" in r.detail
        assert "oldest" not in r.detail

    def test_not_alive_is_warn(self, monkeypatch, tmp_path: Path) -> None:
        """Mutation-check target: dropping the `not alive` half of the
        WARN condition must fail this test."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now)
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is False and r.warn is True
        assert "not alive" in r.detail
        assert r.fix_suggestions

    def test_stale_wake_exactly_at_bound_is_not_warn(self, monkeypatch, tmp_path: Path) -> None:
        """Mutation-check target: the boundary is `>`, not `>=` -- exactly
        at the bound (3 x 25s = 75s) must still be OK. Uses the `now=`
        test seam so the comparison is pinned exactly rather than raced
        against wall-clock drift between writing the fixture and reading
        it back."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
        old = (fixed_now - timedelta(seconds=h._TUPLE_CHANNEL_DELIVERY_STALE_S)).isoformat()
        _write_status(tmp_path, "sess-1", alive=True, last_wake=old)
        r = h._check_tuple_channel_delivery(now=fixed_now)[0]
        assert r.ok is True, r.detail

    def test_stale_wake_past_bound_is_warn(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        fixed_now = datetime(2026, 1, 1, tzinfo=UTC)
        old = (fixed_now - timedelta(seconds=h._TUPLE_CHANNEL_DELIVERY_STALE_S + 1)).isoformat()
        _write_status(tmp_path, "sess-1", alive=True, last_wake=old)
        r = h._check_tuple_channel_delivery(now=fixed_now)[0]
        assert r.ok is False and r.warn is True
        assert "stale" in r.detail
        assert r.fix_suggestions

    def test_not_alive_no_stopped_reason_suggests_mcp_restart(self, monkeypatch, tmp_path: Path) -> None:
        """A dead waiter with no known cause (a crash, or an ordinary
        `cancel()` teardown never wrote a fresh record) gets the generic
        `/mcp` restart suggestion -- restarting is the only lever when
        there is no more specific cause to name."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now, stopped_reason=None)
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is False and r.warn is True
        assert any("/mcp" in s for s in r.fix_suggestions)
        assert not any("current engine" in s for s in r.fix_suggestions)

    def test_no_announce_support_names_the_cause_and_suggests_the_local_convergence_verbs(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """bead nexus-vsipz review round: `stopped_reason="no_announce_
        support"` means the engine this session is serving through
        predates announce mode -- the detail must name that (not the
        generic "not alive"), and the fix must point at the LIVE
        local-mode convergence verbs, never `/mcp` restart (a restart
        alone would hit the identical stale engine). nexus-6konb.15 (D1):
        the old text named `nx daemon service install a current engine`,
        which is not a real command -- `nx daemon service install`
        registers the autostart unit and installs no engine at all."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now, stopped_reason="no_announce_support")
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.service_endpoint.is_dev_checkout_process", return_value=False,
        ):
            r = h._check_tuple_channel_delivery()[0]
        assert r.ok is False and r.warn is True
        assert "announce_count" in r.detail
        assert any("nx daemon restart-stale" in s for s in r.fix_suggestions)
        assert any("nx daemon service install-binary" in s for s in r.fix_suggestions)
        assert not any("install a current engine" in s for s in r.fix_suggestions)
        assert not any(s == "Restart the MCP server: /mcp" for s in r.fix_suggestions)

    def test_no_announce_support_dev_checkout_also_gets_the_rebuild_advice(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """nexus-6konb.15 (D1): the dev-checkout rebuild advice is shown
        ONLY when this process itself is a dev checkout -- an installed
        user has no `service/` tree to rebuild from."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now, stopped_reason="no_announce_support")
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.service_endpoint.is_dev_checkout_process", return_value=True,
        ):
            r = h._check_tuple_channel_delivery()[0]
        assert any("dev checkout" in s and "rebuild" in s for s in r.fix_suggestions)
        assert any("nx daemon restart-stale" in s for s in r.fix_suggestions)

    def test_no_announce_support_cloud_mode_names_no_local_fix(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """nexus-6konb.15 (D1): a cloud-mode session has no local engine to
        install, converge, or rebuild -- the managed deployment is
        conexus's to upgrade, never this box's, so none of the local-mode
        verbs belong in a cloud user's fix suggestions."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now, stopped_reason="no_announce_support")
        with patch("nexus.config.is_local_mode", return_value=False):
            r = h._check_tuple_channel_delivery()[0]
        assert r.ok is False and r.warn is True
        assert not any("nx daemon restart-stale" in s for s in r.fix_suggestions)
        assert not any("install-binary" in s for s in r.fix_suggestions)
        assert not any("rebuild the local engine's cached build" in s for s in r.fix_suggestions)
        assert any("managed" in s and "conexus" in s for s in r.fix_suggestions)

    def test_no_subscriber_support_names_the_cause_and_suggests_the_local_convergence_verbs(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Bead nexus-q82tk: `stopped_reason="no_subscriber_support"` means
        the serving engine accepted `announce` but never read its
        `subscriber` (v0.1.128), the window between a client upgrade and
        a local engine's convergence. Named, with the live engine fix, and
        with the reassurance that mail still arrives."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now, stopped_reason="no_subscriber_support")
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.service_endpoint.is_dev_checkout_process", return_value=False,
        ):
            r = h._check_tuple_channel_delivery()[0]
        assert r.ok is False and r.warn is True
        assert "subscriber" in r.detail
        assert "drain hook" in r.detail
        assert any("nx daemon restart-stale" in s for s in r.fix_suggestions)
        assert any("nx daemon service install-binary" in s for s in r.fix_suggestions)
        assert not any("install a current engine" in s for s in r.fix_suggestions)
        assert not any(s == "Restart the MCP server: /mcp" for s in r.fix_suggestions)

    def test_superseded_names_the_duplicate_session_cause(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Bead nexus-rxuiq: a live waiter only ever sees `superseded` when a
        newer waiter for the same session exists, i.e. two nx-mcp processes
        serve one session. The engine is fine; the duplicate is the cause."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now, stopped_reason="superseded")
        r = h._check_tuple_channel_delivery()[0]
        assert r.ok is False and r.warn is True
        assert "newer channel waiter" in r.detail
        assert not any("current engine" in s for s in r.fix_suggestions)

    def test_no_wait_support_names_the_cause_and_suggests_the_local_convergence_verbs(
        self, monkeypatch, tmp_path: Path,
    ) -> None:
        """Same class as `no_announce_support` above, for an engine that
        predates `/wait` itself (a bare 404)."""
        monkeypatch.setattr("nexus.session.resolve_active_session_id", lambda: "sess-1")
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        now = datetime.now(UTC).isoformat()
        _write_status(tmp_path, "sess-1", alive=False, last_wake=now, stopped_reason="no_wait_support")
        with patch("nexus.config.is_local_mode", return_value=True), patch(
            "nexus.db.service_endpoint.is_dev_checkout_process", return_value=False,
        ):
            r = h._check_tuple_channel_delivery()[0]
        assert r.ok is False and r.warn is True
        assert "/wait" in r.detail
        assert any("nx daemon restart-stale" in s for s in r.fix_suggestions)
        assert not any("install a current engine" in s for s in r.fix_suggestions)
        assert not any(s == "Restart the MCP server: /mcp" for s in r.fix_suggestions)


def test_rdr211_channel_delivery_row_is_registered_in_run_health_checks() -> None:
    import inspect

    source = inspect.getsource(h.run_health_checks)
    assert "_check_tuple_channel_delivery()" in source, "nx doctor must invoke _check_tuple_channel_delivery()"


def test_new_channel_delivery_row_absent_from_fresh_install_mvv_allowlist() -> None:
    """Same nexus-7zhag doctrine as the park_slots/queue_depth rows above:
    a virgin box resolves not-applicable (no active session under the
    MVV's scrubbed env, or no status record), never a warning to
    allowlist."""
    import re as _re
    from pathlib import Path as _Path

    mvv_path = _Path(__file__).resolve().parent.parent / "tests" / "e2e" / "fresh-install-mvv.sh"
    source = mvv_path.read_text(encoding="utf-8")
    match = _re.search(r"ALLOWLIST_REGEX='([^']*)'", source)
    assert match is not None, "fresh-install-mvv.sh must still define ALLOWLIST_REGEX"
    allowlist_regex = match.group(1)
    assert "channel_delivery" not in allowlist_regex
    assert "tuples.channel_delivery" not in source
