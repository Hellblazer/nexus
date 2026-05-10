# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vwu1 (GH #621): on Windows, the CLI's stdout/stderr must be
configured to UTF-8 with ``errors="replace"`` so non-ASCII status
glyphs (checkmarks, crosses, ellipses, em-dashes) don't crash a
default cp1252 console with UnicodeEncodeError.

POSIX hosts are unaffected; this only matters on ``sys.platform == 'win32'``.
"""
from __future__ import annotations

import io
import sys
from unittest.mock import patch


class TestWindowsStreamReconfigure:
    """The stream-reconfigure block at module import time runs once
    per process. The contract under test is the BRANCH it takes when
    ``sys.platform == 'win32'``: UTF-8 + replace + line-buffered.
    Importing the module a second time after patching ``sys.platform``
    is the cleanest way to exercise the branch without spawning a
    Windows subprocess.
    """

    def test_windows_branch_calls_reconfigure_with_utf8_replace(self):
        """Stub stdout/stderr with a recording reconfigure(); patch
        sys.platform to 'win32'; reload nexus.cli; assert the
        reconfigure was called with the UTF-8 + replace + line-
        buffered shape. Reverting the Windows-specific kwargs makes
        this fail because reconfigure receives only line_buffering.
        """
        recorded: list[dict] = []

        class _RecordingStream(io.TextIOBase):
            def reconfigure(self, **kwargs) -> None:  # type: ignore[override]
                recorded.append(kwargs)

        fake_out = _RecordingStream()
        fake_err = _RecordingStream()

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "stdout", fake_out),
            patch.object(sys, "stderr", fake_err),
        ):
            # Force a fresh import so module-top-level code re-runs
            # against the patched sys attributes.
            sys.modules.pop("nexus.cli", None)
            import nexus.cli  # noqa: F401

        assert len(recorded) == 2, (
            f"expected two reconfigure calls (stdout + stderr), "
            f"got {len(recorded)}: {recorded!r}"
        )
        for call in recorded:
            assert call.get("encoding") == "utf-8", (
                f"Windows branch must force UTF-8; got {call!r}"
            )
            assert call.get("errors") == "replace", (
                f"Windows branch must use errors='replace' so unprintable "
                f"glyphs degrade gracefully; got {call!r}"
            )
            assert call.get("line_buffering") is True, (
                f"line-buffering preserved across the Windows branch "
                f"(Issue #370 contract); got {call!r}"
            )

    def test_posix_branch_calls_reconfigure_with_only_line_buffering(self):
        """The POSIX branch is line-buffering only; touching encoding
        on macOS/Linux would risk overriding the user's locale
        (LC_ALL=C scenarios) for no benefit (UTF-8 is already the
        default on every supported POSIX host). Reverting the
        platform split would either drop the encoding kwargs on
        Windows OR add them on POSIX; both fail this test.
        """
        recorded: list[dict] = []

        class _RecordingStream(io.TextIOBase):
            def reconfigure(self, **kwargs) -> None:  # type: ignore[override]
                recorded.append(kwargs)

        fake_out = _RecordingStream()
        fake_err = _RecordingStream()

        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys, "stdout", fake_out),
            patch.object(sys, "stderr", fake_err),
        ):
            sys.modules.pop("nexus.cli", None)
            import nexus.cli  # noqa: F401

        assert len(recorded) == 2
        for call in recorded:
            assert call == {"line_buffering": True}, (
                f"POSIX branch must call reconfigure with line_buffering "
                f"only; got {call!r}"
            )

    def test_oserror_on_reconfigure_does_not_propagate(self):
        """pytest's ``capsys`` and similar fixtures replace stdout
        with non-reconfigurable streams. The CLI must keep importing
        cleanly when reconfigure raises OSError or AttributeError;
        otherwise every test that imports nexus.cli would fail
        whenever pytest captures output.
        """

        class _BrokenStream:
            def reconfigure(self, **kwargs) -> None:
                raise OSError("captured stream cannot reconfigure")

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "stdout", _BrokenStream()),
            patch.object(sys, "stderr", _BrokenStream()),
        ):
            sys.modules.pop("nexus.cli", None)
            # The import below would raise if the OSError escaped
            # the try/except.
            import nexus.cli  # noqa: F401
