# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-243 Phase 1 Step 4b (nexus-rfvyy.6.14): the document gates of the aspect pipeline.

Census rows 1-6 of ``src/nexus/tables/llm-surfaces.toml``: the enqueue hook, the worker drain
(the tuple queue AND the retired table), ``extract_aspects`` and its batch form, ``nx enrich
aspects`` selection, the ``--validate-sample`` frame and the ``aspect_readers`` source reads.

Against the real engine substrate and the real catalog: a document is flagged through
``HttpCatalogClient.set_sensitivity`` and every gate reads the flag from that catalog row. The
only fakes are the model (a recorder that stands where the extractor would call it) and a
logger recorder. Every case also runs the plain control document through the same surface, so a
gate that refuses everything, or a surface that never ran, does not pass.

VACUITY: each gate was deleted in turn and its case went red; the runs are recorded in
nexus-rfvyy.6.14.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

from nexus.aspect_extractor import AspectRecord, ExtractFail
from nexus.aspect_worker import AspectExtractionWorker
from nexus.sensitivity import SensitiveDocumentRefused, llm_bound
from tests._catalog_fixture_ops import register_real_doc_id

pytestmark = pytest.mark.usefixtures("t2_service_env")

_SUB = "aspects"
_KEYS = {"queue": "aspects"}
_COLL = "knowledge__delos"
_SECRET = "SECRET-LEASE-MEMO"


@pytest.fixture(autouse=True)
def _no_document_is_sensitive() -> None:
    """Overrides tests/conftest.py's fixture of this name: this module's subject is the gates, so it
    reads the real catalog."""


class _Log:
    """A stand-in module logger that keeps (event, kwargs). ``capture_logs`` cannot see a logger
    that bound itself on first use."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, level: str):
        return lambda event, **kw: self.events.append((event, kw))

    def named(self, event: str) -> list[dict[str, Any]]:
        return [kw for name, kw in self.events if name == event]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _Log:
    log = _Log()
    for target in (
        "nexus.aspect_worker._log", "nexus.aspect_worker_tuples._log",
        "nexus.doc_sensitivity._log", "nexus.commands.enrich._log",
        "nexus.aspect_readers._log",
    ):
        monkeypatch.setattr(target, log)
    return log


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import nexus.mcp_infra as infra
    from nexus.db.t2 import T2Database

    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    d = T2Database(tmp_path / "t2.db")
    monkeypatch.setattr("nexus.commands._helpers.default_db_path", lambda: tmp_path / "t2.db")
    monkeypatch.setattr(infra, "t2_index_write", lambda fn, **kw: fn(d))
    yield d
    d.close()


def _doc(label: str, *, sensitive: bool = False, collection: str = _COLL, **register: Any) -> str:
    doc = register_real_doc_id(
        title=f"nx243 {label} {uuid.uuid4().hex[:6]}", physical_collection=collection, **register,
    )
    if sensitive:
        _flag(doc)
    return doc


def _flag(doc: str) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    with HttpCatalogClient() as cat:
        out = cat.set_sensitivity([doc], True, reason="RDR-243 gate test")
    assert out["updated"] == [1], f"the test document was not flagged: {out}"


def _record(collection: str, source_path: str) -> AspectRecord:
    return AspectRecord(
        collection=collection, source_path=source_path, problem_formulation="P",
        proposed_method="M", experimental_datasets=["d"], experimental_baselines=["b"],
        experimental_results="R", extras={}, confidence=0.9,
        extracted_at="2026-11-05T00:00:00+00:00", model_version="test", extractor_name="test",
    )


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The model, as the worker reaches it: every call to the extractor is recorded."""
    calls: list[dict[str, Any]] = []

    def single(content="", source_path="", collection="", **kw: Any):
        calls.append({"kind": "single", "content": content, "source_path": source_path, **kw})
        return _record(collection, source_path)

    def batch(items, **kw: Any):
        calls.append({"kind": "batch", "items": [tuple(i) for i in items]})
        return [_record(i[0], i[1]) for i in items]

    monkeypatch.setattr("nexus.aspect_worker._extract_aspects", single)
    monkeypatch.setattr("nexus.aspect_worker._extract_aspects_batch", batch)
    return calls


def _task_docs(db: Any) -> list[str]:
    return sorted(json.loads(r.body)["doc_id"] for r in db.tuples.rd(_SUB, dict(_KEYS), n=300))


def _out(db: Any, doc_id: str, source: str) -> None:
    db.tuples.out(
        _SUB, dict(_KEYS),
        body=json.dumps({"collection": _COLL, "source_path": source, "doc_id": doc_id, "content_hash": "h"}),
        nonce=doc_id,
    )


