# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8o06q: the manifest heal never writes into a document that has manifest rows.

``heal_manifest_gaps`` finds a document's missing chunks by ``content_hash`` on the T3 chunk rows.
A T3 chunk row is shared by every document that holds that text and its ``content_hash`` is the
LAST writer's, so a chunk a document shares with a document that wrote it last carries the other
document's hash and is not fetched. The old heal replaced the manifest with exactly the rows it
fetched, which deleted the document's own correct row for that shared chunk, booked the shortfall
as a benign duplicate collapse, and lowered ``chunk_count`` so the gap never showed again. The
first fix appended the fetched rows after the highest position instead, which put a chunk missing
from the middle at the tail, matched the count, and left a complete fence over a wrong manifest.

Now a gapped document that has rows is left as it is and its RUNFENCE ``index_state`` is failed, so
the next plain ``nx index repo`` re-indexes it (``test_the_next_index_run_re_indexes_a_marked_document``
drives that for real). A manifest-less document is rebuilt by append; short of ``chunk_count`` it is
marked too; a document whose fence is ``'indexing'`` is skipped.

These tests drive the real heal against the engine substrate (the same shape as
``test_wbfpw29_manifest_hook_exception_index_run.py``).
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.manifest_heal import heal_manifest_gaps
from nexus.cli import main
from nexus.db import make_t3
from nexus.repo_identity import _repo_identity
from tests._chunk_seed import seed_chunks_direct
from tests.conftest import fake_credentials

pytestmark = [pytest.mark.integration]


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _meta(content_hash: str, chash: str, start: int) -> dict:
    return {
        "content_hash": content_hash,
        "chunk_text_hash": chash,
        "chunk_start_char": start, "chunk_end_char": start + 10,
        "line_start": 0, "line_end": 0,
    }


def _row(chash: str, position: int, title: str = "") -> dict:
    # The three label keys are always sent, as ``_manifest_chunk_rows`` sends them: their presence
    # is what marks the row's labels as this document's own (labels_written).
    return {
        "chash": chash, "position": position, "chunk_index": position,
        "section_title": title, "section_type": "", "page_number": None,
    }


def _entry(reader, tumbler: str):
    (entry,) = [e for e in reader.all_documents() if str(e.tumbler) == tumbler]
    return entry


def _state(reader, tumbler: str):
    return reader.by_doc_id(tumbler).index_state


def _positions(reader, tumbler: str) -> dict[str, int]:
    return {r.chash: r.position for r in reader.get_manifest(tumbler)}


def _heal(reader, entries: list, **kw):
    return heal_manifest_gaps(entries, reader, make_t3, make_catalog_writer, **kw)


class _Doc:
    """One document ``A`` with its chunks seeded in T3.

    ``rows`` maps chunk name -> manifest position for the rows A's manifest holds (none for a
    manifest-less document); ``chunk_count`` is A's own count. Every chunk named in ``chunks``
    carries A's file hash unless listed in ``other_hash`` (a chunk another document wrote last).
    """

    def __init__(
        self, tag: str, chunks: list[str], rows: dict[str, int], chunk_count: int,
        other_hash: tuple[str, ...] = (),
    ) -> None:
        self.collection = f"docs__{tag}__bge-base-en-v15-768__v1"
        self.h_a, self.h_b = _chash(f"{tag}-file-a"), _chash(f"{tag}-file-b")
        self.ch = {n: _chash(f"{tag}-{n}") for n in chunks}
        with HttpCatalogClient() as cat:
            owner = cat.register_owner(f"{tag}-owner", "repo", repo_hash=f"{tag}-hash")
            self.a = str(cat.register(
                owner, "Doc A", content_type="pdf", physical_collection=self.collection,
                chunk_count=chunk_count, meta={"content_hash": self.h_a},
            ))
            self.b = str(cat.register(
                owner, "Doc B", content_type="pdf", physical_collection=self.collection,
                chunk_count=len(other_hash), meta={"content_hash": self.h_b},
            ))
        seed_chunks_direct(
            self.collection,
            ids=[self.ch[n] for n in chunks],
            documents=[f"{tag} {n} text" for n in chunks],
            embed=True,
            metadatas=[
                _meta(self.h_b if n in other_hash else self.h_a, self.ch[n], 10 * i)
                for i, n in enumerate(chunks)
            ],
        )
        self.reader = make_catalog_reader()
        self.writer = make_catalog_writer()
        if rows:
            self.writer.write_manifest(
                self.a, [_row(self.ch[n], pos, f"{n}-title") for n, pos in rows.items()],
                collection=self.collection)
            # write_manifest folds chunk_count down to the rows it wrote; A's real count is the
            # caller's.
            self.writer.update(self.a, chunk_count=chunk_count)
        if other_hash:
            self.writer.write_manifest(
                self.b, [_row(self.ch[n], i) for i, n in enumerate(other_hash)],
                collection=self.collection)
            self.writer.update(self.b, chunk_count=len(other_hash))

    def entry(self):
        return _entry(self.reader, self.a)


