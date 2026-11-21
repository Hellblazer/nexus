# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P5-S2, the Minimum Viable Validation for GH #1571: one journey, every step through the CLI.

``nx index pdf`` a PDF (tumbler OLD), link another document to it, move the file, ``nx index pdf`` at the new path
(a second live document, GH #1571), take the hint line that run printed and run it exactly as printed (the dry run),
then rerun it with ``--no-dry-run --confirm``. Acceptance: one live document at the new path, it carries OLD, the link
is on it, and a search for a phrase in the PDF returns exactly one hit.

The PDF goes into a ``knowledge__`` collection so the indexing run queues aspect extraction. The journey settles that
queue with a worker whose extractor is the only faked part (``nexus.aspect_worker._extract_aspects``, as in the
RDR-235 journeys): the old document's extraction is drained BEFORE the move, so the move has an aspect row to
re-key, and the new document's task is held IN FLIGHT across the move and released after it. That is the race the
P1 review recorded (a queued extraction for the absorbed document upserting after the move and flipping the old
tumbler's re-keyed row back to the absorbed one through ``EX_DOC_ID_COALESCE``). The assertion is that the old
tumbler's row is still the old tumbler's, at the new path, with no stale row at the old path.

Not covered here: the owner fold measured on a real ~7,000-row owner (the other P5 MVV residual), which needs a large
owner and is a measurement, not a regression test."""
from __future__ import annotations

import json
import re
import shlex
import shutil
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.aspect_extractor import AspectRecord
from nexus.aspect_worker import AspectExtractionWorker
from nexus.cli import main
from nexus.db.t2 import T2Database
from tests._catalog_fixture_ops import ActiveCatalog, documents_by_file_path
from tests.integration.test_catalog_owners_move_journey import _counts
from tests.integration.test_index_curator_move_hint import _hint_lines, _pdf
from tests.integration.test_rdr223_pdf_journey import _FakeChunker, _FakeExtractor

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

_PHRASE = "zorblax quenelle protocol of the moved pdf"
_OTHER = "an unrelated companion document about harbour tides"
_HANG_S = 120.0
_SEQ = [0]


class _PathExtractor(_FakeExtractor):
    """One page per line; the first file indexed is the phrase document, any other is the companion."""

    def extract(self, pdf_path, **kwargs):
        self.lines = [_OTHER, "second page of the companion"] if Path(pdf_path).name == "other.pdf" else [
            _PHRASE, "second page of the moved pdf"]
        return super().extract(pdf_path, **kwargs)


@pytest.fixture(autouse=True)
def _env(monkeypatch, t2_service_env):
    for mod in ("nexus.doc_indexer", "nexus.pipeline_stages"):
        monkeypatch.setattr(f"{mod}.PDFExtractor", type("Extractor", (_PathExtractor,), {"calls": []}))
        monkeypatch.setattr(f"{mod}.PDFChunker", _FakeChunker)
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)


def _wait(pred, what: str, timeout: float = _HANG_S) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError(f"hang bound ({timeout}s) reached waiting for: {what}")


class _Extractor:
    """The faked aspect extractor: answers ``extracted:<path>`` for every task, and holds the task of one path (until
    ``release`` is set) after saying it is extracting (``entered``). Every call is recorded."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.hold: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def install(self, monkeypatch) -> None:
        monkeypatch.setattr("nexus.aspect_worker._extract_aspects", self.single)
        monkeypatch.setattr("nexus.aspect_worker._extract_aspects_batch",
                            lambda items, **kw: [self.single("", s, c, doc_id_lookup=None) for (c, s, *_r) in items])

    def single(self, content, source_path, collection, doc_id_lookup=None, **kw):
        doc_id = doc_id_lookup(collection, source_path) if doc_id_lookup else ""
        self.calls.append((source_path, doc_id))
        if source_path == self.hold:
            self.entered.set()
            assert self.release.wait(_HANG_S), "the held extraction was never released"
        return AspectRecord(
            collection=collection, source_path=source_path, problem_formulation=f"extracted:{source_path}",
            proposed_method="M", experimental_datasets=["d"], experimental_baselines=["b"],
            experimental_results="R", extras={}, confidence=0.9,
            extracted_at="2026-11-20T00:00:00+00:00", model_version="test", extractor_name="test",
        )


class _Worker:
    """An aspect worker on its own thread; ``settle`` waits until the ``aspects`` queue holds no live task."""

    def __init__(self, db: T2Database) -> None:
        self.db = db
        self.worker = AspectExtractionWorker(
            poll_interval=0.5, batch_size=1,
            tuple_options={"lease_s": 60, "park_timeout_s": 1, "renew_every_s": 5, "deadline_s": lambda n: 60.0},
        )

    def live(self) -> int:
        s = self.db.tuples.subspace_stats("aspects")
        return s.available + s.claimed

    def __enter__(self) -> _Worker:
        assert self.live() > 0, "non-vacuity: the indexing run queued aspect extraction"
        self.worker.start()
        return self

    def settle(self) -> None:
        _wait(lambda: self.live() == 0, "the aspects queue to drain")

    def __exit__(self, *exc) -> None:
        self.worker.stop(timeout=30)


def _index(path: Path, collection: str):
    return CliRunner().invoke(main, ["index", "pdf", str(path), "--collection", collection, "--streaming", "never"])


def _cli(*args: str):
    return CliRunner().invoke(main, list(args))


def _links_from(tumbler: str) -> set[tuple[str, str]]:
    res = _cli("catalog", "links", "--from", tumbler, "--json")
    assert res.exit_code == 0, res.output
    payload = res.output[res.output.index("["):]
    return {(row["to"], row["type"]) for row in json.loads(payload)}


def _search_hits(collection: str, phrase: str) -> list[str]:
    """The ``path:page:text`` lines of ``nx search`` whose text is the phrase."""
    res = _cli("search", phrase, "--corpus", collection, "--n", "10")
    assert res.exit_code == 0, res.output
    return [ln for ln in res.output.splitlines() if ln.endswith(phrase)]


@pytest.mark.parametrize("in_flight", [False, True], ids=["extraction-drained-before-the-move", "extraction-in-flight-across-the-move"])
def test_gh1571_moved_pdf_keeps_its_tumbler_its_link_and_one_search_hit(tmp_path, monkeypatch, in_flight) -> None:
    _SEQ[0] += 1
    subject = f"mvv254-{_SEQ[0]}"
    collection = f"knowledge__{subject}"
    extractor = _Extractor()
    extractor.install(monkeypatch)

    # 1. nx index pdf; note the tumbler
    old = _pdf(tmp_path / "a", name="x.pdf")
    other = _pdf(tmp_path / "other", body=b"%PDF-1.4 the companion\n%%EOF\n", name="other.pdf")
    first = _index(old, collection)
    assert first.exit_code == 0, first.output
    assert _hint_lines(first) == [], "the first registration of a file hints nothing"
    assert _index(other, collection).exit_code == 0
    (old_row,) = documents_by_file_path(str(old))
    (other_row,) = documents_by_file_path(str(other))
    old_t, other_t = str(old_row.tumbler), str(other_row.tumbler)
    physical = old_row.physical_collection
    assert _search_hits(collection, _PHRASE) == [f"{old}:1:{_PHRASE}"], "non-vacuity: the phrase is searchable at the old path"

    # settle the old document's extraction now, so the move has an aspect row to re-key
    with T2Database(tmp_path / "unused.db") as db:
        with _Worker(db) as worker:
            worker.settle()
        seeded = db.document_aspects.get_by_doc_id(old_t)
        assert seeded is not None and seeded.source_path == str(old), "non-vacuity: the old document has an aspect row"

        # 2. link it to another document
        linked = _cli("catalog", "link", other_t, old_t, "--type", "cites")
        assert linked.exit_code == 0, linked.output
        assert (old_t, "cites") in _links_from(other_t)

        # 3. move the file; nx index pdf at the new path
        new = tmp_path / "b" / "x.pdf"
        new.parent.mkdir()
        shutil.move(str(old), str(new))
        new = new.resolve()
        second = _index(new, collection)
        assert second.exit_code == 0, second.output
        (new_row,) = documents_by_file_path(str(new))
        new_t = str(new_row.tumbler)
        assert new_t != old_t, "GH #1571: the re-index at the new path registered a second document"

        # 4. the hint the run printed, run exactly as printed (the dry run)
        (hint,) = _hint_lines(second)
        assert second.output.rstrip("\n").splitlines()[-1] == hint, "the hint is the last line"
        assert "--no-dry-run" not in hint and "--confirm" not in hint
        argv = shlex.split(hint)[1:]
        assert argv[:4] == ["catalog", "owners", "--execute", "move"] and old_t in argv and str(new) in argv

        # the new document's extraction is either drained before the move, or claimed and held IN FLIGHT across it
        # (the P5 MVV aspect-queue race)
        extractor.hold = str(new) if in_flight else None
        before = (ActiveCatalog().resolve(old_t).to_dict(), ActiveCatalog().resolve(new_t).to_dict())
        with _Worker(db) as worker:
            if in_flight:
                _wait(extractor.entered.is_set, "the worker to claim the new document's task")
            else:
                worker.settle()
                assert (str(new), new_t) in extractor.calls, "non-vacuity: the new document's extraction ran"
            dry = _cli(*argv)
            if "does not support catalog moves" in dry.output:
                pytest.skip("the substrate engine predates the move routes: rebuild the gate jar")
            assert dry.exit_code == 0, dry.output
            assert "(dry run)" in dry.output and "no catalog writes performed" in dry.output
            dry_counts = _counts(dry.output)
            assert dry_counts["repointed"] == 1 and dry_counts["absorbed (trashed)"] == 1, dry_counts
            assert dry_counts == {
                "repointed": 1, "absorbed (trashed)": 1, "skipped": 0, "unchanged": 0,
                # in flight, the absorbed document has no aspect row yet: the old row is re-keyed to the new path.
                # Drained, it has one at the new path: that row is re-keyed to the old tumbler, the old path's deleted.
                "aspect rows rewritten": 1, "aspect rows deleted": 0 if in_flight else 1,
                "highlight rows rewritten": 0, "highlight rows deleted": 0,
                "links remapped": 0, "links collapsed": 0, "links dropped": 0}, dry_counts
            assert re.findall(r"^    (\d+\.\d+\.\d+)$", dry.output, re.M) == [new_t], "the absorbed tumbler"
            assert (ActiveCatalog().resolve(old_t).to_dict(), ActiveCatalog().resolve(new_t).to_dict()) == before, (
                "the dry run writes nothing")

            # 5. rerun it with --no-dry-run --confirm, while the absorbed document's extraction is still in flight
            done = _cli(*argv, "--no-dry-run", "--confirm")
            assert done.exit_code == 0, done.output
            assert "(write)" in done.output and "Done." in done.output
            assert _counts(done.output) == dry_counts, "the dry run reports the counts the write reports"
            assert re.findall(r"^    (\d+\.\d+\.\d+)$", done.output, re.M) == [new_t]

            # the held extraction (for the absorbed, now trashed, document) finishes after the move
            extractor.release.set()
            worker.settle()
        assert (str(new), new_t) in extractor.calls, "non-vacuity: the held extraction ran, for the absorbed document"

        # acceptance: one live document at the new path, carrying the OLD tumbler, with the link
        live = documents_by_file_path(str(new))
        assert [str(d.tumbler) for d in live] == [old_t]
        assert documents_by_file_path(str(old)) == [], "nothing is left live at the old path"
        assert str(ActiveCatalog().by_source_uri("file://" + str(new)).tumbler) == old_t
        assert (old_t, "cites") in _links_from(other_t), "the link is on the old tumbler"
        assert not any(to == new_t for to, _ in _links_from(other_t))
        assert _search_hits(collection, _PHRASE) == [f"{new}:1:{_PHRASE}"], "exactly one hit, at the new path"

        # the aspect row: the old tumbler's, at the new path, never the absorbed document's
        def aspect_row_is_the_old_tumblers() -> None:
            row = db.document_aspects.get_by_doc_id(old_t)
            assert row is not None, (
                "the old tumbler lost its aspect row: the row at the new path now names "
                f"{db.document_aspects.get(physical, str(new)).doc_id}")
            assert row.source_path == str(new) and row.collection == physical
            assert db.document_aspects.get(physical, str(old)) is None, "no stale aspect row at the old path"
            assert db.document_aspects.get_by_doc_id(new_t) is None, "no aspect row names the absorbed tumbler"

        # in flight, the absorbed document's late extraction completes after the move: the engine keeps the old
        # tumbler on the row (AspectDocIdAttribution, the RDR-254 aspect-queue race fix)
        aspect_row_is_the_old_tumblers()
