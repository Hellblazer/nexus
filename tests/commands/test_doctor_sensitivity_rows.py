# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 Step 5b (nexus-rfvyy.6.17): the doctor rows ``sensitive_in_aspect_eligible`` (WARN) and
``sensitive_with_aspects`` (FAIL), on ``nx catalog doctor`` and ``nx doctor``.

Two layers. The finding logic runs on fakes of the catalog reader, the aspects store and the queue (what
each verdict needs is a sensitive document and one fact about it). The wiring runs on the engine substrate:
a virgin tenant reports NOT APPLICABLE on both surfaces and never a pass (nexus-7zhag), and a flagged
document with a real aspect row fails with the remedy line.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nexus.catalog import sensitivity_doctor as sd
from nexus.cli import main


def _doc(tumbler: str, collection: str = "knowledge__sens__bge-base-en-v15-768__v1", sensitive: bool = True):
    return SimpleNamespace(tumbler=tumbler, physical_collection=collection, sensitive=sensitive)


class _Reader:
    def __init__(self, docs, *, supported=True, raises=None):
        self._docs, self._supported, self._raises = docs, supported, raises

    def catalog_sensitivity_probe(self):
        return self._supported, "1.2.3" if self._supported else "0.0.1"

    def all_documents(self, limit=0):
        if self._raises:
            raise self._raises
        return self._docs


class _Aspects:
    def __init__(self, by_doc=(), collections_with_rows=()):
        self._by_doc, self._colls = set(by_doc), set(collections_with_rows)

    def get_by_doc_id(self, doc_id):
        return object() if doc_id in self._by_doc else None

    def list_by_collection(self, collection, limit=None, offset=0):
        return [object()] if collection in self._colls else []


# ── sensitive_in_aspect_eligible ────────────────────────────────────────────


def test_eligible_warns_when_the_collection_has_an_extractor() -> None:
    report = sd.check_sensitive_in_aspect_eligible(
        reader=_Reader([_doc("1.1.1"), _doc("1.1.2", sensitive=False)]), aspects=_Aspects(), select=lambda c: object())
    assert report["status"] == sd.WARN
    assert [f["tumbler"] for f in report["findings"]] == ["1.1.1"], "only the sensitive document is named"
    assert "extractor" in report["findings"][0]["why"]


def test_eligible_warns_when_the_collection_already_holds_aspect_rows() -> None:
    coll = "docs__sens__bge-base-en-v15-768__v1"
    report = sd.check_sensitive_in_aspect_eligible(
        reader=_Reader([_doc("1.1.1", coll)]), aspects=_Aspects(collections_with_rows=[coll]), select=lambda c: None)
    assert report["status"] == sd.WARN
    assert "aspect rows" in report["findings"][0]["why"]


def test_eligible_passes_when_no_sensitive_document_is_exposed() -> None:
    report = sd.check_sensitive_in_aspect_eligible(
        reader=_Reader([_doc("1.1.1")]), aspects=_Aspects(), select=lambda c: None)
    assert report["status"] == sd.PASS and report["findings"] == []
    assert report["sensitive_documents"] == 1, "a pass names how many documents it examined"


# ── sensitive_with_aspects ──────────────────────────────────────────────────


def test_with_aspects_fails_on_an_aspect_row_and_prints_the_remedy() -> None:
    report = sd.check_sensitive_with_aspects(
        reader=_Reader([_doc("1.1.1"), _doc("1.1.2")]), aspects=_Aspects(by_doc=["1.1.1"]),
        queue=object(), queued_doc_ids=lambda q: set())
    assert report["status"] == sd.FAIL
    assert [f["tumbler"] for f in report["findings"]] == ["1.1.1"]
    assert sd.remedy_lines(report) == ["nx enrich delete --tumbler 1.1.1"]
    assert "nx enrich delete --tumbler 1.1.1" in "\n".join(sd.render_text(report))


def test_with_aspects_fails_on_a_queued_task_alone() -> None:
    report = sd.check_sensitive_with_aspects(
        reader=_Reader([_doc("1.1.1")]), aspects=_Aspects(), queue=object(), queued_doc_ids=lambda q: {"1.1.1"})
    assert report["status"] == sd.FAIL
    assert "queued" in report["findings"][0]["why"]


def test_with_aspects_passes_when_nothing_was_extracted_or_queued() -> None:
    report = sd.check_sensitive_with_aspects(
        reader=_Reader([_doc("1.1.1")]), aspects=_Aspects(), queue=object(), queued_doc_ids=lambda q: {"9.9.9"})
    assert report["status"] == sd.PASS


