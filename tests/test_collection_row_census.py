# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P1.2 (nexus-p2m1x.2), deliverable A: the read-only census of registry rows against their names.

``nexus.catalog.collection_row_census.census`` is pure: it takes the registry rows ``GET /collections/list`` returns
(every state, tombstones included) and answers the five questions of the bead. The fixtures below hold one row of
every shape the bead names: a mismatched row, a label clash, a multi-candidate tuple counted with and without the
grandfathered filter, a ``c123`` name and a dangling tombstone. ``nx catalog collections-census`` makes the one read
and prints the report; it writes nothing and never walks the catalog.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.catalog import collection_row_census as crc
from nexus.cli import main

NOW = "2026-11-20T00:00:00Z"


def row(name: str, content_type: str, owner: str, model: str, version: str = "v1", *, lifecycle: str = "live",
        superseded_by: str = "", grandfathered: bool = False) -> dict:
    return {"name": name, "content_type": content_type, "owner_id": owner, "embedding_model": model,
            "model_version": version, "legacy_grandfathered": grandfathered, "superseded_by": superseded_by,
            "lifecycle_state": lifecycle, "display_name": "", "superseded_at": "", "created_at": "",
            "dimension": 1024, "aspects_enabled": None}


def good(content_type="docs", owner="1-2", model="voyage-context-3", version="v1", **kw) -> dict:
    return row(f"{content_type}__{owner}__{model}__{version}", content_type, owner, model, version, **kw)


def report(rows):
    return crc.census(rows, tenant_label="t", generated_at=NOW)


# ── row versus name (Critical Assumption 1) ─────────────────────────────────────────────────────────────────────


def test_agreeing_rows_have_no_disagreement() -> None:
    r = report([good(), good("code", "1-3", "voyage-code-3")])
    rv = r["row_vs_name"]
    assert rv["live_rows"] == 2 and rv["name_conformant"] == 2
    assert rv["content_type"] == {"compared": 2, "mismatch": 0}
    assert rv["embedding_model"] == {"compared": 2, "mismatch": 0}
    assert rv["model_version"] == {"compared": 2, "mismatch": 0}
    assert rv["lifecycle"]["mismatch"] == 0
    assert rv["rows_that_disagree"] == []


def test_a_mismatched_row_is_counted_per_field_and_listed_with_both_values() -> None:
    bad = row("docs__1-2__voyage-context-3__v1", "code", "1-2", "voyage-code-3", "v2")
    r = report([bad, good("rdr", "1-9")])
    rv = r["row_vs_name"]
    assert rv["content_type"] == {"compared": 2, "mismatch": 1}
    assert rv["embedding_model"] == {"compared": 2, "mismatch": 1}
    assert rv["model_version"] == {"compared": 2, "mismatch": 1}
    [listed] = rv["rows_that_disagree"]
    assert listed["name"] == bad["name"]
    assert listed["differences"] == {
        "content_type": {"row": "code", "name": "docs"},
        "embedding_model": {"row": "voyage-code-3", "name": "voyage-context-3"},
        "model_version": {"row": "v2", "name": "v1"},
    }


def test_an_empty_row_value_is_a_mismatch_not_a_skip() -> None:
    r = report([row("docs__1-2__voyage-context-3__v1", "", "1-2", "voyage-context-3")])
    assert r["row_vs_name"]["content_type"]["mismatch"] == 1
    assert r["row_vs_name"]["rows_that_disagree"][0]["differences"]["content_type"] == {"row": "", "name": "docs"}


def test_an_owner_difference_is_its_own_bucket_and_is_not_a_disagreement() -> None:
    r = report([row("docs__1-2__voyage-context-3__v1", "docs", "slug-owner", "voyage-context-3")])
    rv = r["row_vs_name"]
    assert rv["owner_differences"] == {"compared": 1, "differ": 1, "expected": True,
                                       "names": ["docs__1-2__voyage-context-3__v1"]}
    assert rv["rows_that_disagree"] == [], "an owner difference alone is expected, so the row is not listed as a disagreement"
    assert rv["content_type"]["mismatch"] == 0


