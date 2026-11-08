# SPDX-License-Identifier: AGPL-3.0-or-later
"""The merge pass's written claims (nexus-bivz3.24, review round 1): what the docs and labels say about the window,
the chunk guard and what stays behind the alias must match what the code does. Each assertion fails if its fix is
reverted."""
from __future__ import annotations

import re
from pathlib import Path

from click.testing import CliRunner

from nexus.catalog import note_uri_merge
from nexus.cli import main
from nexus.commands.catalog_cmds import note_uri_repair as cmd
from tests._changelog import changelog_text

_ROOT = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    raw = changelog_text(_ROOT) if rel == "CHANGELOG.md" else (_ROOT / rel).read_text(encoding="utf-8")
    return re.sub(r"\s+", " ", raw)


_PAGES = ("CHANGELOG.md", "docs/cli-reference.md", "docs/rdr/rdr-231-note-source-uri-scheme.md")


def test_no_page_calls_the_window_one_request_long_unconditionally():
    for rel in _PAGES:
        assert "It is one request long" not in _text(rel), rel
    assert "It is one request long" not in cmd._WINDOW_NOTE


def test_every_page_says_the_twin_is_merged_last_and_who_finishes_an_interrupted_run():
    for rel in _PAGES:
        text = _text(rel)
        assert "twin" in text and "merged last" in text, rel
        assert "repair-note-uris --apply" in text, rel
    assert "merged last" in cmd._WINDOW_NOTE and "repair-note-uris --apply" in cmd._WINDOW_NOTE


def test_help_says_the_same():
    out = CliRunner().invoke(main, ["catalog", "repair-note-uris", "--help"]).stdout
    flat = re.sub(r"\s+", " ", out)
    assert "one-request window" not in flat
    assert "merged last" in flat and "repair-note-uris --apply" in flat


def test_the_rule_reads_the_chunk_count_column_and_the_guard_rereads_it():
    cli = _text("docs/cli-reference.md")
    assert "catalog_documents.chunk_count" in cli and "not a manifest recount" in cli
    assert "never applies to a pair it decides" not in _text("docs/rdr/rdr-231-note-source-uri-scheme.md")
    assert "never applies to a pair it decides" not in re.sub(r"\s+", " ", note_uri_merge.__doc__)
    assert "re-reads" in cli or "reads both rows again" in cli


def test_the_loser_keeps_its_chunks_and_meta_behind_the_alias():
    for rel in ("docs/cli-reference.md", "docs/rdr/rdr-231-note-source-uri-scheme.md"):
        text = _text(rel)
        assert "chunks and meta" in text and "ttl" in text, rel


def test_the_catalog_handler_javadoc_is_not_spliced():
    java = _text("service/src/main/java/dev/nexus/service/http/CatalogHandler.java")
    assert "The response is * {@code tumblers}" not in java
