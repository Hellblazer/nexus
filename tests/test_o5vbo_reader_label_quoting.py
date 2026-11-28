# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-o5vbo: a label the RDR-229 rule accepts but a reader gives a meaning of its own is named by quoting it.

``all`` (the corpus wildcard), a trailing ``*``, ``-*`` or ``-<8 hex>`` (scope-tag globs and hash suffixes) and a
leading ``$`` (a plan binding) each register and then could not be named through ``_resolve_corpus_target`` or the
scope-tag normaliser. One pair of double quotes names the label exactly; an unquoted part reads as it always did.
"""
from __future__ import annotations

import pytest

from nexus.corpus import validate_collection_name
from nexus.label_quote import is_quoted, label_needs_quote, quote_corpus_label, unquote_label
from nexus.mcp import core as mcp_core
from nexus.plans.scope import _infer_scope_tags, _normalize_scope_string, normalize_scope_tags

#: One label per problem class, each legal under the RDR-229 label rule.
PROBLEM_LABELS = ["all", "notes*", "notes-*", "notes-deadbeef", "notes-5AF9BFE0", "$topic", "*", "-*"]
OTHER_NAMES = ["code__repo__minilm-l6-v2__v1", "plain"]


@pytest.mark.parametrize("label", PROBLEM_LABELS)
def test_each_problem_label_passes_the_label_rule(label: str) -> None:
    validate_collection_name(label)  # the rule is untouched: nothing here is a rule clause


# --- the helper ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", PROBLEM_LABELS)
def test_a_problem_label_wrapped_in_quotes_unquotes_to_itself(label: str) -> None:
    assert label_needs_quote(label)
    assert unquote_label(f'"{label}"') == (label, True)


@pytest.mark.parametrize("label", OTHER_NAMES)
def test_an_unproblematic_label_needs_no_quote(label: str) -> None:
    assert not label_needs_quote(label)
    assert unquote_label(label) == (label, False)


@pytest.mark.parametrize(("label", "part"), [
    ("all", '"all"'), ('"x"', '""x""'),                                   # the corpus reader misreads these
    ("notes*", "notes*"), ("notes-deadbeef", "notes-deadbeef"), ("$topic", "$topic"),  # it reads these as names
    ("legacy__repo-5af9bfe0", "legacy__repo-5af9bfe0"), ("plain", "plain"),
])
def test_a_corpus_part_is_quoted_only_where_the_corpus_reader_would_misread_the_label(label: str, part: str) -> None:
    assert quote_corpus_label(label) == part
    assert unquote_label(part)[0] == label


def test_a_label_that_starts_and_ends_with_a_quote_is_named_by_wrapping_it_again() -> None:
    assert label_needs_quote('"x"')
    assert quote_corpus_label('"x"') == '""x""'
    assert unquote_label('""x""') == ('"x"', True)


def test_a_lone_quote_or_a_one_sided_quote_is_not_a_quoted_part() -> None:
    for part in ('"', '"x', 'x"', "x"):
        assert not is_quoted(part)
        assert unquote_label(part) == (part, False)


# --- reader 1: the MCP corpus list -----------------------------------------------------------------------------

def _no_registry_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve_corpus`` and the shorthand lookup read a collection's registry row through ``nexus.mcp_infra.get_collection_row``, which
    lists the registry through the service on a cold cache. No row is the answer for a name no registry backs, and it keeps these tests
    off the engine: they need no tenant."""
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name, **kw: None)


