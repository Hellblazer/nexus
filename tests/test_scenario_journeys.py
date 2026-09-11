# SPDX-License-Identifier: AGPL-3.0-or-later
"""P2 reduced scenario harness (test-suite-compression, nexus-test-cleanup
2026-08-05): a handful of cross-verb CLI journeys against the REAL
per-process engine substrate the whole unit suite already boots
(``tests/conftest.py::_pin_t2_substrate`` -> ``t2_service_env`` ->
``tests._engine_substrate.ensure_engine`` / ``mint_test_tenant``).

Design of record: T2 ``nexus/test-suite-compression-P2-harness-design``
(architect-planner) + its substantive-critic critique (T2
``nexus/test-suite-compression-P2-harness-design-critique``, verdict
BUILD-REDUCED). This file implements the Hal-ratified REDUCED scope: no
new ``Journey``/``Step`` harness class, no in-memory/fake substrate — each
scenario invokes real ``nx`` CLI commands in-process via
``click.testing.CliRunner`` against the session's real PG-backed engine,
and observes the result through a DIFFERENT verb or store handle than the
one that wrote it (CLI -> catalog reader, or CLI -> CLI through a
different command), per the design memo's cross-verb-by-construction rule.

Binding constraints (do not violate when adding a 5th journey):

* ``scenario`` runs in the DEFAULT pytest loop (not ``integration``-marked,
  not excluded from ``addopts``) — see the marker's registration in
  ``pyproject.toml``.
* Each scenario is ONE self-contained test function with function-scoped
  fixtures only. CI shards with ``pytest-split`` and the dev loop uses
  ``xdist``; both partition by individual test id, so an order-dependent
  test class or a module-scoped fixture that leaks state across scenarios
  would break under either. No shared mutable state between scenarios.
* A scenario touches more than one verb/tier — a single-verb test belongs
  in the ordinary unit suite, not here.
* If a future scenario touches ``nx doctor``, it may assert ONLY the
  portion of doctor's output derived from storage the scenario itself
  wrote (e.g. the collection census) — never overall health. ``doctor``
  is fundamentally an ambient-environment probe (real ``shutil.which``,
  subprocess handshakes, git-hooks-on-disk); asserting "doctor is
  healthy" here would be a non-hermetic, CI-flaky claim of coverage this
  file does not have. (No journey below touches doctor.)
* No credential/embedding patching: with no injected ``_client``,
  ``nexus.db.make_t3()`` returns the real service-backed
  ``HttpVectorClient`` unconditionally (RDR-155 P4a.2), and
  ``t2_service_env`` already points ``NX_SERVICE_URL`` /
  ``NX_SERVICE_TOKEN`` at the session engine with ``NX_LOCAL=1`` (local
  posture — server-side bge-768 embeddings, no Voyage key needed). Do not
  reintroduce ``local_t3`` / ``make_vector_test_client()`` /
  ``fake_credentials()`` patching here — that is the OLD fake-substrate
  pattern this harness deliberately does not use (see
  ``tests/test_indexer_e2e.py``, which stays untouched as the deeper,
  ``integration``-marked coverage).

Non-vacuity: a ``pytest_sessionfinish`` guard in ``tests/conftest.py``
fails the session loudly if any scenario selected in this run skips
(default skip budget: 0), so a silently-degraded engine substrate can
never read as green just because every ``scenario`` test skipped.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2.http_tuple_store import HttpTupleStore
from tests._catalog_fixture_ops import active_reader, documents_by_file_path, documents_by_title
from tests._engine_substrate import ensure_engine

#: Refusal-diagnostic events (nexus-c8hl7's WARN + its
#: write_manifest_many sibling) the failure-tail below prioritizes over a
#: blind byte-count tail — see ``_engine_log_on_failure``.
_ENGINE_REFUSAL_EVENTS = ("complete_index_run_refused", "write_manifest_many_complete_refused")

#: nexus-gtl01 (upsert-chunks ACK coverage): engine-side vector-write events
#: that already exist in engine.log today (``PgVectorRepository.java``'s
#: ``upsertChunksInternal`` — ``event=upsert_dedup_collapsed`` on in-batch
#: dedup, ``event=upsert_embed_skipped`` on the RDR-181 existence-partition
#: skip). Neither is new logging — this file cannot touch service/ (path
#: fence) — this only widens what the ALREADY-EMITTED lines get matched
#: against, so the healthy-shape residue (probe present=0, branch=
#: full_upsert_no_existing, no exception, chunk absent at verify) gets
#: whatever server-side upsert trace already exists for the failing tenant
#: folded into the failure report instead of silently tailed off by the
#: last-200-lines fallback.
_ENGINE_UPSERT_EVENTS = ("event=upsert_dedup_collapsed", "event=upsert_embed_skipped")

#: Extracts the ``missing_chash_sample=[...]`` payload CatalogRepository's
#: ``complete_index_run_refused`` WARN logs (Java ``List<String>.toString()``
#: — ``[a, b, c]`` or ``[]``) so the tail below can also grep the rest of
#: engine.log for any OTHER line mentioning one of those exact chashes.
_MISSING_CHASH_SAMPLE_RE = re.compile(r"missing_chash_sample=\[([^\]]*)\]")

#: Fallback tail length (lines) when no refusal event is present in the log
#: — still bounded, never the whole session's engine.log.
_ENGINE_LOG_TAIL_LINES = 200

#: nexus-gtl01 (critique round 3, item S2/A3): env var NAMES matching any of
#: these substrings (case-insensitive) render as ``name=<redacted>`` in
#: ``_invocation_env_snapshot`` — never the value. This repo's operative
#: invocation env is predominantly ``NX_*`` (widened below from the
#: original ``NEXUS_*``-only filter), and ``NX_SERVICE_TOKEN`` is live in
#: these journeys' env (``t2_service_env`` sets it) — it must never reach
#: pytest failure output or CI logs.
_REDACT_ENV_NAME_RE = re.compile(r"TOKEN|KEY|SECRET|PASSWORD", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _engine_log_on_failure(request: pytest.FixtureRequest):
    """On a scenario-journey FAILURE, fold a bounded tail of the substrate's
    engine.log into the test report (critic Q1 Critical, 2026-08-08): the
    engine-side nexus-c8hl7 refusal WARN
    (``event=complete_index_run_refused`` / its ``write_manifest_many``
    sibling) lands in ``<pgdata>/engine.log`` inside the session's
    mkdtemp'd PG cluster, which the substrate's teardown ``rmtree``s at
    session end -- so on a green run nothing is lost, but on a red run the
    ONLY surviving evidence today is the pytest failure text itself. This
    grabs the refusal lines specifically when present (falls back to a
    bounded byte tail otherwise) and attaches them via
    ``add_report_section`` so they render in the failure output without
    needing the log file to survive teardown.

    Scoped to this file only (module-local autouse, not suite-wide) — the
    scenario journeys are the only tests this investigation's evidence gap
    concerns; nothing else in the suite loses evidence this way.
    """
    yield
    rep = getattr(request.node, "rep_call", None)
    if rep is None or not rep.failed:
        return
    # nexus-gtl01: capture the invocation environment unconditionally on
    # failure — the 2026-08-08 recurrence noted the reds clustered in
    # orchestrator-invoked full runs (3/3) and the greens in
    # debugger-invoked full runs (3/3), an unexplained delta this makes
    # checkable instead of folklore. One bounded line: every NEXUS_*/NX_*
    # env var (plus CLAUDE_CODE_SESSION_ID) — the invocation-style delta
    # this snapshot chases lives predominantly in NX_* space (NX_AGENT,
    # NX_SESSION_ID, NX_SERVICE_HOST/PORT), not the narrower NEXUS_* prefix
    # — with TOKEN/KEY/SECRET/PASSWORD-named values redacted, the xdist
    # worker id, and the host load average at failure time.
    request.node.add_report_section(
        "teardown", "invocation environment", _invocation_env_snapshot(),
    )
    try:
        state = ensure_engine()
    except Exception as exc:  # noqa: BLE001 — best-effort diagnostic; must never mask the real failure
        request.node.add_report_section(
            "teardown", "engine.log (unavailable)",
            f"could not reach the engine substrate to tail engine.log: {exc}",
        )
        return
    log_path = os.path.join(state["pgdata"], "engine.log")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        request.node.add_report_section(
            "teardown", "engine.log (unavailable)", f"could not read {log_path}: {exc}",
        )
        return
    refusal_lines, upsert_lines = _select_failure_evidence_lines(lines)
    if refusal_lines or upsert_lines:
        content = "".join(refusal_lines + upsert_lines)
        label = f"engine.log refusal + upsert-event lines ({log_path})"
    else:
        content = "".join(lines[-_ENGINE_LOG_TAIL_LINES:])
        label = f"engine.log tail, last {_ENGINE_LOG_TAIL_LINES} lines ({log_path})"
    request.node.add_report_section("teardown", label, content)


def _select_failure_evidence_lines(lines: list[str]) -> tuple[list[str], list[str]]:
    """Split a full ``engine.log`` into ``(refusal_lines, upsert_lines)`` —
    the widened selection nexus-gtl01 adds on top of the bare refusal WARN:
    (a) any line naming one of the refusal's own ``missing_chash_sample``
    values (an engine-side event that happens to mention that exact chash,
    e.g. an exception around it), and (b) any already-emitted engine-side
    upsert/vector-write event line (``_ENGINE_UPSERT_EVENTS`` — existing
    ``PgVectorRepository`` logging, not new; this file cannot touch
    service/), so the write-side trace for the failing tenant/collection
    rides along with the refusal instead of being at the mercy of the
    last-200-lines fallback.

    ``upsert_lines`` is capped to the LAST ``_ENGINE_LOG_TAIL_LINES``
    matches (critique round 3, item A2): engine.log is session-scoped, so a
    full-run log can carry hundreds-to-thousands of
    ``upsert_dedup_collapsed``/``upsert_embed_skipped`` lines from UNRELATED
    tests by the time one journey fails — an uncapped selector would fold
    all of them into the failure report. Capping to the most recent matches
    keeps the report bounded and biases toward the lines temporally closest
    to the failure. ``refusal_lines`` is left uncapped: it is refusal-event-
    gated (only this test file's own journeys emit it) rather than a
    suite-wide event, so it stays small in practice.

    Pure and pytest-node-free so it's unit-testable directly — see
    ``tests/test_gtl01_journey_failure_evidence.py``.
    """
    refusal_lines = [ln for ln in lines if any(ev in ln for ev in _ENGINE_REFUSAL_EVENTS)]
    missing_chashes = _missing_chashes_from(refusal_lines)
    upsert_lines = [
        ln for ln in lines
        if ln not in refusal_lines
        and (
            any(ev in ln for ev in _ENGINE_UPSERT_EVENTS)
            or any(chash and chash in ln for chash in missing_chashes)
        )
    ]
    return refusal_lines, upsert_lines[-_ENGINE_LOG_TAIL_LINES:]


def _missing_chashes_from(refusal_lines: list[str]) -> list[str]:
    """Pull the ``missing_chash_sample=[...]`` values out of the refusal
    WARN lines (see ``_MISSING_CHASH_SAMPLE_RE``). Returns an empty list
    when no refusal line matched or the sample was empty (``missing=0``
    refusals — the zero-content shape — legitimately carry ``[]``)."""
    out: list[str] = []
    for ln in refusal_lines:
        m = _MISSING_CHASH_SAMPLE_RE.search(ln)
        if not m:
            continue
        raw = m.group(1).strip()
        if not raw:
            continue
        out.extend(part.strip() for part in raw.split(",") if part.strip())
    return out


def _invocation_env_snapshot() -> str:
    """One bounded diagnostic line: every ``NEXUS_*``/``NX_*`` env var plus
    ``CLAUDE_CODE_SESSION_ID`` (name=value, sorted; names matching
    ``_REDACT_ENV_NAME_RE`` render ``name=<redacted>`` — never the value),
    the xdist worker id (``PYTEST_XDIST_WORKER``, or ``master`` when not
    running under xdist), and the host load average — captured at the
    moment of a scenario-journey failure so an environmental delta between
    invocation styles is checkable instead of anecdotal.

    Names are always listed (even redacted ones) so a redaction itself is
    visible in the report; only the value is withheld.
    """
    def _rendered(name: str, value: str) -> str:
        if _REDACT_ENV_NAME_RE.search(name):
            return f"{name}=<redacted>"
        return f"{name}={value}"

    relevant = {
        k: v for k, v in os.environ.items()
        if k.startswith(("NEXUS_", "NX_")) or k == "CLAUDE_CODE_SESSION_ID"
    }
    env_vars = ",".join(_rendered(k, v) for k, v in sorted(relevant.items()))
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    try:
        load1, load5, load15 = os.getloadavg()
        load = f"{load1:.2f},{load5:.2f},{load15:.2f}"
    except OSError:  # pragma: no cover — getloadavg is POSIX-only; belt-and-suspenders
        load = "unavailable"
    return (
        f"xdist_worker={worker} load_avg(1,5,15)={load} invocation_env=[{env_vars}]"
    )


def _git_init(repo: Path) -> None:
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@nexus"],
        ["git", "config", "user.name", "Nexus Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


@pytest.mark.scenario
def test_store_put_is_searchable_and_catalogued(t2_service_env) -> None:
    """Journey 1: ``store put`` -> ``search`` -> catalog, three different
    surfaces observing the same write.

    Proves the T3 write is real (server-side embedding + pgvector ranking
    return the note for a semantically related query, not an exact string
    match) and that the CLI's catalog pre-registration hook
    (``nexus.catalog.store_hook``) actually lands a Document keyed on
    (collection, title) — the identity contract ``store.py`` documents for
    ``store put`` (nexus-sdp0u).
    """
    runner = CliRunner()
    title = "scenario-journey-1-note"
    put = runner.invoke(main, [
        "store", "put", "-", "--title", title, "-c", "fixture-subject",
    ], input="HNSW indexes approximate nearest neighbor search using a "
             "layered proximity graph.\n")
    assert put.exit_code == 0, put.output
    assert title not in put.output  # sanity: output is the doc id + collection, not an echo
    assert "Stored:" in put.output

    search = runner.invoke(main, [
        "search", "layered proximity graph nearest neighbor",
        "--corpus", "knowledge", "--json",
    ])
    assert search.exit_code == 0, search.output
    hits = json.loads(search.stdout)
    assert any(h.get("title") == title for h in hits), (
        f"expected {title!r} among search hits: {[h.get('title') for h in hits]}"
    )

    docs = documents_by_title(title)
    assert len(docs) == 1, f"expected exactly one catalog document for {title!r}, got {docs}"
    assert docs[0].chunk_count > 0, "catalog row must reflect the written chunk"


@pytest.mark.scenario
def test_index_md_creates_doc_level_content_with_catalog_registration(t2_service_env, tmp_path: Path) -> None:
    """Journey 2: ``index md`` -> ``search`` -> catalog manifest join.

    ``tests/test_indexer_e2e.py`` mocks the indexer in virtually every
    ``test_index_cmd.py`` test (cluster-A audit, T1 scratch ``4af53f27``),
    so no in-process default-suite test proves that ``nx index md``
    actually makes a document searchable end to end. This does, and it
    additionally pins the RDR-108 catalog/T3 split: that
    ``documents.tumbler -> document_chunks.doc_id -> document_chunks.chash``
    resolves to the SAME chash the search path just returned from live T3
    rows — non-vacuous because it would fail the moment the manifest write
    and the T3 write disagree (e.g. a doc_id typo, a dropped hook, a stale
    manifest).
    """
    md = tmp_path / "scenario-journey-2.md"
    md.write_text(
        "# Journey Two Doc\n\n"
        "Consistent hashing distributes keys across a ring of nodes to "
        "minimize rebalancing when nodes join or leave.\n"
    )

    runner = CliRunner()
    # nexus critic (2026-08-08, Q1): DEBUG so the gtl01 write-side decision
    # trail (upsert_skip_reembed_probe/branch/disposition) rides into
    # idx.output and therefore into the pytest failure text on any
    # recurrence of the completion-refusal class this journey chases —
    # the CLI's default structlog threshold is WARNING and nothing else
    # in this invoke sets NEXUS_LOG_LEVEL.
    idx = runner.invoke(main, ["index", "md", str(md), "--corpus", "scenario2"],
                         env={"NEXUS_LOG_LEVEL": "DEBUG"})
    assert idx.exit_code == 0, idx.output
    assert "Indexed 1 chunk" in idx.output

    search = runner.invoke(main, [
        "search", "consistent hashing ring nodes rebalancing",
        "--corpus", "docs__scenario2", "--json",
    ])
    assert search.exit_code == 0, search.output
    hits = json.loads(search.stdout)
    assert hits, "expected at least one search hit for the freshly indexed doc"
    returned_chashes = {h["chash"] for h in hits}

    catalog_docs = documents_by_file_path(str(md.resolve()))
    assert len(catalog_docs) == 1, (
        f"expected exactly one catalog document for {md}, got {catalog_docs}"
    )
    doc = catalog_docs[0]
    assert doc.chunk_count > 0

    manifest = active_reader().get_manifest(str(doc.tumbler))
    assert manifest, f"expected non-empty manifest for tumbler {doc.tumbler}"
    manifest_chashes = {row.chash for row in manifest}

    # The join, pinned: every chash search returned for THIS doc must be
    # one the catalog manifest actually lists for THIS tumbler.
    assert returned_chashes <= manifest_chashes, (
        f"search returned chash(es) {returned_chashes - manifest_chashes} not "
        f"present in the catalog manifest for {doc.tumbler} — RDR-108 "
        f"catalog/T3 join is broken"
    )


@pytest.mark.scenario
def test_index_repo_routes_code_to_code_corpus(t2_service_env, tmp_path: Path) -> None:
    """Journey 3: ``index repo`` -> code-corpus search, with a negative
    routing check.

    Cluster-A's audit found ``test_index_cmd.py`` mocks
    ``index_repository`` in essentially every test, so nothing in the
    default suite proves a real repo index makes code searchable, or that
    it lands specifically in ``code__`` and nowhere else. This chains
    real ``git init`` -> ``nx index repo`` -> two searches (code corpus
    hits, knowledge corpus does not) through the session engine.
    """
    repo = tmp_path / "scenario-journey-3-repo"
    repo.mkdir()
    (repo / "ring_buffer.py").write_text(
        "def next_power_of_two(n: int) -> int:\n"
        '    """Round n up to the next power of two for ring-buffer sizing."""\n'
        "    p = 1\n"
        "    while p < n:\n"
        "        p *= 2\n"
        "    return p\n"
    )
    _git_init(repo)

    runner = CliRunner()

    # Seed an unrelated knowledge-corpus document FIRST so the negative
    # check below exercises a real "collection exists, zero hits" search
    # rather than a "no matching collections" resolver failure — each test
    # gets a brand-new tenant (t2_service_env), so `knowledge` would
    # otherwise never exist for this test at all, and the negative check
    # would pass for the wrong reason (nothing to search) instead of
    # proving routing.
    seed = runner.invoke(main, [
        "store", "put", "-", "--title", "scenario3-unrelated-seed", "-c", "fixture-subject",
    ], input="Sourdough starters need daily feeding to stay active.\n")
    assert seed.exit_code == 0, seed.output

    idx = runner.invoke(main, ["index", "repo", str(repo), "--no-taxonomy"])
    assert idx.exit_code == 0, idx.output
    assert "Done." in idx.output

    code_search = runner.invoke(main, [
        "search", "round up to next power of two ring buffer sizing",
        "--corpus", "code", "--json",
    ])
    assert code_search.exit_code == 0, code_search.output
    code_hits = json.loads(code_search.stdout)
    assert any("ring_buffer.py" in h.get("title", "") for h in code_hits), (
        f"expected ring_buffer.py among code-corpus hits: "
        f"{[h.get('title') for h in code_hits]}"
    )
    assert all(h["collection"].startswith("code__") for h in code_hits)

    # Negative check: the same query against knowledge must not surface
    # this repo's code — proving routing, not merely presence.
    knowledge_search = runner.invoke(main, [
        "search", "round up to next power of two ring buffer sizing",
        "--corpus", "knowledge", "--json",
    ])
    assert knowledge_search.exit_code == 0, knowledge_search.output
    # nexus-von7f: `nx search --json` now emits the same top-level shape
    # (a JSON array) on the zero-hits path as on the populated path — an
    # empty results set is `[]`, not the human-readable "No results."
    # string. Parse unconditionally rather than special-casing the old
    # broken contract.
    knowledge_hits = json.loads(knowledge_search.stdout)
    assert not any("ring_buffer.py" in h.get("title", "") for h in knowledge_hits), (
        "code content leaked into the knowledge corpus"
    )


@pytest.mark.scenario
def test_cross_corpus_search_routes_correctly(t2_service_env, tmp_path: Path) -> None:
    """Journey 4: cross-corpus search routing — the CSV/multi-``--corpus``
    expansion and merge path that journeys 1-3 never exercise (each of
    them targets exactly one corpus).

    Seeds two DIFFERENT corpora (``knowledge`` via ``store put``,
    ``docs__scenario4`` via ``index md``) with topically distinct content,
    then issues ONE combined ``--corpus knowledge,docs__scenario4`` search
    per query and asserts each query surfaces the document from its own
    corpus — proving ``search_cmd.py``'s comma-expansion + ``resolve_corpus``
    + cross-collection merge actually routes results to the right corpus
    rather than merely unioning everything.
    """
    runner = CliRunner()

    put = runner.invoke(main, [
        "store", "put", "-", "--title", "scenario4-wombat-note", "-c", "fixture-subject",
    ], input="Wombats dig extensive burrow systems across the southern "
             "hemisphere grasslands.\n")
    assert put.exit_code == 0, put.output

    md = tmp_path / "scenario-journey-4.md"
    md.write_text(
        "# Scenario4 Octopus Notes\n\n"
        "Octopuses use chromatophores to rapidly change skin color for "
        "camouflage against coral reef backgrounds.\n"
    )
    # nexus critic (2026-08-08, Q1): DEBUG so the gtl01 write-side trail
    # survives into idx.output on recurrence — see journey 2's comment.
    idx = runner.invoke(main, ["index", "md", str(md), "--corpus", "scenario4"],
                         env={"NEXUS_LOG_LEVEL": "DEBUG"})
    assert idx.exit_code == 0, idx.output

    combined_corpus = "knowledge,docs__scenario4"

    wombat_search = runner.invoke(main, [
        "search", "wombat burrow grassland digging",
        "--corpus", combined_corpus, "--json",
    ])
    assert wombat_search.exit_code == 0, wombat_search.output
    wombat_hits = json.loads(wombat_search.stdout)
    assert any(
        h.get("title") == "scenario4-wombat-note" and h["collection"].startswith("knowledge__")
        for h in wombat_hits
    ), f"expected the knowledge-corpus wombat note among combined-corpus hits: {wombat_hits}"

    octopus_search = runner.invoke(main, [
        "search", "octopus chromatophore camouflage coral reef",
        "--corpus", combined_corpus, "--json",
    ])
    assert octopus_search.exit_code == 0, octopus_search.output
    octopus_hits = json.loads(octopus_search.stdout)
    assert any(
        h["collection"].startswith("docs__scenario4__") for h in octopus_hits
    ), f"expected the docs__scenario4 octopus doc among combined-corpus hits: {octopus_hits}"

    # The two queries, run against the SAME combined corpus spec, must have
    # actually reached both distinct collections between them — proving the
    # comma-expansion resolved to >=2 real target collections, not one.
    all_collections_seen = {h["collection"] for h in wombat_hits + octopus_hits}
    knowledge_seen = any(c.startswith("knowledge__") for c in all_collections_seen)
    docs_seen = any(c.startswith("docs__scenario4__") for c in all_collections_seen)
    assert knowledge_seen and docs_seen, (
        f"combined --corpus {combined_corpus!r} did not route to both corpora: "
        f"{all_collections_seen}"
    )


# ── RDR-205 Phase 5: the mailbox consumer (bead nexus-em75s.25) ─────────────
#
# Eight journeys against the ``mailbox/<address>`` template (``service/src/
# main/resources/tuples/templates/mailbox.yaml``): keys ``[to]``, dims
# ``{from (required), kind, correlation_id, address_kind}``, ``id_from=
# keys+nonce``, ``id_dims=[from]`` -- the tuple id is a hash of (tenant,
# subspace, to, from, nonce), never of body or the other dims (``Tuple
# Repository.computeId``). Every journey below drives ``nx tuple`` through
# the CLI (cross-verb by construction: out/rd/in/ack/nack/stats), matching
# this file's own binding constraint.


def _tuple_uniq(label: str) -> str:
    return f"{label}-{uuid.uuid4().hex[:10]}"


def _tuple_last_json_line(output: str):
    """Parse the LAST non-empty line of *output* as JSON -- a structlog
    line can land on stdout ahead of the command's own JSON line."""
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


@pytest.mark.scenario
def test_mailbox_mid_turn_directive_is_drained_before_hand_back(t2_service_env) -> None:
    """Journey 5: the mailbox skill's convention -- send by ``tuple out`` to
    the agent's address, drain by ``tuple in`` BEFORE composing any
    hand-back.

    A directive lands mid-turn; the drain step finds and acks it before any
    hand-back would be composed; afterwards the mailbox is empty by both
    reads a caller might use -- ``rd`` never returns a consumed row and a
    fresh ``in`` probe misses.
    """
    runner = CliRunner()
    address = _tuple_uniq("agent")
    nonce = _tuple_uniq("nonce")

    out = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=orchestrator",
        "--dim", "kind=directive", "--body", "check X before finishing",
        "--nonce", nonce,
    ])
    assert out.exit_code == 0, out.output
    directive_id = out.output.strip().splitlines()[-1]

    # The drain, run BEFORE the agent composes its hand-back.
    claim = runner.invoke(main, [
        "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
        "--claimant", address, "--lease-s", "30", "--json",
    ])
    assert claim.exit_code == 0, claim.output
    payload = _tuple_last_json_line(claim.output)
    assert payload["tuple"]["id"] == directive_id
    assert payload["tuple"]["body"] == "check X before finishing"
    assert payload["tuple"]["dims"] == {"from": "orchestrator", "kind": "directive"}

    ack = runner.invoke(main, ["tuple", "ack", payload["claim_id"], "--claimant", address])
    assert ack.exit_code == 0, ack.output

    # Hand-back would be composed only now. The mailbox reads empty either
    # way a caller looks.
    rd = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
    assert rd.exit_code == 0, rd.output
    assert _tuple_last_json_line(rd.output) == [], "an acked row must never come back from rd"

    probe = runner.invoke(main, [
        "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
        "--claimant", address, "--lease-s", "5",
    ])
    assert probe.exit_code == 1, probe.output


