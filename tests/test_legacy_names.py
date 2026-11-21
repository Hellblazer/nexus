# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.2 (nexus-p2m1x.7): ``nexus.catalog.rows.legacy_names`` is the one
place a collection name is still parsed.

Every parser that lived in ``nexus.corpus`` and ``nexus.catalog.collection_name``
moves here, with no alias left behind, so a caller that still parses a name has to
import it from here and the census sees it.
"""
from __future__ import annotations

import inspect

import pytest
from structlog.testing import capture_logs

import nexus.catalog.collection_name as collection_name_mod
import nexus.corpus as corpus_mod
from nexus.catalog.rows import legacy_names

pytestmark = pytest.mark.usefixtures("cloud_mode")

_CONFORMANT = "code__1-1__bge-base-en-v15-768__v4"
_MODELS = collection_name_mod.KNOWN_EMBEDDING_MODELS

#: Every name parser that used to be defined in ``nexus.corpus``.
_MOVED_FROM_CORPUS = (
    "is_conformant_collection_name",
    "parse_conformant_collection_name",
    "embedding_model_for_collection_name",
    "model_version_for_collection_name",
    "split_candidate_collection_name",
    "candidate_knowledge_subject",
    "_CANDIDATE_STRING_PRIMITIVES",
    "_CONFORMANT_COLLECTION_RE",
    "_LEGACY_SPLIT_RE",
    "_OWNER_SEGMENT_RE",
)


@pytest.mark.parametrize("name", _MOVED_FROM_CORPUS)
def test_a_moved_parser_is_defined_in_legacy_names_and_left_no_alias_in_corpus(name: str) -> None:
    assert hasattr(legacy_names, name), f"legacy_names does not define {name}"
    assert not hasattr(corpus_mod, name), (
        f"nexus.corpus still exposes {name}: a parser outside legacy_names is a census defect, "
        "and an alias hides its callers from the 'call sites updated' check"
    )


def test_collection_name_parses_through_legacy_names_and_holds_no_regex() -> None:
    """``CollectionName.parse`` is the value object's strict constructor, and the empty-version fallback was
    collection_name's other parse helper: both read the name through ``legacy_names``, neither owns a regex."""
    assert "candidate_attributes(" in inspect.getsource(collection_name_mod.CollectionName.parse)
    assert "version_of_blank_row_label(" in inspect.getsource(collection_name_mod.version_from_for_tuple_row)
    assert "re.compile" not in inspect.getsource(collection_name_mod)


def test_the_content_types_the_regex_uses_are_corpus_content_types() -> None:
    """legacy_names cannot import corpus (corpus imports it), so it holds its own copy; this pins them equal."""
    assert legacy_names.NAME_CONTENT_TYPES == corpus_mod.CONTENT_TYPES


def test_candidate_attributes_of_a_conformant_name() -> None:
    got = legacy_names.candidate_attributes(_CONFORMANT)
    assert got == legacy_names.CandidateAttributes(
        name=_CONFORMANT, content_type="code", owner_id="1-1",
        embedding_model="bge-base-en-v15-768", model_version=4, conformant=True,
    )


def test_candidate_attributes_accepts_an_owner_with_single_underscores() -> None:
    got = legacy_names.candidate_attributes("knowledge__my_subject__voyage-context-3__v1")
    assert got is not None and got.conformant and got.owner_id == "my_subject"


def test_candidate_attributes_of_a_legacy_two_segment_name() -> None:
    """No model token and no version: the name carries only a type and an owner."""
    got = legacy_names.candidate_attributes("docs__nexus-571b8edd")
    assert got == legacy_names.CandidateAttributes(
        name="docs__nexus-571b8edd", content_type="docs", owner_id="nexus-571b8edd",
        embedding_model=None, model_version=None, conformant=False,
    )


def test_candidate_attributes_of_a_bare_name_has_no_content_type() -> None:
    got = legacy_names.candidate_attributes("delos")
    assert got == legacy_names.CandidateAttributes(
        name="delos", content_type="", owner_id="delos",
        embedding_model=None, model_version=None, conformant=False,
    )


def test_candidate_attributes_keeps_a_compound_remainder_whole() -> None:
    got = legacy_names.candidate_attributes("knowledge__a__b__c")
    assert got is not None and not got.conformant
    assert (got.content_type, got.owner_id) == ("knowledge", "a__b__c")


@pytest.mark.parametrize("bad", ["", "docs__", "__", "__weird__"])
def test_candidate_attributes_of_a_malformed_name_is_none(bad: str) -> None:
    """An empty name, or one with no owner, has nothing to derive."""
    assert legacy_names.candidate_attributes(bad) is None


def test_candidate_attributes_of_a_non_string_is_none() -> None:
    assert legacy_names.candidate_attributes(None) is None  # type: ignore[arg-type]


def test_the_primitives_tuple_names_real_functions() -> None:
    assert legacy_names._CANDIDATE_STRING_PRIMITIVES
    for fn in legacy_names._CANDIDATE_STRING_PRIMITIVES:
        assert inspect.isfunction(getattr(legacy_names, fn)), fn