@pytest.fixture
def corpus_names(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    names = ["all", "notes*", "notes-*", "notes-deadbeef", "$topic", "code__repo__minilm-l6-v2__v1", "plain"]
    monkeypatch.setattr(mcp_core, "_get_collection_names", lambda: names)
    monkeypatch.setattr(mcp_core, "_get_fanout_counts", lambda: dict.fromkeys(names, 100))
    _no_registry_rows(monkeypatch)
    return names


@pytest.mark.parametrize("label", ["all", "notes*", "notes-*", "notes-deadbeef", "$topic"])
def test_the_corpus_reader_names_exactly_a_quoted_label(corpus_names: list[str], label: str) -> None:
    assert mcp_core._resolve_corpus_target(f'"{label}"', t3=None) == [label]


def test_the_corpus_reader_keeps_the_unquoted_all_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unquoted ``all`` fans out to every collection; the same word quoted names a collection that is not there."""
    names = ["notes*", "plain", "code__repo__minilm-l6-v2__v1"]
    monkeypatch.setattr(mcp_core, "_get_collection_names", lambda: names)
    monkeypatch.setattr(mcp_core, "_get_fanout_counts", lambda: dict.fromkeys(names, 100))
    _no_registry_rows(monkeypatch)
    assert mcp_core._resolve_corpus_target("all", t3=None) == names
    assert mcp_core._resolve_corpus_target('"all"', t3=None) == []


@pytest.mark.parametrize("label", ["notes*", "notes-*", "notes-deadbeef", "$topic"])
def test_the_corpus_reader_reads_an_unquoted_glob_label_by_exact_name_as_before(corpus_names: list[str], label: str) -> None:
    """The premise behind ``quote_corpus_label``: this reader does not strip a trailing ``*``, ``-*`` or an 8-hex suffix and does
    not read a leading ``$`` as a binding, so each resolves to exactly its own label, with nothing else beside it."""
    assert mcp_core._resolve_corpus_target(label, t3=None) == [label]


def test_a_quoted_part_in_a_list_is_one_label_beside_ordinary_parts(corpus_names: list[str]) -> None:
    target = mcp_core._resolve_corpus_target('"all",plain,code__repo__minilm-l6-v2__v1', t3=None)
    assert target == ["all", "plain", "code__repo__minilm-l6-v2__v1"]


def test_a_quoted_label_names_no_collection_that_merely_starts_with_it(corpus_names: list[str]) -> None:
    """Quoting is exact: ``"notes"`` is not the prefix fan-out and does not find ``notes*``."""
    assert mcp_core._resolve_corpus_target('"notes"', t3=None) == []


def test_a_quoted_dunder_label_resolves_as_the_same_name_unquoted(corpus_names: list[str]) -> None:
    name = "code__repo__minilm-l6-v2__v1"
    assert mcp_core._resolve_corpus_target(f'"{name}"', t3=None) == mcp_core._resolve_corpus_target(name, t3=None) == [name]


# --- reader 2: scope tags --------------------------------------------------------------------------------------

@pytest.mark.parametrize("label", ["all", "notes*", "notes-*", "notes-deadbeef", "notes-5AF9BFE0", "$topic"])
def test_the_scope_reader_keeps_a_quoted_problem_label_whole(label: str) -> None:
    quoted = f'"{label}"'
    assert _normalize_scope_string(quoted) == quoted
    assert normalize_scope_tags(quoted) == quoted
    assert normalize_scope_tags(normalize_scope_tags(quoted)) == quoted  # idempotent


@pytest.mark.parametrize("label", ["code__repo", "plain", "code__repo__minilm-l6-v2__v1", "rdr__", "1.16"])
def test_the_scope_reader_drops_the_quotes_where_no_reader_would_misread_the_label(label: str) -> None:
    """The branch the old function lacked: it left a quoted string as it was."""
    assert _normalize_scope_string(f'"{label}"') == label
    assert normalize_scope_tags(f'"{label}",zzz') == ",".join(sorted([label, "zzz"]))


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


# --- a comma list is read part by part (review A) ----------------------------------------------------------------

def _plan(corpus: str) -> str:
    import json  # noqa: PLC0415

    return json.dumps({"steps": [{"tool": "search", "args": {"corpus": corpus}}]})


def test_a_plan_corpus_list_of_two_quoted_parts_is_two_tags() -> None:
    tags = _infer_scope_tags(_plan('"all","$x"')).split(",")
    assert sorted(tags) == ['"$x"', '"all"']


def test_a_plan_corpus_list_mixing_quoted_and_unquoted_parts_reads_each_alone() -> None:
    tags = _infer_scope_tags(_plan('"all",code__a-deadbeef,"notes*",docs__b,$bind,all'))
    assert sorted(tags.split(",")) == ['"all"', '"notes*"', "code__a", "docs__b"]


def test_a_stored_corpus_list_survives_a_second_normalisation() -> None:
    tags = _infer_scope_tags(_plan('"all","notes*"'))
    assert normalize_scope_tags(tags) == tags


# --- a stored tag is read in canonical form too (review D) -------------------------------------------------------

def test_a_tag_stored_with_redundant_quotes_still_fits_a_caller_naming_the_label() -> None:
    from nexus.plans.matcher import _scope_fit  # noqa: PLC0415

    assert _scope_fit('"code__repo"', "code__repo") == 1.0
    assert _scope_fit('"code__repo",rdr__', _normalize_scope_string('"code__repo"')) == 1.0


# --- the owner narrowing re-joins labels quoted (review C) -------------------------------------------------------

def _narrow(monkeypatch: pytest.MonkeyPatch, expanded: list[str], kinds: dict[str, tuple[str, str]]) -> dict:
    from nexus.plans import owner_narrowing as on  # noqa: PLC0415

    monkeypatch.setattr(on, "expand_corpus", lambda corpus: list(expanded))
    monkeypatch.setattr(on, "attribute_collection", lambda name: kinds[name])
    args, narrowing = on.narrow_args_by_owner({"corpus": "knowledge,code"}, "1.2")
    assert narrowing is not None and narrowing.status == "narrowed"
    return args


def test_a_sole_kept_label_all_is_not_rejoined_as_the_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    kinds = {"all": ("knowledge", "x"), "code__1-9__bge__v1": ("code", "1-9")}
    args = _narrow(monkeypatch, ["all", "code__1-9__bge__v1"], kinds)
    assert args["corpus"] == '"all"'


def test_kept_labels_are_rejoined_each_in_its_own_form(monkeypatch: pytest.MonkeyPatch) -> None:
    kinds = {"plain": ("knowledge", "x"), "all": ("knowledge", "y"), "code__1-9__bge__v1": ("code", "1-9")}
    args = _narrow(monkeypatch, ["plain", "all", "code__1-9__bge__v1"], kinds)
    assert args["corpus"] == 'plain,"all"'


# --- a quoted part is not a binding (review E) -------------------------------------------------------------------

def test_a_dollar_inside_a_quoted_corpus_part_is_not_a_binding() -> None:
    from nexus.plans.schema import _binding_refs  # noqa: PLC0415

    assert _binding_refs('"$topic"') == set()
    assert _binding_refs('"$topic",plain') == set()
    assert _binding_refs('$topic') == {"topic"}  # unquoted: still a binding
    assert _binding_refs('"$a",$b') == {"b"}
    assert _binding_refs("repo/$area") == {"area"}