@pytest.mark.scenario
def test_mailbox_resent_message_lands_once_with_expires_at_unchanged(t2_service_env) -> None:
    """Journey 6: the same sender resending the same nonce to one address
    is ONE row -- the id is a hash of (to, from, nonce), so a resend is an
    idempotent upsert that touches ``expires_at`` only (never body, per
    ``TupleRepository.out``'s DO UPDATE clause), and under the default TTL
    (== the template's retention) ``expires_at`` is already clamped to
    ``created_at + retention`` and cannot move.
    """
    runner = CliRunner()
    address = _tuple_uniq("agent")
    nonce = _tuple_uniq("nonce")

    first = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "v1", "--nonce", nonce,
    ])
    assert first.exit_code == 0, first.output
    tuple_id = first.output.strip().splitlines()[-1]

    rd1 = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
    row1 = _tuple_last_json_line(rd1.output)[0]
    assert row1["id"] == tuple_id

    resend = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "v2-should-not-land", "--nonce", nonce,
    ])
    assert resend.exit_code == 0, resend.output
    assert resend.output.strip().splitlines()[-1] == tuple_id, (
        "a resend with the same nonce must land on the SAME tuple id"
    )

    rd2 = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
    rows2 = _tuple_last_json_line(rd2.output)
    assert len(rows2) == 1, f"a resend must be one row, not a second: {rows2}"
    assert rows2[0]["id"] == tuple_id
    assert rows2[0]["body"] == "v1", "a resend's DO UPDATE never touches body"
    assert rows2[0]["expires_at"] == row1["expires_at"], "expires_at must not move on a resend"


