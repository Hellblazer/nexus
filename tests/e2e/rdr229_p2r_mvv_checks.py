# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.R (nexus-p2m1x.16): the Minimum Viable Validation, Phase 2 half, checks 1 to 4.

Driven by ``tests/e2e/rdr229-p2r-mvv.sh`` and by nothing else: the file is not named ``test_*.py``, so no
default or integration run collects it, and it refuses to run without ``P2R_RESULTS`` (the gate's results file).
The gate script builds the stamped service jar, runs this one test on the hermetic engine substrate
(``t2_service_env``: a throwaway PG and the jar of this checkout, a minted tenant, never production), and adds
check 5 (the two census gates) itself.

One fixture tenant, one collection. The collection is named ``knowledge__<name owner>__<name model>__v1`` and its
catalog row says another owner and the engine's own model, so a reader that parses the name gets both wrong and a
reader that reads the row gets both right. The row is made the way a rename and catalog-044 leave one: registered
with the row's model under the other name, then its owner corrected by SQL (the gate's own database). The
checks run in sequence against that one collection:

1. ``nx index md`` into it stamps the row's model into chunk metadata.
2. ``nx search`` calibrates and thresholds with the row's model.
3. The engine quarantines one of its chunks into a sibling whose ``lifecycle_state`` column says ``quarantine``,
   and the engine's lifecycle guards read that column, not the ``quarantine-`` prefix.
4. A recovery bundle recorded under legacy names restores.

Each check records ``{n, name, status, evidence, error}`` to ``$P2R_RESULTS`` (one JSON line) whatever happens in
it, and the test fails at the end when any did not pass, so a red check never hides the ones after it. A check
that examined nothing fails (non-vacuity): ``examined`` must be positive.

Expected red on develop until the Phase 2 steps land are listed by the gate's summary, not here. A check is
never weakened to be green.
"""
from __future__ import annotations

import hashlib
import json
import os
import traceback
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from click.testing import CliRunner

import nexus.catalog.recovery_bundle as rb
import nexus.mcp_infra as mi
from nexus.catalog.chunk_quarantine import now_stamp, quarantine_collection_name
from nexus.catalog.factory import make_catalog_reader
from nexus.cli import main
from nexus.corpus import discard_cached_registration, ensure_collection_registered
from nexus.db import make_t3
from nexus.db.http_vector_client import HttpVectorClient
from nexus.scoring import _resolve_calibration_factors
from nexus.search_engine import _threshold_for_collection_model, search_cross_corpus
from nexus.sensitivity import read_sensitivity
from tests._chunk_seed import _lit, _pg_state, _psql_superuser, seed_chunks_direct
from tests._reapable_age import age_chunks_past_grace

pytestmark = [pytest.mark.t2_tenant]

#: The model the substrate engine embeds with, and so the only one a registration may hold on it.
ROW_MODEL = "bge-base-en-v15-768"
#: The model the NAME claims: a Voyage one, which this engine cannot embed with and which a name-reading reader
#: would stamp, calibrate and threshold by.
NAME_MODEL = "voyage-context-3"
NAME_OWNER = "p2rnameowner"
ROW_OWNER = "p2r-row-owner"
QUERY = "p2r mvv unique marker phrase zebra quokka"


def _results_path() -> Path:
    raw = os.environ.get("P2R_RESULTS", "")
    if not raw:
        raise RuntimeError("P2R_RESULTS is not set: run this through tests/e2e/rdr229-p2r-mvv.sh")
    return Path(raw)


def _record(n: int, name: str, status: str, evidence: dict[str, Any], error: str = "") -> None:
    with _results_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"n": n, "name": name, "status": status, "evidence": evidence, "error": error},
                            sort_keys=True, default=str) + "\n")


def _run_check(n: int, name: str, fn: Callable[[], dict[str, Any]]) -> bool:
    """Run one check, record PASS or FAIL with its evidence, and say whether it passed. ``examined`` in the
    returned evidence must be a positive count: a check that looked at nothing is a FAIL, not a pass."""
    try:
        evidence = fn()
        examined = evidence.get("examined", 0)
        if not isinstance(examined, int) or examined <= 0:
            raise AssertionError(f"non-vacuity: the check examined {examined!r} items, it must examine at least one")
    except Exception as exc:  # noqa: BLE001 -- a check's failure is its result, recorded; the next check still runs
        detail = getattr(exc, "evidence", {})
        _record(n, name, "FAIL", detail if isinstance(detail, dict) else {}, f"{type(exc).__name__}: {exc}\n"
                + "".join(traceback.format_exception(exc)[-4:]))
        return False
    _record(n, name, "PASS", evidence)
    return True


class _CheckFailed(AssertionError):
    """An assertion that carries the evidence gathered before it tripped."""

    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _require(cond: bool, message: str, evidence: dict[str, Any]) -> None:
    if not cond:
        raise _CheckFailed(message, evidence)


# ── substrate helpers (the gate's own database) ─────────────────────────────────────────────────────────────


def _sql(sql: str) -> str:
    return _psql_superuser(_pg_state(), sql)


def _row(tenant: str, name: str) -> dict[str, str] | None:
    out = _sql(
        "SELECT owner_id, embedding_model, content_type, lifecycle_state FROM nexus.catalog_collections "
        f"WHERE tenant_id = {_lit(tenant)} AND name = {_lit(name)}")
    if not out:
        return None
    owner, model, ctype, state = out.split("|")
    return {"owner_id": owner, "embedding_model": model, "content_type": ctype, "lifecycle_state": state}


def _insert_row(tenant: str, name: str, *, owner: str, state: str) -> None:
    """A registry row with exactly *state*, whatever the name says (substrate SQL, not the code under test)."""
    _sql("INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, "
         f"lifecycle_state) VALUES ({_lit(tenant)}, {_lit(name)}, 'knowledge', {_lit(owner)}, "
         f"{_lit(ROW_MODEL)}, {_lit(state)})")


def _chunk_count(tenant: str, collection: str) -> int:
    return int(_sql(f"SELECT count(*) FROM nexus.chunks WHERE tenant_id = {_lit(tenant)} "
                    f"AND collection = {_lit(collection)}"))


def _chunk_models(collection: str, exclude: set[str]) -> Counter:

    got = HttpVectorClient().get_or_create_collection(collection).get(
        include=["metadatas"], limit=500, sensitivity="include")
    return Counter((m or {}).get("embedding_model", "")
                   for i, m in zip(got["ids"], got["metadatas"], strict=True) if i not in exclude)


def _engine_post(path: str, body: dict[str, Any]) -> tuple[int, str]:
    """A raw request to the engine: no client in the way, so the answer is the engine's own."""
    resp = httpx.post(os.environ["NX_SERVICE_URL"].rstrip("/") + path, json=body, timeout=60,
                      headers={"Authorization": "Bearer " + os.environ["NX_SERVICE_TOKEN"]})
    return resp.status_code, resp.text


