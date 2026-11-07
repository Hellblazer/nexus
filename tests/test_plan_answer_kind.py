# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 1 (nexus-jypmj.2.1): the plan answer-kind declaration.

A plan declares the kind of answer it can serve in a validated, versioned top-level ``plan_json`` field,
never in ``dimensions`` (those are plan identity). Nothing here filters plans by kind: that is
nexus-jypmj.2.3. These tests pin the vocabulary, the 13 builtin declarations, the tolerant decoder,
the grown-plan provenance rule, and that the declaration reaches the session cache without moving a
single dimension or description.

All tenant-free: the plan library is stubbed and the cache runs on the in-memory vector client.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from nexus.plans.answer_kind import (
    ANSWER_KIND_KEY,
    ANSWER_KIND_VERSION,
    GOLDEN_LABEL_TO_KIND,
    ROUTE_SOURCES,
    AnswerKindError,
    PlanAnswerKind,
    decode_answer_kind,
    decode_declaration,
    declaration_payload,
    PROVENANCES,
    golden_label_kind,
    grown_plan_json,
    saved_plan_json,
    strip_declaration,
    terminal_is_retrieval_only,
    validate_declaration,
)
from nexus.plans.schema import (
    PlanTemplateSchemaError,
    canonical_dimensions_json,
    validate_plan_steps,
    validate_plan_template,
)
from nexus.plans.seed_loader import desired_row_for_template, load_seed_directory
from tests.conftest import make_vector_test_client

_BUILTIN_DIR = Path(__file__).parent.parent / "conexus" / "plans" / "builtin"

#: RDR-232 section 1, "Builtin template file -> routing capability", keyed by template file.
_TABLE: dict[str, PlanAnswerKind] = {
    "abstract-themes.yml": PlanAnswerKind.SYNTHESIS,
    "analyze-default.yml": PlanAnswerKind.SYNTHESIS,
    "citation-traversal.yml": PlanAnswerKind.SYNTHESIS,
    "research-default.yml": PlanAnswerKind.SYNTHESIS,
    "corpus-coverage-check.yml": PlanAnswerKind.DISCOVERY_LISTING,
    "document-discovery.yml": PlanAnswerKind.DISCOVERY_LISTING,
    "find-by-author.yml": PlanAnswerKind.DISCOVERY_LISTING,
    "type-scoped-search.yml": PlanAnswerKind.DISCOVERY_LISTING,
    "entity-resolution.yml": PlanAnswerKind.DISCOVERY_LISTING,
    "debug-default.yml": PlanAnswerKind.DEBUG,
    "document-default.yml": PlanAnswerKind.AUDIT,
    "hybrid-factual-lookup.yml": PlanAnswerKind.LOOKUP,
    "review-default.yml": PlanAnswerKind.COMPARISON,
}

#: Builtins added after the declaration shipped (RDR-147 Step 4 branch (c), nexus-irndi). They have no pre-declaration
#: row: a library seeded by the previous release holds the other twelve, and a reseed inserts these.
_ADDED_AFTER_DECLARATION: frozenset[str] = frozenset({"entity-resolution.yml"})

#: What the 12 pre-existing builtins were BEFORE this bead, captured from the tree at origin/develop 4898d437d (the
#: entity-resolution row is a freeze pin of its own YAML, see its comment):
#: (canonical dimensions, sha256[:12] of the description, name, verb, scope, sha256[:12] of the
#: canonical JSON of the YAML plan_json body). The declaration may add a key beside ``steps``; it must
#: move none of these.
_BEFORE: dict[str, tuple[str, str, str, str, str, str]] = {
    "abstract-themes.yml": ('{"scope":"global","strategy":"abstract-themes","verb":"query"}', "2c8b32fe2954", "abstract-themes", "query", "global", "197d6e7f28a7"),
    "analyze-default.yml": ('{"scope":"global","strategy":"default","verb":"analyze"}', "92d76c1c6890", "default", "analyze", "global", "a62aa486d019"),
    "citation-traversal.yml": ('{"scope":"global","strategy":"citation-traversal","verb":"research"}', "c9e0e3702824", "citation-traversal", "research", "global", "a04f9e1f1caf"),
    "corpus-coverage-check.yml": ('{"scope":"global","strategy":"corpus-coverage-check","verb":"research"}', "074cf6f1330f", "corpus-coverage-check", "research", "global", "84ea44981806"),
    "debug-default.yml": ('{"scope":"global","strategy":"default","verb":"debug"}', "dbd0623d59ea", "default", "debug", "global", "86c8c533da69"),
    "document-default.yml": ('{"scope":"global","strategy":"default","verb":"document"}', "afbb8e70a34e", "default", "document", "global", "3f302c09fa3c"),
    "document-discovery.yml": ('{"scope":"global","strategy":"document-discovery","verb":"research"}', "546d2e15c18a", "document-discovery", "research", "global", "84ea44981806"),
    "find-by-author.yml": ('{"scope":"global","strategy":"find-by-author","verb":"research"}', "5be70bc2a349", "find-by-author", "research", "global", "2d5217c1ce02"),
    "hybrid-factual-lookup.yml": ('{"scope":"global","strategy":"hybrid-factual-lookup","verb":"lookup"}', "1e3502b55575", "hybrid-factual-lookup", "lookup", "global", "57af6c5e0bd4"),
    "research-default.yml": ('{"scope":"global","strategy":"default","verb":"research"}', "8d17c00f852f", "default", "research", "global", "d7bff227acdd"),
    "review-default.yml": ('{"scope":"global","strategy":"default","verb":"review"}', "62f68754c0cd", "default", "review", "global", "21c8428f4736"),
    "type-scoped-search.yml": ('{"scope":"global","strategy":"type-scoped","verb":"research"}', "7754909a23d4", "type-scoped-search", "research", "global", "77a84d788747"),
    # FREEZE PIN, not a develop capture (the file has no row on develop 4898d437d): the entity-resolution YAML
    # as of fix round 1 of the nexus-irndi review, whose steps are hybrid-factual-lookup's. A change to its
    # description, dimensions, tags or steps moves these hashes on purpose and the diff shows the edit.
    "entity-resolution.yml": ('{"scope":"global","strategy":"entity-resolution","verb":"research"}', "ca44c80f5808", "entity-resolution", "research", "global", "57af6c5e0bd4"),
}


#: Per pre-existing builtin, captured from the tree at origin/develop 4898d437d (the entity-resolution row is a
#: freeze pin of its own YAML; the YAML blobs there run through the
#: row builder): (sha256[:12] of the tags string, sha256[:12] of the default_bindings JSON or None when the
#: template has none, parent_dims). Literals, so a change to the YAML tags or bindings cannot hide behind a
#: comparison of the template with itself.
_BEFORE_ROW: dict[str, tuple[str, str | None, str | None]] = {
    "abstract-themes.yml": ('672aaa7f76d1', '8449582a6db2', None),
    "analyze-default.yml": ('40977f44f3c4', '179e669c10c9', None),
    "citation-traversal.yml": ('4f0ba286d45b', 'b1d8cdcd7a35', None),
    "corpus-coverage-check.yml": ('9e573813441e', 'f4c503fae815', None),
    "debug-default.yml": ('5441d9457bc5', None, None),
    "document-default.yml": ('cf5058035213', '7d0587b5acc4', None),
    "document-discovery.yml": ('9e573813441e', 'f4c503fae815', None),
    "find-by-author.yml": ('77b33f7be6df', '7d0587b5acc4', None),
    "hybrid-factual-lookup.yml": ('19689799e542', 'f71db903c830', None),
    "research-default.yml": ('7a87d6cc0664', '7d0587b5acc4', None),
    "review-default.yml": ('2534dd636291', 'a4f389eb2c29', None),
    "type-scoped-search.yml": ('f20f6848e2a3', '7d0587b5acc4', None),
    # FREEZE PIN, as in _BEFORE: tags now carry `cosine-only`; the bindings are hybrid-factual-lookup's.
    "entity-resolution.yml": ('dcbdb52917d7', 'f71db903c830', None),
}