# -- documents that have manifest rows: never written, marked for re-index ----------------------


def test_a_shared_chunk_row_another_document_wrote_last_survives_and_the_document_is_marked() -> None:
    """The original pin. A is partly manifested (S@0, P@1; Q missing; chunk_count 3) and shares S
    with B, whose hash S carries. After the heal A holds exactly the rows it held, with S's label,
    its chunk_count is still 3, and it is marked for re-index. The old heal rebuilt A's manifest
    from P and Q alone, dropping S, and lowered chunk_count."""
    d = _Doc("8o06q-shared", ["s", "p", "q"], {"s": 0, "p": 1}, 3, other_hash=("s",))

    result = _heal(d.reader, [d.entry(), _entry(d.reader, d.b)])

    rows = {r.chash: r for r in d.reader.get_manifest(d.a)}
    assert set(rows) == {d.ch["s"], d.ch["p"]}, "no row dropped, none added"
    # 7.86.3 carries no tko2w (per-document labels ship with engine 0.1.165), so ManifestRow has
    # no section_title / labels_written here; develop keeps those two assertions.
    assert rows[d.ch["s"]].position == 0
    assert d.entry().chunk_count == 3
    assert _state(d.reader, d.a) == "failed"
    assert [(str(r.entry.tumbler), r.manifest_rows, r.chunk_count) for r in result.reindex] == [(d.a, 2, 3)]
    assert result.reconciled == 0 and result.lost == []
    # B's manifest and fence are untouched.
    assert [r.chash for r in d.reader.get_manifest(d.b)] == [d.ch["s"]]


def test_a_chunk_missing_from_the_middle_is_not_appended_at_the_tail() -> None:
    """The mid-document gap. A's manifest holds P@0 and R@2, Q is missing, chunk_count is 3, and T3
    still holds Q with A's hash. The first fix appended Q after the highest position (3), which
    made rows == chunk_count, put Q at the wrong position and left the fence complete. The heal now
    writes nothing and marks the document."""
    d = _Doc("8o06q-mid", ["p", "q", "r"], {"p": 0, "r": 2}, 3)

    result = _heal(d.reader, [d.entry()])

    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["r"]: 2}, "nothing written into A"
    assert d.entry().chunk_count == 3
    assert _state(d.reader, d.a) == "failed"
    assert [(r.manifest_rows, r.chunk_count) for r in result.reindex] == [(2, 3)]
    assert result.reconciled == 0 and result.gapped == 1


def test_a_marked_document_stays_listed_until_something_re_indexes_it() -> None:
    d = _Doc("8o06q-again", ["p", "q", "r"], {"p": 0, "r": 2}, 3)
    _heal(d.reader, [d.entry()])

    second = _heal(d.reader, [d.entry()])

    assert second.gapped == 1 and len(second.reindex) == 1
    assert _state(d.reader, d.a) == "failed"
    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["r"]: 2}


