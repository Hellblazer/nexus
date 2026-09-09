"""AC2/AC6: Embedding model selection and --corpus prefix resolution."""
import re
from pathlib import Path

import pytest

from nexus.corpus import (
    PLACEHOLDER_SUBJECTS,
    PlaceholderCollectionError,
    embedding_model_for_collection,
    index_model_for_collection,
    resolve_corpus,
    t3_collection_name,
    validate_collection_name,
)

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
pytestmark = pytest.mark.usefixtures("cloud_mode")


@pytest.fixture(autouse=True)
def _fake_collection_rows(monkeypatch):
    """RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26): collection_content_type/
    collection_owner/collection_model (and everything built on them --
    voyage_model_for_collection, resolve_corpus's bare-content-type stage)
    now read a collection's catalog row via
    ``nexus.mcp_infra.get_collection_row`` instead of parsing the name.
    This file's fixture collection names (``code__myrepo``, ``bare_name``,
    ...) are bare test strings with no real row behind them -- this fake
    derives a row from the SAME first-segment convention the retired
    string-parse used, so the content-type-to-model DISPATCH logic these
    tests exercise is unaffected by the row-vs-name authority change these
    tests are not about. The fail-loud-on-no-row contract itself is
    covered separately by test_collection_content_type_row_based_repoint
    below.
    """
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


# ── Embedding model selection ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "collection, expected",
    [
        ("code__myrepo", "voyage-code-3"),
        ("docs__papers", "voyage-context-3"),
        ("knowledge__security", "voyage-context-3"),
        ("rdr__myrepo-abcdef12", "voyage-context-3"),
        ("other__collection", "voyage-code-3"),
    ],
    ids=["code", "docs", "knowledge", "rdr", "unknown_prefix_defaults_voyage_code3"],
)
def test_embedding_model_for_collection(collection: str, expected: str) -> None:
    assert embedding_model_for_collection(collection) == expected


def test_embedding_model_for_collection_regression() -> None:
    """Query model must match index model for each collection type.

    Mismatched models produce random noise (cosine sim ≈ 0.05).
    See RDR-059: code__ was queried with voyage-4 against voyage-code-3 index.
    """
    # CCE collections → voyage-context-3
    assert embedding_model_for_collection("docs__papers") == "voyage-context-3"
    assert embedding_model_for_collection("knowledge__security") == "voyage-context-3"
    assert embedding_model_for_collection("rdr__myrepo-abcdef12") == "voyage-context-3"
    # Code collections → voyage-code-3 (matches index model)
    assert embedding_model_for_collection("code__myrepo") == "voyage-code-3"
    # Unknown prefix → voyage-code-3 (safe default)
    assert embedding_model_for_collection("other__collection") == "voyage-code-3"


# ── index_model_for_collection ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "collection, expected",
    [
        ("code__myrepo", "voyage-code-3"),
        ("docs__manual", "voyage-context-3"),
        ("knowledge__wiki", "voyage-context-3"),
        ("rdr__myrepo-abcdef12", "voyage-context-3"),
        ("scratch__anything", "voyage-code-3"),
        ("bare_name", "voyage-code-3"),
    ],
    ids=[
        "code",
        "docs_cce",
        "knowledge_cce",
        "rdr_cce",
        "unrecognized_prefix_defaults",
        "no_separator_defaults",
    ],
)
def test_index_model_for_collection(collection: str, expected: str) -> None:
    assert index_model_for_collection(collection) == expected


# ── A3: Cross-model invariant regression ─────────────────────────────────────