def _template(name: str) -> dict[str, Any]:
    return yaml.safe_load((_BUILTIN_DIR / name).read_text(encoding="utf-8"))


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def test_the_table_covers_every_builtin_file() -> None:
    """Non-vacuity: the parametrized tests below walk a table that is the whole shipped set."""
    on_disk = sorted(p.name for p in _BUILTIN_DIR.glob("*.yml"))
    assert on_disk == sorted(_TABLE) == sorted(_BEFORE) == sorted(_BEFORE_ROW)
    assert len(on_disk) == 13


class TestVocabulary:
    def test_the_closed_vocabulary_is_the_rdrs_seven(self) -> None:
        assert {k.value for k in PlanAnswerKind} == {
            "lookup", "synthesis", "discovery/listing", "comparison", "audit", "debug", "unknown",
        }

    def test_it_is_not_the_terminal_string_classifier(self) -> None:
        from nexus.plans.answer_shape import AnswerShape

        assert PlanAnswerKind is not AnswerShape
        # the two share no member name, so one cannot be passed for the other by accident
        assert not ({k.name for k in PlanAnswerKind} & {s.name for s in AnswerShape})

    def test_the_declaration_is_versioned(self) -> None:
        assert ANSWER_KIND_VERSION == 1
        payload = declaration_payload(PlanAnswerKind.LOOKUP, "curated")
        assert payload == {"version": 1, "kind": "lookup", "provenance": "curated"}


class TestBuiltinDeclarations:
    @pytest.mark.parametrize("name", sorted(_TABLE))
    def test_the_builtin_decodes_to_its_table_kind(self, name: str) -> None:
        row = desired_row_for_template(_template(name))
        assert decode_answer_kind(row.plan_json) is _TABLE[name], name
        decl = decode_declaration(row.plan_json)
        assert (decl.kind, decl.provenance, decl.route, decl.version) == (_TABLE[name], "curated", None, 1)

    @pytest.mark.parametrize("name", sorted(_TABLE))
    def test_the_stored_field_is_top_level_in_plan_json_and_not_in_dimensions(self, name: str) -> None:
        template = _template(name)
        row = desired_row_for_template(template)
        stored = json.loads(row.plan_json)
        assert stored[ANSWER_KIND_KEY] == {"version": 1, "kind": _TABLE[name].value, "provenance": "curated"}
        assert "steps" in stored
        assert not [k for k in template["dimensions"] if "kind" in k.lower()], "dimensions carry no kind"

    @pytest.mark.parametrize("name", sorted(_TABLE))
    def test_dimensions_and_plan_identity_are_byte_identical_to_before(self, name: str) -> None:
        dims, desc_h, plan_name, verb, scope, body_h = _BEFORE[name]
        template = _template(name)
        row = desired_row_for_template(template)
        assert canonical_dimensions_json(template["dimensions"]) == dims
        assert (_h(row.query), row.name, row.verb, row.scope) == (desc_h, plan_name, verb, scope)
        tags_h, bindings_h, parent_dims = _BEFORE_ROW[name]
        assert _h(row.tags) == tags_h, "tags are byte-identical to the captured literal (develop 4898d437d; entity-resolution: its freeze pin)"
        assert (_h(row.default_bindings) if row.default_bindings is not None else None) == bindings_h
        assert row.parent_dims == parent_dims
        stored = json.loads(row.plan_json)
        stored.pop(ANSWER_KIND_KEY)
        body = {k: v for k, v in stored.items() if k not in ("required_bindings", "optional_bindings")}
        assert _h(json.dumps(body, sort_keys=True)) == body_h, "the steps are untouched"

    def test_a_template_with_no_declaration_keeps_the_exact_row_it_had(self) -> None:
        template = _template("hybrid-factual-lookup.yml")
        template.pop("answer_kind", None)
        stored = json.loads(desired_row_for_template(template).plan_json)
        assert ANSWER_KIND_KEY not in stored
        assert decode_answer_kind(json.dumps(stored)) is PlanAnswerKind.UNKNOWN


class TestDecodeIsTolerant:
    """Missing or invalid legacy declarations decode to unknown; no migration guesses from plan text."""

    @pytest.mark.parametrize(
        "plan_json",
        [
            None,
            "",
            "not json",
            "[]",
            "{}",
            json.dumps({"steps": [{"tool": "search", "args": {"query": "list every document"}}]}),
            json.dumps({"answer_kind": None}),
            json.dumps({"answer_kind": "lookup"}),
            json.dumps({"answer_kind": ["lookup"]}),
            json.dumps({"answer_kind": {}}),
            json.dumps({"answer_kind": {"version": 1, "kind": "bogus", "provenance": "curated"}}),
            json.dumps({"answer_kind": {"version": 1, "kind": 7, "provenance": "curated"}}),
            json.dumps({"answer_kind": {"version": 2, "kind": "lookup", "provenance": "curated"}}),
            json.dumps({"answer_kind": {"version": "1", "kind": "lookup", "provenance": "curated"}}),
            json.dumps({"answer_kind": {"version": True, "kind": "lookup", "provenance": "curated"}}),
            json.dumps({"answer_kind": {"kind": "lookup", "provenance": "curated"}}),
            json.dumps({"answer_kind": {"version": 1, "provenance": "curated"}}),
            json.dumps({"answer_kind": {"version": 1, "kind": "lookup"}}),
            json.dumps({"answer_kind": {"version": 1, "kind": "lookup", "provenance": "invented"}}),
            json.dumps({"answer_kind": {"version": 1, "kind": "Lookup", "provenance": "curated"}}),
            "[" * 200_000 + "]" * 200_000,
        ],
        ids=lambda v: str(v)[:40],
    )
    def test_absent_or_invalid_decodes_to_unknown(self, plan_json: Any) -> None:
        assert decode_answer_kind(plan_json) is PlanAnswerKind.UNKNOWN
        decl = decode_declaration(plan_json)
        assert decl.kind is PlanAnswerKind.UNKNOWN and decl.provenance is None

    def test_a_dict_plan_json_decodes_too(self) -> None:
        plan = {"steps": [], "answer_kind": declaration_payload(PlanAnswerKind.DEBUG, "curated")}
        assert decode_answer_kind(plan) is PlanAnswerKind.DEBUG

    def test_a_grown_kind_without_route_provenance_decodes_to_unknown(self) -> None:
        raw = {"version": 1, "kind": "synthesis", "provenance": "grown"}
        assert decode_answer_kind({"answer_kind": raw}) is PlanAnswerKind.UNKNOWN
        raw["route"] = "explicit"
        assert decode_answer_kind({"answer_kind": raw}) is PlanAnswerKind.SYNTHESIS

    def test_a_grown_unknown_decodes_with_its_grown_provenance(self) -> None:
        decl = decode_declaration({"answer_kind": {"version": 1, "kind": "unknown", "provenance": "grown"}})
        assert (decl.kind, decl.provenance) == (PlanAnswerKind.UNKNOWN, "grown")

    def test_the_decoder_never_reads_the_plan_text(self) -> None:
        listing = json.dumps({"steps": [{"tool": "search", "args": {"query": "list all documents"}}]})
        assert decode_answer_kind(listing) is PlanAnswerKind.UNKNOWN


