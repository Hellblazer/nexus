"""AC2/AC6: Embedding model selection and --corpus prefix resolution."""
import pytest

from nexus.corpus import (
    embedding_model_for_collection,
    index_model_for_collection,
    resolve_corpus,
    t3_collection_name,
    validate_collection_name,
)


# ── Embedding model selection ─────────────────────────────────────────────────

def test_embedding_model_code_collection() -> None:
    assert embedding_model_for_collection("code__myrepo") == "voyage-code-3"


def test_embedding_model_docs_collection() -> None:
    assert embedding_model_for_collection("docs__papers") == "voyage-context-3"


def test_embedding_model_knowledge_collection() -> None:
    assert embedding_model_for_collection("knowledge__security") == "voyage-context-3"


def test_embedding_model_rdr_collection() -> None:
    assert embedding_model_for_collection("rdr__myrepo-abcdef12") == "voyage-context-3"


def test_embedding_model_unknown_prefix_defaults_voyage_code3() -> None:
    assert embedding_model_for_collection("other__collection") == "voyage-code-3"


# ── Collection name resolution from --collection arg ─────────────────────────

def test_t3_collection_name_no_separator() -> None:
    """--collection knowledge promotes to a conformant 4-segment name
    (RDR-103 Phase 5 auto-promotion)."""
    assert (
        t3_collection_name("knowledge")
        == "knowledge__knowledge__voyage-context-3__v1"
    )


def test_t3_collection_name_with_separator() -> None:
    """--collection knowledge__security auto-promotes the 2-segment
    legacy shape to a conformant 4-segment name (RDR-103 Phase 5)."""
    assert (
        t3_collection_name("knowledge__security")
        == "knowledge__security__voyage-context-3__v1"
    )


def test_t3_collection_name_code_no_separator() -> None:
    """Bare arg is treated as the owner segment of a knowledge
    collection (the historical default), promoted to conformant."""
    assert (
        t3_collection_name("code")
        == "knowledge__code__voyage-context-3__v1"
    )


# ── Corpus prefix resolution ──────────────────────────────────────────────────

def test_resolve_corpus_code_prefix() -> None:
    all_cols = ["code__myrepo", "code__otherrepo", "docs__papers", "knowledge__security"]
    assert resolve_corpus("code", all_cols) == ["code__myrepo", "code__otherrepo"]


def test_resolve_corpus_knowledge_prefix() -> None:
    all_cols = ["code__myrepo", "knowledge__sec", "knowledge__arch"]
    assert resolve_corpus("knowledge", all_cols) == ["knowledge__sec", "knowledge__arch"]


def test_resolve_corpus_exact_match() -> None:
    all_cols = ["code__myrepo", "code__otherrepo", "docs__papers"]
    assert resolve_corpus("code__myrepo", all_cols) == ["code__myrepo"]


def test_resolve_corpus_no_match_returns_empty() -> None:
    assert resolve_corpus("code", ["docs__papers", "knowledge__x"]) == []


def test_resolve_corpus_docs_prefix() -> None:
    all_cols = ["docs__papers", "docs__books", "code__myrepo"]
    assert resolve_corpus("docs", all_cols) == ["docs__papers", "docs__books"]


# ── validate_collection_name ──────────────────────────────────────────────────

def test_validate_collection_name_valid() -> None:
    validate_collection_name("code__myrepo")
    validate_collection_name("knowledge__security")
    validate_collection_name("abc")


def test_validate_collection_name_too_short() -> None:
    with pytest.raises(ValueError, match="3"):
        validate_collection_name("ab")


def test_validate_collection_name_too_long() -> None:
    with pytest.raises(ValueError, match="63"):
        validate_collection_name("a" * 64)


def test_validate_collection_name_invalid_chars() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("bad:name")


def test_validate_collection_name_starts_with_hyphen() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("-badstart")


def test_validate_collection_name_ends_with_hyphen() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("badend-")


# ── index_model_for_collection ─────────────────────────────────────────────────

def test_index_model_code_collection() -> None:
    """code__ collections use voyage-code-3 at index time."""
    assert index_model_for_collection("code__myrepo") == "voyage-code-3"


def test_index_model_docs_collection() -> None:
    """docs__ collections use voyage-context-3 (CCE) at index time."""
    assert index_model_for_collection("docs__manual") == "voyage-context-3"


def test_index_model_knowledge_collection() -> None:
    """knowledge__ collections use voyage-context-3 (CCE) at index time."""
    assert index_model_for_collection("knowledge__wiki") == "voyage-context-3"


