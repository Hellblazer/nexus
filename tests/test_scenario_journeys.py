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
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
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
        "store", "put", "-", "--title", title, "-c", "knowledge",
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
        "store", "put", "-", "--title", "scenario3-unrelated-seed", "-c", "knowledge",
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
    # `nx search --json` does NOT emit valid JSON on the zero-hits path —
    # search_cmd.py's zero-result branches unconditionally `click.echo("No
    # results.")` regardless of `--json` (known CLI --json-contract gap,
    # T2 nexus/test-suite-compression-P2-reduced-critique FINDING IN
    # PASSING; out of scope here). Assert the exact documented output
    # shape rather than silently skipping the check on a non-JSON payload.
    stripped = knowledge_search.stdout.strip()
    if stripped == "No results.":
        pass  # zero-hits contract, asserted explicitly above
    else:
        knowledge_hits = json.loads(stripped)
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
        "store", "put", "-", "--title", "scenario4-wombat-note", "-c", "knowledge",
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