class TestValidation:
    def test_validate_declaration_accepts_the_curated_and_grown_shapes(self) -> None:
        assert validate_declaration(declaration_payload(PlanAnswerKind.AUDIT, "curated")).kind is PlanAnswerKind.AUDIT
        grown = declaration_payload(PlanAnswerKind.LOOKUP, "grown", "heuristic")
        assert validate_declaration(grown).route == "heuristic"

    @pytest.mark.parametrize(
        "raw",
        [
            "lookup",
            {"version": 1, "kind": "nope", "provenance": "curated"},
            {"version": 9, "kind": "lookup", "provenance": "curated"},
            {"version": 1, "kind": "lookup", "provenance": "curated", "route": "explicit"},
            {"version": 1, "kind": "lookup", "provenance": "grown"},
            {"version": 1, "kind": "lookup", "provenance": "grown", "route": "guessed"},
            {"version": 1, "kind": "lookup", "provenance": "curated", "extra": 1},
            {"version": 1, "kind": "lookup", "provenance": "saved", "route": "explicit"},
            {"version": 1, "kind": "unknown", "provenance": "saved", "route": "explicit"},
            {"version": 1, "kind": "lookup", "provenance": "saved-ish"},
        ],
    )
    def test_validate_declaration_refuses_a_bad_one_by_name(self, raw: Any) -> None:
        with pytest.raises(AnswerKindError):
            validate_declaration(raw)

    def test_the_provenance_vocabulary_is_curated_grown_and_saved(self) -> None:
        assert PROVENANCES == {"curated", "grown", "saved"}

    @pytest.mark.parametrize("kind", list(PlanAnswerKind), ids=lambda k: k.value)
    def test_a_saved_declaration_takes_any_kind_unknown_included_and_no_route(self, kind: PlanAnswerKind) -> None:
        payload = declaration_payload(kind, "saved")
        assert payload == {"version": 1, "kind": kind.value, "provenance": "saved"}
        decl = validate_declaration(payload)
        assert (decl.kind, decl.provenance, decl.route) == (kind, "saved", None)
        assert decode_declaration(json.dumps({ANSWER_KIND_KEY: payload})).provenance == "saved"

    def test_a_route_source_is_one_of_the_rdrs_three_words(self) -> None:
        assert ROUTE_SOURCES == {"explicit", "heuristic", "model-assisted"}
        assert validate_declaration(
            declaration_payload(PlanAnswerKind.LOOKUP, "grown", "model-assisted")).route == "model-assisted"
        with pytest.raises(AnswerKindError):
            validate_declaration(declaration_payload(PlanAnswerKind.LOOKUP, "grown", "model"))


#: Stored shapes a reader must tolerate: a future version, a malformed object, and an unrelated string
#: that happens to sit under the same key.
_ODD_DECLARATIONS: list[Any] = [
    {"version": 2, "kind": "lookup", "provenance": "curated"},
    {"version": 1, "kind": "essay", "provenance": "curated"},
    "essay",
    "lookup",
    7,
    ["lookup"],
]


class TestReadPathsNeverRetireAPlanForItsDeclaration:
    """The spec decodes a bad or future-version declaration to unknown; a read path must not disable the row."""

    @pytest.mark.parametrize("declared", _ODD_DECLARATIONS, ids=lambda v: json.dumps(v)[:40])
    def test_validate_plan_steps_ignores_the_declaration(self, declared: Any) -> None:
        validate_plan_steps(
            {"steps": [{"tool": "search", "args": {"query": "$intent"}}], ANSWER_KIND_KEY: declared},
            require_steps=True,
        )

    def test_hygiene_leaves_a_v2_a_malformed_and_an_unrelated_string_declaration_alone(self) -> None:
        from nexus.commands.plan import _hygiene_apply, _hygiene_scan

        steps = [{"tool": "search", "args": {"query": "$intent"}}]
        rows = [
            {"id": i, "query": f"row {i}", "verb": "research",
             "plan_json": json.dumps({"steps": steps, ANSWER_KIND_KEY: declared}),
             "success_count": 1, "failure_count": 0, "tags": "", "project": ""}
            for i, declared in enumerate(_ODD_DECLARATIONS, start=1)
        ]
        # non-vacuity: the same library does flag a row that really is broken
        rows.append({"id": 99, "query": "dump", "verb": "research", "plan_json": json.dumps({"phases": ["a"]}),
                     "success_count": 0, "failure_count": 0, "tags": "", "project": ""})

        class _Lib:
            def __init__(self) -> None:
                self.disabled: list[int] = []

            def list_plans(self, limit=20, project="", *, include_disabled=False):  # noqa: ARG002
                return list(rows)

            def set_plan_disabled(self, plan_id, *, reason=""):  # noqa: ARG002
                self.disabled.append(plan_id)
                return True

        lib = _Lib()
        findings = _hygiene_scan(lib)
        assert [f["id"] for f in findings] == [99], findings
        _hygiene_apply(lib, findings)
        assert lib.disabled == [99], "only the genuinely non-executable row is retired"


def _plan_save(
    plan: dict[str, Any], tags: str = "", answer_kind: str = "discovery/listing", project: str = "nexus",
    session: tuple[str, tuple[str, ...]] = ("nexus", ()),
) -> tuple[str, list[dict[str, Any]]]:
    """Run the plan_save MCP tool against a recording library; returns (reply, save_plan kwargs per call)."""
    from unittest.mock import MagicMock

    from nexus.mcp.core import plan_save

    saved: list[dict[str, Any]] = []
    db = MagicMock()

    def _save(**kw: Any) -> int:
        saved.append(kw)
        return 7

    db.plans.save_plan.side_effect = _save

    def fake_write(fn, **_kw):  # noqa: ARG001
        return fn(db)

    with (
        patch("nexus.mcp.core._t2_index_write", fake_write),
        patch("nexus.mcp.core._record_tier_write"),
        patch("nexus.mcp.core._nx_answer_session_project", return_value=session[0]),
        patch("nexus.mcp.core._nx_answer_session_project_aliases", return_value=session[1]),
    ):
        return plan_save(
            query="q", plan_json=json.dumps(plan), verb="research", tags=tags, answer_kind=answer_kind,
            project=project,
        ), saved


#: A plan that ends in an operator, so it may declare any kind; and one that ends in retrieval.
_ANSWERING = [{"tool": "search", "args": {"query": "$intent"}}, {"tool": "summarize", "args": {"inputs": "$step1.ids"}}]
_RETRIEVING = [{"tool": "memory_get", "args": {}}, {"tool": "search", "args": {"query": "$intent"}}]