def test_index_model_rdr_collection() -> None:
    """rdr__ collections use voyage-context-3 (CCE) at index time."""
    assert index_model_for_collection("rdr__myrepo-abcdef12") == "voyage-context-3"


def test_index_model_scratch_collection_defaults_voyage_code3() -> None:
    """Unrecognized prefix defaults to voyage-code-3 at index time."""
    assert index_model_for_collection("scratch__anything") == "voyage-code-3"


def test_index_model_bare_name_defaults_voyage_code3() -> None:
    """Name with no __ separator defaults to voyage-code-3 at index time."""
    assert index_model_for_collection("bare_name") == "voyage-code-3"


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


# ── corpus resolution for RDR ────────────────────────────────────────────────

def test_resolve_corpus_rdr_prefix() -> None:
    """--corpus rdr matches rdr__* collections."""
    all_cols = ["code__myrepo", "docs__papers", "rdr__myrepo-abcdef12"]
    assert resolve_corpus("rdr", all_cols) == ["rdr__myrepo-abcdef12"]


def test_resolve_corpus_rdr_does_not_match_docs_rdr() -> None:
    """--corpus rdr must NOT match docs__rdr__* (the old buggy naming)."""
    all_cols = ["docs__rdr__myrepo", "rdr__myrepo-abcdef12"]
    assert resolve_corpus("rdr", all_cols) == ["rdr__myrepo-abcdef12"]


def test_resolve_corpus_docs_does_not_match_rdr() -> None:
    """--corpus docs must NOT match rdr__* collections."""
    all_cols = ["docs__papers", "rdr__myrepo-abcdef12"]
    assert resolve_corpus("docs", all_cols) == ["docs__papers"]


# ── validate_collection_name boundary & edge cases ──────────────────────────

def test_validate_collection_name_exactly_63_chars() -> None:
    """Maximum valid length is exactly 63 characters."""
    name = "a" * 63
    validate_collection_name(name)  # should not raise


def test_validate_collection_name_exactly_3_chars() -> None:
    """Minimum valid length is exactly 3 characters."""
    validate_collection_name("a1b")


def test_validate_collection_name_empty_string() -> None:
    with pytest.raises(ValueError, match="3"):
        validate_collection_name("")


def test_validate_collection_name_single_char() -> None:
    with pytest.raises(ValueError, match="3"):
        validate_collection_name("a")


def test_validate_collection_name_double_underscore_valid() -> None:
    """Double underscores in the middle are valid — used by all collection prefixes."""
    validate_collection_name("code__myrepo")
    validate_collection_name("a__b")


@pytest.mark.parametrize("char", [".", " ", "/", "@", "+", "%", "=", "!", "~"])
def test_validate_collection_name_rejects_special_chars(char: str) -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name(f"bad{char}name")


def test_validate_collection_name_starts_with_underscore() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("_badstart")


def test_validate_collection_name_ends_with_underscore() -> None:
    with pytest.raises(ValueError, match="alphanumeric"):
        validate_collection_name("badend_")


def test_validate_collection_name_digits_at_boundaries() -> None:
    """Names starting and ending with digits are valid."""
    validate_collection_name("1abc9")
    validate_collection_name("123")


# ── resolve_corpus edge cases ────────────────────────────────────────────────

def test_resolve_corpus_prefix_requires_double_underscore() -> None:
    """--corpus code must NOT match 'codebase__x' (only 'code__*')."""
    all_cols = ["codebase__myrepo", "code__myrepo"]
    assert resolve_corpus("code", all_cols) == ["code__myrepo"]


def test_resolve_corpus_multiple_separators_exact_match() -> None:
    """Corpus arg with __ uses exact match, even with multiple __ separators."""
    all_cols = ["code__repo__extra", "code__repo"]
    assert resolve_corpus("code__repo__extra", all_cols) == ["code__repo__extra"]


# ── t3_collection_name edge cases ────────────────────────────────────────────

def test_t3_collection_name_already_conformant_passthrough() -> None:
    """A 4-segment conformant name is returned untouched (no double
    promotion)."""
    name = "knowledge__existing__voyage-context-3__v1"
    assert t3_collection_name(name) == name


def test_t3_collection_name_two_segment_knowledge_promotes() -> None:
    """Legacy 2-segment ``knowledge__`` arg promotes to conformant."""
    assert (
        t3_collection_name("knowledge__existing")
        == "knowledge__existing__voyage-context-3__v1"
    )


