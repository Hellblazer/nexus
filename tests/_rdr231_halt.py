# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR-231 halt as the local rehearsal of 2026-10-29 really left it.

``fixtures/rdr231_halt/`` holds verbatim log text from that rehearsal: the line
the supervisor wrote to its crash log, and the failed boot's stretch of the
engine's native log. :func:`failed_start_message` plays those two files through
the real :func:`nexus.commands.daemon.ensure_storage_supervisor`, so what a test
holds is the text ``nx daemon service start`` really builds for that halt.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

FIXTURES: Path = Path(__file__).parent / "fixtures" / "rdr231_halt"
SUPERVISOR_LINE: str = (FIXTURES / "supervisor_crash_line.txt").read_text(encoding="utf-8").rstrip("\n")
NATIVE_LOG_EXCERPT: str = (FIXTURES / "engine_native_log_failed_boot.txt").read_text(encoding="utf-8")

#: The RAISE's message as the engine's log carries it, without the PostgreSQL driver's
#: ``ERROR: `` prefix. Its start and its runbook URL are what an operator needs.
RAISE_START: str = "RDR-231 note-URI backfill halted and nothing was changed: 1 live chroma:// note row(s)"
RAISE_URL: str = (
    "https://github.com/Hellblazer/nexus/blob/main/docs/migration-runbook.md"
    "#if-the-rdr-231-note-uri-backfill-halts-startup"
)

#: The line ``nx daemon service start`` printed first in the rehearsal (stderr), before the error.
AUTOSTART_PREAMBLE: str = (
    "Not starting through the autostart unit /home/nexus/.config/systemd/user/nexus-service.service "
    "(no_manager: systemctl not found on PATH or at /usr/bin/systemctl, /bin/systemctl); "
    "starting the supervisor directly."
)


class _ExitedSupervisor:
    pid = 2041
    returncode = 2

    def poll(self) -> int:
        return 2


def failed_start_message(config_dir: Path) -> str:
    """``str()`` of the ``StorageServiceStartError`` that ``ensure_storage_supervisor``
    raises when the supervisor exits 2 after writing :data:`SUPERVISOR_LINE` and the
    engine wrote :data:`NATIVE_LOG_EXCERPT`."""
    import nexus.commands.daemon as daemon_mod  # noqa: PLC0415 — keeps this helper importable without the CLI
    from nexus.daemon.storage_service_daemon import StorageServiceStartError  # noqa: PLC0415

    logs = config_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    native_log = logs / "storage_service_native.log"
    crash_log = logs / "storage_service.crash.log"
    # An earlier boot already in the file: the mark must keep it out.
    native_log.write_text(
        "2026-10-29 13:55:52,384 ERROR [main] dev.nexus.service.Main - event=an_earlier_boot_error\n",
        encoding="utf-8",
    )

    def _popen(*_a, **_k):
        with native_log.open("a", encoding="utf-8") as fh:
            fh.write(NATIVE_LOG_EXCERPT)
        with crash_log.open("a", encoding="utf-8") as fh:
            fh.write(SUPERVISOR_LINE + "\n")
        return _ExitedSupervisor()

    with (
        patch.object(daemon_mod, "_resolve_nx_bin", return_value=["nx"]),
        patch.object(daemon_mod, "_popen", side_effect=_popen),
    ):
        try:
            daemon_mod.ensure_storage_supervisor(config_dir)
        except StorageServiceStartError as exc:
            return str(exc)
    raise AssertionError("ensure_storage_supervisor did not raise")