def test_cce_index_query_model_invariant() -> None:
    """Joint invariant: CCE index model requires CCE query model.

    The original CCE bug (post-mortem: cce-query-model-mismatch) had
    index_model_for_collection returning voyage-context-3 while
    embedding_model_for_collection returned voyage-4. This test catches
    that exact regression by checking both functions agree for CCE prefixes.
    """
    cce_prefixes = ("docs__papers", "knowledge__security", "rdr__myrepo-abcdef12")
    for prefix in cce_prefixes:
        idx = index_model_for_collection(prefix)
        qry = embedding_model_for_collection(prefix)
        if idx == "voyage-context-3":
            assert qry == "voyage-context-3", (
                f"{prefix}: CCE index model ({idx}) requires CCE query model, "
                f"got query={qry}. See post-mortem: cce-query-model-mismatch"
            )

    # Non-CCE prefixes: query model must match index model (RDR-059 fix)
    non_cce = ("code__repo", "scratch__temp")
    for prefix in non_cce:
        idx = index_model_for_collection(prefix)
        qry = embedding_model_for_collection(prefix)
        assert idx == qry, (
            f"{prefix}: index model ({idx}) must match query model ({qry}). "
            f"Mismatched models produce random noise. See RDR-059."
        )


# ── Corpus prefix resolution ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "query, all_cols, expected",
    [
        (
            "code",
            ["code__myrepo", "code__otherrepo", "docs__papers", "knowledge__security"],
            ["code__myrepo", "code__otherrepo"],
        ),
        (
            "knowledge",
            ["code__myrepo", "knowledge__sec", "knowledge__arch"],
            ["knowledge__sec", "knowledge__arch"],
        ),
        (
            "code__myrepo",
            ["code__myrepo", "code__otherrepo", "docs__papers"],
            ["code__myrepo"],
        ),
        ("code", ["docs__papers", "knowledge__x"], []),
        (
            "docs",
            ["docs__papers", "docs__books", "code__myrepo"],
            ["docs__papers", "docs__books"],
        ),
        # RDR-103 follow-up: a user typing the legacy two-segment name
        # (`knowledge__security`) should still match the conformant
        # `knowledge__security__voyage-context-3__v1` collection that the
        # store auto-promotes to. Without this fallback, `nx store put`
        # and `nx search` disagree on the name and the search silently
        # misses.
        (
            "knowledge__security",
            [
                "knowledge__security__voyage-context-3__v1",
                "knowledge__other__voyage-context-3__v1",
            ],
            ["knowledge__security__voyage-context-3__v1"],
        ),
        # When an exact match exists, it is preferred and prefix is not used.
        (
            "knowledge__foo",
            ["knowledge__foo", "knowledge__foo__voyage-context-3__v1"],
            ["knowledge__foo"],
        ),
        (
            "rdr",
            ["code__myrepo", "docs__papers", "rdr__myrepo-abcdef12"],
            ["rdr__myrepo-abcdef12"],
        ),
        # --corpus rdr must NOT match docs__rdr__* (the old buggy naming).
        (
            "rdr",
            ["docs__rdr__myrepo", "rdr__myrepo-abcdef12"],
            ["rdr__myrepo-abcdef12"],
        ),
        # --corpus docs must NOT match rdr__* collections.
        ("docs", ["docs__papers", "rdr__myrepo-abcdef12"], ["docs__papers"]),
        # --corpus code must NOT match 'codebase__x' (only 'code__*').
        ("code", ["codebase__myrepo", "code__myrepo"], ["code__myrepo"]),
        # Corpus arg with __ uses exact match, even with multiple __ separators.
        (
            "code__repo__extra",
            ["code__repo__extra", "code__repo"],
            ["code__repo__extra"],
        ),
    ],
    ids=[
        "code_prefix",
        "knowledge_prefix",
        "exact_match",
        "no_match_returns_empty",
        "docs_prefix",
        "two_segment_matches_conformant_suffix",
        "exact_wins_over_prefix",
        "rdr_prefix",
        "rdr_does_not_match_docs_rdr",
        "docs_does_not_match_rdr",
        "prefix_requires_double_underscore",
        "multiple_separators_exact_match",
    ],
)
def test_resolve_corpus_prefix_matching(
    query: str, all_cols: list[str], expected: list[str]
) -> None:
    assert resolve_corpus(query, all_cols) == expected


# ── RDR-204 Phase 3 THE REPOINT (nexus-ft04v.26) ──────────────────────────────

