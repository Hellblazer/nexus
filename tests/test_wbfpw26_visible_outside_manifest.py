# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wbfpw.26 (RDR-192 S14, client): ``nx catalog doctor
--visible-outside-manifest``.

The check is a canary that can only fire on a live(c) regression. Under
live(c) a chunk with no own-collection manifest row is hidden from search and
get; the census ``superseded`` bucket is the set of such chunks whose owning
document is live. A superseded chunk that the NORMAL reader still returns is
the divergence signature of RDR-192 Step 14 (``nx catalog show`` reports a clean
manifest while raw search returns two versions of one title).

Two kinds of test, on purpose:

* Real engine substrate (``t2_service_env``): the healthy case (a stranded
  superseded chunk that live(c) correctly hides, zero findings, checked 1 of 1)
  and the regression case. A live(c) regression cannot be produced against a
  healthy engine, so the regression is simulated by a reader that asks the
  engine for stored rows (``include_non_live``), which is exactly what a
  regressed live(c) filter would return. The engine data and the census are
  real; only the visibility filter is the stand-in.
* A strict fake for the budget, paging, error and "never reads physical rows"
  assertions, where a real engine cannot be made to misbehave on demand.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.aspect_readers import uri_for
from nexus.commands.catalog_cmds.doctor import (
    _print_visible_outside_manifest_text,
    _run_visible_outside_manifest,
    doctor_cmd,
)
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError
from nexus.mcp.core import store_put

KNOWLEDGE = "knowledge__wbfpw26__bge-base-en-v1.5__v1"


# ── strict fake ──────────────────────────────────────────────────────────────


class _FakeCollection:
    """A collection handle that accepts only the live(c)-filtered read.

    ``get`` takes no ``include_non_live`` parameter, so a reader that passes it
    raises TypeError: the assertion that the check never reads physical rows
    is the signature itself, not a spy that could be forgotten.
    """

    def __init__(self, parent: "_FakeT3", name: str) -> None:
        self._parent = parent
        self._name = name

    def get(self, ids=None, include=None, limit=None, offset=0):
        self._parent.get_calls.append({"collection": self._name, "ids": list(ids or [])})
        visible = self._parent.visible.get(self._name, set())
        got = [i for i in (ids or []) if i in visible]
        return {"ids": got, "documents": [], "metadatas": [{} for _ in got]}


class _FakeT3:
    def __init__(self, census: dict[str, dict[str, list[str]]], *,
                 visible: dict[str, set[str]] | None = None,
                 extra_collections: list[str] | None = None) -> None:
        # census: collection -> bucket -> chashes (already sorted by chash).
        self.census = census
        self.visible = visible or {}
        self.collections = sorted(census) + list(extra_collections or [])
        self.census_calls: list[tuple[str, int, int]] = []
        self.get_calls: list[dict] = []
        self.census_error: Exception | None = None

    def list_collections(self):
        return [{"name": n, "count": 0, "stored_count": 0} for n in self.collections]

    def get_or_create_collection(self, name):
        return _FakeCollection(self, name)

    def manifest_less_census(self, collection, limit=100, offset=0):
        self.census_calls.append((collection, limit, offset))
        if self.census_error is not None:
            raise self.census_error
        buckets = self.census.get(collection, {})
        flat = sorted(
            (chash, bucket) for bucket, hs in buckets.items() for chash in hs
        )
        page = flat[offset: offset + limit]
        out: dict[str, list[str]] = {b: [] for b in
                                     ("superseded", "legacy-unmanifested", "dead-owner", "no-owner", "unclassified")}
        owners = {}
        for chash, bucket in page:
            out[bucket].append(chash)
            owners[chash] = {"owner_tumbler": "1.9.1", "owner_path": "forward"}
        return {
            "collection": collection, "returned": len(page), "chashes": out,
            "owners": owners,
            "totals": {b: len(buckets.get(b, [])) for b in out},
            "scope_chunk_total": len(flat),
        }


def _ch(i: int) -> str:
    return hashlib.sha256(f"wbfpw26-{i}".encode()).hexdigest()


def _no_titles(monkeypatch):
    monkeypatch.setattr(
        "nexus.commands.catalog_cmds.doctor._titles_for_tumblers", lambda tumblers: {},
    )


# ── fake-backed: logic ───────────────────────────────────────────────────────


