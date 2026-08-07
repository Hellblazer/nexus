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
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests._catalog_fixture_ops import active_reader, documents_by_file_path, documents_by_title


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
    idx = runner.invoke(main, ["index", "md", str(md), "--corpus", "scenario2"])
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
    idx = runner.invoke(main, ["index", "md", str(md), "--corpus", "scenario4"])
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
