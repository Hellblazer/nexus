# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-235 Phase 2 pre-condition (nexus-fx9k2): the three seams the MVV approximated.

``tests/test_rdr235_mvv_aspect_queue_tuples.py`` registers documents and enqueues
them through ``enqueue_many`` rather than indexing, fakes the extractor, and runs
the worker at ``batch_size=1`` (the production default is 5). This journey closes
those three gaps against the real engine substrate, in ONE test so the tenant
ceiling rises by one (``tests/storage_touch_ceiling.toml``).

THE SCRIPT. A tmp git repo with four small RDR files under ``docs/rdr/`` (so the
collection is ``rdr__*`` and the extractor is the pure-Python
``rdr-frontmatter-v1``, no model call), ``NX_ASPECT_QUEUE=tuples``, and a real
``index_repository()`` run. Then a worker at ``batch_size=5`` claims the four
tasks in one pass.

SEAM 1, index to task on the success path. No stub on the producer: the
ChunkBatcher's flush hook (``indexer._fire_flush_grain_hooks``) calls
``enqueue_many`` and four ``aspects`` tasks appear on the engine. Their bodies
are the identity body (collection, source_path, doc_id, content_hash; no text),
each ``doc_id`` is a document of the catalog in that collection whose file is the
one the task names, and the ``hook_failures`` table has no row for the enqueue
hook. A delegating spy on ``HttpAspectQueue.enqueue`` / ``enqueue_many`` records
that the batch producer ran and the per-document one did not; the real method
runs either way.

SEAM 2, a real read through the manifest. For two of the four documents the
extractor is the REAL ``extract_aspects_batch``. Each task carries no text, so
the call arrives with ``content == ""`` and the text comes from the T3 chunks
through the catalog manifest lookup, with the frontmatter put back from the source
file (the indexer strips it before chunking, nexus-xho6c). The ``document_aspects``
rows are asserted on the facts the files declare: the title, status and id in
``extras`` and a phrase that exists only in the section body (so it can have come
only from the chunks). Field presence and values, never whole strings (A2).

SEAM 3, the batch-of-5 mixed verdict. The other two documents are forced through
the documented patch point ``nexus.aspect_worker._extract_aspects_batch`` BY PATH:
the stand-in delegates the rest of the batch to the real function and gives one
path a retryable error (``httpx.ConnectError``) on its first call and the other a
non-retryable one (``ValueError``). The stand-in puts them in their slots as
:class:`ExtractRaised`, the per-row carrier ``_extract_tuple_rows`` itself uses;
the real batch function cannot raise for one row only. One pass of four claims
(five slots, four tasks) gives, on the engine: two consumed with rows, one nacked
with ``attempts == 1`` and the ladder's ``available_at`` stamp, one dead with no
row. A second pass consumes the retried one. ``nx aspects requeue-failed
--dry-run`` lists the dead one.

TIMING is the engine's: ``available_at`` against the ``claimed_at`` the engine
stamped, never an immediate re-claim to show a delay (the lesson of 39edf6cf2).
Local ``time.monotonic`` is only the hang bound (``_HANG_S``).