class TestPlanSaveWritesASavedDeclaration:
    """nexus-0vh80 (Sam, 2026-11-03): a user-saved plan declares its kind, with ``saved`` provenance."""

    @pytest.mark.parametrize("kind", [k.value for k in PlanAnswerKind], ids=str)
    def test_the_declared_kind_is_stored_as_a_saved_declaration(self, kind: str) -> None:
        # a listing may end in retrieval; every other known kind needs an operator at the end
        out, saved = _plan_save({"steps": _ANSWERING}, answer_kind=kind)
        assert out.startswith("Saved plan: [7]"), out
        stored = json.loads(saved[0]["plan_json"])
        assert stored[ANSWER_KIND_KEY] == {"version": 1, "kind": kind, "provenance": "saved"}
        decl = decode_declaration(saved[0]["plan_json"])
        assert (decl.kind.value, decl.provenance, decl.route) == (kind, "saved", None)
        assert stored["steps"] == _ANSWERING, "the steps are stored as given"

    def test_unknown_is_allowed_and_stored_as_a_saved_unknown(self) -> None:
        out, saved = _plan_save({"steps": _RETRIEVING}, answer_kind="unknown")
        assert out.startswith("Saved plan: [7]"), out
        assert decode_declaration(saved[0]["plan_json"]) == decode_declaration(
            json.dumps({ANSWER_KIND_KEY: declaration_payload(PlanAnswerKind.UNKNOWN, "saved")}))

    @pytest.mark.parametrize("given", ["Lookup", " lookup ", "discovery", "essay", "saved", "1"])
    def test_a_kind_outside_the_vocabulary_is_refused_and_nothing_is_saved(self, given: str) -> None:
        out, saved = _plan_save({"steps": _ANSWERING}, answer_kind=given)
        assert out.startswith("Plan not saved") and "answer_kind" in out and "discovery/listing" in out
        assert saved == []

    def test_a_save_with_no_kind_is_refused_like_one_with_no_verb(self) -> None:
        from nexus.mcp.core import plan_save

        out = plan_save(query="q", plan_json=json.dumps({"steps": _ANSWERING}), verb="research", project="nexus")
        assert out.startswith("Plan not saved") and "answer_kind" in out and "required" in out

    @pytest.mark.parametrize("kind", ["lookup", "synthesis", "comparison", "audit", "debug"])
    def test_the_shape_veto_refuses_a_plan_that_ends_in_retrieval_under_an_answering_kind(self, kind: str) -> None:
        out, saved = _plan_save({"steps": _RETRIEVING}, answer_kind=kind)
        assert out.startswith("Plan not saved") and kind in out and "search" in out, out
        assert saved == []

    def test_the_veto_would_have_refused_plan_598_as_a_lookup_and_still_allows_a_listing(self) -> None:
        steps = [{"tool": "memory_get", "args": {}}, {"tool": "memory_get", "args": {}}, {"tool": "search", "args": {}}]
        out, saved = _plan_save({"steps": steps}, answer_kind="lookup")
        assert saved == [] and "Plan not saved" in out
        out, saved = _plan_save({"steps": steps}, answer_kind="discovery/listing")
        assert out.startswith("Saved plan: [7]") and len(saved) == 1

    def test_a_forged_embedded_declaration_is_replaced_by_the_declared_one(self) -> None:
        forged = declaration_payload(PlanAnswerKind.SYNTHESIS, "curated")
        out, saved = _plan_save({"steps": _ANSWERING, ANSWER_KIND_KEY: forged}, answer_kind="lookup")
        decl = decode_declaration(saved[0]["plan_json"])
        assert (decl.kind, decl.provenance) == (PlanAnswerKind.LOOKUP, "saved")
        assert "embedded answer_kind field replaced" in out

    def test_a_forged_grown_declaration_cannot_reach_the_row_either(self) -> None:
        forged = declaration_payload(PlanAnswerKind.LOOKUP, "grown", "explicit")
        _, saved = _plan_save({"steps": _ANSWERING, ANSWER_KIND_KEY: forged}, answer_kind="unknown")
        decl = decode_declaration(saved[0]["plan_json"])
        assert (decl.kind, decl.provenance, decl.route) == (PlanAnswerKind.UNKNOWN, "saved", None)

    def test_the_reply_stays_plain_when_nothing_was_replaced(self) -> None:
        out, _ = _plan_save({"steps": _ANSWERING}, answer_kind="lookup")
        assert out == "Saved plan: [7] q"

    def test_a_save_with_no_project_is_stored_and_the_reply_says_it_serves_nothing(self) -> None:
        out, saved = _plan_save({"steps": _ANSWERING}, answer_kind="lookup", project="")
        assert out.startswith("Saved plan: [7]") and "no project" in out and "serves no request" in out
        assert saved[0]["project"] == ""
        quiet, _ = _plan_save({"steps": _ANSWERING}, answer_kind="unknown", project="")
        assert "no project" not in quiet, "an unknown-kind plan has its own note"

    def test_an_unknown_kind_save_says_it_serves_nothing(self) -> None:
        out, saved = _plan_save({"steps": _ANSWERING}, answer_kind="unknown")
        assert out.startswith("Saved plan: [7]") and "answer_kind unknown" in out and "serves no request" in out
        assert len(saved) == 1

    def test_a_project_that_is_not_the_sessions_is_named_in_the_reply(self) -> None:
        out, saved = _plan_save({"steps": _ANSWERING}, answer_kind="lookup", project="ART")
        assert out.startswith("Saved plan: [7]") and "'ART' is not this session's project, nexus" in out
        assert saved[0]["project"] == "ART", "stored as given: the warning does not change the save"

    def test_a_save_under_the_repos_name_from_a_worktree_session_is_not_warned(self) -> None:
        out, _ = _plan_save({"steps": _ANSWERING}, answer_kind="lookup", project="nexus", session=("wt-feature", ("nexus",)))
        assert out == "Saved plan: [7] q"
        out, _ = _plan_save({"steps": _ANSWERING}, answer_kind="lookup", project="wt-feature", session=("wt-feature", ("nexus",)))
        assert out == "Saved plan: [7] q", "the session's own directory name matches too"
        out, _ = _plan_save({"steps": _ANSWERING}, answer_kind="lookup", project="ART", session=("wt-feature", ("nexus",)))
        assert "not this session's project, nexus, wt-feature" in out

    def test_the_project_is_compared_stripped(self) -> None:
        out, _ = _plan_save({"steps": _ANSWERING}, answer_kind="lookup", project=" nexus ")
        assert out == "Saved plan: [7] q"

    @pytest.mark.parametrize("kind", ["lookup", "synthesis", "discovery/listing", "comparison", "audit", "debug"])
    def test_the_stored_row_reads_saved_and_serves_its_kind_in_its_project_only(self, kind: str) -> None:
        from nexus.plans import eligibility as el

        _, saved = _plan_save({"steps": _ANSWERING}, answer_kind=kind, project="nexus")
        standing = el.classify_plan(saved[0]["tags"], saved[0]["plan_json"])
        assert (standing.provenance, standing.kind.value, standing.flag) == ("saved", kind, None)
        for request_kind in PlanAnswerKind:
            for request_project, here in (("nexus", True), ("ART", False)):
                verdict = el._judge(
                    request_kind, saved[0]["tags"], saved[0]["plan_json"], None, 0,
                    plan_project=saved[0]["project"], request_project=request_project,
                )[1]
                assert verdict.eligible is (here and request_kind.value == kind), (kind, request_kind, request_project)

    def test_the_tool_documents_the_argument_and_its_refusals(self) -> None:
        from nexus.mcp.core import plan_save

        assert "answer_kind" in plan_save.__doc__ and "saved" in plan_save.__doc__
        assert "ends in retrieval" in plan_save.__doc__ or "retrieval-only" in plan_save.__doc__
        assert "reads the last step only" in plan_save.__doc__ or "last step is retrieval-only" in plan_save.__doc__