def test_a_gapped_document_whose_fence_is_indexing_is_skipped() -> None:
    """A run may be writing it now: no fence write, no rows."""
    d = _Doc("8o06q-busy", ["p", "q", "r"], {"p": 0, "r": 2}, 3)
    d.writer.begin_index_run(d.a, d.h_a, "run-8o06q-busy", d.collection)

    result = _heal(d.reader, [d.entry()])

    assert _state(d.reader, d.a) == "indexing"
    assert result.reindex == [] and len(result.skipped_indexing) == 1
    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["r"]: 2}


def test_dry_run_reports_a_document_with_rows_and_changes_nothing() -> None:
    d = _Doc("8o06q-dry-rows", ["p", "q", "r"], {"p": 0, "r": 2}, 3)
    before = _state(d.reader, d.a)

    dry = _heal(d.reader, [d.entry()], dry_run=True)

    assert [(r.manifest_rows, r.chunk_count) for r in dry.reindex] == [(2, 3)]
    assert _state(d.reader, d.a) == before and before != "failed", "a dry run fails no fence"
    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["r"]: 2}
    # The real pass reports the same list.
    live = _heal(d.reader, [d.entry()])
    assert [(r.manifest_rows, r.chunk_count) for r in live.reindex] == [(2, 3)]
    assert live.gapped == dry.gapped and live.reconciled == dry.reconciled


def test_a_run_that_re_indexed_the_document_since_the_snapshot_is_not_failed() -> None:
    """The heal works from a snapshot of the whole pass. A: chunk_count 3, rows P@0 R@2 (gapped
    in the snapshot). Before the heal reaches it a run re-indexes A to two chunks and completes
    its fence: rows 2, chunk_count 2, state complete. Judged against the snapshot's count (3)
    the heal would fail a fence the run just completed and force a second re-index; judged
    against the document as it is now, it is not gapped and nothing is written."""
    d = _Doc("8o06q-stale-count", ["p", "q", "r"], {"p": 0, "r": 2}, 3)
    snapshot = d.entry()
    assert snapshot.chunk_count == 3

    d.writer.begin_index_run(d.a, d.h_a, "run-8o06q-stale-count", d.collection)
    d.writer.write_manifest(
        d.a, [_row(d.ch["p"], 0), _row(d.ch["r"], 1)], collection=d.collection)
    d.writer.complete_index_run(d.a, d.h_a, 2)
    assert _state(d.reader, d.a) == "complete" and d.entry().chunk_count == 2

    result = _heal(d.reader, [snapshot])

    assert _state(d.reader, d.a) == "complete", "a fence a run just completed is not failed"
    assert result.reindex == [] and result.skipped_indexing == []
    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["r"]: 1}


def test_a_run_that_began_since_the_snapshot_is_not_failed() -> None:
    """The snapshot says the fence is not 'indexing'; a run began after it. The heal re-reads the
    fence before it writes one, and leaves the document to the run."""
    d = _Doc("8o06q-stale-state", ["p", "q", "r"], {"p": 0, "r": 2}, 3)
    snapshot = d.entry()
    assert snapshot.index_state != "indexing"

    d.writer.begin_index_run(d.a, d.h_a, "run-8o06q-stale-state", d.collection)

    result = _heal(d.reader, [snapshot])

    assert _state(d.reader, d.a) == "indexing"
    assert result.reindex == [] and len(result.skipped_indexing) == 1
    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["r"]: 2}


# -- documents with no manifest rows: rebuilt by append ------------------------------------------


def test_a_manifestless_document_is_rebuilt_in_span_order_and_its_fence_is_untouched() -> None:
    d = _Doc("8o06q-whole", ["p", "q", "r"], {}, 3)
    before = _state(d.reader, d.a)

    result = _heal(d.reader, [d.entry()])

    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["q"]: 1, d.ch["r"]: 2}
    assert d.entry().chunk_count == 3, "the append folds chunk_count to the row count"
    assert _state(d.reader, d.a) == before
    assert result.reconciled == 1 and result.reindex == [] and result.lost == []


