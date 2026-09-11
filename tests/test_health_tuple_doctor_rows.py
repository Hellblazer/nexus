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

import subprocess
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

import nexus.engine_version as ev
import nexus.health as h


# ── row 1: _check_tuple_unclaimed_age ────────────────────────────────────────


class _FakeSubspace:
    def __init__(self, subspace: str, available: int, oldest_created_at: str | None = None) -> None:
        self.subspace = subspace
        self.available = available
        self.oldest_created_at = oldest_created_at


class _FakeTupleRow:
    def __init__(self, id_: str, created_at: str | None, claim_state: str | None) -> None:
        self.id = id_
        self.created_at = created_at
        self.claim_state = claim_state


def _fake_template(name: str, *, take_enabled: bool = True) -> dict:
    return {"name": name, "take": {"enabled": take_enabled}}


class _FakeTupleStore:
    closed = False

    def __init__(
        self, subspaces=None, rd_by_subspace=None, list_exc=None,
        templates=None, rd_calls: list[str] | None = None,
    ) -> None:
        self._subspaces = subspaces or []
        self._rd_by_subspace = rd_by_subspace or {}
        self._list_exc = list_exc
        self._templates = templates if templates is not None else []
        self._rd_calls = rd_calls

    def subspace_list(self, prefix=None):
        if self._list_exc is not None:
            raise self._list_exc
        return self._subspaces

    def registry(self):
        return {"templates": self._templates}

    def rd(self, subspace, keys_pattern, n=1, since=None, timeout_s=0):
        if self._rd_calls is not None:
            self._rd_calls.append(subspace)
        return self._rd_by_subspace.get(subspace, [])


def _run_unclaimed(monkeypatch, store) -> h.HealthResult:
    monkeypatch.setattr(
        "nexus.db.t2.http_tuple_store.HttpTupleStore",
        lambda *a, **k: store, raising=False,
    )
    return h._check_tuple_unclaimed_age()[0]


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
        stale, so the per-subspace rd(n=300) fetch is skipped entirely."""
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
        assert "mailbox/a=" in r.detail

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
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="0||\n", stderr="")

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
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"3|0|{recent}\n", stderr="")

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
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="2|2|\n", stderr="")

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
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"1|0|{stale}\n", stderr="")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
        assert r.ok is False and r.warn is not True
        assert "over the" in r.detail

    def test_engine_unreachable(self, tmp_path) -> None:
        creds = _make_creds_file(tmp_path)

        def responder(sql, cmd):
            return subprocess.CompletedProcess(args=cmd, returncode=2, stdout="", stderr="connection refused")

        r = h._check_tuple_sweep_freshness(
            creds_path=creds, psql_bin=Path("/fake/psql"), psql_runner=_psql_runner(responder),
        )[0]
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
    """``_TUPLE_ROUTE_FIRST_ENGINE_VERSION`` must never sit BELOW
    ``REQUIRED_ENGINE_VERSION`` (that would claim the tuple route ships
    in an engine no local install can even reach any more) and must
    never sit ABOVE the newest published ``engine-service-v*`` tag this
    repo's git history knows about (that would name a tag that does not
    exist yet). Both directions drift the moment either side moves
    without the other -- this is the mechanical half of the comment
    above the constant's definition in ``src/nexus/health.py``.
    """
    assert h._TUPLE_ROUTE_FIRST_ENGINE_VERSION >= ev.REQUIRED_ENGINE_VERSION, (
        f"_TUPLE_ROUTE_FIRST_ENGINE_VERSION {h._TUPLE_ROUTE_FIRST_ENGINE_VERSION} is BELOW "
        f"REQUIRED_ENGINE_VERSION {ev.REQUIRED_ENGINE_VERSION} -- the tuple route cannot "
        "predate a floor no local install can even reach any more; fix the constant in "
        "src/nexus/health.py."
    )

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
