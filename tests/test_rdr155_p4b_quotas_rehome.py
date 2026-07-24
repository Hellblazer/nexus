# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-rn3wo.2: chroma_quotas.py import-site audit — RDR-155 P4b Phase 0.

Before ``nexus-g37fr`` can delete ``chroma_quotas.py``, every caller that
only needs the generic size/batch ceilings (``SAFE_CHUNK_BYTES`` /
``MAX_QUERY_RESULTS`` / ``QUOTAS``) must be re-pointed at
``nexus.db.limits``. Only the migration read-leg files that construct real
Chroma clients — and therefore need the actual ``QuotaValidator`` /
Chroma-Cloud error hierarchy — may still import from ``chroma_quotas``. This
module locks that set with an inverse-grep scan so a future edit can't
silently reintroduce a ``chroma_quotas`` dependency in a file that's
supposed to be decoupled (or silently drop the constant from a file that
still needs it).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "nexus"

# The only files still permitted to import from chroma_quotas after
# nexus-rn3wo.2: the genuine Chroma migration-read leg. QuotaValidator and
# the Chroma-Cloud error hierarchy die WITH chroma_quotas.py in nexus-g37fr;
# these three files die alongside it (RDR-155 P4b).
# RDR-155 P4b P2: the migration dying set is DELETED; the remainder shrinks
# to the ONE permitted importer — db/t3.py (QuotaValidator) — until both die
# together at P3 with the chromadb dependency.
_CHROMA_QUOTAS_IMPORT_ALLOWED = {
    SRC / "db" / "t3.py",
}

# Only the rehomed CONSTANTS (QUOTAS / SAFE_CHUNK_BYTES) are in scope for
# this bead. ``QuotaValidator`` and the Chroma-Cloud error hierarchy are
# genuinely Chroma-specific and legitimately keep importing from
# chroma_quotas (e.g. db/t3.py's ``QuotaValidator()`` usage) until they die
# WITH the module in nexus-g37fr — that is a different, later bead, not a
# drift this test should flag.
_IMPORT_PATTERN = re.compile(
    r"from\s+nexus\.db\.chroma_quotas\s+import\s+(?:\([^)]*\)|[^\n]*)"
    r"|from\s+nexus\.db\s+import\s+chroma_quotas"
)


def _chroma_quotas_importers() -> dict[Path, list[int]]:
    sites: dict[Path, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if path == SRC / "db" / "chroma_quotas.py":
            continue  # defines the symbols, does not import itself
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _IMPORT_PATTERN.search(line):
                sites.setdefault(path, []).append(lineno)
    return sites


def test_chroma_quotas_has_no_importer_outside_dying_set() -> None:
    sites = _chroma_quotas_importers()
    offenders = {
        str(p.relative_to(REPO_ROOT)): lines
        for p, lines in sites.items()
        if p not in _CHROMA_QUOTAS_IMPORT_ALLOWED
    }
    assert offenders == {}, (
        "chroma_quotas.py has live importers outside its P3 remainder "
        "(db/t3.py's QuotaValidator is the ONLY permitted importer until "
        "both die at P3 with the chromadb dependency) — re-point new "
        f"callers to nexus.db.limits: {offenders}"
    )


def test_dying_set_files_still_import_chroma_quotas() -> None:
    # Sanity (non-vacuity): db/t3.py should still be wired to chroma_quotas
    # (QuotaValidator) — if this flips, either P3 landed (delete this suite
    # with it) or the rehome accidentally touched t3.py early.
    sites = _chroma_quotas_importers()
    for path in _CHROMA_QUOTAS_IMPORT_ALLOWED:
        assert path in sites, (
            f"{path.relative_to(REPO_ROOT)} was expected to still import "
            "chroma_quotas (QuotaValidator; dies WITH the module at P3) "
            "but no import was found"
        )


_REHOMED_FILES = [
    SRC / "aspect_readers.py",
    SRC / "catalog" / "manifest_backfill.py",
    SRC / "catalog" / "orphan_backfill.py",
    SRC / "chunker.py",
    SRC / "commands" / "catalog_cmds" / "doctor.py",
    SRC / "commands" / "collection.py",
    SRC / "commands" / "doctor.py",
    SRC / "db" / "http_vector_client.py",
    SRC / "db" / "t2" / "aspects_etl.py",
    SRC / "db" / "t2" / "catalog_etl.py",
    SRC / "db" / "t2" / "http_telemetry_store.py",
    SRC / "db" / "t2" / "memory_etl.py",
    SRC / "db" / "t2" / "plan_etl.py",
    SRC / "db" / "t2" / "taxonomy_etl.py",
    SRC / "db" / "t2" / "telemetry_etl.py",
    SRC / "db" / "t3_reidentify.py",
    SRC / "db" / "t3.py",
    SRC / "exporter.py",
    SRC / "md_chunker.py",
    SRC / "pdf_chunker.py",
    SRC / "search_engine.py",
]

_LIMITS_IMPORT_PATTERN = re.compile(r"from\s+nexus\.db\.limits\s+import")


def test_all_rehomed_callers_import_from_limits_module() -> None:
    assert len(_REHOMED_FILES) == 21, "the rehomed-file roster itself drifted"
    missing = []
    for path in _REHOMED_FILES:
        assert path.is_file(), f"expected rehomed file missing: {path}"
        text = path.read_text(encoding="utf-8")
        if not _LIMITS_IMPORT_PATTERN.search(text):
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert missing == [], (
        f"these files were expected to import from nexus.db.limits: {missing}"
    )


def test_rehomed_files_no_longer_import_chroma_quotas() -> None:
    sites = _chroma_quotas_importers()
    offenders = [
        str(p.relative_to(REPO_ROOT))
        for p in _REHOMED_FILES
        # db/t3.py is BOTH a rehomed QUOTAS caller and the P3 remainder's
        # sole permitted QuotaValidator importer — exempt here.
        if p in sites and p not in _CHROMA_QUOTAS_IMPORT_ALLOWED
    ]
    assert offenders == [], (
        f"these re-pointed files still import chroma_quotas: {offenders}"
    )


def test_doctor_comment_no_longer_references_chroma_quotas() -> None:
    doctor = SRC / "commands" / "doctor.py"
    text = doctor.read_text(encoding="utf-8")
    assert "chroma_quotas._PAGE" not in text, (
        "commands/doctor.py still has a stale chroma_quotas comment "
        "reference — nexus-rn3wo.2 requires updating it to point at "
        "nexus.db.limits"
    )
