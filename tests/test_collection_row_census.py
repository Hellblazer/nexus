# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P1.2 (nexus-p2m1x.2), deliverable A: the read-only census of registry rows against their names.

``nexus.catalog.rdr229.collection_row_census.census`` is pure: it takes the registry rows ``GET /collections/list`` returns
(every state, tombstones included) and answers the five questions of the bead. The fixtures below hold one row of
every shape the bead names: a mismatched row, a label clash, a multi-candidate tuple counted with and without the
grandfathered filter, a ``c123`` name and a dangling tombstone. ``nx catalog collections-census`` makes the one read
and prints the report; it writes nothing and never walks the catalog.
"""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from nexus.catalog.rdr229 import collection_row_census as crc
from nexus.cli import main

NOW = "2026-11-20T00:00:00Z"


def row(name: str, content_type: str, owner: str, model: str, version: str = "v1", *, lifecycle: str = "live",
        superseded_by: str = "", grandfathered: bool = False) -> dict:
    return {"name": name, "content_type": content_type, "owner_id": owner, "embedding_model": model,
            "model_version": version, "legacy_grandfathered": grandfathered, "superseded_by": superseded_by,
            "lifecycle_state": lifecycle, "display_name": "", "superseded_at": "", "created_at": "",
            "dimension": 1024, "aspects_enabled": None}


def good(content_type="docs", owner="1-2", model="minilm-l6-v2-384", version="v1", **kw) -> dict:
    return row(f"{content_type}__{owner}__{model}__{version}", content_type, owner, model, version, **kw)


def report(rows):
    return crc.census(rows, tenant_label="t", generated_at=NOW)


# ── row versus name (Critical Assumption 1) ─────────────────────────────────────────────────────────────────────


def test_agreeing_rows_have_no_disagreement() -> None:
    r = report([good(), good("code", "1-3", "bge-base-en-v15-768")])
    rv = r["row_vs_name"]
    assert rv["live_rows"] == 2 and rv["name_conformant"] == 2
    assert rv["content_type"] == {"compared": 2, "mismatch": 0}
    assert rv["embedding_model"] == {"compared": 2, "mismatch": 0}
    assert rv["model_version"] == {"compared": 2, "mismatch": 0}
    assert rv["lifecycle"]["mismatch"] == 0
    assert rv["rows_that_disagree"] == []


def test_a_mismatched_row_is_counted_per_field_and_listed_with_both_values() -> None:
    bad = row("docs__1-2__minilm-l6-v2-384__v1", "code", "1-2", "bge-base-en-v15-768", "v2")
    r = report([bad, good("rdr", "1-9")])
    rv = r["row_vs_name"]
    assert rv["content_type"] == {"compared": 2, "mismatch": 1}
    assert rv["embedding_model"] == {"compared": 2, "mismatch": 1}
    assert rv["model_version"] == {"compared": 2, "mismatch": 1}
    [listed] = rv["rows_that_disagree"]
    assert listed["name"] == bad["name"]
    assert listed["differences"] == {
        "content_type": {"row": "code", "name": "docs"},
        "embedding_model": {"row": "bge-base-en-v15-768", "name": "minilm-l6-v2-384"},
        "model_version": {"row": "v2", "name": "v1"},
    }


def test_an_empty_row_value_is_a_mismatch_not_a_skip() -> None:
    r = report([row("docs__1-2__minilm-l6-v2-384__v1", "", "1-2", "minilm-l6-v2-384")])
    assert r["row_vs_name"]["content_type"]["mismatch"] == 1
    assert r["row_vs_name"]["rows_that_disagree"][0]["differences"]["content_type"] == {"row": "", "name": "docs"}


def test_an_owner_difference_is_its_own_bucket_and_is_not_a_disagreement() -> None:
    r = report([row("docs__1-2__minilm-l6-v2-384__v1", "docs", "slug-owner", "minilm-l6-v2-384")])
    rv = r["row_vs_name"]
    assert rv["owner_differences"] == {"compared": 1, "differ": 1, "expected": True,
                                       "names": ["docs__1-2__minilm-l6-v2-384__v1"]}
    assert rv["rows_that_disagree"] == [], "an owner difference alone is expected, so the row is not listed as a disagreement"
    assert rv["content_type"]["mismatch"] == 0


def test_lifecycle_is_quarantine_exactly_when_the_name_starts_with_quarantine() -> None:
    ok_q = row("quarantine-docs__1-2__minilm-l6-v2-384__v1", "docs", "1-2", "minilm-l6-v2-384", lifecycle="quarantine")
    state_no_prefix = good("code", "1-3", "bge-base-en-v15-768", lifecycle="quarantine")
    prefix_no_state = row("quarantine-rdr__1-4__minilm-l6-v2-384__v1", "rdr", "1-4", "minilm-l6-v2-384", lifecycle="live")
    r = report([ok_q, state_no_prefix, prefix_no_state, good()])
    lc = r["row_vs_name"]["lifecycle"]
    assert lc == {"checked": 4, "mismatch": 2, "quarantine_state_without_prefix": 1, "prefix_without_quarantine_state": 1}
    listed = {x["name"]: x["differences"] for x in r["row_vs_name"]["rows_that_disagree"]}
    assert listed[state_no_prefix["name"]] == {"lifecycle": {"row": "quarantine", "name": "live"}}
    assert listed[prefix_no_state["name"]] == {"lifecycle": {"row": "live", "name": "quarantine"}}
    assert ok_q["name"] not in listed
    assert r["row_vs_name"]["name_quarantine_conformant"] == 2 and r["row_vs_name"]["name_conformant"] == 2


def test_a_quarantine_row_is_compared_on_the_name_it_quarantines() -> None:
    q = row("quarantine-code__1-3__bge-base-en-v15-768__v1", "code", "1-3", "bge-base-en-v15-768", lifecycle="quarantine")
    r = report([q])
    assert r["row_vs_name"]["content_type"] == {"compared": 1, "mismatch": 0}


def test_a_name_that_does_not_parse_is_counted_and_not_compared() -> None:
    r = report([row("legacy-slug", "docs", "o", "minilm-l6-v2-384"), row("c123", "code", "1-1", "bge-base-en-v15-768")])
    rv = r["row_vs_name"]
    assert rv["name_not_conformant"] == 2 and rv["not_conformant_names"] == ["c123", "legacy-slug"]
    assert rv["content_type"]["compared"] == 0 and rv["rows_that_disagree"] == []
    assert rv["lifecycle"]["checked"] == 2, "lifecycle is checked on every live row, parseable or not"


def test_a_tombstone_is_not_a_live_row() -> None:
    tomb = row("docs__1-2__minilm-l6-v2-384__v1", "code", "1-2", "bge-base-en-v15-768", superseded_by="docs__1-2__minilm-l6-v2-384__v2")
    r = report([tomb, good(version="v2")])
    assert r["registry"] == {"rows": 2, "live": 1, "tombstones": 1}
    assert r["row_vs_name"]["live_rows"] == 1 and r["row_vs_name"]["content_type"]["mismatch"] == 0


# ── label clash (Critical Assumption 6) ─────────────────────────────────────────────────────────────────────────


def test_two_live_rows_with_one_name_clash() -> None:
    a, b = good(), good(owner="9-9")
    b["name"] = a["name"]
    r = report([a, b, good("code", "1-3", "bge-base-en-v15-768")])
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
    b = row("docs__1-2__minilm-l6-v2-384__v2", "docs", "1-2", "minilm-l6-v2-384", "v2")
    r = report([a, b, good("code", "1-3", "bge-base-en-v15-768")])
    mt = r["multi_candidate_tuples"]
    assert mt["with_grandfathered_filter"]["count"] == 1
    assert mt["without_grandfathered_filter"]["count"] == 1
    [t] = mt["with_grandfathered_filter"]["tuples"]
    assert t == {"content_type": "docs", "owner_id": "1-2", "embedding_model": "minilm-l6-v2-384",
                 "candidates": [{"name": a["name"], "model_version": "v1"}, {"name": b["name"], "model_version": "v2"}]}
    assert mt["ambiguous_only_without_the_filter"] == 0


def test_the_grandfathered_filter_changes_the_count() -> None:
    conformant = good()
    slug = row("legacy-slug", "docs", "1-2", "minilm-l6-v2-384", grandfathered=True)
    r = report([conformant, slug])
    mt = r["multi_candidate_tuples"]
    assert mt["with_grandfathered_filter"]["count"] == 0, "collectionForTuple drops the grandfathered slug today"
    assert mt["without_grandfathered_filter"]["count"] == 1, "D5 drops that filter, so this tuple becomes ambiguous"
    assert [c["name"] for c in mt["without_grandfathered_filter"]["tuples"][0]["candidates"]] == [
        "docs__1-2__minilm-l6-v2-384__v1", "legacy-slug"]
    assert mt["ambiguous_only_without_the_filter"] == 1


def test_quarantine_and_tombstoned_rows_are_never_candidates() -> None:
    q = row("quarantine-docs__1-2__minilm-l6-v2-384__v1", "docs", "1-2", "minilm-l6-v2-384", lifecycle="quarantine")
    tomb = row("docs__1-2__minilm-l6-v2-384__v0", "docs", "1-2", "minilm-l6-v2-384", "v0", superseded_by="x")
    r = report([good(), q, tomb])
    assert r["multi_candidate_tuples"]["with_grandfathered_filter"]["count"] == 0
    assert r["multi_candidate_tuples"]["without_grandfathered_filter"]["count"] == 0


def test_a_row_with_an_empty_tuple_component_is_excluded_and_counted() -> None:
    r = report([row("legacy-a", "", "1-2", "minilm-l6-v2-384"), row("legacy-b", "", "1-2", "minilm-l6-v2-384")])
    mt = r["multi_candidate_tuples"]
    assert mt["rows_without_a_complete_tuple"] == 2
    assert mt["with_grandfathered_filter"]["count"] == 0


# ── names matching ^c[0-9]+$ ─────────────────────────────────────────────────────────────────────────────────────


def test_live_names_of_the_minted_form_are_listed() -> None:
    r = report([row("c123", "code", "1-1", "bge-base-en-v15-768"), row("c7", "docs", "1-1", "minilm-l6-v2-384"),
                row("c12x", "docs", "1-1", "minilm-l6-v2-384"), row("cat", "docs", "1-1", "minilm-l6-v2-384"),
                row("c99", "docs", "1-1", "minilm-l6-v2-384", superseded_by="c100"), good()])
    assert r["minted_form_names"] == {"count": 2, "names": ["c123", "c7"]}, "a tombstone named c99 is not live"


# ── dangling tombstones ──────────────────────────────────────────────────────────────────────────────────────────


def test_a_tombstone_whose_successor_names_no_row_is_dangling() -> None:
    kept = good(version="v2")
    ok_tomb = row("docs__1-2__minilm-l6-v2-384__v1", "docs", "1-2", "minilm-l6-v2-384", superseded_by=kept["name"])
    dangling = row("docs__1-2__minilm-l6-v2-384__v0", "docs", "1-2", "minilm-l6-v2-384", "v0", superseded_by="deleted-successor")
    r = report([kept, ok_tomb, dangling])
    assert r["dangling_tombstones"] == {
        "tombstones": 2, "dangling": 1,
        "rows": [{"name": dangling["name"], "superseded_by": "deleted-successor"}]}


def test_a_tombstone_pointing_at_a_tombstone_is_not_dangling_while_that_row_exists() -> None:
    a = row("a-old", "docs", "1-2", "minilm-l6-v2-384", superseded_by="b-mid")
    b = row("b-mid", "docs", "1-2", "minilm-l6-v2-384", superseded_by="c-new")
    c = row("c-new", "docs", "1-2", "minilm-l6-v2-384")
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
    b = row("docs__1-2__minilm-l6-v2-384__v2", "docs", "1-2", "minilm-l6-v2-384", "v2")
    slug = row("legacy-slug", "docs", "1-2", "minilm-l6-v2-384", grandfathered=True)
    mism = row("code__1-3__bge-base-en-v15-768__v1", "docs", "1-3", "bge-base-en-v15-768")
    clash_a = good("rdr", "1-7")
    clash_b = dict(clash_a, owner_id="other")
    minted = row("c123", "knowledge", "1-1", "minilm-l6-v2-384")
    dang = row("rdr__1-8__minilm-l6-v2-384__v1", "rdr", "1-8", "minilm-l6-v2-384", superseded_by="gone")
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
    r = _Reader([good(), good(version="v2"), row("c5", "docs", "1-1", "minilm-l6-v2-384")])
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: r)
    return r


def test_the_command_prints_json_from_one_unfiltered_read(reader: _Reader) -> None:
    result = CliRunner().invoke(main, ["catalog", "collections-census", "--json", "--tenant-label", "local-e2e"])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["tenant_label"] == "local-e2e" and body["tenant_label_verified"] is False
    assert "unverified" in result.stderr and body["registry"]["live"] == 3
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


# ── fix round 1: versions, ties, the P1.4 tie-break, non-conformant rows, unusable versions ──────────────────────


def _legacy(name: str, created: str = "", **kw) -> dict:
    r = row(name, "docs", "1-2", "minilm-l6-v2-384", **kw)
    r["created_at"] = created
    return r


def _good_at(created: str, version: str = "v1", **kw) -> dict:
    r = good(version=version, **kw)
    r["created_at"] = created
    return r


def test_each_candidate_carries_its_model_version() -> None:
    r = report([good(version="v1"), good(version="v3")])
    cands = r["multi_candidate_tuples"]["with_grandfathered_filter"]["tuples"][0]["candidates"]
    assert cands == [{"name": "docs__1-2__minilm-l6-v2-384__v1", "model_version": "v1"},
                     {"name": "docs__1-2__minilm-l6-v2-384__v3", "model_version": "v3"}]


def test_a_tuple_tied_at_the_top_version_is_listed_and_a_lower_version_is_not_a_tie() -> None:
    a = good(version="v2")
    b = row("docs__1-2__minilm-l6-v2-384__v2x", "docs", "1-2", "minilm-l6-v2-384", "v2")   # same version, another name
    low = good(version="v1")
    r = report([a, b, low])
    tied = r["multi_candidate_tuples"]["tied_at_top_version"]["with_grandfathered_filter"]
    assert tied["count"] == 1
    [t] = tied["tuples"]
    assert t["tied"] == sorted([a["name"], b["name"]]) and low["name"] not in t["tied"]
    assert t["top_version"] == "v2" and t["winner_name_asc"] == a["name"]
    only_versions = report([good(version="v1"), good(version="v2")])
    assert only_versions["multi_candidate_tuples"]["tied_at_top_version"]["with_grandfathered_filter"]["count"] == 0


def test_the_tie_at_the_top_is_counted_with_and_without_the_grandfathered_filter() -> None:
    conformant = good()
    slug = _legacy("legacy-slug", grandfathered=True)
    r = report([conformant, slug])
    tied = r["multi_candidate_tuples"]["tied_at_top_version"]
    assert tied["with_grandfathered_filter"]["count"] == 0
    assert tied["without_grandfathered_filter"]["count"] == 1


def test_a_tuple_whose_winner_changes_when_the_grandfathered_filter_is_dropped_is_listed() -> None:
    conformant = good(version="v1")          # wins today (only candidate with the filter)
    slug = _legacy("aaa-slug", grandfathered=True)   # v-less (version 0): loses on version... make it win on version
    slug["model_version"] = "v1"            # ties at v1, and "aaa-slug" < "docs__..." so NAME ASC picks it once unfiltered
    r = report([conformant, slug])
    wc = r["multi_candidate_tuples"]["winner_changes_when_filter_dropped"]
    assert wc["count"] == 1
    assert wc["tuples"][0]["winner_with_filter"] == conformant["name"]
    assert wc["tuples"][0]["winner_without_filter"] == "aaa-slug"


def test_a_tuple_of_only_grandfathered_rows_gains_a_winner_and_is_counted_apart() -> None:
    r = report([_legacy("only-slug", grandfathered=True)])
    wc = r["multi_candidate_tuples"]["winner_changes_when_filter_dropped"]
    assert wc["count"] == 0 and wc["tuples_that_only_gain_a_winner"]["count"] == 1


def test_p14_the_created_at_winner_differs_when_the_older_row_has_the_higher_name() -> None:
    older = _good_at("2026-01-01T00:00:00Z", owner="1-2")
    older["name"], older["model_version"] = "docs__1-2__minilm-l6-v2-384__v1z", "v1"
    newer = _good_at("2026-06-01T00:00:00Z")
    r = report([older, newer])
    p14 = r["multi_candidate_tuples"]["p14_created_at_tie_break"]
    assert p14["tied_tuples_with_filter"] == 1
    d = p14["created_at_winner_differs"]
    assert d["count"] == 1
    [t] = d["tuples"]
    assert t["winner_name_asc"] == newer["name"] and t["winner_created_at_then_name"] == older["name"]
    assert t["created_at"] == {older["name"]: "2026-01-01T00:00:00Z", newer["name"]: "2026-06-01T00:00:00Z"}
    assert "switch_condition" in p14 and "zero" in p14["switch_condition"]


def test_p14_marks_a_legacy_created_at_winner_and_names_its_conformant_sibling() -> None:
    conformant = _good_at("2026-06-01T00:00:00Z", version="v1")
    legacy = _legacy("legacy-label", "2026-01-01T00:00:00Z", version="v1")   # not grandfathered, non-conformant name
    r = report([conformant, legacy])
    p14 = r["multi_candidate_tuples"]["p14_created_at_tie_break"]
    [t] = p14["created_at_winner_differs"]["tuples"]
    assert t["winner_name_asc"] == conformant["name"], "'d' sorts before 'l'"
    assert t["winner_created_at_then_name"] == "legacy-label"
    assert t["created_at_winner_is_legacy_or_nonconformant"] is True
    assert t["conformant_siblings"] == [conformant["name"]]
    assert p14["created_at_winner_is_legacy_or_nonconformant_count"] == 1


def test_p14_count_is_zero_when_name_and_created_at_agree() -> None:
    first = _good_at("2026-01-01T00:00:00Z", version="v1")
    second = row("docs__1-2__minilm-l6-v2-384__v1b", "docs", "1-2", "minilm-l6-v2-384", "v1")
    second["created_at"] = "2026-06-01T00:00:00Z"
    p14 = report([first, second])["multi_candidate_tuples"]["p14_created_at_tie_break"]
    assert p14["tied_tuples_with_filter"] == 1 and p14["created_at_winner_differs"]["count"] == 0


def test_p14_flags_tied_candidates_with_no_created_at() -> None:
    a, b = _good_at(""), _good_at("2026-01-01T00:00:00Z")
    b["name"] = "docs__1-2__minilm-l6-v2-384__v1b"
    p14 = report([a, b])["multi_candidate_tuples"]["p14_created_at_tie_break"]
    assert p14["tied_candidates_without_created_at"] == 1


def test_non_conformant_live_names_list_their_attributes_and_flag_the_empty_ones() -> None:
    r = report([row("legacy-a", "docs", "o", "minilm-l6-v2-384", "v1", grandfathered=True),
                row("legacy-b", "", "o", "", "", grandfathered=False)])
    rows = {x["name"]: x for x in r["row_vs_name"]["not_conformant_rows"]}
    assert rows["legacy-a"] == {"name": "legacy-a", "content_type": "docs", "embedding_model": "minilm-l6-v2-384",
                                "model_version": "v1", "legacy_grandfathered": True, "empty_fields": []}
    assert rows["legacy-b"]["legacy_grandfathered"] is False
    assert rows["legacy-b"]["empty_fields"] == ["content_type", "embedding_model", "model_version"]


def test_live_rows_with_an_empty_or_malformed_model_version_are_counted_and_labelled() -> None:
    r = report([good(), row("a", "docs", "1-2", "m", ""), row("b", "docs", "1-2", "m", "version1"),
                row("c", "docs", "1-2", "m", "v7", superseded_by="x")])   # a tombstone is not live
    uv = r["unusable_model_versions"]
    assert (uv["count"], uv["empty"], uv["malformed"]) == (2, 1, 1)
    assert [x["name"] for x in uv["rows"]] == ["a", "b"]
    assert "P1.4" in uv["label"] and "empty" in uv["label"]


def test_the_text_names_the_new_counts(reader: _Reader) -> None:
    result = CliRunner().invoke(main, ["catalog", "collections-census"])
    assert result.exit_code == 0, result.output
    for needle in ("tied at the TOP version", "winner changes when the grandfathered filter is dropped",
                   "P1.4 TIE-BREAK", "model_version is empty or not v<digits>"):
        assert needle in result.output, needle


def test_created_at_orders_by_instant_not_by_spelling() -> None:
    """``2026-11-20 09:56:57+00`` and ``2026-11-20T10:00:00Z`` are in instant order though ' ' sorts before 'T'; a
    string compare would call the later one older when the spellings are swapped."""
    older = _good_at("2026-11-20T08:00:00Z")                # 'T' spelling, earlier instant
    older["name"] = "docs__1-2__minilm-l6-v2-384__v1z"
    newer = _good_at("2026-11-20 09:56:57.054349+00")       # ' ' spelling, later instant
    p14 = report([older, newer])["multi_candidate_tuples"]["p14_created_at_tie_break"]
    [t] = p14["created_at_winner_differs"]["tuples"]
    assert t["winner_created_at_then_name"] == older["name"]
    assert p14["tied_candidates_with_unreadable_created_at"] == 0


def test_an_unreadable_created_at_is_counted_and_sorts_last() -> None:
    a, b = _good_at("not a time"), _good_at("2026-01-01T00:00:00Z")
    b["name"] = "docs__1-2__minilm-l6-v2-384__v1b"
    p14 = report([a, b])["multi_candidate_tuples"]["p14_created_at_tie_break"]
    assert p14["tied_candidates_with_unreadable_created_at"] == 1
    [t] = p14["created_at_winner_differs"]["tuples"]
    assert t["winner_created_at_then_name"] == b["name"]


def test_two_spellings_of_one_instant_tie_and_fall_to_the_name() -> None:
    """Confirmation-round finding: equal instants in T and space spellings must tie, so NAME ASC decides and the
    created_at winner agrees with the name winner (no false 'differs')."""
    rows = [
        {"name": "docs__1-1__minilm-l6-v2-384__v1", "content_type": "docs", "owner_id": "1-1",
         "embedding_model": "minilm-l6-v2-384", "model_version": "v1", "superseded_by": "",
         "lifecycle_state": "live", "legacy_grandfathered": False, "created_at": "2026-11-20T09:56:57Z"},
        {"name": "zslug", "content_type": "docs", "owner_id": "1-1",
         "embedding_model": "minilm-l6-v2-384", "model_version": "v1", "superseded_by": "",
         "lifecycle_state": "live", "legacy_grandfathered": False, "created_at": "2026-11-20 09:56:57+00"},
    ]
    report = crc.census(rows, tenant_label="t", generated_at="2026-11-20T00:00:00Z")
    p14 = report["multi_candidate_tuples"]["p14_created_at_tie_break"]
    assert p14["tied_tuples_with_filter"] == 1
    assert p14["created_at_winner_differs"]["count"] == 0
    assert "codepoint" in p14["switch_condition"]
