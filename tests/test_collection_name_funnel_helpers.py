# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-204 Phase 3 funnel slice 1 (nexus-ft04v.21): unit tests for the
three collection-name identity helpers introduced in ``nexus.corpus``
(``collection_content_type``, ``collection_owner``, ``collection_model``)
and pinning tests for every call site this slice funnelled through them.

The funnel is explicitly BEHAVIOR-PRESERVING at this step (the RDR's
"first the funnel, then the repoint" split, step 5): these helpers still
parse the collection name, exactly reproducing what the raw
split/partition/startswith/``"__" in`` call at each site used to compute.
Every parametrize table below pins the value produced BEFORE this bead
(verified against ``git show HEAD~1:src/nexus/corpus.py`` while writing
this file) and now, so a later regression in the funnel or the eventual
repoint (nexus-ft04v.26) has something concrete to diff against.

Covers, per input class named in the bead: RDR-101 conformant names,
legacy 2-segment (bare content-type prefix) names, ``quarantine-``
prefixed names, bare/no-separator names, and garbage/unrecognized-prefix
and malformed multi-segment names.
"""
from __future__ import annotations

import pytest

from nexus.corpus import (
    CONTENT_TYPES,
    PlaceholderCollectionError,
    _legacy_content_type_for_collection,
    collection_content_type,
    collection_model,
    collection_owner,
    collection_registration_kwargs,
    default_projection_threshold,
    t3_collection_name,
    validate_collection_name,
    voyage_model_for_collection,
)

pytestmark = pytest.mark.usefixtures("cloud_mode")


# ── The three helpers themselves ───────────────────────────────────────────

@pytest.mark.parametrize(
    "name, expected",
    [
        # conformant
        ("code__myrepo__voyage-code-3__v1", "code"),
        ("docs__myrepo__voyage-context-3__v1", "docs"),
        # legacy 2-segment
        ("code__myrepo", "code"),
        ("docs__myrepo", "docs"),
        ("knowledge__myrepo", "knowledge"),
        ("rdr__myrepo", "rdr"),
        # unrecognized prefix (still returned raw, unfiltered)
        ("other__x", "other"),
        ("test__coll", "test"),
        # quarantine-prefixed (raw, unfiltered -- callers strip separately)
        ("quarantine-docs__x", "quarantine-docs"),
        # malformed multi-segment (first segment only)
        ("foo__bar__baz__qux", "foo"),
        # bare / no separator at all
        ("nodunder", ""),
        ("", ""),
        # degenerate
        ("__", ""),
        ("__b", ""),
    ],
    ids=[
        "conformant_code", "conformant_docs", "legacy_code", "legacy_docs",
        "legacy_knowledge", "legacy_rdr", "unrecognized_prefix",
        "opaque_test_fixture_prefix", "quarantine_prefixed_raw",
        "malformed_multi_segment", "bare_no_dunder", "empty_string",
        "degenerate_bare_dunder", "degenerate_dunder_then_char",
    ],
)
def test_collection_content_type(name: str, expected: str) -> None:
    assert collection_content_type(name) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("code__myrepo__voyage-code-3__v1", "myrepo"),
        ("code__myrepo", "myrepo"),
        ("knowledge__my_project_notes", "my_project_notes"),
        ("foo__bar__baz__qux", "bar__baz__qux"),  # whole remainder, unsplit
        ("nodunder", "nodunder"),  # no separator: identity is the whole name
        ("", ""),
        ("__", ""),
        ("__b", "b"),
        ("foo____bar", "__bar"),  # partition stops at the FIRST "__"
    ],
    ids=[
        "conformant", "legacy_two_segment", "compound_owner_keeps_underscores",
        "malformed_keeps_whole_remainder", "bare_no_dunder_returns_whole_name",
        "empty_string", "degenerate_bare_dunder", "degenerate_dunder_then_char",
        "adjacent_double_dunder",
    ],
)
def test_collection_owner(name: str, expected: str) -> None:
    assert collection_owner(name) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("code__myrepo__voyage-code-3__v1", "voyage-code-3"),
        ("docs__myrepo__voyage-context-3__v1", "voyage-context-3"),
        ("code__myrepo", ""),  # legacy: no model segment to read
        ("nodunder", ""),
        ("", ""),
    ],
    ids=["conformant_code", "conformant_docs", "legacy_no_model", "bare", "empty"],
)
def test_collection_model(name: str, expected: str) -> None:
    assert collection_model(name) == expected


def test_collection_owner_equals_input_iff_no_separator() -> None:
    """The corollary the funnel relies on at several call sites (a
    dunder-free substitute for a raw ``"__" in x`` test)."""
    for name in ["nodunder", "", "abc", "distributed-systems"]:
        assert collection_owner(name) == name
    for name in ["a__b", "__", "__x", "code__myrepo__voyage-code-3__v1"]:
        assert collection_owner(name) != name


# ── Pinned call sites: content-type-derived functions ──────────────────────

_CT_INPUTS = [
    "code__x", "docs__x", "knowledge__x", "rdr__x",
    "other__x", "weird-prefix__y", "nodunder",
    "quarantine-docs__x", "quarantine-code__x", "", "code", "docs", "rdr", "knowledge",
]


@pytest.mark.parametrize(
    "name, expected",
    list(zip(_CT_INPUTS, [
        "voyage-code-3", "voyage-context-3", "voyage-context-3", "voyage-context-3",
        "voyage-code-3", "voyage-code-3", "voyage-code-3",
        "voyage-code-3", "voyage-code-3", "voyage-code-3",
        "voyage-code-3", "voyage-code-3", "voyage-code-3", "voyage-code-3",
    ], strict=True)),
)
def test_voyage_model_for_collection_pinned(name: str, expected: str) -> None:
    assert voyage_model_for_collection(name) == expected


@pytest.mark.parametrize(
    "name, expected",
    list(zip(_CT_INPUTS, [
        0.70, 0.55, 0.50, 0.55,
        0.70, 0.70, 0.70,
        0.70, 0.70, 0.70,
        0.70, 0.70, 0.70, 0.70,
    ], strict=True)),
)
def test_default_projection_threshold_pinned(name: str, expected: float) -> None:
    assert default_projection_threshold(name) == expected


@pytest.mark.parametrize(
    "name, expected",
    list(zip(_CT_INPUTS, [
        "code", "docs", "knowledge", "rdr",
        "code", "code", "code",
        "code", "code", "code",
        "code", "code", "code", "code",
    ], strict=True)),
)
def test_legacy_content_type_for_collection_pinned(name: str, expected: str) -> None:
    """Regression pin for the specific bug caught while writing this
    slice: ``collection_content_type(name) or "code"`` is WRONG here --
    it only substitutes on an empty (no-``__``) result, but this
    function's contract defaults to "code" for ANY unrecognized prefix
    too (``other__x`` -> ``code``), not only a dunder-free name. The
    fix filters explicitly against ("docs", "knowledge", "rdr")."""
    assert _legacy_content_type_for_collection(name) == expected


# ── validate_collection_name's overflow hint ────────────────────────────────

@pytest.mark.parametrize(
    "name, expect_hint_type",
    [
        ("x" * 70, None),
        ("code__" + "y" * 70, "code"),
        ("docs__" + "y" * 70, "docs"),
        ("rdr__" + "y" * 70, "rdr"),
        ("knowledge__" + "y" * 70, "knowledge"),
        ("weirdo__" + "y" * 70, None),
        ("quarantine-docs__" + "y" * 70, None),
    ],
    ids=[
        "no_dunder_no_hint", "code_hint", "docs_hint", "rdr_hint",
        "knowledge_hint", "unrecognized_prefix_no_hint", "quarantine_prefix_no_hint",
    ],
)
def test_validate_collection_name_overflow_hint(name: str, expect_hint_type: str | None) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_collection_name(name)
    msg = str(exc_info.value)
    if expect_hint_type is None:
        assert "--content-type" not in msg
    else:
        assert f"--content-type {expect_hint_type}" in msg


# ── t3_collection_name: ct/rest branch + placeholder guard + bare-prefix probe ──

@pytest.mark.parametrize(
    "user_arg, expected",
    [
        ("distributed-systems", "knowledge__distributed-systems__voyage-context-3__v1"),
        ("knowledge__foo", "knowledge__foo__voyage-context-3__v1"),
        ("docs__bar", "docs__bar__voyage-context-3__v1"),
        ("code", "knowledge__code__voyage-context-3__v1"),
        ("docs", "knowledge__docs__voyage-context-3__v1"),
        ("knowledge", "knowledge__knowledge__voyage-context-3__v1"),
        ("rdr", "knowledge__rdr__voyage-context-3__v1"),
        ("code__myrepo__voyage-code-3__v1", "code__myrepo__voyage-code-3__v1"),
        ("test", "knowledge__test__voyage-context-3__v1"),
        ("default", "knowledge__default__voyage-context-3__v1"),
        # malformed multi-segment: ct="" (from an empty-first-segment name)
        # never happens for `foo__bar__baz` (ct="foo", not empty) -- pinned
        # separately below for the truly-degenerate ct="" case.
        ("foo__bar__baz", "foo__bar__baz"),
        ("weird__thing", "weird__thing"),
    ],
)
def test_t3_collection_name_pinned(user_arg: str, expected: str) -> None:
    assert t3_collection_name(user_arg) == expected


@pytest.mark.parametrize("user_arg", ["__b", "__"])
def test_t3_collection_name_degenerate_empty_content_type_returns_unchanged(user_arg: str) -> None:
    """A user_arg whose first '__'-delimited segment is empty fails the
    ``ct in CONTENT_TYPES`` membership test and is returned unchanged --
    this is the case the ct/rest split's explicit dunder-presence branch
    (rather than a single ``collection_content_type(...) or "knowledge"``)
    exists to keep distinct from the true bare-name "knowledge" default."""
    assert t3_collection_name(user_arg) == user_arg


@pytest.mark.parametrize(
    "user_arg",
    ["test", "default", "knowledge__test", "docs__default"],
)
def test_t3_collection_name_for_write_refuses_placeholder(user_arg: str) -> None:
    with pytest.raises(PlaceholderCollectionError):
        t3_collection_name(user_arg, for_write=True)


def test_t3_collection_name_for_write_accepts_real_subject() -> None:
    assert (
        t3_collection_name("distributed-systems", for_write=True)
        == "knowledge__distributed-systems__voyage-context-3__v1"
    )


# ── collection_registration_kwargs ──────────────────────────────────────────

def test_collection_registration_kwargs_conformant() -> None:
    assert collection_registration_kwargs("code__myrepo__voyage-code-3__v1") == {
        "content_type": "code",
        "owner_id": "myrepo",
        "embedding_model": "voyage-code-3",
        "model_version": "v1",
    }


def test_collection_registration_kwargs_legacy_two_segment() -> None:
    assert collection_registration_kwargs("knowledge__distributed-systems") == {
        "content_type": "knowledge",
        "owner_id": "distributed-systems",
        "embedding_model": "voyage-context-3",
        "model_version": "v1",
    }


def test_collection_registration_kwargs_bare_no_dunder() -> None:
    assert collection_registration_kwargs("my-notes") == {
        "content_type": "knowledge",
        "owner_id": "my-notes",
        "embedding_model": "voyage-context-3",
        "model_version": "v1",
    }
    # RDR-204: content_type is not restricted to the four canonical types
    # here by design (opaque test-fixture identifiers like "test__coll" are
    # a large pre-existing surface) -- both raise from
    # canonical_embedding_model, not from a shape guard in this function.


@pytest.mark.parametrize(
    "name",
    ["test__coll", "quarantine-docs__x", "foo__bar__baz__qux__extra", "foo__bar__"],
)
def test_collection_registration_kwargs_opaque_prefix_raises_from_embedding_model(
    name: str,
) -> None:
    with pytest.raises(ValueError, match="canonical_embedding_model"):
        collection_registration_kwargs(name)


@pytest.mark.parametrize("name", ["a__", "__b", "foo____bar"])
def test_collection_registration_kwargs_malformed_shape_raises(name: str) -> None:
    with pytest.raises(ValueError, match="has no <content_type>__<owner_id> shape"):
        collection_registration_kwargs(name)


def test_collection_registration_kwargs_empty_name() -> None:
    assert collection_registration_kwargs("") == {
        "content_type": "knowledge",
        "owner_id": "",
        "embedding_model": "voyage-context-3",
        "model_version": "v1",
    }


def test_content_types_membership_still_the_public_alias() -> None:
    """Sanity: CONTENT_TYPES is unchanged by the funnel (used directly by
    two of the pinned sites above)."""
    assert CONTENT_TYPES == ("code", "docs", "rdr", "knowledge")
