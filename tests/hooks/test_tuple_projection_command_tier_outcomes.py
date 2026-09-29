# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The command-tier ledger projections log their outcome (nexus-8he82).

``hooks.json`` wires SubagentStart/SubagentStop to ``subagent-start-tuple`` /
``subagent-stop-tuple`` (:mod:`nexus.hooks.subagent_start_tuple`,
:mod:`nexus.hooks.subagent_stop_tuple`), not to the ``mcp_tool`` pair whose
``_project`` logs each outcome as its own event. Those two ``run()`` functions
used to discard :func:`~nexus.hooks.tuple_ledger_project.project`'s return, so
a live dispatch emitted no ``tuple_projection_*`` event at all.

Cost boundary (nexus-zfxo3): the harness-internal stop (IGNORED /
DROPPED_ORPHAN) is ~95% of SubagentStop events, and ``_emit`` pays a structlog
import (~0.05 s) the first time it logs. Those two outcomes log nothing here;
they were decided before any network or file work and there is nothing to
diagnose.
"""
from __future__ import annotations

import pytest

from nexus.hooks import subagent_start_tuple, subagent_stop_tuple, tuple_ledger_project

_START = {"session_id": "s-ct", "agent_id": "a-ct", "agent_type": "Explore"}


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def emitted(monkeypatch) -> list[tuple]:
    rows: list[tuple] = []
    monkeypatch.setattr(
        tuple_ledger_project, "_emit",
        lambda level, event, **fields: rows.append((level, event, fields)),
    )
    return rows


def _resolvable(monkeypatch) -> None:
    monkeypatch.setattr(
        tuple_ledger_project, "_resolve_endpoint_and_token",
        lambda _dir: ("http://127.0.0.1:1", "tok", False),
    )


@pytest.mark.parametrize(
    ("module", "kind", "payload"),
    [
        (subagent_start_tuple, "start", _START),
        (subagent_stop_tuple, "report", _START),
    ],
)
class TestEachVerbLogsItsOutcome:
    def test_a_write_logs_ok_at_info(self, monkeypatch, emitted, module, kind, payload):
        _resolvable(monkeypatch)
        monkeypatch.setattr(tuple_ledger_project, "_post_via_urllib", lambda *_a, **_kw: None)
        module.run(dict(payload))
        assert emitted == [("info", "tuple_projection_ok", {"verb": kind})]

    def test_a_missing_lease_logs_skipped_at_info(
        self, monkeypatch, emitted, module, kind, payload
    ):
        # Explicit, not ambient: a substrate-backed run has a live endpoint.
        def _unresolvable(_config_dir):
            raise tuple_ledger_project._Skip("no service endpoint resolvable")

        monkeypatch.setattr(tuple_ledger_project, "_resolve_endpoint_and_token", _unresolvable)
        module.run(dict(payload))
        assert emitted == [("info", "tuple_projection_skipped", {"verb": kind})]

    def test_a_refused_post_logs_a_failure_at_warning(
        self, monkeypatch, emitted, module, kind, payload
    ):
        _resolvable(monkeypatch)

        def _refused(*_a, **_kw):
            raise tuple_ledger_project._Skip("engine returned HTTP 503 posting to x")

        monkeypatch.setattr(tuple_ledger_project, "_post_via_urllib", _refused)
        module.run(dict(payload))
        assert emitted == [("warning", "tuple_projection_write_failed", {"verb": kind})]

    def test_the_verb_result_stays_silent_on_every_outcome(
        self, monkeypatch, emitted, module, kind, payload
    ):
        _resolvable(monkeypatch)
        monkeypatch.setattr(tuple_ledger_project, "_post_via_urllib", lambda *_a, **_kw: None)
        result = module.run(dict(payload))
        assert not result.stdout and result.exit_code == 0


class TestTheOrphanPathLogsNothing:
    def test_a_harness_internal_stop_emits_nothing(self, emitted, tmp_path):
        subagent_stop_tuple.run({
            "session_id": "s-orphan", "agent_id": "a-orphan",
            "agent_transcript_path": str(tmp_path / "never-written.jsonl"),
        })
        assert emitted == []

    def test_a_stop_with_no_agent_id_emits_nothing(self, emitted):
        subagent_stop_tuple.run({"session_id": "s-none"})
        assert emitted == []


class TestTheOutcomeVocabulary:
    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (tuple_ledger_project.POSTED, ("info", "tuple_projection_ok")),
            (tuple_ledger_project.DROPPED_ORPHAN, ("info", "tuple_projection_dropped_orphan")),
            (tuple_ledger_project.IGNORED, ("info", "tuple_projection_ignored")),
            (tuple_ledger_project.SKIPPED, ("info", "tuple_projection_skipped")),
            (tuple_ledger_project.FAILED, ("warning", "tuple_projection_write_failed")),
        ],
    )
    def test_every_outcome_has_a_level_and_an_event(self, outcome, expected):
        assert tuple_ledger_project.outcome_event(outcome) == expected
