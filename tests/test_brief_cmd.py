# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx brief [TOPIC]``: the CLI face of :mod:`nexus.brief` (SessionStart progressive disclosure).

Design of record: T2 nexus/design-sessionstart-progressive-disclosure-2026-11-20. With no topic the verb prints the
index; a topic prints what the hook used to inject, from the same functions; ``me`` prints the seat's startup note as data.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from nexus import brief
from nexus.cli import main
from nexus.commands.brief import brief_cmd
from nexus.hooks import session_context


def test_no_topic_prints_the_index() -> None:
    result = CliRunner().invoke(brief_cmd, [])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == brief.index_text()
    for topic in brief.TOPICS:
        assert topic in result.output


def test_a_topic_prints_its_text() -> None:
    result = CliRunner().invoke(brief_cmd, ["caps"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == brief.render("caps", source="cli").text


def test_an_unknown_topic_is_a_usage_error_naming_the_choices() -> None:
    result = CliRunner().invoke(brief_cmd, ["nonsense"])
    assert result.exit_code == 2
    for topic in brief.TOPICS:
        assert topic in result.output


def test_a_topic_that_breaks_exits_1_with_the_text_on_stderr(monkeypatch) -> None:
    def boom() -> list[str]:
        raise RuntimeError("cache exploded")

    monkeypatch.setattr(session_context, "_build_capabilities_block", boom)
    result = CliRunner().invoke(brief_cmd, ["caps"])
    assert result.exit_code == 1
    assert "brief caps failed: RuntimeError: cache exploded" in result.output


def test_the_verb_is_registered_on_the_cli_and_records_its_source_as_cli(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    assert "brief" in main.commands
    CliRunner().invoke(main.commands["brief"], ["caps"])
    [line] = (tmp_path / "logs" / "brief.log").read_text(encoding="utf-8").splitlines()
    record = json.loads(line)
    assert record["source"] == "cli" and record["topic"] == "caps" and record["ok"] is True