# ── Row 1: the enqueue hook ─────────────────────────────────────────────────────────────────────


def test_enqueue_of_a_sensitive_document_persists_nothing_and_logs(
    db: Any, logs: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.aspect_worker import aspect_extraction_enqueue_hook

    sensitive = _doc("enqueue-sensitive", sensitive=True)
    control = _doc("enqueue-control")

    aspect_extraction_enqueue_hook(f"/{sensitive}.md", _COLL, _SECRET, doc_id=sensitive)
    aspect_extraction_enqueue_hook(f"/{control}.md", _COLL, "plain text", doc_id=control)
    queued = _task_docs(db)

    assert queued == [control], f"only the control document is queued: {queued}"
    skipped = logs.named("aspect_enqueue_skipped_sensitive")
    assert [s["tumbler"] for s in skipped] == [sensitive], skipped
    assert skipped[0]["surface"] == "aspect_enqueue_hook" and skipped[0]["collection"] == _COLL


def test_the_indexers_aspect_batch_never_queues_a_sensitive_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``index_repository``'s flush hook queues through ``enqueue_many``, not the per-document hook. A document
    flagged after its first index is not queued when the repo is indexed again; its sibling is."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from tests.test_5r2sc_aspect_flush_batch_failure_tripwire import _index, _rdr_repo, _tasks_for

    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.delenv("NX_ASPECT_QUEUE", raising=False)
    name = f"gates-{uuid.uuid4().hex[:6]}"
    repo = _rdr_repo(tmp_path, name)
    _index(repo, tmp_path)
    first = _tasks_for(name)
    assert len(first) == 2, f"harness: the first index queued both RDRs: {first}"
    by_path = {b["source_path"]: b["doc_id"] for b in first}
    flagged_path = next(p for p in by_path if p.endswith("rdr-001-thing.md"))
    _flag(by_path[flagged_path])
    for i in (1, 2):  # new prose: the indexer re-chunks and fires the flush hook again
        path = repo / "docs" / "rdr" / f"rdr-00{i}-thing.md"
        path.write_text(path.read_text() + f"\n\nA later paragraph {i} of {name}, so the document changes.\n")
    for cmd in (["git", "add", "."], ["git", "commit", "-m", "second"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)

    from nexus.db.t2.http_aspect_queue import HttpAspectQueue
    from nexus.indexer import index_repository
    from nexus.registry import RepoRegistry

    handed: list[str] = []  # the doc ids the flush hook gave the queue, after its gate
    real = HttpAspectQueue.enqueue_many

    def spy(self: HttpAspectQueue, rows: list[dict]) -> int:
        handed.extend(r["doc_id"] for r in rows)
        return real(self, rows)

    monkeypatch.setattr(HttpAspectQueue, "enqueue_many", spy)
    index_repository(repo, RepoRegistry(tmp_path / "repos.json"), force=True)

    assert by_path[flagged_path] not in handed, f"the flagged document was handed to the queue: {handed}"
    sibling = next(d for p, d in by_path.items() if p != flagged_path)
    assert sibling in handed, f"the sibling document WAS handed to the queue (the flush hook ran): {handed}"
    with HttpCatalogClient() as cat:
        assert cat.resolve(by_path[flagged_path]).sensitive is True


def test_the_enqueue_filter_drops_flagged_rows_and_fails_closed(monkeypatch: pytest.MonkeyPatch, logs: _Log) -> None:
    from nexus import doc_sensitivity
    from nexus.aspect_worker import sensitive_enqueue_filter

    sensitive = _doc("filter-sensitive", sensitive=True)
    control = _doc("filter-control")
    rows = [
        {"collection": _COLL, "source_path": "/s.md", "content": _SECRET, "doc_id": sensitive},
        {"collection": _COLL, "source_path": "/c.md", "content": "", "doc_id": control},
    ]
    kept = sensitive_enqueue_filter(rows)
    assert [r["doc_id"] for r in kept] == [control]
    assert [s["tumbler"] for s in logs.named("aspect_enqueue_skipped_sensitive")] == [sensitive]

    def unreadable() -> Any:
        raise ConnectionError("catalog down")

    monkeypatch.setattr(doc_sensitivity, "_catalog_reader", unreadable)
    with pytest.raises(doc_sensitivity.SensitivityUnknown):
        sensitive_enqueue_filter(rows)


# ── Row 2: the worker drain after a late flag ───────────────────────────────────────────────────


def test_a_task_queued_before_the_flag_is_dropped_at_drain_on_the_tuple_queue(
    db: Any, model: list[dict[str, Any]], logs: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive = _doc("drain-sensitive")
    control = _doc("drain-control")
    _out(db, sensitive, "/sensitive.md")
    _out(db, control, "/control.md")
    assert _task_docs(db) == sorted([sensitive, control])
    _flag(sensitive)  # the flag arrives after the task was queued

    side = AspectExtractionWorker(poll_interval=0.1)._tuple_side
    claims, _ = side.claim_batch(park=False)
    assert len(claims) == 2
    for claim in claims:  # one at a time: a lone claim takes the single-document extractor
        side.process([claim])

    assert [c["source_path"] for c in model] == ["/control.md"], "the model saw the control document only"
    assert _task_docs(db) == [], "both tasks are consumed: the sensitive one acked, the control one done"
    assert db.document_aspects.get_by_doc_id(sensitive) is None, "nothing persisted for the sensitive document"
    skipped = logs.named("aspect_worker_skipped_sensitive")
    assert [s["tumbler"] for s in skipped] == [sensitive] and skipped[0]["surface"] == "aspect_worker_drain"


def test_a_sensitivity_read_that_fails_retries_the_task_and_sends_nothing(
    db: Any, model: list[dict[str, Any]], logs: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus import doc_sensitivity

    doc = _doc("drain-unknown")
    _out(db, doc, "/unknown.md")
    monkeypatch.setattr(doc_sensitivity, "_catalog_reader", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    side = AspectExtractionWorker(poll_interval=0.1)._tuple_side
    claims, _ = side.claim_batch(park=False)
    side.process(claims)

    assert model == [], "a flag that could not be read is not a clear: nothing reached the model"
    left = db.tuples.rd(_SUB, dict(_KEYS), n=10)
    assert len(left) == 1 and left[0].attempts == 1 and left[0].claim_state != "dead", "retried on the ladder, kept"
    assert logs.named("aspect_task_sensitivity_unknown")


def test_a_tuple_task_whose_document_became_sensitive_inside_extract_is_acked_not_retried(
    db: Any, logs: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim-time check saw a clear flag (patched); the extractor's own gate then refuses. The refusal is a
    verdict on the document: the task is acked as dropped, not nacked onto the retry ladder."""
    doc = _doc("tuple-race")
    _out(db, doc, "/race.md")

    def refuse(**kw: Any):
        raise SensitiveDocumentRefused(doc, "aspect_extraction_cli")

    monkeypatch.setattr("nexus.aspect_worker._extract_aspects", refuse)
    monkeypatch.setattr(AspectExtractionWorker, "_document_sensitive", staticmethod(lambda d: False))
    side = AspectExtractionWorker(poll_interval=0.1)._tuple_side
    claims, _ = side.claim_batch(park=False)
    side.process(claims)

    assert _task_docs(db) == [], "acked, so gone from the queue"
    assert [s["tumbler"] for s in logs.named("aspect_worker_skipped_sensitive")] == [doc]


# ── Rows 3 and 4: extract_aspects refuses ───────────────────────────────────────────────────────


@pytest.fixture
def llm_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The extractor's two LLM funnels, recorded."""
    import nexus.aspect_extractor as ae

    prompts: list[str] = []

    def single(prompt: str, config: Any) -> dict:
        prompts.append(prompt)
        return {}

    def batch(prompt: str, config: Any, *, timeout: int) -> dict:
        prompts.append(prompt)
        return {"papers": []}

    monkeypatch.setattr(ae, "_retry_subprocess", single)
    monkeypatch.setattr(ae, "_retry_subprocess_batch", batch)
    return prompts


def test_extract_aspects_refuses_a_sensitive_document_before_any_dispatch(
    llm_calls: list[str], logs: _Log,
) -> None:
    from nexus.aspect_extractor import extract_aspects

    sensitive = _doc("extract-sensitive", sensitive=True)
    control = _doc("extract-control")

    with pytest.raises(SensitiveDocumentRefused) as refused:
        extract_aspects(
            _SECRET, "/s.md", _COLL, doc_id_lookup=lambda c, s: sensitive,
        )
    assert refused.value.tumbler == sensitive and refused.value.surface is not None
    assert llm_calls == [], "no prompt was built for the sensitive document"

    record = extract_aspects(
        "plain text", "/c.md", _COLL, doc_id_lookup=lambda c, s: control,
    )
    assert isinstance(record, AspectRecord) and len(llm_calls) == 1 and "plain text" in llm_calls[0], (
        "the control document reached the model through the same surface"
    )


def test_extract_aspects_batch_gives_a_flagged_document_a_refused_slot_and_sends_the_rest(
    llm_calls: list[str],
) -> None:
    from nexus.aspect_extractor import extract_aspects_batch

    sensitive = _doc("xbatch-sensitive", sensitive=True)
    control = _doc("xbatch-control")
    out = extract_aspects_batch([
        (_COLL, "/s.md", _SECRET, sensitive),
        (_COLL, "/c.md", "plain text", control),
    ])

    assert isinstance(out[0], ExtractFail) and out[0].reason == "sensitive_document", out[0]
    assert isinstance(out[1], AspectRecord)
    assert len(llm_calls) == 1 and "plain text" in llm_calls[0] and _SECRET not in llm_calls[0]


# ── Rows 5 and 6: nx enrich aspects ─────────────────────────────────────────────────────────────


def test_enrich_aspects_selection_skips_sensitive_documents_and_the_summary_counts_them(
    logs: _Log, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from click.testing import CliRunner

    from nexus.commands import enrich as enrich_mod

    collection = f"knowledge__rdr243-{uuid.uuid4().hex[:8]}"
    sensitive = _doc("select-sensitive", sensitive=True, collection=collection)
    control = _doc("select-control", collection=collection)

    counts: dict[str, int] = {}
    entries = enrich_mod._select_entries(
        collection=collection, re_extract=False, extractor_version="", extract_all=True, counts=counts,
    )
    assert [str(e.tumbler) for e in entries] == [control]
    assert counts == {"sensitive": 1}
    assert [s["tumbler"] for s in logs.named("aspect_enrich_skipped_sensitive")] == [sensitive]

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    result = CliRunner().invoke(enrich_mod.enrich_aspects, [collection, "--all", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "1 sensitive skipped" in result.output, result.output
    assert "1 document(s)" in result.output, "the control document is still counted"


def test_the_validation_sample_is_drawn_from_eligible_documents_only(
    logs: _Log, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from nexus.commands import enrich as enrich_mod

    sensitive = [_doc(f"sample-sensitive-{i}", sensitive=True) for i in range(2)]
    controls = [_doc(f"sample-control-{i}") for i in range(2)]
    by_path = {f"/s{i}.md": d for i, d in enumerate(sensitive)} | {f"/c{i}.md": d for i, d in enumerate(controls)}
    extracted = [(p, _record(_COLL, p), p) for p in by_path]

    monkeypatch.setattr(enrich_mod, "_build_catalog_doc_id_lookup", lambda: (lambda coll, sid: by_path.get(sid, "")))
    monkeypatch.setattr(enrich_mod, "_build_catalog_manifest_lookup", lambda: None)
    verified: list[str] = []

    async def verify(claim_json: str, evidence: str) -> dict:
        verified.append(evidence)
        return {"verified": True, "reason": ""}

    monkeypatch.setattr(enrich_mod, "_verify", verify)
    monkeypatch.setattr(
        "nexus.aspect_extractor.read_indexed_text",
        lambda *, source_path, chunks_out=None, **kw: f"indexed text of {source_path} {_SECRET if source_path.startswith('/s') else ''}",
    )

    enrich_mod._run_validation_sample(
        extracted, collection=_COLL, sample_pct=50,
        validation_out=str(tmp_path / "f.jsonl"), validation_all_out=str(tmp_path / "a.jsonl"),
    )

    assert len(verified) == 1, "50% of the TWO eligible documents, not of the four extracted"
    assert _SECRET not in "".join(verified) and "/c" in verified[0]
    assert sorted(s["tumbler"] for s in logs.named("validate_sample_skipped_sensitive")) == sorted(sensitive)


# ── aspect_readers ──────────────────────────────────────────────────────────────────────────────


def test_a_source_read_of_a_sensitive_document_is_refused_under_the_scope(
    tmp_path: Path, logs: _Log, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nexus.aspect_readers import ReadOk, read_source

    sensitive_file = tmp_path / "sensitive.md"
    sensitive_file.write_text(f"{_SECRET}\n")
    control_file = tmp_path / "control.md"
    control_file.write_text("plain text\n")
    _doc("read-sensitive", sensitive=True, source_uri=sensitive_file.as_uri())
    _doc("read-control", source_uri=control_file.as_uri())

    with llm_bound("aspect_extraction_cli"):
        with pytest.raises(SensitiveDocumentRefused):
            read_source(sensitive_file.as_uri())
        ok = read_source(control_file.as_uri())
    assert isinstance(ok, ReadOk) and "plain text" in ok.text, "the control file reads under the same scope"

    monkeypatch.delenv("NX_SENSITIVITY_POSTURE")  # the suite's autouse blanket puts every test in the scope
    outside = read_source(sensitive_file.as_uri())
    assert isinstance(outside, ReadOk) and _SECRET in outside.text, (
        "the gate is the scope's: a default-posture read (indexing, doctor) is not refused"
    )
