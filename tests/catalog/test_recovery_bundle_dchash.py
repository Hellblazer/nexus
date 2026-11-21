# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 Step 4 (nexus-qrg1r.7): the recovery import refuses a malformed ``dchash:`` span.

``tests/catalog/test_recovery_bundle.py`` is over its test-file cap, so these live here and import its fake catalog
server and helpers. The import must not abort: the link is recorded in ``ImportSummary.links_refused_span`` and the
rest go on. The ``nx catalog import`` exit status still counts documents only. No tenant is minted.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import nexus.catalog.recovery_bundle as rb
from nexus.catalog.recovery_bundle import ImportSummary, export_bundle, import_bundle
from nexus.cli import main
from tests.catalog.test_recovery_bundle import (  # noqa: F401 — fake_catalog is a fixture, used by name
    CH_A,
    _client,
    _FakeT3,
    _stub_import_doc,
    fake_catalog,
)

pytestmark = pytest.mark.usefixtures("rowless_collection_cache")  # RDR-229 P2.9: a tenant-free test takes item 1's NoRow outcome without a listing

H = "cd" * 32
BAD_SPAN = f"dchash:{H}:9-4"


def _link(frm: str, to: str, link_type: str, **spans: str) -> dict:
    return {"from_tumbler": frm, "to_tumbler": to, "link_type": link_type, "from_span": "", "to_span": "",
            "created_by": "u", "created_at": "", **spans}


class _SpyWriter:
    """Delegates to the real client and records each ``link`` call."""

    def __init__(self, client) -> None:
        self._c = client
        self.links: list[tuple] = []

    def link(self, *args, **kwargs):
        self.links.append((args, kwargs))
        return self._c.link(*args, **kwargs)


def _import(url, state, tmp_path):
    bundle = tmp_path / "b.jsonl"
    with _client(url) as reader:
        export_bundle(reader, _FakeT3(), bundle)
    with _client(url) as client:
        spy = _SpyWriter(client)
        summary = import_bundle(client, spy, _FakeT3(), bundle, import_doc=_stub_import_doc([], state))
    return summary, spy


def test_a_link_with_a_malformed_dchash_span_is_refused_and_the_others_import(fake_catalog, tmp_path):
    url, state = fake_catalog
    state.links = [
        _link("1.9.1", "1.9.6", "cites", from_span=BAD_SPAN),
        _link("1.9.1", "1.9.2", "relates", to_span=f"chash:{CH_A}"),
        _link("1.9.2", "1.9.6", "relates", from_span=f"dchash:{H}:2-9"),
    ]
    summary, spy = _import(url, state, tmp_path)

    assert len(spy.links) == 2
    assert summary.links_created + summary.links_merged == 2
    assert len(summary.links_refused_span) == 1
    row = summary.links_refused_span[0]
    assert set(row) == {"from_uri", "to_uri", "link_type", "span", "error"}
    assert row["span"] == BAD_SPAN
    assert row["link_type"] == "cites"
    assert row["from_uri"] == "nx-note://knowledge/title-a"
    assert row["to_uri"] == "file:///repo/src/alpha.py"
    assert BAD_SPAN in row["error"]
    # a refusal is not a document failure: the exit-status driver is untouched
    assert summary.docs_unverified == 0
    assert summary.unresolvable_links == []
    # the refused link never reached the engine
    assert [c["link_type"] for c in state.created_links] == ["relates", "relates"]


def test_a_malformed_to_span_is_refused_too_and_names_that_span(fake_catalog, tmp_path):
    url, state = fake_catalog
    bad = f"dchash:{H}:01-3"
    state.links = [_link("1.9.1", "1.9.6", "cites", to_span=bad)]
    summary, spy = _import(url, state, tmp_path)
    assert spy.links == []
    assert [r["span"] for r in summary.links_refused_span] == [bad]


def test_a_bundle_with_only_good_spans_refuses_nothing(fake_catalog, tmp_path):
    url, state = fake_catalog
    state.links = [_link("1.9.1", "1.9.6", "cites", from_span=f"dchash:{H}:2-9")]
    summary, spy = _import(url, state, tmp_path)
    assert len(spy.links) == 1
    assert summary.links_refused_span == []


def test_the_imported_log_carries_the_refused_count(fake_catalog, tmp_path, monkeypatch):
    url, state = fake_catalog
    state.links = [_link("1.9.1", "1.9.6", "cites", from_span=BAD_SPAN)]
    events: list[tuple[str, dict]] = []

    class _Log:
        def info(self, event, **kw): events.append((event, kw))
        def warning(self, event, **kw): events.append((event, kw))

    monkeypatch.setattr(rb, "_log", _Log())
    _import(url, state, tmp_path)
    by_event = dict(events)
    assert by_event["recovery_bundle_imported"]["links_refused_span"] == 1
    assert by_event["recovery_import_link_span_refused"]["span"] == BAD_SPAN


def test_the_cli_prints_the_refused_count_and_one_line_per_record_and_exits_zero(tmp_path, monkeypatch):
    bundle = tmp_path / "recovery.jsonl"
    bundle.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n")
    summary = ImportSummary(
        docs_imported=2, links_created=1,
        links_refused_span=[{
            "from_uri": "nx-note://k/a", "to_uri": "file:///x.py", "link_type": "cites",
            "span": BAD_SPAN, "error": f"malformed dchash span (start must be below end): {BAD_SPAN!r}",
        }],
    )
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.import_bundle", lambda r, w, t, p: summary)
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())

    result = CliRunner().invoke(main, ["catalog", "import", str(bundle)])
    assert result.exit_code == 0, result.output
    assert "links refused (malformed dchash span): 1" in result.output
    assert "nx-note://k/a -[cites]-> file:///x.py" in result.output
    assert BAD_SPAN in result.output


def test_the_cli_prints_no_refused_block_when_none_were_refused(tmp_path, monkeypatch):
    bundle = tmp_path / "recovery.jsonl"
    bundle.write_text(json.dumps({"format": "nexus-recovery-bundle", "format_version": 1}) + "\n")
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: object())
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: object())
    monkeypatch.setattr("nexus.catalog.recovery_bundle.import_bundle", lambda r, w, t, p: ImportSummary())
    monkeypatch.setattr("nexus.db.make_t3", lambda: object())
    result = CliRunner().invoke(main, ["catalog", "import", str(bundle)])
    assert "links refused" not in result.output
