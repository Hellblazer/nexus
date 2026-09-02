# SPDX-License-Identifier: AGPL-3.0-or-later
"""Byte-identity golden tests for ``nexus.mcp.operator_requests`` —
RDR-200 Phase 0 (nexus-5mft0.1).

Each ``build_<op>_request`` was hoisted verbatim out of the
corresponding ``operator_*`` tool in ``src/nexus/mcp/core.py``. These
goldens pin the EXACT (prompt, schema) pair each builder returns for
fixed args, captured from the pre-refactor inline construction (develop
3c3c10321) — a change to either the literal prompt text or the schema
shape fails here first. Modeled on
``tests/test_operator_proxy_builder_fidelity.py``'s verbatim-fidelity
approach, but simpler: since the builder IS the extracted source (not a
separately-maintained harness copy), a plain equality assertion against
a committed expected value is the direct byte-identity check — no
JoinedStr-shape rendering needed.
"""
from __future__ import annotations

import ast
import inspect

from nexus.mcp import operator_requests as req


class TestExtractGolden:
    def test_prompt_and_schema(self) -> None:
        prompt, schema = req.build_extract_request(
            inputs='[{"id": 1}]', fields="id,name",
        )
        assert prompt == (
            "Extract the following fields from each item: id,name\n\n"
            'Items:\n[{"id": 1}]'
        )
        assert schema == {
            "type": "object",
            "required": ["extractions"],
            "properties": {
                "extractions": {
                    "type": "array",
                    "items": {"type": "object"},
                }
            },
        }


class TestRankGolden:
    def test_prompt_and_schema(self) -> None:
        prompt, schema = req.build_rank_request(
            items='["a", "b"]', criterion="relevance",
        )
        assert prompt == (
            "Rank the following items by relevance.\n"
            "Return them in ranked order, best first.\n\n"
            'Items:\n["a", "b"]'
        )
        assert schema == {
            "type": "object",
            "required": ["ranked"],
            "properties": {
                "ranked": {"type": "array", "items": {"type": "string"}},
            },
        }


class TestCompareGolden:
    _EXPECTED_SCHEMA = {
        "type": "object",
        "required": ["comparison"],
        "properties": {
            "comparison": {"type": "string"},
        },
    }

    def test_one_sided_branch(self) -> None:
        """``items`` only — one-sided compare. This is the branch a lone
        call with no ``items_a``/``items_b`` takes (core.py:5584)."""
        prompt, schema = req.build_compare_request(items="alpha, beta", focus="cost")
        assert prompt == (
            "Compare the following items. Focus on: cost.\n\n"
            "Items:\nalpha, beta"
        )
        assert schema == self._EXPECTED_SCHEMA

    def test_one_sided_branch_no_focus(self) -> None:
        prompt, schema = req.build_compare_request(items="alpha, beta")
        assert prompt == "Compare the following items.\n\nItems:\nalpha, beta"
        assert schema == self._EXPECTED_SCHEMA

    def test_two_sided_branch(self) -> None:
        """``items_a`` and ``items_b`` both given — the cross-set compare
        branch (core.py:5567), distinct prompt shape from one-sided."""
        prompt, schema = req.build_compare_request(
            items_a="foo", items_b="bar", label_a="Left", label_b="Right",
        )
        assert prompt == (
            "Compare two sets of items across corpora.\n\n"
            "Set Left:\nfoo\n\n"
            "Set Right:\nbar\n\n"
            "Name:\n"
            "  * **Shared axes**: concerns both Left and Right "
            "address with comparable intent (even if mechanism differs).\n"
            "  * **Divergent decisions**: places where Left and Right "
            "take different approaches on the same question; attribute each "
            "choice to its side.\n"
            "  * **Side-only axes**: concerns that appear in Left or "
            "Right but not both.\n"
            "  * **Philosophy difference**: one or two sentences on the "
            "underlying stance difference, if one emerges from the evidence."
        )
        assert schema == self._EXPECTED_SCHEMA

    def test_two_sided_branch_json_serializes_list_dict_values(self) -> None:
        """``items_a``/``items_b`` given as list/dict get JSON-serialized,
        not Python-``repr``'d (core.py's ``_fmt`` helper)."""
        prompt, _schema = req.build_compare_request(
            items_a=[{"x": 1}], items_b={"y": 2},
        )
        assert '"x": 1' in prompt
        assert '"y": 2' in prompt
        assert "{'x': 1}" not in prompt