MUTATIONS, each run red and restored; the record is in T2
``nexus/rdr235-mvv-seams-2026-10-30``.
"""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.aspect_worker import AspectExtractionWorker
from nexus.aspect_worker_tuples import ExtractRaised
from nexus.db.t2.http_aspect_queue import ASPECT_QUEUE_ENV, HttpAspectQueue

pytestmark = pytest.mark.usefixtures("t2_service_env")

_HOOK = "aspect_extraction_enqueue_hook"
_SUB = "aspects"
_KEYS = {"queue": "aspects"}

#: A hang bound, not a performance assertion (nexus-61vos).
_HANG_S = 120.0
#: The ladder, shrunk through the module's own constants: 2 s * 2**attempts, no jitter.
_RETRY_BASE_S = 2.0
#: Lease of every claim; no pass is long, so it never lapses.
_LEASE_S = 30

#: (file stem, id, title, status, section prose). The prose is unique per file and
#: appears only in a section body, so a row that carries it was read from chunks.
_RDRS = [
    ("rdr-901-alpha", "RDR-901", "Alpha Seam Probe", "accepted", "alpha reads its body from the manifest"),
    ("rdr-902-bravo", "RDR-902", "Bravo Seam Probe", "draft", "bravo reads its body from the manifest"),
    ("rdr-903-retry", "RDR-903", "Charlie Seam Probe", "closed", "charlie is retried once before it reads"),
    ("rdr-904-dead", "RDR-904", "Delta Seam Probe", "draft", "delta is refused and never reads"),
]
_REAL = ("rdr-901-alpha", "rdr-902-bravo")
_RETRIED = "rdr-903-retry"
_DEAD = "rdr-904-dead"


def _stem(path: str) -> str:
    return Path(path).stem


def _git_init(repo: Path) -> None:
    for cmd in (
        ["git", "init"],
        ["git", "config", "user.email", "test@test"],
        ["git", "config", "user.name", "test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


def _rdr_repo(tmp_path: Path) -> Path:
    repo = tmp_path / f"seams-repo-{uuid.uuid4().hex[:6]}"
    (repo / "docs" / "rdr").mkdir(parents=True)
    for stem, rid, title, status, phrase in _RDRS:
        (repo / "docs" / "rdr" / f"{stem}.md").write_text(
            f"---\nid: {rid}\ntitle: {title}\ntype: Feature\nstatus: {status}\n---\n\n"
            f"# {title}\n\n## Problem Statement\n\nThe seams journey says {phrase}, "
            f"and run {uuid.uuid4().hex[:8]} keeps its chunk apart from every other repo.\n\n"
            f"## Proposed Solution\n\nIndex it, queue it, read it back.\n"
        )
    _git_init(repo)
    return repo


def _ts(value: str | None) -> datetime:
    assert value, "the engine must render this timestamp"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _wait(pred, what: str, timeout: float = _HANG_S, poll: float = 0.2) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(poll)
    raise AssertionError(f"hang bound ({timeout}s) reached waiting for: {what}")


def _tasks(db: Any) -> list:
    return db.tuples.rd(_SUB, dict(_KEYS), n=300)


def _by_stem(db: Any) -> dict[str, Any]:
    return {_stem(json.loads(t.body)["source_path"]): t for t in _tasks(db)}


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One T2Database per test; the worker's routed writes use it."""
    import nexus.mcp_infra as infra
    from nexus.db.t2 import T2Database

    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setenv("NX_LOCAL", "1")
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
    monkeypatch.setenv(ASPECT_QUEUE_ENV, "table")  # explicit; the seams that need the tuple producer set "tuples"
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    d = T2Database(tmp_path / "t2.db")
    monkeypatch.setattr(infra, "t2_index_write", lambda fn, **kw: fn(d))
    yield d
    d.close()


class _ProducerSpy:
    """Delegating spy on the aspect queue's two producer calls: it records, then
    runs the real method. Nothing is faked and nothing is short-circuited."""

    def __init__(self) -> None:
        self.enqueue_many_rows: list[int] = []
        self.enqueue_calls = 0

    def install(self, mp: pytest.MonkeyPatch) -> None:
        spy = self
        real_many, real_one = HttpAspectQueue.enqueue_many, HttpAspectQueue.enqueue

        def many(self_, rows):  # noqa: ANN001
            spy.enqueue_many_rows.append(len(rows))
            return real_many(self_, rows)

        def one(self_, *a, **kw):  # noqa: ANN001
            spy.enqueue_calls += 1
            return real_one(self_, *a, **kw)

        mp.setattr(HttpAspectQueue, "enqueue_many", many)
        mp.setattr(HttpAspectQueue, "enqueue", one)


