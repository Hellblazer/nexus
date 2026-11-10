# SPDX-License-Identifier: AGPL-3.0-or-later
"""F30 (nexus-f3yqd.6): `nx doctor`'s three sensitivity rows share one catalog walk.

Measured 2026-11-10 on a laptop tenant: a default `nx doctor` took 2 m 16 s, and the three rows
sensitive-in-aspect-eligible, sensitive-with-aspects and sensitive-in-external-embedder took 22.2 s, 23.1 s
and 23.4 s, each walking the whole catalog (``all_documents(limit=0)``) to find no sensitive document. The walk
cost scales with the catalog, so on a large one the three repeats are most of the run.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.catalog import sensitivity_doctor as sd
from nexus.commands import doctor as doctor_mod


class _Doc:
    def __init__(self, sensitive: bool) -> None:
        self.sensitive = sensitive
        self.collection = "knowledge__c__voyage-context-3__v1"
        self.tumbler = "1.1.1"
        self.title = "t"


class _Reader:
    def __init__(self, docs: list[_Doc]) -> None:
        self.docs = docs
        self.walks = 0

    def catalog_sensitivity_probe(self) -> tuple[bool, str]:
        return True, "9.9.9"

    def all_documents(self, limit: int = 0) -> list[_Doc]:
        self.walks += 1
        return list(self.docs)


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> _Reader:
    r = _Reader([_Doc(False), _Doc(False)])
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    return r


def _three() -> list[dict]:
    return [
        sd.check_sensitive_in_aspect_eligible(),
        sd.check_sensitive_with_aspects(),
        sd.check_sensitive_in_external_embedder(),
    ]


def test_inside_one_walk_the_three_rows_read_the_catalog_once(reader: _Reader) -> None:
    with sd.one_walk():
        reports = _three()
    assert reader.walks == 1
    assert [r["status"] for r in reports] == [sd.NOT_APPLICABLE] * 3
    assert {r["reason"] for r in reports} == {"no sensitive documents in this catalog"}
    assert [r["name"] for r in reports] == [sd.IN_ELIGIBLE, sd.WITH_ASPECTS, sd.IN_EXTERNAL_EMBEDDER]


def test_outside_one_walk_each_row_walks_for_itself(reader: _Reader) -> None:
    _three()
    assert reader.walks == 3


def test_the_shared_walk_ends_with_its_scope(reader: _Reader) -> None:
    with sd.one_walk():
        sd.check_sensitive_with_aspects()
    reader.docs.append(_Doc(True))
    no_aspects = type("NoAspects", (), {"get_by_doc_id": lambda self, _t: None})()
    with sd.one_walk():
        report = sd.check_sensitive_with_aspects(aspects=no_aspects, queue=object(), queued_doc_ids=lambda _q: set())
        assert report["sensitive_documents"] == 1, report
    assert reader.walks == 2


def test_doctors_supplementary_sweep_walks_once(reader: _Reader, monkeypatch: pytest.MonkeyPatch) -> None:
    keep = {"sensitive-in-aspect-eligible", "sensitive-with-aspects", "sensitive-in-external-embedder"}
    monkeypatch.setattr(doctor_mod, "_SUPPLEMENTARY_CHECK_NAMES", tuple(n for n in doctor_mod._SUPPLEMENTARY_CHECK_NAMES if n in keep))
    doctor_mod._run_supplementary_checks()
    assert reader.walks == 1


def test_a_failed_walk_is_shared_too(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    class _Broken(_Reader):
        def all_documents(self, limit: int = 0) -> list[_Doc]:
            calls.append(1)
            raise ConnectionError("engine gone")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Broken([]))
    with sd.one_walk():
        reports = _three()
    assert len(calls) == 1
    assert all(r["status"] == sd.NOT_APPLICABLE and "engine gone" in r["reason"] for r in reports)