@pytest.mark.scenario
def test_mailbox_two_different_nonces_to_one_address_are_two_rows(t2_service_env) -> None:
    """Journey 7 (RDR-205 Test Plan): two mailbox messages to one address
    with different sender-minted nonces are two independent rows -- the
    nonce is part of the tuple's identity."""
    runner = CliRunner()
    address = _tuple_uniq("agent")

    first = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "first", "--nonce", _tuple_uniq("nonce"),
    ])
    assert first.exit_code == 0, first.output
    second = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "second", "--nonce", _tuple_uniq("nonce"),
    ])
    assert second.exit_code == 0, second.output
    id1, id2 = first.output.strip().splitlines()[-1], second.output.strip().splitlines()[-1]
    assert id1 != id2, "two different nonces must never collide onto one tuple id"

    rd = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}",
                              "-n", "10", "--json"])
    assert rd.exit_code == 0, rd.output
    rows = _tuple_last_json_line(rd.output)
    assert {r["id"] for r in rows} == {id1, id2}, f"expected exactly two rows: {rows}"


@pytest.mark.scenario
def test_mailbox_two_senders_minting_the_same_nonce_are_two_rows(t2_service_env) -> None:
    """Journey 8 (RDR-205 Test Plan): two SENDERS minting the same nonce to
    one address are two rows -- ``from`` is an ``id_dims`` field, so the
    sender is part of the tuple's identity and a nonce need only be unique
    among ONE sender's own messages."""
    runner = CliRunner()
    address = _tuple_uniq("agent")
    shared_nonce = _tuple_uniq("nonce")

    from_a = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "from a", "--nonce", shared_nonce,
    ])
    assert from_a.exit_code == 0, from_a.output
    from_b = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-b",
        "--body", "from b", "--nonce", shared_nonce,
    ])
    assert from_b.exit_code == 0, from_b.output
    id_a, id_b = from_a.output.strip().splitlines()[-1], from_b.output.strip().splitlines()[-1]
    assert id_a != id_b, "the same nonce from two different senders must never collide"

    rd = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}",
                              "-n", "10", "--json"])
    assert rd.exit_code == 0, rd.output
    rows = _tuple_last_json_line(rd.output)
    assert {r["id"] for r in rows} == {id_a, id_b}, f"expected exactly two rows: {rows}"


