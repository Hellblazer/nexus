# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-161 P3 inverse-grep gate — the JVM launch/install path stays expunged.

Mirrors the exhaustive-surface-audit discipline: rather than a blanket ban on
the substring ``jar`` (which legitimately appears as the ``.jar`` file
extension in the classifier and in deployment-skew comments describing
field-deployed JAR services), this gate forbids the specific *launch/install*
identifiers that would signal the ``java -jar`` path has crept back into the
non-test surface. A re-introduction anywhere under ``src/nexus`` fails here.
"""
from __future__ import annotations

import re
from pathlib import Path

# Identifiers that only exist if the legacy JVM launch/install path is present.
_FORBIDDEN = [
    re.compile(r"java\s+-jar"),          # the actual subprocess launch
    re.compile(r"_find_service_jar"),    # JAR discovery (deleted)
    re.compile(r"well_known_jar_path"),  # JAR well-known location (deleted)
    re.compile(r"\binstall_jar\b"),      # JAR install function (deleted)
    re.compile(r"install-jar"),          # JAR install CLI command (deleted)
    re.compile(r"\bjar_path\b"),         # supervisor jar_path param (dropped)
    re.compile(r"\b_launch_mode\b"),     # dual native/jar launch flag (dropped)
    re.compile(r"check_schema_skew"),    # JAR-only schema-skew gate (deleted)
]

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "nexus"


def test_no_jvm_launch_install_identifiers_in_src() -> None:
    """No non-test module under src/nexus may reference the expunged JVM
    launch/install identifiers."""
    offenders: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            for pat in _FORBIDDEN:
                if pat.search(line):
                    offenders.append(f"{py}:{line_no}: {pat.pattern!r} -> {line.strip()}")

    assert not offenders, (
        "RDR-161: the java -jar launch/install path must stay expunged from the "
        "non-test surface. Re-introduced identifiers:\n" + "\n".join(offenders)
    )