def _json_array(output: str) -> list[dict[str, Any]]:
    """The JSON array a ``--json`` verb printed, skipping any log line ahead of it and any text after it."""
    lines = output.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("["))
    value, _end = json.JSONDecoder().raw_decode("".join(lines[start:]))
    return value


def _md(root: Path, label: str) -> Path:
    path = root / f"{label}.md"
    path.write_text(f"# {label}\n\n{QUERY}. P2R {label} prose {uuid.uuid4().hex}.\n", encoding="utf-8")
    return path


# ── the checks ──────────────────────────────────────────────────────────────────────────────────────────────


def check_index_md(ctx: dict[str, Any]) -> dict[str, Any]:
    """1. ``nx index md`` into the fixture collection stamps the ROW's model, not the name's token."""
    name, ctl = ctx["name"], ctx["control"]
    runner = CliRunner()
    seeded = {ctx["orphan"]}
    mi.invalidate_collections_cache()
    # the control collection, for check 2: a conformant bge name whose row agrees with it
    ctl_res = runner.invoke(main, ["index", "md", str(_md(ctx["root"], "control-md")), "--collection", ctl])
    res = runner.invoke(main, ["index", "md", str(_md(ctx["root"], "fixture-md")), "--collection", name])
    evidence: dict[str, Any] = {"collection": name, "name_model": NAME_MODEL, "row_model": ROW_MODEL,
                                "exit_code": res.exit_code, "cli_tail": res.output[-300:],
                                "control_exit_code": ctl_res.exit_code}
    _require(res.exit_code == 0, f"nx index md exited {res.exit_code}", evidence)
    _require(ctl_res.exit_code == 0,
             f"nx index md into the control exited {ctl_res.exit_code}: {ctl_res.output[-300:]}", evidence)
    stamps = _chunk_models(name, exclude=seeded)
    evidence.update(stamps=dict(stamps), examined=sum(stamps.values()))
    _require(sum(stamps.values()) > 0, "nx index md wrote no chunk into the fixture collection", evidence)
    _require(set(stamps) == {ROW_MODEL},
             f"chunk metadata embedding_model is {sorted(stamps)}, the row says {ROW_MODEL} and the name says {NAME_MODEL}",
             evidence)
    return evidence