@pytest.mark.t2_tenant
def test_a_real_saved_row_read_back_through_the_library_carries_its_project_and_the_filter_uses_it() -> None:
    """Engine-backed: the project survives the write and the read, so the filter's project check has something to
    compare (a fake row that carries ``project`` would pass with a library that dropped it)."""
    import re

    from nexus.mcp.core import plan_save
    from nexus.mcp_infra import t2_ctx
    from nexus.plans.eligibility import EligibilityFilter
    from nexus.plans.match import Match

    with patch("nexus.mcp.core._nx_answer_session_project", return_value="savedproj"), \
         patch("nexus.mcp.core._nx_answer_session_project_aliases", return_value=()):
        out = plan_save(
            query="where did the saved-provenance round-trip land", plan_json=json.dumps({"steps": _ANSWERING}),
            verb="query", answer_kind="lookup", project="savedproj", tags="roundtrip",
        )
    assert out.startswith("Saved plan: ["), out
    plan_id = int(re.match(r"Saved plan: \[(\d+)\]", out).group(1))
    with t2_ctx() as db:
        row = db.plans.get_plan(plan_id)
    assert row is not None and row["project"] == "savedproj"
    assert Match.from_plan_row(row).project == "savedproj"
    decl = decode_declaration(row["plan_json"])
    assert (decl.kind, decl.provenance) == (PlanAnswerKind.LOOKUP, "saved")
    here = EligibilityFilter(PlanAnswerKind.LOOKUP, enforce=True, project="savedproj")
    assert here.admit(row, "cosine") is True
    worktree = EligibilityFilter(PlanAnswerKind.LOOKUP, enforce=True, project="wt-x", project_aliases=("savedproj",))
    assert worktree.admit(row, "cosine") is True
    elsewhere = EligibilityFilter(PlanAnswerKind.LOOKUP, enforce=True, project="otherproj")
    assert elsewhere.admit(row, "cosine") is False
    assert elsewhere.to_wire()["branches"]["cosine"]["reasons"] == {"saved_other_project": 1}


class TestSavedPlanJson:
    def test_it_stamps_a_saved_declaration_and_replaces_whatever_the_caller_embedded(self) -> None:
        plan = {"steps": _ANSWERING, ANSWER_KIND_KEY: declaration_payload(PlanAnswerKind.SYNTHESIS, "curated")}
        stored = json.loads(saved_plan_json(json.dumps(plan), PlanAnswerKind.LOOKUP))
        assert stored[ANSWER_KIND_KEY] == {"version": 1, "kind": "lookup", "provenance": "saved"}
        assert stored["steps"] == _ANSWERING

    def test_a_plan_json_that_is_not_an_object_comes_back_unchanged(self) -> None:
        assert saved_plan_json("[1]", PlanAnswerKind.LOOKUP) == "[1]"
        assert saved_plan_json("nope", PlanAnswerKind.LOOKUP) == "nope"

    def test_strip_declaration_reports_whether_it_removed_anything(self) -> None:
        assert strip_declaration({"steps": [], ANSWER_KIND_KEY: 1}) == ({"steps": []}, True)
        plan = {"steps": []}
        assert strip_declaration(plan) == (plan, False)


class TestTheShapeVeto:
    @pytest.mark.parametrize(
        ("steps", "expected"),
        [
            pytest.param(_ANSWERING, False, id="ends-in-an-operator"),
            pytest.param(_RETRIEVING, True, id="ends-in-search"),
            pytest.param([{"tool": "extract"}, {"tool": "rank"}], False, id="every-operator-counts"),
            pytest.param([{"tool": "operator_generate"}], False, id="resolved-operator-name"),
            pytest.param([{"tool": "mcp__plugin_conexus_nexus__operator_summarize"}], False, id="mcp-prefixed-operator"),
            pytest.param([{"tool": "mcp__plugin_conexus_nexus__summarize"}], False, id="mcp-prefixed-plan-name"),
            pytest.param([{"tool": "mcp__plugin_conexus_nexus__search"}], True, id="mcp-prefixed-retrieval-still-vetoed"),
            # Sam, nexus-ynkw1: a check or verify verdict is an answer, so a plan ending in one is not vetoed.
            pytest.param([{"tool": "search"}, {"tool": "check"}], False, id="check-answers"),
            pytest.param([{"tool": "search"}, {"tool": "verify"}], False, id="verify-answers"),
            pytest.param([{"tool": "mcp__plugin_conexus_nexus__operator_verify"}], False, id="mcp-prefixed-verify-answers"),
            pytest.param([{"tool": "summarize"}, {"tool": "traverse"}], True, id="only-the-last-step-counts"),
            pytest.param([], True, id="no-steps"),
            pytest.param(["x"], True, id="not-a-step"),
            pytest.param([{"args": {}}], True, id="no-tool"),
        ],
    )
    def test_the_terminal_step_decides(self, steps: list, expected: bool) -> None:
        assert terminal_is_retrieval_only(json.dumps({"steps": steps})) is expected

    @pytest.mark.parametrize("plan_json", [None, "", "not json", "[]", "{}", json.dumps({"steps": "x"})])
    def test_an_unreadable_plan_is_retrieval_only_never_raises(self, plan_json: Any) -> None:
        assert terminal_is_retrieval_only(plan_json) is True


class TestPlanSaveNeverStoresProvenanceTags:
    """nexus-3ihve: ``grown`` and ``builtin-template`` are provenance a writer earns (the grow site, the seed
    loader), never one a caller of ``plan_save`` can claim. Other tags pass through untouched."""

    _STEPS = [{"tool": "search", "args": {"query": "$intent"}}]

    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            pytest.param("grown", "", id="grown"),
            pytest.param("builtin-template", "", id="builtin-template"),
            pytest.param("grown,builtin-template", "", id="both"),
            pytest.param("rdr-097, grown ,research", "rdr-097,research", id="spaced-among-others"),
            pytest.param("ad-hoc,grown", "ad-hoc", id="adhoc-kept-grown-dropped"),
            pytest.param("research,notes", "research,notes", id="no-reserved-tag-is-stored-as-given"),
            pytest.param("", "", id="empty"),
        ],
    )
    def test_reserved_tags_are_not_stored(self, given: str, stored: str) -> None:
        out, saved = _plan_save({"steps": self._STEPS}, tags=given)
        assert out.startswith("Saved plan: [7]"), out
        assert saved[0]["tags"] == stored

    def test_the_reply_says_what_was_removed(self) -> None:
        out, _ = _plan_save({"steps": self._STEPS}, tags="grown,builtin-template,x")
        assert "reserved tags removed" in out and "grown" in out and "builtin-template" in out
        quiet, _ = _plan_save({"steps": self._STEPS}, tags="x")
        assert quiet == "Saved plan: [7] q"

    def test_a_tag_only_grown_claim_does_not_serve_a_lookup_after_the_save(self) -> None:
        from nexus.plans import eligibility as el

        _, saved = _plan_save({"steps": self._STEPS}, tags="grown")
        standing = el.classify_plan(saved[0]["tags"], saved[0]["plan_json"])
        assert standing.provenance == "saved", "never grown: the tag is gone"
        assert not el.eligibility(PlanAnswerKind.LOOKUP, standing.kind, standing.provenance).eligible

    def test_the_tool_description_names_the_reserved_tags(self) -> None:
        from nexus.mcp.core import plan_save

        assert "grown" in plan_save.__doc__ and "builtin-template" in plan_save.__doc__