def test_collection_content_type_row_based_repoint(monkeypatch) -> None:
    """The funnel helpers read the catalog row, never the name -- and fail
    loud rather than fall back to parsing when no row backs the name.

    This is the test that proves the authority MOVED (RDR §Test Plan): a
    collection whose NAME says one content type and whose ROW says
    another resolves BY THE ROW.
    """
    import nexus.mcp_infra as mi
    from nexus.corpus import (
        CollectionNotRegisteredError,
        collection_content_type,
        collection_model,
        collection_owner,
    )

    def _row(name: str) -> dict | None:
        if name == "code__nexus__voyage-code-3__v1":
            # The NAME says "code"; the ROW disagrees (the exact drift
            # class GH #667 came from). The row must win.
            return {
                "content_type": "docs",
                "owner_id": "nexus-row-owner",
                "embedding_model": "voyage-context-3",
                "lifecycle_state": "live",
            }
        return None

    monkeypatch.setattr(mi, "get_collection_row", _row)

    assert collection_content_type("code__nexus__voyage-code-3__v1") == "docs"
    assert collection_owner("code__nexus__voyage-code-3__v1") == "nexus-row-owner"
    assert collection_model("code__nexus__voyage-code-3__v1") == "voyage-context-3"

    for fn in (collection_content_type, collection_owner, collection_model):
        with pytest.raises(CollectionNotRegisteredError):
            fn("docs__never-registered__voyage-context-3__v1")


@pytest.mark.parametrize(
    "lifecycle_state",
    ["quarantine", "dormant", "disputed"],
    ids=["quarantine_excluded", "dormant_excluded", "disputed_excluded"],
)
def test_resolve_corpus_excludes_non_live_lifecycle_states(
    monkeypatch, lifecycle_state: str,
) -> None:
    """RDR-204 Gap 4: a bare-content-type corpus fan-out excludes every
    non-``live`` row BY COLUMN -- quarantine, dormant and disputed each
    asserted separately, never lumped into one case."""
    import nexus.mcp_infra as mi

    rows = {
        "code__nexus__voyage-code-3__v1": {
            "content_type": "code", "owner_id": "nexus", "embedding_model": "voyage-code-3",
            "lifecycle_state": "live",
        },
        "quarantine-code__nexus__voyage-code-3__v1": {
            "content_type": "code", "owner_id": "nexus", "embedding_model": "voyage-code-3",
            "lifecycle_state": lifecycle_state,
        },
    }
    monkeypatch.setattr(mi, "get_collection_row", lambda name: rows.get(name))

    result = resolve_corpus("code", list(rows))
    assert result == ["code__nexus__voyage-code-3__v1"], (
        f"a {lifecycle_state} row must never join a bare 'code' fan-out"
    )


def test_resolve_corpus_drops_unregistered_name_from_fanout(monkeypatch) -> None:
    """A candidate with no catalog row at all is dropped from a bare-corpus
    fan-out (never raised loud -- RDR §Failure Modes: a fan-out skips an
    unregistered name with a logged warning, only a direct single-collection
    read is a hard 422)."""
    import nexus.mcp_infra as mi

    monkeypatch.setattr(mi, "get_collection_row", lambda name: None)

    assert resolve_corpus("code", ["code__ghost__voyage-code-3__v1"]) == []


# ── RDR-204 Phase 3 item 7: owner grammar alignment ───────────────────────────

def test_is_conformant_collection_name_admits_underscored_owner() -> None:
    """is_conformant_collection_name used to be STRICTER than the physical
    name regex (_COLLECTION_NAME_RE), rejecting an underscored owner a real
    ChromaDB-shaped name allows. Aligning the two (nexus-ft04v.26 item 7)
    means an underscored owner renders and round-trips through parse."""
    from nexus.catalog.collection_name import CollectionName
    from nexus.corpus import is_conformant_collection_name, parse_conformant_collection_name

    name = CollectionName(
        content_type="code", owner_id="my_repo", embedding_model="voyage-code-3", model_version=1,
    ).render()
    assert name == "code__my_repo__voyage-code-3__v1"
    assert is_conformant_collection_name(name)
    parsed = parse_conformant_collection_name(name)
    assert parsed["owner_id"] == "my_repo"
    assert CollectionName.parse(name) == CollectionName(
        content_type="code", owner_id="my_repo", embedding_model="voyage-code-3", model_version=1,
    )