def check_search_calibration(ctx: dict[str, Any]) -> dict[str, Any]:
    """2. ``nx search`` over the fixture and a control calibrates and thresholds by the row's model.

    The window spans two collections that embed with ONE model (the engine's). A name-reading calibrator sees
    ``voyage-context-3`` for one and bge for the other, takes two distinct models, and applies a per-model scale
    factor; a row-reading one sees a single model and every factor is exactly 1.0. The Voyage prefix threshold is
    withheld from a collection whose row model is local and given to one whose name token is Voyage, so the
    threshold the helper returns is the second discriminator. The helpers are the ones ``search_cross_corpus``
    hands to ``apply_hybrid_scoring`` and ``_threshold_for``, and the CLI is run beside them for the hits."""

    name, ctl = ctx["name"], ctx["control"]
    runner = CliRunner()
    cli = runner.invoke(main, ["search", QUERY, "--corpus", name, "--corpus", ctl, "--json", "-m", "20"])
    evidence: dict[str, Any] = {"cli_exit": cli.exit_code}
    _require(cli.exit_code == 0, f"nx search exited {cli.exit_code}: {cli.output[-300:]}", evidence)
    hits = _json_array(cli.output)
    by_col = Counter(h.get("collection") for h in hits)
    evidence["cli_hits_by_collection"] = dict(by_col)
    _require(by_col.get(name, 0) > 0 and by_col.get(ctl, 0) > 0,
             f"nx search did not return hits from both collections: {dict(by_col)}", evidence)

    holder: list = []
    results = search_cross_corpus(QUERY, [name, ctl], 20, make_t3(), sensitivity=read_sensitivity(),
                                  model_of_out=holder)
    model_of = holder[0]
    cfg = {"search": {"distance_threshold": {"knowledge": 0.65, "default": 0.7}}}
    factors = _resolve_calibration_factors(results, model_of=model_of)
    evidence.update(
        examined=len(results),
        model_of_fixture=model_of(name), model_of_control=model_of(ctl),
        name_only_model_of_fixture=NAME_MODEL,
        factors=factors,
        threshold_with_row=_threshold_for_collection_model(name, cfg, model_of=model_of),
        threshold_from_name_alone=_threshold_for_collection_model(name, cfg),
    )
    _require(model_of(name) == ROW_MODEL, f"the search resolved model {model_of(name)!r} for the fixture, "
             f"the row says {ROW_MODEL}", evidence)
    _require(evidence["threshold_from_name_alone"] == 0.65,
             "fixture non-vacuity: the name alone must select the Voyage prefix threshold, or the row-versus-name "
             "difference cannot show", evidence)
    _require(evidence["threshold_with_row"] is None,
             f"the row's local model must withhold the Voyage threshold, got {evidence['threshold_with_row']!r}", evidence)
    _require(set(factors.values()) == {1.0} and {name, ctl} <= set(factors),
             f"one model across the window must calibrate every collection by 1.0, got {factors}", evidence)
    return evidence


