# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx doctor`` row for documents whose manifest names chunks outside their collection (GH #1611)."""
from __future__ import annotations

import inspect

import pytest

import nexus.catalog.foreign_collection_doctor as dfc
import nexus.engine_version as ev
import nexus.health as h

_HOME = "knowledge__ads__bge-base-en-v15-768__v1"
_REPO = "docs__1-48__bge-base-en-v15-768__v1"


def _doc(n: int, chunks: int = 56, collections: list[str] | None = None) -> dict:
    return {"tumbler": f"1.23.{n}", "physical_collection": _HOME,
            "collections": collections or [_REPO], "chunks": chunks}


def _report(docs: list[dict], *, count: int | None = None, capped: bool = False) -> dict:
    return {"count": len(docs) if count is None else count, "capped": capped, "documents": docs}


def _run(monkeypatch: pytest.MonkeyPatch, report: object = None, *, raises: Exception | None = None):
    class _Cat:
        def manifest_foreign_collection(self, limit: int = 1000):
            if raises is not None:
                raise raises
            return report

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: _Cat(), raising=False)
    (row,) = dfc.check_manifest_foreign_collection()
    return row


def test_no_document_is_a_clean_none(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _run(monkeypatch, _report([]))
    assert row.ok is True and row.warn is False and row.detail == "none"


def test_a_finding_is_a_warn_naming_the_document_its_collection_and_the_others(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _run(monkeypatch, _report([_doc(96, 56, [_REPO, "docs__2-1__m__v1"])]))
    assert row.ok is False and row.warn is True
    assert "1 document(s)" in row.detail
    assert f"1.23.96 (in {_HOME}) has 56 chunk(s) in {_REPO}, docs__2-1__m__v1" in row.detail


def test_at_most_ten_documents_are_named_and_the_total_is_always_given(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [_doc(n) for n in range(100, 100 + dfc.MAX_NAMED_DOCUMENTS + 3)]
    row = _run(monkeypatch, _report(docs))
    assert f"{len(docs)} document(s)" in row.detail
    assert row.detail.count("chunk(s) in") == dfc.MAX_NAMED_DOCUMENTS
    assert "1.23.109" in row.detail and "1.23.110" not in row.detail
    assert "(+3 more)" in row.detail


def test_a_capped_read_reports_the_engines_exact_total_not_the_list_length(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [_doc(n) for n in range(100, 105)]
    row = _run(monkeypatch, _report(docs, count=2500, capped=True))
    assert "2500 document(s)" in row.detail
    assert "(+2495 more)" in row.detail


def test_the_fix_text_says_what_each_command_does_and_what_stays(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _run(monkeypatch, _report([_doc(96)]))
    first, second, third = row.fix_suggestions
    assert first.startswith("nx catalog show 1.23.96")
    assert second.startswith(f"nx index pdf <that file> --collection {_HOME} --force")
    assert "replaces ALL of its manifest rows" in second
    # the stray copy is NOT removed by the re-index: the fix must say so rather than promise a clean-up
    assert f"does not remove the copy in {_REPO}" in second
    assert third.startswith(f"nx t3 gc -c {_REPO} --dry-run")
    assert "30 days" in third and "stays until" in third


@pytest.mark.parametrize("floor", [(0, 1, 100), (0, 1, 188)])
def test_cannot_say_is_informational_while_the_floor_is_at_or_below_the_last_engine_without_the_route(
    monkeypatch: pytest.MonkeyPatch, floor: tuple[int, int, int],
) -> None:
    monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", floor)
    row = _run(monkeypatch, None)
    assert row.ok is True and row.warn is False
    assert row.detail.startswith("informational")
    assert "none" != row.detail, "a read that could not say must never read as a clean pass"


def test_cannot_say_is_a_warn_once_the_floor_is_past_the_last_engine_without_the_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", (0, 1, 189))
    row = _run(monkeypatch, None)
    assert row.ok is False and row.warn is True
    assert row.detail.startswith("UNKNOWN")


def test_a_reader_without_the_method_or_that_raises_cannot_say_and_never_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ev, "REQUIRED_ENGINE_VERSION", (0, 1, 189))
    row = _run(monkeypatch, raises=AttributeError("no such method"))
    assert row.warn is True and row.detail.startswith("UNKNOWN")


def test_an_unopenable_catalog_is_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("no service")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", boom, raising=False)
    (row,) = dfc.check_manifest_foreign_collection()
    assert row.ok is True and row.detail == "skipped (catalog unavailable)"


def test_the_default_sweep_runs_the_row() -> None:
    """A defined-but-unregistered check is dead code: the sweep must call it, through the health wrapper."""
    source = inspect.getsource(h.run_health_checks)
    assert "_check_manifest_foreign_collection()" in source
    assert h._check_manifest_foreign_collection.__module__ == "nexus.health"
    assert inspect.getsource(h._check_manifest_foreign_collection).count("check_manifest_foreign_collection()") >= 1