# ── not applicable, never pass ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "reader",
    [
        _Reader([]),
        _Reader([_doc("1.1.1", sensitive=False)]),
        _Reader([_doc("1.1.1")], supported=False),
        _Reader([], raises=OSError("engine unreachable")),
    ],
    ids=["empty-catalog", "no-sensitive-document", "engine-without-the-column", "catalog-unreadable"],
)
@pytest.mark.parametrize("check", [sd.check_sensitive_in_aspect_eligible, sd.check_sensitive_with_aspects])
def test_a_row_that_examined_no_sensitive_document_is_not_applicable_never_a_pass(check, reader) -> None:
    kwargs = {"queue": object(), "queued_doc_ids": lambda q: set()} if check is sd.check_sensitive_with_aspects else {"select": lambda c: object()}
    report = check(reader=reader, aspects=_Aspects(by_doc=["1.1.1"]), **kwargs)
    assert report["status"] == sd.NOT_APPLICABLE
    assert report["findings"] == []
    text = "\n".join(sd.render_text(report))
    assert "not applicable" in text and "[✓]" not in text and "PASS" not in text.upper().replace("NOT APPLICABLE", "")


def test_an_unreadable_aspect_store_is_not_applicable_with_the_cause_not_a_false_red() -> None:
    class Broken(_Aspects):
        def get_by_doc_id(self, doc_id):
            raise OSError("aspects store down")

    report = sd.check_sensitive_with_aspects(
        reader=_Reader([_doc("1.1.1")]), aspects=Broken(), queue=object(), queued_doc_ids=lambda q: set())
    assert report["status"] == sd.NOT_APPLICABLE and "aspects store down" in report["reason"]


# ── the wiring, on the engine substrate ─────────────────────────────────────

substrate = pytest.mark.t2_tenant


@substrate
def test_catalog_doctor_on_a_tenant_with_no_sensitive_documents_is_not_applicable(t2_service_env) -> None:
    result = CliRunner().invoke(main, ["catalog", "doctor", "--sensitive-in-aspect-eligible", "--sensitive-with-aspects"])
    assert result.exit_code == 0, result.output
    assert result.output.count("not applicable") == 2
    assert "[✓]" not in result.output


@substrate
def test_catalog_doctor_json_carries_both_rows(t2_service_env) -> None:
    result = CliRunner().invoke(
        main, ["catalog", "doctor", "--sensitive-in-aspect-eligible", "--sensitive-with-aspects", "--json"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    payload = json.loads("\n".join(lines[next(i for i, l in enumerate(lines) if l.startswith("{")):]))
    assert payload["sensitive_in_aspect_eligible"]["status"] == "not_applicable"
    assert payload["sensitive_with_aspects"]["status"] == "not_applicable"


@substrate
def test_catalog_doctor_fails_on_a_flagged_document_with_an_aspect_row_and_warns_on_its_collection(t2_service_env) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
    from tests.test_document_aspects_store import _make_record

    coll = "knowledge__rfvyy617__bge-base-en-v15-768__v1"
    with HttpCatalogClient() as cat:
        owner = cat.register_owner("rfvyy617-owner", owner_type="curator", tumbler_prefix="72.1")
        doc = str(cat.register(owner, "flagged", content_type="knowledge", physical_collection=coll,
                               source_uri="file:///rfvyy617/f.md", file_path="f.md"))
        cat.set_sensitivity([doc], True, reason="rfvyy617")
    rec = _make_record(collection=coll, source_path="f.md", doc_id=doc)
    assert HttpDocumentAspectsStore().upsert(rec) is True

    runner = CliRunner()
    both = runner.invoke(main, ["catalog", "doctor", "--sensitive-in-aspect-eligible", "--sensitive-with-aspects"])
    assert both.exit_code == 1, both.output
    assert f"nx enrich delete --tumbler {doc}" in both.output
    assert "WARN" in both.output and "FAIL" in both.output

    only_warn = runner.invoke(main, ["catalog", "doctor", "--sensitive-in-aspect-eligible"])
    assert only_warn.exit_code == 0, "the warn row alone never changes the exit status"
    assert "WARN" in only_warn.output


@substrate
def test_nx_doctor_supplementary_rows_are_not_applicable_on_a_virgin_tenant_and_never_gate(t2_service_env, capsys) -> None:
    from nexus.commands import doctor as doctor_mod

    assert "sensitive-in-aspect-eligible" in doctor_mod._SUPPLEMENTARY_CHECK_NAMES
    assert "sensitive-with-aspects" in doctor_mod._SUPPLEMENTARY_CHECK_NAMES
    doctor_mod._run_check_sensitive_in_aspect_eligible()
    doctor_mod._run_check_sensitive_with_aspects()
    out = capsys.readouterr().out
    assert out.count("not applicable") == 2 and "[✓]" not in out


def _failing_report() -> dict:
    return {"name": sd.WITH_ASPECTS, "status": sd.FAIL, "reason": "", "sensitive_documents": 1,
            "findings": [{"tumbler": "1.1.1", "collection": "c", "why": "has an aspect row"}]}


def test_nx_doctor_supplementary_fail_row_prints_its_remedy_and_reports_the_failure(monkeypatch, capsys) -> None:
    """RDR-243 item 10: the FAIL row gates the sweep. The row prints (it never raises Exit into the
    supplementary runner) and RETURNS True so the sweep can fail; a pass or not-applicable row returns False."""
    from nexus.commands import doctor as doctor_mod

    monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda **kw: _failing_report())
    assert doctor_mod._run_check_sensitive_with_aspects() is True
    assert "nx enrich delete --tumbler 1.1.1" in capsys.readouterr().out

    ok = {**_failing_report(), "status": sd.PASS, "findings": []}
    na = {**_failing_report(), "status": sd.NOT_APPLICABLE, "findings": [], "reason": "no sensitive documents"}
    for report in (ok, na):
        monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda report=report, **kw: report)
        assert doctor_mod._run_check_sensitive_with_aspects() is False


