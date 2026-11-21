# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.R (nexus-p2m1x.16): the Minimum Viable Validation, Phase 2 half, checks 1 to 4 and 6.

Driven by ``tests/e2e/rdr229-p2r-mvv.sh`` and by nothing else: the file is not named ``test_*.py``, so no
default or integration run collects it, and it refuses to run without ``P2R_RESULTS`` (the gate's results file).
The gate script builds the stamped service jar, runs these two tests on the hermetic engine substrate
(``t2_service_env``: a throwaway PG and the jar of this checkout, a minted tenant each, never production), and adds
check 5 (the two census gates) itself. Check 6 (P2.10, nexus-p2m1x.15) is the second test, in a tenant of its own
so its ``chroma://`` rows are the only ones the doctor and the backfill verb see.

One fixture tenant, one collection. The collection is named ``knowledge__<name owner>__<name model>__v1`` and its
catalog row says another owner and the engine's own model, so a reader that parses the name gets both wrong and a
reader that reads the row gets both right. The row is made the way a rename and catalog-044 leave one: registered
with the row's model under the other name, then its owner corrected by SQL (the gate's own database). The
checks run in sequence against that one collection:

1. ``nx index md`` into it stamps the row's model into chunk metadata.
2. PRE-SATISFIED GUARD: ``nx search`` calibrates and thresholds with the row's model. The row-preferred model
   resolver landed before Phase 2 (7476beecce, nexus-ml9vu), so this guards against a regression and proves no
   Phase 2 work.
