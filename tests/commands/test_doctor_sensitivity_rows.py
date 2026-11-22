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
from nexus.catalog.factory import _SharedServiceCatalogHandle
from nexus.catalog.sensitivity_doctor import SensitiveListUnavailable
from nexus.commands import doctor as doctor_mod
from nexus.commands.catalog_cmds.doctor import EXIT_COULD_NOT_CHECK
from nexus.cli import main
from nexus.service_handles import SharedClientSlot


def _doc(tumbler: str, collection: str = "knowledge__sens__bge-base-en-v15-768__v1", sensitive: bool = True):
    return SimpleNamespace(tumbler=tumbler, physical_collection=collection, sensitive=sensitive)


class _Reader:
    def __init__(self, docs, *, supported=True, raises=None):
        self._docs, self._supported, self._raises = docs, supported, raises

    def catalog_sensitivity_probe(self):
        return self._supported, "1.2.3" if self._supported else "0.0.1"

    def sensitive_documents(self, limit=1000, *, strict=False):
        """The engine's bounded list (``GET /sensitive``): the sensitive documents only, uncapped. The rows make
        no catalog walk, so this is the only read a row has (RDR-241, nexus-uwggq.1.8)."""
        if self._raises:
            raise self._raises
        sensitive = [d for d in self._docs if d.sensitive]
        return {"count": len(sensitive), "capped": False,
                "documents": [{"tumbler": d.tumbler, "physical_collection": d.physical_collection} for d in sensitive]}


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
    assert ("not applicable" in text or "not checked:" in text) and "[✓]" not in text
    assert "PASS" not in text.upper().replace("NOT APPLICABLE", "")


def test_a_list_that_fails_is_not_checked_with_the_cause_and_not_the_exception_text() -> None:
    report = sd.check_sensitive_in_aspect_eligible(
        reader=_Reader([], raises=OSError("engine unreachable")), aspects=_Aspects(), select=lambda c: object())
    assert report["status"] == sd.NOT_APPLICABLE
    assert report["reason"] == ("not checked: the engine could not be reached or failed; run nx doctor again; "
                              "if it persists, check the engine's logs or the token")


def test_an_engine_without_the_column_is_not_checked_and_says_what_to_do() -> None:
    report = sd.check_sensitive_with_aspects(reader=_Reader([_doc("1.1.1")], supported=False))
    assert report["reason"] == ("not checked: engine 0.0.1 does not advertise catalog_sensitivity_supported; "
                                "a local engine needs a newer engine-service, and on a managed engine the public "
                                "edge may trim the flag (ask the engine's operator)")


def test_an_unreadable_aspect_store_is_not_applicable_with_the_cause_not_a_false_red() -> None:
    class Broken(_Aspects):
        def get_by_doc_id(self, doc_id):
            raise OSError("aspects store down")

    report = sd.check_sensitive_with_aspects(
        reader=_Reader([_doc("1.1.1")]), aspects=Broken(), queue=object(), queued_doc_ids=lambda q: set())
    assert report["status"] == sd.NOT_APPLICABLE and "aspects store down" in report["reason"]
    assert report["reason"].startswith("not checked: the aspect state could not be read (OSError: ")
    assert report["reason"].endswith("); run nx doctor again") and sd.is_not_checked(report)


# ── sensitive_in_external_embedder (nexus-rfvyy.8.7) ────────────────────────

VOYAGE = "knowledge__ext__voyage-context-3__v1"
BGE = "knowledge__loc__bge-base-en-v15-768__v1"
MINILM = "knowledge__loc__minilm-l6-v2-384__v1"


def test_external_embedder_warns_on_a_sensitive_document_in_a_voyage_collection(cloud_mode) -> None:
    report = sd.check_sensitive_in_external_embedder(
        reader=_Reader([_doc("1.1.1", VOYAGE), _doc("1.1.2", VOYAGE, sensitive=False)]))
    assert report["status"] == sd.WARN
    assert [f["tumbler"] for f in report["findings"]] == ["1.1.1"], "only the sensitive document is named"
    assert report["findings"][0]["collection"] == VOYAGE
    assert "voyage-context-3" in report["findings"][0]["why"]
    text = "\n".join(sd.render_text(report))
    assert "1.1.1" in text and VOYAGE in text and "WARN" in text