def test_lifecycle_is_quarantine_exactly_when_the_name_starts_with_quarantine() -> None:
    ok_q = row("quarantine-docs__1-2__voyage-context-3__v1", "docs", "1-2", "voyage-context-3", lifecycle="quarantine")
    state_no_prefix = good("code", "1-3", "voyage-code-3", lifecycle="quarantine")
    prefix_no_state = row("quarantine-rdr__1-4__voyage-context-3__v1", "rdr", "1-4", "voyage-context-3", lifecycle="live")
    r = report([ok_q, state_no_prefix, prefix_no_state, good()])
    lc = r["row_vs_name"]["lifecycle"]
    assert lc == {"checked": 4, "mismatch": 2, "quarantine_state_without_prefix": 1, "prefix_without_quarantine_state": 1}
    listed = {x["name"]: x["differences"] for x in r["row_vs_name"]["rows_that_disagree"]}
    assert listed[state_no_prefix["name"]] == {"lifecycle": {"row": "quarantine", "name": "live"}}
    assert listed[prefix_no_state["name"]] == {"lifecycle": {"row": "live", "name": "quarantine"}}
    assert ok_q["name"] not in listed
    assert r["row_vs_name"]["name_quarantine_conformant"] == 2 and r["row_vs_name"]["name_conformant"] == 2


def test_a_quarantine_row_is_compared_on_the_name_it_quarantines() -> None:
    q = row("quarantine-code__1-3__voyage-code-3__v1", "code", "1-3", "voyage-code-3", lifecycle="quarantine")
    r = report([q])
    assert r["row_vs_name"]["content_type"] == {"compared": 1, "mismatch": 0}


def test_a_name_that_does_not_parse_is_counted_and_not_compared() -> None:
    r = report([row("legacy-slug", "docs", "o", "voyage-context-3"), row("c123", "code", "1-1", "voyage-code-3")])
    rv = r["row_vs_name"]
    assert rv["name_not_conformant"] == 2 and rv["not_conformant_names"] == ["c123", "legacy-slug"]
    assert rv["content_type"]["compared"] == 0 and rv["rows_that_disagree"] == []
    assert rv["lifecycle"]["checked"] == 2, "lifecycle is checked on every live row, parseable or not"


def test_a_tombstone_is_not_a_live_row() -> None:
    tomb = row("docs__1-2__voyage-context-3__v1", "code", "1-2", "voyage-code-3", superseded_by="docs__1-2__voyage-context-3__v2")
    r = report([tomb, good(version="v2")])
    assert r["registry"] == {"rows": 2, "live": 1, "tombstones": 1}
    assert r["row_vs_name"]["live_rows"] == 1 and r["row_vs_name"]["content_type"]["mismatch"] == 0


# ── label clash (Critical Assumption 6) ─────────────────────────────────────────────────────────────────────────


def test_two_live_rows_with_one_name_clash() -> None:
    a, b = good(), good(owner="9-9")
    b["name"] = a["name"]
    r = report([a, b, good("code", "1-3", "voyage-code-3")])
    assert r["label_clash"] == {"live_names": 2, "clashing_names": 1, "clashes": [{"name": a["name"], "rows": 2}]}


def test_a_live_row_and_a_tombstone_of_one_name_do_not_clash() -> None:
    live = good()
    tomb = good(owner="9-9", superseded_by="somewhere")
    tomb["name"] = live["name"]
    assert report([live, tomb])["label_clash"]["clashing_names"] == 0


def test_distinct_names_never_clash() -> None:
    assert report([good(), good(version="v2")])["label_clash"] == {"live_names": 2, "clashing_names": 0, "clashes": []}


# ── multi-candidate tuples, with and without the grandfathered filter ────────────────────────────────────────────


