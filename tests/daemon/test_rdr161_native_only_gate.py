# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-161 inverse-grep gate — the EXPUNGED JVM dual-mode/install path stays gone.

RDR-161 made the cosign-verified native binary the production launch artifact
and removed the auto-fallback JVM dual-mode + JAR install path. The 2026-06-20
amendment then added back a SINGLE, explicit, opt-in JVM launch
(``NEXUS_SERVICE_JAR`` → ``java -jar``) confined to
``daemon/storage_service_daemon.py``.

This gate therefore splits the identifiers:

- ``_FORBIDDEN_EVERYWHERE`` — the expunged auto-fallback / install dual-mode.
  These must never reappear anywhere under ``src/nexus``.
- ``_OPT_IN_LAUNCH`` — the explicit-opt-in launch identifiers. Allowed ONLY in
  the sanctioned ``storage_service_daemon.py``; forbidden elsewhere so the
  java-jar launch cannot creep into other modules.
"""
from __future__ import annotations

import re
from pathlib import Path

# The expunged auto-fallback / install dual-mode — forbidden EVERYWHERE.
_FORBIDDEN_EVERYWHERE = [
    re.compile(r"well_known_jar_path"),  # JAR well-known auto-location (deleted)
    re.compile(r"\binstall_jar\b"),      # JAR install function (deleted)
    re.compile(r"install-jar"),          # JAR install CLI command (deleted)
    re.compile(r"\bjar_path\b"),         # supervisor jar_path param (dropped)
    re.compile(r"\b_launch_mode\b"),     # dual native/jar launch flag (dropped)
    re.compile(r"check_schema_skew"),    # JAR-only schema-skew gate (deleted)
]

# The explicit-opt-in launch identifiers — allowed ONLY in the sanctioned file.
_OPT_IN_LAUNCH = [
    re.compile(r"java\s+-jar"),
    re.compile(r"-jar\b"),
    re.compile(r"_find_service_jar"),
]

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "nexus"
#: The one module that may carry the explicit NEXUS_SERVICE_JAR opt-in.
_SANCTIONED = _SRC_ROOT / "daemon" / "storage_service_daemon.py"


def test_no_jvm_launch_install_identifiers_in_src() -> None:
    """The expunged dual-mode stays gone everywhere; the explicit opt-in launch
    is contained to storage_service_daemon.py."""
    offenders: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        is_sanctioned = py.resolve() == _SANCTIONED.resolve()
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pat in _FORBIDDEN_EVERYWHERE:
                if pat.search(line):
                    offenders.append(f"{py}:{line_no}: {pat.pattern!r} -> {line.strip()}")
            if not is_sanctioned:
                for pat in _OPT_IN_LAUNCH:
                    if pat.search(line):
                        offenders.append(
                            f"{py}:{line_no}: opt-in launch {pat.pattern!r} outside "
                            f"the sanctioned storage_service_daemon.py -> {line.strip()}"
                        )

    assert not offenders, (
        "RDR-161 (+2026-06-20 amendment): the expunged JVM dual-mode/install path "
        "must stay gone everywhere, and the explicit NEXUS_SERVICE_JAR opt-in must "
        "stay confined to storage_service_daemon.py. Offenders:\n" + "\n".join(offenders)
    )