def check_quarantine_sibling(ctx: dict[str, Any]) -> dict[str, Any]:
    """3. A quarantine of one chunk registers a sibling whose lifecycle_state is ``quarantine``, and the engine
    reads that column, not the ``quarantine-`` prefix.

    (a) The engine's own mover quarantines the fixture's one orphan chunk; the sibling's ROW is read back from the
    database and must say ``quarantine`` (the prefix is the client's naming; the state is the engine's column).
    (b) A name the prefix rule misclassifies, in both directions, probed on the engine's own routes with no client
    in the way: a plain-named collection whose row says ``quarantine`` is refused a rename; a ``quarantine-``-named
    collection whose row says ``live`` is not. An engine that reads the prefix does the opposite of both."""

    name, tenant = ctx["name"], ctx["tenant"]
    mi.invalidate_collections_cache()
    sibling = quarantine_collection_name(name)
    before = _chunk_count(tenant, name)
    age_chunks_past_grace(name, tenant=tenant)
    evidence: dict[str, Any] = {"origin": name, "sibling": sibling, "origin_chunks_before": before}
    moved = HttpVectorClient().gc_quarantine_orphans_bounded(name, sibling, now_stamp(), 20, 1000)
    evidence.update(moved=moved.get("moved"), origin_chunks_after=_chunk_count(tenant, name),
                    sibling_chunks=_chunk_count(tenant, sibling),
                    sibling_row=_row(tenant, sibling), origin_row=_row(tenant, name))
    evidence["examined"] = int(moved.get("moved") or 0)
    _require(moved.get("moved") == 1, f"the mover moved {moved.get('moved')!r} chunks, the one orphan was expected", evidence)
    _require(evidence["sibling_chunks"] == 1 and evidence["origin_chunks_after"] == before - 1,
             "the quarantined chunk is not in the sibling, or the origin lost more than one chunk", evidence)
    _require(evidence["sibling_row"] is not None, "the mover registered no sibling row", evidence)
    _require(evidence["sibling_row"]["lifecycle_state"] == "quarantine",
             f"the sibling row's lifecycle_state is {evidence['sibling_row']['lifecycle_state']!r}", evidence)
    _require(evidence["origin_row"]["lifecycle_state"] == "live", "the origin row left 'live'", evidence)

    # (b) the prefix rule misclassifies both of these; the engine's guards read the row
    plain_q = f"knowledge__p2rplainq{uuid.uuid4().hex[:6]}__{ROW_MODEL}__v1"          # no prefix, row: quarantine
    prefixed_live = f"quarantine-knowledge__p2rlive{uuid.uuid4().hex[:6]}__{ROW_MODEL}__v1"  # prefix, row: live
    _insert_row(tenant, plain_q, owner="p2r-plainq", state="quarantine")
    _insert_row(tenant, prefixed_live, owner="p2r-live", state="live")
    target_a = f"knowledge__p2rto{uuid.uuid4().hex[:6]}__{ROW_MODEL}__v1"
    target_b = f"knowledge__p2rto{uuid.uuid4().hex[:6]}__{ROW_MODEL}__v1"
    code_a, text_a = _engine_post("/v1/catalog/collections/rename", {"old_name": plain_q, "new_name": target_a})
    code_b, text_b = _engine_post("/v1/catalog/collections/rename", {"old_name": prefixed_live, "new_name": target_b})
    evidence.update(
        plain_named_row_quarantine={"name": plain_q, "rename_status": code_a, "answer": text_a[:200]},
        prefix_named_row_live={"name": prefixed_live, "rename_status": code_b, "answer": text_b[:200]},
    )
    _require(code_a == 400 and "quarantine collection" in text_a,
             f"a plain-named collection whose row says quarantine was not refused (status {code_a}): "
             "the engine did not read the row", evidence)
    _require(_row(tenant, plain_q) is not None and _row(tenant, target_a) is None,
             "the refused rename still moved the row", evidence)
    _require(code_b == 200,
             f"a quarantine--named collection whose row says live was refused (status {code_b}): "
             "the engine read the prefix", evidence)
    return evidence


def check_recovery_bundle(ctx: dict[str, Any]) -> dict[str, Any]:
    """4. A recovery bundle recorded under legacy names restores: a legacy two-segment name and a conformant name
    that another install minted with its own embedding model, neither of which has a row on this install."""

    tenant = ctx["tenant"]
    legacy = f"knowledge__p2rlegacy{uuid.uuid4().hex[:6]}"
    other_install = f"knowledge__p2rother{uuid.uuid4().hex[:6]}__{NAME_MODEL}__v1"
    recorded = {"p2r legacy note": legacy, "p2r other install note": other_install}
    evidence: dict[str, Any] = {"recorded": recorded}
    for rec_name in recorded.values():
        _require(_row(tenant, rec_name) is None, f"fixture non-vacuity: {rec_name} already has a row", evidence)
    recs = [{"record": "knowledge_doc", "source_uri": "", "collection": col, "title": title, "tags": "p2r",
             "category": "ref", "content": f"{QUERY} body of {title} {uuid.uuid4().hex}"}
            for title, col in recorded.items()]
    bundle = ctx["root"] / "bundle.jsonl"
    rb.write_bundle(bundle, recs, [], rb.ExportSummary())
    res = CliRunner().invoke(main, ["catalog", "import", str(bundle)])
    evidence.update(exit_code=res.exit_code, cli_tail=res.output[-400:])
    _require(res.exit_code == 0, f"nx catalog import exited {res.exit_code}", evidence)

    t3, vec, reader = make_t3(), HttpVectorClient(), make_catalog_reader()
    docs = {d.title: d for d in reader.all_documents()}
    restored = {}
    for title, col in recorded.items():
        target = rb.target_collection_for(col, t3)
        doc = docs.get(title)
        _require(doc is not None and doc.physical_collection == target,
                 f"{title!r} was not restored into {target!r}", evidence)
        chashes = [r.chash for r in reader.get_manifest(str(doc.tumbler))]
        present = set(vec.existing_ids(target, chashes))
        restored[title] = {"target": target, "chunks": len(chashes), "present": len(present),
                           "target_row": _row(tenant, target)}
        _require(chashes and present == set(chashes), f"{title!r}: manifest {len(chashes)} chunks, "
                 f"{len(present)} present in {target!r}", {**evidence, "restored": restored})
        _require(restored[title]["target_row"] is not None and restored[title]["target_row"]["embedding_model"] == ROW_MODEL,
                 f"{target!r} has no row on this install's model", {**evidence, "restored": restored})
    evidence.update(restored=restored, examined=len(restored))
    return evidence