def test_a_hidden_superseded_chunk_passes_and_is_counted(monkeypatch):
    _no_titles(monkeypatch)
    t3 = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1), _ch(2)]}})
    report = _run_visible_outside_manifest(t3=t3)
    assert report["pass"] is True and not report["findings"]
    assert report["checked"] == 2 and report["total"] == 2
    assert report["truncated"] is False


def test_a_visible_superseded_chunk_is_a_finding_naming_collection_tumbler_and_chash(monkeypatch):
    monkeypatch.setattr(
        "nexus.commands.catalog_cmds.doctor._titles_for_tumblers",
        lambda tumblers: {"1.9.1": "the note"},
    )
    t3 = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1), _ch(2)]}}, visible={KNOWLEDGE: {_ch(2)}})
    report = _run_visible_outside_manifest(t3=t3)
    assert report["pass"] is False
    assert report["findings"] == [
        {"collection": KNOWLEDGE, "chash": _ch(2), "tumbler": "1.9.1", "title": "the note"},
    ]
    assert report["checked"] == 2


def test_the_reader_never_asks_for_non_live_rows(monkeypatch):
    """_FakeCollection.get has no include_non_live parameter: passing it is a
    TypeError, so completing the run IS the assertion. Also pins that the
    reader reads by id, never by a physical listing."""
    _no_titles(monkeypatch)
    t3 = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1)]}})
    _run_visible_outside_manifest(t3=t3)
    assert t3.get_calls == [{"collection": KNOWLEDGE, "ids": [_ch(1)]}]


def test_only_the_superseded_bucket_is_probed(monkeypatch):
    _no_titles(monkeypatch)
    t3 = _FakeT3({KNOWLEDGE: {
        "superseded": [_ch(1)], "legacy-unmanifested": [_ch(2)],
        "dead-owner": [_ch(3)], "no-owner": [_ch(4)], "unclassified": [_ch(5)],
    }}, visible={KNOWLEDGE: {_ch(i) for i in range(1, 6)}})
    report = _run_visible_outside_manifest(t3=t3)
    assert [f["chash"] for f in report["findings"]] == [_ch(1)]
    assert t3.get_calls[0]["ids"] == [_ch(1)]


def test_no_knowledge_collection_is_not_applicable_and_passes():
    t3 = _FakeT3({}, extra_collections=["code__x__bge-base-en-v1.5__v1", "docs__y__bge-base-en-v1.5__v1"])
    report = _run_visible_outside_manifest(t3=t3)
    assert report["pass"] is True and report["not_applicable"] is True
    assert report["findings"] == [] and t3.census_calls == []
    assert "no knowledge__ collection" in report["reason"]


def test_non_knowledge_and_quarantine_collections_are_never_censused(monkeypatch):
    _no_titles(monkeypatch)
    t3 = _FakeT3({KNOWLEDGE: {"superseded": []}},
                 extra_collections=["code__x__bge-base-en-v1.5__v1", "quarantine-knowledge__a__m__v1"])
    _run_visible_outside_manifest(t3=t3)
    assert {c for c, _l, _o in t3.census_calls} == {KNOWLEDGE}


def test_a_collection_with_no_superseded_chunks_costs_one_census_page(monkeypatch):
    _no_titles(monkeypatch)
    t3 = _FakeT3({KNOWLEDGE: {"legacy-unmanifested": [_ch(i) for i in range(700)]}})
    report = _run_visible_outside_manifest(t3=t3)
    assert len(t3.census_calls) == 1, "totals.superseded == 0 on page one ends the collection"
    assert report["checked"] == 0 and report["total"] == 0 and report["pass"] is True
    assert report["not_applicable"] is False


def test_superseded_chunks_beyond_the_first_census_page_and_probe_batch_are_checked(monkeypatch):
    _no_titles(monkeypatch)
    chashes = sorted(_ch(i) for i in range(700))
    superseded = chashes[::100]   # spread across every census page
    other = [c for c in chashes if c not in superseded]
    t3 = _FakeT3({KNOWLEDGE: {"superseded": superseded, "no-owner": other}},
                 visible={KNOWLEDGE: {superseded[-1]}})
    report = _run_visible_outside_manifest(t3=t3)
    assert report["total"] == len(superseded) == report["checked"]
    assert [f["chash"] for f in report["findings"]] == [superseded[-1]]
    assert all(limit <= 300 for _c, limit, _o in t3.census_calls), "QUOTAS: <= 300 per call"
    assert len(t3.census_calls) == 3, t3.census_calls


