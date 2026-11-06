# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 1 fix round 1 (nexus-rfvyy.6.19): the unreadable-catalog branch of every document gate.

A gate that cannot read the catalog row must never read that as "not sensitive". Each case here makes the
catalog read fail (``doc_sensitivity._catalog_reader`` raises) and pins what the surface does instead, with the
control (a readable catalog) already pinned beside it in ``tests/test_aspect_sensitivity_gates.py``:

* ``read_source`` answers ``ReadFail("transport")`` (retried), never the text;
* ``extract_aspects`` and ``extract_aspects_batch`` raise ``SensitivityUnknown`` with no prompt built;
* the ``--validate-sample`` frame validates nothing and says why;
* ``nx aspects requeue-failed`` revives nothing, says why and exits 1 (the dry run too);
* the enqueue hook queues nothing. The drain's own unknown-flag retry is pinned in
  ``tests/test_aspect_sensitivity_gates.py`` (the tuple drain is the only drain since nexus-hbnml).

VACUITY: for each case the ``except SensitivityUnknown`` (or the ``raise SensitivityUnknown`` that feeds a
propagating one) was turned into a swallow and the case went red; the runs are in the fix-round report.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus import doc_sensitivity
from nexus.doc_sensitivity import SensitivityUnknown
from nexus.sensitivity import llm_bound
from tests.test_aspect_sensitivity_gates import (  # noqa: F401 — fixtures reused by name
    _COLL, _SECRET, _Log, _doc, _flag, _record, _task_docs, db, llm_calls, logs, model,
)

pytestmark = pytest.mark.usefixtures("t2_service_env")


@pytest.fixture(autouse=True)
def _no_document_is_sensitive() -> None:
    """Overrides tests/conftest.py's fixture of this name: the subject here is the real read failing."""


@pytest.fixture
def unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def down() -> Any:
        raise ConnectionError("catalog unreachable")

    monkeypatch.setattr(doc_sensitivity, "_catalog_reader", down)


def test_read_source_is_a_transport_failure_when_the_catalog_cannot_answer(tmp_path: Path, unreadable: None) -> None:
    from nexus.aspect_readers import ReadFail, ReadOk, read_source

    f = tmp_path / "unknown.md"
    f.write_text(f"{_SECRET}\n")

    with llm_bound("aspect_extraction_cli"):
        out = read_source(f.as_uri())

    assert not isinstance(out, ReadOk), "an unreadable flag must not release the text"
    assert isinstance(out, ReadFail) and out.reason == "transport", out
    assert "sensitivity unknown" in out.detail


def test_extract_aspects_raises_sensitivity_unknown_and_builds_no_prompt(llm_calls: list[str], unreadable: None) -> None:
    from nexus.aspect_extractor import extract_aspects

    with pytest.raises(SensitivityUnknown):
        extract_aspects(_SECRET, "/u.md", _COLL, doc_id_lookup=lambda c, s: "")
    assert llm_calls == []


def test_extract_aspects_batch_raises_sensitivity_unknown_and_builds_no_prompt(llm_calls: list[str], unreadable: None) -> None:
    from nexus.aspect_extractor import extract_aspects_batch

    with pytest.raises(SensitivityUnknown):
        extract_aspects_batch([(_COLL, "/u.md", _SECRET, "1.1.1")])
    assert llm_calls == []


def test_the_validation_sample_validates_nothing_and_says_why(
    logs: _Log, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], unreadable: None,
) -> None:
    from nexus.commands import enrich as enrich_mod

    doc = "1.1.1"
    extracted = [("/u.md", _record(_COLL, "/u.md"), "/u.md")]
    monkeypatch.setattr(enrich_mod, "_build_catalog_doc_id_lookup", lambda: (lambda coll, sid: doc))
    monkeypatch.setattr(enrich_mod, "_build_catalog_manifest_lookup", lambda: None)
    verified: list[str] = []

    async def verify(claim_json: str, evidence: str) -> dict:
        verified.append(evidence)
        return {"verified": True, "reason": ""}

    monkeypatch.setattr(enrich_mod, "_verify", verify)

    enrich_mod._run_validation_sample(
        extracted, collection=_COLL, sample_pct=100,
        validation_out=str(tmp_path / "f.jsonl"), validation_all_out=str(tmp_path / "a.jsonl"),
    )

    assert verified == [], "no sample was drawn from a frame whose sensitivity is unknown"
    assert "could not say which documents are sensitive" in capsys.readouterr().out
    assert logs.named("validate_sample_sensitivity_unknown")


def test_requeue_failed_revives_nothing_when_the_catalog_cannot_answer(
    db: Any, logs: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from nexus.commands.aspects import aspects_group
    from tests.test_aspect_sensitivity_gates import _dead, _states

    doc = _doc("requeue-unknown")
    _dead(db, doc, "/u.md")
    down = lambda: (_ for _ in ()).throw(ConnectionError("catalog unreachable"))  # noqa: E731
    monkeypatch.setattr(doc_sensitivity, "_catalog_reader", down)

    for argv in (["requeue-failed", "--dry-run"], ["requeue-failed"]):
        result = CliRunner().invoke(aspects_group, argv)
        assert result.exit_code == 1, (argv, result.output)
        assert "could not say whether the dead tasks' documents are sensitive" in result.output, result.output
        assert "Nothing was revived." in result.output, result.output
    assert _states(db) == {doc: "dead"}, "a task whose document may be sensitive stays dead"


def test_the_enqueue_hook_queues_nothing_when_the_catalog_cannot_answer(
    db: Any, logs: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.aspect_worker import aspect_extraction_enqueue_hook

    doc = _doc("enqueue-unknown")
    down = lambda: (_ for _ in ()).throw(ConnectionError("catalog unreachable"))  # noqa: E731
    monkeypatch.setattr(doc_sensitivity, "_catalog_reader", down)

    aspect_extraction_enqueue_hook("/u.md", _COLL, _SECRET, doc_id=doc)
    queued = _task_docs(db)

    assert queued == [], "a flag that cannot be read queues nothing"
    assert logs.named("aspect_enqueue_skipped_sensitivity_unknown")
    assert json.dumps(queued) == "[]"