def test_a_manifestless_document_rebuilt_short_is_written_and_marked() -> None:
    """chunk_count 3, T3 holds two chunks with A's hash. The rows T3 has are written (they are
    otherwise hidden), the fence is failed so the next run re-indexes, and chunk_count is whatever
    the append folded it to (the row count), not put back by a second update."""
    d = _Doc("8o06q-short", ["p", "q"], {}, 3)

    result = _heal(d.reader, [d.entry()])

    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["q"]: 1}
    assert d.entry().chunk_count == 2
    assert _state(d.reader, d.a) == "failed"
    assert result.reconciled == 1
    assert [(r.manifest_rows, r.chunk_count) for r in result.reindex] == [(2, 3)]


def test_a_second_pass_over_a_rebuilt_short_document_does_not_list_it_again() -> None:
    """After the append folds chunk_count to the rows written, the document holds as many rows
    as its count, so it is no longer gapped: a second pass does not list it, writes nothing and
    leaves the fence failed. The failed fence, not a gap, is what sends the next index run to
    its source."""
    d = _Doc("8o06q-short-twice", ["p", "q"], {}, 3)
    _heal(d.reader, [d.entry()])
    assert d.entry().chunk_count == 2 and _state(d.reader, d.a) == "failed"

    second = _heal(d.reader, [d.entry()])

    assert second.gapped == 0 and second.reindex == [] and second.reconciled == 0
    assert _state(d.reader, d.a) == "failed"
    assert d.entry().chunk_count == 2
    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["q"]: 1}


def test_a_manifestless_document_with_more_rows_than_chunk_count_is_not_written() -> None:
    """Rows of an older chunker run of the same file carry the same hash: three T3 rows for a
    document that holds two. Writing them would fill a position the document does not have."""
    d = _Doc("8o06q-over", ["p", "q", "stale"], {}, 2)

    result = _heal(d.reader, [d.entry()])

    assert d.reader.get_manifest(d.a) == []
    assert d.entry().chunk_count == 2
    assert _state(d.reader, d.a) == "failed"
    assert result.reconciled == 0
    assert [(r.manifest_rows, r.chunk_count) for r in result.reindex] == [(0, 2)]


def test_a_manifestless_document_whose_fence_is_indexing_is_skipped() -> None:
    d = _Doc("8o06q-busy-empty", ["p", "q"], {}, 2)
    d.writer.begin_index_run(d.a, d.h_a, "run-8o06q-busy-empty", d.collection)

    result = _heal(d.reader, [d.entry()])

    assert d.reader.get_manifest(d.a) == []
    assert _state(d.reader, d.a) == "indexing"
    assert result.reconciled == 0 and len(result.skipped_indexing) == 1


def test_dry_run_reports_a_short_manifestless_document_and_changes_nothing() -> None:
    d = _Doc("8o06q-dry-short", ["p", "q"], {}, 3)
    before = _state(d.reader, d.a)

    dry = _heal(d.reader, [d.entry()], dry_run=True)

    assert d.reader.get_manifest(d.a) == []
    assert d.entry().chunk_count == 3
    assert _state(d.reader, d.a) == before and before != "failed"
    live = _heal(d.reader, [d.entry()])
    assert dry.reconciled == live.reconciled == 1
    assert ([(r.manifest_rows, r.chunk_count) for r in dry.reindex]
            == [(r.manifest_rows, r.chunk_count) for r in live.reindex] == [(2, 3)])


# -- the claim: the next plain index run re-indexes a marked document -----------------------------


def _git_init(repo: Path) -> None:
    for cmd in (
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "test@nexus"],
        ["git", "config", "user.name", "Nexus Test"],
        ["git", "add", "."],
        ["git", "commit", "-m", "Initial commit"],
    ):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)


