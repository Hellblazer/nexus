# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-hg2dw acceptance tests, round 2 (T2 critique-nexus-hg2dw-36602c67f
[24598], code-review-nexus-hg2dw-36602c67f [24601]).

Round 1's design fence-began the WHOLE run's needs_fence set right after
Pass 1 registration, closing the registration-to-first-flush window but
enlarging an UNCATCHABLE kill's (SIGKILL/OOM-kill) blast radius from the
original incident's small in-flight batch to the run's entire not-yet-
processed file set. Reverted: the fence now begins per FILE, immediately
before that file's own chunking starts (see prose_indexer.index_prose_file
/ code_indexer.index_code_file). This file covers two properties:

1. A CATCHABLE exit (an exception, or KeyboardInterrupt) still closes the
   window for every registered document: each is either fence-stamped
   'indexing' -> 'failed' via ``index_repository``'s exit-time
   ``_reconcile_needs_fence``, or was never touched because it was
   genuinely unchanged. Both HALVES are asserted independently per the
   code-review finding: the state right after the per-file begin fires
   (BEFORE reconcile ever runs) must already be 'indexing' with a run id,
   and the FINAL state (after reconcile has run in the `finally`) must be
   'failed' — a test that only checked the final state would pass even if
   begin were a no-op, since 'failed' entries look identical either way.

2. An UNCATCHABLE exit (this test simulates it via ``os.fork()`` + the
   child calling ``os._exit()`` from inside the patched fence-begin,
   which bypasses ``finally``/atexit exactly like a real SIGKILL) strands
   ONLY the in-flight file(s) — not the whole run's registered set. T3 is
   pure in-memory (``InMemoryVectorClient``, no sockets) so it is safe to
   fork; the catalog client is constructed fresh per call in every fence
   helper, so it carries no shared file descriptor across the fork.

Fixtures mirror ``tests/test_cp46b_runfence_repo_staleness.py`` and
``tests/db/test_bhlfy_runfence_fail_arms.py``: real engine catalog via
the suite-wide substrate, fake local T3 for chunk content.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction
from nexus.db.t3 import T3Database
from nexus.registry import RepoRegistry
from tests._catalog_fixture_ops import active_reader
from tests.conftest import fake_credentials, make_vector_test_client


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@nexus")
    _git(repo, "config", "user.name", "Nexus Test")


def _commit(repo: Path) -> None:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")


def _entry_ending_with(suffix: str):
    matches = [
        e for e in active_reader().all_documents()
        if str(getattr(e, "file_path", "")).endswith(suffix)
    ]
    assert len(matches) == 1, f"expected exactly one document ending with {suffix!r}, got {matches}"
    return matches[0]


def _entry_by_tumbler(tumbler: str):
    matches = [e for e in active_reader().all_documents() if str(e.tumbler) == tumbler]
    assert len(matches) == 1, f"expected exactly one document with tumbler {tumbler!r}, got {matches}"
    return matches[0]


@pytest.fixture(autouse=True)
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in [
        ("GIT_AUTHOR_NAME", "Test"),
        ("GIT_AUTHOR_EMAIL", "test@test.invalid"),
        ("GIT_COMMITTER_NAME", "Test"),
        ("GIT_COMMITTER_EMAIL", "test@test.invalid"),
    ]:
        monkeypatch.setenv(k, v)


@pytest.fixture
def local_t3() -> T3Database:
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture
def catalog_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    return catalog_dir


@pytest.fixture(autouse=True)
def mock_voyage_client():
    ef = DefaultEmbeddingFunction()
    mock_client = MagicMock()

    def fake_embed(texts, model, input_type="document"):
        r = MagicMock()
        r.embeddings = ef(texts)
        return r

    def fake_contextualized_embed(inputs, model, input_type="document"):
        r = MagicMock()
        br = MagicMock()
        br.embeddings = ef(inputs[0])
        r.results = [br]
        return r

    mock_client.embed.side_effect = fake_embed
    mock_client.contextualized_embed.side_effect = fake_contextualized_embed
    with patch("voyageai.Client", return_value=mock_client):
        yield mock_client


