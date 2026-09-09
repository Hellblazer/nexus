# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-204 Phase 3 (nexus-ft04v.21 funnel, nexus-ft04v.26 THE REPOINT):
unit tests for the three collection-name identity helpers in
``nexus.corpus`` (``collection_content_type``, ``collection_owner``,
``collection_model``) and pinning tests for every downstream call site
this slice funnelled through them.

nexus-ft04v.26 landed: these three helpers no longer parse the collection
name at all -- they read ``nexus.catalog_collections`` (the collection-row
cache, ``nexus.mcp_infra.get_collection_row``) and raise
``CollectionNotRegisteredError`` when a name has no row, never falling
back to a string parse. The row-read + fail-loud contract is pinned
directly below; the "authority moved" story (a name whose string
disagrees with its row resolves BY THE ROW) is pinned in
``tests/test_corpus.py::test_collection_content_type_row_based_repoint``.

The DOWNSTREAM functions this file also pins
(``voyage_model_for_collection``, ``default_projection_threshold``,
``_legacy_content_type_for_collection``) still call
``collection_content_type`` internally, so their tests fake a catalog row
per input (``_fake_collection_rows`` below) deriving content_type from
the SAME first-segment convention the retired parser used -- these tests
are about the content-type-to-model DISPATCH table, not the row-lookup
mechanism, which is covered separately.
"""
from __future__ import annotations

import pytest

from nexus.corpus import (
    CONTENT_TYPES,
    CollectionNotRegisteredError,
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


@pytest.fixture
def fake_row(monkeypatch):
    """Install a single-name-to-row map as ``nexus.mcp_infra.get_collection_row``
    and return the dict so a test can populate it. Absent from the dict ->
    ``None`` (no catalog row), matching the real accessor's contract."""
    import nexus.mcp_infra as mi

    rows: dict[str, dict] = {}
    monkeypatch.setattr(mi, "get_collection_row", lambda name: rows.get(name))
    return rows


@pytest.fixture(autouse=True)
def _fake_collection_rows(monkeypatch):
    """For the DOWNSTREAM content-type-to-model dispatch tests below
    (voyage_model_for_collection / default_projection_threshold /
    _legacy_content_type_for_collection): derives a row from the same
    first-segment convention the retired parser used, so those tests keep
    exercising their own dispatch table rather than the row-lookup
    mechanism. Tests of the three funnel helpers THEMSELVES override this
    via the ``fake_row`` fixture (function-scoped, applied after this one)."""
    import nexus.mcp_infra as mi

    def _fake_get_collection_row(name: str) -> dict | None:
        content_type = name.partition("__")[0] if "__" in name else ""
        return {
            "content_type": content_type,
            "owner_id": "test-owner",
            "embedding_model": "test-model",
            "lifecycle_state": "live",
        }

    monkeypatch.setattr(mi, "get_collection_row", _fake_get_collection_row)


# ── The three helpers themselves: row-read + fail-loud ─────────────────────

@pytest.mark.parametrize(
    "field, accessor",
    [
        ("content_type", collection_content_type),
        ("owner_id", collection_owner),
        ("embedding_model", collection_model),
    ],
    ids=["collection_content_type", "collection_owner", "collection_model"],
)
def test_funnel_helper_reads_the_row_field(fake_row, field, accessor) -> None:
    fake_row["code__myrepo__voyage-code-3__v1"] = {
        "content_type": "docs", "owner_id": "row-owner", "embedding_model": "row-model",
        "lifecycle_state": "live",
    }
    assert accessor("code__myrepo__voyage-code-3__v1") == fake_row[
        "code__myrepo__voyage-code-3__v1"
    ][field]


@pytest.mark.parametrize(
    "accessor",
    [collection_content_type, collection_owner, collection_model],
    ids=["collection_content_type", "collection_owner", "collection_model"],
)
def test_funnel_helper_fails_loud_on_no_row(fake_row, accessor) -> None:
    with pytest.raises(CollectionNotRegisteredError):
        accessor("docs__never-registered__voyage-context-3__v1")


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
