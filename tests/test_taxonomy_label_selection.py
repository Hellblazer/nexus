# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-function unit tests for ``_topic_has_auto_label`` (nexus taxonomy
label-pipeline fix, 2026-08-19).

No engine substrate, no T2/T3 — plain dicts in, bool out. Split out of
``tests/test_taxonomy_label_pipeline.py`` (whose module-level autouse
fixture provisions the engine substrate for every test in that file)
specifically so this selection-logic contract can run standalone.

``update_topic_label`` deliberately leaves ``review_status='pending'``
after a successful LLM relabel (GH #241 Item 3 — label and review are
separate axes). With no dedicated "has been labeled" column,
``_topic_has_auto_label`` is the only signal available for excluding
already-relabeled topics from ``get_unreviewed_topics`` re-selection: a
topic's label still equals ``" ".join(top_terms[:3])`` — the c-TF-IDF
auto-label ``taxonomy_compute.compute_discovered_topics`` assigns at
discovery time — until something (LLM label, human rename) overwrites it.
"""
from __future__ import annotations

import json

from nexus.commands import taxonomy_cmd


class TestTopicHasAutoLabel:

    def test_matches_auto_pattern_returns_true(self) -> None:
        topic = {"label": "alpha beta", "terms": json.dumps(["alpha", "beta"])}
        assert taxonomy_cmd._topic_has_auto_label(topic) is True

    def test_human_label_returns_false(self) -> None:
        topic = {"label": "Deep Learning", "terms": json.dumps(["alpha", "beta"])}
        assert taxonomy_cmd._topic_has_auto_label(topic) is False

    def test_missing_terms_fails_open_true(self) -> None:
        topic = {"label": "anything", "terms": None}
        assert taxonomy_cmd._topic_has_auto_label(topic) is True

    def test_empty_terms_list_fails_open_true(self) -> None:
        topic = {"label": "anything", "terms": json.dumps([])}
        assert taxonomy_cmd._topic_has_auto_label(topic) is True

    def test_malformed_terms_json_fails_open_true(self) -> None:
        topic = {"label": "anything", "terms": "{not json"}
        assert taxonomy_cmd._topic_has_auto_label(topic) is True

    def test_uses_only_top_three_terms(self) -> None:
        """Auto-label pattern is the top-3 terms joined by a space
        (``compute_discovered_topics``), not the full terms list."""
        topic = {
            "label": "alpha beta gamma",
            "terms": json.dumps(["alpha", "beta", "gamma", "delta", "epsilon"]),
        }
        assert taxonomy_cmd._topic_has_auto_label(topic) is True

    def test_extra_term_in_label_beyond_top_three_returns_false(self) -> None:
        topic = {
            "label": "alpha beta gamma delta",
            "terms": json.dumps(["alpha", "beta", "gamma", "delta"]),
        }
        assert taxonomy_cmd._topic_has_auto_label(topic) is False

    def test_missing_label_key_fails_open_false_not_matched(self) -> None:
        # No "label" key at all: get("label", "") == "" never equals a
        # non-empty joined-terms string, so this correctly reads as
        # "already labeled" (edge case; real topics always carry a label).
        topic = {"terms": json.dumps(["alpha", "beta"])}
        assert taxonomy_cmd._topic_has_auto_label(topic) is False

    def test_two_term_topic_matches_full_join(self) -> None:
        """Fewer than 3 terms: top_terms[:3] is just the whole list."""
        topic = {"label": "solo", "terms": json.dumps(["solo"])}
        assert taxonomy_cmd._topic_has_auto_label(topic) is True
