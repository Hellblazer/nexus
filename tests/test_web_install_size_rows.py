# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The web/ copies of the default install sizes (nexus-2k66t.4.9): install.sh, install.ps1, index.html.

Split from tests/test_install_size_rows.py so it lands WITH the web/ edits. Pages publishes web/ from
develop on every push, and the new installers call ``nx self install --bootstrap --extras pdf``, which
only the next client release accepts, so these files reach develop after that release is on PyPI.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WEB_FILES = ("web/install.sh", "web/install.ps1", "web/index.html")

#: The default install, measured on 7.89.0 on macOS and Linux on 2026-10-29 (nexus-tr1rj.18; T2
#: ``nexus/install-weight-measurement-2026-10-29``). Windows was not measured. Kept equal to
#: tests/test_install_size_rows.py, which pins the same figures in the non-web places.
LOCAL = "about 0.8 GB down (1.0 GB on Linux), 2.6 to 2.9 GB on disk"
CLI = "about 0.5 GB down (0.6 GB on Linux), 1.9 to 2.2 GB on disk"
STALE_TABLE = (
    "about 0.55 GB", "1.6 to 2.3 GB", "1.3 to 1.5 GB", "about 0.2 GB down", "about 1.3 GB", "about 0.7 GB down",
    "2.4 to 3.1 GB", "2.1 to 2.3 GB", "about 0.45 GB", "1.8 to 2.2 GB", "2.5 to 2.9 GB", "0.9 GB on Linux)",
)
STALE_PROVENANCE = ("derived", "7.81.0-era", "upper bound", "0.8 GB less", "2.4 GB on Linux", "3.6 GB on macOS", "2.4 GB, not 4.4 GB", "not 4.5 GB")


def _text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def test_the_web_page_shows_the_default_sizes_the_slim_flag_and_the_macos_floor() -> None:
    text = _text("web/index.html")
    for cell in (f"<td>{LOCAL}</td>", f"<td>{CLI}</td>"):
        assert cell in text, f"web/index.html lacks the cell {cell!r}"
    assert text.count(f"<td>{CLI}</td>") == 2
    for stale in STALE_TABLE + ("~1.3 GB", "~700 MB", "~580 MB", "about 0.7 GB down"):
        assert stale not in text, f"web/index.html still quotes {stale!r}"
    assert "~0.8 GB" in text
    assert "include PDF support" in text
    assert "measured on 7.89.0 on macOS and Linux (2026-10-29); Windows was not measured" in text
    for stale in STALE_PROVENANCE:
        assert stale not in text, f"web/index.html still says {stale!r}"
    assert "about 2.1 GB on Linux and 1.9 GB on macOS" in text and "0.83 GB" in text
    assert "0.27 GB less" in text and "1.0 GB less on disk on macOS and 1.3 GB on Linux" in text
    assert text.count("<code>--slim</code>") >= 3, "the flag lists (macOS/Linux and the Windows script block) must name --slim"
    assert "macOS 14 or later" in text


@pytest.mark.parametrize("path", ["web/install.sh", "web/install.ps1"])
def test_the_installers_state_the_default_size_the_slim_size_and_that_they_are_measured(path: str) -> None:
    text = _text(path)
    assert "about 0.8 GB" in text and "1.0 GB on Linux" in text
    assert "about 0.5 GB on macOS and 0.6 GB on Linux" in text
    assert "--slim" in text and "about 0.2 GB (0.25 GB on Linux) for the" in text and "about 0.55 GB with --local" in text
    assert "measured on 7.89.0 on macOS and Linux (2026-10-29)" in text
    assert "1.9 to 2.2 GB for the command-line tool and 2.6 to 2.9 GB with --local" in " ".join(text.split())
    for stale in STALE_PROVENANCE:
        assert stale not in text, f"{path} still says {stale!r}"
    assert "1.3 GB" not in text and "700 MB" not in text


def test_the_windows_installer_says_windows_was_not_measured_in_its_menu_and_its_help() -> None:
    text = _text("web/install.ps1")
    assert "Windows not measured" in text.split("function Select-Mode")[1].split("function Select-Autostart")[0]
    assert "Windows sizes were not\nmeasured" in text.replace("\r\n", "\n")


def test_the_web_census_looked_at_every_place_it_names() -> None:
    """Non-vacuity: a web file that vanished must fail this, not shrink the sweep."""
    assert [p for p in WEB_FILES if not (REPO / p).is_file()] == []