def test_a_tuple_with_two_candidates_is_counted_by_both_forms() -> None:
    a = good(version="v1")
    b = row("docs__1-2__voyage-context-3__v2", "docs", "1-2", "voyage-context-3", "v2")
    r = report([a, b, good("code", "1-3", "voyage-code-3")])
    mt = r["multi_candidate_tuples"]
    assert mt["with_grandfathered_filter"]["count"] == 1
    assert mt["without_grandfathered_filter"]["count"] == 1
    [t] = mt["with_grandfathered_filter"]["tuples"]
    assert t == {"content_type": "docs", "owner_id": "1-2", "embedding_model": "voyage-context-3",
                 "candidates": [a["name"], b["name"]]}
    assert mt["ambiguous_only_without_the_filter"] == 0


def test_the_grandfathered_filter_changes_the_count() -> None:
    conformant = good()
    slug = row("legacy-slug", "docs", "1-2", "voyage-context-3", grandfathered=True)
    r = report([conformant, slug])
    mt = r["multi_candidate_tuples"]
    assert mt["with_grandfathered_filter"]["count"] == 0, "collectionForTuple drops the grandfathered slug today"
    assert mt["without_grandfathered_filter"]["count"] == 1, "D5 drops that filter, so this tuple becomes ambiguous"
    assert mt["without_grandfathered_filter"]["tuples"][0]["candidates"] == ["docs__1-2__voyage-context-3__v1", "legacy-slug"]
    assert mt["ambiguous_only_without_the_filter"] == 1


def test_quarantine_and_tombstoned_rows_are_never_candidates() -> None:
    q = row("quarantine-docs__1-2__voyage-context-3__v1", "docs", "1-2", "voyage-context-3", lifecycle="quarantine")
    tomb = row("docs__1-2__voyage-context-3__v0", "docs", "1-2", "voyage-context-3", "v0", superseded_by="x")
    r = report([good(), q, tomb])
    assert r["multi_candidate_tuples"]["with_grandfathered_filter"]["count"] == 0
    assert r["multi_candidate_tuples"]["without_grandfathered_filter"]["count"] == 0


def test_a_row_with_an_empty_tuple_component_is_excluded_and_counted() -> None:
    r = report([row("legacy-a", "", "1-2", "voyage-context-3"), row("legacy-b", "", "1-2", "voyage-context-3")])
    mt = r["multi_candidate_tuples"]
    assert mt["rows_without_a_complete_tuple"] == 2
    assert mt["with_grandfathered_filter"]["count"] == 0


# ── names matching ^c[0-9]+$ ─────────────────────────────────────────────────────────────────────────────────────


def test_live_names_of_the_minted_form_are_listed() -> None:
    r = report([row("c123", "code", "1-1", "voyage-code-3"), row("c7", "docs", "1-1", "voyage-context-3"),
                row("c12x", "docs", "1-1", "voyage-context-3"), row("cat", "docs", "1-1", "voyage-context-3"),
                row("c99", "docs", "1-1", "voyage-context-3", superseded_by="c100"), good()])
    assert r["minted_form_names"] == {"count": 2, "names": ["c123", "c7"]}, "a tombstone named c99 is not live"


# ── dangling tombstones ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_tombstone_whose_successor_names_no_row_is_dangling() -> None:
    kept = good(version="v2")
    ok_tomb = row("docs__1-2__voyage-context-3__v1", "docs", "1-2", "voyage-context-3", superseded_by=kept["name"])
    dangling = row("docs__1-2__voyage-context-3__v0", "docs", "1-2", "voyage-context-3", "v0", superseded_by="deleted-successor")
    r = report([kept, ok_tomb, dangling])
    assert r["dangling_tombstones"] == {
        "tombstones": 2, "dangling": 1,
        "rows": [{"name": dangling["name"], "superseded_by": "deleted-successor"}]}