class TestTemplatesDeclareOnlyAtTemplateLevel:
    def _dir_with(self, tmp_path: Path, mutate) -> Path:
        template = _template("hybrid-factual-lookup.yml")
        mutate(template)
        (tmp_path / "hybrid-factual-lookup.yml").write_text(yaml.safe_dump(template), encoding="utf-8")
        return tmp_path

    def test_a_template_with_an_unknown_kind_is_refused_at_validation(self) -> None:
        template = _template("hybrid-factual-lookup.yml")
        template["answer_kind"] = "recipe"
        with pytest.raises(PlanTemplateSchemaError, match="answer_kind"):
            validate_plan_template(template)

    def test_every_builtin_template_still_validates(self) -> None:
        for name in _TABLE:
            validate_plan_template(_template(name))

    def test_an_embedded_plan_json_declaration_is_refused_at_validation(self) -> None:
        template = _template("hybrid-factual-lookup.yml")
        template["plan_json"][ANSWER_KIND_KEY] = declaration_payload(PlanAnswerKind.SYNTHESIS, "curated")
        with pytest.raises(PlanTemplateSchemaError, match="template-level"):
            validate_plan_template(template)

    def test_the_seed_loader_refuses_an_embedded_declaration_and_inserts_nothing(self, tmp_path: Path) -> None:
        directory = self._dir_with(tmp_path, lambda t: t["plan_json"].update(
            {ANSWER_KIND_KEY: declaration_payload(PlanAnswerKind.SYNTHESIS, "curated")}))
        lib = _FakeLibrary()
        result = load_seed_directory(directory, library=lib, reconcile=True)
        assert [p for p, _ in result.errors] == [str(directory / "hybrid-factual-lookup.yml")]
        assert "template-level" in result.errors[0][1]
        assert lib.saves == [] and not (result.inserted or result.updated)

    def test_the_seed_loader_refuses_an_explicit_null_kind_and_validation_agrees(self, tmp_path: Path) -> None:
        template = _template("hybrid-factual-lookup.yml")
        template["answer_kind"] = None
        with pytest.raises(PlanTemplateSchemaError, match="answer_kind"):
            validate_plan_template(template)
        directory = self._dir_with(tmp_path, lambda t: t.update({"answer_kind": None}))
        lib = _FakeLibrary()
        result = load_seed_directory(directory, library=lib, reconcile=True)
        assert len(result.errors) == 1 and "answer_kind" in result.errors[0][1]
        assert lib.saves == []
        # the row builder, called directly, agrees: null is not "absent"
        with pytest.raises(ValueError):
            desired_row_for_template(template)

    def test_the_row_builder_drops_an_embedded_declaration_when_the_template_level_key_is_absent(self) -> None:
        template = _template("hybrid-factual-lookup.yml")
        template.pop("answer_kind")
        template["plan_json"][ANSWER_KIND_KEY] = declaration_payload(PlanAnswerKind.SYNTHESIS, "curated")
        stored = json.loads(desired_row_for_template(template).plan_json)
        assert ANSWER_KIND_KEY not in stored

    def test_a_valid_template_level_kind_is_loaded_curated(self, tmp_path: Path) -> None:
        directory = self._dir_with(tmp_path, lambda t: t.update({"answer_kind": "debug"}))
        lib = _FakeLibrary()
        result = load_seed_directory(directory, library=lib, reconcile=True)
        assert not result.errors and len(result.inserted) == 1
        decl = decode_declaration(lib.saves[0]["plan_json"])
        assert (decl.kind, decl.provenance) == (PlanAnswerKind.DEBUG, "curated")


class TestOnlyTheBuiltinTierClaimsCurated:
    """nexus-0vh80 (Sam, 2026-11-03, closes nexus-8ofx8): a template under a project, RDR or repo tier is authored
    by whoever owns that repo, so its declared kind is not a reviewed one. The seed loader writes it ``saved``."""

    def _tier(self, tmp_path: Path, scope: str, *, tags: str | None = None, kind: str = "lookup"):
        template = _template("hybrid-factual-lookup.yml")
        template["answer_kind"] = kind
        if tags is not None:
            template["tags"] = tags
        template["dimensions"] = {**template["dimensions"], "scope": scope}
        directory = tmp_path / scope
        directory.mkdir()
        (directory / "t.yml").write_text(yaml.safe_dump(template), encoding="utf-8")
        lib = _FakeLibrary()
        result = load_seed_directory(directory, library=lib, reconcile=True, scope_override=scope)
        assert not result.errors, result.errors
        return lib.saves[0]

    @pytest.mark.parametrize("scope", ["project", "repo", "rdr-232-answer-kind"])
    def test_a_builtin_tagged_template_in_another_tier_reads_saved(self, tmp_path: Path, scope: str) -> None:
        from nexus.plans.eligibility import classify_plan

        saved = self._tier(tmp_path, scope, tags="builtin-template,research")
        assert "builtin-template" in saved["tags"], "the author's tags are stored as written"
        decl = decode_declaration(saved["plan_json"])
        assert (decl.kind, decl.provenance, decl.route) == (PlanAnswerKind.LOOKUP, "saved", None)
        standing = classify_plan(saved["tags"], saved["plan_json"])
        assert (standing.kind, standing.provenance, standing.flag) == (PlanAnswerKind.LOOKUP, "saved", None)

    def test_the_global_tier_still_declares_curated(self, tmp_path: Path) -> None:
        from nexus.plans.eligibility import classify_plan

        saved = self._tier(tmp_path, "global", tags="builtin-template")
        assert saved["project"] == ""
        assert decode_declaration(saved["plan_json"]).provenance == "curated"
        assert classify_plan(saved["tags"], saved["plan_json"]).provenance == "curated"

    def test_the_row_builder_follows_the_templates_scope(self) -> None:
        template = _template("hybrid-factual-lookup.yml")
        assert decode_declaration(desired_row_for_template(template).plan_json).provenance == "curated"
        template["dimensions"] = {**template["dimensions"], "scope": "project"}
        assert decode_declaration(desired_row_for_template(template).plan_json).provenance == "saved"
        template["dimensions"] = {k: v for k, v in template["dimensions"].items() if k != "scope"}
        assert decode_declaration(desired_row_for_template(template).plan_json).provenance == "curated", (
            "no scope is the global tier's project ('') in the loader's own mapping"
        )

    def test_a_template_with_no_declared_kind_still_declares_nothing_in_any_tier(self, tmp_path: Path) -> None:
        template = _template("hybrid-factual-lookup.yml")
        template.pop("answer_kind")
        template["dimensions"] = {**template["dimensions"], "scope": "project"}
        assert ANSWER_KIND_KEY not in json.loads(desired_row_for_template(template).plan_json)

    def test_reseeding_rewrites_a_project_tier_row_that_still_says_curated(self, tmp_path: Path) -> None:
        template = _template("hybrid-factual-lookup.yml")
        template["dimensions"] = {**template["dimensions"], "scope": "project"}
        directory = tmp_path / "p"
        directory.mkdir()
        (directory / "t.yml").write_text(yaml.safe_dump(template), encoding="utf-8")
        lib = _FakeLibrary()
        stale_row = json.loads(desired_row_for_template(template).plan_json)
        stale_row[ANSWER_KIND_KEY] = declaration_payload(PlanAnswerKind.LOOKUP, "curated")
        desired = desired_row_for_template(template)
        lib.save_plan(
            query=desired.query, plan_json=json.dumps(stale_row), outcome="success", tags=desired.tags, project="project",
            name=desired.name, verb=desired.verb, scope=desired.scope,
            dimensions=canonical_dimensions_json(template["dimensions"]),
            default_bindings=desired.default_bindings, parent_dims=desired.parent_dims,
        )
        lib.saves.clear()
        result = load_seed_directory(directory, library=lib, reconcile=True, scope_override="project")
        assert len(result.updated) == 1 and not result.errors
        assert decode_declaration(lib.saves[0]["plan_json"]).provenance == "saved"


