# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``install_layout.declared_console_scripts`` against a REAL interpreter.

Every other fixture in the layout suites fakes ``<gen>/bin/python`` with a
shell stub that ignores argv and prints a canned list, so the query string,
the dist argument, the exit-3 lookup-failure path and the empty-declaration
refusal were never executed in the fast loop (review of GH #1487 /
nexus-50hm9). Here ``bin/python`` is a wrapper that execs THIS test run's own
interpreter, whose environment has ``conexus`` installed, so the query runs
for real and the answer is the distribution's actual console scripts.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nexus import install_layout


def _generation_with_real_python(tmp_path: Path) -> Path:
    gen = tmp_path / "gen-real"
    (gen / "bin").mkdir(parents=True)
    wrapper = gen / "bin" / "python"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    wrapper.chmod(0o755)
    return gen


def test_the_query_returns_the_distributions_real_console_scripts(tmp_path: Path) -> None:
    gen = _generation_with_real_python(tmp_path)

    names = install_layout.declared_console_scripts(gen)

    assert {"nx", "nx-mcp", "nx-mcp-catalog", "nx-session-end-launcher"} <= names, names
    assert not (names & install_layout.NEVER_SHIM)


def test_a_distribution_the_interpreter_cannot_find_is_a_refusal_not_an_empty_set(tmp_path: Path) -> None:
    """The shell twin exits 3 and prints NX_LOOKUP_FAILED so that 'declares
    nothing' and 'could not ask' never collapse into one empty answer; the
    Python half must surface that, never return frozenset()."""
    gen = _generation_with_real_python(tmp_path)

    with pytest.raises(install_layout.InstallLayoutError, match="NX_LOOKUP_FAILED"):
        install_layout.declared_console_scripts(gen, dist="no-such-distribution-xyz")


def test_a_generation_without_an_interpreter_is_a_refusal(tmp_path: Path) -> None:
    gen = tmp_path / "gen-empty"
    (gen / "bin").mkdir(parents=True)

    with pytest.raises(install_layout.InstallLayoutError, match="no bin/python"):
        install_layout.declared_console_scripts(gen)


def test_owned_names_are_restricted_to_what_the_generation_built(tmp_path: Path) -> None:
    """Declared but not present in bin/ gets no shim from the writer, so it is
    not owned; a dependency script that IS present is."""
    gen = _generation_with_real_python(tmp_path)
    (gen / "bin" / "nx").write_text("#!/bin/sh\n")
    (gen / "bin" / "mineru").write_text("#!/bin/sh\n")

    owned = install_layout.owned_shim_names(gen)

    assert owned == {"nx", "mineru"}, owned
    assert os.access(gen / "bin" / "python", os.X_OK)