class _Extractor:
    """The documented patch points, by path only.

    ``_extract_aspects_batch`` gives the retried path a ``ConnectError`` on its
    first call and the dead path a ``ValueError`` on every call, and hands every
    other item to the REAL ``extract_aspects_batch``. ``_extract_aspects`` (the
    single-document call a lone claim takes) does the same. Every item that
    arrives is recorded, with the engine's ``claimed_at`` for its task read at
    that moment.
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self.batch_items: list[list[tuple]] = []
        self.single_items: list[tuple] = []
        self.calls: dict[str, int] = {}
        self.claimed_at: dict[str, datetime] = {}

    def install(self, mp: pytest.MonkeyPatch) -> None:
        mp.setattr("nexus.aspect_worker._extract_aspects_batch", self.batch)
        mp.setattr("nexus.aspect_worker._extract_aspects", self.single)

    def _note(self, source_path: str) -> str:
        stem = _stem(source_path)
        self.calls[stem] = self.calls.get(stem, 0) + 1
        for t in _tasks(self._db):
            body = json.loads(t.body)
            if body["source_path"] == source_path and t.claimed_at:
                self.claimed_at.setdefault(f"{stem}#{self.calls[stem]}", _ts(t.claimed_at))
        return stem

    def _forced_failure(self, stem: str) -> BaseException | None:
        if stem == _RETRIED and self.calls[stem] == 1:
            return httpx.ConnectError("T3 unreachable (forced, first call)")
        if stem == _DEAD:
            return ValueError("malformed document (forced)")
        return None

    def batch(self, items, **kw):
        from nexus.aspect_extractor import extract_aspects_batch as real

        self.batch_items.append(list(items))
        stems = [self._note(it[1]) for it in items]
        out: list[Any] = [None] * len(items)
        passthrough = []
        for i, stem in enumerate(stems):
            failure = self._forced_failure(stem)
            if failure is not None:
                out[i] = ExtractRaised(failure)
            else:
                passthrough.append(i)
        if passthrough:
            for i, rec in zip(passthrough, real([items[i] for i in passthrough], **kw)):
                out[i] = rec
        return out

    def single(self, content, source_path, collection, **kw):
        from nexus.aspect_extractor import extract_aspects as real

        self.single_items.append((collection, source_path, content))
        failure = self._forced_failure(self._note(source_path))
        if failure is not None:
            raise failure
        return real(content, source_path, collection, **kw)


def _assert_facts(db: Any, doc_id: str, stem: str, label: str) -> None:
    """The frontmatter facts the file declares, and a phrase that only the
    chunks carry, on the document's ``document_aspects`` row."""
    _, rid, title, status, phrase = next(r for r in _RDRS if r[0] == stem)
    rec = db.document_aspects.get_by_doc_id(doc_id)
    assert rec is not None, f"{label}: no document_aspects row for {stem}"
    extras = rec.extras or {}
    assert extras.get("rdr_title") == title, (label, stem, extras)
    assert extras.get("rdr_status") == status, (label, stem, extras)
    assert extras.get("rdr_id") == rid, (label, stem, extras)
    assert rec.extractor_name == "rdr-frontmatter-v1", (label, stem, rec.extractor_name)
    assert phrase in (rec.problem_formulation or ""), (
        f"{label}: {stem}'s section body did not come through the manifest: {rec.problem_formulation!r}"
    )