def test_external_embedder_counts_a_legacy_two_segment_name_as_external() -> None:
    report = sd.check_sensitive_in_external_embedder(reader=_Reader([_doc("1.1.1", "knowledge__legacy")]))
    assert report["status"] == sd.WARN


@pytest.mark.parametrize("collection", [BGE, MINILM], ids=["bge", "minilm"])
def test_external_embedder_does_not_warn_on_a_local_model_collection(collection) -> None:
    report = sd.check_sensitive_in_external_embedder(reader=_Reader([_doc("1.1.1", collection)]))
    assert report["status"] == sd.PASS and report["findings"] == []
    assert report["sensitive_documents"] == 1, "a pass names how many documents it examined"


@pytest.mark.parametrize(
    "reader",
    [
        _Reader([]),
        _Reader([_doc("1.1.1", VOYAGE, sensitive=False)]),
        _Reader([_doc("1.1.1", VOYAGE)], supported=False),
        _Reader([], raises=OSError("engine unreachable")),
    ],
    ids=["empty-catalog", "no-sensitive-document", "engine-without-the-column", "catalog-unreadable"],
)
def test_external_embedder_with_nothing_examined_is_not_applicable_never_a_pass(reader) -> None:
    report = sd.check_sensitive_in_external_embedder(reader=reader)
    assert report["status"] == sd.NOT_APPLICABLE and report["findings"] == []
    text = "\n".join(sd.render_text(report))
    assert ("not applicable" in text or "not checked:" in text) and "[✓]" not in text


# ── sensitivity.mcp_output (nexus-rfvyy.8.7 note: the Day 2 Verify column) ──


def test_mcp_output_row_reports_withhold_as_the_default_and_does_not_warn() -> None:
    report = sd.check_sensitivity_mcp_output(value=None)
    assert report["status"] == sd.PASS and report["value"] == "withhold"
    assert "withhold" in "\n".join(sd.render_text(report))


def test_mcp_output_row_warns_when_it_is_flag() -> None:
    report = sd.check_sensitivity_mcp_output(value="flag")
    assert report["status"] == sd.WARN and report["value"] == "flag"
    text = "\n".join(sd.render_text(report))
    assert "flag" in text and "WARN" in text


def test_mcp_output_row_warns_on_an_unrecognised_value() -> None:
    report = sd.check_sensitivity_mcp_output(value="open")
    assert report["status"] == sd.WARN and "open" in report["reason"]


def test_mcp_output_row_reads_the_config_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yml").write_text("sensitivity:\n  mcp_output: flag\n")
    assert sd.check_sensitivity_mcp_output()["value"] == "flag"
    (tmp_path / "config.yml").write_text("{}\n")
    assert sd.check_sensitivity_mcp_output()["value"] == "withhold"


def test_nx_doctor_runs_the_two_new_rows_and_neither_gates(monkeypatch, capsys) -> None:
    from nexus.commands import doctor as doctor_mod

    assert "sensitive-in-external-embedder" in doctor_mod._SUPPLEMENTARY_CHECK_NAMES
    assert "sensitivity-mcp-output" in doctor_mod._SUPPLEMENTARY_CHECK_NAMES
    assert not {"sensitive-in-external-embedder", "sensitivity-mcp-output"} & doctor_mod._GATING_SUPPLEMENTARY_CHECKS
    warn = {"name": sd.IN_EXTERNAL_EMBEDDER, "status": sd.WARN, "reason": "", "sensitive_documents": 1,
            "findings": [{"tumbler": "1.1.1", "collection": VOYAGE, "why": "x"}]}
    monkeypatch.setattr(sd, "check_sensitive_in_external_embedder", lambda **kw: warn)
    flag_report = sd.check_sensitivity_mcp_output(value="flag")
    monkeypatch.setattr(sd, "check_sensitivity_mcp_output", lambda **kw: flag_report)
    assert doctor_mod._run_check_sensitive_in_external_embedder() is None
    assert doctor_mod._run_check_sensitivity_mcp_output() is None
    out = capsys.readouterr().out
    assert "sensitive_in_external_embedder" in out and "sensitivity_mcp_output" in out


