# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every place a user chooses an install shows the DEFAULT size, and the default includes the PDF stack.

nexus-2k66t.4.9 (Sam, 2026-10-23: install everything by default), figures replaced by nexus-2k66t.4.10.
The sizes below are MEASURED on 7.85.0 on macOS (hellmini) and Linux (chas, qwent WSL) on 2026-10-24
(T2 ``nexus/install-size-measurement-2026-10-24``): wheels, Python and the local service's downloads,
and the generation directory on disk. Windows was not measured. They are pinned in each place so a
copy cannot keep quoting the derived figures (2.4 to 3.1 / 2.1 to 2.3 GB on disk, a 0.8 GB slim
saving on disk, a 2.4 GB Linux and 3.6 GB macOS uv cache) or the slim figures of nexus-2k66t.4.2.

The web/ copies (install.sh, install.ps1, index.html) are pinned in tests/test_web_install_size_rows.py,
which lands with them: Pages publishes web/ from develop on every push, so they wait for the client
release that accepts ``--bootstrap --extras``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: The default install (PDF support included), as the table rows state it.
LOCAL = "about 0.8 GB down (0.9 GB on Linux), 2.5 to 2.9 GB on disk"
CLI = "about 0.45 GB down (0.6 GB on Linux), 1.8 to 2.2 GB on disk"

ROWS = (
    f"| Local | Install the CLI and run a Nexus service on this machine | {LOCAL} |",
    f"| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | {CLI} |",
    f"| CLI only | Install the CLI and decide about a service later | {CLI} |",
)

#: The figures a copy must not keep (the slim ones from the PDF split, the 7.81.0 download ones, and
#: the derived disk ones nexus-2k66t.4.10 replaced).
STALE_TABLE = (
    "about 0.55 GB", "1.6 to 2.3 GB", "1.3 to 1.5 GB", "about 0.2 GB down", "about 1.3 GB", "about 0.7 GB down",
    "2.4 to 3.1 GB", "2.1 to 2.3 GB",
)

#: Files that carry the three-row table. README writes the separator as a semicolon.
TABLE_FILES = {
    "README.md": True,
    "docs/getting-started.md": False,
    "docs/windows-install.md": False,
    "docs/desktop-deployment.md": False,
    "conexus/commands/setup.md": False,
    "docs/rdr/rdr-228-plugin-installs-the-cli.md": False,
}

#: Every file the census looked at must be one of these; a sweep that found fewer is a failure.
EXPECTED_CHECKED = len(TABLE_FILES) + 2  # + the stand-in and the real server's twin


#: How every place that gives sizes says where they come from (the date is allowed in prose, not in a tool description).
PROVENANCE = "measured on 7.85.0 on macOS and Linux"

#: Words that described the old figures and must not survive anywhere.
STALE_PROVENANCE = ("derived", "7.81.0-era", "0.8 GB less", "0.8 GB off", "0.8 GB on disk")

#: Pages that carry a uv cache sentence.
CACHE_FILES = ("README.md", "docs/getting-started.md", "docs/windows-install.md", "docs/desktop-deployment.md")


def _text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted(TABLE_FILES))
def test_the_table_shows_the_default_sizes_and_no_stale_ones(path: str) -> None:
    text = _text(path)
    semicolon = TABLE_FILES[path]
    for row in ROWS:
        # README writes "about 0.8 GB down (0.9 GB on Linux); 2.5 to 2.9 GB on disk"
        expected = row.replace("), ", "); ") if semicolon else row
        assert expected in text, f"{path} lacks the row {expected!r}"
    for stale in STALE_TABLE:
        assert stale not in text, f"{path} still quotes {stale!r}"


@pytest.mark.parametrize("path", sorted(set(TABLE_FILES) - {"docs/rdr/rdr-228-plugin-installs-the-cli.md"}))
def test_the_page_says_the_sizes_include_pdf_support_and_are_measured(path: str) -> None:
    text = _text(path)
    assert "include PDF support" in text, path
    assert PROVENANCE in text, f"{path} does not say the figures were measured"
    assert "Windows was not measured" in text, f"{path} does not say Windows was not measured"
    for stale in STALE_PROVENANCE:
        assert stale not in text, f"{path} still says {stale!r}"
    assert "slim" in text.lower(), f"{path} gives no slim line"
    assert "0.25 GB less" in text or "0.25 GB off" in text, f"{path} does not size the slim saving"
    assert "0.85 GB" in text and "1.3 GB" in text, f"{path} does not give the slim saving on disk per OS"


