# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.6: the engine's own response bodies, read by the real client and rendered by the doctor's route rows.

``index-run-census-golden.json`` and ``next-seq-drift-golden.json`` hold the body each route answered for every case of
the shared tables (``index-run-census-cases.json``, ``next-seq-drift-cases.json``), read from the real engine route by
the Java parity tests, which fail when the live body drifts from them. Here those bytes go through the real
``HttpCatalogClient.index_run_census`` and ``next_seq_drift`` (only ``_get`` and ``_version_flags`` are stubbed, so the
body validators, the ``threshold_s``/``floor`` cross-check included, run on what the engine really sends), and through the
route rows of a default ``nx doctor``; the rows must equal the Python oracle's over the same documents, text and marks.
A wire-shape change the validators or the renderers do not follow fails here, not on a customer's tenant.

Every scenario is compared the same way. The goldens are read at ``limit=100`` (the census) and the default limit (the
drift list); no case in either table reaches its cap, so no capped rule applies to any of them, and
``test_no_golden_body_is_capped`` fails if one ever does (the capped rules are pinned by
``test_doctor_index_run_census_rows.py``).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nexus import health
from nexus.catalog.http_catalog_client import HttpCatalogClient
from tests.catalog.test_index_run_census_cases import (  # noqa: F401 — clock is the fixture the oracle rows need
    _Catalog,
    _T3,
    _table,
    _walk,
    clock,
)
from tests.catalog.test_next_seq_drift_cases import _cases, _owners

_RESOURCES = Path(__file__).resolve().parents[2] / "service/src/test/resources"
_CENSUS_GOLDEN = json.loads((_RESOURCES / "index-run-census-golden.json").read_text(encoding="utf-8"))["scenarios"]
_DRIFT_GOLDEN = json.loads((_RESOURCES / "next-seq-drift-golden.json").read_text(encoding="utf-8"))["cases"]
_BOTH = {"catalog_index_run_census_supported": True, "catalog_next_seq_drift_read_supported": True}


def _client(monkeypatch: pytest.MonkeyPatch, body: Any) -> HttpCatalogClient:
    c = object.__new__(HttpCatalogClient)
    monkeypatch.setattr(c, "_version_flags", lambda: _BOTH, raising=False)
    monkeypatch.setattr(c, "_get", lambda path, **kw: copy.deepcopy(body), raising=False)
    return c


def _same(oracle: list[health.HealthResult], route: list[health.HealthResult]) -> None:
    assert [(r.ok, r.warn, r.skipped, r.fatal, r.detail, r.fix_suggestions) for r in route] == \
           [(r.ok, r.warn, r.skipped, r.fatal, r.detail, r.fix_suggestions) for r in oracle]


_SCENARIOS = {s["id"]: s for s in _table()["scenarios"]}
_CASES = {c["id"]: c for c in _cases()}


# ── the goldens and the tables cover each other ──────────────────────────────


def test_every_scenario_has_a_golden_body_and_every_golden_a_scenario() -> None:
    assert len(_SCENARIOS) >= 14, "the table lost its scenarios"
    assert set(_CENSUS_GOLDEN) == set(_SCENARIOS), (set(_SCENARIOS) ^ set(_CENSUS_GOLDEN))


def test_every_drift_case_has_a_golden_body_and_every_golden_a_case() -> None:
    assert len(_CASES) >= 10, "the table lost its cases"
    assert set(_DRIFT_GOLDEN) == set(_CASES), (set(_CASES) ^ set(_DRIFT_GOLDEN))


def test_no_golden_body_is_capped() -> None:
    """The comparison below is exact only while no body hit its limit; one that does needs the capped rules."""
    for sid, body in _CENSUS_GOLDEN.items():
        assert body["limit"] == 100, sid
        for bucket in (body["stale"], body["failed_hidden"], body["fence"]["post_anchor"]):
            assert bucket["capped"] is False, sid
        assert body["failed_hidden"]["failed"]["capped"] is False, sid
    for cid, body in _DRIFT_GOLDEN.items():
        assert body["capped"] is False and body["drifted"] == len(body["examples"]), cid


# ── the census ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sid", sorted(_CENSUS_GOLDEN))
def test_the_real_client_accepts_the_engines_census_body(sid: str, monkeypatch: pytest.MonkeyPatch) -> None:
    census = _client(monkeypatch, _CENSUS_GOLDEN[sid]).index_run_census(limit=100)
    assert census is not None, f"{sid}: the client rejected the engine's own body"
    assert set(census) == {"stale", "fence", "failed_hidden"}
    assert not any(b.get("timed_out") for b in census.values())


@pytest.mark.parametrize("sid", sorted(_CENSUS_GOLDEN))
def test_the_stale_and_fence_rows_from_the_engines_body_are_the_oracles(
    sid: str, monkeypatch: pytest.MonkeyPatch, clock: None,
) -> None:
    scenario = _SCENARIOS[sid]
    census = _client(monkeypatch, _CENSUS_GOLDEN[sid]).index_run_census(limit=100)
    assert census is not None
    cat = _Catalog(scenario)
    route = health._stale_fence_rows_from_census(cat, health._STALE_FENCES_LABEL, census)
    _same(health._stale_fence_rows_over(_walk(scenario), cat), route)


@pytest.mark.parametrize("sid", sorted(_CENSUS_GOLDEN))
def test_the_failed_runs_row_from_the_engines_body_is_the_oracles(sid: str, monkeypatch: pytest.MonkeyPatch) -> None:
    scenario = _SCENARIOS[sid]
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _T3(scenario))
    census = _client(monkeypatch, _CENSUS_GOLDEN[sid]).index_run_census(limit=100)
    assert census is not None
    route = health._failed_hidden_rows_from_census(health._HIDDEN_CHUNKS_LABEL, census["failed_hidden"])
    _same(health._failed_hidden_rows_over(_walk(scenario), _Catalog(scenario)), route)


# ── the drift list ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("cid", sorted(_DRIFT_GOLDEN))
def test_the_real_client_accepts_the_engines_drift_body_and_its_row_is_the_oracles(cid: str, monkeypatch: pytest.MonkeyPatch) -> None:
    case = _CASES[cid]
    drift = _client(monkeypatch, _DRIFT_GOLDEN[cid]).next_seq_drift()
    assert drift is not None, f"{cid}: the client rejected the engine's own body"
    route = health._next_seq_drift_rows_from_route(health._NEXT_SEQ_LABEL, drift)
    oracle = health._next_seq_drift_rows_over(
        _owners(case), [SimpleNamespace(tumbler=t) for t in case["live"]], case["tombstoned"])
    if drift["drifted"] > 1:
        # The engine lists owners in prefix order, the oracle in the catalog's: the order-free text, and the marks.
        def names(r: health.HealthResult) -> list[str]:
            return sorted(r.detail.split("highest existing tumbler: ")[1].split(". The engine floors")[0].split("; "))

        assert names(route[0]) == names(oracle[0])
        assert (route[0].ok, route[0].warn, route[0].fix_suggestions) == (oracle[0].ok, oracle[0].warn, oracle[0].fix_suggestions)
        assert route[0].detail.split(":")[0] == oracle[0].detail.split(":")[0]
    else:
        _same(oracle, route)
    assert drift["drifted"] == len(case["expect"]["drifted"])
