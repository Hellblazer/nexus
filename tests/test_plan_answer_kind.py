# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 2 step 1 (nexus-jypmj.2.1): the plan answer-kind declaration.

A plan declares the kind of answer it can serve in a validated, versioned top-level ``plan_json`` field,
never in ``dimensions`` (those are plan identity). Nothing here filters plans by kind: that is
nexus-jypmj.2.3. These tests pin the vocabulary, the 12 builtin declarations, the tolerant decoder,
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
    golden_label_kind,
    grown_plan_json,
    strip_declaration,
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
    "debug-default.yml": PlanAnswerKind.DEBUG,
    "document-default.yml": PlanAnswerKind.AUDIT,
    "hybrid-factual-lookup.yml": PlanAnswerKind.LOOKUP,
    "review-default.yml": PlanAnswerKind.COMPARISON,
}

#: What the 12 builtins were BEFORE this bead, captured from the tree at origin/develop 4898d437d:
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
}


#: Per builtin, captured from the tree at origin/develop 4898d437d (the YAML blobs there run through the
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
}


def _template(name: str) -> dict[str, Any]:
    return yaml.safe_load((_BUILTIN_DIR / name).read_text(encoding="utf-8"))


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def test_the_table_covers_every_builtin_file() -> None:
    """Non-vacuity: the parametrized tests below walk a table that is the whole shipped set."""
    on_disk = sorted(p.name for p in _BUILTIN_DIR.glob("*.yml"))
    assert on_disk == sorted(_TABLE) == sorted(_BEFORE) == sorted(_BEFORE_ROW)
    assert len(on_disk) == 12


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
        assert _h(row.tags) == tags_h, "tags are byte-identical to develop 4898d437d"
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
        ],
    )
    def test_validate_declaration_refuses_a_bad_one_by_name(self, raw: Any) -> None:
        with pytest.raises(AnswerKindError):
            validate_declaration(raw)

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


def _plan_save(plan: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
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
    ):
        return plan_save(query="q", plan_json=json.dumps(plan), verb="research"), saved


class TestPlanSaveNeverStoresADeclaration:
    _STEPS = [{"tool": "search", "args": {"query": "$intent"}}]

    def test_a_forged_curated_declaration_is_not_stored(self) -> None:
        forged = declaration_payload(PlanAnswerKind.SYNTHESIS, "curated")
        out, saved = _plan_save({"steps": self._STEPS, ANSWER_KIND_KEY: forged})
        assert out.startswith("Saved plan: [7]") and "answer_kind field removed" in out
        assert ANSWER_KIND_KEY not in json.loads(saved[0]["plan_json"])
        assert decode_declaration(saved[0]["plan_json"]).provenance is None

    def test_a_forged_grown_route_declaration_is_not_stored(self) -> None:
        forged = declaration_payload(PlanAnswerKind.LOOKUP, "grown", "explicit")
        out, saved = _plan_save({"steps": self._STEPS, ANSWER_KIND_KEY: forged})
        stored = json.loads(saved[0]["plan_json"])
        assert ANSWER_KIND_KEY not in stored and stored["steps"] == self._STEPS
        assert decode_answer_kind(saved[0]["plan_json"]) is PlanAnswerKind.UNKNOWN
        assert "only seeded templates declare an answer kind" in out

    def test_a_plan_with_an_unrelated_answer_kind_key_saves_with_the_key_gone(self) -> None:
        out, saved = _plan_save({"steps": self._STEPS, ANSWER_KIND_KEY: "essay"})
        assert out.startswith("Saved plan: [7]"), out
        assert json.loads(saved[0]["plan_json"]) == {"steps": self._STEPS}

    def test_a_plan_without_the_key_is_stored_byte_for_byte_and_the_reply_is_unchanged(self) -> None:
        plan = {"steps": self._STEPS}
        out, saved = _plan_save(plan)
        assert saved[0]["plan_json"] == json.dumps(plan)
        assert out == "Saved plan: [7] q"

    def test_strip_declaration_reports_whether_it_removed_anything(self) -> None:
        assert strip_declaration({"steps": [], ANSWER_KIND_KEY: 1}) == ({"steps": []}, True)
        plan = {"steps": []}
        assert strip_declaration(plan) == (plan, False)


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
    for name in sorted(_TABLE):
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

        assert sorted(result.updated) == sorted(_TABLE), result
        assert not (result.inserted or result.errors or result.protected)
        assert lib.deletes == [], "no row is dropped and re-inserted: the match/use counters survive"
        assert len(lib.saves) == 12
        after = {r["id"]: (r["dimensions"], r["query"], r["name"], r["verb"], r["scope"], r["tags"])
                 for r in lib.rows.values()}
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
        assert sorted(again.skipped_existing) == sorted(_TABLE)

    def test_the_insert_only_path_leaves_an_old_row_declaring_nothing(self) -> None:
        lib = _pre_declaration_library()
        result = load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=False)
        assert lib.saves == [] and sorted(result.skipped_existing) == sorted(_TABLE)
        assert all(decode_answer_kind(r["plan_json"]) is PlanAnswerKind.UNKNOWN for r in lib.rows.values())

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
        assert cache.populate(_Active()) == 12
        ids = {r["name"] + "/" + r["verb"]: r["id"] for r in lib.rows.values()}
        lookup_id = ids["hybrid-factual-lookup/lookup"]
        assert self._meta(cache, "kind-refresh", lookup_id)["answer_kind"] == "unknown"

        load_seed_directory(_BUILTIN_DIR, library=lib, reconcile=True)
        assert cache.populate(_Active()) == 12
        assert self._meta(cache, "kind-refresh", lookup_id)["answer_kind"] == "lookup"