3. The engine quarantines one of its chunks into a sibling whose ``lifecycle_state`` column says ``quarantine``,
   and the guards it probes (rename's source side) read that column, not the ``quarantine-`` prefix. It does not
   prove the engine never reads the prefix: the mover still requires a ``quarantine-`` sibling name and P2.5
   keeps named name reads (the engine census's QUARANTINE_READ ceiling, 36).
4. PRE-SATISFIED GUARD: a recovery bundle recorded under legacy names restores. P2.1's non-stranding tests made it
   hold before Phase 2 by design; this guards against a regression.

LIMIT OF CHECKS 1 AND 2: the substrate engine embeds with one model (bge), so the row's model here equals the
engine's default model. A reader that ignored the row and used the engine default would still pass, and the
checks tell "reads the row" from "reads the name" only. The name says Voyage on purpose, so a name reader fails.

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
import time
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
    except _GateError as exc:
        # the gate could not build the fixture or lacks a dependency: not a finding about the code under test
        _record(n, name, "ERROR", getattr(exc, "evidence", {}), f"GateError: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 -- a check's failure is its result, recorded; the next check still runs
        detail = getattr(exc, "evidence", {})
        _record(n, name, "FAIL", detail if isinstance(detail, dict) else {}, f"{type(exc).__name__}: {exc}\n"
                + "".join(traceback.format_exception(exc)[-4:]))
        return False
    _record(n, name, "PASS", evidence)
    return True


class _GateError(RuntimeError):
    """The gate's own fixture or dependency is wrong (no pg_background, a seed that did not take, a worker that is
    not running). Recorded as ``ERROR``, which the report turns into exit 2: it says nothing about the code under test."""

    def __init__(self, message: str, evidence: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.evidence = evidence or {}


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
    """1. ``nx index md`` into the fixture collection stamps the ROW's model, not the name's token. Limit: the row's
    model is also the engine's default (bge), so this cannot tell the row from the engine default, only from the
    name's Voyage token."""
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
    """2. PRE-SATISFIED GUARD (the row-preferred resolver is nexus-ml9vu, 7476beecce, before Phase 2): ``nx search``
    over the fixture and a control calibrates and thresholds by the row's model. Same limit as check 1: the row's
    model is the engine's default here.

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
    """3. A quarantine of one chunk registers a sibling whose lifecycle_state column says ``quarantine``, and the
    rename route's source-side guard reads that column.

    (a) The engine's own mover quarantines the fixture's one orphan chunk; the sibling's ROW is read back from the
    database and must say ``quarantine``.
    (b) A name the prefix rule misclassifies, in both directions, probed on the engine's own rename route with no
    client in the way: a plain-named collection whose row says ``quarantine`` is refused; a ``quarantine-``-named
    collection whose row says ``live`` is not. An engine that reads the prefix there does the opposite of both.

    WHAT THIS DOES NOT PROVE: that the engine never reads the prefix. The mover still requires the sibling's name to
    start with ``quarantine-`` (a registration-side name guard), and P2.5 keeps named name reads for names with no
    row (the engine census's QUARANTINE_READ ceiling, 36). It proves the state is a column the guard it probes reads."""

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
    """4. PRE-SATISFIED GUARD (P2.1's non-stranding tests held it before Phase 2): a recovery bundle recorded under
    legacy names restores: a legacy two-segment name and a conformant name
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


# ── check 6: P2.10 (nexus-p2m1x.15), URI moves leave residue alone and repaired residue is re-derived ───────────
#
# Keyed on behaviour and on the registered moves' labels, never on a changeset number. Three parts, each a finding
# about P2.10 when it trips; a fixture that does not take, a missing dependency or a worker that is not running is a
# gate ERROR (exit 2), not a FAIL. A surface that develop lacks (no registered repair move, a verb that refuses
# residue the rule relaxes) is a FAIL that says so and names P2.10.

RESIDUE_PREFIX = "rdr229-residue-"
RESIDUE_MOVES = (RESIDUE_PREFIX + "catalog-documents", RESIDUE_PREFIX + "document-aspects",
                 RESIDUE_PREFIX + "document-highlights")
DOCS_MOVE = RESIDUE_MOVES[0]
WORKER_DEADLINE_S = 300.0
BOOT_WORK_DEADLINE_S = 90.0
WORKER_ENTRY_IDS = ("data-move-worker", "data-move-boot-work")


def _json_object(output: str) -> dict[str, Any]:
    """The JSON object a ``--json`` verb printed, skipping any log line ahead of it."""
    lines = output.splitlines(keepends=True)
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith("{"))
        value, _end = json.JSONDecoder().raw_decode("".join(lines[start:]))
    except (StopIteration, ValueError) as exc:
        raise _GateError(f"a --json verb printed no JSON object: {output[-300:]!r}") from exc
    return value


def _boot_work() -> list[dict[str, Any]]:
    resp = httpx.get(os.environ["NX_SERVICE_URL"].rstrip("/") + "/v1/status", timeout=30)
    if resp.status_code != 200:
        raise _GateError(f"GET /v1/status answered {resp.status_code}")
    return list(resp.json().get("boot_work", []))


def _seed(what: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except _GateError:
        raise
    except Exception as exc:  # noqa: BLE001 -- any fixture failure is the gate's own error
        raise _GateError(f"fixture: {what} failed: {type(exc).__name__}: {exc}") from exc


def _p210_collection(ctx: dict[str, Any], name: str, content_type: str) -> None:
    """A registered collection holding one chunk, so its row is visible to a reader that lists collections."""
    def go() -> None:
        ensure_collection_registered(name, kwargs={"content_type": content_type, "owner_id": f"own-{name}",
                                                   "embedding_model": ROW_MODEL, "model_version": "v1"})
        seed_chunks_direct(name, [hashlib.sha256(f"{name}/chunk".encode()).hexdigest()], [f"p210 chunk of {name}"],
                           [{"title": name}], tenant=ctx["tenant"])
    _seed(f"collection {name}", go)
    mi.invalidate_collections_cache()


def _p210_doc(ctx: dict[str, Any], collection: str, uri: str, *, owner: Any = None,
              content_type: str = "prose") -> str:
    """A catalog document filed under *collection* whose source_uri is exactly *uri* (substrate SQL sets it: no
    client call spells a ``chroma://`` value). Returns the tumbler."""
    def go() -> str:
        ctx["n"] += 1
        n = ctx["n"]
        # a repo owner refuses a source_uri outside its repo_root (/r); the curator owner takes any
        root = "/r" if owner is not None else "/p2r210"
        tumbler = str(ctx["cat"].register(owner or ctx["owner"], f"p210-doc-{n}", content_type=content_type,
                                          physical_collection=collection, source_uri=f"file://{root}/{n}.md",
                                          file_path=f"p2r210-{n}.md", head_hash="H0"))
        _sql(f"UPDATE nexus.catalog_documents SET source_uri = {_lit(uri)} "
             f"WHERE tenant_id = {_lit(ctx['tenant'])} AND tumbler = {_lit(tumbler)}")
        if _doc_uri(ctx, tumbler) != uri:
            raise RuntimeError(f"source_uri of {tumbler} is not {uri!r} after the seed")
        return tumbler
    return _seed(f"document under {collection}", go)


def _file_path(ctx: dict[str, Any], tumbler: str) -> str:
    return _sql("SELECT file_path FROM nexus.catalog_documents "
                f"WHERE tenant_id = {_lit(ctx['tenant'])} AND tumbler = {_lit(tumbler)}")


def _doc_uri(ctx: dict[str, Any], tumbler: str) -> str:
    return _sql("SELECT source_uri FROM nexus.catalog_documents "
                f"WHERE tenant_id = {_lit(ctx['tenant'])} AND tumbler = {_lit(tumbler)}")


def _aspect_uri(ctx: dict[str, Any], tumbler: str) -> str:
    return _sql("SELECT source_uri FROM nexus.document_aspects "
                f"WHERE tenant_id = {_lit(ctx['tenant'])} AND doc_id = {_lit(tumbler)}")


def _doctor_documents() -> dict[str, Any]:
    """The ``catalog_documents`` table's counts from ``nx catalog doctor --uri-residue --json``: the repair moves run
    per table, so the document table is the one a single seeded row counts in once (the totals add the aspects row)."""
    res = CliRunner().invoke(main, ["catalog", "doctor", "--uri-residue", "--json"])
    report = _json_object(res.output).get("uri_residue") or {}
    if "counts" not in report or report.get("status") == "not_applicable":
        raise _GateError(f"nx catalog doctor --uri-residue produced no counts: {json.dumps(report)[:300]}")
    return report["counts"]["catalog_documents"]


def _backfill_dry_run() -> dict[str, Any]:
    res = CliRunner().invoke(main, ["catalog", "backfill-source-uri", "--json"])
    return _json_object(res.output)


def _p210_preflight(ctx: dict[str, Any]) -> None:
    if _sql("SELECT count(*) FROM pg_available_extensions WHERE name = 'pg_background'") != "1":
        raise _GateError("pg_background is not in the substrate PG: the data-move worker cannot run, so P2.10's repair "
                         "moves cannot be exercised. Use a PG bundle that carries it; this is not a pass")
    if _sql("SELECT count(*) FROM pg_namespace WHERE nspname = 'dml_utils'") != "1":
        raise _GateError("the engine did not install dml_utils into the substrate PG (no data-move worker); the gate "
                         "script sets NX_TEST_SUBSTRATE_DATA_MOVES=1 for tests/_engine_substrate.py to bootstrap it")
    stopped = [e for e in _boot_work() if e.get("id") in WORKER_ENTRY_IDS]
    if stopped:
        raise _GateError(f"the data-move worker is not running: {stopped}")


def _wait_for_moves(ev: dict[str, Any]) -> None:
    """Wait on the registry and on /v1/status boot_work: all three moves have completed_at, then boot_work lists none
    of them. Each wait is a bounded poll of a condition (a deadline, one read per second), not a sleep standing in
    for the condition."""
    deadline = time.monotonic() + WORKER_DEADLINE_S
    while True:
        rows = _sql("SELECT label || '|' || (completed_at IS NOT NULL) FROM nexus.data_moves "
                    f"WHERE label LIKE {_lit(RESIDUE_PREFIX + '%')} ORDER BY label")
        state = dict(r.split("|") for r in rows.splitlines() if r)
        work = _boot_work()
        ev["moves"] = state
        if [e for e in work if e.get("id") in WORKER_ENTRY_IDS]:
            raise _GateError(f"the data-move worker stopped while the repair moves ran: {work}")
        bad = [e for e in work if str(e.get("id", "")).startswith("data-move:" + RESIDUE_PREFIX)
               and str(e.get("what", "")).startswith(("failed:", "blocked:"))]
        if bad:
            raise _CheckFailed(f"a repair move failed or is blocked: {bad}", ev)
        if set(state) == set(RESIDUE_MOVES) and set(state.values()) == {"true"}:
            break
        if time.monotonic() > deadline:
            raise _CheckFailed(f"the repair moves did not complete in {WORKER_DEADLINE_S:.0f}s: {state}", ev)
        time.sleep(1)
    deadline = time.monotonic() + BOOT_WORK_DEADLINE_S
    while [e["id"] for e in _boot_work() if str(e.get("id", "")).startswith("data-move:" + RESIDUE_PREFIX)]:
        if time.monotonic() > deadline:
            raise _CheckFailed("boot_work still lists a repair move after all three completed", ev)
        time.sleep(1)


def _p210_part_repair(ctx: dict[str, Any]) -> dict[str, Any]:
    """(2) Residue already moved onto a prefix-free segment is repaired by registered data moves."""
    ev: dict[str, Any] = {}
    _p210_collection(ctx, "gate-stem", "docs")
    _p210_collection(ctx, "ctl-know", "knowledge")
    r = _p210_doc(ctx, "gate-stem", "chroma://gate-stem//abs/path/r.md")
    ctrl_note = _p210_doc(ctx, "ctl-know", "chroma://ctl-know//abs/note")
    ctrl_elsewhere = _p210_doc(ctx, "ctl-know", "chroma://elsewhere//abs/z.md")
    _seed("the aspects row of the repairable document", lambda: _sql(
        "INSERT INTO nexus.document_aspects (tenant_id, collection, source_path, extracted_at, model_version, "
        f"extractor_name, source_uri, doc_id) VALUES ({_lit(ctx['tenant'])}, 'gate-stem', 'abs/path/r.md', now(), "
        "'m', 'e', 'chroma://gate-stem//abs/path/r.md', " + _lit(r) + ")"))
    ctx["r"] = r
    before = _doctor_documents()
    ev["doctor_before"] = {"file_typed": before["chroma_prefix_free"]["file_typed"],
                           "other": before["chroma_prefix_free"]["other"],
                           "chroma_file_routed_segment": before["chroma_file_routed_segment"]}
    ft = before["chroma_prefix_free"]["file_typed"]
    if not (ft["rows"] == 1 and ft["segment_not_collection"] == 0 and before["chroma_file_routed_segment"] == 0):
        raise _GateError(f"fixture: the doctor does not see exactly the one repairable row before the moves: "
                         f"{ev['doctor_before']}", ev)

    labels = _sql(f"SELECT label FROM nexus.data_moves WHERE label LIKE {_lit(RESIDUE_PREFIX + '%')} ORDER BY label")
    ev["registered_moves"] = labels.splitlines()
    _require(DOCS_MOVE in labels.splitlines(),
             f"surface absent: no registered data move {DOCS_MOVE!r} (P2.10 registers the repair of residue already "
             "moved, one move per table); the engine never repairs it", ev)

    # The substrate engine booted before this fixture existed, so its worker found nothing and completed the moves
    # over an empty table. Re-open the documents move exactly as the changeset registered it (the satellites it
    # registers on completion go too: that INSERT has no ON CONFLICT), so the worker meets the fixture.
    cols = "label, template, driving_schema, driving_table, chunk_size, sleep_ms, statement_timeout_ms, verification, on_complete, changeset_id"
    _seed("re-opening the repair moves", lambda: _sql(
        f"CREATE TEMP TABLE p2r_move AS SELECT * FROM nexus.data_moves WHERE label = {_lit(DOCS_MOVE)};\n"
        "SELECT dml_utils.archive_migration_run(label) FROM dml_utils_data.migration_run "
        f"WHERE archived_at IS NULL AND label LIKE {_lit(RESIDUE_PREFIX + '%')};\n"
        f"DELETE FROM nexus.data_moves WHERE label LIKE {_lit(RESIDUE_PREFIX + '%')};\n"
        f"INSERT INTO nexus.data_moves ({cols}) SELECT {cols} FROM p2r_move;"))
    _wait_for_moves(ev)

    after = _doctor_documents()
    ev["doctor_after"] = {"file_typed": after["chroma_prefix_free"]["file_typed"],
                          "other": after["chroma_prefix_free"]["other"],
                          "chroma_file_routed_segment": after["chroma_file_routed_segment"]}
    ev["r_uri"], ev["r_aspect_uri"] = _doc_uri(ctx, r), _aspect_uri(ctx, r)
    ev["controls"] = {"knowledge_note": _doc_uri(ctx, ctrl_note), "segment_not_collection": _doc_uri(ctx, ctrl_elsewhere)}
    want = "chroma://docs__gate-stem//abs/path/r.md"
    _require(ev["r_uri"] == want, f"the repaired document's source_uri is {ev['r_uri']!r}, want {want!r} "
             "(a chroma:// value with the content type's segment, not file://)", ev)
    _require(ev["r_aspect_uri"] == want, f"its aspects row is {ev['r_aspect_uri']!r}, want {want!r}", ev)
    _require(after["chroma_prefix_free"]["file_typed"]["rows"] == 0 and after["chroma_file_routed_segment"] == 1,
             f"the doctor after the moves: {ev['doctor_after']}", ev)
    _require(ev["controls"] == {"knowledge_note": "chroma://ctl-know//abs/note",
                                "segment_not_collection": "chroma://elsewhere//abs/z.md"},
             f"a control row was touched: {ev['controls']}", ev)
    return ev


def _p210_part_backfill(ctx: dict[str, Any]) -> dict[str, Any]:
    """(3) The backfill verb re-derives moved and repaired residue to file://, and still refuses a relative path."""
    ev: dict[str, Any] = {}
    _p210_collection(ctx, "gate-stem2", "code")
    _p210_doc(ctx, "gate-stem2", "chroma://gate-stem2//abs/path/s.md")
    rep = _backfill_dry_run()
    ev["dry_run"] = {k: rep.get(k) for k in ("candidates", "rederivable", "refused", "total_chroma_rows")}
    ev["updates_now"] = sorted(u.get("now", "") for u in rep.get("updates", []))
    ev["refusals"] = [x.get("reason", "") for x in rep.get("refusals", [])]
    ev["by_collection"] = {k: v for k, v in rep.get("by_collection", {}).items() if k in ("gate-stem", "gate-stem2")}
    _require(rep.get("candidates") == 2 and rep.get("rederivable") == 2 and rep.get("refused") == 0,
             f"want candidates=2 rederivable=2 refused=0 over the repaired row and the code-typed prefix-free row, "
             f"got {ev['dry_run']}", ev)
    _require(len(ev["updates_now"]) == 2 and all(n.startswith("file://") for n in ev["updates_now"]),
             f"each update must re-derive to file://, got {ev['updates_now']}", ev)
    _require(all(ev["by_collection"].get(c, {}).get("candidate") is True for c in ("gate-stem", "gate-stem2")),
             f"both collections must be candidates in the census: {ev['by_collection']}", ev)
    _p210_doc(ctx, "gate-stem", "chroma://gate-stem/relative/x.md")
    ctl = _backfill_dry_run()
    reasons = [x.get("reason", "") for x in ctl.get("refusals", [])]
    ev["refusal_control"] = {"refused": ctl.get("refused"), "reasons": reasons}
    _require(ctl.get("refused") == 1 and reasons and reasons[0].startswith("parsed path is not absolute"),
             f"a relative path must be refused as 'parsed path is not absolute', got {ev['refusal_control']}", ev)
    return ev


def _shown(tumbler: str) -> tuple[str, str]:
    """``(source_uri, physical_collection)`` of a document as ``nx catalog show --json`` prints it."""
    res = CliRunner().invoke(main, ["catalog", "show", tumbler, "--json"])
    doc = _json_object(res.output)
    return str(doc.get("source_uri", "")), str(doc.get("physical_collection", ""))


def _mv_rename(ctx: dict[str, Any], old: str, new: str, docs: list[str]) -> None:
    res = CliRunner().invoke(main, ["collection", "rename", old, new])
    if res.exit_code != 0:
        raise RuntimeError(f"nx collection rename {old} {new} exited {res.exit_code}: {res.output[-400:]}")


def _mv_rename_to_prefix_free(ctx: dict[str, Any], old: str, new: str, docs: list[str]) -> None:
    # a rename that changes the content-type prefix is the operator override of the CLI's prefix guard
    res = CliRunner().invoke(main, ["collection", "rename", old, new, "--force-prefix-change"])
    if res.exit_code != 0:
        raise RuntimeError(f"nx collection rename {old} {new} --force-prefix-change exited {res.exit_code}: "
                           f"{res.output[-400:]}")


def _mv_rehome(ctx: dict[str, Any], old: str, new: str, docs: list[str]) -> None:
    _p210_collection(ctx, new, "docs")  # a re-home goes onto a live target
    for _ in range(5):
        code, text = _engine_post("/v1/catalog/collections/rehome", {"source": old, "target": new})
        if code != 200:
            raise RuntimeError(f"POST /v1/catalog/collections/rehome answered {code}: {text[:300]}")
        if json.loads(text).get("done"):
            return
    raise RuntimeError("the re-home was not done after five batches")


def _mv_update(ctx: dict[str, Any], old: str, new: str, docs: list[str]) -> None:
    for tumbler in docs:
        ctx["cat"].update(tumbler, physical_collection=new)


def _mv_reconcile_commit(ctx: dict[str, Any], old: str, new: str, docs: list[str]) -> None:
    paths = [_file_path(ctx, t) for t in docs]
    rows = [{"seq": i, "kind": "present", "file_path": path, "content_type": "docs", "title": path,
             "physical_collection": new, "head_hash": "H1", "source_mtime": 1.0, "content_hash": "",
             "base_metadata": {}, "stamp": {}, "sensitive": False} for i, path in enumerate(paths)]
    owner, run = str(ctx["repo_owner"]), f"p2r-{uuid.uuid4().hex[:8]}"
    ctx["cat"].reconcile_stage(owner, run, "partial", len(rows), rows)
    outcome = ctx["cat"].reconcile_commit(owner, run)
    actions = sorted(str(r.get("action")) for r in outcome.get("rows", []))
    if actions != ["updated"] * len(docs):
        raise RuntimeError(f"the reconcile commit did not repoint both documents by the update arm: {actions}")


#: The moves P2.10 names that a fixture can drive (RDR P2.10 text and item 7). A re-embed move is NOT driven: it
#: re-registers the documents under a sibling of another embedding model, and this substrate engine embeds with one
#: model only (the local bge-768), so there is no second model to move to. It is listed in the evidence, not faked.
_MOVE_PROBES = (
    ("rename to a prefixed label", "docs__gate-old", "docs__gate-new", _mv_rename),
    ("rename to a prefix-free label", "docs__gate-oldb", "plainb", _mv_rename_to_prefix_free),
    ("re-home onto a prefix-free label", "docs__gate-oldc", "plainc", _mv_rehome),
    ("collection update onto a prefix-free label", "docs__gate-oldd", "plaind", _mv_update),
    ("reconcile commit onto a prefix-free label", "docs__gate-olde", "plaine", _mv_reconcile_commit),
)


def _p210_part_moves(ctx: dict[str, Any]) -> dict[str, Any]:
    """(1) A move of a file-routed collection's documents leaves ``chroma://`` residue and an ``nx-note://`` value
    with a file-routed segment on their old segment, whichever move it is, and a note that names a non-file-routed
    label still follows its rename. Probes: rename onto a prefixed and onto a prefix-free label, re-home, collection
    update and reconcile commit onto a prefix-free label. Not driven: the re-embed move (see ``_MOVE_PROBES``)."""
    ev: dict[str, Any] = {"probes": {}, "not_driven": ["re-embed move: needs a second embedding model"]}
    failures: list[str] = []
    for label, old, new, move in _MOVE_PROBES:
        _p210_collection(ctx, old, "docs")
        residue, note = f"chroma://{old}//abs/path/r.md", f"nx-note://{old}/n"
        docs = [_p210_doc(ctx, old, residue, owner=ctx["repo_owner"], content_type="docs"),
                _p210_doc(ctx, old, note, owner=ctx["repo_owner"], content_type="docs")]
        try:
            move(ctx, old, new, docs)
        except Exception as exc:  # noqa: BLE001 -- a move the fixture cannot drive is the gate's own error
            raise _GateError(f"fixture: the {label} probe failed: {type(exc).__name__}: {exc}", ev) from exc
        got = [_shown(t) for t in docs]
        ev["probes"][label] = {"residue": got[0], "file_routed_note": got[1]}
        if [g[1] for g in got] != [new, new]:
            raise _GateError(f"fixture: the {label} probe did not move the documents: {got}", ev)
        if got[0][0] != residue:
            failures.append(f"{label}: chroma:// residue is now {got[0][0]!r}, it must keep {residue!r}")
        if got[1][0] != note:
            failures.append(f"{label}: the file-routed nx-note:// value is now {got[1][0]!r}, it must keep {note!r}")

    # the control that keeps the rule narrow: a note of a non-file-routed label follows its rename
    _p210_collection(ctx, "plainstem", "knowledge")
    b = _p210_doc(ctx, "plainstem", "nx-note://plainstem/n1")
    _mv_rename(ctx, "plainstem", "plainstem2", [b])
    ev["note_of_renamed_label"] = _shown(b)
    if ev["note_of_renamed_label"] != ("nx-note://plainstem2/n1", "plainstem2"):
        failures.append(f"the note of the renamed label is {ev['note_of_renamed_label']}, want nx-note://plainstem2/n1")
    _require(not failures, "; ".join(failures), ev)
    return ev


def check_p210(ctx: dict[str, Any]) -> dict[str, Any]:
    """6. P2.10: URI moves leave residue alone (rename, re-home, collection update and reconcile commit, onto a
    prefixed and onto a prefix-free label; the re-embed move cannot be driven on this one-model substrate and is
    named in the evidence), already-moved residue is repaired by registered data moves, and the backfill verb
    re-derives it. Part (2) runs first (its fixture needs the empty tenant), then (3) over its result, then (1),
    whose moves leave residue rows that would change (3)'s census."""
    _p210_preflight(ctx)
    ev: dict[str, Any] = {"parts": {}}
    failures: dict[str, str] = {}
    for key, part in (("2 repair moves", _p210_part_repair), ("3 backfill verb", _p210_part_backfill),
                      ("1 URI moves", _p210_part_moves)):
        try:
            ev["parts"][key] = part(ctx)
        except _CheckFailed as exc:
            ev["parts"][key] = {"status": "FAIL", **exc.evidence}
            failures[key] = str(exc)
    ev["examined"] = ctx["n"]
    if failures:
        raise _CheckFailed("P2.10 has not landed: " + "; ".join(f"part {k}: {v}" for k, v in failures.items()), ev)
    return ev


def test_rdr229_p2r_mvv_p210(t2_service_env: str) -> None:
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 -- only this test needs the catalog client

    _results_path().parent.mkdir(parents=True, exist_ok=True)
    mi.invalidate_collections_cache()
    with HttpCatalogClient() as cat:
        owner = _seed("owner", lambda: cat.register_owner("p2r-p210-owner", owner_type="curator", tumbler_prefix="97.1"))
        repo_owner = _seed("repo owner", lambda: cat.register_owner(
            "p2r-p210-repo", owner_type="repo", repo_hash="p2rp210", repo_root="/r", tumbler_prefix="97.2"))
        ctx = {"tenant": t2_service_env, "cat": cat, "owner": owner, "repo_owner": repo_owner, "n": 0}
        ok = _run_check(6, "P2.10: URI moves leave residue alone; repaired residue is re-derived; the backfill verb is relaxed",
                        lambda: check_p210(ctx))
    assert ok, "check 6 did not pass"