# ── the test ────────────────────────────────────────────────────────────────────────────────────────────────


def test_rdr229_p2r_mvv_phase2_half(t2_service_env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    results = _results_path()
    results.parent.mkdir(parents=True, exist_ok=True)
    tenant = t2_service_env
    suffix = uuid.uuid4().hex[:6]
    name = f"knowledge__{NAME_OWNER}{suffix}__{NAME_MODEL}__v1"
    control = f"knowledge__p2rctl{suffix}__{ROW_MODEL}__v1"
    mi.invalidate_collections_cache()

    # The fixture: the row holds the engine's model under a name that says Voyage, and an owner the name does
    # not carry (corrected by SQL, as catalog-044 left rows).
    ensure_collection_registered(name, kwargs={"content_type": "knowledge", "owner_id": f"{NAME_OWNER}{suffix}",
                                               "embedding_model": ROW_MODEL, "model_version": "v1"})
    ensure_collection_registered(control, kwargs={"content_type": "knowledge", "owner_id": f"p2rctl{suffix}",
                                                  "embedding_model": ROW_MODEL, "model_version": "v1"})
    _sql(f"UPDATE nexus.catalog_collections SET owner_id = {_lit(ROW_OWNER)} "
         f"WHERE tenant_id = {_lit(tenant)} AND name = {_lit(name)}")
    # One chunk the row can be read through from the first write (a collection with no chunk has no visible row,
    # item 1's NoRow outcome until P3.8), and the orphan check 3 quarantines.
    orphan = hashlib.sha256(f"{name}/orphan".encode()).hexdigest()
    seed_chunks_direct(name, [orphan], ["p2r orphan chunk text"], [{"title": "p2r orphan"}], tenant=tenant)
    discard_cached_registration(name)
    mi.invalidate_collections_cache()

    row = _row(tenant, name)
    name_owner, name_model = f"{NAME_OWNER}{suffix}", NAME_MODEL
    fixture_facts = {"name": name, "row": row, "name_owner": name_owner, "name_model": name_model,
                     "orphan_chunks": _chunk_count(tenant, name)}
    # non-vacuity of the fixture itself: the row differs from the name in both attributes, and has a chunk
    assert row is not None and row["owner_id"] == ROW_OWNER != name_owner, fixture_facts
    assert row["embedding_model"] == ROW_MODEL != name_model, fixture_facts
    assert fixture_facts["orphan_chunks"] == 1, fixture_facts
    _record(0, "fixture", "PASS", fixture_facts)

    ctx = {"name": name, "control": control, "tenant": tenant, "root": tmp_path, "orphan": orphan}
    checks = [
        (1, "nx index md stamps the row's embedding_model", check_index_md),
        (2, "nx search calibrates with the row's model", check_search_calibration),
        (3, "chunk quarantine registers a lifecycle_state=quarantine sibling; the engine reads the column", check_quarantine_sibling),
        (4, "a recovery bundle recorded under legacy names restores", check_recovery_bundle),
    ]
    passed = [_run_check(n, title, lambda fn=fn: fn(ctx)) for n, title, fn in checks]
    assert all(passed), f"MVV checks failed: {[n for (n, _t, _f), ok in zip(checks, passed, strict=True) if not ok]}"
