# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Non-vacuity guard for the platform-specific tests of the worker suite (tests/CLAUDE.md, the vacuous-gate doctrine).

``platform_only`` tests skip on the other platforms. A suite that runs on a
platform where every such test skipped, or whose platform tests were deleted,
would pass having proved nothing there. This imports every test module of the
package and requires that each of the three platforms has registered tests,
and that the platform running the suite has more than one, none of them
skipped by their mark here.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

from tests.lua_runtime import _support

_PLATFORMS = ("darwin", "linux", "win32")
#: The least a platform must register: its end-of-life tests and its OS-limit tests.
_FLOOR = {"darwin": 3, "linux": 4, "win32": 3}


def _import_all_test_modules() -> None:
    for path in sorted(pathlib.Path(__file__).parent.glob("test_*.py")):
        importlib.import_module(f"tests.lua_runtime.{path.stem}")


def test_every_platform_has_registered_tests_and_this_platform_runs_its_own() -> None:
    _import_all_test_modules()
    for platform in _PLATFORMS:
        registered = _support.PLATFORM_TESTS.get(platform, [])
        assert len(registered) >= _FLOOR[platform], (
            f"{platform}: {len(registered)} platform-only tests registered, floor {_FLOOR[platform]}: {registered}"
        )
    here = next(p for p in _PLATFORMS if sys.platform.startswith(p))
    assert _support.PLATFORM_TESTS[here], f"no platform-only test would run on {here}"
