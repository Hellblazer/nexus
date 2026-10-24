# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every place a user chooses an install shows the DEFAULT size, and the default includes the PDF stack.

nexus-2k66t.4.9 (Sam, 2026-10-23: install everything by default). The four sizes below are the
default install, labelled DERIVED: the download figures come from the 2026-10-23 measurement of
7.85.0 (T2 ``nexus/install-weight-measurement-2026-10-23``: the CLI alone 428 MB on macOS and 567 MB
on Linux, a local service about 0.34 GB more), and the disk figures are the 7.81.0 measurement of the
full install. They are pinned in each place so a copy cannot keep quoting the slim figures of
nexus-2k66t.4.2 (0.55 / 0.2 GB down, 1.6 to 2.3 / 1.3 to 1.5 GB on disk) or the 7.81.0 download
figures (1.3 GB, 700 MB).

The web/ copies (install.sh, install.ps1, index.html) are pinned in tests/test_web_install_size_rows.py,
which lands with them: Pages publishes web/ from develop on every push, so they wait for the client
release that accepts ``--bootstrap --extras``. The disk figures are 7.81.0-era upper bounds and Windows
was not measured; every place says so.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: The default install (PDF support included), as the table rows state it.
LOCAL = "about 0.8 GB down (0.9 GB on Linux), 2.4 to 3.1 GB on disk"
CLI = "about 0.45 GB down (0.6 GB on Linux), 2.1 to 2.3 GB on disk"

ROWS = (
    f"| Local | Install the CLI and run a Nexus service on this machine | {LOCAL} |",
    f"| Cloud | Install the CLI and connect it to a managed service you have an address, token and tenant for | {CLI} |",
    f"| CLI only | Install the CLI and decide about a service later | {CLI} |",
)

#: The figures a copy must not keep (the slim ones from the PDF split, and the 7.81.0 ones).
STALE_TABLE = ("about 0.55 GB", "1.6 to 2.3 GB", "1.3 to 1.5 GB", "about 0.2 GB down", "about 1.3 GB", "about 0.7 GB down")

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


#: The words every place that gives sizes carries about where they come from.
PROVENANCE_PAGES = ("derived", "7.81.0-era")


def _text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted(TABLE_FILES))
def test_the_table_shows_the_default_sizes_and_no_stale_ones(path: str) -> None:
    text = _text(path)
    semicolon = TABLE_FILES[path]
    for row in ROWS:
        # README writes "about 0.8 GB down (0.9 GB on Linux); 2.4 to 3.1 GB on disk"
        expected = row.replace("), ", "); ") if semicolon else row
        assert expected in text, f"{path} lacks the row {expected!r}"
    for stale in STALE_TABLE:
        assert stale not in text, f"{path} still quotes {stale!r}"


@pytest.mark.parametrize("path", sorted(set(TABLE_FILES) - {"docs/rdr/rdr-228-plugin-installs-the-cli.md"}))
def test_the_page_says_the_sizes_include_pdf_support_and_are_derived(path: str) -> None:
    text = _text(path)
    assert "include PDF support" in text, path
    assert "derived" in text, f"{path} does not label the figures as derived"
    assert "slim" in text.lower(), f"{path} gives no slim line"
    assert "0.25 GB less" in text or "0.25 GB off" in text, f"{path} does not size the slim saving"
    if path == "conexus/commands/setup.md":  # the SIZES_NOTE twin; the guides word their provenance themselves
        for word in PROVENANCE_PAGES:
            assert word in text, f"{path} does not say where the sizes come from ({word!r})"
        assert "upper bounds" in text, f"{path} does not call the disk sizes upper bounds"


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
        assert "up to about 0.8 GB to download (0.9 GB on Linux), 2.4 to 3.1 GB on disk" in description
        assert "up to about 0.45 GB to download (0.6 GB on Linux), 2.1 to 2.3 GB on disk" in description
        assert "include PDF support" in description and "derived from a measurement of 7.85.0" in description
        assert "upper bounds from a 7.81.0-era measurement" in description
        assert "Windows was not measured" in description
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
