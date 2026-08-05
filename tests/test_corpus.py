"""AC2/AC6: Embedding model selection and --corpus prefix resolution."""
import pytest

from nexus.corpus import (
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
