# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx doctor --check-search``: ``model_drift`` is a row-consistency check (nexus-p2m1x.48).

The expected model used to come from the label's first segment. Since 8.5.6 a new knowledge subject is registered by
attributes and gets an engine-minted ``c<id>`` label that names no content type, so the label rule answered
voyage-code-3 for each one and every such collection read as drift; the same rule read a ``quarantine-docs__`` or
``quarantine-rdr__`` twin the same way. The probe now compares the model the collection's own row ``content_type``
calls for (``corpus.canonical_embedding_model``) with the ``embedding_model`` the same row registers. It does NOT compare
the label's model token: once the one-row rename is live (8.5.6 and later, after the engine's restart), a rename changes
only the label (RDR-229), so a label's model token is not authority. The
label decides only for a collection whose row has no content type.

(Split from ``test_doctor_search.py``, which is at its file-size cap.)
"""
from __future__ import annotations

import json

import pytest

from nexus.doctor_search import run_retrieval_quality_probe as _REAL_PROBE
from tests.test_doctor_search import _stub_search_fn

_TWIN_DOCS = "quarantine-docs__1-2__voyage-context-3__v1"

# The tests that name a voyage model assert it as behaviour: they take cloud_mode (the RDR-109 mode lint).
_cloud = pytest.mark.usefixtures("cloud_mode")


class _ListingT3:
    """A listing plus what the probe reads of one collection (no chunk to sample); counts the listing calls."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.listings = 0

    def list_collections(self, *a, **k) -> list:
        self.listings += 1
        return list(self._rows)

    def get_or_create_collection(self, name: str):
        class _Col:
            def get(self, **_kw):
                return {"ids": [], "documents": []}

        return _Col()


def _check_search(monkeypatch, capsys, rows: list, meta: dict[str, str] | None = None):
    """``run_check_search`` over *rows* with the real probe and a stubbed search: (the printed JSON's outcomes by name,
    the exit code, the fake client). *meta* maps a name to the ``embedding_model`` its metadata reports."""
    import nexus.doctor_search as ds

    t3 = _ListingT3(rows)
    meta = meta or {}
    names = [r["name"] for r in rows if isinstance(r, dict) and r.get("name")]
    monkeypatch.setattr(ds, "_load_canaries", lambda: [])
    monkeypatch.setattr(ds, "run_name_resolution_probe", lambda *a, **k: [])
    monkeypatch.setattr(ds, "_make_t3", lambda: t3)
    monkeypatch.setattr(
        ds, "run_retrieval_quality_probe",
        lambda **k: _REAL_PROBE(search_fn=_stub_search_fn({n: (3, 0) for n in names}),
                                metadata_fn=lambda col: {"embedding_model": meta[col]}, **k),
    )
    code = 0
    try:
        ds.run_check_search(json_out=True)
    except SystemExit as exc:
        code = int(exc.code or 0)
    payload = json.loads(capsys.readouterr().out)
    retrieval = next(p for p in payload["probes"] if p["probe"] == "retrieval_quality")
    return {r["name"]: r["outcome"] for r in retrieval["results"]}, code, t3


def _row(name: str, content_type: str | None, **kw) -> dict:
    base = {"name": name, "count": 4, "stored_count": 4}
    if content_type:
        base.update(content_type=content_type, owner_id="1-9")
    return {**base, **kw}


# ── the expected model comes from the row ────────────────────────────────────────────────────────────────────────────────


@_cloud
def test_a_c_id_knowledge_collection_with_the_context_model_is_not_model_drift(monkeypatch, capsys) -> None:
    outcomes, code, _ = _check_search(monkeypatch, capsys, [_row("c12", "knowledge")], {"c12": "voyage-context-3"})
    assert outcomes == {"c12": "matched"} and code == 0


@_cloud
def test_a_c_id_knowledge_collection_with_the_code_model_is_model_drift(monkeypatch, capsys) -> None:
    outcomes, code, _ = _check_search(monkeypatch, capsys, [_row("c12", "knowledge")], {"c12": "voyage-code-3"})
    assert outcomes == {"c12": "model_drift"} and code == 2


@_cloud
def test_a_c_id_code_collection_expects_the_code_model(monkeypatch, capsys) -> None:
    rows = [_row("c3", "code")]
    outcomes, code, _ = _check_search(monkeypatch, capsys, rows, {"c3": "voyage-code-3"})
    assert (outcomes, code) == ({"c3": "matched"}, 0)
    outcomes, code, _ = _check_search(monkeypatch, capsys, rows, {"c3": "voyage-context-3"})
    assert (outcomes, code) == ({"c3": "model_drift"}, 2)


@_cloud
def test_a_real_drift_in_a_live_docs_collection_is_still_model_drift(monkeypatch, capsys) -> None:
    live = "docs__1-9__voyage-code-3__v1"  # a docs collection registered with the code model: the real thing
    outcomes, code, _ = _check_search(monkeypatch, capsys, [_row(live, "docs")], {live: "voyage-code-3"})
    assert outcomes == {live: "model_drift"} and code == 2


@_cloud
def test_a_renamed_collection_with_a_stale_model_token_in_its_label_is_not_drift(monkeypatch, capsys) -> None:
    # Once the one-row rename is live (8.5.6 and later, after the engine's restart) a rename changes only the label, so
    # a label's model token is not authority. The row's content type
    # and the row's registered model agree: that is what the probe checks, and the label is not compared.
    label = "docs__1-9__voyage-code-3__v1"  # the token says code; the row says docs registered with the context model
    outcomes, code, _ = _check_search(monkeypatch, capsys, [_row(label, "docs")], {label: "voyage-context-3"})
    assert outcomes == {label: "matched"} and code == 0