class TestSummarizeGolden:
    def test_uncited(self) -> None:
        prompt, schema = req.build_summarize_request("some content")
        assert prompt == "Summarize the following content concisely.\n\nsome content"
        assert schema == {
            "type": "object",
            "required": ["summary"],
            "properties": {
                "summary": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
        }

    def test_cited(self) -> None:
        prompt, _schema = req.build_summarize_request("some content", cited=True)
        assert prompt == (
            "Summarize the following content concisely. Include citations "
            "as a list of source references.\n\nsome content"
        )


class TestGenerateGolden:
    def test_uncited(self) -> None:
        prompt, schema = req.build_generate_request("summary", "the context")
        assert prompt == "Generate a summary.\n\nContext:\nthe context"
        assert schema == {
            "type": "object",
            "required": ["output"],
            "properties": {
                "output": {"type": "string"},
                "citations": {"type": "array", "items": {"type": "string"}},
            },
        }

    def test_cited(self) -> None:
        prompt, _schema = req.build_generate_request("summary", "the context", cited=True)
        assert prompt == (
            "Generate a summary. Include citations as a list of source "
            "references.\n\nContext:\nthe context"
        )

    def test_schema_not_unified_with_bundle_terminal_schema(self) -> None:
        """RDR-200 P0 audit round 2 residual: this schema carries
        ``citations``; ``bundle._terminal_schema("generate")`` does not.
        They must stay divergent."""
        _prompt, schema = req.build_generate_request("t", "c")
        assert "citations" in schema["properties"]


class TestFilterGolden:
    def test_prompt_and_schema(self) -> None:
        prompt, schema = req.build_filter_request(
            items='[{"id": "a"}]', criterion="category is hooks",
        )
        assert prompt == (
            "Filter the following items by this criterion: category is hooks\n"
            "Return only the items that satisfy the criterion in the 'items' "
            "array. Populate 'rationale' with one entry per input item, "
            "keyed by the item's id, giving the reason each item was kept "
            "or rejected. The output 'items' array must be a subset of the "
            "input; never add synthetic items.\n\n"
            'Items:\n[{"id": "a"}]'
        )
        assert schema == {
            "type": "object",
            "required": ["items", "rationale"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object"},
                },
                "rationale": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["id", "reason"],
                        "properties": {
                            "id": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                },
            },
        }


class TestCheckGolden:
    def test_prompt_and_schema(self) -> None:
        prompt, schema = req.build_check_request(
            items='[{"id": "a"}]', check_instruction="all items agree",
        )
        assert prompt == (
            "Check whether the following items are consistent with this "
            "claim or question: all items agree\n"
            "Set ok=true when every item supports the claim, false when at "
            "least one item contradicts it. Populate 'evidence' with a "
            "record per item containing a short grounding 'quote' and a "
            "'role' of 'supports', 'contradicts', or 'neutral'. Keep quotes "
            "short enough to be verifiable against the source item.\n\n"
            'Items:\n[{"id": "a"}]'
        )
        assert schema == {
            "type": "object",
            "required": ["ok", "evidence"],
            "properties": {
                "ok": {"type": "boolean"},
                "evidence": {
                    "type": "array",
                    "items": req._CHECK_EVIDENCE_ITEM_SCHEMA,
                },
            },
        }
        assert schema["properties"]["evidence"]["items"] == {
            "type": "object",
            "required": ["item_id", "quote", "role"],
            "properties": {
                "item_id": {"type": "string"},
                "quote": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["supports", "contradicts", "neutral"],
                },
            },
        }


