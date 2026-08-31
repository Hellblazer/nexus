# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""How to upgrade THIS box, in ONE place. nexus-utpuw.13.

Every user-facing remediation string that named a uv-tool command became wrong
advice under the generation layout: `uv tool upgrade conexus`,
`uv tool install --reinstall conexus` and `uv tool install conexus==<pin>` do
not touch a generation install, and the middle one actively triggers .7's
accepted risk by rebuilding the uv tree over the shims.

They are NOT simply wrong, which is why this is a function and not a
find-and-replace (contract 12): a box that has not migrated still upgrades
through uv, and .7 leaves boxes in that state until their legacy tree has zero
holders. So the advice follows the layout the box actually has.

.11 built this rule inside health.py and pointed the generation branch at
`scripts/reinstall-tool.sh`. That predates .14. `nx self install` now ships as
the packaged installer, it is what .15's rewired hook actually runs
(version_lockstep_action.py:348), and it needs no checkout -- a generation box
has `nx` on PATH by construction. A reader whose `nx` IS a checkout gets a
refusal that names scripts/reinstall-tool.sh, so that case is self-correcting.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import install_advice, install_layout


def _generation(tools: Path, stamp: str) -> Path:
    gen = tools / f"gen-{stamp}"
    (gen / "bin").mkdir(parents=True)
    (gen / "nexus-install.json").write_text("{}")
    return gen


@pytest.fixture
def unmigrated(tmp_path, monkeypatch):
    """A tools root with nothing installed: no pointer at all."""
    tools = tmp_path / "tools"
    tools.mkdir()
    monkeypatch.setenv(install_layout.TOOLS_DIR_ENV, str(tools))
    return tools


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    """A box with a working generation layout."""
    tools = tmp_path / "tools"
    tools.mkdir()
    gen = _generation(tools, "20260826T010000Z")
    (tools / "current").symlink_to(gen)
    monkeypatch.setenv(install_layout.TOOLS_DIR_ENV, str(tools))
    return tools


class TestUpgradeCommand:
    def test_a_generation_box_is_told_to_use_the_packaged_installer(
        self, migrated
    ) -> None:
        assert install_advice.upgrade_command("uv tool upgrade conexus") == (
            "nx self install"
        )

    def test_an_unmigrated_box_keeps_the_uv_form_verbatim(self, unmigrated) -> None:
        """The half a find-and-replace gets wrong. Returned VERBATIM, not
        merely uv-shaped: each caller's fallback carries its own pin or flag
        and the helper must not paraphrase it."""
        for legacy in (
            "uv tool upgrade conexus",
            "uv tool install --reinstall conexus",
        ):
            assert install_advice.upgrade_command(legacy) == legacy

    def test_a_dangling_pointer_is_not_a_working_layout(
        self, migrated
    ) -> None:
        """current present but its target reaped: nothing resolves, so the
        generation advice would send the reader at a hole."""
        import shutil

        shutil.rmtree(install_layout.current_generation())
        assert install_advice.upgrade_command("uv tool upgrade conexus") == (
            "uv tool upgrade conexus"
        )

    def test_an_unreadable_layout_falls_back_rather_than_raising(
        self, monkeypatch, unmigrated
    ) -> None:
        def _boom(*a, **kw):
            raise RuntimeError("layout unreadable")

        monkeypatch.setattr(install_layout, "current_generation", _boom)
        assert install_advice.upgrade_command("uv tool upgrade conexus") == (
            "uv tool upgrade conexus"
        )


class TestUpgradeAdvice:
    def test_note_is_appended_on_a_generation_box(self, migrated) -> None:
        """The note carries information the command does not (which version is
        available). .11 dropped it on the generation branch by returning a
        fixed string; a caller should not have to choose between correct
        advice and the reason for it."""
        advice = install_advice.upgrade_advice(
            "uv tool upgrade conexus", note="→ 6.20.0"
        )
        assert advice.startswith("nx self install")
        assert "→ 6.20.0" in advice
        assert "uv tool" not in advice

    def test_note_is_appended_on_an_unmigrated_box(self, unmigrated) -> None:
        advice = install_advice.upgrade_advice(
            "uv tool upgrade conexus", note="→ 6.20.0"
        )
        assert advice.startswith("uv tool upgrade conexus")
        assert "→ 6.20.0" in advice

    def test_no_note_means_no_trailing_comment(self, migrated) -> None:
        """doctor.py used to strip the note with .split("    #")[0].strip().
        A caller that wants the bare command asks for the bare command."""
        assert install_advice.upgrade_advice("uv tool upgrade conexus") == (
            "nx self install"
        )
        assert "#" not in install_advice.upgrade_advice("uv tool upgrade conexus")


class TestPinnedInstall:
    def test_the_two_hop_pin_is_expressible_in_generation_vocabulary(
        self, migrated
    ) -> None:
        """The stranded-install recovery is a REAL documented procedure: pin
        to the last migration-capable release, run the ladder, upgrade back.
        The first hop is a DOWNGRADE to a named version, so it cannot become
        a bare `nx self install` -- that would silently drop the pin and
        install the newest release, which is precisely the hop the procedure
        exists to avoid."""
        assert install_advice.pinned_install_command(
            "6.18.1", legacy="uv tool install conexus==6.18.1"
        ) == "nx self install --version 6.18.1"

    def test_the_pin_survives_on_an_unmigrated_box(self, unmigrated) -> None:
        assert install_advice.pinned_install_command(
            "6.18.1", legacy="uv tool install conexus==6.18.1"
        ) == "uv tool install conexus==6.18.1"

    def test_the_pinned_form_is_never_the_bare_installer(self, migrated) -> None:
        """Mutation guard: a helper that ignored the pin would still look
        plausible and would still be layout-aware."""
        out = install_advice.pinned_install_command(
            "6.18.1", legacy="uv tool install conexus==6.18.1"
        )
        assert out != "nx self install"


class TestLocalExtraAdvice:
    """nexus-hbgso, updated at nexus-pffc4: the generation branch names the
    REAL command now — `nx self install --extras local` (merge-with-receipt
    rebuild) exists, so the honest-limitation sentence this class used to
    pin is retired. The legacy branch is unchanged: on an unmigrated uv box
    the uv form is still the correct answer."""

    def test_legacy_box_gets_the_reinstall_form(self, unmigrated) -> None:
        assert install_advice.local_extra_advice() == [
            'uv tool install --reinstall "conexus[local]"',
        ]

    def test_legacy_box_honors_a_custom_legacy_string(self, unmigrated) -> None:
        assert install_advice.local_extra_advice(legacy="custom-cmd") == [
            "custom-cmd",
        ]

    def test_generation_box_names_the_extras_command(
        self, migrated,
    ) -> None:
        out = install_advice.local_extra_advice()
        assert len(out) == 1
        joined = out[0]
        assert joined.startswith("nx self install --extras local"), joined
        # the dead ends nexus-hbgso banned stay banned:
        assert "nx init" not in joined
        assert "pip install" not in joined
        assert "uv tool" not in joined
