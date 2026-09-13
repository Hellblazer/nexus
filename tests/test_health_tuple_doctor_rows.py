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
        templates=None, rd_calls: list[str] | None = None, registry_exc=None,
    ) -> None:
        self._subspaces = subspaces or []
        self._rd_by_subspace = rd_by_subspace or {}
        self._list_exc = list_exc
        self._templates = templates if templates is not None else []
        self._rd_calls = rd_calls
        self._registry_exc = registry_exc

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


# ── row 4: _check_tuple_watch_permission (bead nexus-rml7o) ─────────────────
#
# MM-3.4 critic finding S5 (T2 mm34-phase3-critic-pass-2026-09-13): a Monitor
# running ``nx tuple watch`` goes through the same permission machinery as
# Bash. This row reads every settings file Claude Code consults for
# permissions -- user (``~/.claude/settings.json``, honouring
# ``CLAUDE_CONFIG_DIR``), project (``<root>/.claude/settings.json``), and
# project-local (``<root>/.claude/settings.local.json``), ``root`` being the
# git top-level of the cwd, falling back to the cwd itself outside a repo --
# and reports whether a covering ``permissions.allow`` rule exists, and
# separately whether a ``permissions.deny`` rule anywhere overrides it
# (nexus-rml7o review pass, T2 nexus/cleanup-batch-cre-pass-2026-09-13 and
# nexus/cleanup-batch-critic-pass-2026-09-13). It makes NO claim about
# whether arming raises a permission prompt in either direction -- only
# whether a covering rule is present, which file supplied it, and what the
# rule is for. Always informational (never fatal): ``warn=True`` when no
# covering rule is found or a deny rule wins, ``ok=True`` when a covering
# allow rule stands unchallenged by any deny.