# ── nx catalog doctor --sensitive-with-aspects: the exit status when the row could not look ───────


class _NoList(_Reader):
    """An engine that does not answer the sensitive list."""

    def sensitive_documents(self, limit=1000, *, strict=False):
        raise SensitiveListUnavailable(SensitiveListUnavailable.TIMEOUT)


def _run_catalog_doctor(monkeypatch, reader, *flags):
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: reader)
    return CliRunner().invoke(main, ["catalog", "doctor", *flags])


def test_the_could_not_check_exit_code_is_its_own_code() -> None:
    assert EXIT_COULD_NOT_CHECK == 3
    assert EXIT_COULD_NOT_CHECK not in {0, 1, 2}, "pass, fail and a usage error keep their codes"


def test_catalog_doctor_with_aspects_exits_could_not_check_when_the_row_reads_not_checked(monkeypatch) -> None:
    result = _run_catalog_doctor(monkeypatch, _NoList([_doc("1.1.1")]), "--sensitive-with-aspects")
    assert result.exit_code == EXIT_COULD_NOT_CHECK, result.output
    assert "not checked: the engine did not answer in time or was unavailable; run nx doctor again" in result.output
    assert "[✓]" not in result.output


def test_catalog_doctor_with_aspects_json_still_prints_the_row_when_it_exits_could_not_check(monkeypatch) -> None:
    result = _run_catalog_doctor(monkeypatch, _NoList([_doc("1.1.1")]), "--sensitive-with-aspects", "--json")
    assert result.exit_code == EXIT_COULD_NOT_CHECK, result.output
    payload = json.loads(result.stdout)
    assert payload["sensitive_with_aspects"]["status"] == "not_applicable"
    assert payload["sensitive_with_aspects"]["reason"].startswith("not checked:")


def test_catalog_doctor_with_aspects_exits_zero_on_a_catalog_with_no_sensitive_document(monkeypatch) -> None:
    """It looked and found none: not "could not check"."""
    result = _run_catalog_doctor(monkeypatch, _Reader([_doc("1.1.1", sensitive=False)]), "--sensitive-with-aspects")
    assert result.exit_code == 0, result.output
    assert "not applicable - no sensitive documents in this catalog" in result.output


def test_catalog_doctor_with_aspects_still_exits_one_on_a_fail(monkeypatch) -> None:
    monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda **kw: _failing_report())
    result = _run_catalog_doctor(monkeypatch, _Reader([]), "--sensitive-with-aspects")
    assert result.exit_code == 1, result.output


def test_catalog_doctor_with_aspects_exits_zero_on_a_pass(monkeypatch) -> None:
    passing = {"name": sd.WITH_ASPECTS, "status": sd.PASS, "reason": "", "sensitive_documents": 1, "findings": []}
    monkeypatch.setattr(sd, "check_sensitive_with_aspects", lambda **kw: passing)
    result = _run_catalog_doctor(monkeypatch, _Reader([]), "--sensitive-with-aspects")
    assert result.exit_code == 0, result.output


def test_the_warn_rows_reading_not_checked_do_not_change_the_exit_status(monkeypatch) -> None:
    """Only the gating row has a "could not check" status; the WARN rows keep "does not change the exit status"."""
    result = _run_catalog_doctor(monkeypatch, _NoList([_doc("1.1.1")]),
                                 "--sensitive-in-aspect-eligible", "--sensitive-in-external-embedder")
    assert result.exit_code == 0, result.output
    assert result.output.count("not checked:") == 2


def test_nx_doctor_keeps_a_not_checked_row_non_gating(monkeypatch, capsys) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _NoList([_doc("1.1.1")]))
    assert doctor_mod._run_check_sensitive_with_aspects() is False
    assert "not checked:" in capsys.readouterr().out


# ── a reader whose attribute reads raise: the production handle when the service client cannot be built ─────


def _handle_over_a_failing_build():
    """The real lazy handle over a slot whose build raises: ``__getattr__`` builds the client and re-raises the failure,
    which is a RuntimeError and not an AttributeError, so ``getattr(reader, name, default)`` does not return the
    default. No client is ever built, so nothing touches an engine."""
    def build():
        raise RuntimeError("service client cannot be built")

    return _SharedServiceCatalogHandle(SharedClientSlot(build=build, close=lambda client: None))


