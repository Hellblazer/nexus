# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx uninstall``'s user-level beads PRIME.md teardown (nexus-cnzei.8).

Covers ``_teardown_beads_prime`` directly (dry-run preview, real removal,
never-destroys-user-authored-content, best-effort on an unlink failure)
and the CLI wiring (``uninstall_cmd`` echoes its lines and folds its
warnings into the run). All paths are ``tmp_path``-scoped via monkeypatching
``nexus.beads_prime.user_prime_path`` directly — this file never relies on
the conftest fence alone, since it is asserting the TEARDOWN's own path
resolution, not merely guarding against an accidental real write.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.beads_prime import install, load_template
from nexus.commands.uninstall import _teardown_beads_prime, uninstall_cmd


class TestTeardownBeadsPrimeUnit:
    def test_absent_is_a_no_op(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "nexus.beads_prime.user_prime_path", lambda: tmp_path / "PRIME.md"
        )
        lines, warnings = _teardown_beads_prime(confirm=True)
        assert lines == []
        assert warnings == []

    def test_managed_current_dry_run_previews_without_removing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        install(target)
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        lines, warnings = _teardown_beads_prime(confirm=False)
        assert any("would remove" in line for line in lines)
        assert warnings == []
        assert target.exists()  # dry-run touches nothing

    def test_managed_current_confirm_removes_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        install(target)
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        lines, warnings = _teardown_beads_prime(confirm=True)
        assert any("removed" in line for line in lines)
        assert warnings == []
        assert not target.exists()

    def test_managed_stale_confirm_also_removes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A legitimately-managed OLDER version (marker hash matches its own
        # body, but the packaged template has moved on) must still be
        # removable -- it is still ours, just outdated.
        target = tmp_path / "PRIME.md"
        old_body = "old body\n"
        old_digest = hashlib.sha256(old_body.encode("utf-8")).hexdigest()
        target.write_text(f"<!-- conexus-managed beads PRIME v1 sha256:{old_digest} -->\n{old_body}")
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        lines, warnings = _teardown_beads_prime(confirm=True)
        assert any("removed" in line for line in lines)
        assert warnings == []
        assert not target.exists()

    def test_user_authored_never_touched_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        target.write_text("# my own notes\n")
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        lines, warnings = _teardown_beads_prime(confirm=False)
        assert any("left in place" in line and "user-authored" in line for line in lines)
        assert warnings == []
        assert target.read_text() == "# my own notes\n"

    def test_user_authored_never_touched_confirm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The critical case: --yes must NOT delete a hand-authored file just
        # because it happens to sit at the managed path.
        target = tmp_path / "PRIME.md"
        target.write_text("# my own notes\n")
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        lines, warnings = _teardown_beads_prime(confirm=True)
        assert any("left in place" in line for line in lines)
        assert warnings == []
        assert target.exists()
        assert target.read_text() == "# my own notes\n"

    def test_hand_edited_body_under_intact_marker_never_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same protection as install(): a body edited under an intact
        # marker classifies USER_AUTHORED (hash mismatch) and must survive
        # `nx uninstall --yes` exactly like it survives `nx init`/`nx upgrade`.
        target = tmp_path / "PRIME.md"
        wrong_digest = "0" * 64
        edited = f"<!-- conexus-managed beads PRIME v1 sha256:{wrong_digest} -->\nedited by hand\n"
        target.write_text(edited)
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        lines, warnings = _teardown_beads_prime(confirm=True)
        assert any("left in place" in line for line in lines)
        assert warnings == []
        assert target.read_text() == edited

    def test_unlink_failure_is_a_warning_not_a_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        install(target)
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        def _boom(self):  # noqa: ANN001 — Path.unlink signature
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "unlink", _boom)
        lines, warnings = _teardown_beads_prime(confirm=True)
        assert lines == []
        assert any("could not remove" in w for w in warnings)
        assert target.exists()


class TestUninstallCliWiring:
    """CLI-level: uninstall_cmd echoes the beads-prime teardown's lines and
    surfaces its warnings, without disturbing the managed-only /
    fresh-install "nothing to uninstall" message when beads-prime is the
    only thing at the machine level.
    """

    def _run(self, args: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        # Keep every OTHER branch a genuine no-op so the beads-prime line is
        # the only signal under test.
        monkeypatch.setattr(
            "nexus.commands.uninstall._local_service_present", lambda: False
        )
        # By-value import in uninstall.py (`from nexus.config import
        # get_credential`) -- patch the bound name in ITS namespace, not
        # nexus.config's, or the patch never reaches _teardown_managed.
        monkeypatch.setattr(
            "nexus.commands.uninstall.get_credential", lambda _name: ""
        )
        return CliRunner().invoke(uninstall_cmd, args)

    def test_dry_run_previews_managed_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        install(target)
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        result = self._run([], monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        assert "would remove the conexus-managed file" in result.output
        assert target.exists()

    def test_confirm_removes_managed_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        install(target)
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        result = self._run(["--yes"], monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        assert "removed the conexus-managed file" in result.output
        assert not target.exists()

    def test_confirm_leaves_user_authored_file_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "PRIME.md"
        target.write_text("# hand-written\n")
        monkeypatch.setattr("nexus.beads_prime.user_prime_path", lambda: target)

        result = self._run(["--yes"], monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        assert "left in place" in result.output
        assert target.read_text() == "# hand-written\n"

    def test_absent_does_not_break_nothing_to_uninstall_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "nexus.beads_prime.user_prime_path", lambda: tmp_path / "does-not-exist" / "PRIME.md"
        )
        result = self._run([], monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        assert "Nothing to uninstall" in result.output

    def test_teardown_failure_is_best_effort_and_does_not_abort(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*, confirm: bool):  # noqa: ANN001, ARG001
            raise RuntimeError("boom")

        with patch("nexus.commands.uninstall._teardown_beads_prime", _boom):
            result = self._run([], monkeypatch, tmp_path)
        assert result.exit_code == 0, result.output
        assert "beads PRIME.md check failed" in result.output
