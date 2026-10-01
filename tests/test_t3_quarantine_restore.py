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


def _row(chash: str, outcome: str, *, no_manifest=None, reapable_after=None) -> dict:
    return {"chash": chash, "outcome": outcome, "no_manifest": no_manifest, "reapable_after": reapable_after}


def _page(rows, *, audit_id=None, dry_run=False, source=None, next_after=None) -> dict:
    counts = {k: sum(1 for r in rows if r["outcome"] == k)
              for k in ("restored", "would_restore", "present", "dim_conflict", "missing")}
    return {"origin_collection": ORIGIN, "quarantine_collection": SIBLING, "dry_run": dry_run,
            "audit_id": audit_id, **counts, "rows": rows, "source": source, "next_after": next_after}


class _Stub:
    def __init__(self, pages=None, error: Exception | None = None):
        self.pages = list(pages or [])
        self.error = error
        self.calls: list[dict] = []

    def gc_quarantine_restore(self, origin, quarantine, **kw):
        assert origin == ORIGIN and quarantine == SIBLING
        self.calls.append(kw)
        if self.error is not None:
            raise self.error
        return self.pages.pop(0)


def _run(runner: CliRunner, stub: _Stub, *args: str):
    with patch.object(t3_quarantine, "_make_t3", return_value=stub), \
         patch.object(t3_quarantine, "_quarantine_name", return_value=SIBLING):
        return runner.invoke(t3, ["quarantine", "restore", "--collection", ORIGIN, *args])


class TestCli:
    def test_the_table_the_totals_and_the_date_a_manifestless_chunk_is_reapable_again(self, runner) -> None:
        a, b, c = _chash("a"), _chash("b"), _chash("c")
        stub = _Stub([_page([
            _row(a, "restored", no_manifest=True, reapable_after="2026-10-31T12:00:00Z"),
            _row(b, "present"),
            _row(c, "missing"),
        ], audit_id=41)])

        result = _run(runner, stub, "--chash", a, "--chash", b, "--chash", c)

        out = result.output
        assert result.exit_code == 1, "a requested chash that is missing is a failed restore: exit 1\n" + out
        assert f"{a}  restored" in out and f"{b}  present" in out and f"{c}  missing" in out
        assert "restored 1, present 1, dim_conflict 0, missing 1" in out
        assert "gc_audit 41" in out
        # The operator is told, not left to find out: no manifest row means the reaper takes it again.
        assert "no manifest row" in out and "2026-10-31T12:00:00Z" in out
        assert "reaper" in out and "owner row" in out
        assert stub.calls[0]["chashes"] == [a, b, c]

    def test_a_clean_restore_exits_zero_and_names_what_it_did(self, runner) -> None:
        a = _chash("a")
        stub = _Stub([_page([_row(a, "restored", no_manifest=True, reapable_after="2026-10-31T00:00:00Z")], audit_id=3)])
        result = _run(runner, stub, "--chash", a)
        assert result.exit_code == 0, result.output
        assert "restored 1" in result.output

    def test_a_dry_run_says_nothing_moved_and_passes_dry_run_through(self, runner) -> None:
        a = _chash("a")
        stub = _Stub([_page([_row(a, "would_restore")], dry_run=True)])
        result = _run(runner, stub, "--chash", a, "--dry-run")
        assert result.exit_code == 0, result.output
        assert stub.calls[0]["dry_run"] is True
        assert "dry run" in result.output.lower() and "would restore 1" in result.output
        assert "reapable again" not in result.output

    def test_chashes_go_in_batches_of_a_thousand_and_are_deduplicated(self, runner) -> None:
        hs = [_chash(str(i)) for i in range(2500)]
        stub = _Stub([_page([_row(h, "restored", no_manifest=True, reapable_after="2026-10-31T00:00:00Z")
                             for h in hs[i:i + 1000]], audit_id=i) for i in (0, 1000, 2000)])
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
            _page([_row(a, "restored", no_manifest=True, reapable_after="2026-10-31T00:00:00Z")], next_after=a),
            _page([_row(b, "restored", no_manifest=True, reapable_after="2026-10-31T00:00:00Z")]),
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
            _page([_row(a, "restored", no_manifest=True, reapable_after="2026-10-31T00:00:00Z")], source=src(1)),
            _page([_row(b, "restored", no_manifest=True, reapable_after="2026-10-31T00:00:00Z")], source=src(None)),
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

    def test_json_is_one_parseable_document_with_every_row(self, runner) -> None:
        a, b = _chash("a"), _chash("b")
        stub = _Stub([_page([
            _row(a, "restored", no_manifest=True, reapable_after="2026-10-31T00:00:00Z"), _row(b, "present"),
        ], audit_id=5)])
        result = _run(runner, stub, "--chash", a, "--chash", b, "--json")
        assert result.exit_code == 0, result.output
        doc = _doc(result.stdout)
        assert doc["origin_collection"] == ORIGIN and doc["quarantine_collection"] == SIBLING
        assert doc["totals"] == {"restored": 1, "would_restore": 0, "present": 1, "dim_conflict": 0, "missing": 0}
        assert doc["audit_ids"] == [5]
        assert [r["outcome"] for r in doc["rows"]] == ["restored", "present"]
        assert doc["reapable_again_after"] == "2026-10-31T00:00:00Z"

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


# ── the verb against the real engine ─────────────────────────────────────────


def _seed_quarantined(tenant: str, origin: str, seeds: list[str]) -> tuple[list[str], str]:
    """Ownerless chunks in *origin*, aged past the grace, moved into quarantine by the engine's own
    bounded sweep (the route ``nx index repo`` and ``nx t3 gc`` use). Returns (chashes, sibling)."""
    from nexus.catalog.chunk_quarantine import now_stamp, quarantine_collection_name
    from tests._chunk_seed import seed_chunks_direct
    from tests._reapable_age import age_chunks_past_grace

    hs = [_chash(f"{origin}/{s}") for s in seeds]
    seed_chunks_direct(origin, hs, [f"{s} text" for s in seeds], [{"title": s} for s in seeds], tenant=tenant)
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
    assert dry.exit_code == 0, dry.output
    assert _doc(dry.stdout)["totals"]["would_restore"] == 3

    done = runner.invoke(t3, ["quarantine", "restore", "--collection", origin,
                              "--quarantined-since", "2020-01-01", "--json"])
    assert done.exit_code == 0, done.output
    doc = _doc(done.stdout)
    assert doc["totals"]["restored"] == 3
    assert sorted(r["chash"] for r in doc["rows"]) == sorted(hs)
    assert len(doc["audit_ids"]) == 1