@pytest.mark.scenario
def test_mailbox_out_with_no_from_is_a_schema_violation(t2_service_env) -> None:
    """Journey 9 (RDR-205 Test Plan): a mailbox ``out`` naming no ``from``
    is a SchemaViolation -- ``from`` is the template's one required
    dimension, and it is what keeps two senders' nonces from colliding."""
    runner = CliRunner()
    address = _tuple_uniq("agent")

    out = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--body", "no sender named",
        "--nonce", _tuple_uniq("nonce"),
    ])
    assert out.exit_code == 1, out.output
    assert "SchemaViolation" in out.output, out.output

    rd = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
    assert rd.exit_code == 0, rd.output
    assert _tuple_last_json_line(rd.output) == [], "a refused out() must write no row"


@pytest.mark.scenario
def test_mailbox_resend_every_day_for_a_week_never_extends_expires_at(t2_service_env) -> None:
    """Journey 10 (RDR-205 Test Plan): a message resent every day for a
    week never pushes ``expires_at`` past ``created_at`` plus the
    template's 7-day retention.

    No engine test-only clock seam exists for tuples (``TupleRepository``
    stamps every column from the JVM's own ``OffsetDateTime.now()``), so
    this asserts the invariant directly rather than fast-forwarding a real
    week: under the default TTL (== retention), each resend's candidate
    expiry is ``now() + retention``, always AFTER the original row's
    ``created_at + retention`` ceiling (``now() > created_at`` for every
    resend after the first), so ``LEAST(candidate, ceiling)`` picks the
    fixed ceiling every time -- seven resends back-to-back exercises the
    same clamp seven real weeks apart would. The purge-with-claim-history
    leg of this Test Plan bullet is engine-sweep/admin-SQL territory with
    no client-reachable trigger (Day 2 Operations table: "no client
    operation reads the log in v1") and is out of scope here; Phase 3's
    sweep beads (nexus-em75s.37/.38) own that half in Java.
    """
    runner = CliRunner()
    address = _tuple_uniq("agent")
    nonce = _tuple_uniq("nonce")

    first = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "day 0", "--nonce", nonce,
    ])
    assert first.exit_code == 0, first.output
    tuple_id = first.output.strip().splitlines()[-1]
    rd0 = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
    row0 = _tuple_last_json_line(rd0.output)[0]
    ceiling_expires_at = row0["expires_at"]
    # The ceiling is derived INDEPENDENTLY of what the engine wrote: the
    # template's retention_seconds (mailbox.yaml, 604800) added to the
    # row's own created_at. Asserting only self-consistency across resends
    # would pass a regression in the ceiling calculation itself.
    from datetime import datetime, timedelta
    created_at = datetime.fromisoformat(row0["created_at"])
    expected_ceiling = created_at + timedelta(seconds=604800)
    assert datetime.fromisoformat(ceiling_expires_at) == expected_ceiling, (
        f"expires_at {ceiling_expires_at!r} is not created_at + 7 days ({expected_ceiling.isoformat()!r})"
    )

    for day in range(1, 8):
        resend = runner.invoke(main, [
            "tuple", "out", f"mailbox/{address}",
            "--key", f"to={address}", "--dim", "from=sender-a",
            "--body", f"day {day}", "--nonce", nonce,
        ])
        assert resend.exit_code == 0, resend.output
        assert resend.output.strip().splitlines()[-1] == tuple_id

        rd = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
        rows = _tuple_last_json_line(rd.output)
        assert len(rows) == 1, f"day {day}: still one row, not {len(rows)}"
        assert rows[0]["expires_at"] == ceiling_expires_at, (
            f"day {day}: expires_at moved past created_at + retention: "
            f"{rows[0]['expires_at']!r} != {ceiling_expires_at!r}"
        )