def test_the_parsers_behave_as_they_did_in_corpus() -> None:
    assert legacy_names.is_conformant_collection_name(_CONFORMANT)
    assert not legacy_names.is_conformant_collection_name("docs__default")
    assert legacy_names.parse_conformant_collection_name(_CONFORMANT) == {
        "content_type": "code", "owner_id": "1-1",
        "embedding_model": "bge-base-en-v15-768", "model_version": "v4",
    }
    with pytest.raises(ValueError, match="not conformant"):
        legacy_names.parse_conformant_collection_name("docs__default")
    assert legacy_names.embedding_model_for_collection_name(_CONFORMANT) == "bge-base-en-v15-768"
    assert legacy_names.embedding_model_for_collection_name("docs__x") is None
    assert legacy_names.model_version_for_collection_name(_CONFORMANT) == "v4"
    assert legacy_names.model_version_for_collection_name("docs__x") is None
    assert legacy_names.split_candidate_collection_name("knowledge__a__b") == ("knowledge", "a__b")
    assert legacy_names.split_candidate_collection_name("plain") == ("", "plain")
    assert legacy_names.candidate_knowledge_subject("knowledge__delos__voyage-context-3__v1") == "delos"
    assert legacy_names.candidate_knowledge_subject("docs__1-48") == ""
    assert legacy_names.candidate_knowledge_subject("delos") == "delos"


# -- the empty-version fallback (migration source; removed once D2's cloud run completes) ----


def test_the_empty_version_fallback_reads_a_conformant_names_version_and_warns() -> None:
    with capture_logs() as logs:
        assert legacy_names.version_of_blank_row_label(_CONFORMANT, models=_MODELS) == 4
    warned = [e for e in logs if e["event"] == "collection_for_row_model_version_empty"]
    assert len(warned) == 1 and warned[0]["collection"] == _CONFORMANT
    assert warned[0]["log_level"] == "warning"


def test_the_empty_version_fallback_of_a_label_with_no_version_is_none() -> None:
    """A legacy slug or a non-conformant label carries none: the caller decides what that means."""
    with capture_logs() as logs:
        assert legacy_names.version_of_blank_row_label("notes-from-2024", models=_MODELS) is None
    assert [e for e in logs if e["event"] == "collection_for_row_model_version_unreadable"]


def test_the_empty_version_fallback_refuses_a_v0_name() -> None:
    with pytest.raises(ValueError, match="empty model_version"):
        legacy_names.version_of_blank_row_label("code__1-1__bge-base-en-v15-768__v0", models=_MODELS)


def test_the_empty_version_fallback_refuses_a_non_canonical_model_token_as_parse_did() -> None:
    """``CollectionName.parse`` refused a model token outside the closed set, so a blank-version row under such a
    label had no readable version; the move into legacy_names keeps that (P2.2 changes no behaviour)."""
    label = "code__1-1__stub-code-1024__v3"
    assert legacy_names.is_conformant_collection_name(label)  # the regex alone would accept it
    with capture_logs() as logs:
        assert legacy_names.version_of_blank_row_label(label, models=_MODELS) is None
    assert [e for e in logs if e["event"] == "collection_for_row_model_version_unreadable"]


def test_a_for_tuple_row_with_a_blank_version_under_a_non_canonical_model_has_no_version() -> None:
    row = {
        "name": "code__1-1__stub-code-1024__v3", "content_type": "code", "owner_id": "1-1",
        "embedding_model": "stub-code-1024", "model_version": "", "lifecycle_state": "live",
    }
    got = collection_name_mod.version_from_for_tuple_row(
        row, content_type="code", owner_id="1-1", embedding_model="stub-code-1024",
    )
    assert got is None


# -- P2.9's sites read through legacy_names ---------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "taxonomy__x", "taxonomy__", "taxonomy__a__b", "taxonomy", "taxonomy_x", "Taxonomy__x", "docs__taxonomy__x",
    "", "__taxonomy__", "code__1-1__bge-base-en-v15-768__v1", "quarantine-taxonomy__x",
])
def test_the_taxonomy_name_test_is_exactly_the_old_prefix_test(name: str) -> None:
    assert legacy_names.is_taxonomy_collection_name(name) is name.startswith("taxonomy__")


@pytest.mark.parametrize("name,expected", [
    ("docs__1-1__bge-base-en-v15-768__v1", "docs"), ("code__legacy-abcd1234", "code"),
    ("knowledge__delos", "knowledge"), ("c1234", None), ("", None), ("docs__", "docs"), ("__x", None),
])
def test_the_no_row_content_type_is_the_names_first_segment(name: str, expected: str | None) -> None:
    """corpus.candidate_content_type, the single NoRow parse every write router calls (RDR-229 P2.9), reads the
    name through legacy_names and answers what it always did (a ``docs__`` stub still routes as ``docs``)."""
    assert corpus_mod.candidate_content_type(name) == expected