# ── validate_collection_name ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name",
    [
        "code__myrepo",
        "knowledge__security",
        "abc",
        "a" * 63,  # exactly 63 chars: maximum valid length
        "a1b",  # exactly 3 chars: minimum valid length
        "code__myrepo",  # double underscore in the middle is valid
        "a__b",
        "1abc9",  # digits at boundaries are valid
        "123",
    ],
    ids=[
        "realistic_code_name",
        "realistic_knowledge_name",
        "minimal_name",
        "exactly_63_chars",
        "exactly_3_chars",
        "double_underscore_realistic",
        "double_underscore_minimal",
        "digit_boundaries_mixed",
        "digit_boundaries_all_digits",
    ],
)
def test_validate_collection_name_accepts(name: str) -> None:
    validate_collection_name(name)  # should not raise


@pytest.mark.parametrize(
    "name, match",
    [
        ("ab", "3"),
        ("a" * 64, "63"),
        ("bad:name", "alphanumeric"),
        ("-badstart", "alphanumeric"),
        ("badend-", "alphanumeric"),
        ("", "3"),
        ("a", "3"),
        ("_badstart", "alphanumeric"),
        ("badend_", "alphanumeric"),
        *[
            (f"bad{char}name", "alphanumeric")
            for char in [".", " ", "/", "@", "+", "%", "=", "!", "~"]
        ],
    ],
    ids=[
        "too_short",
        "too_long",
        "invalid_chars_colon",
        "starts_with_hyphen",
        "ends_with_hyphen",
        "empty_string",
        "single_char",
        "starts_with_underscore",
        "ends_with_underscore",
        "special_char_dot",
        "special_char_space",
        "special_char_slash",
        "special_char_at",
        "special_char_plus",
        "special_char_percent",
        "special_char_equals",
        "special_char_bang",
        "special_char_tilde",
    ],
)
def test_validate_collection_name_rejects(name: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_collection_name(name)


# ── nexus-hmxi: t3-aware grandfathering ──────────────────────────────────────


class _FakeT3:
    """Minimal T3 stand-in for the legacy-grandfathering probe."""

    def __init__(self, collections: set[str]) -> None:
        self._collections = set(collections)

    def collection_exists(self, name: str) -> bool:
        return name in self._collections

    def list_collections(self) -> list[dict]:
        return [{"name": c} for c in sorted(self._collections)]


# t3_collection_name behavior with NO t3 probe supplied: unconditional
# auto-promotion (RDR-103 Phase 5). Distinct from the t3-probe-supplied
# grandfathering tests below.
@pytest.mark.parametrize(
    "arg, expected",
    [
        ("knowledge", "knowledge__knowledge__voyage-context-3__v1"),
        ("knowledge__security", "knowledge__security__voyage-context-3__v1"),
        # Bare arg is treated as the owner segment of a knowledge
        # collection (the historical default), promoted to conformant.
        ("code", "knowledge__code__voyage-context-3__v1"),
        # A 4-segment conformant name is returned untouched (no double
        # promotion).
        (
            "knowledge__existing__voyage-context-3__v1",
            "knowledge__existing__voyage-context-3__v1",
        ),
        ("knowledge__existing", "knowledge__existing__voyage-context-3__v1"),
        # ``code__myrepo`` promotes to ``code__myrepo__voyage-code-3__v1``;
        # the canonical embedding model is selected from the content_type
        # prefix, not assumed.
        ("code__myrepo", "code__myrepo__voyage-code-3__v1"),
        # Matches the pre-nexus-hmxi contract: without a t3 probe (static
        # / test contexts), the resolver auto-promotes unconditionally.
        ("knowledge__art", "knowledge__art__voyage-context-3__v1"),
    ],
    ids=[
        "bare_knowledge_promotes",
        "two_segment_with_separator_promotes",
        "bare_code_treated_as_knowledge_owner",
        "already_conformant_passthrough",
        "two_segment_knowledge_promotes",
        "other_prefix_promotes_to_canonical_model",
        "no_t3_probe_always_promotes_pre_hmxi_contract",
    ],
)
def test_t3_collection_name_promotion_without_t3_probe(arg: str, expected: str) -> None:
    assert t3_collection_name(arg) == expected


# t3-supplied grandfathering priority for a 2-segment arg
# (`knowledge__art`): legacy-only wins, legacy-absent promotes,
# both-exist prefers conformant (nexus-hmxi).
@pytest.mark.parametrize(
    "t3_collections, expected",
    [
        ({"knowledge__art"}, "knowledge__art"),
        (set(), "knowledge__art__voyage-context-3__v1"),
        (
            {"knowledge__art", "knowledge__art__voyage-context-3__v1"},
            "knowledge__art__voyage-context-3__v1",
        ),
    ],
    ids=[
        "legacy_only_wins_over_missing_conformant",
        "legacy_absent_promotes_to_conformant",
        "both_exist_prefers_conformant",
    ],
)
def test_t3_collection_name_grandfathering_priority(
    t3_collections: set[str], expected: str
) -> None:
    """nexus-hmxi: with a t3 probe, an existing legacy 2-segment
    collection wins over the auto-promoted conformant target so put /
    list / search all resolve to the same physical collection — unless
    the conformant collection ALSO exists (mid-migration), in which case
    the conformant target wins so in-progress migrations converge.
    """
    t3 = _FakeT3(t3_collections)
    assert t3_collection_name("knowledge__art", t3=t3) == expected


# Symmetric priority chain for the bare-prefix shorthand ('knowledge'
# instead of 'knowledge__art') — GH #535/#536.
@pytest.mark.parametrize(
    "t3_collections, expected",
    [
        ({"knowledge__knowledge"}, "knowledge__knowledge"),
        (set(), "knowledge__knowledge__voyage-context-3__v1"),
        (
            {"knowledge__knowledge", "knowledge__knowledge__voyage-context-3__v1"},
            "knowledge__knowledge__voyage-context-3__v1",
        ),
    ],
    ids=[
        "bare_prefix_falls_back_to_2segment_legacy",
        "bare_prefix_promotes_when_no_legacy",
        "bare_prefix_prefers_conformant_when_both_exist",
    ],
)
def test_t3_collection_name_bare_prefix_priority(
    t3_collections: set[str], expected: str
) -> None:
    """#535/#536: bare-prefix arg ('knowledge') must reach the documented
    legacy 2-segment collection ('knowledge__knowledge') when that's the
    only physical collection that exists, promote when it's absent, and
    prefer the conformant shape when both exist — mirroring the
    2-segment-arg priority chain above.

    Pre-fix: nx store list (no args, default --collection knowledge) on
    installs with knowledge__knowledge from before the RDR-103 transition
    returned 'No entries' because the resolver promoted to
    knowledge__knowledge__voyage-context-3__v1 (which does not exist) and
    never tried the 2-segment legacy fallback.
    """
    t3 = _FakeT3(t3_collections)
    assert t3_collection_name("knowledge", t3=t3) == expected


def test_t3_collection_name_bare_prefix_falls_through_when_multiple() -> None:
    """GH #545: when 2+ ``code__*`` collections exist, the unique-match
    branch falls through to the existing promotion logic so the
    operator gets back the conformant target. This documents the
    behaviour rather than the ideal (a candidate-list disambiguation
    error would be cleaner; that's a separate UX call captured in #545).
    """
    t3 = _FakeT3({
        "code__a__voyage-code-3__v1",
        "code__b__voyage-code-3__v1",
    })
    # Falls through to promotion: bare ``code`` -> knowledge__code__...
    # Not ideal but documents the current behaviour. The fix's value
    # is the unique-match path, which is the common case.
    out = t3_collection_name("code", t3=t3)
    assert "code" in out  # don't pin the exact promoted shape


# GH #545: bare prefix ("code" / "docs" / "rdr") on installs that have
# exactly one matching ``{prefix}__*`` collection must resolve to it.
# Pre-fix the resolver treated bare ``code`` as an owner under
# content_type ``knowledge`` and produced
# ``knowledge__code__voyage-context-3__v1`` — wrong namespace.
@pytest.mark.parametrize(
    "prefix, only_collection",
    [
        ("code", "code__myrepo__voyage-code-3__v1"),
        ("docs", "docs__myrepo__voyage-context-3__v1"),
        ("rdr", "rdr__nexus__voyage-context-3__v1"),
    ],
    ids=["code", "docs", "rdr"],
)
def test_t3_collection_name_bare_prefix_resolves_to_unique_match(
    prefix: str, only_collection: str
) -> None:
    t3 = _FakeT3({only_collection})
    assert t3_collection_name(prefix, t3=t3) == only_collection


def test_t3_collection_name_bare_knowledge_still_uses_legacy_fallback() -> None:
    """GH #545 backwards-compat: the existing ``knowledge`` -> ``knowledge__knowledge``
    legacy fallback (#536) must still fire when the bare-prefix probe
    returns no unique match (e.g. no ``knowledge__*`` collections of
    any other shape exist).
    """
    legacy = "knowledge__knowledge"
    t3 = _FakeT3({legacy})  # only the legacy 2-seg, no other knowledge__*
    # Probe sees one match, returns it. (Single-match path.)
    assert t3_collection_name("knowledge", t3=t3) == legacy


def test_t3_collection_name_t3_probe_failure_falls_through_to_promoted() -> None:
    """When the t3 probe raises (cloud transient / quota error), the
    resolver falls through to the auto-promoted shape; legacy reads
    still work via T3's existing-collection bypass on read paths."""
    class _RaisingT3:
        def collection_exists(self, name):  # noqa: D401
            raise RuntimeError("transient cloud error")
    assert (
        t3_collection_name("knowledge__art", t3=_RaisingT3())
        == "knowledge__art__voyage-context-3__v1"
    )


# nexus-0f3h: multi-match tie-break priority (conformant 4-segment
# default > legacy 2-segment default > deterministic alphabetical
# first) when 2+ ``{prefix}__*`` collections exist and neither the
# unique-match nor the exact-passthrough paths apply.
@pytest.mark.parametrize(
    "t3_collections, expected",
    [
        (
            {
                "code__nexus-1__voyage-code-3__v1",
                "code__myrepo__voyage-code-3__v1",
                "code__code__voyage-code-3__v1",  # the conformant default
            },
            "code__code__voyage-code-3__v1",
        ),
        (
            {
                "code__nexus-1__voyage-code-3__v1",
                "code__myrepo__voyage-code-3__v1",
                "code__code",  # legacy 2-segment default
            },
            "code__code",
        ),
        (
            {
                "code__myrepo-bbb__voyage-code-3__v1",
                "code__myrepo-aaa__voyage-code-3__v1",
                "code__myrepo-ccc__voyage-code-3__v1",
            },
            "code__myrepo-aaa__voyage-code-3__v1",
        ),
        (
            {
                "code__b__voyage-code-3__v1",
                "code__a__voyage-code-3__v1",
            },
            "code__a__voyage-code-3__v1",
        ),
    ],
    ids=[
        "picks_conformant_4seg_default",
        "picks_2seg_legacy_when_no_conformant_default",
        "picks_alphabetical_first_when_no_canonical_default",
        "picks_alphabetical_when_no_canonical_2_candidates",
    ],
)
def test_t3_collection_name_bare_prefix_multi_match_tie_break(
    t3_collections: set[str], expected: str
) -> None:
    t3 = _FakeT3(t3_collections)
    out = t3_collection_name("code", t3=t3)
    assert out == expected
    # Anti-regression: must never land in the wrong knowledge__ namespace
    # (the pre-fix fall-through-to-promotion bug for the multi-match case).
    assert not out.startswith("knowledge__"), out


def test_t3_collection_name_bare_knowledge_falls_through_to_legacy_default() -> None:
    """nexus-0f3h regression guard: bare ``knowledge`` on an install
    with multiple ``knowledge__*`` collections (none of which is the
    ``knowledge__knowledge`` 2-seg default) MUST NOT pick alphabetical
    first. The historical contract — ``knowledge`` resolves to the
    auto-promoted ``knowledge__knowledge__voyage-context-3__v1`` (or
    the legacy 2-seg ``knowledge__knowledge`` if it exists) — is the
    one the test suite + production tooling locks.
    """
    # Multiple knowledge__ matches, none is knowledge__knowledge.
    t3 = _FakeT3({
        "knowledge__art",
        "knowledge__delos",
        "knowledge__greenfield__voyage-context-3__v1",
    })
    out = t3_collection_name("knowledge", t3=t3)
    # MUST be the auto-promoted shape (no knowledge__knowledge on disk).
    assert out == "knowledge__knowledge__voyage-context-3__v1"



# nexus-0fw11 (Sam, 2026-09-08): a write that names a placeholder where a
# subject was required is refused at the one resolver every writer uses.


@pytest.mark.parametrize("name", ["knowledge", "default", "test", "notes", "tmp",
                                  "knowledge__knowledge", "docs__default", "knowledge__test"])
def test_write_resolution_refuses_placeholder_subjects(name: str) -> None:
    with pytest.raises(PlaceholderCollectionError) as excinfo:
        t3_collection_name(name, for_write=True)
    assert "docs/collections.md" in str(excinfo.value)
    assert name in str(excinfo.value)


@pytest.mark.parametrize("name", ["knowledge", "default", "knowledge__knowledge", "docs__default"])
def test_read_resolution_still_accepts_placeholder_subjects(name: str) -> None:
    """Reads keep resolving so existing placeholder collections stay reachable."""
    assert t3_collection_name(name)


def test_conformant_placeholder_name_passes_through_on_write() -> None:
    """The deliberate escape: the full four-segment name of an existing
    placeholder collection is accepted verbatim, so nothing already minted
    becomes unwritable by name."""
    full = "knowledge__knowledge__voyage-context-3__v1"
    assert t3_collection_name(full, for_write=True) == full


def test_real_subject_is_accepted_on_write() -> None:
    assert t3_collection_name("distributed-systems", for_write=True) == (
        "knowledge__distributed-systems__voyage-context-3__v1"
    )


def test_placeholder_set_matches_docs_collections_rule_1() -> None:
    """docs/collections.md Rule 1 lists the placeholders in one parenthetical;
    the code's set is that list, checked mechanically so the two cannot drift."""
    text = (Path(__file__).resolve().parents[1] / "docs" / "collections.md").read_text()
    m = re.search(r"a placeholder\s*\(([^)]*)\)", text)
    assert m, "docs/collections.md no longer lists the placeholders in a parenthetical"
    documented = frozenset(re.findall(r"`([^`]+)`", m.group(1)))
    assert documented == PLACEHOLDER_SUBJECTS


def test_placeholder_refusal_is_a_click_exception_for_every_cli_writer() -> None:
    """Critique of 86cd65ef0: only nx store put caught the error; promote,
    index and dt index printed a traceback. The exception is a
    ClickException, so every command prints it and exits 1."""
    import click

    with pytest.raises(click.ClickException) as excinfo:
        t3_collection_name("knowledge", for_write=True)
    assert isinstance(excinfo.value, PlaceholderCollectionError)


def test_allow_placeholder_lifts_the_refusal_for_a_restore() -> None:
    assert t3_collection_name("knowledge__knowledge", for_write=True, allow_placeholder=True) == (
        "knowledge__knowledge__voyage-context-3__v1"
    )