@pytest.mark.scenario
def test_mailbox_nack_by_max_attempts_different_claimants_dead_letters(t2_service_env) -> None:
    """Journey 11 (RDR-205 Test Plan): ``nack`` by ``max_attempts`` (3)
    DIFFERENT claimants dead-letters the row either way -- parked out of
    every future claimant's view, still readable by ``rd`` with
    ``claim_state=dead``, and counted under ``dead`` by ``tuple stats``.
    """
    runner = CliRunner()
    address = _tuple_uniq("agent")

    out = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "keeps getting nacked", "--nonce", _tuple_uniq("nonce"),
    ])
    assert out.exit_code == 0, out.output

    for i in range(3):
        claimant = _tuple_uniq(f"claimant{i}")
        claim = runner.invoke(main, [
            "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
            "--claimant", claimant, "--lease-s", "30", "--json",
        ])
        assert claim.exit_code == 0, f"claim #{i}: {claim.output}"
        claim_id = _tuple_last_json_line(claim.output)["claim_id"]

        nack = runner.invoke(main, ["tuple", "nack", claim_id, "--claimant", claimant])
        assert nack.exit_code == 0, f"nack #{i}: {nack.output}"

    # A fourth claimant finds nothing: the row is dead, not available.
    probe = runner.invoke(main, [
        "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
        "--claimant", _tuple_uniq("claimant-late"), "--lease-s", "30",
    ])
    assert probe.exit_code == 1, probe.output

    rd = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
    rows = _tuple_last_json_line(rd.output)
    assert len(rows) == 1
    assert rows[0]["claim_state"] == "dead"
    assert rows[0]["claimant"] is None

    stats = runner.invoke(main, ["tuple", "stats", f"mailbox/{address}", "--json"])
    assert stats.exit_code == 0, stats.output
    census = _tuple_last_json_line(stats.output)
    assert census["dead"] == 1, census
    assert census["available"] == 0, census
    assert census["claimed"] == 0, census