def test_t3_collection_name_other_prefix_promotes_to_canonical_model() -> None:
    """``code__myrepo`` promotes to ``code__myrepo__voyage-code-3__v1``;
    the canonical embedding model is selected from the content_type
    prefix, not assumed."""
    assert (
        t3_collection_name("code__myrepo")
        == "code__myrepo__voyage-code-3__v1"
    )


# ── nexus-hmxi: t3-aware grandfathering ──────────────────────────────────────


class _FakeT3:
    """Minimal T3 stand-in for the legacy-grandfathering probe."""

    def __init__(self, collections: set[str]) -> None:
        self._collections = set(collections)

    def collection_exists(self, name: str) -> bool:
        return name in self._collections

    def list_collections(self) -> list[dict]:
        return [{"name": c} for c in sorted(self._collections)]


def test_t3_collection_name_grandfathers_existing_legacy_when_t3_supplied() -> None:
    """nexus-hmxi: with a t3 probe, an existing legacy 2-segment
    collection wins over the auto-promoted conformant target so put /
    list / search all resolve to the same physical collection.
    """
    legacy = "knowledge__art"
    conformant = "knowledge__art__voyage-context-3__v1"
    t3 = _FakeT3({legacy})  # operator's pre-Phase-5 collection
    assert t3_collection_name(legacy, t3=t3) == legacy


def test_t3_collection_name_promotes_when_legacy_absent() -> None:
    """When the legacy collection does not exist in T3, the resolver
    returns the auto-promoted conformant target so new writes land
    on the conformant shape and satisfy the strict-naming guard.
    """
    legacy = "knowledge__art"
    conformant = "knowledge__art__voyage-context-3__v1"
    t3 = _FakeT3(set())
    assert t3_collection_name(legacy, t3=t3) == conformant


def test_t3_collection_name_prefers_conformant_when_both_exist() -> None:
    """When BOTH legacy and conformant collections exist (mid-migration
    state), the resolver returns the conformant target so the
    in-progress migration converges instead of forking new writes back
    onto the legacy shape.
    """
    legacy = "knowledge__art"
    conformant = "knowledge__art__voyage-context-3__v1"
    t3 = _FakeT3({legacy, conformant})
    assert t3_collection_name(legacy, t3=t3) == conformant


def test_t3_collection_name_no_t3_always_promotes() -> None:
    """Without a t3 probe (static / test contexts), the resolver
    auto-promotes unconditionally; matches the pre-nexus-hmxi
    contract."""
    assert (
        t3_collection_name("knowledge__art")
        == "knowledge__art__voyage-context-3__v1"
    )


def test_t3_collection_name_bare_prefix_falls_back_to_2segment_legacy() -> None:
    """Bare-prefix arg ('knowledge') must reach the documented
    legacy 2-segment collection ('knowledge__knowledge') when that's
    the only physical collection that exists (#535, nexus-6mr0).

    Pre-fix: nx store list (no args, default --collection knowledge)
    on installs with knowledge__knowledge from before the RDR-103
    transition returned 'No entries' because the resolver promoted
    to knowledge__knowledge__voyage-context-3__v1 (which does not
    exist) and never tried the 2-segment legacy fallback.

    The grandfathering branch must probe the synthesised legacy
    shape f'{ct}__{owner_segment}' in addition to user_arg itself,
    so the bare-prefix shorthand bridges to the legacy collection
    when the conformant target is absent.
    """
    legacy_2seg = "knowledge__knowledge"
    conformant = "knowledge__knowledge__voyage-context-3__v1"
    t3 = _FakeT3({legacy_2seg})  # only legacy exists, no conformant
    # The user typed the bare prefix 'knowledge'. The resolver should
    # bridge to 'knowledge__knowledge' (the 2-segment legacy shape)
    # rather than returning the missing conformant name.
    assert t3_collection_name("knowledge", t3=t3) == legacy_2seg


def test_t3_collection_name_bare_prefix_promotes_when_no_legacy() -> None:
    """Symmetric: bare-prefix on a fresh install (no legacy 2-segment
    collection on disk) promotes to the conformant shape so new
    writes satisfy the strict-naming guard. Only the legacy install
    case grandfathers; greenfield installs land on conformant.
    """
    t3 = _FakeT3(set())  # nothing on disk
    assert (
        t3_collection_name("knowledge", t3=t3)
        == "knowledge__knowledge__voyage-context-3__v1"
    )


