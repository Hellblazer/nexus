# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx t3 quarantine restore`` (RDR-192 Step 9 Day-2, bead nexus-2x9xa).

Client half of the engine route ``POST /v1/vectors/gc/quarantine-restore``
(``nexus.quarantine_restore_chunks``, changeset ``vectors-025``). Three layers, each
owning what it can prove:

* the wire: ``HttpVectorClient.gc_quarantine_restore`` posts the right body to the right
  path, and the route is registered as a write that is never auto-retried;
* the CLI's own logic against a stubbed client (paging, validation, rendering, exit
  codes). A stub is right here because every one of these is a branch on the engine's
  answer, which the Java suites (``QuarantineRestoreIntegrationTest``,
  ``VectorHandlerQuarantineRestoreRouteTest``) already pin against a real database;
* the verb end to end against the real engine substrate (``t2_service_env``): chunks the
  engine's own quarantine route moved, restored by chash, by ``quarantined_at`` window and
  by audit id, with the refusal of a sample-only audit row.
"""
from __future__ import annotations

import hashlib
import json as _json
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands.t3_cmds import quarantine as t3_quarantine
from nexus.commands.t3 import t3
from nexus.db import gateway_backoff
from nexus.db import http_vector_client as hv
from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError
from tests._catalog_fixture_ops import register_real_doc_id


def _chash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


ORIGIN = "knowledge__qrestore__bge-base-en-v15-768__v1"
SIBLING = "quarantine-knowledge__qrestore__bge-base-en-v15-768__v1"


def _doc(stdout: str) -> dict:
    """The one JSON document on stdout. In a dev checkout the production-write guard logs a structlog
    line to stdout before a write route's first call (``guard_production_write.opt_in_accepted``); that is
    the test harness, never a deployed client, so skip to the document."""
    return _json.loads(stdout[stdout.index("{"):])


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── the wire ─────────────────────────────────────────────────────────────────


class TestWire:
    def test_the_method_posts_only_the_fields_it_was_given(self) -> None:
        client = HttpVectorClient.__new__(HttpVectorClient)
        client._tenant = "tenant-x"
        with patch.object(hv, "_post", return_value={"rows": []}) as post:
            client.gc_quarantine_restore(ORIGIN, SIBLING, chashes=[_chash("a")], dry_run=True, actor="me")
        path, body = post.call_args.args
        assert path == "/v1/vectors/gc/quarantine-restore"
        assert body == {
            "origin_collection": ORIGIN, "quarantine_collection": SIBLING,
            "chashes": [_chash("a")], "dry_run": True, "actor": "me",
        }
        assert post.call_args.kwargs == {"tenant": "tenant-x"}

    def test_reattach_is_sent_only_when_given(self) -> None:
        client = HttpVectorClient.__new__(HttpVectorClient)
        client._tenant = "t"
        with patch.object(hv, "_post", return_value={"rows": []}) as post:
            client.gc_quarantine_restore(ORIGIN, SIBLING, chashes=[_chash("a")])
            client.gc_quarantine_restore(ORIGIN, SIBLING, chashes=[_chash("a")], reattach=False)
            client.gc_quarantine_restore(ORIGIN, SIBLING, chashes=[_chash("a")], reattach=True)
        none, off, on = (c.args[1] for c in post.call_args_list)
        assert "reattach" not in none, "unsent means the engine's default (reattach on)"
        assert off["reattach"] is False and on["reattach"] is True

    def test_each_source_maps_to_its_own_fields(self) -> None:
        client = HttpVectorClient.__new__(HttpVectorClient)
        client._tenant = "t"
        with patch.object(hv, "_post", return_value={"rows": []}) as post:
            client.gc_quarantine_restore(ORIGIN, SIBLING, audit_id=7, offset=1000, limit=500)
            client.gc_quarantine_restore(
                ORIGIN, SIBLING, quarantined_since="2026-09-01T00:00:00Z",
                quarantined_before="2026-09-08T00:00:00Z", after_chash=_chash("z"))
        first, second = (c.args[1] for c in post.call_args_list)
        assert first == {"origin_collection": ORIGIN, "quarantine_collection": SIBLING,
                         "audit_id": 7, "offset": 1000, "limit": 500}
        assert second == {"origin_collection": ORIGIN, "quarantine_collection": SIBLING,
                          "quarantined_since": "2026-09-01T00:00:00Z",
                          "quarantined_before": "2026-09-08T00:00:00Z", "after_chash": _chash("z")}

    def test_the_route_is_a_write_and_is_never_auto_retried(self) -> None:
        path = "/v1/vectors/gc/quarantine-restore"
        assert any(path.endswith(s) for s in hv._T3_WRITE_PATH_SUFFIXES), \
            "the dev-checkout production-write guard (nexus-a2qhz) must see this route as a write"
        assert gateway_backoff.is_non_idempotent_sweep_path(path), \
            "a resent restore would re-run against the already-moved state and misreport (nexus-ll31n)"


# ── the CLI against a stubbed client ─────────────────────────────────────────


def _row(chash: str, outcome: str, *, no_manifest=None, reapable_after=None, reattach=None, attached=False,
         owner=None, owner_title=None, position=None, chunk_title=None, reason=None, owner_rows=None,
         owner_chunks=None) -> dict:
    return {"chash": chash, "outcome": outcome, "no_manifest": no_manifest, "reapable_after": reapable_after,
            "reattach": reattach, "attached": attached, "owner": owner, "owner_title": owner_title,
            "position": position, "chunk_title": chunk_title, "reason": reason, "owner_rows": owner_rows,
            "owner_chunks": owner_chunks}


def _hidden_row(chash: str, *, verdict="no_live_owner", reapable_after="2026-10-31T12:00:00Z", **kw) -> dict:
    """A restored chunk the engine could not attach: bytes back, hidden."""
    return _row(chash, "restored", no_manifest=True, reapable_after=reapable_after, reattach=verdict, **kw)


def _attached_row(chash: str, owner="1.2.3", title="Legacy Note", position=0, **kw) -> dict:
    return _row(chash, "restored", no_manifest=False, reattach="attach", attached=True,
                owner=owner, owner_title=title, position=position, **kw)


def _page(rows, *, audit_id=None, dry_run=False, source=None, next_after=None) -> dict:
    counts = {k: sum(1 for r in rows if r["outcome"] == k)
              for k in ("restored", "would_restore", "present", "dim_conflict", "missing")}
    return {"origin_collection": ORIGIN, "quarantine_collection": SIBLING, "dry_run": dry_run,
            "audit_id": audit_id, **counts, "rows": rows, "source": source, "next_after": next_after}


class _Stub:
    def __init__(self, pages=None, error: Exception | None = None, error_after: int | None = None,
                 origin: str = ORIGIN, content_type: str = "knowledge"):
        self.origin = origin
        #: what the origin's catalog row says (the verb reads it from the row, never from the name)
        self.content_type = content_type
        self.pages = list(pages or [])
        self.error = error
        #: raise *error* on the call after this many pages were served (None: on the first call)
        self.error_after = error_after
        self.calls: list[dict] = []

    def gc_quarantine_restore(self, origin, quarantine, **kw):
        assert origin == self.origin and quarantine == SIBLING
        self.calls.append(kw)
        if self.error is not None and (self.error_after is None or len(self.calls) > self.error_after):
            raise self.error
        return self.pages.pop(0)


def _run(runner: CliRunner, stub: _Stub, *args: str):
    with patch.object(t3_quarantine, "_make_t3", return_value=stub), \
         patch.object(t3_quarantine, "_quarantine_name", return_value=SIBLING), \
         patch.object(t3_quarantine, "_content_type", return_value=stub.content_type):
        return runner.invoke(t3, ["quarantine", "restore", "--collection", stub.origin, *args])


class TestCli:
    def test_the_table_the_totals_and_what_a_hidden_chunk_costs(self, runner) -> None:
        a, b, c = _chash("a"), _chash("b"), _chash("c")
        stub = _Stub([_page([
            _hidden_row(a, chunk_title="Untitled"),
            _row(b, "present", reattach="owned"),
            _row(c, "missing"),
        ], audit_id=41)])

        result = _run(runner, stub, "--chash", a, "--chash", b, "--chash", c)

        out = result.output
        assert result.exit_code == 1, "a requested chash that is missing is a failed restore: exit 1\n" + out
        assert f"{a}  restored" in out and f"{b}  present" in out and f"{c}  missing" in out
        assert "restored 1, present 1, dim_conflict 0, missing 1" in out
        assert "gc_audit 41" in out
        # The operator is told, not left to find out: the chunk is back but nothing shows it, and until it is
        # owned the reaper may take it again on the date given.
        assert "HIDDEN from search and get" in out
        assert "2026-10-31T12:00:00Z" in out and "reaper" in out
        assert "reaper will quarantine them again" not in out, "the census gate can refuse the whole collection instead"
        assert stub.calls[0]["chashes"] == [a, b, c]

    def test_an_attached_chunk_is_named_with_its_owner_and_position_and_is_not_called_hidden(self, runner) -> None:
        a = _chash("a")
        stub = _Stub([_page([_attached_row(a, owner="1.2.3", title="Legacy Note", position=4)], audit_id=3)])

        result = _run(runner, stub, "--chash", a)

        assert result.exit_code == 0, result.output
        assert "attached to 'Legacy Note' (1.2.3) at position 4" in result.output
        assert "reattach: attached 1, superseded 0, no live owner 0, no position 0" in result.output
        assert "HIDDEN" not in result.output and "nx store put" not in result.output
        assert stub.calls[0]["reattach"] is True, "reattach is the default"

    def test_a_knowledge_chunk_with_no_key_prints_the_reput_recipe_and_a_superseded_one_does_not(self, runner) -> None:
        a, b, c = _chash("a"), _chash("b"), _chash("c")
        stub = _Stub([_page([
            _hidden_row(a, verdict="superseded", reason="position_taken", owner="1.2.3", owner_title="Legacy Note",
                        position=0),
            _hidden_row(b, verdict="no_position", owner="1.2.4", owner_title="It's a Multi"),
            _hidden_row(c, verdict="no_live_owner", chunk_title="orphan title"),
        ], audit_id=8)])

        result = _run(runner, stub, "--chash", a, "--chash", b, "--chash", c)

        out = result.output
        assert result.exit_code == t3_quarantine.EXIT_HIDDEN == 3, out
        assert "3 chunks stay HIDDEN from search and get" in out
        assert f"nx store put - --collection {ORIGIN} --title 'It'\"'\"'s a Multi'" in out, \
            "a title with a quote is shell-quoted"
        assert "--title 'orphan title'" in out and "no live owner named" in out
        assert "--title 'Legacy Note'" not in out, \
            "a superseded chunk's document is live and current: a re-put would REPLACE its text with the stale chunk"
        assert "current text is live; nothing to do unless you need the old text" in out
        assert "reattach: attached 0, superseded 1, no live owner 1, no position 1" in out
        assert "backfill-manifest" not in out, "backfill does nothing for this class, so the output never suggests it"

    def test_a_file_collections_keyless_chunks_get_the_reindex_advice_never_a_store_put(self, runner) -> None:
        origin = "docs__qrestore__bge-base-en-v15-768__v1"
        a, b = _chash("a"), _chash("b")
        stub = _Stub([_page([
            _hidden_row(a, verdict="no_live_owner", chunk_title="README.md chunk"),
            _hidden_row(b, verdict="no_position", owner="1.2.4", owner_title="Big doc"),
        ])], origin=origin, content_type="docs")

        result = _run(runner, stub, "--chash", a, "--chash", b)

        out = result.output
        assert result.exit_code == 3, out
        assert "nx store put" not in out, "a store put mints a stray note in a file collection and fixes nothing"
        assert "probably still indexed but its chunks carry no key" in out
        assert "nx index repo --force" in out

    def test_an_unreadable_content_type_names_both_remedies_never_guesses_one(self, runner) -> None:
        a = _chash("a")
        stub = _Stub([_page([_hidden_row(a, verdict="no_live_owner", chunk_title="x")])], content_type="")

        result = _run(runner, stub, "--chash", a)

        assert result.exit_code == 3, result.output
        assert "If this collection holds notes" in result.output and "re-index the owning file" in result.output
        assert "# owner" not in result.output and "no live owner named" not in result.output, "no per-chunk recipe"

    def test_each_superseded_reason_gets_its_own_words(self, runner) -> None:
        hs = [_chash(str(i)) for i in range(5)]
        reasons = ["complete", "indexing", "other_collection", "rival", "race"]
        stub = _Stub([_page([
            _hidden_row(h, verdict="superseded", reason=why, owner="1.2.3", owner_title="Doc", position=0)
            for h, why in zip(hs, reasons, strict=True)])])

        result = _run(runner, stub, *[x for h in hs for x in ("--chash", h)])

        out = result.output
        assert result.exit_code == 3, out
        assert "current text is live; nothing to do unless you need the old text" in out      # complete
        assert "in the middle of an index run" in out and "Run the same command again" in out  # indexing
        assert "sit under another collection" in out                                           # other_collection
        assert "claims the same position" in out and "nx store get CHASH" in out               # rival, race
        assert "nx store put" not in out
        assert "mid index run" in out and "version ambiguous" in out, "the NOTE column says it too"

    def test_no_reattach_is_passed_through_and_the_output_says_what_it_would_have_done(self, runner) -> None:
        a = _chash("a")
        stub = _Stub([_page([_hidden_row(a, verdict="attach", owner="1.2.3", owner_title="Legacy Note", position=0)],
                            audit_id=2)])

        result = _run(runner, stub, "--chash", a, "--no-reattach")

        assert result.exit_code == 3, "bytes-only leaves the chunk hidden: a script must see it\n" + result.output
        assert stub.calls[0]["reattach"] is False
        out = result.output
        assert "reattach: off (--no-reattach)" in out and "a run without the flag would attach 1" in out
        assert "NOT attached (--no-reattach)" in out
        assert "HIDDEN from search and get" in out, "a bytes-only restore leaves the chunk hidden, and says so"
        assert "Run the command again without it" in out
        assert "nx store put" not in out, "the remedy is a rerun, not a re-put"

    def test_a_dry_run_reports_would_attach_and_would_stay_hidden_and_passes_dry_run_through(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        stub = _Stub([_page([
            _row(a, "would_restore", reattach="attach", owner="1.2.3", owner_title="Legacy Note", position=0),
            _row(b, "would_restore", reattach="superseded", owner="1.2.3", owner_title="Legacy Note", position=1),
        ], dry_run=True)])

        result = _run(runner, stub, "--chash", a, "--chash", b, "--dry-run")

        assert result.exit_code == 3, "a dry run exits 3 when a real run would leave a chunk hidden\n" + result.output
        assert stub.calls[0]["dry_run"] is True
        out = result.output
        assert "dry run" in out.lower() and "would restore 2" in out
        assert "would attach to 'Legacy Note' (1.2.3) at position 0" in out
        assert "reattach: would attach 1, superseded 1" in out
        assert "1 chunk would stay HIDDEN" in out
        assert "gc_audit" not in out

    @pytest.mark.parametrize("outcome", ["missing", "dim_conflict"])
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_a_missing_or_width_conflicting_chunk_exits_1_even_on_a_dry_run(self, runner, outcome, dry_run) -> None:
        a, b = _chash("a"), _chash("b")
        ok = "would_restore" if dry_run else "restored"
        stub = _Stub([_page([_attached_row(a), _row(b, outcome)], dry_run=dry_run)])
        if dry_run:
            stub.pages[0]["rows"][0]["outcome"] = ok
            stub.pages[0]["restored"], stub.pages[0]["would_restore"] = 0, 1
        args = ["--chash", a, "--chash", b] + (["--dry-run"] if dry_run else [])

        result = _run(runner, stub, *args)

        assert result.exit_code == t3_quarantine.EXIT_UNRESTORED, result.output

    def test_chashes_go_in_batches_of_a_thousand_and_are_deduplicated(self, runner) -> None:
        hs = [_chash(str(i)) for i in range(2500)]
        stub = _Stub([_page([_attached_row(h) for h in hs[i:i + 1000]], audit_id=i) for i in (0, 1000, 2000)])
        args = []
        for h in hs + hs[:5]:
            args += ["--chash", h]
        result = _run(runner, stub, *args)
        assert result.exit_code == 0, result.output[-400:]
        assert [len(c["chashes"]) for c in stub.calls] == [1000, 1000, 500]
        assert "restored 2500" in result.output

    def test_a_window_pages_by_next_after_until_it_is_exhausted(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        stub = _Stub([
            _page([_attached_row(a)], next_after=a),
            _page([_attached_row(b)]),
        ])
        result = _run(runner, stub, "--quarantined-since", "2026-09-01", "--quarantined-before", "2026-09-08T12:00:00+00:00")
        assert result.exit_code == 0, result.output
        assert stub.calls[0]["quarantined_since"] == "2026-09-01T00:00:00Z"
        assert stub.calls[0]["quarantined_before"] == "2026-09-08T12:00:00Z"
        assert "after_chash" not in stub.calls[0] or stub.calls[0]["after_chash"] is None
        assert stub.calls[1]["after_chash"] == a
        assert "restored 2" in result.output

    def test_an_audit_id_pages_by_the_sources_next_offset(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        src = lambda nxt: {"audit_id": 9, "operation": "reaper_quarantine", "chash_count": 2,  # noqa: E731
                           "chashes_listed": 2, "offset": 0, "next_offset": nxt}
        stub = _Stub([
            _page([_attached_row(a)], source=src(1)),
            _page([_attached_row(b)], source=src(None)),
        ])
        result = _run(runner, stub, "--audit-id", "9")
        assert result.exit_code == 0, result.output
        assert [c["offset"] for c in stub.calls] == [0, 1]
        assert all(c["audit_id"] == 9 for c in stub.calls)
        assert "reaper_quarantine" in result.output

    @pytest.mark.parametrize("args", [
        [],
        ["--chash", _chash("a"), "--audit-id", "3"],
        ["--audit-id", "3", "--quarantined-since", "2026-01-01"],
        ["--chash", "not-a-chash"],
        ["--chash", _chash("a").upper()],
        ["--quarantined-since", "yesterday"],
        ["--quarantined-since", "2026-09-08", "--quarantined-before", "2026-09-01"],
        ["--dry-run"],
    ])
    def test_a_bad_source_is_refused_before_the_engine_is_called(self, runner, args) -> None:
        stub = _Stub()
        result = _run(runner, stub, *args)
        assert result.exit_code != 0
        assert stub.calls == []

    def test_json_is_one_parseable_document_with_every_row_and_the_reattach_tally(self, runner) -> None:
        a, b, c = _chash("a"), _chash("b"), _chash("c")
        stub = _Stub([_page([
            _attached_row(a), _row(b, "present", reattach="owned"), _hidden_row(c, verdict="superseded"),
        ], audit_id=5)])
        result = _run(runner, stub, "--chash", a, "--chash", b, "--chash", c, "--json")
        assert result.exit_code == 3, result.output
        doc = _doc(result.stdout)
        assert doc["hidden"] == 1, "the one superseded chunk stays hidden"
        assert doc["origin_collection"] == ORIGIN and doc["quarantine_collection"] == SIBLING
        assert doc["reattach"] is True
        assert doc["totals"] == {"restored": 2, "would_restore": 0, "present": 1, "dim_conflict": 0, "missing": 0}
        assert doc["reattach_totals"] == {"attached": 1, "would_attach": 0, "superseded": 1,
                                          "no_live_owner": 0, "no_position": 0}
        assert doc["audit_ids"] == [5]
        assert [r["outcome"] for r in doc["rows"]] == ["restored", "present", "restored"]
        assert doc["reapable_again_after"] == "2026-10-31T12:00:00Z"
        assert "error" not in doc

    def test_the_earliest_reapable_date_is_the_earliest_in_time_not_in_string_order(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        # "...:00.123456Z" sorts BEFORE "...:00Z" as a string ('.' < 'Z') and AFTER it in time.
        stub = _Stub([_page([
            _hidden_row(a, reapable_after="2026-10-31T12:00:00.123456Z"),
            _hidden_row(b, reapable_after="2026-10-31T12:00:00Z"),
        ])])
        result = _run(runner, stub, "--chash", a, "--chash", b, "--json")
        assert _doc(result.stdout)["reapable_again_after"] == "2026-10-31T12:00:00Z", \
            "the string sort would pick the fractional one"

    def test_an_engine_without_the_route_exits_4_and_a_refusal_exits_5_never_a_traceback(self, runner) -> None:
        no_route = _run(runner, _Stub(error=VectorServiceError("not found", code=404)), "--chash", _chash("a"))
        assert no_route.exit_code == t3_quarantine.EXIT_NO_ROUTE, no_route.output
        assert "engine" in no_route.output.lower()
        refused = _run(runner, _Stub(error=VectorServiceError(
            "gc_audit row 7 (gc_quarantine_orphans) lists only a sample; select them by "
            "(quarantined_since / quarantined_before)", code=400)), "--audit-id", "7")
        assert refused.exit_code == t3_quarantine.EXIT_ENGINE_ERROR, refused.output
        assert "sample" in refused.output
        assert "--quarantined-since / --quarantined-before" in refused.output, \
            "the engine's field names read as the flags the operator types"
        assert refused.exception is None or isinstance(refused.exception, SystemExit)

    def test_a_held_lock_is_a_typed_retryable_exit_6_not_a_refusal(self, runner) -> None:
        busy = VectorServiceError(
            "a manifest writer or an index run holds the collection's lock; nothing was moved, attached or audited",
            code=503, reason="quarantine_restore_busy")
        result = _run(runner, _Stub(error=busy), "--chash", _chash("a"))
        assert result.exit_code == 6 == t3_quarantine.EXIT_BUSY, \
            "the documented literal: docs and scripts depend on the number, not the constant\n" + result.output
        assert "busy" in result.output and "nothing moved" in result.output and "again" in result.output
        assert "refused by the engine" not in result.output
        # A 503 that is NOT the typed busy answer is still an engine failure.
        other = _run(runner, _Stub(error=VectorServiceError("bad gateway", code=503)), "--chash", _chash("a"))
        assert other.exit_code == t3_quarantine.EXIT_ENGINE_ERROR, other.output

    def test_a_failure_on_a_later_page_still_reports_what_the_earlier_pages_committed(self, runner) -> None:
        hs = [_chash(str(i)) for i in range(1500)]
        first = _page([_attached_row(h) for h in hs[:1000]], audit_id=77)
        stub = _Stub([first], error=VectorServiceError("statement timeout", code=500), error_after=1)

        result = _run(runner, stub, *[x for h in hs for x in ("--chash", h)])

        assert result.exit_code == t3_quarantine.EXIT_ENGINE_ERROR, result.output[-500:]
        out = result.output
        assert "restored 1000" in out, "the committed page is reported, not dropped"
        assert "gc_audit 77" in out, "and so is its audit id"
        assert "Pages before it are committed" in out and "1 page" in out
        assert "refused by the engine" in out

    def test_a_failure_on_a_later_page_keeps_the_json_document_and_names_the_error(self, runner) -> None:
        hs = [_chash(str(i)) for i in range(1100)]
        first = _page([_attached_row(h) for h in hs[:1000]], audit_id=78)
        busy = VectorServiceError("held", code=503, reason="quarantine_restore_busy")
        stub = _Stub([first], error=busy, error_after=1)

        result = _run(runner, stub, *[x for h in hs for x in ("--chash", h)], "--json")

        assert result.exit_code == t3_quarantine.EXIT_BUSY, result.output[-500:]
        doc = _doc(result.stdout)
        assert doc["audit_ids"] == [78] and doc["totals"]["restored"] == 1000
        assert doc["error"]["exit_code"] == t3_quarantine.EXIT_BUSY and doc["error"]["pages_committed"] == 1

    def test_a_failure_on_the_first_page_prints_no_empty_report(self, runner) -> None:
        result = _run(runner, _Stub(error=VectorServiceError("boom", code=500)), "--chash", _chash("a"))
        assert result.exit_code == t3_quarantine.EXIT_ENGINE_ERROR
        assert "CHASH" not in result.output and "restored 0" not in result.output
        assert "Pages before it" not in result.output


    def test_exit_3_means_restored_but_hidden_and_exit_0_means_everything_is_visible(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        visible = _run(runner, _Stub([_page([_attached_row(a), _row(b, "present", reattach="owned")])]),
                       "--chash", a, "--chash", b, "--json")
        assert visible.exit_code == 0, visible.output
        assert _doc(visible.stdout)["hidden"] == 0

        hidden = _run(runner, _Stub([_page([_attached_row(a), _hidden_row(b)])]), "--chash", a, "--chash", b, "--json")
        assert hidden.exit_code == 3 == t3_quarantine.EXIT_HIDDEN, hidden.output
        assert _doc(hidden.stdout)["hidden"] == 1

    def test_a_missing_chunk_wins_over_a_hidden_one(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        result = _run(runner, _Stub([_page([_hidden_row(a), _row(b, "missing")])]), "--chash", a, "--chash", b)
        assert result.exit_code == t3_quarantine.EXIT_UNRESTORED, result.output
        assert "HIDDEN" in result.output, "and the hidden chunk is still reported"

    def test_a_partial_attach_says_m_of_n_and_the_json_carries_it(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        rows = [_attached_row(a, position=0, owner_rows=2, owner_chunks=3),
                _attached_row(b, position=1, owner_rows=2, owner_chunks=3)]

        result = _run(runner, _Stub([_page(rows)]), "--chash", a, "--chash", b)

        assert result.exit_code == 0, result.output
        assert "'Legacy Note' (1.2.3): 2 of 3 attached: restore the rest" in result.output
        assert "or re-index it" in result.output
        doc = _doc(_run(runner, _Stub([_page(rows)]), "--chash", a, "--chash", b, "--json").stdout)
        assert doc["owners"] == [{"owner": "1.2.3", "title": "Legacy Note", "attached": 2,
                                  "manifest_rows": 2, "chunk_count": 3}]
        assert doc["partial_owners"] == doc["owners"]

        whole = _run(runner, _Stub([_page([_attached_row(a, owner_rows=3, owner_chunks=3)])]), "--chash", a)
        assert "attached: restore the rest" not in whole.output, "3 of 3 is not partial"

    def test_the_owner_count_is_the_one_from_the_last_page_that_attached_to_it(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        stub = _Stub([
            _page([_attached_row(a, position=0, owner_rows=1, owner_chunks=3)], next_after=a),
            _page([_attached_row(b, position=1, owner_rows=2, owner_chunks=3)]),
        ])

        result = _run(runner, stub, "--quarantined-since", "2026-09-01", "--json")

        doc = _doc(result.stdout)
        assert doc["owners"] == [{"owner": "1.2.3", "title": "Legacy Note", "attached": 2,
                                  "manifest_rows": 2, "chunk_count": 3}], "the later page saw the later state"

    def test_an_unexpected_failure_on_a_later_page_still_prints_the_report_not_a_traceback(self, runner) -> None:
        hs = [_chash(str(i)) for i in range(1100)]
        first = _page([_attached_row(h) for h in hs[:1000]], audit_id=79)
        stub = _Stub([first], error=RuntimeError("decoder blew up"), error_after=1)

        result = _run(runner, stub, *[x for h in hs for x in ("--chash", h)])

        assert result.exit_code == t3_quarantine.EXIT_ENGINE_ERROR, result.output[-500:]
        assert "gc_audit 79" in result.output and "restored 1000" in result.output, "the committed page survives"
        assert "RuntimeError" in result.output and "decoder blew up" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit), "no traceback"


# ── the verb against the real engine ─────────────────────────────────────────


def _seed_quarantined(tenant: str, origin: str, seeds: list[str],
                      metas: list[dict] | None = None) -> tuple[list[str], str]:
    """Ownerless chunks in *origin*, aged past the grace, moved into quarantine by the engine's own
    bounded sweep (the route ``nx index repo`` and ``nx t3 gc`` use). Returns (chashes, sibling)."""
    from nexus.catalog.chunk_quarantine import now_stamp, quarantine_collection_name
    from tests._chunk_seed import seed_chunks_direct
    from tests._reapable_age import age_chunks_past_grace

    hs = [_chash(f"{origin}/{s}") for s in seeds]
    metas = metas or [{"title": s} for s in seeds]
    seed_chunks_direct(origin, hs, [f"{s} text" for s in seeds], metas, tenant=tenant)
    age_chunks_past_grace(origin, tenant=tenant)
    sibling = quarantine_collection_name(origin)
    result = HttpVectorClient().gc_quarantine_orphans_bounded(origin, sibling, now_stamp(), 20, 1000)
    assert result["moved"] == len(seeds), result
    return hs, sibling


def test_restore_by_chash_against_the_real_engine(runner: CliRunner, t2_service_env) -> None:
    origin = "knowledge__qrestore-chash__bge-base-en-v15-768__v1"
    hs, sibling = _seed_quarantined(t2_service_env, origin, ["one", "two"])
    nowhere = _chash("never-existed")

    result = runner.invoke(t3, ["quarantine", "restore", "--collection", origin,
                                "--chash", hs[0], "--chash", nowhere, "--json"])

    assert result.exit_code == 1, result.output   # one chash was missing
    doc = _doc(result.stdout)
    assert doc["totals"]["restored"] == 1 and doc["totals"]["missing"] == 1
    by = {r["chash"]: r for r in doc["rows"]}
    assert by[hs[0]]["outcome"] == "restored" and by[hs[0]]["no_manifest"] is True
    assert by[hs[0]]["reapable_after"], "a restored manifest-less chunk carries the date it is reapable again"
    assert by[nowhere]["outcome"] == "missing"
    # Moved, not copied: a second restore finds it in the origin (the sibling no longer holds it).
    again = runner.invoke(t3, ["quarantine", "restore", "--collection", origin, "--chash", hs[0], "--json"])
    assert _doc(again.stdout)["totals"]["present"] == 1, "a second restore is an idempotent report"


def test_restore_by_quarantined_at_window_and_the_sample_audit_row_is_refused(
    runner: CliRunner, t2_service_env,
) -> None:
    origin = "knowledge__qrestore-window__bge-base-en-v15-768__v1"
    hs, sibling = _seed_quarantined(t2_service_env, origin, ["w1", "w2", "w3"])

    # nx index repo / nx t3 gc quarantine through gc_quarantine_orphans, whose gc_audit row is a SAMPLE.
    from nexus.catalog.factory import make_catalog_reader
    entries = make_catalog_reader().gc_audit_list(operation="gc_quarantine_orphans_bounded", collection=origin)
    assert entries, "the engine's bounded sweep wrote a gc_audit row"
    sample_id = entries[0]["id"]

    refused = runner.invoke(t3, ["quarantine", "restore", "--collection", origin, "--audit-id", str(sample_id)])
    assert refused.exit_code == t3_quarantine.EXIT_ENGINE_ERROR, refused.output
    assert "sample" in refused.output and "quarantined-since" in refused.output

    dry = runner.invoke(t3, ["quarantine", "restore", "--collection", origin,
                             "--quarantined-since", "2020-01-01", "--dry-run", "--json"])
    assert dry.exit_code == t3_quarantine.EXIT_HIDDEN, "keyless chunks would stay hidden: " + dry.output
    assert _doc(dry.stdout)["totals"]["would_restore"] == 3
    assert _doc(dry.stdout)["hidden"] == 3

    done = runner.invoke(t3, ["quarantine", "restore", "--collection", origin,
                              "--quarantined-since", "2020-01-01", "--json"])
    assert done.exit_code == t3_quarantine.EXIT_HIDDEN, "restored, and hidden from search and get: " + done.output
    doc = _doc(done.stdout)
    assert doc["totals"]["restored"] == 3 and doc["hidden"] == 3
    assert sorted(r["chash"] for r in doc["rows"]) == sorted(hs)
    assert len(doc["audit_ids"]) == 1


def _visible(origin: str, chash: str) -> bool:
    """Whether the engine's store-get returns the chunk: live(c) hides one no live manifest row names."""
    return HttpVectorClient().get_by_id(origin, chash) is not None