@pytest.mark.scenario
def test_mailbox_max_attempts_lapsed_leases_dead_letters(t2_service_env) -> None:
    """Journey 12 (RDR-205 Test Plan): ``max_attempts`` (3) LAPSED leases
    -- nobody ever nacks -- dead-letter the row exactly as explicit nacks
    do: a claim that finds a lapsed prior lease releases it (an ``expire``
    log row) and re-claims with ``attempts`` incremented; the claim that
    brings ``attempts`` to ``max_attempts`` dead-letters the row IN THAT
    SAME transaction and its own select re-runs, returning the probe
    result (RDR-205 Test Plan: "the claim re-runs its select and returns
    the next candidate or the probe result"). ``lease_s`` must be a
    positive integer, so this sleeps past the 1s minimum lease three times
    rather than controlling a clock -- no seam exists for this engine path
    (see journey 10's docstring).
    
    Note: this journey sleeps past three 1-second leases (about 4.5 s
    wall clock), the outlier against the file's ~1 s per-journey budget;
    tuples have no engine-side clock seam, the same convention as
    ``TupleRepositoryTest``'s ``Thread.sleep(1_500)``.
    """
    runner = CliRunner()
    address = _tuple_uniq("agent")

    out = runner.invoke(main, [
        "tuple", "out", f"mailbox/{address}",
        "--key", f"to={address}", "--dim", "from=sender-a",
        "--body", "keeps lapsing", "--nonce", _tuple_uniq("nonce"),
    ])
    assert out.exit_code == 0, out.output

    # Three claims, each left to lapse rather than acked or nacked.
    for i in range(3):
        claim = runner.invoke(main, [
            "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
            "--claimant", _tuple_uniq(f"claimant{i}"), "--lease-s", "1",
        ])
        assert claim.exit_code == 0, f"claim #{i}: {claim.output}"
        time.sleep(1.5)  # let the 1-second lease lapse (house convention: TupleRepositoryTest.java)

    # The fourth claim attempt is the one that dead-letters: the first of
    # the three loop claims takes the fresh row at attempts=0 (no prior
    # lapsed lease to notice); the second and third each find the PREVIOUS
    # lease lapsed and raise attempts by one on their way to re-claiming
    # (0->1, then 1->2); this fourth call finds the third claim's lease
    # lapsed too, raises attempts to 3 = max_attempts, dead-letters in that
    # same transaction, and its own re-run select finds no other candidate.
    dead_letter_claim = runner.invoke(main, [
        "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
        "--claimant", _tuple_uniq("claimant-final"), "--lease-s", "1",
    ])
    assert dead_letter_claim.exit_code == 1, dead_letter_claim.output

    rd = runner.invoke(main, ["tuple", "rd", f"mailbox/{address}", "--pattern", f"to={address}", "--json"])
    rows = _tuple_last_json_line(rd.output)
    assert len(rows) == 1
    assert rows[0]["claim_state"] == "dead"
    assert rows[0]["claimant"] is None

    stats = runner.invoke(main, ["tuple", "stats", f"mailbox/{address}", "--json"])
    assert stats.exit_code == 0, stats.output
    census = _tuple_last_json_line(stats.output)
    assert census["dead"] == 1, census

    # No further in returns it.
    probe = runner.invoke(main, [
        "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
        "--claimant", _tuple_uniq("claimant-later"), "--lease-s", "5",
    ])
    assert probe.exit_code == 1, probe.output


