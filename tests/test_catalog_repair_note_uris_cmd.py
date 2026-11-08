# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog repair-note-uris`` (nexus-bivz3.24, RDR-231 critique Issue 7), client half.

The engine's ``POST /v1/catalog/note_uri_repair`` is pinned in Java (``NoteUriRepairTest``: exact census
counts, the three-table rewrite, the concurrent twin). These tests pin the client: the verb's dry-run
default, what it prints and returns, the apply loop, its refusals, and the wire shape of
``HttpCatalogClient.repair_stale_note_uris``. The writer is a fake, so no engine and no tenant is used;
the one test against a real engine is ``tests/test_catalog_repair_note_uris_engine.py``.
"""
from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.factory import _SERVICE_ONLY_WRITE_OPS, _ServiceCatalogWriter
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.cli import main

_Y = "knowledge__y__some-model__v1"
_X = "knowledge__x__some-model__v1"


def _census(**over) -> dict:
    base = {
        "apply": False, "stale_live": 6, "rewrite": 3, "merge": 2, "merge_twin": 1, "merge_sibling": 1,
        "refused": 1, "refused_file_routed_collection": 1, "refused_malformed": 0, "tombstoned": 4,
        "export_copies": 5, "export_copies_target_moved": 2,
        "sample": {
            "rewrite": [{"tumbler": "1.1.1", "collection": _Y, "source_uri": f"nx-note://{_X}/a",
                         "new_uri": f"nx-note://{_Y}/a"}],
            "merge": [{"tumbler": "1.1.7", "collection": _Y, "source_uri": f"nx-note://{_X}/m",
                       "new_uri": f"nx-note://{_Y}/m", "twin": "1.1.8", "reason": "twin"}],
            "refused": [{"tumbler": "1.1.9", "collection": "docs__d__some-model__v1",
                         "source_uri": f"nx-note://{_X}/odd", "reason": "file_routed_collection"}],
            "tombstoned": [{"tumbler": "1.1.5", "collection": _Y, "source_uri": f"nx-note://{_X}/dead"}],
            "export_target_moved": [{"tumbler": "1.1.6", "collection": _Y,
                                     "source_uri": f"nxexp://{_X}/nx-note://{_X}/e", "reason": "target_moved"}],
        },
    }
    base.update(over)
    return base


class _FakeWriter:
    """Answers ``repair_stale_note_uris`` from a script: one response per call, in order."""

    def __init__(self, responses: list[dict | Exception]):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def repair_stale_note_uris(self, *, apply: bool = False, limit: int = 1000, sample: int = 20) -> dict:
        self.calls.append({"apply": apply, "limit": limit, "sample": sample})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return dict(r, apply=apply)

    def merge_documents(self, *a, **k):  # the verb must never merge
        raise AssertionError("repair-note-uris must not merge")

    def update(self, *a, **k):
        raise AssertionError("repair-note-uris must not write through update")

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch, writer) -> None:
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)


def _status(code: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/v1/catalog/note_uri_repair")
    return httpx.HTTPStatusError(f"HTTP {code}", request=request,
                                 response=httpx.Response(code, request=request, text=body))


def _run(*args: str):
    return CliRunner().invoke(main, ["catalog", "repair-note-uris", *args])


class TestDryRunDefault:
    def test_one_census_call_and_nothing_applied(self, monkeypatch):
        w = _FakeWriter([_census()])
        _patch(monkeypatch, w)

        result = _run()

        assert result.exit_code == 0, result.output
        assert w.calls == [{"apply": False, "limit": 1000, "sample": 20}]
        assert w.closed

    def test_the_census_is_printed_with_every_class(self, monkeypatch):
        _patch(monkeypatch, _FakeWriter([_census()]))

        out = _run().output

        assert "stale note rows (live): 6" in out
        assert "rewrite candidates (no live twin): 3" in out
        assert "merge candidates: 2 (1 with a live twin, 1 rewriting onto one URI with another stale row)" in out
        assert "refused: 1" in out
        assert "tombstoned stale rows (left as they are): 4" in out
        assert "nxexp:// export copies: 5, 2 whose target collection is not the one they live in" in out
        assert "Dry run" in out and "--apply" in out

    def test_merge_candidates_are_listed_but_named_unmerged(self, monkeypatch):
        _patch(monkeypatch, _FakeWriter([_census()]))

        out = _run().output

        assert "1.1.7" in out and "twin 1.1.8" in out
        assert "merge" in out.lower() and "not merged" in out.lower()

    def test_json_is_the_engine_report_verbatim(self, monkeypatch):
        _patch(monkeypatch, _FakeWriter([_census()]))

        result = _run("--json")

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == _census()

    def test_sample_and_batch_size_are_forwarded(self, monkeypatch):
        w = _FakeWriter([_census()])
        _patch(monkeypatch, w)

        assert _run("--sample", "3", "--batch-size", "7").exit_code == 0

        assert w.calls == [{"apply": False, "limit": 7, "sample": 3}]


class TestApply:
    def test_censuses_then_rewrites_in_batches_then_censuses_again(self, monkeypatch):
        w = _FakeWriter([
            _census(),
            _census(rewritten=2, remaining=1),
            _census(rewritten=1, remaining=0),
            _census(rewrite=0, stale_live=3),
        ])
        _patch(monkeypatch, w)

        result = _run("--apply", "--batch-size", "2")

        assert result.exit_code == 0, result.output
        assert [c["apply"] for c in w.calls] == [False, True, True, False]
        assert all(c["limit"] == 2 for c in w.calls[1:3])
        assert "Rewrote 3 note URI(s) in 2 batch(es)" in result.output
        assert "rewrite candidates (no live twin): 0" in result.output
        assert w.closed

    def test_nothing_to_rewrite_makes_no_apply_call(self, monkeypatch):
        w = _FakeWriter([_census(rewrite=0, stale_live=3)])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert [c["apply"] for c in w.calls] == [False]
        assert "Nothing to rewrite" in result.output

    def test_apply_json_carries_before_after_and_the_total(self, monkeypatch):
        w = _FakeWriter([_census(), _census(rewritten=3, remaining=0), _census(rewrite=0)])
        _patch(monkeypatch, w)

        result = _run("--apply", "--json")

        assert result.exit_code == 0, result.output
        doc = json.loads(result.output)
        assert doc["rewritten"] == 3 and doc["batches"] == 1
        assert doc["before"]["rewrite"] == 3 and doc["after"]["rewrite"] == 0

    def test_a_batch_that_rewrites_nothing_while_rows_remain_stops_instead_of_spinning(self, monkeypatch):
        w = _FakeWriter([_census(), _census(rewritten=0, remaining=3), _census()])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        assert [c["apply"] for c in w.calls] == [False, True, False]
        assert "made no progress" in result.output

    def test_a_409_names_the_holder_and_stops_with_the_progress_so_far(self, monkeypatch):
        body = json.dumps({"error": "repairing the stale note URI of 1.1.2 would rewrite a document's "
                                    "source_uri to nx-note://k/a, which live document 1.1.99 in k already holds."})
        w = _FakeWriter([_census(), _census(rewritten=2, remaining=1), _status(409, body)])
        _patch(monkeypatch, w)

        result = _run("--apply", "--batch-size", "2")

        assert result.exit_code == 1
        assert "live document 1.1.99" in result.output
        assert "2 note URI(s) were rewritten before it" in result.output
        assert "census again" in result.output.lower()
        assert w.closed

    def test_other_http_errors_are_not_swallowed(self, monkeypatch):
        w = _FakeWriter([_census(), _status(500)])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code != 0
        assert isinstance(result.exception, httpx.HTTPStatusError)

    def test_a_read_timeout_says_the_run_may_have_completed(self, monkeypatch):
        w = _FakeWriter([_census(), httpx.ReadTimeout("slow")])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        assert "may have completed" in result.output


class TestRefusals:
    def test_an_engine_without_the_route_is_named_not_traced(self, monkeypatch):
        _patch(monkeypatch, _FakeWriter([_status(404, '{"error":"not found: /note_uri_repair"}')]))

        result = _run()

        assert result.exit_code == 1
        assert "does not yet expose" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize("bad", [
        {"rewrite": "3"},      # a count that is not an integer
        {"merge": None},
        {"sample": []},        # the samples are not an object
    ])
    def test_a_malformed_report_is_refused_before_anything_is_written(self, monkeypatch, bad):
        w = _FakeWriter([_census(**bad), _census(rewritten=1, remaining=0)])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        assert [c["apply"] for c in w.calls] == [False]
        assert "unexpected report" in result.output

    def test_a_report_missing_a_class_is_refused(self, monkeypatch):
        partial = _census()
        del partial["tombstoned"]
        w = _FakeWriter([partial])
        _patch(monkeypatch, w)

        result = _run()

        assert result.exit_code == 1
        assert "tombstoned" in result.output

    def test_batch_size_and_sample_are_bounded(self):
        for args in (("--batch-size", "0"), ("--batch-size", "5001"), ("--sample", "-1"), ("--sample", "501")):
            result = _run(*args)
            assert result.exit_code == 2, args
            assert "Invalid value" in result.output, args


class TestClientWire:
    @pytest.fixture
    def client(self, monkeypatch):
        posted: list[tuple[str, dict, dict]] = []
        c = HttpCatalogClient(base_url="http://127.0.0.1:9", tenant="t", _token="x")
        monkeypatch.setattr(
            c, "_post", lambda path, body=None, **kw: (posted.append((path, body, kw)) or _census()))
        return c, posted

    def test_dry_run_posts_apply_false_and_keeps_the_retry(self, client):
        c, posted = client
        c.repair_stale_note_uris()
        (path, body, kw), = posted
        assert path == "/note_uri_repair"
        assert body == {"apply": False, "limit": 1000, "sample": 20}
        assert kw["idempotent"] is True and kw["mutates"] is False

    def test_apply_makes_one_attempt(self, client):
        c, posted = client
        c.repair_stale_note_uris(apply=True, limit=50, sample=0)
        (_, body, kw), = posted
        assert body == {"apply": True, "limit": 50, "sample": 0}
        assert kw["idempotent"] is False and kw["mutates"] is True

    def test_it_is_a_service_only_write_op_on_the_write_proxy(self):
        assert "repair_stale_note_uris" in _SERVICE_ONLY_WRITE_OPS

        class _Backend:
            def repair_stale_note_uris(self, **kw):
                return {"ok": kw}

        assert _ServiceCatalogWriter(_Backend()).repair_stale_note_uris(apply=True) == {"ok": {"apply": True}}


def test_the_client_method_speaks_to_the_fake_engine_in_the_engines_shape():
    from tests.catalog.test_http_catalog_client import start_fake_server

    server, base_url = start_fake_server()
    try:
        c = HttpCatalogClient(base_url=base_url, tenant="t", _token="x")
        dry = c.repair_stale_note_uris()
        done = c.repair_stale_note_uris(apply=True, limit=5, sample=0)
        c.close()
    finally:
        server.shutdown()
    assert dry["apply"] is False and "rewritten" not in dry
    assert done["apply"] is True and done["rewritten"] == 0 and done["remaining"] == 0
    assert set(dry["sample"]) == {"rewrite", "merge", "refused", "tombstoned", "export_target_moved"}