class TestGoldenLabelMap:
    """The golden-set request labels (never renamed) map one-to-one onto the plan vocabulary."""

    def test_every_golden_label_maps_and_the_image_is_the_whole_vocabulary(self) -> None:
        from bench.schema import ANSWER_KINDS

        assert set(GOLDEN_LABEL_TO_KIND) == set(ANSWER_KINDS), "no golden label is unmapped, no stray key"
        assert set(GOLDEN_LABEL_TO_KIND.values()) == set(PlanAnswerKind), "bijection onto all seven, unknown too"
        assert len(set(GOLDEN_LABEL_TO_KIND.values())) == len(GOLDEN_LABEL_TO_KIND)
        assert all(isinstance(k, PlanAnswerKind) for k in GOLDEN_LABEL_TO_KIND.values())

    def test_the_listing_kind_is_the_only_label_that_is_spelled_differently(self) -> None:
        assert golden_label_kind("discovery") is PlanAnswerKind.DISCOVERY_LISTING
        renamed = {label for label, kind in GOLDEN_LABEL_TO_KIND.items() if label != kind.value}
        assert renamed == {"discovery"}

    def test_the_golden_set_file_only_uses_mapped_labels(self) -> None:
        from bench.schema import load_golden_set

        golden = load_golden_set(Path(__file__).parent.parent / "bench" / "queries" / "golden_v1.yaml")
        labels = {q.answer_kind for q in golden}
        assert labels and labels <= set(GOLDEN_LABEL_TO_KIND), labels
        assert "discovery" in labels, "non-vacuity: the one spelled-differently label is in use"
        assert [golden_label_kind(q.answer_kind) for q in golden][0] in set(PlanAnswerKind)

    def test_an_unmapped_label_is_a_key_error_not_a_silent_unknown(self) -> None:
        with pytest.raises(KeyError):
            golden_label_kind("listing")


class TestGrownProvenance:
    _BODY = json.dumps({"steps": [{"tool": "search", "args": {"query": "$intent"}}]})

    def test_a_grown_save_with_no_known_route_records_unknown_and_grown_provenance(self) -> None:
        out = json.loads(grown_plan_json(self._BODY, route_kind=None, route_source=None))
        assert out[ANSWER_KIND_KEY] == {"version": 1, "kind": "unknown", "provenance": "grown"}
        assert out["steps"] == json.loads(self._BODY)["steps"]

    def test_a_known_route_kind_is_recorded_only_with_its_provenance(self) -> None:
        out = json.loads(grown_plan_json(
            self._BODY, route_kind=PlanAnswerKind.SYNTHESIS, route_source="heuristic"))
        assert out[ANSWER_KIND_KEY] == {
            "version": 1, "kind": "synthesis", "provenance": "grown", "route": "heuristic"}
        assert decode_answer_kind(json.dumps(out)) is PlanAnswerKind.SYNTHESIS

    @pytest.mark.parametrize("route_kind, route_source", [
        (PlanAnswerKind.SYNTHESIS, None),
        (PlanAnswerKind.SYNTHESIS, ""),
        (PlanAnswerKind.SYNTHESIS, "guessed"),
        (None, "explicit"),
        (PlanAnswerKind.UNKNOWN, "explicit"),
    ])
    def test_a_kind_without_route_provenance_is_never_recorded(self, route_kind: Any, route_source: Any) -> None:
        out = json.loads(grown_plan_json(self._BODY, route_kind=route_kind, route_source=route_source))
        assert out[ANSWER_KIND_KEY] == {"version": 1, "kind": "unknown", "provenance": "grown"}

    def test_a_string_route_kind_is_coerced_and_a_bad_string_records_unknown(self) -> None:
        out = json.loads(grown_plan_json(self._BODY, route_kind="synthesis", route_source="model-assisted"))
        assert out[ANSWER_KIND_KEY] == {
            "version": 1, "kind": "synthesis", "provenance": "grown", "route": "model-assisted"}
        for bad in ("essay", "", 7, "Synthesis"):
            out = json.loads(grown_plan_json(self._BODY, route_kind=bad, route_source="explicit"))  # type: ignore[arg-type]
            assert out[ANSWER_KIND_KEY] == {"version": 1, "kind": "unknown", "provenance": "grown"}, bad

    def test_a_declaration_the_planner_emitted_is_replaced_not_trusted(self) -> None:
        forged = json.dumps({
            "steps": [], "answer_kind": declaration_payload(PlanAnswerKind.LOOKUP, "curated")})
        out = json.loads(grown_plan_json(forged, route_kind=None, route_source=None))
        assert out[ANSWER_KIND_KEY]["provenance"] == "grown"
        assert out[ANSWER_KIND_KEY]["kind"] == "unknown"

    def test_a_plan_json_that_is_not_an_object_is_returned_untouched(self) -> None:
        assert grown_plan_json("not json", route_kind=None, route_source=None) == "not json"
        assert grown_plan_json("[1]", route_kind=None, route_source=None) == "[1]"


class TestMatchSeesTheDeclaration:
    def test_a_match_exposes_the_decoded_kind(self) -> None:
        from nexus.plans.match import Match

        row = {
            "id": 5, "query": "q", "plan_json": desired_row_for_template(
                _template("review-default.yml")).plan_json,
        }
        assert Match.from_plan_row(row).answer_kind is PlanAnswerKind.COMPARISON
        legacy = {"id": 6, "query": "q", "plan_json": json.dumps({"steps": []})}
        assert Match.from_plan_row(legacy).answer_kind is PlanAnswerKind.UNKNOWN