# ── RDR-205 Phase 6: cross-instance request and ack (bead nexus-em75s.29) ───
#
# Two sessions on one box, both minting against the ONE tenant t2_service_env
# provisions -- the RDR's own "not a third consumer" clause: address_kind
# `instance` reuses the mailbox/<address> template, addressed to a session
# name instead of an agent id. Journey per docs/tuple-space-walkthroughs.md
# § Cross-instance request and ack: A outs a request to B's mailbox and
# parks an `in` on its OWN mailbox for the ack; B drains, acks by outing
# back to A's address; A's parked `in` returns.


def _parked_in_loop(
    runner: CliRunner, address: str, *, claimant: str, lease_s: int,
    per_call_timeout_s: int, overall_budget_s: float,
) -> tuple[dict | None, int]:
    """Mirror ``scripts/spikes/rdr-205-mvv/run1.py``'s
    ``parked_rd_for_report``: loop ``tuple in --timeout-s <n>`` calls
    within an overall budget rather than depending on one park call
    outlasting it -- the orchestration skill's "a wait of minutes is a
    LOOP of parked calls" contract, restated here for `in` instead of
    `rd`. ``per_call_timeout_s`` stands in for the engine's real 25 s
    per-call cap (CA 3) at a scale a unit test can afford; the shape of
    the loop -- not the literal 25 -- is what this journey proves.
    Returns ``(claim_payload_or_None, calls_made)``.
    """
    deadline = time.monotonic() + overall_budget_s
    calls = 0
    while time.monotonic() < deadline:
        calls += 1
        result = runner.invoke(main, [
            "tuple", "in", f"mailbox/{address}", "--pattern", f"to={address}",
            "--claimant", claimant, "--lease-s", str(lease_s),
            "--timeout-s", str(per_call_timeout_s), "--json",
        ])
        if result.exit_code == 0:
            return _tuple_last_json_line(result.output), calls
    return None, calls


@pytest.mark.scenario
def test_cross_instance_request_and_ack_two_sessions_one_box(t2_service_env) -> None:
    """Journey (RDR-205 Test Plan / Phase 6): A (session ``nexus-a6``-shaped)
    sends a ``kind=request`` to B's (``conexus-58``-shaped) mailbox and
    parks an `in` on its OWN mailbox for the ack, looping past a per-call
    cap rather than depending on a single park to outlast B's work; B
    drains the request, does its own bookkeeping, and acks by ``out``ing a
    ``kind=ack`` tuple carrying the same ``correlation_id`` back to A's
    mailbox; A's parked loop returns it.

    A drives entirely through the ``nx tuple`` CLI (this file's own
    cross-verb binding constraint); B runs off a background thread and
    drives ``HttpTupleStore`` directly -- ``click.testing.CliRunner``
    patches process-wide stdout around each invocation and is not safe
    for two threads to call concurrently, and B's actions must genuinely
    overlap A's parked wait for this journey to prove the loop rather
    than a lucky single park.
    """
    runner = CliRunner()
    a = _tuple_uniq("nexus-a")
    b = _tuple_uniq("conexus-b")
    correlation_id = _tuple_uniq("corr")

    # A -> B: the request, address_kind instance (RDR-205 Phase 6 addressing).
    out = runner.invoke(main, [
        "tuple", "out", f"mailbox/{b}",
        "--key", f"to={b}", "--dim", f"from={a}",
        "--dim", "kind=request", "--dim", f"correlation_id={correlation_id}",
        "--dim", "address_kind=instance", "--body", "deploy and re-gate",
        "--nonce", correlation_id,
    ])
    assert out.exit_code == 0, out.output
    request_id = out.output.strip().splitlines()[-1]

    # Before B has acted at all, a bare probe on A's own mailbox finds
    # nothing -- no ack exists yet.
    early_probe = runner.invoke(main, [
        "tuple", "in", f"mailbox/{a}", "--pattern", f"to={a}",
        "--claimant", a, "--lease-s", "30",
    ])
    assert early_probe.exit_code == 1, early_probe.output

    # B's side, run off a background thread (its own HttpTupleStore, never
    # the CliRunner A uses) so it lands WHILE A's parked loop below is
    # already waiting -- proving the loop (not one park outlasting the
    # cap) is what finds the ack.
    b_result: dict[str, object] = {}

    def _b_drains_and_acks() -> None:
        b_store = HttpTupleStore()
        time.sleep(1.5)
        claimed = b_store.in_(
            f"mailbox/{b}", {"to": b}, claimant=b, lease_s=60,
        )
        assert claimed is not None, "B must find the request A sent"
        row, claim_id = claimed
        assert row.id == request_id
        assert row.dims["kind"] == "request"
        assert row.dims["correlation_id"] == correlation_id
        b_result["claim_id"] = claim_id

        ack_id = b_store.out(
            f"mailbox/{a}", {"to": a},
            {"from": b, "kind": "ack", "correlation_id": correlation_id},
            "done", nonce=f"ack-{correlation_id}",
        )
        b_result["ack_id"] = ack_id
        b_store.ack(claim_id, b)

    b_thread = threading.Thread(target=_b_drains_and_acks)
    b_thread.start()

    # A parks on its OWN mailbox for the ack, looping at a short per-call
    # cap over an overall budget comfortably longer than B's delay.
    found, calls = _parked_in_loop(
        runner, a, claimant=a, lease_s=30, per_call_timeout_s=1, overall_budget_s=8,
    )
    b_thread.join(timeout=10)
    assert not b_thread.is_alive(), "B's background thread did not finish within the test's budget"

    assert found is not None, "A's parked loop must find B's ack within the overall budget"
    assert calls >= 2, (
        f"a single park call must not have outlasted B's work -- expected the loop to make "
        f"more than one call, got {calls}"
    )
    assert found["tuple"]["dims"]["kind"] == "ack"
    assert found["tuple"]["dims"]["from"] == b
    assert found["tuple"]["dims"]["correlation_id"] == correlation_id
    assert found["tuple"]["id"] == b_result["ack_id"]

    a_ack = runner.invoke(main, ["tuple", "ack", found["claim_id"], "--claimant", a])
    assert a_ack.exit_code == 0, a_ack.output

    # B's own claim on the request tuple was acked too -- the request is
    # fully consumed on both sides.
    assert b_result.get("claim_id")
    rd_b = runner.invoke(main, ["tuple", "rd", f"mailbox/{b}", "--pattern", f"to={b}", "--json"])
    assert rd_b.exit_code == 0, rd_b.output
    assert _tuple_last_json_line(rd_b.output) == [], "B's acked request row must never come back from rd"


