# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P1.2 (nexus-p2m1x.2), deliverable B: the ``uri_residue`` doctor row, the residue pre-flight.

Counts, on ``catalog_documents``, ``document_aspects`` and ``document_highlights``: the ``chroma://`` rows whose
segment has no ``rdr__``/``docs__``/``code__`` prefix, split by whether the row's collection has content type code,
docs or rdr; among those the rows whose segment is not their own collection; and the ``nx-note://`` rows of a
file-typed collection whose segment has no file-routed prefix. The engine groups the rows
(``GET /v1/catalog/uri_segment_census``); the client classifies them with ``aspect_readers.FILE_ROUTED_PREFIXES``.

The finding logic runs on a fake reader of the route. The reader never offers a catalog walk: any other attribute
raises, so a row that walked the catalog would fail loudly here (RDR-241).
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.aspect_readers import FILE_ROUTED_PREFIXES
from nexus.catalog.rdr229 import uri_residue_doctor as ur
from nexus.catalog.engine_reads import uri_segments
from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable
from nexus.cli import main
from nexus.commands import doctor as doctor_mod

DOCS = "docs__1-2__minilm-l6-v2-384__v1"
CODE = "code__1-3__bge-base-en-v15-768__v1"
KNOW = "knowledge__sub__minilm-l6-v2-384__v1"


def g(table: str, scheme: str, segment: str, collection: str, content_type: str, rows: int) -> dict:
    return {"table": table, "scheme": scheme, "segment": segment, "collection": collection,
            "content_type": content_type, "rows": rows}


#: Every attribute a reader double was asked for besides the census read. A walk of the catalog would show here even if
#: the row's own code swallowed the error the double raised (a broad ``except`` cannot hide a recorded read).
WALKED: list[str] = []


@pytest.fixture(autouse=True)
def _no_walk():
    WALKED.clear()
    yield
    assert WALKED == [], f"the row read {WALKED} besides the one census read: it must never walk the catalog"


class _Reader:
    """The one read a row may make. Any other attribute read is recorded in :data:`WALKED` and raises."""

    def __init__(self, groups=(), *, capped=False, raises=None):
        self._groups, self._capped, self._raises = list(groups), capped, raises
        self.calls = 0

    def uri_segment_census(self, limit: int = 10_000) -> dict:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return {"count": len(self._groups), "limit": limit, "capped": self._capped, "groups": self._groups}

    def __getattr__(self, name):
        WALKED.append(name)
        raise AttributeError(name)


# ── the verdict on fixtures: one row of every shape the RDR names ───────────────────────────────────────────────


def _fixture() -> list[dict]:
    return [
        # prefix-free chroma:// segment, file-typed collection, segment IS its collection (the shape P2.10 repairs)
        g("catalog_documents", "chroma", "mylabel", "mylabel", "docs", 5),
        # prefix-free chroma:// segment, file-typed collection, segment is NOT its collection (no safe rule)
        g("catalog_documents", "chroma", "oldlabel", "mylabel", "docs", 2),
        g("document_aspects", "chroma", "mylabel", "mylabel", "docs", 3),
        g("document_highlights", "chroma", "slug", "slug", "code", 1),
        # prefix-free chroma:// segment on a knowledge collection: a legacy note, not residue of a file type
        g("catalog_documents", "chroma", KNOW, KNOW, "knowledge", 7),
        # a file-routed segment: the residue backfill-source-uri already handles; never counted as prefix-free
        g("catalog_documents", "chroma", DOCS, DOCS, "docs", 11),
        # nx-note:// in a file-typed collection with a prefix-free segment (note-uri-001 rewrote residue to this)
        g("catalog_documents", "nx-note", "mylabel", "mylabel", "docs", 4),
        g("catalog_documents", "nx-note", "elsewhere", "mylabel", "docs", 1),
        # nx-note:// in a knowledge collection: an ordinary note
        g("catalog_documents", "nx-note", KNOW, KNOW, "knowledge", 100),
        # nx-note:// whose segment has a file-routed prefix: not counted
        g("catalog_documents", "nx-note", CODE, CODE, "code", 9),
        # a row whose collection has no registry row: content type unknown, so not file-typed
        g("document_highlights", "chroma", "ghost", "ghost", "", 6),
    ]