def test_a_reader_whose_attribute_reads_raise_is_not_checked_and_the_handler_does_not_raise() -> None:
    report = sd.check_sensitive_with_aspects(reader=_handle_over_a_failing_build(), aspects=_Aspects(),
                                             queue=object(), queued_doc_ids=lambda q: set())
    assert report["status"] == sd.NOT_APPLICABLE and sd.is_not_checked(report), report
    assert report["reason"].startswith("not checked: the catalog could not be read (RuntimeError: ")


def test_the_scrubber_helper_survives_a_reader_whose_base_url_read_raises() -> None:
    class Hostile:
        @property
        def _base_url(self):
            raise RuntimeError("no client")

    assert sd._scrubbed(ValueError("plain text"), Hostile()) == "plain text"
    assert sd._scrubbed(ValueError("plain text"), None) == "plain text"


def test_catalog_doctor_with_aspects_exits_could_not_check_when_the_service_client_cannot_be_built(monkeypatch) -> None:
    """Exit 3, not 1 and not a traceback: the state the exit code exists for."""
    result = _run_catalog_doctor(monkeypatch, _handle_over_a_failing_build(), "--sensitive-with-aspects")
    assert result.exit_code == EXIT_COULD_NOT_CHECK, (result.output, repr(result.exception))
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert "not checked: the catalog could not be read (RuntimeError: " in result.output


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
def test_the_default_queue_read_fails_on_a_live_tuple_task_and_passes_over_a_dead_one(t2_service_env) -> None:
    """nexus-hbnml: the row's DEFAULT ``queued_doc_ids`` (no injection) reads the ``aspects`` tuple
    subspace. Before the fix it called the retired table's ``list_waiting``; the AttributeError landed in
    the row's catch-all and the row said NOT APPLICABLE, so a flagged document with a queued task never
    failed. A live (available) task fails the row. A dead one does not: requeue-failed never revives the
    task of a flagged document and the task holds no text, so it would hold the row red with no remedy."""
    import json as _json

    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.db.t2.http_tuple_store import HttpTupleStore

    coll = "knowledge__hbnmldoc__bge-base-en-v15-768__v1"
    keys = {"queue": "aspects"}
    store = HttpTupleStore()

    def task(doc: str, path: str) -> None:
        body = _json.dumps({"collection": coll, "source_path": path, "doc_id": doc, "content_hash": "h"},
                           separators=(",", ":"))
        store.out("aspects", keys, None, body, nonce=doc)

    with HttpCatalogClient() as cat:
        owner = cat.register_owner("hbnmldoc-owner", owner_type="curator", tumbler_prefix="73.1")
        dead_doc, live_doc = (
            str(cat.register(owner, name, content_type="knowledge", physical_collection=coll,
                             source_uri=f"file:///hbnmldoc/{name}.md", file_path=f"{name}.md"))
            for name in ("dead", "live")
        )
        cat.set_sensitivity([dead_doc, live_doc], True, reason="hbnml")
    task(dead_doc, "dead.md")
    got = store.inp("aspects", keys, claimant="hbnml-dead", lease_s=60)
    assert got is not None and _json.loads(got[0].body)["doc_id"] == dead_doc
    store.nack(got[1], "hbnml-dead", dead=True)

    only_dead = sd.check_sensitive_with_aspects()
    assert only_dead["status"] == sd.PASS, only_dead

    task(live_doc, "live.md")
    report = sd.check_sensitive_with_aspects()
    assert report["status"] == sd.FAIL, report
    assert [f["tumbler"] for f in report["findings"]] == [live_doc]
    assert report["findings"][0]["why"] == "has a queued aspect task"


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