def test_the_next_index_run_re_indexes_a_marked_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index a repo, drop one MIDDLE manifest row of one document (and put its chunk_count back),
    then run ``nx index repo`` twice with no --force. The first run's self-heal marks the document
    and writes nothing into it; the second re-indexes it: the manifest holds every chunk at
    positions 0..N-1 again and the fence is complete. Before the fix the first run appended the
    dropped chunk at the tail (or lowered chunk_count) and no run repaired it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)

    repo = tmp_path / "reindex-repo"
    repo.mkdir()
    sections = "\n\n".join(
        f"## Section {i}\n\n" + " ".join(f"word{i}x{j}" for j in range(400)) for i in range(8)
    )
    (repo / "guide.md").write_text("# Guide\n\n" + sections + "\n")
    _git_init(repo)

    runner = CliRunner()

    def _run():
        with patch("nexus.config.get_credential", side_effect=fake_credentials()):
            return runner.invoke(main, ["index", "repo", str(repo)])

    first = _run()
    assert first.exit_code == 0, first.output

    reader = make_catalog_reader()
    _, repo_hash = _repo_identity(repo)
    owner = reader.owner_for_repo(repo_hash)
    (doc,) = [e for e in reader.by_owner(owner) if (e.file_path or "").endswith("guide.md")]
    tumbler = str(doc.tumbler)
    rows = sorted(reader.get_manifest(tumbler), key=lambda r: r.position)
    n = len(rows)
    assert n >= 3, f"the fixture must chunk into at least three chunks, got {n}"
    assert [r.position for r in rows] == list(range(n))

    writer = make_catalog_writer()
    middle = rows[1]
    kept = [
        _row(r.chash, r.position)
        for r in rows if r.chash != middle.chash
    ]
    writer.atomic_manifest_replace(tumbler, kept, collection=doc.physical_collection)
    writer.update(tumbler, chunk_count=n)
    assert len(reader.get_manifest(tumbler)) == n - 1

    second = _run()
    assert second.exit_code == 0, second.output
    assert "marked for re-index" in second.output, second.output
    assert _state(reader, tumbler) == "failed"
    assert len(reader.get_manifest(tumbler)) == n - 1, "the heal wrote nothing into the document"

    third = _run()
    assert third.exit_code == 0, third.output
    after = sorted(reader.get_manifest(tumbler), key=lambda r: r.position)
    assert [r.position for r in after] == list(range(n))
    assert [r.chash for r in after] == [r.chash for r in rows]
    assert _state(reader, tumbler) == "complete"


def test_reconcile_names_the_real_route_and_dry_run_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``nx catalog reconcile`` prints a REINDEX line for a gapped document with rows, says the
    route depends on how it was indexed (not only ``nx index repo``), and ``--dry-run`` says "would be marked" and fails no
    fence. Nothing here says the heal repaired or kept a repair route that does not exist."""
    d = _Doc("8o06q-cli", ["p", "q", "r"], {"p": 0, "r": 2}, 3)
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()

    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        dry = runner.invoke(main, ["catalog", "reconcile", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "would be marked for re-index" in dry.output, dry.output
    assert f"REINDEX {d.a}" in dry.output and "2 of 3 rows" in dry.output, dry.output
    assert _state(d.reader, d.a) != "failed"

    with patch("nexus.config.get_credential", side_effect=fake_credentials()):
        real = runner.invoke(main, ["catalog", "reconcile"])
    assert real.exit_code == 0, real.output
    assert (
        "marked for re-index; re-index each one's source: the next nx index repo for a repo file, "
        "nx index pdf / md / rdr for one indexed that way"
    ) in real.output, real.output
    assert f"REINDEX {d.a}" in real.output, real.output
    assert "re-index the source" not in real.output
    assert _state(d.reader, d.a) == "failed"
    assert _positions(d.reader, d.a) == {d.ch["p"]: 0, d.ch["r"]: 2}