def test_counts_every_shape_the_rdr_names_per_table() -> None:
    report = ur.check_uri_residue(reader=_Reader(_fixture()))
    assert report["status"] == ur.WARN
    docs, asp, hl = (report["counts"][t] for t in ("catalog_documents", "document_aspects", "document_highlights"))
    assert docs["chroma_prefix_free"]["file_typed"] == {"rows": 7, "segment_not_collection": 2}
    assert docs["chroma_prefix_free"]["other"] == {"rows": 7, "segment_not_collection": 0}
    assert docs["note_prefix_free_in_file_typed"] == {"rows": 5, "segment_not_collection": 1}
    assert docs["chroma_file_routed_segment"] == 11
    assert asp["chroma_prefix_free"]["file_typed"] == {"rows": 3, "segment_not_collection": 0}
    assert asp["note_prefix_free_in_file_typed"] == {"rows": 0, "segment_not_collection": 0}
    assert hl["chroma_prefix_free"]["file_typed"] == {"rows": 1, "segment_not_collection": 0}
    assert hl["chroma_prefix_free"]["other"] == {"rows": 6, "segment_not_collection": 0}, "no registry row: not file-typed"
    totals = report["totals"]
    assert totals["chroma_prefix_free_file_typed"] == {"rows": 11, "segment_not_collection": 2}
    assert totals["note_prefix_free_in_file_typed"] == {"rows": 5, "segment_not_collection": 1}
    assert report["rows_examined"] == sum(x["rows"] for x in _fixture())


@pytest.mark.parametrize("prefix", FILE_ROUTED_PREFIXES)
def test_the_prefix_test_is_aspect_readers_not_a_copy(prefix: str) -> None:
    """Each prefix of ``FILE_ROUTED_PREFIXES`` makes a segment file-routed, so it is never counted as prefix-free."""
    seg = f"{prefix}1-9__bge-base-en-v15-768__v1"
    groups = [g("catalog_documents", "chroma", seg, seg, prefix.rstrip("_"), 3),
              g("catalog_documents", "nx-note", seg, seg, prefix.rstrip("_"), 3)]
    report = ur.check_uri_residue(reader=_Reader(groups))
    assert report["status"] == ur.PASS
    assert report["totals"]["chroma_prefix_free_file_typed"]["rows"] == 0
    assert report["totals"]["note_prefix_free_in_file_typed"]["rows"] == 0
    assert report["counts"]["catalog_documents"]["chroma_file_routed_segment"] == 3


def test_the_file_typed_content_types_come_from_the_file_routed_prefixes() -> None:
    """A content type is file-typed exactly when ``<content_type>__`` is one of FILE_ROUTED_PREFIXES."""
    assert {p[:-2] for p in FILE_ROUTED_PREFIXES} == set(ur.FILE_TYPED_CONTENT_TYPES)
    for ct in ("code", "docs", "rdr"):
        r = ur.check_uri_residue(reader=_Reader([g("catalog_documents", "chroma", "x", "x", ct, 1)]))
        assert r["totals"]["chroma_prefix_free_file_typed"]["rows"] == 1, ct
    r = ur.check_uri_residue(reader=_Reader([g("catalog_documents", "chroma", "x", "x", "knowledge", 1)]))
    assert r["totals"]["chroma_prefix_free_file_typed"]["rows"] == 0


# ── pass, not applicable, not checked: a row that examined nothing is never a pass ───────────────────────────────


def test_pass_names_what_it_examined() -> None:
    report = ur.check_uri_residue(reader=_Reader([g("catalog_documents", "nx-note", KNOW, KNOW, "knowledge", 40)]))
    assert report["status"] == ur.PASS
    assert report["rows_examined"] == 40
    assert "40" in report["reason"]


def test_a_virgin_catalog_is_not_applicable_not_a_pass() -> None:
    report = ur.check_uri_residue(reader=_Reader([]))
    assert report["status"] == ur.NOT_APPLICABLE
    assert not ur.is_not_checked(report), "it looked and found no row to classify"


def test_an_engine_without_the_route_is_not_checked_and_names_the_engine() -> None:
    exc = EngineRouteUnavailable("/uri_segment_census", ur.FLAG, EngineRouteUnavailable.UNSUPPORTED)
    report = ur.check_uri_residue(reader=_Reader(raises=exc))
    assert report["status"] == ur.NOT_APPLICABLE and ur.is_not_checked(report)
    assert "0.1.200" in report["reason"], "the reason says which engine carries the route"


@pytest.mark.parametrize("reason", [EngineRouteUnavailable.TIMEOUT, EngineRouteUnavailable.FAILED,
                                    EngineRouteUnavailable.MALFORMED, EngineRouteUnavailable.PROBE_FAILED])
