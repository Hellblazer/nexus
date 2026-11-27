# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o5vbo: a label the RDR-229 rule accepts but a reader gives a meaning of its own is named by quoting it.

``all`` (the corpus wildcard), a trailing ``*``, ``-*`` or ``-<8 hex>`` (scope-tag globs and hash suffixes) and a
leading ``$`` (a plan binding) each register and then could not be named through ``_resolve_corpus_target`` or the
scope-tag normaliser. One pair of double quotes names the label exactly; an unquoted part reads as it always did.
"""
from __future__ import annotations

import pytest

from nexus.corpus import validate_collection_name
from nexus.label_quote import is_quoted, label_needs_quote, quote_label, unquote_label
from nexus.mcp import core as mcp_core
from nexus.plans.scope import _infer_scope_tags, _normalize_scope_string, normalize_scope_tags

#: One label per problem class, each legal under the RDR-229 label rule.
PROBLEM_LABELS = ["all", "notes*", "notes-*", "notes-deadbeef", "notes-5AF9BFE0", "$topic", "*", "-*"]
OTHER_NAMES = ["code__repo__voyage-code-3__v1", "plain"]


@pytest.mark.parametrize("label", PROBLEM_LABELS)
def test_each_problem_label_passes_the_label_rule(label: str) -> None:
    validate_collection_name(label)  # the rule is untouched: nothing here is a rule clause


# --- the helper ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", PROBLEM_LABELS)
def test_quote_label_wraps_a_problem_label_and_unquote_returns_it(label: str) -> None:
    quoted = quote_label(label)
    assert quoted == f'"{label}"'
    assert unquote_label(quoted) == (label, True)


@pytest.mark.parametrize("label", OTHER_NAMES)
def test_quote_label_leaves_an_unproblematic_label_alone(label: str) -> None:
    assert not label_needs_quote(label)
    assert quote_label(label) == label
    assert unquote_label(label) == (label, False)


def test_a_label_that_starts_and_ends_with_a_quote_is_named_by_wrapping_it_again() -> None:
    assert label_needs_quote('"x"')
    assert quote_label('"x"') == '""x""'
    assert unquote_label('""x""') == ('"x"', True)


def test_a_lone_quote_or_a_one_sided_quote_is_not_a_quoted_part() -> None:
    for part in ('"', '"x', 'x"', "x"):
        assert not is_quoted(part)
        assert unquote_label(part) == (part, False)


# --- reader 1: the MCP corpus list -----------------------------------------------------------------------------

@pytest.fixture
def corpus_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    names = ["all", "notes*", "notes-*", "notes-deadbeef", "$topic", "code__repo__voyage-code-3__v1", "plain"]
    monkeypatch.setattr(mcp_core, "_get_collection_names", lambda: names)
    monkeypatch.setattr(mcp_core, "_get_collection_counts", lambda: dict.fromkeys(names, 100))
    return names


@pytest.mark.parametrize("label", ["all", "notes*", "notes-*", "notes-deadbeef", "$topic"])
def test_the_corpus_reader_names_exactly_a_quoted_label(corpus_names: list[str], label: str) -> None:
    assert mcp_core._resolve_corpus_target(f'"{label}"', t3=None) == [label]


def test_the_corpus_reader_keeps_the_unquoted_all_wildcard(corpus_names: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Unquoted ``all`` still fans out; it does not name the label ``all``."""
    target = mcp_core._resolve_corpus_target("all", t3=None)
    assert "all" not in target or len(target) > 1


def test_the_corpus_reader_reads_an_unquoted_glob_label_by_exact_name_as_before(corpus_names: list[str]) -> None:
    for label in ("notes*", "notes-*", "notes-deadbeef", "$topic"):
        assert mcp_core._resolve_corpus_target(label, t3=None) == [label]


def test_a_quoted_part_in_a_list_is_one_label_beside_ordinary_parts(corpus_names: list[str]) -> None:
    target = mcp_core._resolve_corpus_target('"all",plain,code__repo__voyage-code-3__v1', t3=None)
    assert target == ["all", "plain", "code__repo__voyage-code-3__v1"]


def test_a_quoted_label_names_no_collection_that_merely_starts_with_it(corpus_names: list[str]) -> None:
    """Quoting is exact: ``"notes"`` is not the prefix fan-out and does not find ``notes*``."""
    assert mcp_core._resolve_corpus_target('"notes"', t3=None) == []


def test_a_quoted_dunder_label_resolves_as_the_same_name_unquoted(corpus_names: list[str]) -> None:
    name = "code__repo__voyage-code-3__v1"
    assert mcp_core._resolve_corpus_target(f'"{name}"', t3=None) == mcp_core._resolve_corpus_target(name, t3=None) == [name]


# --- reader 2: scope tags --------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["all", "notes*", "notes-*", "notes-deadbeef", "notes-5AF9BFE0", "$topic"])
def test_the_scope_reader_keeps_a_quoted_problem_label_whole(label: str) -> None:
    quoted = f'"{label}"'
    assert _normalize_scope_string(quoted) == quoted
    assert normalize_scope_tags(quoted) == quoted
    assert normalize_scope_tags(normalize_scope_tags(quoted)) == quoted  # idempotent


def test_the_scope_reader_drops_the_quotes_where_no_reader_would_misread_the_label() -> None:
    assert _normalize_scope_string('"code__repo"') == "code__repo"
    assert normalize_scope_tags('"code__repo",rdr__') == "code__repo,rdr__"


@pytest.mark.parametrize(
    ("raw", "stored"),
    [("all", ""), ("notes*", "notes"), ("notes-*", "notes"), ("notes-deadbeef", "notes"), ("code__x-5AF9BFE0", "code__x")],
)
def test_the_scope_reader_reads_an_unquoted_part_as_before(raw: str, stored: str) -> None:
    assert normalize_scope_tags(raw) == stored


def test_the_scope_reader_still_reads_a_leading_dollar_unquoted_as_a_binding() -> None:
    import json  # noqa: PLC0415

    plan = json.dumps({"steps": [{"tool": "search", "args": {"corpus": "$topic"}}]})
    assert _infer_scope_tags(plan) == ""


@pytest.mark.parametrize("label", ["all", "notes*", "notes-deadbeef", "$topic"])
def test_a_plan_naming_a_quoted_label_infers_that_label_as_its_scope(label: str) -> None:
    import json  # noqa: PLC0415

    plan = json.dumps({"steps": [{"tool": "search", "args": {"corpus": f'"{label}"'}}]})
    assert _infer_scope_tags(plan) == f'"{label}"'


def test_a_quoted_scope_preference_fits_only_a_plan_tagged_with_the_same_quoted_label() -> None:
    from nexus.plans.matcher import _scope_fit  # noqa: PLC0415

    pref = _normalize_scope_string('"all"')
    assert _scope_fit('"all"', pref) == 1.0
    assert _scope_fit("rdr__", pref) is None
