"""nexus-ger23: ``nx collection shape`` -- read-only shape audit of the
collection SET against docs/collections.md (not the RDR-087 per-collection
``nx collection audit NAME``, which is nexus.collection_audit).

Pure-logic tests over row dicts (no engine needed) plus a CLI test with the
same mock shape ``test_collection_cmd.py`` uses. The rule-pin test is the
contract: every ``## Rule N`` heading in docs/collections.md has at least
one check, and every check names a rule that exists.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner
from unittest.mock import MagicMock

from nexus.commands import collection as _collection_mod
from nexus.commands.collection import _SHAPE_MAX_LISTED_PER_CHECK
from nexus.corpus import resolve_read_embedding_model
from nexus.mcp.core import _FANOUT_MIN_COLLECTION_CHUNK_COUNT

from nexus.collection_shape import (
    CHECKS_BY_RULE,
    FANOUT_FLOOR,
    PLACEHOLDER_SUBJECTS,
    THIN_CHUNK_FLOOR,
    AuditReport,
    CollectionFacts,
    collection_attributes,
    gather_facts,
    run_checks,
)

_REPO = Path(__file__).resolve().parents[1]

# ── fixtures ───────────────────────────────────────────────────────────────


def _row(name: str, **over) -> dict:
    """A catalog_collections row as HttpCatalogClient.list_collections returns it."""
    base = {
        "name": name, "content_type": "", "owner_id": "", "embedding_model": "",
        "model_version": "", "display_name": "", "legacy_grandfathered": False,
        "superseded_by": "", "superseded_at": "", "created_at": "2026-09-01T00:00:00Z",
    }
    base.update(over)
    return base


def _filled(name: str, **over) -> dict:
    ct, owner, model, ver = name.split("__")
    return _row(name, content_type=ct, owner_id=owner, embedding_model=model,
                model_version=ver, **over)


def _stats(name: str, count: int, dim: int = 1024) -> dict:
    return {"name": name, "dim": dim, "count": count, "last_write": None}


def _cloud_write_model(content_type: str) -> str | None:
    return "voyage-code-3" if content_type == "code" else "voyage-context-3"


def _facts(rows, stats, docs) -> list[CollectionFacts]:
    return gather_facts(catalog_rows=rows, stats_rows=stats, doc_counts=docs)


def _checks(findings, check: str) -> list:
    return [f for f in findings if f.check == check]


# ── the RDR-204 seam ───────────────────────────────────────────────────────


class TestCollectionAttributes:
    def test_row_columns_win_when_present(self) -> None:
        a = collection_attributes(_filled("knowledge__vector-search__voyage-context-3__v1"))
        assert (a.content_type, a.owner_id, a.embedding_model, a.model_version) == (
            "knowledge", "vector-search", "voyage-context-3", "v1")
        assert a.source == "row"

    def test_blank_row_falls_back_to_the_name_and_says_so(self) -> None:
        a = collection_attributes(_row("code__1-2__voyage-code-3__v1"))
        assert (a.content_type, a.owner_id, a.embedding_model) == ("code", "1-2", "voyage-code-3")
        assert a.source == "name"

    def test_two_segment_legacy_name(self) -> None:
        a = collection_attributes(_row("code__myrepo-a1b2c3"))
        assert (a.content_type, a.owner_id) == ("code", "myrepo-a1b2c3")
        assert a.embedding_model == "" and a.source == "name"

    def test_quarantine_prefix_is_a_lifecycle_state_not_a_content_type(self) -> None:
        a = collection_attributes(_row("quarantine-code__1-1__voyage-code-3__v1"))
        assert a.content_type == "code" and a.quarantine is True

    def test_unparseable_name_is_unknown_not_a_crash(self) -> None:
        a = collection_attributes(_row("weird"))
        assert a.source == "unknown" and a.content_type == ""


# ── gather ─────────────────────────────────────────────────────────────────


class TestGatherFacts:
    def test_joins_rows_stats_and_doc_counts(self) -> None:
        rows = [_filled("knowledge__vector-search__voyage-context-3__v1"),
                _row("knowledge__ghost__voyage-context-3__v1")]
        stats = [_stats("knowledge__vector-search__voyage-context-3__v1", 332)]
        facts = {f.name: f for f in _facts(rows, stats, {"knowledge__vector-search__voyage-context-3__v1": 12})}
        live = facts["knowledge__vector-search__voyage-context-3__v1"]
        assert live.has_stats and live.chunk_count == 332 and live.doc_count == 12 and live.dim == 1024
        ghost = facts["knowledge__ghost__voyage-context-3__v1"]
        assert not ghost.has_stats and ghost.chunk_count == 0 and ghost.doc_count == 0

    def test_stats_without_catalog_row_is_still_examined(self) -> None:
        """A collection T3 knows and the catalog does not is itself a finding
        surface; it must not vanish from the join."""
        facts = _facts([], [_stats("knowledge__orphan__voyage-context-3__v1", 5)], {})
        assert [f.name for f in facts] == ["knowledge__orphan__voyage-context-3__v1"]
        assert facts[0].catalog_row is False


# ── checks, one class per rule ─────────────────────────────────────────────


class TestRule1Subjects:
    @pytest.mark.parametrize("subject", sorted(PLACEHOLDER_SUBJECTS))
    def test_placeholder_subject_is_flagged(self, subject) -> None:
        name = f"knowledge__{subject}__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 100)], {name: 5}), write_model_for=_cloud_write_model)
        hits = _checks(f, "placeholder-subject")
        assert [h.collection for h in hits] == [name]
        assert hits[0].rule == 1 and "subject" in hits[0].action

    @pytest.mark.parametrize("subject", ["2026-09-06", "research-2026-09", "20260906-notes"])
    def test_date_like_subject_is_flagged(self, subject) -> None:
        name = f"knowledge__{subject}__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 100)], {name: 5}), write_model_for=_cloud_write_model)
        assert _checks(f, "placeholder-subject")

    def test_docs_default_corpus_is_flagged(self) -> None:
        name = "docs__default__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 168)], {name: 9}), write_model_for=_cloud_write_model)
        assert _checks(f, "default-corpus")

    def test_real_subject_is_not_flagged(self) -> None:
        name = "knowledge__distributed-systems__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 2101)], {name: 60}), write_model_for=_cloud_write_model)
        assert not _checks(f, "placeholder-subject") and not _checks(f, "default-corpus")

    def test_repo_owned_collections_are_never_subject_checked(self) -> None:
        name = "code__1-1__voyage-code-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 39981)], {name: 900}), write_model_for=_cloud_write_model)
        assert not _checks(f, "placeholder-subject")


class TestRule2Duplicates:
    def test_near_duplicate_subjects_are_paired_once_and_labelled_name_only(self) -> None:
        a = "knowledge__consensus-papers__voyage-context-3__v1"
        b = "knowledge__consensus__voyage-context-3__v1"
        f = run_checks(_facts([_filled(a), _filled(b)], [_stats(a, 50), _stats(b, 40)], {a: 3, b: 2}),
                       write_model_for=_cloud_write_model)
        hits = _checks(f, "duplicate-subject")
        assert len(hits) == 1
        assert {hits[0].collection, hits[0].related} == {a, b}
        assert "name-only" in hits[0].message

    def test_distinct_subjects_are_not_paired(self) -> None:
        a = "knowledge__vector-search__voyage-context-3__v1"
        b = "knowledge__interpretability__voyage-context-3__v1"
        f = run_checks(_facts([_filled(a), _filled(b)], [_stats(a, 50), _stats(b, 40)], {a: 3, b: 2}),
                       write_model_for=_cloud_write_model)
        assert not _checks(f, "duplicate-subject")

    def test_generic_token_alone_does_not_pair(self) -> None:
        """Code review: "search" is a lexical subset of "vector-search" but
        not the same subject; generic tokens are stripped before the
        subset test."""
        a = "knowledge__search__voyage-context-3__v1"
        b = "knowledge__vector-search__voyage-context-3__v1"
        f = run_checks(_facts([_filled(a), _filled(b)], [_stats(a, 50), _stats(b, 40)], {a: 3, b: 2}),
                       write_model_for=_cloud_write_model)
        assert not _checks(f, "duplicate-subject")

    def test_same_subject_under_two_models_is_a_sibling_not_a_duplicate(self) -> None:
        """A model switch mints a sibling on purpose (RDR-103 rationale kept
        by RDR-204); the audit must not call that a duplicate."""
        a = "knowledge__vector-search__voyage-context-3__v1"
        b = "knowledge__vector-search__bge-base-en-v15-768__v1"
        f = run_checks(_facts([_filled(a), _filled(b)], [_stats(a, 50), _stats(b, 40, 768)], {a: 3, b: 2}),
                       write_model_for=_cloud_write_model)
        assert not _checks(f, "duplicate-subject")


class TestRule3ModelToken:
    def test_live_collection_under_a_model_the_install_does_not_write_is_info(self) -> None:
        name = "knowledge__vector-search__bge-base-en-v15-768__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 40, 768)], {name: 2}), write_model_for=_cloud_write_model)
        hits = _checks(f, "model-differs-from-install")
        assert hits and hits[0].severity == "info" and "voyage-context-3" in hits[0].message

    def test_unresolvable_write_model_is_reported_not_raised(self) -> None:
        name = "knowledge__vector-search__voyage-context-3__v1"
        def boom(_ct):
            raise RuntimeError("no key")
        f = run_checks(_facts([_filled(name)], [_stats(name, 40)], {name: 2}), write_model_for=boom)
        assert _checks(f, "write-model-unresolvable")

    def test_ghosts_are_not_model_checked(self) -> None:
        name = "knowledge__ingestgate__minilm-l6-v2-384__v1"
        f = run_checks(_facts([_filled(name)], [], {}), write_model_for=_cloud_write_model)
        assert not _checks(f, "model-differs-from-install")


class TestRule4Size:
    def test_thin_and_one_document(self) -> None:
        name = "knowledge__wow-addon-dev__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 12)], {name: 1}), write_model_for=_cloud_write_model)
        assert _checks(f, "thin-collection") and _checks(f, "one-document")

    def test_below_fanout_floor_is_its_own_finding(self) -> None:
        name = "knowledge__tiny__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, FANOUT_FLOOR - 1)], {name: 1}), write_model_for=_cloud_write_model)
        assert _checks(f, "below-fanout-floor")

    def test_fanout_floor_matches_the_mcp_layer(self) -> None:
        assert FANOUT_FLOOR == _FANOUT_MIN_COLLECTION_CHUNK_COUNT

    def test_thin_floor_is_above_fanout_floor(self) -> None:
        assert THIN_CHUNK_FLOOR > FANOUT_FLOOR


class TestRule5Residue:
    def test_placeholder_that_is_also_residue_is_reported_once(self) -> None:
        name = "knowledge__test__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 10)], {name: 1}), write_model_for=_cloud_write_model)
        assert _checks(f, "placeholder-subject") and not _checks(f, "test-residue")

    @pytest.mark.parametrize("subject", ["shakedown", "smoke-run", "fixture-probe"])
    def test_test_residue_is_flagged(self, subject) -> None:
        name = f"knowledge__{subject}__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 105)], {name: 4}), write_model_for=_cloud_write_model)
        assert _checks(f, "test-residue")


class TestRule6Lifecycle:
    def test_ghost_row(self) -> None:
        name = "code__1-17__voyage-code-3__v1"
        f = run_checks(_facts([_filled(name, legacy_grandfathered=True)], [], {}), write_model_for=_cloud_write_model)
        assert _checks(f, "ghost-row") and _checks(f, "grandfathered-relic")

    def test_two_segment_ghost(self) -> None:
        f = run_checks(_facts([_row("code__myrepo-a1b2c3")], [], {}), write_model_for=_cloud_write_model)
        assert _checks(f, "ghost-row")

    def test_blank_attributes_on_a_live_collection(self) -> None:
        name = "code__1-2__voyage-code-3__v1"
        f = run_checks(_facts([_row(name)], [_stats(name, 45525)], {name: 800}), write_model_for=_cloud_write_model)
        hits = _checks(f, "blank-attributes")
        assert hits and hits[0].severity == "info"

    def test_superseded_but_still_live(self) -> None:
        name = "knowledge__old__voyage-context-3__v1"
        f = run_checks(_facts([_filled(name, superseded_by="knowledge__old__voyage-context-3__v2")],
                              [_stats(name, 10)], {name: 1}), write_model_for=_cloud_write_model)
        assert _checks(f, "superseded-live")

    def test_stats_without_catalog_row(self) -> None:
        f = run_checks(_facts([], [_stats("knowledge__orphan__voyage-context-3__v1", 5)], {}),
                       write_model_for=_cloud_write_model)
        assert _checks(f, "unregistered-collection")

    def test_quarantine_collections_are_skipped(self) -> None:
        name = "quarantine-code__1-1__voyage-code-3__v1"
        f = run_checks(_facts([_filled(name)], [_stats(name, 2)], {}), write_model_for=_cloud_write_model)
        assert f == []


# ── report, non-vacuity, and the rule pin ──────────────────────────────────


class TestReport:
    def test_clean_set_has_zero_findings_and_a_nonzero_examined_count(self) -> None:
        rows = [_filled("code__1-1__voyage-code-3__v1"),
                _filled("docs__1-1__voyage-context-3__v1"),
                _filled("knowledge__distributed-systems__voyage-context-3__v1")]
        stats = [_stats(rows[0]["name"], 39981), _stats(rows[1]["name"], 5538), _stats(rows[2]["name"], 2101)]
        docs = {r["name"]: 100 for r in rows}
        report = AuditReport.build(_facts(rows, stats, docs), write_model_for=_cloud_write_model)
        assert report.findings == []
        assert report.collections_examined == 3, "a zero-finding report must prove it examined something"

    def test_to_dict_is_json_serialisable_and_counts_by_check(self) -> None:
        name = "knowledge__knowledge__voyage-context-3__v1"
        report = AuditReport.build(_facts([_filled(name)], [_stats(name, 1464)], {name: 90}),
                                   write_model_for=_cloud_write_model)
        d = json.loads(json.dumps(report.to_dict()))
        assert d["collections_examined"] == 1
        assert d["by_check"]["placeholder-subject"] == 1
        assert d["findings"][0]["rule"] == 1


class TestRulePin:
    def test_every_rule_heading_has_a_check_and_every_check_names_a_rule(self) -> None:
        text = (_REPO / "docs" / "collections.md").read_text()
        headings = {int(m.group(1)) for m in re.finditer(r"^## Rule (\d+):", text, re.M)}
        assert headings, "docs/collections.md lost its rule headings"
        assert set(CHECKS_BY_RULE) == headings, (
            f"rules in the doc {sorted(headings)} vs rules with checks {sorted(CHECKS_BY_RULE)}")
        assert all(CHECKS_BY_RULE[r] for r in headings), "a rule with no check"

    def test_check_ids_are_unique_across_rules(self) -> None:
        ids = [c for checks in CHECKS_BY_RULE.values() for c in checks]
        assert len(ids) == len(set(ids))


# ── CLI ────────────────────────────────────────────────────────────────────


class TestWriteModelSeam:
    def test_shape_uses_the_read_shaped_resolver(self) -> None:
        """Code review Critical: the write-shaped resolver raises on a keyless
        local install with a voyage-shaped local.embed_model; a read-only
        audit must use the credential-free counterpart."""
        assert _collection_mod._shape_write_model() is resolve_read_embedding_model


class TestCli:
    def _run(self, monkeypatch, rows, stats, docs, args):
        mod = _collection_mod
        t3 = MagicMock(); t3.collection_stats.return_value = stats
        cat = MagicMock(); cat.list_collections.return_value = rows
        cat.collection_doc_counts.return_value = docs
        monkeypatch.setattr(mod, "_t3", lambda: t3)
        monkeypatch.setattr(mod, "_shape_catalog", lambda: cat)
        monkeypatch.setattr(mod, "_shape_write_model", lambda: _cloud_write_model)
        return CliRunner().invoke(mod.collection, ["shape", *args])

    def test_human_output_groups_findings_and_exits_zero(self, monkeypatch) -> None:
        name = "knowledge__knowledge__voyage-context-3__v1"
        res = self._run(monkeypatch, [_filled(name)], [_stats(name, 1464)], {name: 90}, [])
        assert res.exit_code == 0, res.output
        assert "placeholder-subject" in res.output and name in res.output
        assert "examined 1 collection" in res.output and "shape" in res.output

    def test_human_output_caps_each_check_and_names_the_escape_hatch(self, monkeypatch) -> None:
        n = _SHAPE_MAX_LISTED_PER_CHECK + 5
        rows = [_filled(f"code__1-{i}__voyage-code-3__v1") for i in range(n)]  # all ghosts
        res = self._run(monkeypatch, rows, [], {}, [])
        assert res.exit_code == 0, res.output
        assert f"ghost-row ({n})" in res.output
        assert "... and 5 more (--full or --json to list all)" in res.output
        res_full = self._run(monkeypatch, rows, [], {}, ["--full"])
        assert "more (--full" not in res_full.output
        assert res_full.output.count("catalog row with no chunks") == n

    def test_json_output(self, monkeypatch) -> None:
        name = "docs__default__voyage-context-3__v1"
        res = self._run(monkeypatch, [_filled(name)], [_stats(name, 168)], {name: 9}, ["--json"])
        assert res.exit_code == 0, res.output
        d = json.loads(res.output)
        assert d["by_check"]["default-corpus"] == 1

    def test_read_failure_is_loud_not_an_empty_report(self, monkeypatch) -> None:
        mod = _collection_mod
        t3 = MagicMock(); t3.collection_stats.side_effect = RuntimeError("engine down")
        monkeypatch.setattr(mod, "_t3", lambda: t3)
        monkeypatch.setattr(mod, "_shape_catalog", lambda: MagicMock())
        monkeypatch.setattr(mod, "_shape_write_model", lambda: _cloud_write_model)
        res = CliRunner().invoke(mod.collection, ["shape"])
        assert res.exit_code != 0
        assert "engine down" in res.output