def test_any_other_failure_is_not_checked(reason: str) -> None:
    report = ur.check_uri_residue(reader=_Reader(raises=EngineRouteUnavailable("/uri_segment_census", ur.FLAG, reason)))
    assert report["status"] == ur.NOT_APPLICABLE and ur.is_not_checked(report)


def test_a_capped_answer_is_not_checked_because_its_counts_would_be_lower_bounds() -> None:
    report = ur.check_uri_residue(reader=_Reader(_fixture(), capped=True))
    assert report["status"] == ur.NOT_APPLICABLE and ur.is_not_checked(report)


def test_a_reader_without_the_method_is_not_checked_and_never_walks() -> None:
    class Old:
        def all_documents(self, *a, **k):  # pragma: no cover - must not be reached
            WALKED.append("all_documents")
            return []

        def list_scoped(self, *a, **k):  # pragma: no cover - must not be reached
            WALKED.append("list_scoped")
            return []

    report = ur.check_uri_residue(reader=Old())
    assert report["status"] == ur.NOT_APPLICABLE and ur.is_not_checked(report)


def test_a_reader_whose_attribute_read_raises_is_not_checked_and_never_raises() -> None:
    """The production handle builds its client on the first attribute read and re-raises a failed build (not an
    AttributeError), so a plain ``getattr(..., default)`` would crash ``nx doctor``."""

    class Failing:
        def __getattr__(self, name):
            raise RuntimeError("service client could not be built")

    report = ur.check_uri_residue(reader=Failing())
    assert report["status"] == ur.NOT_APPLICABLE and ur.is_not_checked(report)


def test_no_catalog_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: None)
    report = ur.check_uri_residue()
    assert report["status"] == ur.NOT_APPLICABLE and ur.is_not_checked(report)


# ── gating decision: the row warns and never fails ──────────────────────────────────────────────────────────────


def test_the_row_never_reports_a_fail_and_is_not_a_gating_row() -> None:
    """RDR-229 calls it a pre-flight, but P2.10's repair move is what clears it, so a FAIL would exit ``nx doctor``
    non-zero on installs that cannot yet clear it. The decision is :data:`doctor._GATING_SUPPLEMENTARY_CHECKS`."""
    assert "uri-residue" not in doctor_mod._GATING_SUPPLEMENTARY_CHECKS
    assert ur.check_uri_residue(reader=_Reader(_fixture()))["status"] == ur.WARN
    assert not hasattr(ur, "GATES")


def test_the_other_bucket_is_keyed_by_the_engines_content_type_so_attribute_less_rows_show() -> None:
    groups = [g("catalog_documents", "chroma", KNOW, KNOW, "knowledge", 7),
              g("catalog_documents", "chroma", "ghost", "ghost", "", 6),
              g("document_highlights", "chroma", "other", "elsewhere", "unknown", 2)]
    report = ur.check_uri_residue(reader=_Reader(groups))
    by_type = report["counts"]["catalog_documents"]["chroma_prefix_free"]["other_by_content_type"]
    assert by_type == {"knowledge": {"rows": 7, "segment_not_collection": 0},
                       ur.NO_CONTENT_TYPE: {"rows": 6, "segment_not_collection": 0}}
    assert report["totals"]["chroma_prefix_free_other_by_content_type"] == {
        "knowledge": {"rows": 7, "segment_not_collection": 0},
        ur.NO_CONTENT_TYPE: {"rows": 6, "segment_not_collection": 0},
        "unknown": {"rows": 2, "segment_not_collection": 2}}
    text = "\n".join(ur.render_text(report))
    assert ur.NO_CONTENT_TYPE in text and "knowledge" in text


def test_the_unsupported_text_names_the_edge_allowlist_for_a_cloud_client() -> None:
    exc = EngineRouteUnavailable("/uri_segment_census", ur.FLAG, EngineRouteUnavailable.UNSUPPORTED)
    reason = ur.check_uri_residue(reader=_Reader(raises=exc))["reason"]
    assert "0.1.200" in reason and "conexus edge" in reason and ur.FLAG in reason


# ── rendering ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_render_names_the_counts_and_the_split() -> None:
    lines = ur.render_text(ur.check_uri_residue(reader=_Reader(_fixture())))
    text = "\n".join(lines)
    assert lines[0].startswith("uri-residue: WARN")
    assert "catalog_documents" in text and "document_aspects" in text and "document_highlights" in text
    assert "segment is not its collection" in text
    assert "nx-note://" in text
    assert "P2.10" in text, "names the repair that clears it"