def test_probe_batches_respect_the_300_limit(monkeypatch):
    _no_titles(monkeypatch)
    t3 = _FakeT3({KNOWLEDGE: {"superseded": sorted(_ch(i) for i in range(650))}})
    report = _run_visible_outside_manifest(t3=t3, row_budget=10_000)
    assert report["checked"] == 650
    assert [len(c["ids"]) for c in t3.get_calls] == [300, 300, 50]


def test_row_budget_truncates_and_says_checked_n_of_m(monkeypatch):
    _no_titles(monkeypatch)
    t3 = _FakeT3({KNOWLEDGE: {"superseded": sorted(_ch(i) for i in range(5))}})
    report = _run_visible_outside_manifest(t3=t3, row_budget=2)
    assert report["checked"] == 2 and report["total"] == 5
    assert report["truncated"] is True and "row budget" in report["truncated_reason"]
    assert report["pass"] is True, "no finding in what was checked; the budget line carries the caveat"
    lines: list[str] = []
    with patch("click.echo", lambda m="", **k: lines.append(str(m))):
        _print_visible_outside_manifest_text(report)
    assert any("checked 2 of 5" in line for line in lines), lines


def test_time_budget_stops_and_names_the_unreached_collections(monkeypatch):
    """Each census call costs 6 s on a fake clock against a 10 s budget: the
    first collection is censused and probed, the second is censused but its
    probe starts past the budget, the third is never reached."""
    _no_titles(monkeypatch)
    names = [f"knowledge__wbfpw26-{c}__bge-base-en-v1.5__v1" for c in "abc"]
    t3 = _FakeT3({n: {"superseded": [_ch(i)]} for i, n in enumerate(names)})
    now = [0.0]
    real_census = t3.manifest_less_census

    def slow_census(*a, **k):
        now[0] += 6.0
        return real_census(*a, **k)

    t3.manifest_less_census = slow_census
    report = _run_visible_outside_manifest(t3=t3, time_budget_s=10.0, clock=lambda: now[0])
    assert report["truncated"] is True and "time budget" in report["truncated_reason"]
    assert report["collections_unreached"] == 1
    assert (report["checked"], report["total"]) == (1, 2)


def test_an_engine_without_the_census_route_is_not_applicable():
    t3 = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1)]}})
    t3.census_error = VectorServiceError("not found", code=404)
    report = _run_visible_outside_manifest(t3=t3)
    assert report["pass"] is True and report["not_applicable"] is True
    assert "census route" in report["reason"]


def test_a_census_error_other_than_404_fails_and_is_named():
    t3 = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1)]}})
    t3.census_error = VectorServiceError("boom", code=500)
    report = _run_visible_outside_manifest(t3=t3)
    assert report["pass"] is False
    assert report["check_errors"] and report["check_errors"][0]["collection"] == KNOWLEDGE


def test_a_failed_collection_listing_fails_not_passes():
    class _Broken:
        def list_collections(self):
            raise RuntimeError("service down")
    report = _run_visible_outside_manifest(t3=_Broken())
    assert report["pass"] is False and "service down" in report["error"]


# ── fake-backed: command wiring ──────────────────────────────────────────────


def test_the_flag_exits_one_on_a_finding_and_zero_when_clean(monkeypatch):
    _no_titles(monkeypatch)
    dirty = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1)]}}, visible={KNOWLEDGE: {_ch(1)}})
    with patch("nexus.db.make_t3", return_value=dirty):
        res = CliRunner().invoke(doctor_cmd, ["--visible-outside-manifest"])
    assert res.exit_code == 1, res.output
    assert _ch(1) in res.output and "FAIL" in res.output

    clean = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1)]}})
    with patch("nexus.db.make_t3", return_value=clean):
        res = CliRunner().invoke(doctor_cmd, ["--visible-outside-manifest"])
    assert res.exit_code == 0, res.output
    assert "checked 1 of 1" in res.output


