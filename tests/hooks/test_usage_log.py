# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nexus.hooks.usage_log``: the one rotating JSON-line file behind the 30-day ``nx brief`` review.

``brief_load`` lines (one per ``nx brief`` / MCP ``brief`` call) and ``session_start`` lines (one per SessionStart, the
denominator) share this writer, so their counts are read from one place. Past 1 MB the file moves to ``brief.log.1``.
Two processes that cross the cap together can both rotate, so the worst case is one line lost or duplicated in a usage
count; there is no lock because ``fcntl`` is absent on Windows.
"""
from __future__ import annotations

import json
from pathlib import Path

from nexus.hooks import usage_log


def _lines(config: Path) -> list[dict]:
    path = config / "logs" / "brief.log"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_record_is_one_json_line_with_ts_and_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    usage_log.append("brief_load", topic="caps", ok=True)
    usage_log.append("session_start", session_id="s1", repo="nexus", source="resume")
    first, second = _lines(tmp_path)
    assert first["event"] == "brief_load" and first["topic"] == "caps" and first["ts"].endswith("Z")
    assert second == {"ts": second["ts"], "event": "session_start", "session_id": "s1", "repo": "nexus", "source": "resume"}


def test_a_full_file_rotates_to_dot_one_and_the_new_line_is_recorded(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "logs").mkdir()
    path = tmp_path / "logs" / "brief.log"
    older = path.with_name("brief.log.1")
    older.write_text("old\n", encoding="utf-8")
    full = "x" * (usage_log.MAX_BYTES + 1)
    path.write_text(full, encoding="utf-8")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    usage_log.append("session_start", session_id="s", repo="r", source="startup")
    assert older.read_text(encoding="utf-8") == full
    [line] = _lines(tmp_path)
    assert line["event"] == "session_start"


def test_a_file_at_its_cap_is_appended_not_rotated(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "logs").mkdir()
    path = tmp_path / "logs" / "brief.log"
    path.write_bytes(b"x" * (usage_log.MAX_BYTES - 1) + b"\n")  # bytes: text mode on Windows would write \r\n, one byte over
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    usage_log.append("brief_load", topic="caps")
    assert not path.with_name("brief.log.1").exists()


def test_an_unwritable_log_never_raises(tmp_path: Path, monkeypatch) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(blocker))
    usage_log.append("brief_load", topic="caps")  # no exception
