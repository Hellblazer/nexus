# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-192 Step 13 (nexus-wbfpw.25): ``store_put`` reports the chunks its re-put superseded.

The engine's ``sweep_detail`` entry for a note's own write names the chashes the sweep DELETED
(``swept_chashes``, capped, with ``swept_chashes_truncated``). The note writer carries them to
``NoteWriteResult``; MCP ``store_put`` and ``nx store put`` add a second line,
``Superseded: N chunk(s) removed: [<chash>, ...]``, only when the sweep removed something, so a first
put and an identical re-put read exactly as before. A chunk the sweep kept (another document owns it)
is not reported: the line says what was removed, not what the manifest dropped.

An engine that predates the field answers with counts only; the line then carries the count and no
chashes. The first three tests run the real engine substrate.
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.catalog.multi_batch_write import write_one_request
from nexus.catalog.note_write import (
    STORED,
    NoteWriteResult,
    PutNoteOutcome,
    superseded_line,
)
from nexus.cli import main
from nexus.corpus import t3_collection_name
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp.core import store_put


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(result: str) -> list[str]:
    return result.split("\n")


# ── real engine ───────────────────────────────────────────────────────────────


def test_a_changed_reput_names_the_chunk_it_removed_and_a_first_or_identical_put_do_not(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject, title = "wbfpw25-mcp", "wbfpw25-note"
    v1 = "wbfpw25 version one of the note"
    v2 = "wbfpw25 version TWO of the note, different text"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        first = store_put(content=v1, collection=subject, title=title)
        same = store_put(content=v1, collection=subject, title=title)
        changed = store_put(content=v2, collection=subject, title=title)

    col = t3_collection_name(subject, t3=client)
    assert first == f"Stored: {_chash(v1)} -> {col}", "a first put reads exactly as before"
    assert same == first, "an identical re-put drops nothing and reports nothing"
    lines = _lines(changed)
    assert lines[0] == f"Stored: {_chash(v2)} -> {col}"
    assert lines[1] == f"Superseded: 1 chunk removed: [{_chash(v1)}]"
    assert len(lines) == 2


def test_a_chunk_another_document_owns_is_kept_and_so_not_reported(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject = "wbfpw25-shared"
    shared = "wbfpw25 text two documents share"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=shared, collection=subject, title="owner-a")
        store_put(content=shared, collection=subject, title="owner-b")
        result = store_put(content="wbfpw25 owner-a now says something else", collection=subject, title="owner-a")

    assert "Superseded" not in result, (
        "owner-b still owns the shared chunk, the sweep kept it, and the line reports removals only")
    assert result.startswith("Stored: ")


def test_cli_store_put_prints_the_same_superseded_line(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    subject, title = "wbfpw25-cli", "wbfpw25-cli-note"
    v1, v2 = "wbfpw25 cli version one", "wbfpw25 cli version two, different"

    def put(body: str):
        f = tmp_path / "note.md"
        f.write_text(body)
        with patch("nexus.commands.store._t3", return_value=client):
            return CliRunner().invoke(main, ["store", "put", str(f), "-c", subject, "-t", title])

    first, changed = put(v1), None
    assert first.exit_code == 0, first.output
    assert "Superseded" not in first.output
    changed = put(v2)
    assert changed.exit_code == 0, changed.output
    assert f"Superseded: 1 chunk removed: [{_chash(v1)}]" in changed.output


# ── the wording, and an engine that predates the field ───────────────────────


def _outcome(**write_kw) -> PutNoteOutcome:
    return PutNoteOutcome(
        status=STORED, collection="knowledge__x__bge-base-en-v15-768__v1",
        write=NoteWriteResult(catalog_doc_id="1.1.1", collection="c", **write_kw))


def test_no_line_when_nothing_was_removed_or_the_write_is_unknown():
    assert superseded_line(_outcome(swept=0, swept_chashes=[])) is None
    assert superseded_line(_outcome(swept=0)) is None
    assert superseded_line(PutNoteOutcome(status=STORED, collection="c")) is None


def test_an_engine_without_the_field_degrades_to_the_count():
    assert superseded_line(_outcome(swept=2, swept_chashes=None)) == "Superseded: 2 chunks removed"
    assert superseded_line(_outcome(swept=1, swept_chashes=None)) == "Superseded: 1 chunk removed"


def test_the_text_line_lists_at_most_three_chashes_and_counts_the_rest():
    ids = [_chash(f"c{i}") for i in range(5)]
    line = superseded_line(_outcome(swept=5, swept_chashes=ids))
    assert line == f"Superseded: 5 chunks removed: [{', '.join(ids[:3])}] (and 2 more)"


def test_a_capped_engine_list_still_reports_the_exact_count():
    ids = [_chash(f"c{i}") for i in range(300)]
    line = superseded_line(_outcome(swept=450, swept_chashes=ids, swept_chashes_truncated=True))
    assert line is not None and line.startswith("Superseded: 450 chunks removed: [") and line.endswith("(and 447 more)")


class _FakeCat:
    def __init__(self, entry: dict) -> None:
        self.entry = entry

    def write_manifest_many(self, docs, **kwargs):
        doc = docs[0][0]
        return {
            "failed_doc_ids": [], "complete_refused": [], "complete_refused_count": 0,
            "swept": self.entry.get("swept", 0), "sweep_skipped": 0, "sweep_detail": [{"doc_id": doc, **self.entry}],
            "dropped_chashes": {doc: []}, "dropped_count": {doc: 0}, "dropped_unknown": [],
            "chunks_written": 0,
        }


def _one_request(entry: dict):
    return write_one_request(
        _FakeCat(entry), doc_id="1.1.1", collection="c", rows=[], chunks=[], dropped="optional")


def test_write_one_request_reads_swept_chashes_from_the_documents_sweep_detail_entry():
    out = _one_request({"swept": 1, "swept_chashes": ["a" * 64], "swept_chashes_truncated": False})
    assert out.swept_chashes == ["a" * 64] and out.swept_chashes_truncated is False
    capped = _one_request({"swept": 9, "swept_chashes": ["a" * 64], "swept_chashes_truncated": True})
    assert capped.swept_chashes_truncated is True


def test_write_one_request_leaves_swept_chashes_unknown_on_an_old_engine():
    out = _one_request({"swept": 1})
    assert out.swept_chashes is None and out.swept == 1


@pytest.mark.parametrize("bad", ["not-a-list", [1, 2], None])
def test_a_malformed_swept_chashes_is_treated_as_absent(bad):
    out = _one_request({"swept": 1, "swept_chashes": bad})
    assert out.swept_chashes is None


# ── a sweep that did not finish (fix round 2, critique S1) ───────────────────
#
# The engine fails a note's sweep open: the manifest write commits, the sweep errors (gate_timeout,
# statement_timeout, sweep_failed, before_read_failed), ``sweep_skipped`` counts it and the old chunk
# stays in T3, hidden by live(c) until the reaper collects it. ``Stored:`` alone would read exactly
# like a put that superseded nothing, so the result carries one line saying the sweep did not
# finish. The ENGINE half (a real gate timeout, statement timeout and permission failure, each
# answering with the errored ``sweep_detail`` entry) is forced in CatalogManifestSweepRepositoryTest;
# the tests below take the client side from that shape. A real engine can be made to fail a sweep only
# by holding its advisory gate or revoking a grant on the shared session substrate, which would leak
# into every other test, so the failure is injected at the catalog writer: the real engine runs the
# write with ``sweep=False`` (the old chunk really stays) and the response is rewritten to the shape the
# engine answers with when the sweep errors.

_NOT_FINISHED = "Superseded: the sweep did not finish"


class _SweepFails:
    """A catalog writer that runs the real write WITHOUT the sweep and answers as the engine does for a
    sweep that errored: ``swept`` 0, ``sweep_skipped`` 1, an errored ``sweep_detail`` entry."""

    def __init__(self, real, reason: str = "gate_timeout") -> None:
        self._real, self._reason = real, reason

    def write_manifest_many(self, docs, *args, **kwargs):
        resp = dict(self._real.write_manifest_many(docs, *args, **{**kwargs, "sweep": False}))
        doc = docs[0][0]
        n = len((resp.get("dropped_chashes") or {}).get(doc) or [])
        resp.update(swept=0, sweep_skipped=1, sweep_detail=[{
            "doc_id": doc, "dropped": n, "swept": 0, "kept": n, "errored": True,
            "reason": self._reason, "swept_chashes": [], "swept_chashes_truncated": False}])
        return resp

    def __getattr__(self, name):
        return getattr(self._real, name)


def _failing_sweep_writer(reason: str = "gate_timeout"):
    from nexus.catalog.factory import make_catalog_writer as real_factory

    return lambda **kw: _SweepFails(real_factory(**kw), reason)


def test_a_failed_sweep_is_reported_not_left_to_the_log(t2_service_env):
    client = HttpVectorClient(tenant=t2_service_env)
    subject, title = "wbfpw25-failed", "wbfpw25-failed-note"
    v1 = "wbfpw25 failed-sweep version one"
    v2 = "wbfpw25 failed-sweep version TWO, different text"
    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=v1, collection=subject, title=title)
        with patch("nexus.catalog.factory.make_catalog_writer", _failing_sweep_writer()):
            changed = store_put(content=v2, collection=subject, title=title)

    col = t3_collection_name(subject, t3=client)
    lines = _lines(changed)
    assert lines[0] == f"Stored: {_chash(v2)} -> {col}"
    assert lines[1].startswith(_NOT_FINISHED), changed
    assert "up to 1 replaced chunk " in lines[1], "the count is the manifest's drop list, an upper bound"
    assert "not removed" in lines[1] and "engine reaper" in lines[1]
    assert "removed:" not in lines[1], "the line must not claim a removal that did not happen"
    assert len(lines) == 2


def test_cli_store_put_reports_a_failed_sweep_too(t2_service_env, tmp_path):
    client = HttpVectorClient(tenant=t2_service_env)
    subject, title = "wbfpw25-cli-failed", "wbfpw25-cli-failed-note"

    def put(body: str, *, failing: bool):
        f = tmp_path / "note.md"
        f.write_text(body)
        writer = _failing_sweep_writer() if failing else None
        with patch("nexus.commands.store._t3", return_value=client):
            if writer is None:
                return CliRunner().invoke(main, ["store", "put", str(f), "-c", subject, "-t", title])
            with patch("nexus.catalog.factory.make_catalog_writer", writer):
                return CliRunner().invoke(main, ["store", "put", str(f), "-c", subject, "-t", title])

    assert put("wbfpw25 cli failed one", failing=False).exit_code == 0
    changed = put("wbfpw25 cli failed two, different", failing=True)
    assert changed.exit_code == 0, changed.output
    assert _NOT_FINISHED in changed.output
    assert "removed:" not in changed.output


class _SweepCat:
    """A writer that answers every write with the given sweep counts and drop list (no engine)."""

    def __init__(self, *, swept: int, sweep_skipped: int, dropped: list[str] | None) -> None:
        self._swept, self._sweep_skipped, self._dropped = swept, sweep_skipped, dropped

    def write_manifest_many(self, docs, **kwargs):
        doc = docs[0][0]
        resp = {"failed_doc_ids": [], "chunks_written": 1, "swept": self._swept,
                "sweep_skipped": self._sweep_skipped}
        if self._dropped is None:
            resp["dropped_unknown"] = [doc]
        else:
            resp.update(dropped_chashes={doc: list(self._dropped)}, dropped_count={doc: len(self._dropped)})
        return resp

    def begin_index_run(self, doc_id, content_hash, run_id, collection, **kw):
        return {"prior_chashes": [], "prior_count": 0}

    def complete_index_run(self, doc_id, content_hash, count):
        return {}

    def fail_index_run(self, doc_id, error):
        return {}

    def close(self):
        pass


def _put_through(cat: _SweepCat, label: str) -> PutNoteOutcome:
    from nexus.catalog.note_write import put_note

    return put_note(content=f"wbfpw25 {label}", collection="knowledge__wbfpw25seam__bge-base-en-v15-768__v1",
                    title=f"wbfpw25-{label}", cat=cat)


def test_the_failed_sweep_line_counts_what_the_manifest_dropped_as_an_upper_bound(t2_service_env):
    two = _put_through(_SweepCat(swept=0, sweep_skipped=1, dropped=["a" * 64, "b" * 64]), "two")
    assert superseded_line(two) == (
        f"{_NOT_FINISHED}; up to 2 replaced chunks were not removed. Those no other document owns "
        "are hidden from search; the engine reaper collects them later.")
    one = _put_through(_SweepCat(swept=0, sweep_skipped=1, dropped=["a" * 64]), "one")
    assert "up to 1 replaced chunk was not removed" in superseded_line(one)


def test_a_failed_sweep_whose_drop_list_is_unknown_names_no_count(t2_service_env):
    """before_read_failed: the engine could not read the previous manifest, so there is no drop list."""
    unknown = _put_through(_SweepCat(swept=0, sweep_skipped=1, dropped=None), "unknown")
    assert superseded_line(unknown) == (
        f"{_NOT_FINISHED}; the replaced chunks were not removed. Those no other document owns "
        "are hidden from search; the engine reaper collects them later.")


def test_a_clean_sweep_and_a_nothing_dropped_put_say_nothing_about_failure(t2_service_env):
    assert superseded_line(_put_through(_SweepCat(swept=0, sweep_skipped=0, dropped=[]), "none")) is None
    assert superseded_line(_put_through(_SweepCat(swept=0, sweep_skipped=1, dropped=[]), "none-skipped")) is None, (
        "a drop list KNOWN to be empty leaves no chunk behind, so a skipped sweep has nothing to report")
    removed = superseded_line(_put_through(_SweepCat(swept=1, sweep_skipped=0, dropped=["a" * 64]), "ok"))
    assert removed == "Superseded: 1 chunk removed"


# ── a lost acknowledgement (fix round 2, code review) ────────────────────────


class _LoseTheAnswerOnce:
    """Runs the real write, then raises a 504 the first time: the engine committed AND swept, only the
    answer was lost."""

    def __init__(self, real) -> None:
        self._real, self.fired = real, False

    def write_manifest_many(self, docs, *args, **kwargs):
        out = self._real.write_manifest_many(docs, *args, **kwargs)
        if not self.fired:
            self.fired = True
            import httpx

            request = httpx.Request("POST", "http://engine.invalid/v1/catalog/manifest/write_many")
            raise httpx.HTTPStatusError(
                "HTTP 504", request=request, response=httpx.Response(504, request=request))
        return out

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_lost_ack_resend_prints_no_superseded_line_even_though_the_first_attempt_swept(
        t2_service_env, monkeypatch):
    """The first attempt committed and removed the old chunk; its answer was lost; the resend sweeps
    nothing. The result is ``recovered``, its ``swept_chashes`` is unknown, and no line is printed:
    the report cannot name what it never saw, and an empty line would claim nothing was removed."""
    from nexus.catalog.factory import make_catalog_writer as real_factory
    from nexus.rate_brake import reset_brake

    monkeypatch.setattr("nexus.retry.time.sleep", lambda seconds: None)
    reset_brake()
    client = HttpVectorClient(tenant=t2_service_env)
    subject, title = "wbfpw25-lostack", "wbfpw25-lostack-note"
    v1 = "wbfpw25 lost-ack version one"
    v2 = "wbfpw25 lost-ack version TWO, different text"
    losers: list[_LoseTheAnswerOnce] = []

    def factory(**kw):
        w = _LoseTheAnswerOnce(real_factory(**kw))
        losers.append(w)
        return w

    with patch("nexus.mcp.core._get_t3", return_value=client):
        store_put(content=v1, collection=subject, title=title)
        with patch("nexus.catalog.factory.make_catalog_writer", factory):
            changed = store_put(content=v2, collection=subject, title=title)
    reset_brake()

    col = t3_collection_name(subject, t3=client)
    assert any(w.fired for w in losers), "the lost answer was never injected: the test proved nothing"
    assert changed == f"Stored: {_chash(v2)} -> {col}", changed
