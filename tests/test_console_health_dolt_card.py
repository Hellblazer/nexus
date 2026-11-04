# SPDX-License-Identifier: AGPL-3.0-or-later
"""The console Health panel's Dolt card is not applicable without Dolt (nexus-pplwc).

Dolt is the beads backend, not a nexus dependency. The card warned "no log" for every user without
``dolt-server.log``, so a user who never installed Dolt saw a yellow card for software nexus does not need.
With no log and no ``dolt`` on PATH the card is neutral and says so; with Dolt installed and no log it still warns.
"""
from __future__ import annotations

from pathlib import Path

import jinja2
import pytest

from nexus.console.routes import health as health_mod

_TEMPLATES = Path(health_mod.__file__).resolve().parents[1] / "templates"


def _render(dolt_server: dict) -> str:
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(_TEMPLATES)), undefined=jinja2.StrictUndefined)
    return env.get_template("health/_dolt_card.html").render(dolt_server=dolt_server, age_str=health_mod._age_str)


def test_no_dolt_and_no_log_is_not_applicable(tmp_path: Path) -> None:
    data = health_mod._collect_dolt_data(tmp_path, dolt_on_path=False)
    assert data == {"exists": False, "applicable": False}
    html = _render(data)
    assert "warn" not in html and "not installed" in html


def test_dolt_installed_without_a_log_still_warns(tmp_path: Path) -> None:
    data = health_mod._collect_dolt_data(tmp_path, dolt_on_path=True)
    assert data == {"exists": False, "applicable": True}
    html = _render(data)
    assert 'health-card warn' in html and "no log" in html


@pytest.mark.parametrize("on_path", [True, False])
def test_a_log_is_ok_whatever_the_path(tmp_path: Path, on_path: bool) -> None:
    (tmp_path / "dolt-server.log").write_text("x", encoding="utf-8")
    data = health_mod._collect_dolt_data(tmp_path, dolt_on_path=on_path)
    assert data["exists"] is True and data["applicable"] is True
    assert 'health-card ok' in _render(data)


def test_the_cards_template_includes_the_dolt_card() -> None:
    assert '{% include "health/_dolt_card.html" %}' in (_TEMPLATES / "health" / "_cards.html").read_text(encoding="utf-8")