@pytest.mark.scenario
def test_the_three_seams_the_mvv_approximated(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index to task on success, a real manifest read, and a mixed batch verdict."""
    from click.testing import CliRunner

    from nexus import aspect_worker as w
    from nexus.catalog.factory import make_catalog_reader
    from nexus.commands.aspects import aspects_group
    from nexus.indexer import index_repository
    from nexus.registry import RepoRegistry

    monkeypatch.setattr(w, "_RETRY_BASE_SECONDS", _RETRY_BASE_S)
    monkeypatch.setattr(w, "_RETRY_JITTER_FRACTION", 0.0)
    monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuples")
    spy = _ProducerSpy()
    spy.install(monkeypatch)

    # ── seam 1: a real index_repository() reaches the tuple queue ─────────────
    repo = _rdr_repo(tmp_path)
    failures_before = db.telemetry.list_hook_failures(hook_names=[_HOOK])["total"]
    reg = RepoRegistry(tmp_path / "repos.json")
    reg.add(repo)
    index_repository(repo, reg)

    assert spy.enqueue_many_rows and sum(spy.enqueue_many_rows) == 4, (
        f"seam 1: the flush hook's batch producer did not enqueue the four documents: {spy.enqueue_many_rows}"
    )
    assert spy.enqueue_calls == 0, "seam 1: the per-document producer ran; the flush hook did not own the enqueue"
    tasks = _tasks(db)
    assert len(tasks) == 4, f"seam 1: four aspects tasks on the engine, got {len(tasks)}"
    cat = make_catalog_reader()
    collections = set()
    for t in tasks:
        body = json.loads(t.body)
        assert sorted(body) == ["collection", "content_hash", "doc_id", "source_path"], (
            f"seam 1: not an identity body: {sorted(body)}"
        )
        assert len(t.body.encode("utf-8")) < 4096
        assert body["collection"].startswith("rdr__"), body
        entry = cat.by_doc_id(body["doc_id"])
        assert entry is not None, f"seam 1: doc_id {body['doc_id']} is not in the catalog"
        assert entry.physical_collection == body["collection"], (body, entry.physical_collection)
        assert _stem(entry.file_path or entry.source_uri) == _stem(body["source_path"]), (
            f"seam 1: the task's doc_id is another document's: {body} vs {entry.file_path}"
        )
        collections.add(body["collection"])
    assert sorted(_by_stem(db)) == sorted(r[0] for r in _RDRS)
    assert len(collections) == 1
    coll = collections.pop()
    failures = db.telemetry.list_hook_failures(hook_names=[_HOOK])
    assert failures["total"] == failures_before == 0, (
        f"seam 1: the enqueue hook recorded a failure: {failures}"
    )
    doc_of = {stem: json.loads(t.body)["doc_id"] for stem, t in _by_stem(db).items()}
    consumed_before = db.tuples.subspace_stats(_SUB).consumed

    # ── seams 2 and 3: one batch pass of the production size ──────────────────
    ex = _Extractor(db)
    ex.install(monkeypatch)
    worker = AspectExtractionWorker(
        poll_interval=0.1, batch_size=5,
        tuple_options={
            "lease_s": _LEASE_S, "park_timeout_s": 1, "renew_every_s": 1.0,
            "deadline_s": lambda n: float(_LEASE_S),
        },
    )
    side = worker._tuple_side
    claims, failed = side.claim_batch(park=False)
    assert not failed
    assert len(claims) == 4 and len({c.task_id for c in claims}) == 4, "four tasks held in one pass"
    assert len({c.claimant for c in claims}) == 4 and all(c.slot < 5 for c in claims), (
        "one claimant string per slot, slots 1..4 are visited before 0"
    )
    assert all(c.attempts == 0 for c in claims)
    side.process(claims)

    assert len(ex.batch_items) == 1 and not ex.single_items, (
        "the four claims went to the extractor as ONE batch call"
    )
    assert len(ex.batch_items[0]) == 4 and all(it[2] == "" for it in ex.batch_items[0]), (
        "seam 2: the extractor was handed no text; the read is its own"
    )
    after = db.tuples.subspace_stats(_SUB)
    assert after.consumed - consumed_before == 2, (
        f"seam 3: two tasks consumed in the first pass, got {after.consumed - consumed_before}"
    )
    held = _by_stem(db)
    assert sorted(held) == sorted([_DEAD, _RETRIED]), f"seam 3: the survivors of the pass: {sorted(held)}"

    # seam 2: the rows carry what the files declare
    for stem in _REAL:
        _assert_facts(db, doc_of[stem], stem, "seam 2")

    # seam 3: one nacked with the ladder's stamp, one dead, neither wrote a row
    retried, dead = held[_RETRIED], held[_DEAD]
    assert retried.attempts == 1 and retried.claim_state != "dead", retried
    assert retried.available_at is not None, "seam 3: the retried task carries the ladder's available_at stamp"
    interval = w._backoff_interval_seconds(0)
    assert interval == 2
    gap = (_ts(retried.available_at) - ex.claimed_at[f"{_RETRIED}#1"]).total_seconds()
    assert gap >= interval, f"seam 3: engine-clock available_at is {gap}s after the claim, under the ladder's {interval}s"
    assert dead.claim_state == "dead" and dead.attempts == 1, dead
    for stem in (_RETRIED, _DEAD):
        assert db.document_aspects.get_by_doc_id(doc_of[stem]) is None, f"seam 3: {stem} wrote a row"

    # ── the second pass consumes the retried task ──────────────────────────────
    second: list = []

    def claimable() -> bool:
        got, _ = side.claim_batch(park=False)
        second.extend(got)
        return bool(got)

    _wait(claimable, "the retried task to become claimable at its available_at")
    assert len(second) == 1 and second[0].attempts == 1
    assert _stem(second[0].row.source_path) == _RETRIED
    side.process(second)
    assert [_stem(i[1]) for i in ex.single_items] == [_RETRIED], "a lone claim takes the single-document call"
    assert ex.single_items[0][2] == "", "seam 2: the retried document was read with no text handed in"
    _assert_facts(db, doc_of[_RETRIED], _RETRIED, "seam 3 second pass")
    assert db.tuples.subspace_stats(_SUB).consumed - consumed_before == 3
    assert sorted(_by_stem(db)) == [_DEAD], "only the dead task is left"
    assert db.document_aspects.get_by_doc_id(doc_of[_DEAD]) is None

    dry = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run", "--collection", coll])
    assert dry.exit_code == 0, dry.output
    assert "Would re-enqueue 1 dead task(s)" in dry.output, dry.output
    assert f"{_DEAD}.md" in dry.output and "attempts=1" in dry.output, dry.output
    assert [_stem(json.loads(t.body)["source_path"]) for t in _tasks(db) if t.claim_state == "dead"] == [_DEAD], (
        "a dry run revives nothing"
    )