def _only_the_sensitive_rows(monkeypatch) -> None:
    from nexus.commands import doctor as doctor_mod

    for name in dir(doctor_mod):
        if name.startswith("_run_check_") and name not in (
            "_run_check_sensitive_with_aspects", "_run_check_sensitive_in_aspect_eligible",
        ):
            monkeypatch.setattr(doctor_mod, name, lambda *a, **k: None)


def test_the_supplementary_sweep_reports_a_failed_sensitive_row_and_only_that_one(monkeypatch, capsys) -> None:
    from nexus.commands import doctor as doctor_mod

    _only_the_sensitive_rows(monkeypatch)
    warn = {**_failing_report(), "name": sd.IN_ELIGIBLE, "status": sd.WARN}
    monkeypatch.setattr(sd, "check_sensitive_in_aspect_eligible", lambda **kw: warn)
    monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda **kw: {**_failing_report(), "status": sd.PASS, "findings": []})
    assert doctor_mod._run_supplementary_checks() is False, "a WARN row, and a PASS row, do not gate"

    monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda **kw: _failing_report())
    assert doctor_mod._run_supplementary_checks() is True
    assert "FAIL" in capsys.readouterr().out


@substrate
def test_nx_doctor_exits_non_zero_when_the_sensitive_with_aspects_row_fails(monkeypatch) -> None:
    """The sweep's exit code: a healthy main sweep plus a FAIL sensitive_with_aspects row is exit 1."""
    import nexus.commands.doctor as doctor_mod
    import nexus.health as health_mod
    from nexus.health import HealthResult

    monkeypatch.setattr(health_mod, "run_health_checks", lambda *a, **k: ([HealthResult(label="ok", ok=True)], False))
    _only_the_sensitive_rows(monkeypatch)
    monkeypatch.setattr(sd, "check_sensitive_in_aspect_eligible", lambda **kw: {**_failing_report(), "status": sd.PASS, "findings": [], "name": sd.IN_ELIGIBLE})

    monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda **kw: _failing_report())
    failing = CliRunner().invoke(main, ["doctor"])
    assert failing.exit_code == 1, failing.output
    assert "nx enrich delete --tumbler 1.1.1" in failing.output

    monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda **kw: {**_failing_report(), "status": sd.PASS, "findings": []})
    clean = CliRunner().invoke(main, ["doctor"])
    assert clean.exit_code == 0, clean.output


def test_the_row_does_not_walk_the_catalog_when_the_engine_cannot_hold_the_flag() -> None:
    walked: list[int] = []

    class Unsupported(_Reader):
        def all_documents(self, limit=0):
            walked.append(1)
            return super().all_documents(limit)

    for check, kw in ((sd.check_sensitive_with_aspects, {"queue": object(), "queued_doc_ids": lambda q: set()}),
                      (sd.check_sensitive_in_aspect_eligible, {"select": lambda c: object()})):
        report = check(reader=Unsupported([_doc("1.1.1")], supported=False), aspects=_Aspects(), **kw)
        assert report["status"] == sd.NOT_APPLICABLE
    assert walked == [], "the full-catalog walk is skipped when the engine does not advertise the capability"