class _FakeLibrary:
    """The slice of HttpPlanLibrary the seed loader calls, with rows held in memory."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.saves: list[dict[str, Any]] = []
        self.deletes: list[int] = []
        self._next = 100

    def get_plan_by_dimensions(self, *, project: str, dimensions: str):
        for row in self.rows.values():
            if (row.get("project") or "") == project and row["dimensions"] == dimensions:
                return dict(row)
        return None

    def list_plans(self, *, limit: int, project: str, include_disabled: bool = False):
        return [dict(r) for r in self.rows.values() if (r.get("project") or "") == project]

    def delete_plan(self, plan_id: int) -> bool:
        self.deletes.append(plan_id)
        return self.rows.pop(plan_id, None) is not None

    def save_plan(self, **kw: Any) -> int:
        self.saves.append(kw)
        for row in self.rows.values():  # upsert on (project, query), as the service does
            if (row.get("project") or "") == kw["project"] and row["query"] == kw["query"]:
                row.update(kw)
                return int(row["id"])
        self._next += 1
        self.rows[self._next] = {"id": self._next, **kw}
        return self._next


def _pre_declaration_library() -> _FakeLibrary:
    """A library holding the 12 builtins exactly as the previous release seeded them (no declaration)."""
    lib = _FakeLibrary()
    for name in sorted(set(_TABLE) - _ADDED_AFTER_DECLARATION):
        template = _template(name)
        template.pop("answer_kind", None)
        row = desired_row_for_template(template)
        lib.save_plan(
            query=row.query, plan_json=row.plan_json, outcome="success", tags=row.tags, project="",
            name=row.name, verb=row.verb, scope=row.scope,
            dimensions=canonical_dimensions_json(template["dimensions"]),
            default_bindings=row.default_bindings, parent_dims=row.parent_dims,
        )
    lib.saves.clear()
    return lib


class TestSeedReconciliation:
    def test_reconcile_updates_each_builtin_in_place_and_moves_no_identity(self) -> None:
        lib = _pre_declaration_library()
        before = {r["id"]: (r["dimensions"], r["query"], r["name"], r["verb"], r["scope"], r["tags"])
                  for r in lib.rows.values()}
        assert len(before) == 12
        assert all(decode_answer_kind(r["plan_json"]) is PlanAnswerKind.UNKNOWN for r in lib.rows.values())

        result = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)

        previous = sorted(set(_TABLE) - _ADDED_AFTER_DECLARATION)
        assert sorted(result.updated) == previous, result
        assert sorted(result.inserted) == sorted(_ADDED_AFTER_DECLARATION), "a reseed inserts only the builtin added since"
        assert not (result.errors or result.protected)
        assert lib.deletes == [], "no row is dropped and re-inserted: the match/use counters survive"
        assert len(lib.saves) == 13, "12 updates in place and 1 insert"
        after = {r["id"]: (r["dimensions"], r["query"], r["name"], r["verb"], r["scope"], r["tags"])
                 for r in lib.rows.values() if r["id"] in before}
        assert after == before, "ids, dimensions, descriptions, names, verbs, scopes, tags: byte-identical"
        by_dims = {canonical_dimensions_json(_template(n)["dimensions"]): _template(n) for n in _TABLE}
        for row in lib.rows.values():
            assert row["tags"] == by_dims[row["dimensions"]]["tags"], "tags equal the YAML's own"
        for row in lib.rows.values():
            assert decode_answer_kind(row["plan_json"]) is _TABLE[next(
                n for n in _TABLE if canonical_dimensions_json(_template(n)["dimensions"]) == row["dimensions"])]

    def test_a_second_reconcile_writes_nothing(self) -> None:
        lib = _pre_declaration_library()
        load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
        lib.saves.clear()
        again = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
        assert lib.saves == [] and again.updated == []
        assert sorted(again.skipped_existing) == sorted(_TABLE)  # the 12 updated, the 1 inserted

    def test_the_insert_only_path_leaves_an_old_row_declaring_nothing(self) -> None:
        lib = _pre_declaration_library()
        result = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=False)
        previous = sorted(set(_TABLE) - _ADDED_AFTER_DECLARATION)
        assert sorted(result.skipped_existing) == previous and sorted(result.inserted) == sorted(_ADDED_AFTER_DECLARATION)
        assert [kw["name"] for kw in lib.saves] == ["entity-resolution"], "only the new builtin is written"
        old_rows = [r for r in lib.rows.values() if r["name"] != "entity-resolution"]
        assert len(old_rows) == 12
        assert all(decode_answer_kind(r["plan_json"]) is PlanAnswerKind.UNKNOWN for r in old_rows)

    def test_a_grown_row_at_a_builtin_description_is_still_protected(self) -> None:
        lib = _pre_declaration_library()
        victim = next(r for r in lib.rows.values() if r["name"] == "hybrid-factual-lookup")
        victim["tags"] = "ad-hoc,grown"
        result = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
        assert [n for n, _ in result.protected] == ["hybrid-factual-lookup.yml"]
        assert decode_answer_kind(victim["plan_json"]) is PlanAnswerKind.UNKNOWN


class TestSessionCache:
    def _cache(self, session: str):
        from nexus.plans.session_cache import PlanSessionCache

        return PlanSessionCache(client=make_vector_test_client(), session_id=session)

    def _meta(self, cache: Any, session: str, plan_id: int) -> dict[str, Any]:
        got = cache._col.get(ids=[f"{session}:{plan_id}"], include=["metadatas"])
        return got["metadatas"][0]

    def test_an_upserted_row_carries_its_declared_kind(self) -> None:
        cache = self._cache("kind-upsert")
        row = {
            "id": 7, "query": "Which paper reported this result?", "verb": "lookup",
            "name": "hybrid-factual-lookup", "scope": "global", "tags": "builtin-template",
            "plan_json": desired_row_for_template(_template("hybrid-factual-lookup.yml")).plan_json,
        }
        assert cache.upsert(row)
        assert self._meta(cache, "kind-upsert", 7)["answer_kind"] == "lookup"

    def test_a_legacy_row_is_cached_as_unknown(self) -> None:
        cache = self._cache("kind-legacy")
        row = {"id": 8, "query": "some old plan", "plan_json": json.dumps({"steps": []})}
        assert cache.upsert(row)
        meta = self._meta(cache, "kind-legacy", 8)
        assert meta["answer_kind"] == "unknown"
        assert (meta["answer_provenance"], meta["answer_route"]) == ("", ""), "unattested: no provenance"

    def test_the_cache_carries_provenance_and_route_beside_the_kind(self) -> None:
        cache = self._cache("kind-provenance")
        curated = desired_row_for_template(_template("debug-default.yml")).plan_json
        grown_unknown = grown_plan_json(json.dumps({"steps": []}), route_kind=None, route_source=None)
        grown_routed = grown_plan_json(
            json.dumps({"steps": []}), route_kind=PlanAnswerKind.AUDIT, route_source="heuristic")
        for pid, plan_json in ((1, curated), (2, grown_unknown), (3, grown_routed)):
            assert cache.upsert({"id": pid, "query": f"plan number {pid}", "plan_json": plan_json})
        got = {pid: self._meta(cache, "kind-provenance", pid) for pid in (1, 2, 3)}
        assert (got[1]["answer_kind"], got[1]["answer_provenance"], got[1]["answer_route"]) == ("debug", "curated", "")
        assert (got[2]["answer_kind"], got[2]["answer_provenance"], got[2]["answer_route"]) == ("unknown", "grown", "")
        assert (got[3]["answer_kind"], got[3]["answer_provenance"], got[3]["answer_route"]) == (
            "audit", "grown", "heuristic")

    def test_a_refresh_after_reseed_sees_the_new_declaration(self) -> None:
        lib = _pre_declaration_library()

        class _Active:
            def list_active_plans(self, *, project: str = "") -> list[dict[str, Any]]:
                return [dict(r) for r in lib.rows.values()]

        cache = self._cache("kind-refresh")
        assert cache.populate(_Active()) == 12  # the library the previous release seeded
        ids = {r["name"] + "/" + r["verb"]: r["id"] for r in lib.rows.values()}
        lookup_id = ids["hybrid-factual-lookup/lookup"]
        assert self._meta(cache, "kind-refresh", lookup_id)["answer_kind"] == "unknown"

        load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
        assert cache.populate(_Active()) == 13  # the reseed inserted entity-resolution
        assert self._meta(cache, "kind-refresh", lookup_id)["answer_kind"] == "lookup"