class TestVerifyGolden:
    def test_prompt_and_schema(self) -> None:
        prompt, schema = req.build_verify_request(
            claim="X is true", evidence="the evidence text",
        )
        assert prompt == (
            "Verify whether the following claim is grounded in the evidence "
            "provided.\n\n"
            "Claim: X is true\n\n"
            "Evidence:\nthe evidence text\n\n"
            "Set verified=true only when the claim is directly supported by "
            "the evidence. Provide a concise 'reason' explaining the "
            "verdict. Populate 'citations' with locators (section, page, "
            "table, or quoted span snippets) that pinpoint the supporting "
            "or contradicting passages."
        )
        assert schema == {
            "type": "object",
            "required": ["verified", "reason", "citations"],
            "properties": {
                "verified": {"type": "boolean"},
                "reason": {"type": "string"},
                "citations": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        }


class TestGroupbyGolden:
    def test_prompt_and_schema(self) -> None:
        prompt, schema = req.build_groupby_request(
            items='[{"id": "a"}]', key="category",
        )
        assert prompt == (
            "Partition the following items by this key: category\n"
            "Output a list of groups. Each group has a string `key_value` "
            "(the partition label, e.g. a year, a fault model, a system "
            "property) and an `items` array carrying each item's full "
            "content INLINE — preserve the original `id` field and any "
            "other fields verbatim. Every input item appears in exactly "
            "one group's `items`. Items the partition cannot confidently "
            'assign go in a group with `key_value` of "unassigned".\n\n'
            "Do not reference items by id-only — carry the full item "
            "dicts in each group's `items` array so downstream operators "
            "see the content without a separate lookup.\n\n"
            'Items:\n[{"id": "a"}]'
        )
        assert schema == {
            "type": "object",
            "required": ["groups"],
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["key_value", "items"],
                        "properties": {
                            "key_value": {"type": "string"},
                            "items": {
                                "type": "array",
                                "items": {"type": "object"},
                            },
                        },
                    },
                },
            },
        }


class TestAggregateGolden:
    def test_prompt_and_schema(self) -> None:
        prompt, schema = req.build_aggregate_request(
            groups='[{"key_value": "a", "items": []}]', reducer="count",
        )
        assert prompt == (
            "Reduce each group of items into a per-group summary using "
            "this reducer instruction: count\n\n"
            "Output one aggregate per input group, preserving the group's "
            "`key_value` verbatim. Each `summary` MUST reference only the "
            "items in that group's `items` array. Do NOT pull content "
            "from items in other groups, even when vocabulary overlaps "
            "across groups. The summary is a short paragraph answering "
            "the reducer instruction USING ONLY this group's items.\n\n"
            'Groups:\n[{"key_value": "a", "items": []}]'
        )
        assert schema == {
            "type": "object",
            "required": ["aggregates"],
            "properties": {
                "aggregates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["key_value", "summary"],
                        "properties": {
                            "key_value": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                    },
                },
            },
        }


class TestBuildersArePure:
    """No I/O, no dispatch, no model logic — the module docstring's
    contract, mechanically checked against actual import/call nodes
    (not the docstring prose, which legitimately names these things
    while explaining why they're absent)."""

    @staticmethod
    def _module_import_names() -> set[str]:
        tree = ast.parse(inspect.getsource(req))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        return names

    def test_module_has_no_dispatch_or_model_tier_imports(self) -> None:
        names = self._module_import_names()
        assert not any("dispatch" in n for n in names), names
        assert not any("model_tiers" in n for n in names), names

    def test_module_does_not_import_mcp_core(self) -> None:
        """operator_requests.py must sit BELOW core.py in the import
        graph — core.py imports it at module scope, so any reverse
        import here would be a cycle."""
        names = self._module_import_names()
        assert not any("nexus.mcp.core" in n for n in names), names
