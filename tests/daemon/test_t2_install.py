# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-137 followup (nexus-kmvf2): T2 launchd plist KeepAlive policy.

The T2 daemon must persist regardless of exit reason. The original
template used ``KeepAlive={Crashed: true}`` which only restarts on a
NON-zero-exit crash; a clean SIGTERM / shutdown / sleep that
terminates the process group sets ``last exit code = 0`` and launchd
did NOT restart, leaving the daemon down until a demand-spawn outside
launchd supervision. The fix is always-on KeepAlive.

Templates ship under ``src/nexus/_resources/daemon/`` (symlinked to
``conexus/daemon/``).
"""
from __future__ import annotations

from nexus.commands import daemon as daemon_cmd


class TestT2PlistKeepAlive:
    def test_t2_plist_uses_always_on_keepalive(self) -> None:
        """KeepAlive must be the bare ``<true/>`` form, NOT the
        ``{Crashed: true}`` dict that ignored clean exits."""
        body = daemon_cmd._read_template("com.nexus.t2.plist")
        assert "com.nexus.t2" in body
        assert "__NX_BIN__" in body
        assert "RunAtLoad" in body
        assert "<string>t2</string>" in body

        # The KeepAlive key must be immediately followed by a bare
        # <true/> (always-keep-alive), not a <dict> with a Crashed key.
        import re
        # Strip XML comments so the historical rationale comment (which
        # mentions "Crashed") does not false-match.
        no_comments = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
        m = re.search(
            r"<key>KeepAlive</key>\s*(<true/>|<dict>)", no_comments,
        )
        assert m is not None, "KeepAlive key not found in t2 plist"
        assert m.group(1) == "<true/>", (
            "nexus-kmvf2 regression: t2 plist KeepAlive must be the "
            "always-on <true/> form, not a <dict> (which on "
            "{Crashed: true} left the daemon down after a clean exit)."
        )
        # Crashed-only policy must be gone from the active config.
        assert "<key>Crashed</key>" not in no_comments

    def test_t2_plist_bounds_shutdown_with_exit_timeout(self) -> None:
        """ExitTimeOut bounds a stuck graceful shutdown so always-on
        KeepAlive does not loop too aggressively (nexus-kmvf2)."""
        body = daemon_cmd._read_template("com.nexus.t2.plist")
        import re
        no_comments = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
        assert "<key>ExitTimeOut</key>" in no_comments