def _seed_manifest_write_batch(t3: T3Database):
    """Wrap *t3*'s ``_write_batch`` so every write also seeds the manifest
    FK the engine's fence/manifest machinery requires (mirrors
    ``test_bhlfy_runfence_fail_arms.py`` / ``test_cp46b_...``'s identical
    helper, inlined here so the fork-based test can rebuild an
    independent T3 instance post-fork without importing a fixture)."""
    from tests._catalog_fixture_ops import seed_manifest_chunks

    orig_write_batch = t3._write_batch

    def _seeding_write_batch(col, collection_name, ids, documents, metadatas,
                              embeddings=None, **kwargs):
        orig_write_batch(col, collection_name, ids, documents, metadatas,
                          embeddings, **kwargs)
        seed_manifest_chunks(collection_name, ids)

    t3._write_batch = _seeding_write_batch
    return t3


_CRASH_MESSAGE = "nexus-hg2dw test: simulated crash before any flush"


def _install_crash_after_first_begin(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Simulate a run dying right after the FIRST file's own fence-begin
    fires, before that file's chunking — the exact "registration-to-
    first-flush" window this bead closes.

    Cannot simply make ``_fence_begin`` itself raise (round-1 approach):
    the per-file begin call site is now deliberately fail-open (its own
    try/except, code-review-nexus-hg2dw-36602c67f [24601] finding 1) so a
    raise from inside it is caught and logged, never propagates, and the
    run would proceed as if nothing happened. Instead: let the begin call
    through for real (capturing the doc's state right after, for the
    "before reconcile" half of the assertion) and inject the crash one
    step later, from the chunker — a raise there is NOT fail-open
    anywhere in the stack, so it aborts ``_run_index`` for real. Chunkers
    for BOTH content types are patched; whichever file the run reaches
    first (code before prose, per ``_run_index``'s loop order) trips it.

    Returns the pre-reconcile snapshot dict, populated with
    ``doc_id`` / ``index_state`` / ``index_run_id`` once the crash fires.
    """
    import nexus.doc_indexer as doc_indexer_mod
    import nexus.chunker as chunker_mod
    import nexus.md_chunker as md_chunker_mod

    real_begin = doc_indexer_mod._fence_begin
    snapshot: dict = {}

    def _capture_begin(doc_id, content_hash, collection):
        real_begin(doc_id, content_hash, collection)
        entry = _entry_by_tumbler(doc_id)
        snapshot["doc_id"] = doc_id
        snapshot["index_state"] = entry.index_state
        snapshot["index_run_id"] = getattr(entry, "index_run_id", "")

    def _raise_crash(*_a, **_k):
        raise RuntimeError(_CRASH_MESSAGE)

    monkeypatch.setattr(doc_indexer_mod, "_fence_begin", _capture_begin)
    monkeypatch.setattr(chunker_mod, "chunk_file", _raise_crash)
    monkeypatch.setattr(md_chunker_mod.SemanticMarkdownChunker, "chunk", _raise_crash)
    return snapshot


def _index(repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the real ``index_repository`` pipeline (a NORMAL run:
    ``force=False``)."""
    from nexus.indexer import index_repository

    monkeypatch.setenv("NX_LOCAL", "1")
    reg = RepoRegistry(repo.parent / "repos.json")
    reg.add(repo)

    _seed_manifest_write_batch(t3)

    with patch("nexus.db.make_t3", return_value=t3), \
         patch("nexus.config.get_credential", side_effect=fake_credentials()):
        index_repository(repo, reg, force=False)


class TestRunDiesAfterRegistrationBeforeAnyFlush:
    """CATCHABLE exits: an exception propagating out of ``_run_index``.
    ``index_repository``'s own ``finally`` still runs (Python guarantees
    this for any raised exception, including ``KeyboardInterrupt``), so
    ``_reconcile_needs_fence`` fires and every needs_fence document ends
    resolved."""

    def test_new_documents_end_failed_not_indexing_not_null(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "a.md").write_text("# A\n\nBrand new content.\n", encoding="utf-8")
        (repo / "b.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        _commit(repo)

        # Simulate the run dying immediately after the FIRST file's own
        # fence-begin fires (per-file placement, critique round 2) —
        # before that file's chunking, and therefore before any chunk
        # ever flushes. Snapshot the catalog state RIGHT THERE, before
        # the exception has even started propagating, let alone before
        # index_repository's finally (and _reconcile_needs_fence) runs —
        # this is the "state BEFORE reconcile" half the code review asked
        # for; the "state AFTER reconcile" half is asserted post-run below.
        pre_reconcile_snapshot = _install_crash_after_first_begin(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash"):
            self._run(repo, local_t3, monkeypatch)

        # Half 1 (code-review finding 2): the begin call is not a no-op —
        # the FIRST file to reach real work was genuinely 'indexing',
        # with a real run id, at the exact moment of the simulated crash,
        # strictly before any reconciliation could have run.
        assert pre_reconcile_snapshot["index_state"] == "indexing", (
            f"fence-begin did not stamp 'indexing' before the crash: {pre_reconcile_snapshot}"
        )
        assert pre_reconcile_snapshot["index_run_id"], (
            "fence-begin stamped 'indexing' but with no run id — "
            f"{pre_reconcile_snapshot}"
        )

        # Half 2: after index_repository's finally has run (we are past
        # pytest.raises here), reconciliation must have resolved BOTH
        # registered documents — the one that got its own begin call
        # (now must be 'failed', not still 'indexing'), and the sibling
        # that never even reached its own begin call (registered by Pass
        # 1, state None, also fail-stamped by the exit-time reconcile).
        for suffix in ("a.md", "b.py"):
            entry = _entry_ending_with(suffix)
            assert entry.index_state == "failed", (
                f"{suffix}: expected 'failed' after index_repository's exit-time "
                f"reconciliation ran, got {entry.index_state!r} (nexus-hg2dw: "
                f"'indexing' or None here means the fence closure did not work)"
            )

    def test_doctor_stale_fence_check_reports_clean_afterward(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.health import _check_stale_indexing_runs

        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "a.md").write_text("# A\n\nBrand new content.\n", encoding="utf-8")
        _commit(repo)

        _install_crash_after_first_begin(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash"):
            self._run(repo, local_t3, monkeypatch)

        results = _check_stale_indexing_runs()
        label = "stale index-run fences"
        matching = [r for r in results if r.label == label]
        assert matching, f"expected a '{label}' result, got {results}"
        for r in matching:
            assert r.ok, f"nexus-hg2dw: doctor still flags a stale fence: {r.detail}"

    def test_unchanged_existing_document_is_left_complete_not_reprocessed(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-regression control: a document already 'complete' from a
        PRIOR successful run, whose file content this run does NOT
        change, must never be touched by the per-file fence — even
        though the crash-injected run also registers a brand-new
        sibling file that DOES need fencing."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        # nexus-cp46b test-fixture note (see test_cp46b_runfence_repo_
        # staleness.py's identical priming): a repo's very FIRST-EVER
        # index run resolves the docs__ physical collection differently
        # from every run after it (the catalog owner doesn't exist yet).
        # Without priming, stable.md's chunks would land in one physical
        # collection on this test's first _run() and get looked up
        # against a DIFFERENT one on the second, making check_staleness
        # wrongly conclude "not fresh" and re-chunk it into this test's
        # injected crash.
        (repo / "prime.md").write_text("# Prime\n\nOwner-registration bootstrap.\n", encoding="utf-8")
        _commit(repo)
        self._run(repo, local_t3, monkeypatch)

        (repo / "stable.md").write_text(
            "# Stable\n\nNever changes.\n", encoding="utf-8",
        )
        _commit(repo)
        self._run(repo, local_t3, monkeypatch)  # normal, successful run
        stable_before = _entry_ending_with("stable.md")
        assert stable_before.index_state == "complete"

        (repo / "new.md").write_text(
            "# New\n\nAdded on the second, crashing run.\n", encoding="utf-8",
        )
        _commit(repo)

        _install_crash_after_first_begin(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash"):
            self._run(repo, local_t3, monkeypatch)

        stable_after = _entry_ending_with("stable.md")
        assert stable_after.index_state == "complete", (
            "nexus-hg2dw: an unchanged, already-complete document must "
            "never be re-fenced just because a sibling file's crashed "
            "processing needed fencing this run"
        )
        new_after = _entry_ending_with("new.md")
        assert new_after.index_state == "failed"

    def test_second_normal_run_heals_a_stranded_document(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Critique observation 8: no test in round 1 ran a SECOND,
        healing pass after a crashed run. A document fail-stamped by
        exit-time reconciliation must actually clear (reach 'complete')
        on the very next normal run."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / "a.md").write_text("# A\n\nBrand new content.\n", encoding="utf-8")
        _commit(repo)

        import nexus.chunker as chunker_mod
        import nexus.md_chunker as md_chunker_mod

        real_chunk_file = chunker_mod.chunk_file
        real_smc_chunk = md_chunker_mod.SemanticMarkdownChunker.chunk

        _install_crash_after_first_begin(monkeypatch)
        with pytest.raises(RuntimeError, match="simulated crash"):
            self._run(repo, local_t3, monkeypatch)
        assert _entry_ending_with("a.md").index_state == "failed"

        # Restore the REAL chunkers for the healing pass — monkeypatch's
        # own restore only fires at test teardown, not mid-test, and this
        # test deliberately runs a SECOND, non-crashing pass within itself.
        monkeypatch.setattr(chunker_mod, "chunk_file", real_chunk_file)
        monkeypatch.setattr(md_chunker_mod.SemanticMarkdownChunker, "chunk", real_smc_chunk)
        self._run(repo, local_t3, monkeypatch)  # a normal, non-crashing pass

        healed = _entry_ending_with("a.md")
        assert healed.index_state == "complete", (
            "nexus-hg2dw: a document fail-stamped by exit-time "
            "reconciliation must clear on the next normal run "
            f"(nexus-cp46b's fence-aware staleness check), got {healed.index_state!r}"
        )

    @staticmethod
    def _run(repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch) -> None:
        _index(repo, t3, monkeypatch)


class TestHardKillBlastRadius:
    """T2 critique-nexus-hg2dw-36602c67f [24598] finding 1's explicit
    ask: an UNCATCHABLE exit (no ``finally`` ever runs) after N files
    must strand only those N — not the whole run's registered set. This
    is the property the round-1 design got wrong (a whole-run-upfront
    begin meant a hard kill anywhere in the run stranded EVERYTHING);
    round 2's per-file placement bounds the blast radius to whatever was
    actively in flight.

    Implementation note: a REAL subprocess (``tests/_hg2dw_hard_kill_
    child.py``) that calls ``os._exit()`` from inside the patched
    fence-begin, rather than ``os.fork()`` + ``os._exit()`` in-process or
    an external ``SIGKILL`` after a timed sleep. ``os.fork()`` was tried
    first and SEGFAULTED — this test process has torch/scipy/lxml and
    other C-extension-heavy libraries loaded with background threads,
    and forking a multi-threaded CPython process is explicitly unsafe
    (measured directly, not assumed). A genuine subprocess (spawned via
    ``sys.executable``, not forked) sidesteps that entirely — its own
    fresh interpreter, its own threads, nothing inherited. It reaches
    the SAME shared engine substrate via the inherited ``NX_SERVICE_URL``
    / ``NX_SERVICE_TOKEN`` / ``NX_LOCAL`` env vars, and ``os._exit()``
    bypasses ``finally``/atexit/every CPython cleanup hook there exactly
    as it would in-process — that bypass IS the property under test,
    and it is what an external ``SIGKILL`` would also produce, just
    without that approach's timing race against a sleep.
    """

    @staticmethod
    def _run_hard_kill_subprocess(
        repo: Path, kill_after: int, monkeypatch: pytest.MonkeyPatch,
    ):
        """Shared by both tests in this class: spawn the child, assert
        the hard-exit fired as designed, return the child's
        ``CompletedProcess`` for detail on failure."""
        import subprocess
        import sys

        monkeypatch.setenv("NX_LOCAL", "1")
        monkeypatch.setenv("NX_INDEX_CONCURRENCY", "1")  # deterministic ordering
        repos_json = repo.parent / "repos.json"

        child_script = Path(__file__).parent / "_hg2dw_hard_kill_child.py"
        env = os.environ.copy()
        env["NX_HG2DW_REPO"] = str(repo)
        env["NX_HG2DW_REPOS_JSON"] = str(repos_json)
        env["NX_HG2DW_KILL_AFTER"] = str(kill_after)
        # nexus-a2qhz's dev-checkout production-write guard (landed on
        # develop after this test was first written) refuses every write
        # from a dev-checkout process unless NX_ALLOW_PROD_WRITE carries
        # an explicit reason. The parent pytest process is exempted via
        # an in-process override (tests/conftest.py's autouse
        # _exempt_pytest_from_production_write_guard) that, BY THAT
        # FIXTURE'S OWN DESIGN, deliberately does NOT leak into a
        # subprocess's environment — a real subprocess needing the
        # guard's actual accept path must set the REAL env var itself.
        # This child targets the SAME throwaway test substrate the
        # parent's own writes already target, never production.
        env["NX_ALLOW_PROD_WRITE"] = (
            "nexus-hg2dw hard-kill test subprocess — targets the shared "
            "pytest engine substrate via inherited NX_SERVICE_URL, never "
            "production"
        )

        result = subprocess.run(
            [sys.executable, "-u", str(child_script)],
            env=env, cwd=str(Path(__file__).parent.parent),
            capture_output=True, text=True, timeout=120,
        )

        assert result.returncode == 137, (
            f"expected the child to hard-exit via os._exit(137) inside the "
            f"{kill_after}th fence-begin call, got returncode {result.returncode!r} "
            f"(99 means the kill never fired at all)\n"
            f"--- child stdout ---\n{result.stdout}\n"
            f"--- child stderr ---\n{result.stderr}"
        )
        return result

    def test_hard_kill_after_n_files_strands_only_those_in_flight(
        self, tmp_path: Path, catalog_env: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        repo = tmp_path / "repo"
        _init_repo(repo)
        file_count = 5
        for i in range(file_count):
            (repo / f"file{i}.md").write_text(f"# File {i}\n\nContent {i}.\n", encoding="utf-8")
        _commit(repo)

        kill_after = 2
        self._run_hard_kill_subprocess(repo, kill_after, monkeypatch)

        # nexus-hg2dw sequential processing (NX_INDEX_CONCURRENCY=1): each
        # file's begin -> chunk -> embed -> upload -> complete runs to
        # full completion before the NEXT file's own begin call ever
        # fires. So a kill on the Nth begin call finds N-1 prior files
        # already 'complete', the Nth file 'indexing' (mid-flight, right
        # where the kill hit), and every later file untouched (None,
        # never even reached its own begin) — never N files 'indexing'
        # at once. This IS the bounded-blast-radius property under test:
        # exactly ONE document is ever left mid-flight, regardless of how
        # many files the run had already gotten through.
        entries = list(active_reader().all_documents())
        assert len(entries) == file_count
        indexing = [e for e in entries if getattr(e, "index_state", None) == "indexing"]
        complete = [e for e in entries if getattr(e, "index_state", None) == "complete"]
        failed = [e for e in entries if getattr(e, "index_state", None) == "failed"]
        untouched = [e for e in entries if getattr(e, "index_state", None) is None]
        detail = [(str(e.file_path), e.index_state) for e in entries]

        assert len(indexing) == 1, (
            f"expected exactly ONE document stranded 'indexing' — the file "
            f"actively being processed at kill time — got {len(indexing)}: {detail}"
        )
        assert len(complete) == kill_after - 1, (
            f"expected exactly {kill_after - 1} document(s) already 'complete' "
            f"before the kill fired on the {kill_after}th begin call, got "
            f"{len(complete)}: {detail}"
        )
        assert len(untouched) == file_count - kill_after, (
            f"expected exactly {file_count - kill_after} document(s) never "
            f"reached (registered by Pass 1, state still None — bounded blast "
            f"radius, nexus-hg2dw critique round 2), got {len(untouched)}: {detail}"
        )
        assert not failed, (
            "no Python cleanup (including _reconcile_needs_fence, which "
            "would fail-stamp the untouched documents) can have run in the "
            f"hard-killed child — found 'failed' entries: {detail}"
        )

    def test_second_normal_run_heals_the_none_state_documents(
        self, tmp_path: Path, catalog_env: Path, local_t3: T3Database,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T2 code-review-nexus-hg2dw-52d06c8c5 [24626] finding 1: the
        documents a hard kill leaves at ``index_state=None`` (never even
        reached their own fence-begin — see the bounded-blast-radius test
        above) are NOT covered by nexus-cp46b's fence-aware staleness
        override at all — that mechanism (``_catalog_hook``'s
        ``stale_fence_doc_ids`` out-param, threaded into the staleness
        caches as ``never_fresh``) only forces a doc stale when its
        REPORTED ``index_state`` is ``'indexing'`` or ``'failed'``; a
        reported-but-``None`` state is excluded from that set by design
        (it is the documented "unknown -> fall through" case, memo
        §3.1/§3.4, also relied on by the ``store_put`` fence-exclusion
        ruling on this same bead). So this population heals by ORDINARY
        content-hash staleness, not by any fence-specific repair: since
        nothing was ever written for them in T3, ``check_staleness``
        finds no matching chunk on the very next run and correctly
        concludes "not fresh, must index" — the same path any newly-
        discovered file takes, fence or no fence. This test is the
        acceptance proof for that healing path specifically, complementing
        (not duplicating) ``test_second_normal_run_heals_a_stranded_
        document`` above, which covers the 'failed'-state (reconciled)
        population via the SAME staleness mechanism from a different
        starting state.

        T3 in this suite is a pure in-memory ``InMemoryVectorClient``
        (process-local), so the killed child's own T3 content does not
        (and structurally cannot) carry over to this test's own
        in-process second pass — that is fine: the claim under test is
        that a None-state document heals via check_staleness on its next
        run, which holds regardless of what any other document's T3
        content happens to be. In a real install T3 is the SAME database
        across runs, so the already-'complete'/'indexing' documents from
        the killed run would not be redundantly reprocessed there.
        """
        repo = tmp_path / "repo"
        _init_repo(repo)
        file_count = 5
        for i in range(file_count):
            (repo / f"file{i}.md").write_text(f"# File {i}\n\nContent {i}.\n", encoding="utf-8")
        _commit(repo)

        kill_after = 2
        self._run_hard_kill_subprocess(repo, kill_after, monkeypatch)

        before = list(active_reader().all_documents())
        none_state_paths = {
            str(e.file_path) for e in before if getattr(e, "index_state", None) is None
        }
        assert none_state_paths, (
            "test setup sanity: the hard kill must leave at least one "
            f"None-state document to heal, got {[(str(e.file_path), e.index_state) for e in before]}"
        )
        assert len(none_state_paths) == file_count - kill_after

        self._run(repo, local_t3, monkeypatch)  # a normal, non-crashing pass

        after = {str(e.file_path): e for e in active_reader().all_documents()}
        for path in none_state_paths:
            healed_state = after[path].index_state
            assert healed_state == "complete", (
                f"{path}: a None-state document (never reached its own "
                f"fence-begin under the hard kill) must reach 'complete' "
                f"on the very next normal run via ordinary check_staleness "
                f"content comparison, got {healed_state!r}"
            )

    @staticmethod
    def _run(repo: Path, t3: T3Database, monkeypatch: pytest.MonkeyPatch) -> None:
        _index(repo, t3, monkeypatch)