@pytest.mark.parametrize("path", CACHE_FILES)
def test_the_uv_cache_sentence_gives_the_measured_figures(path: str) -> None:
    text = _text(path)
    assert "about 2.1 GB on Linux" in text and "1.8 GB on macOS" in text, f"{path} lacks the measured cache sizes"
    for stale in ("2.4 GB on Linux", "3.6 GB on macOS", "2.4 GB, not 4.4 GB", "2.4 GB in uv's cache"):
        assert stale not in text, f"{path} still quotes {stale!r}"


@pytest.mark.parametrize("path", ["README.md", "docs/getting-started.md"])
def test_the_on_demand_pdf_install_sizes_are_the_measured_ones(path: str) -> None:
    text = _text(path)
    assert "240 MB" in text and "370 MB" in text, path
    assert "0.85 GB on disk on macOS and 1.3 GB on Linux" in text, path


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def test_the_stand_in_and_the_real_server_carry_the_same_sizes_in_the_same_words() -> None:
    from nexus.mcp import setup_tools

    stand_in = _load("nx_standin_size_rows", REPO / "conexus" / "launch" / "nx_standin.py")
    for module in (stand_in, setup_tools):
        description = module.SETUP_DESCRIPTION
        assert "up to about 0.8 GB to download (0.9 GB on Linux), 2.5 to 2.9 GB on disk" in description
        assert "up to about 0.45 GB to download (0.6 GB on Linux), 1.8 to 2.2 GB on disk" in description
        assert "include PDF support" in description and "measured on 7.85.0 on macOS and Linux; Windows was not measured" in description
        for word in ("derived", "7.81.0-era", "upper bounds"):
            assert word not in description, (module.__name__, word)
        assert "0.85 GB less on disk on macOS and 1.3 GB on Linux" in description
        assert "2026-" not in description, "an ISO date in a tool description fails tests/test_mcp_tool_description_lint.py"
        assert "slim true" in description
        for stale in ("0.55 GB", "1.6 to 2.3", "1.3 to 1.5", "about 0.2 GB"):
            assert stale not in description, (module.__name__, stale)
        assert module.SIZES_NOTE == stand_in.SIZES_NOTE and module.SLIM_LINE == stand_in.SLIM_LINE
        assert module.LOCAL_SIZE_UP_TO == stand_in.LOCAL_SIZE_UP_TO and module.CLI_SIZE_UP_TO == stand_in.CLI_SIZE_UP_TO
    for row in ROWS:
        assert row in stand_in.QUESTION_TABLE
    # The broken-CLI repair and the manual route quote the same default.
    assert "0.45 GB" in stand_in.BROKEN_REPAIR and "0.8 GB" in stand_in.BROKEN_REPAIR
    assert "0.2 GB" not in stand_in.BROKEN_REPAIR and "0.55 GB" not in stand_in.BROKEN_REPAIR
    assert "0.8 GB" in setup_tools.LOCAL_SIZE_UP_TO and "0.45 GB" in setup_tools.CLI_SIZE_UP_TO


def test_the_census_looked_at_every_place_it_names() -> None:
    """Non-vacuity: a file that vanished (renamed, moved) must fail this, not shrink the sweep."""
    paths = [*TABLE_FILES, "conexus/launch/nx_standin.py", "src/nexus/mcp/setup_tools.py"]
    missing = [p for p in paths if not (REPO / p).is_file()]
    assert missing == []
    assert len(paths) == EXPECTED_CHECKED


@pytest.mark.parametrize("slim", [True, False])
def test_a_slim_resume_names_the_way_back_to_the_full_install_in_both_copies(slim: bool) -> None:
    """nexus-w8ug5: omitted slim resumes slim, so a slim run's resume text must also say how to
    switch to the full install. The stand-in and the real server word it the same."""
    from nexus.mcp import setup_tools

    stand_in = _load("nx_standin_resume_lines", REPO / "conexus" / "launch" / "nx_standin.py")
    view = {"mode": "local", "autostart": False, "slim": slim}
    ours = setup_tools._choice_lines(view, "failed")
    assert ours == stand_in._choice_lines(view, "failed")
    offers_full = any("slim false" in line for line in ours)
    assert offers_full is slim, ours


def test_the_on_demand_install_notice_gives_the_measured_sizes() -> None:
    """The line `nx` prints before it installs PDF support on first use (nexus-2k66t.4.10)."""
    from nexus.pdf_stack import INSTALL_NOTICE

    assert "about 240 MB (macOS) / 370 MB (Linux) download" in INSTALL_NOTICE
    assert "0.85 GB (macOS) / 1.3 GB (Linux) on disk" in INSTALL_NOTICE
    assert "0.8 GB on disk" not in INSTALL_NOTICE