def test_a_pass_render_prints_the_other_bucket_and_the_file_routed_total_too() -> None:
    groups = [g("catalog_documents", "chroma", KNOW, KNOW, "knowledge", 40),
              g("catalog_documents", "chroma", DOCS, DOCS, "docs", 11)]
    report = ur.check_uri_residue(reader=_Reader(groups))
    assert report["status"] == ur.PASS
    lines = ur.render_text(report)
    text = "\n".join(lines)
    assert lines[0].startswith("uri-residue: PASS")
    assert "any other collection: 40" in text and "file-routed segment" in text and ": 11" in text


def test_render_not_checked_says_why() -> None:
    exc = EngineRouteUnavailable("/uri_segment_census", ur.FLAG, EngineRouteUnavailable.TIMEOUT)
    lines = ur.render_text(ur.check_uri_residue(reader=_Reader(raises=exc)))
    assert "not checked" in lines[0]


# ── the two surfaces ────────────────────────────────────────────────────────────────────────────────────────────


def test_nx_catalog_doctor_flag_prints_the_row_and_exits_zero_on_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: _Reader(_fixture()))
    result = CliRunner().invoke(main, ["catalog", "doctor", "--uri-residue"])
    assert result.exit_code == 0, result.output
    assert "uri-residue: WARN" in result.output


def test_nx_catalog_doctor_flag_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: _Reader(_fixture()))
    result = CliRunner().invoke(main, ["catalog", "doctor", "--uri-residue", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["uri_residue"]["status"] == "warn"


def test_nx_doctor_supplementary_sweep_carries_the_row(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    assert "uri-residue" in doctor_mod._SUPPLEMENTARY_CHECK_NAMES
    assert "uri-residue" not in doctor_mod._GATING_SUPPLEMENTARY_CHECKS
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: _Reader(_fixture()))
    doctor_mod._run_check_uri_residue()
    assert "uri-residue: WARN" in capsys.readouterr().out


# ── the reader of the route (engine_reads/uri_segments.py) ──────────────────────────────────────────────────────


class _Client:
    def __init__(self, flags=None, body=None, probe_failed=False):
        self._flags, self._body, self._version_probe_failed = flags or {}, body, probe_failed
        self.gets: list = []

    def _version_flags(self):
        return self._flags

    def _get(self, route, **kw):
        self.gets.append((route, kw))
        return self._body


def _ok_body(groups=None, **over):
    groups = [g("catalog_documents", "chroma", "x", "x", "docs", 2)] if groups is None else groups
    return {"count": len(groups), "limit": 10_000, "capped": False, "groups": groups, **over}


def test_reader_sends_nothing_without_the_capability_flag() -> None:
    client = _Client(flags={})
    with pytest.raises(EngineRouteUnavailable) as e:
        uri_segments.read_uri_segment_census(client)
    assert e.value.reason == EngineRouteUnavailable.UNSUPPORTED
    assert client.gets == [], "an engine without the flag is never asked"


def test_reader_returns_a_well_formed_body() -> None:
    client = _Client(flags={uri_segments.FLAG: True}, body=_ok_body())
    body = uri_segments.read_uri_segment_census(client)
    assert body["groups"][0]["rows"] == 2 and client.gets[0][0] == uri_segments.ROUTE


@pytest.mark.parametrize("bad", [
    {"count": 1, "limit": 10, "capped": False, "groups": [{"table": "x"}]},
    {"count": 2, "limit": 10, "capped": False, "groups": []},
    {"count": 1, "limit": 10, "capped": "no", "groups": []},
    "not a dict",
    {"count": 1, "limit": 10, "capped": False,
     "groups": [{"table": "catalog_documents", "scheme": "ftp", "segment": "x", "collection": "x",
                 "content_type": "", "rows": 1}]},
    {"count": 1, "limit": 10, "capped": False,
     "groups": [{"table": "catalog_documents", "scheme": "chroma", "segment": "x", "collection": "x",
                 "content_type": "", "rows": 0}]},
])
def test_reader_refuses_a_malformed_body(bad) -> None:
    with pytest.raises(EngineRouteUnavailable) as e:
        uri_segments.read_uri_segment_census(_Client(flags={uri_segments.FLAG: True}, body=bad))
    assert e.value.reason == EngineRouteUnavailable.MALFORMED