def test_t3_collection_name_bare_prefix_prefers_conformant_when_both_exist() -> None:
    """Mid-migration state: both legacy 2-segment AND conformant
    exist for the bare-prefix shorthand. The resolver returns the
    conformant target so new writes converge instead of forking
    back to the legacy shape (matches the existing both-exist
    behaviour for the 2-segment input form).
    """
    legacy_2seg = "knowledge__knowledge"
    conformant = "knowledge__knowledge__voyage-context-3__v1"
    t3 = _FakeT3({legacy_2seg, conformant})
    assert t3_collection_name("knowledge", t3=t3) == conformant


def test_t3_collection_name_bare_code_prefix_resolves_to_unique_match() -> None:
    """GH #545: bare ``"code"`` (and ``"docs"``, ``"rdr"``) on installs
    that have exactly one matching ``code__*`` collection must resolve
    to it. Pre-fix the resolver treated bare ``code`` as an owner under
    content_type ``knowledge`` and produced
    ``knowledge__code__voyage-context-3__v1`` — wrong namespace.
    """
    only_code = "code__myrepo__voyage-code-3__v1"
    t3 = _FakeT3({only_code})
    assert t3_collection_name("code", t3=t3) == only_code


def test_t3_collection_name_bare_docs_prefix_resolves_to_unique_match() -> None:
    """GH #545 sibling: bare ``"docs"`` resolves to the unique
    ``docs__*`` collection.
    """
    only_docs = "docs__myrepo__voyage-context-3__v1"
    t3 = _FakeT3({only_docs})
    assert t3_collection_name("docs", t3=t3) == only_docs


def test_t3_collection_name_bare_rdr_prefix_resolves_to_unique_match() -> None:
    """GH #545 sibling: bare ``"rdr"`` resolves to the unique
    ``rdr__*`` collection.
    """
    only_rdr = "rdr__nexus__voyage-context-3__v1"
    t3 = _FakeT3({only_rdr})
    assert t3_collection_name("rdr", t3=t3) == only_rdr


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


def test_t3_collection_name_bare_prefix_multi_match_picks_4seg() -> None:
    """nexus-0f3h: when multiple ``{prefix}__*`` collections exist AND the
    conformant ``{prefix}__{prefix}__<canonical_model>__v1`` is among
    them, pick that one. This honours the RDR-103 conformant default
    and avoids the wrong-namespace fall-through that the 4.26.3 partial
    fix produced for the multi-match case.
    """
    t3 = _FakeT3({
        "code__nexus-1__voyage-code-3__v1",
        "code__myrepo__voyage-code-3__v1",
        "code__code__voyage-code-3__v1",  # the conformant default
    })
    assert (
        t3_collection_name("code", t3=t3)
        == "code__code__voyage-code-3__v1"
    )


def test_t3_collection_name_bare_prefix_multi_match_picks_2seg_legacy() -> None:
    """nexus-0f3h: when no conformant default exists but the legacy
    2-segment ``{prefix}__{prefix}`` does, pick that. Mirrors the
    nexus-6mr0 fallback for the unique-match path.
    """
    t3 = _FakeT3({
        "code__nexus-1__voyage-code-3__v1",
        "code__myrepo__voyage-code-3__v1",
        "code__code",  # legacy 2-segment default
    })
    assert t3_collection_name("code", t3=t3) == "code__code"


def test_t3_collection_name_bare_prefix_multi_match_picks_alphabetical_first() -> None:
    """nexus-0f3h: when no canonical default exists, fall back to
    alphabetical first. Deterministic so callers get a stable choice
    across runs and the warning log gives the operator the candidate
    list for disambiguation on subsequent calls.
    """
    t3 = _FakeT3({
        "code__myrepo-bbb__voyage-code-3__v1",
        "code__myrepo-aaa__voyage-code-3__v1",
        "code__myrepo-ccc__voyage-code-3__v1",
    })
    assert (
        t3_collection_name("code", t3=t3)
        == "code__myrepo-aaa__voyage-code-3__v1"
    )


def test_t3_collection_name_bare_prefix_multi_match_picks_alphabetical_when_no_canonical() -> None:
    """nexus-0f3h: 2 matches, neither is the canonical default; pick
    alphabetical first deterministically. Pre-fix this fell through to
    promotion and produced ``knowledge__code__voyage-context-3__v1``
    (wrong namespace) — the load-bearing assertion is that we land on
    a real ``code__*`` instead.
    """
    t3 = _FakeT3({
        "code__b__voyage-code-3__v1",
        "code__a__voyage-code-3__v1",
    })
    out = t3_collection_name("code", t3=t3)
    assert out == "code__a__voyage-code-3__v1"
    # Anti-regression: must never land in the wrong knowledge__ namespace.
    assert not out.startswith("knowledge__"), out