@pytest.mark.scenario
def test_cross_instance_unacked_request_is_visible_to_the_sweep(t2_service_env) -> None:
    """The nexus-w374z sweep's mailbox-scan half (nexus-em75s.28): a
    ``kind=request`` tuple with no matching ``kind=ack`` at the requester's
    own mailbox is exactly what ``check_inbound_relay_acks.find_unacked_
    requests`` must flag. Exercised against REAL engine data via
    ``HttpTupleStore`` in-process (never the installed ``nx`` binary from
    a dev session) -- the same shape the sweep's ``fetch_tuple_list_json``/
    ``fetch_tuple_rows_json`` IO boundary hands to that pure function.
    """
    import check_inbound_relay_acks as sweep  # noqa: PLC0415 — scripts/ is on pythonpath (pyproject.toml)

    runner = CliRunner()
    a = _tuple_uniq("nexus-a")
    b = _tuple_uniq("conexus-b")
    correlation_id = _tuple_uniq("corr")

    out = runner.invoke(main, [
        "tuple", "out", f"mailbox/{b}",
        "--key", f"to={b}", "--dim", f"from={a}",
        "--dim", "kind=request", "--dim", f"correlation_id={correlation_id}",
        "--dim", "address_kind=instance", "--body", "never acked",
        "--nonce", correlation_id,
    ])
    assert out.exit_code == 0, out.output
    request_id = out.output.strip().splitlines()[-1]

    store = HttpTupleStore()
    rows_by_subspace: dict[str, list[dict]] = {}
    for census in store.subspace_list("mailbox/"):
        rows = store.rd(census.subspace, n=300)
        rows_by_subspace[census.subspace] = [
            {"id": r.id, "dims": r.dims, "created_at": r.created_at} for r in rows
        ]

    # max_age_days=0: this journey proves the ack-matching wiring against
    # a request written moments ago, not the nexus-em75s.30 Q4 grace-period
    # gate (which defaults to 7 days and would otherwise treat this
    # brand-new row as a legitimate in-flight handshake and skip it).
    unacked = sweep.find_unacked_requests(rows_by_subspace, max_age_days=0)
    matches = [f for f in unacked if f["id"] == request_id]
    assert len(matches) == 1, f"expected the never-acked request among the sweep's findings: {unacked}"
    assert matches[0]["subspace"] == f"mailbox/{b}"
    assert matches[0]["from"] == a
    assert matches[0]["correlation_id"] == correlation_id


def _fake_nx_dir(tmp_path: Path) -> Path:
    """A one-file ``nx`` shim on PATH that execs ``python -m nexus.cli`` --
    same precedent as ``tests/hooks/test_subagent_stop_hook.py``'s
    ``_fake_nx_dir``: the real ``nx`` subprocess-wiring test below must not
    depend on whatever ``nx`` generation happens to be installed on the
    box's real PATH (it can predate RDR-205 and lack the ``tuple``
    subcommand entirely, or simply be a different tree than this one)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    nx_path = bin_dir / "nx"
    nx_path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m nexus.cli "$@"\n')
    nx_path.chmod(0o755)
    return bin_dir


@pytest.mark.scenario
def test_mailbox_sweep_real_nx_subprocess_finds_unacked_skips_acked(
    t2_service_env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """critique nexus-em75s.30 Q4: every other mailbox-scan test drives
    ``check_inbound_relay_acks``'s IO boundary through monkeypatched
    ``fetch_tuple_list_json``/``fetch_tuple_rows_json`` (unit tests) or
    ``HttpTupleStore`` directly (the journey above) -- none exercise the
    REAL ``subprocess.run(["nx", "tuple", ...])`` call the production
    script actually makes. This journey does: ``scan_unacked_mailbox_
    requests`` runs unpatched, so its ``nx tuple list --prefix``/``nx
    tuple rd`` calls go through a real OS subprocess. ``subprocess.run``
    inherits ``os.environ`` by default, and ``t2_service_env``'s
    ``monkeypatch.setenv`` already pointed this test process at the real
    engine + minted tenant, so the shimmed ``nx`` subprocess lands on the
    exact same tenant this test writes to via ``CliRunner``.
    """
    import check_inbound_relay_acks as sweep  # noqa: PLC0415 — scripts/ is on pythonpath (pyproject.toml)

    runner = CliRunner()
    run_id = _tuple_uniq("run")
    a = f"{run_id}-a"
    b = f"{run_id}-b"
    corr_unacked = _tuple_uniq("corr-unacked")
    corr_acked = _tuple_uniq("corr-acked")

    # An unacked request: A -> B, never answered.
    out_unacked = runner.invoke(main, [
        "tuple", "out", f"mailbox/{b}",
        "--key", f"to={b}", "--dim", f"from={a}",
        "--dim", "kind=request", "--dim", f"correlation_id={corr_unacked}",
        "--dim", "address_kind=instance", "--body", "never acked",
        "--nonce", corr_unacked,
    ])
    assert out_unacked.exit_code == 0, out_unacked.output
    unacked_request_id = out_unacked.output.strip().splitlines()[-1]

    # An acked request: A -> B, and B's ack back to A's own mailbox.
    out_acked = runner.invoke(main, [
        "tuple", "out", f"mailbox/{b}",
        "--key", f"to={b}", "--dim", f"from={a}",
        "--dim", "kind=request", "--dim", f"correlation_id={corr_acked}",
        "--dim", "address_kind=instance", "--body", "will be acked",
        "--nonce", corr_acked,
    ])
    assert out_acked.exit_code == 0, out_acked.output
    ack = runner.invoke(main, [
        "tuple", "out", f"mailbox/{a}",
        "--key", f"to={a}", "--dim", f"from={b}",
        "--dim", "kind=ack", "--dim", f"correlation_id={corr_acked}",
        "--dim", "address_kind=instance", "--body", "done",
        "--nonce", f"ack-{corr_acked}",
    ])
    assert ack.exit_code == 0, ack.output

    shim_dir = _fake_nx_dir(tmp_path)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")

    # max_age_days=0: both rows were written moments ago; this journey
    # proves the real subprocess wiring, not the Q4 grace-period gate.
    findings = sweep.scan_unacked_mailbox_requests(
        mailbox_prefix=f"mailbox/{run_id}", max_age_days=0,
    )
    ids = {f["id"] for f in findings}
    assert unacked_request_id in ids, f"the never-acked request must be found: {findings}"
    assert not any(f["correlation_id"] == corr_acked for f in findings), (
        f"the acked request must not be reported: {findings}"
    )