def test_the_flag_emits_json(monkeypatch):
    _no_titles(monkeypatch)
    clean = _FakeT3({KNOWLEDGE: {"superseded": [_ch(1)]}})
    with patch("nexus.db.make_t3", return_value=clean):
        res = CliRunner().invoke(doctor_cmd, ["--visible-outside-manifest", "--json"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.stdout)["visible_outside_manifest"]
    assert payload["checked"] == 1 and payload["total"] == 1 and payload["pass"] is True


def test_not_applicable_prints_a_distinct_line_and_exits_zero():
    t3 = _FakeT3({})
    with patch("nexus.db.make_t3", return_value=t3):
        res = CliRunner().invoke(doctor_cmd, ["--visible-outside-manifest"])
    assert res.exit_code == 0, res.output
    assert "not applicable" in res.output.lower()
    assert "PASS" not in res.output, "N/A must not read as a pass"


# ── real engine substrate ────────────────────────────────────────────────────


class _PhysicalReaderCollection:
    """What a live(c) regression would show a normal reader: stored rows."""

    def __init__(self, real) -> None:
        self._real = real

    def get(self, ids=None, include=None, limit=None, offset=0):
        return self._real.get(ids=ids, include=include, limit=limit, offset=offset,
                              include_non_live=True)


class _RegressedT3:
    def __init__(self, real: HttpVectorClient) -> None:
        self._real = real

    def list_collections(self, *a, **k):
        return self._real.list_collections(*a, **k)

    def manifest_less_census(self, *a, **k):
        return self._real.manifest_less_census(*a, **k)

    def get_or_create_collection(self, name):
        return _PhysicalReaderCollection(self._real.get_or_create_collection(name))


def _strand_a_superseded_chunk(client: HttpVectorClient, subject: str, title: str):
    """Put a note, then replace its manifest with a write that has no sweep, so
    the first version stays physically stored with no manifest row: the state
    the census calls superseded (same construction as
    tests/test_wbfpw11_reap_fail_visibility.py)."""
    v1 = f"{subject} first version: heron migration notes from the northern marsh"
    v2 = f"{subject} second version: completely rewritten text about lighthouse lenses"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=v1, collection=subject, title=title)
    collection = t3_collection_name(subject, t3=client)
    reader = make_catalog_reader()
    assert reader is not None
    doc = reader.by_source_uri(uri_for(collection, title))
    assert doc is not None
    tumbler = str(doc.tumbler)
    (v1_chash,) = {r.chash for r in reader.get_manifest(tumbler)}
    v2_chash = hashlib.sha256(v2.encode()).hexdigest()
    writer = make_catalog_writer(priority="interactive")
    try:
        writer.write_manifest_many(
            [(tumbler, [{"chash": v2_chash, "position": 0}])],
            collection=collection, sweep=False,
            chunks=[{"chash": v2_chash, "text": v2,
                     "metadata": {"indexed_at": datetime.now(UTC).isoformat()}}],
        )
    finally:
        writer.close()
    return collection, tumbler, v1_chash


def test_healthy_engine_hides_the_superseded_chunk_so_no_finding(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    collection, _tumbler, v1_chash = _strand_a_superseded_chunk(
        client, "wbfpw26-healthy", "wbfpw26 healthy note")
    census = client.manifest_less_census(collection, limit=300)
    assert v1_chash in census["chashes"]["superseded"], "control: the fixture really is superseded"

    report = _run_visible_outside_manifest(t3=client)

    assert report["pass"] is True and report["findings"] == [], report
    assert report["checked"] == 1 and report["total"] == 1, "non-vacuous: the stranded chunk was probed"


def test_a_live_c_regression_is_flagged_through_the_real_census(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    collection, tumbler, v1_chash = _strand_a_superseded_chunk(
        client, "wbfpw26-regressed", "wbfpw26 regressed note")

    report = _run_visible_outside_manifest(t3=_RegressedT3(client))

    assert report["pass"] is False, report
    assert [(f["collection"], f["chash"], f["tumbler"]) for f in report["findings"]] == [
        (collection, v1_chash, tumbler)], report["findings"]
    assert report["findings"][0]["title"] == "wbfpw26 regressed note"

    with patch("nexus.db.make_t3", return_value=_RegressedT3(client)):
        res = CliRunner().invoke(doctor_cmd, ["--visible-outside-manifest"])
    assert res.exit_code == 1, res.output
    assert v1_chash in res.output and tumbler in res.output


def test_a_virgin_tenant_is_not_applicable(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    report = _run_visible_outside_manifest(t3=client)
    assert report["pass"] is True and report["not_applicable"] is True, report
    with patch("nexus.db.make_t3", return_value=client):
        res = CliRunner().invoke(doctor_cmd, ["--visible-outside-manifest"])
    assert res.exit_code == 0, res.output
    assert "not applicable" in res.output.lower()


@pytest.mark.parametrize("flag", ["--visible-outside-manifest"])
def test_the_flag_is_a_check_flag_not_a_bare_invocation(flag):
    res = CliRunner().invoke(doctor_cmd, [])
    assert res.exit_code == 2 and flag in res.output