# ── the row shapes the engine really sends for a quarantine twin ─────────────────────────────────────────────────────────


@_cloud
def test_a_quarantine_twin_row_as_the_engine_sends_it_is_not_model_drift(monkeypatch, capsys) -> None:
    # /v1/vectors/stats groups chunk rows, so a listed row has stored_count >= 1; its live count can be 0 (every chunk
    # tombstoned). The row carries its origin's content type and says quarantine in lifecycle_state.
    row = _row(_TWIN_DOCS, "docs", count=0, stored_count=7, lifecycle_state="quarantine")
    outcomes, code, _ = _check_search(monkeypatch, capsys, [row], {_TWIN_DOCS: "voyage-context-3"})
    assert outcomes == {_TWIN_DOCS: "matched"} and code == 0


@_cloud
def test_a_c_id_quarantine_twin_row_is_judged_by_its_origins_content_type(monkeypatch, capsys) -> None:
    row = _row("c8", "docs", count=0, stored_count=2, lifecycle_state="quarantine")
    outcomes, code, _ = _check_search(monkeypatch, capsys, [row], {"c8": "voyage-context-3"})
    assert (outcomes, code) == ({"c8": "matched"}, 0)


@_cloud
def test_a_row_whose_content_type_spells_the_quarantine_lead_is_judged_by_the_origin_type(monkeypatch, capsys) -> None:
    row = _row("c9", "quarantine-docs", lifecycle_state="quarantine")
    assert _check_search(monkeypatch, capsys, [row], {"c9": "voyage-context-3"})[:2] == ({"c9": "matched"}, 0)
    assert _check_search(monkeypatch, capsys, [row], {"c9": "voyage-code-3"})[:2] == ({"c9": "model_drift"}, 2)


@_cloud
def test_a_twin_registered_with_the_wrong_model_is_still_drift(monkeypatch, capsys) -> None:
    row = _row(_TWIN_DOCS, "docs", count=0, stored_count=7, lifecycle_state="quarantine")
    outcomes, code, _ = _check_search(monkeypatch, capsys, [row], {_TWIN_DOCS: "voyage-code-3"})
    assert outcomes == {_TWIN_DOCS: "model_drift"} and code == 2


# ── a row with no content type, and the fallbacks ────────────────────────────────────────────────────────────────────────


@_cloud
def test_a_row_with_no_content_type_is_judged_by_its_label(monkeypatch, capsys) -> None:
    # Only labels whose rule gives the right answer are pinned. A bare id label with no content type (c5) has no label
    # rule answer worth pinning: the rule reads it as code, which would be a wrong verdict for a knowledge collection,
    # and a row the engine sends always carries a content type.
    docs, code_label = "docs__1-9__voyage-context-3__v1", "code__1-8__voyage-code-3__v1"
    rows = [_row(docs, None), _row(code_label, None)]
    outcomes, code, _ = _check_search(monkeypatch, capsys, rows, {docs: "voyage-context-3", code_label: "voyage-code-3"})
    assert outcomes == {docs: "matched", code_label: "matched"} and code == 0
    outcomes, code, _ = _check_search(monkeypatch, capsys, rows, {docs: "voyage-code-3", code_label: "voyage-code-3"})
    assert outcomes[docs] == "model_drift" and code == 2


@_cloud
def test_an_unknown_content_type_falls_back_to_the_label_rather_than_raising() -> None:
    from nexus.doctor_search import _model_for_rows

    model_for = _model_for_rows([{"name": "weird__x", "content_type": "not-a-type", "owner_id": "1-1"}])
    assert model_for("weird__x") == "voyage-code-3"  # the label rule, as before


# ── one listing, and rows the engine could send malformed ────────────────────────────────────────────────────────────────


@_cloud
def test_the_tenant_is_listed_once_and_the_listing_serves_both_the_names_and_the_models(monkeypatch, capsys) -> None:
    outcomes, code, t3 = _check_search(monkeypatch, capsys, [_row("c12", "knowledge")], {"c12": "voyage-context-3"})
    assert outcomes == {"c12": "matched"} and t3.listings == 1


@_cloud
def test_a_malformed_listing_row_is_skipped_not_fatal(monkeypatch, capsys) -> None:
    rows = [_row("c12", "knowledge"), {"count": 3}, {"name": "", "content_type": "docs"}, "not a row", None,
            {"name": "c13", "content_type": None}]
    outcomes, code, _ = _check_search(monkeypatch, capsys, rows, {"c12": "voyage-context-3", "c13": "voyage-code-3"})
    # c13 has no content type and a bare label: the label rule (code) is the old behaviour, so its code model reads matched
    assert outcomes == {"c12": "matched", "c13": "matched"} and code == 0


def test_a_failing_listing_is_one_enumerate_error_row(monkeypatch, capsys) -> None:
    import nexus.doctor_search as ds

    class _Broken:
        def list_collections(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(ds, "_load_canaries", lambda: [])
    monkeypatch.setattr(ds, "run_name_resolution_probe", lambda *a, **k: [])
    monkeypatch.setattr(ds, "_make_t3", lambda: _Broken())
    with pytest.raises(SystemExit) as exc:
        ds.run_check_search(json_out=True)
    assert exc.value.code == 2
    retrieval = next(p for p in json.loads(capsys.readouterr().out)["probes"] if p["probe"] == "retrieval_quality")
    assert [(r["name"], r["outcome"]) for r in retrieval["results"]] == [("<enumerate>", "error")]