@substrate
def test_catalog_doctor_external_embedder_row_on_the_engine(t2_service_env) -> None:
    """One journey: a virgin tenant reads not applicable (JSON and text, never a pass), then a flagged document in
    a Voyage collection warns and one in a bge collection is not named. Merged into one test because the second
    half needs the first half's tenant (one tenant minted, not two)."""
    runner = CliRunner()
    result = runner.invoke(main, ["catalog", "doctor", "--sensitive-in-external-embedder", "--json"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    payload = json.loads("\n".join(lines[next(i for i, l in enumerate(lines) if l.startswith("{")):]))
    assert payload["sensitive_in_external_embedder"]["status"] == "not_applicable"
    text = runner.invoke(main, ["catalog", "doctor", "--sensitive-in-external-embedder"])
    assert text.exit_code == 0 and "not applicable" in text.output and "[✓]" not in text.output

    from nexus.catalog.http_catalog_client import HttpCatalogClient

    with HttpCatalogClient() as cat:
        owner = cat.register_owner("rfvyy87-owner", owner_type="curator", tumbler_prefix="74.1")
        ext = str(cat.register(owner, "ext", content_type="knowledge", physical_collection=VOYAGE,
                               source_uri="file:///rfvyy87/ext.md", file_path="ext.md"))
        loc = str(cat.register(owner, "loc", content_type="knowledge", physical_collection=BGE,
                               source_uri="file:///rfvyy87/loc.md", file_path="loc.md"))
        cat.set_sensitivity([ext, loc], True, reason="rfvyy87")
    result = runner.invoke(main, ["catalog", "doctor", "--sensitive-in-external-embedder"])
    assert result.exit_code == 0, "a warn row never changes the exit status"
    assert "WARN" in result.output and ext in result.output and VOYAGE in result.output
    assert loc not in result.output


def test_catalog_doctor_mcp_output_flag_warns_without_the_engine(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yml").write_text("sensitivity:\n  mcp_output: flag\n")
    result = CliRunner().invoke(main, ["catalog", "doctor", "--sensitivity-mcp-output"])
    assert result.exit_code == 0, result.output
    assert "flag" in result.output and "WARN" in result.output


# ── the row and the tools read one posture (nexus-rfvyy.8.7 / .8.2 cross-branch fix) ──


def test_mcp_output_row_ignores_a_per_repo_flag_the_tools_ignore(monkeypatch, tmp_path) -> None:
    """The tools read the GLOBAL config only (a cloned repo must not open the gate); the row said ``flag`` off a
    repo's ``.nexus.yml`` while the tools withheld, so a clean-looking doctor hid the opposite."""
    from nexus.sensitive_output import mcp_output_posture

    cfg, repo = tmp_path / "cfg", tmp_path / "repo"
    cfg.mkdir(), repo.mkdir()
    (cfg / "config.yml").write_text("{}\n")
    (repo / ".nexus.yml").write_text("sensitivity:\n  mcp_output: flag\n")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_REPO_ROOT", str(repo))
    assert mcp_output_posture() == "withhold"
    report = sd.check_sensitivity_mcp_output()
    assert report["value"] == "withhold" and report["status"] == sd.PASS


@pytest.mark.parametrize("text", [
    "{}\n", "sensitivity:\n  mcp_output: flag\n", "sensitivity:\n  mcp_output: ' Flag '\n",
    "sensitivity:\n  mcp_output: withhold\n", "sensitivity:\n  mcp_output: open\n", "sensitivity:\n  mcp_output: true\n",
    "sensitivity: flag\n", "sensitivity:\n  mcp_output: [flag]\n",
])
def test_mcp_output_row_value_is_always_the_posture_the_tools_apply(monkeypatch, tmp_path, text) -> None:
    from nexus.sensitive_output import mcp_output_posture

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yml").write_text(text)
    assert sd.check_sensitivity_mcp_output()["value"] == mcp_output_posture()


def test_mcp_output_row_warns_and_reports_withhold_when_the_config_is_unreadable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yml").write_text("sensitivity: [unclosed\n")
    report = sd.check_sensitivity_mcp_output()
    assert report["status"] == sd.WARN and report["value"] == "withhold" and report["reason"]


def test_mcp_output_row_reports_an_unrecognised_value_as_the_withhold_the_tools_apply(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    (tmp_path / "config.yml").write_text("sensitivity:\n  mcp_output: open\n")
    report = sd.check_sensitivity_mcp_output()
    assert report["status"] == sd.WARN and report["value"] == "withhold" and "open" in report["reason"]