def test_reattach_makes_a_restored_chunk_visible_again_against_the_real_engine(
    runner: CliRunner, t2_service_env,
) -> None:
    origin = "knowledge__qrestore-reattach__bge-base-en-v15-768__v1"
    doc = register_real_doc_id(title="Legacy Note", physical_collection=origin, owner_name="qrestore-reattach-owner")
    hs, _ = _seed_quarantined(
        t2_service_env, origin, ["named", "stranger"],
        metas=[{"title": "Legacy Note", "catalog_doc_id": doc, "chunk_index": 0}, {"title": "Stranger"}])
    assert not _visible(origin, hs[0]), "fixture: in quarantine, so not in the collection"

    result = runner.invoke(t3, ["quarantine", "restore", "--collection", origin,
                                "--chash", hs[0], "--chash", hs[1], "--json"])

    assert result.exit_code == t3_quarantine.EXIT_HIDDEN, "one chunk stays hidden: exit 3\n" + result.output
    doc_out = _doc(result.stdout)
    assert doc_out["hidden"] == 1
    by = {r["chash"]: r for r in doc_out["rows"]}
    named, stranger = by[hs[0]], by[hs[1]]
    assert named["attached"] is True and named["reattach"] == "attach"
    assert named["owner"] == doc and named["owner_title"] == "Legacy Note" and named["position"] == 0
    assert named["no_manifest"] is False
    assert named["owner_rows"] == 1, "the engine reports the owner's manifest rows after the attach"
    assert named["reason"] is None
    assert stranger["attached"] is False and stranger["reattach"] == "no_live_owner"
    assert stranger["no_manifest"] is True and stranger["chunk_title"] == "Stranger"
    assert doc_out["reattach_totals"]["attached"] == 1 and doc_out["reattach_totals"]["no_live_owner"] == 1
    # The point: the attached chunk is back to the read path, the one with no owner is bytes only.
    assert _visible(origin, hs[0]), "the attached chunk is returned by get"
    assert not _visible(origin, hs[1]), "a chunk with no live owner is restored but stays hidden"

    # And the text output says so, with the re-put recipe for the hidden one.
    text = runner.invoke(t3, ["quarantine", "restore", "--collection", origin, "--chash", hs[1]])
    assert "HIDDEN from search and get" in text.output
    assert f"nx store put - --collection {origin} --title Stranger" in text.output