def _write_settings(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _three_files(
    tmp_path: Path, *, user: dict | None = None, project: dict | None = None,
    project_local: dict | None = None,
) -> list[tuple[str, Path]]:
    """Three (label, path) pairs, each written only when its payload is given
    -- an omitted file stays absent, exercising the same "not configured,
    never a crash" path a missing file always has.
    """
    paths = [
        ("user", tmp_path / "user-home" / "settings.json"),
        ("project", tmp_path / "repo" / ".claude" / "settings.json"),
        ("project-local", tmp_path / "repo" / ".claude" / "settings.local.json"),
    ]
    for (name, path), payload in zip(paths, (user, project, project_local), strict=True):
        if payload is not None:
            _write_settings(path, payload)
    return paths


class TestCheckTupleWatchPermission:
    # ── absence / unreadable, never a crash ──────────────────────────────

    def test_all_three_files_missing_is_not_configured_never_crashes(self, tmp_path) -> None:
        paths = _three_files(tmp_path)
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True
        assert "not configured" in r.detail

    def test_directory_in_place_of_a_file_is_skipped_never_crashes(self, tmp_path) -> None:
        paths = _three_files(tmp_path, project=None)
        as_dir = paths[0][1]
        as_dir.parent.mkdir(parents=True, exist_ok=True)
        as_dir.mkdir()
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True
        assert "not configured" in r.detail

    def test_malformed_json_in_one_file_is_skipped_others_still_read(self, tmp_path) -> None:
        paths = _three_files(tmp_path, project={"permissions": {"allow": ["Bash(nx:*)"]}})
        user_path = paths[0][1]
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text("{not json", encoding="utf-8")
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True
        assert "project" in r.detail

    def test_no_permissions_block_is_not_configured(self, tmp_path) -> None:
        paths = _three_files(tmp_path, user={"env": {}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True
        assert "not configured" in r.detail

    def test_unrelated_rules_are_not_configured(self, tmp_path) -> None:
        paths = _three_files(tmp_path, user={"permissions": {"allow": ["Bash(git:*)", "Read"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True
        assert "not configured" in r.detail

    # ── allow-rule matching (per-file, first covering file reported) ────

    def test_exact_documented_rule_in_user_file_is_ok_and_names_user(self, tmp_path) -> None:
        paths = _three_files(tmp_path, user={"permissions": {"allow": ["Bash(nx tuple watch:*)"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True
        assert "covers" in r.detail
        assert "(user)" in r.detail

    def test_covering_rule_in_project_file_names_project(self, tmp_path) -> None:
        paths = _three_files(tmp_path, project={"permissions": {"allow": ["Bash(nx tuple:*)"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True
        assert "(project)" in r.detail

    def test_covering_rule_in_project_local_file_names_project_local(self, tmp_path) -> None:
        paths = _three_files(
            tmp_path, project_local={"permissions": {"allow": ["Bash(nx tuple watch:*)"]}},
        )
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True
        assert "(project-local)" in r.detail

    def test_broader_nx_rule_is_ok(self, tmp_path) -> None:
        # Sam's own real settings.json shape (Bash(nx:*)) -- the broader-rule
        # branch this check exists to recognise, not a hypothetical.
        paths = _three_files(tmp_path, user={"permissions": {"allow": ["Bash(nx:*)"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True

    def test_bare_bash_allow_rule_covers(self, tmp_path) -> None:
        paths = _three_files(tmp_path, user={"permissions": {"allow": ["Bash"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True

    def test_first_covering_file_in_order_wins_when_several_cover(self, tmp_path) -> None:
        paths = _three_files(
            tmp_path,
            user={"permissions": {"allow": ["Bash(nx:*)"]}},
            project={"permissions": {"allow": ["Bash(nx tuple watch:*)"]}},
        )
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True
        assert "(user)" in r.detail

    def test_loose_substring_match_does_not_count(self, tmp_path) -> None:
        paths = _three_files(
            tmp_path, user={"permissions": {"allow": ["Bash(echo nx tuple watch:*)"]}},
        )
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True

    def test_partial_word_prefix_does_not_count(self, tmp_path) -> None:
        # "nx t" is a string-prefix of "nx tuple watch" but not a word-
        # boundary prefix -- must not count (conservative by design).
        paths = _three_files(tmp_path, user={"permissions": {"allow": ["Bash(nx t:*)"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True

    def test_rule_with_no_trailing_colon_star_does_not_count(self, tmp_path) -> None:
        paths = _three_files(tmp_path, user={"permissions": {"allow": ["Bash(nx tuple watch)"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True

    # ── deny overrides allow, wins across files (the dangerous direction) ─

    def test_deny_in_same_file_as_allow_wins(self, tmp_path) -> None:
        paths = _three_files(
            tmp_path,
            user={"permissions": {
                "allow": ["Bash(nx tuple watch:*)"], "deny": ["Bash(nx tuple watch:*)"],
            }},
        )
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True
        assert "denied" in r.detail

    def test_deny_in_a_different_file_wins_over_allow_elsewhere(self, tmp_path) -> None:
        paths = _three_files(
            tmp_path,
            user={"permissions": {"allow": ["Bash(nx:*)"]}},
            project_local={"permissions": {"deny": ["Bash(nx tuple watch:*)"]}},
        )
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is False and r.warn is True
        assert "denied" in r.detail
        assert "(project-local)" in r.detail

    def test_deny_names_the_denying_file(self, tmp_path) -> None:
        paths = _three_files(
            tmp_path,
            user={"permissions": {"allow": ["Bash(nx:*)"]}},
            project={"permissions": {"deny": ["Bash(nx tuple:*)"]}},
        )
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert "(project)" in r.detail

    def test_deny_recognised_forms(self, tmp_path) -> None:
        for deny_rule in (
            "Bash(nx:*)", "Bash(nx tuple:*)", "Bash(nx tuple watch:*)", "Bash",
        ):
            paths = _three_files(
                tmp_path,
                user={"permissions": {
                    "allow": ["Bash(nx tuple watch:*)"], "deny": [deny_rule],
                }},
            )
            r = h._check_tuple_watch_permission(settings_paths=paths)[0]
            assert r.ok is False and r.warn is True, deny_rule
            assert "denied" in r.detail, deny_rule

    def test_unrelated_deny_rule_does_not_override_a_covering_allow(self, tmp_path) -> None:
        paths = _three_files(
            tmp_path,
            user={"permissions": {"allow": ["Bash(nx tuple watch:*)"], "deny": ["Bash(git:*)"]}},
        )
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert r.ok is True

    # ── no prompt claim, either direction ────────────────────────────────

    def test_not_configured_detail_makes_no_claim_about_prompts(self, tmp_path) -> None:
        paths = _three_files(tmp_path)
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert "prompt" not in r.detail.lower()

    def test_denied_detail_makes_no_claim_about_prompts(self, tmp_path) -> None:
        paths = _three_files(tmp_path, user={"permissions": {"deny": ["Bash(nx:*)"]}})
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert "prompt" not in r.detail.lower()

    def test_no_causal_claim_about_a_prior_auto_mode_session(self, tmp_path) -> None:
        """nexus-rml7o review CRITICAL: the earlier text claimed an
        auto-mode session raised no prompt during the RDR-206 live
        verification, as evidence an absent rule is harmless -- FALSE (T2
        nexus/rdr-206-live-verification-correction-2026-09-13: that
        session's own settings.json carries Bash(nx:*), which covers nx
        tuple watch, so the absent prompt it saw proved nothing about the
        no-rule case). The RUNTIME detail text a doctor run actually shows
        must never repeat the claim in either the not-configured or the
        denied branch. The docstring may record the correction for future
        maintainers (this codebase's own convention), but only labelled as
        a correction, never re-asserted as a current justification.
        """
        import inspect
        not_configured = h._check_tuple_watch_permission(settings_paths=_three_files(tmp_path))[0]
        denied = h._check_tuple_watch_permission(
            settings_paths=_three_files(tmp_path, user={"permissions": {"deny": ["Bash"]}}),
        )[0]
        for detail in (not_configured.detail, denied.detail):
            assert "auto-mode" not in detail.lower()
            assert "raised no prompt" not in detail.lower()
            assert "raised none" not in detail.lower()

        docstring = (inspect.getdoc(h._check_tuple_watch_permission) or "").lower()
        if "auto-mode" in docstring:
            # Mentioning it is fine ONLY as a labelled correction, never
            # restated as if still a justification.
            assert "false" in docstring

    # ── severity ──────────────────────────────────────────────────────────

    def test_severity_is_always_informational_never_fatal(self, tmp_path) -> None:
        not_configured = h._check_tuple_watch_permission(settings_paths=_three_files(tmp_path))[0]
        assert not_configured.fatal is False
        denied = h._check_tuple_watch_permission(
            settings_paths=_three_files(tmp_path, user={"permissions": {"deny": ["Bash"]}}),
        )[0]
        assert denied.fatal is False
        covered = h._check_tuple_watch_permission(
            settings_paths=_three_files(tmp_path, user={"permissions": {"allow": ["Bash(nx:*)"]}}),
        )[0]
        assert covered.fatal is False

    def test_names_the_exact_entry_to_add(self, tmp_path) -> None:
        paths = _three_files(tmp_path)
        r = h._check_tuple_watch_permission(settings_paths=paths)[0]
        assert "Bash(nx tuple watch:*)" in r.detail

    # ── default path resolution (no injection) ──────────────────────────

    def test_default_settings_paths_has_user_project_and_project_local(self) -> None:
        labels = [name for name, _ in h._claude_settings_paths()]
        assert labels == ["user", "project", "project-local"]

    def test_default_user_path_honours_claude_config_dir_env_var(self, tmp_path, monkeypatch) -> None:
        config_dir = tmp_path / "custom-claude-home"
        config_dir.mkdir()
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
        paths = dict(h._claude_settings_paths())
        assert paths["user"] == config_dir / "settings.json"

    def test_default_user_path_is_home_dot_claude_settings_json(self, monkeypatch) -> None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        assert h._claude_settings_path() == Path.home() / ".claude" / "settings.json"

    def test_project_paths_use_git_toplevel(self, tmp_path, monkeypatch) -> None:
        fake_root = tmp_path / "fake-repo-root"
        import nexus.indexer_utils as iu
        monkeypatch.setattr(iu, "find_repo_root", lambda _p: fake_root)
        paths = dict(h._claude_settings_paths(cwd=tmp_path / "somewhere" / "deep"))
        assert paths["project"] == fake_root / ".claude" / "settings.json"
        assert paths["project-local"] == fake_root / ".claude" / "settings.local.json"

    def test_project_paths_fall_back_to_cwd_outside_a_repo(self, tmp_path, monkeypatch) -> None:
        import nexus.indexer_utils as iu
        monkeypatch.setattr(iu, "find_repo_root", lambda _p: None)
        cwd = tmp_path / "not-a-repo"
        paths = dict(h._claude_settings_paths(cwd=cwd))
        assert paths["project"] == cwd / ".claude" / "settings.json"


def test_watch_permission_row_is_registered_in_run_health_checks() -> None:
    import inspect

    source = inspect.getsource(h.run_health_checks)
    assert "_check_tuple_watch_permission()" in source


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
