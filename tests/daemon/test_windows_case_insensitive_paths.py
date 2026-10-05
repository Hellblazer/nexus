# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows path comparisons in the holder census and the stack matcher ignore case
(RDR-224, nexus-f9bgu.33, code review m7).

NTFS compares paths case-insensitively, and the process table spells a path the
way its launcher did (a shell, an installer and a task definition each choose
their own case). A case-sensitive substring test reads a held generation, or a
running stack, as free: the under-reporting direction both modules name as the
dangerous one. POSIX stays case-sensitive; every test pairs the Windows arm with
the same input on POSIX so a matcher that folds everywhere fails.
"""
from __future__ import annotations

from pathlib import Path

from nexus._install import census_core
from nexus.daemon.binary_lifecycle import well_known_binary_path
from nexus.daemon.service_registry import storage_service_stack_matcher


class TestCensus:
    GEN = "/Users/Sam/AppData/tools/conexus-gen1"

    def _snapshot(self, spelling: str) -> str:
        return f"4242 {spelling}/Scripts/python.exe -m nexus.cli daemon service start\n"

    def test_a_holder_spelt_in_another_case_is_found_on_windows(self) -> None:
        snap = self._snapshot(self.GEN.lower())
        assert census_core.generation_holder_pids(self.GEN, snap, platform="win32") == [4242]

    def test_the_same_spelling_is_a_different_path_on_posix(self) -> None:
        snap = self._snapshot(self.GEN.lower())
        assert census_core.generation_holder_pids(self.GEN, snap, platform="linux") == []

    def test_the_exact_spelling_still_matches_everywhere(self) -> None:
        snap = self._snapshot(self.GEN)
        for plat in ("win32", "linux"):
            assert census_core.generation_holder_pids(self.GEN, snap, platform=plat) == [4242]


class TestStackMatcher:
    CFG = Path("/Users/Sam/Cfg/nexus")

    def _supervisor(self, cfg_spelling: str) -> str:
        return f"python -m nexus.cli daemon service start --foreground --config-dir {cfg_spelling}"

    def test_a_supervisor_whose_config_dir_differs_only_in_case_matches_on_windows(self) -> None:
        match = storage_service_stack_matcher(self.CFG, platform="win32")
        assert match(self._supervisor(str(self.CFG).upper()))
        assert match(self._supervisor(str(self.CFG).lower()))

    def test_it_does_not_match_on_posix(self) -> None:
        match = storage_service_stack_matcher(self.CFG, platform="linux")
        assert not match(self._supervisor(str(self.CFG).upper()))
        assert match(self._supervisor(str(self.CFG)))  # non-vacuity: the exact spelling does

    def test_an_engine_spelt_in_another_case_matches_on_windows(self) -> None:
        engine = str(well_known_binary_path(self.CFG, platform_tag="windows-x64"))
        match = storage_service_stack_matcher(self.CFG, platform_tag="windows-x64", platform="win32")
        assert match(engine.upper() + " -Xmx1g")
        posix = storage_service_stack_matcher(self.CFG, platform_tag="windows-x64", platform="linux")
        assert not posix(engine.upper() + " -Xmx1g")
        assert posix(engine + " -Xmx1g")

    def test_a_different_config_dir_still_does_not_match_on_windows(self) -> None:
        match = storage_service_stack_matcher(self.CFG, platform="win32")
        assert not match(self._supervisor("/Users/Sam/Cfg/nexus-staging"))
        assert not match(self._supervisor("/users/sam/other/nexus"))