def test_no_reattach_against_the_real_engine_then_a_rerun_attaches(runner: CliRunner, t2_service_env) -> None:
    origin = "knowledge__qrestore-noreattach__bge-base-en-v15-768__v1"
    doc = register_real_doc_id(title="Legacy Note", physical_collection=origin, owner_name="qrestore-noreattach-owner")
    hs, _ = _seed_quarantined(
        t2_service_env, origin, ["bytes"], metas=[{"title": "Legacy Note", "catalog_doc_id": doc, "chunk_index": 0}])

    first = runner.invoke(t3, ["quarantine", "restore", "--collection", origin, "--chash", hs[0],
                               "--no-reattach", "--json"])
    assert first.exit_code == t3_quarantine.EXIT_HIDDEN, "bytes-only leaves it hidden: exit 3\n" + first.output
    row = _doc(first.stdout)["rows"][0]
    assert row["outcome"] == "restored" and row["attached"] is False and row["reattach"] == "attach"
    assert not _visible(origin, hs[0])

    second = runner.invoke(t3, ["quarantine", "restore", "--collection", origin, "--chash", hs[0], "--json"])
    assert second.exit_code == 0, "attached: everything requested is visible\n" + second.output
    row = _doc(second.stdout)["rows"][0]
    assert row["outcome"] == "present" and row["attached"] is True
    assert _visible(origin, hs[0])