def test_a_tombstone_pointing_at_a_tombstone_is_not_dangling_while_that_row_exists() -> None:
    a = row("a-old", "docs", "1-2", "voyage-context-3", superseded_by="b-mid")
    b = row("b-mid", "docs", "1-2", "voyage-context-3", superseded_by="c-new")
    c = row("c-new", "docs", "1-2", "voyage-context-3")
    assert report([a, b, c])["dangling_tombstones"]["dangling"] == 0


# ── the report ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_report_is_json_serialisable_and_labelled() -> None:
    r = report([good()])
    assert json.loads(json.dumps(r))["tenant_label"] == "t"
    assert r["census"] == "rdr229-p1.2" and r["generated_at"] == NOW
    assert set(r) >= {"registry", "row_vs_name", "label_clash", "multi_candidate_tuples", "minted_form_names",
                      "dangling_tombstones"}


def test_an_empty_registry_is_reported_as_empty_not_clean() -> None:
    r = report([])
    assert r["registry"] == {"rows": 0, "live": 0, "tombstones": 0}
    assert r["examined_nothing"] is True


def test_the_full_fixture_reports_all_five_counts() -> None:
    """The five items of the bead in one registry: each count is non-zero, so none can pass vacuously."""
    a = good()
    b = row("docs__1-2__voyage-context-3__v2", "docs", "1-2", "voyage-context-3", "v2")
    slug = row("legacy-slug", "docs", "1-2", "voyage-context-3", grandfathered=True)
    mism = row("code__1-3__voyage-code-3__v1", "docs", "1-3", "voyage-code-3")
    clash_a = good("rdr", "1-7")
    clash_b = dict(clash_a, owner_id="other")
    minted = row("c123", "knowledge", "1-1", "voyage-context-3")
    dang = row("rdr__1-8__voyage-context-3__v1", "rdr", "1-8", "voyage-context-3", superseded_by="gone")
    r = report([a, b, slug, mism, clash_a, clash_b, minted, dang])
    assert r["row_vs_name"]["content_type"]["mismatch"] == 1
    assert r["label_clash"]["clashing_names"] == 1
    assert r["multi_candidate_tuples"]["with_grandfathered_filter"]["count"] == 1
    assert r["multi_candidate_tuples"]["without_grandfathered_filter"]["count"] == 1
    assert r["minted_form_names"]["count"] == 1
    assert r["dangling_tombstones"]["dangling"] == 1


# ── the command ──────────────────────────────────────────────────────────────────────────────────────────────────


class _Reader:
    """The one read the command may make. Anything else, a write or a document walk, raises."""

    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple] = []

    def list_collections(self, lifecycle_state=None, content_type=None):
        self.calls.append((lifecycle_state, content_type))
        return list(self._rows)

    def __getattr__(self, name):
        raise AssertionError(f"collections-census touched {name!r}: it makes one list_collections read and nothing else")


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> _Reader:
    r = _Reader([good(), good(version="v2"), row("c5", "docs", "1-1", "voyage-context-3")])
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: r)
    return r


def test_the_command_prints_json_from_one_unfiltered_read(reader: _Reader) -> None:
    result = CliRunner().invoke(main, ["catalog", "collections-census", "--json", "--tenant-label", "local-e2e"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.output)
    assert body["tenant_label"] == "local-e2e" and body["registry"]["live"] == 3
    assert body["minted_form_names"]["names"] == ["c5"]
    assert reader.calls == [(None, None)], "every state, no filter: tombstones are needed for the dangling count"


def test_the_command_text_names_the_five_counts(reader: _Reader) -> None:
    result = CliRunner().invoke(main, ["catalog", "collections-census"])
    assert result.exit_code == 0, result.output
    for needle in ("row vs name", "label clash", "multi-candidate", "^c[0-9]+$", "dangling"):
        assert needle in result.output, needle


def test_the_command_refuses_an_unreadable_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    class Boom:
        def list_collections(self, *a, **k):
            raise RuntimeError("engine down")

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: Boom())
    result = CliRunner().invoke(main, ["catalog", "collections-census", "--json"])
    assert result.exit_code != 0 and "engine down" in result.output, "an unread registry is an error, never an empty census"
