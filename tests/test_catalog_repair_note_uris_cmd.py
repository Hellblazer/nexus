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
import sys

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
        "apply": False,
        "stale_live": 6,
        "rewrite": 3,
        "merge": 2,
        "merge_twin": 1,
        "merge_sibling": 1,
        "refused": 1,
        "refused_file_routed_collection": 1,
        "refused_malformed": 0,
        "tombstoned": 4,
        "export_copies": 5,
        "export_copies_target_moved": 2,
        "sample": {
            "rewrite": [
                {
                    "tumbler": "1.1.1",
                    "collection": _Y,
                    "source_uri": f"nx-note://{_X}/a",
                    "new_uri": f"nx-note://{_Y}/a",
                }
            ],
            "merge": [
                {
                    "tumbler": "1.1.7",
                    "collection": _Y,
                    "source_uri": f"nx-note://{_X}/m",
                    "new_uri": f"nx-note://{_Y}/m",
                    "twin": "1.1.8",
                    "reason": "twin",
                    "chunk_count": 3,
                    "twin_chunk_count": 0,
                }
            ],
            "refused": [
                {
                    "tumbler": "1.1.9",
                    "collection": "docs__d__some-model__v1",
                    "source_uri": f"nx-note://{_X}/odd",
                    "reason": "file_routed_collection",
                }
            ],
            "tombstoned": [
                {
                    "tumbler": "1.1.5",
                    "collection": _Y,
                    "source_uri": f"nx-note://{_X}/dead",
                }
            ],
            "export_target_moved": [
                {
                    "tumbler": "1.1.6",
                    "collection": _Y,
                    "source_uri": f"nxexp://{_X}/nx-note://{_X}/e",
                    "reason": "target_moved",
                }
            ],
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

    def repair_stale_note_uris(
        self, *, apply: bool = False, limit: int = 1000, sample: int = 20
    ) -> dict:
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
    return httpx.HTTPStatusError(
        f"HTTP {code}",
        request=request,
        response=httpx.Response(code, request=request, text=body),
    )


def _step(n: int, remaining: int, *, first: int = 1, **over) -> dict:
    """An apply response that rewrote ``n`` catalog rows: the report plus its ledger (a satellite row on each)."""
    ledger = []
    for i in range(first, first + n):
        ledger.append(
            {
                "table": "catalog_documents",
                "key": f"1.1.{i}",
                "old_uri": f"nx-note://{_X}/n{i}",
                "new_uri": f"nx-note://{_Y}/n{i}",
            }
        )
        ledger.append(
            {
                "table": "document_aspects",
                "key": str(100 + i),
                "doc_id": f"1.1.{i}",
                "old_uri": f"nx-note://{_X}/p{i}",
                "new_uri": f"nx-note://{_Y}/p{i}",
            }
        )
    return _census(rewritten=n, remaining=remaining, ledger=ledger, **over)


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    """The default ledger lands in the working directory: keep it out of the repo."""
    monkeypatch.chdir(tmp_path)


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
        assert (
            "merge candidates: 2 (1 with a live twin, 1 rewriting onto one URI with another stale row)"
            in out
        )
        assert "refused: 1" in out
        assert "tombstoned stale rows (left as they are): 4" in out
        assert (
            "nxexp:// export copies: 5, 2 whose target collection is not the one they live in"
            in out
        )
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
        assert json.loads(result.stdout) == _census()

    def test_sample_and_batch_size_are_forwarded(self, monkeypatch):
        w = _FakeWriter([_census()])
        _patch(monkeypatch, w)

        assert _run("--sample", "3", "--batch-size", "7").exit_code == 0

        assert w.calls == [{"apply": False, "limit": 7, "sample": 3}]


class TestApply:
    def test_censuses_then_rewrites_in_batches_then_censuses_again(
        self, monkeypatch, tmp_path
    ):
        w = _FakeWriter(
            [
                _census(),
                _step(2, 1, first=1),
                _step(1, 0, first=3),
                _census(rewrite=0, stale_live=3),
            ]
        )
        _patch(monkeypatch, w)

        result = _run("--apply", "--batch-size", "2")

        assert result.exit_code == 0, result.output
        assert [c["apply"] for c in w.calls] == [False, True, True, False]
        assert all(c["limit"] == 2 for c in w.calls[1:3])
        assert "Rewrote 3 note URI(s) in 2 batch(es)" in result.output
        assert "rewrite candidates (no live twin): 0" in result.output
        assert w.closed

    def test_nothing_to_rewrite_makes_no_apply_call_and_writes_no_ledger(
        self, monkeypatch, tmp_path
    ):
        w = _FakeWriter([_census(rewrite=0, stale_live=3)])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert [c["apply"] for c in w.calls] == [False]
        assert "Nothing to rewrite" in result.output
        assert list(tmp_path.glob("*.jsonl")) == []

    def test_apply_json_carries_before_after_the_total_and_the_ledger_path(
        self, monkeypatch, tmp_path
    ):
        w = _FakeWriter([_census(), _step(3, 0), _census(rewrite=0)])
        _patch(monkeypatch, w)

        result = _run("--apply", "--json")

        assert result.exit_code == 0, result.output
        doc = json.loads(result.stdout)
        assert doc["rewritten"] == 3 and doc["batches"] == 1
        assert doc["before"]["rewrite"] == 3 and doc["after"]["rewrite"] == 0
        assert (tmp_path / doc["ledger"]).is_file()

    def test_a_batch_that_rewrites_nothing_while_rows_remain_stops_instead_of_spinning(
        self, monkeypatch
    ):
        w = _FakeWriter(
            [_census(), _census(rewritten=0, remaining=3, ledger=[]), _census()]
        )
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        assert [c["apply"] for c in w.calls] == [False, True, False]
        assert "made no progress" in result.output

    def test_a_409_names_the_holder_and_stops_with_the_progress_so_far(
        self, monkeypatch, tmp_path
    ):
        body = json.dumps(
            {
                "error": "repairing the stale note URI of 1.1.2 would rewrite a document's "
                "source_uri to nx-note://k/a, which live document 1.1.99 in k already holds."
            }
        )
        w = _FakeWriter([_census(), _step(2, 1), _status(409, body)])
        _patch(monkeypatch, w)

        result = _run("--apply", "--batch-size", "2")

        assert result.exit_code == 1
        assert "live document 1.1.99" in result.output
        assert "2 note URI(s) were rewritten before it" in result.output
        assert "census again" in result.output.lower()
        assert w.closed
        (ledger,) = tmp_path.glob("*.jsonl")
        assert str(ledger.name) in result.output
        assert len(ledger.read_text().splitlines()) == 4, (
            "the first batch's rows are in the ledger"
        )

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


class TestLedger:
    def test_every_rewritten_row_of_every_table_is_written_as_one_json_line_with_its_batch(
        self, monkeypatch, tmp_path
    ):
        w = _FakeWriter(
            [
                _census(rewrite=3),
                _step(2, 1, first=1),
                _step(1, 0, first=3),
                _census(rewrite=0),
            ]
        )
        _patch(monkeypatch, w)

        result = _run("--apply", "--batch-size", "2")

        assert result.exit_code == 0, result.output
        (ledger,) = tmp_path.glob("nx-note-uri-repair-*.jsonl")
        rows = [json.loads(line) for line in ledger.read_text().splitlines()]
        assert len(rows) == 6
        assert [r["batch"] for r in rows] == [1, 1, 1, 1, 2, 2]
        assert {r["table"] for r in rows} == {"catalog_documents", "document_aspects"}
        first = rows[0]
        assert first == {
            "batch": 1,
            "table": "catalog_documents",
            "key": "1.1.1",
            "old_uri": f"nx-note://{_X}/n1",
            "new_uri": f"nx-note://{_Y}/n1",
        }
        assert rows[1]["doc_id"] == "1.1.1" and rows[1]["key"] == "101"
        assert str(ledger.name) in result.output

    def test_ledger_option_names_the_file(self, monkeypatch, tmp_path):
        w = _FakeWriter([_census(), _step(3, 0), _census(rewrite=0)])
        _patch(monkeypatch, w)
        target = tmp_path / "undo" / "mine.jsonl"
        target.parent.mkdir()

        result = _run("--apply", "--ledger", str(target))

        assert result.exit_code == 0, result.output
        assert len(target.read_text().splitlines()) == 6
        assert list(tmp_path.glob("nx-note-uri-repair-*.jsonl")) == []
        assert str(target) in result.output

    def test_an_existing_ledger_is_appended_to_never_truncated(
        self, monkeypatch, tmp_path
    ):
        target = tmp_path / "mine.jsonl"
        target.write_text('{"earlier": true}\n')
        _patch(monkeypatch, _FakeWriter([_census(), _step(3, 0), _census(rewrite=0)]))

        assert _run("--apply", "--ledger", str(target)).exit_code == 0

        lines = target.read_text().splitlines()
        assert lines[0] == '{"earlier": true}' and len(lines) == 7

    def test_a_dry_run_writes_no_ledger_and_refuses_the_ledger_option(
        self, monkeypatch, tmp_path
    ):
        _patch(monkeypatch, _FakeWriter([_census()]))
        assert _run().exit_code == 0
        assert list(tmp_path.glob("*.jsonl")) == []
        w = _FakeWriter([_census()])
        _patch(monkeypatch, w)
        result = _run("--ledger", str(tmp_path / "x.jsonl"))
        assert (
            result.exit_code == 2
            and "--ledger only applies with --apply" in result.output
        )
        assert w.calls == [], "the usage error comes before any engine call"

    def test_the_ledger_is_flushed_batch_by_batch_before_the_next_batch_is_asked_for(
        self, monkeypatch, tmp_path
    ):
        seen: list[int] = []

        class _Watching(_FakeWriter):
            def repair_stale_note_uris(self, **kw):
                if kw["apply"]:
                    ledgers = list(tmp_path.glob("*.jsonl"))
                    seen.append(
                        len(ledgers[0].read_text().splitlines()) if ledgers else -1
                    )
                return super().repair_stale_note_uris(**kw)

        _patch(
            monkeypatch,
            _Watching(
                [_census(), _step(2, 1), _step(1, 0, first=3), _census(rewrite=0)]
            ),
        )

        assert _run("--apply", "--batch-size", "2").exit_code == 0

        assert seen == [0, 4], (
            "batch 2 was asked for only after batch 1's four lines were on disk"
        )

    @pytest.mark.parametrize(
        "bad",
        [
            None,  # no ledger at all
            [],  # rows rewritten, none named
            [{"table": "catalog_documents", "key": "1.1.1"}],  # no old/new URI
            "not-a-list",
        ],
    )
    def test_a_step_without_a_usable_ledger_is_stopped_on(
        self, monkeypatch, tmp_path, bad
    ):
        step = _step(1, 0)
        step.pop("ledger")
        if bad is not None:
            step["ledger"] = bad
        w = _FakeWriter([_census(), step, _census(rewrite=0)])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        assert (
            "without a usable ledger" in result.output
            or "ledger names" in result.output
        )
        assert [c["apply"] for c in w.calls] == [False, True], (
            "nothing further was asked for"
        )


class TestFixpoint:
    def test_a_chain_goes_round_again_when_the_census_after_still_lists_rewrite_candidates(
        self, monkeypatch
    ):
        # B moved in pass 1, which freed A's target: the census after pass 1 lists A.
        w = _FakeWriter(
            [
                _census(rewrite=1),
                _step(1, 0, first=1),
                _census(rewrite=1),
                _step(1, 0, first=2),
                _census(rewrite=0),
            ]
        )
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 0, result.output
        assert [c["apply"] for c in w.calls] == [False, True, False, True, False]
        assert "Rewrote 2 note URI(s) in 2 batch(es)" in result.output

    def test_a_rewrite_that_never_settles_is_capped_at_the_batch_count_plus_the_chain_slack(
        self, monkeypatch
    ):
        steps = []
        for i in range(1, 16):
            steps += [_step(1, 0, first=i), _census(rewrite=1)]
        w = _FakeWriter([_census(rewrite=1)] + steps)
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        applies = [c for c in w.calls if c["apply"]]
        assert len(applies) == 11, "ceil(1 / 1000) + 10"
        assert (
            "cap" in result.output.lower()
            or "cap" in (result.stderr if result.stderr_bytes else "").lower()
        )


class TestRefusals:
    def test_an_engine_without_the_route_is_named_not_traced(self, monkeypatch):
        _patch(
            monkeypatch,
            _FakeWriter([_status(404, '{"error":"not found: /note_uri_repair"}')]),
        )

        result = _run()

        assert result.exit_code == 1
        assert "does not yet expose" in result.output
        assert "Traceback" not in result.output

    @pytest.mark.parametrize(
        "bad",
        [
            {"rewrite": "3"},  # a count that is not an integer
            {"merge": None},
            {"sample": []},  # the samples are not an object
        ],
    )
    def test_a_malformed_report_is_refused_before_anything_is_written(
        self, monkeypatch, bad
    ):
        w = _FakeWriter([_census(**bad), _census(rewritten=1, remaining=0)])
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        assert [c["apply"] for c in w.calls] == [False]
        assert "unexpected report" in result.output

    @pytest.mark.parametrize("missing", ["tombstoned", "export_copies_target_moved"])
    def test_a_report_missing_a_class_is_refused(self, monkeypatch, missing):
        partial = _census()
        del partial[missing]
        w = _FakeWriter([partial])
        _patch(monkeypatch, w)

        result = _run()

        assert result.exit_code == 1
        assert missing in result.output

    def test_batch_size_and_sample_are_bounded(self):
        for args in (
            ("--batch-size", "0"),
            ("--batch-size", "5001"),
            ("--sample", "-1"),
            ("--sample", "501"),
        ):
            result = _run(*args)
            assert result.exit_code == 2, args
            assert "Invalid value" in result.output, args


class TestClientWire:
    @pytest.fixture
    def client(self, monkeypatch):
        posted: list[tuple[str, dict, dict]] = []
        c = HttpCatalogClient(base_url="http://127.0.0.1:9", tenant="t", _token="x")
        monkeypatch.setattr(
            c,
            "_post",
            lambda path, body=None, **kw: posted.append((path, body, kw)) or _census(),
        )
        return c, posted

    def test_dry_run_posts_apply_false_and_keeps_the_retry(self, client):
        c, posted = client
        c.repair_stale_note_uris()
        ((path, body, kw),) = posted
        assert path == "/note_uri_repair"
        assert body == {"apply": False, "limit": 1000, "sample": 20}
        assert kw["idempotent"] is True and kw["mutates"] is False

    def test_apply_makes_one_attempt(self, client):
        c, posted = client
        c.repair_stale_note_uris(apply=True, limit=50, sample=0)
        ((_, body, kw),) = posted
        assert body == {"apply": True, "limit": 50, "sample": 0}
        assert kw["idempotent"] is False and kw["mutates"] is True

    def test_it_is_a_service_only_write_op_on_the_write_proxy(self):
        assert "repair_stale_note_uris" in _SERVICE_ONLY_WRITE_OPS

        class _Backend:
            def repair_stale_note_uris(self, **kw):
                return {"ok": kw}

        assert _ServiceCatalogWriter(_Backend()).repair_stale_note_uris(apply=True) == {
            "ok": {"apply": True}
        }


def test_the_client_method_speaks_to_the_fake_engine_in_the_engines_shape():
    from tests.catalog.test_http_catalog_client import start_fake_server  # noqa: PLC0415 — test-only helper, imported where used

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
    assert set(dry["sample"]) == {
        "rewrite",
        "merge",
        "refused",
        "tombstoned",
        "export_target_moved",
    }


class TestLedgerHonesty:
    """Confirmation review of nexus-bivz3.24: what the ledger cannot recover is said, and what it gets is kept."""

    def test_a_timeout_during_apply_says_the_batch_in_flight_may_be_unledgered(
        self, monkeypatch
    ):
        request = httpx.Request("POST", "http://test/v1/catalog/note_uri_repair")
        w = _FakeWriter(
            [_census(rewrite=2), httpx.ReadTimeout("slow", request=request)]
        )
        _patch(monkeypatch, w)

        result = _run("--apply")

        assert result.exit_code == 1
        assert "cannot be undone from the ledger" in result.output, result.output
        assert "engine log" not in result.output

    def test_a_timeout_during_the_dry_run_says_nothing_about_a_ledger(
        self, monkeypatch
    ):
        request = httpx.Request("POST", "http://test/v1/catalog/note_uri_repair")
        w = _FakeWriter([httpx.ReadTimeout("slow", request=request)])
        _patch(monkeypatch, w)

        result = _run()

        assert result.exit_code == 1
        assert "ledger" not in result.output.lower(), result.output

    def test_an_unusable_ledger_for_a_committed_batch_is_saved_beside_the_ledger(
        self, monkeypatch, tmp_path
    ):
        bad = _census(rewritten=1, remaining=0, ledger=[{"table": "catalog_documents"}])
        w = _FakeWriter([_census(rewrite=1), bad])
        _patch(monkeypatch, w)

        result = _run("--apply", "--ledger", str(tmp_path / "led.jsonl"))

        assert result.exit_code == 1
        rejected = tmp_path / "led.jsonl.rejected"
        assert rejected.is_file(), result.output
        saved = json.loads(rejected.read_text().splitlines()[0])
        assert saved["step"]["ledger"] == [{"table": "catalog_documents"}]
        assert (
            "was rewritten" in result.output and "led.jsonl.rejected" in result.output
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_a_new_ledger_file_is_owner_only(self, monkeypatch, tmp_path):
        w = _FakeWriter([_census(rewrite=1), _step(1, 0), _census(rewrite=0)])
        _patch(monkeypatch, w)

        result = _run("--apply", "--ledger", str(tmp_path / "led.jsonl"))

        assert result.exit_code == 0, result.output
        assert (tmp_path / "led.jsonl").stat().st_mode & 0o777 == 0o600
