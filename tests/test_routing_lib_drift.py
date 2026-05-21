# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-125 byte-equality guard against ``_lib.py`` drift between plugins.

The routing-hook framework (``nx/hooks/scripts/routing/_lib.py``) is
vendored into each plugin that ships a routing rule. The clean import
path is structurally blocked (RDR-125 § A2): hook scripts run under a
system ``python3.12`` / ``python3.13`` interpreter with no
``conexus`` venv on ``sys.path``. Vendoring is the chosen mechanism;
this test makes the resulting drift risk loud at PR time rather than
silent.

A failure here means either:

1. Someone edited one copy of ``_lib.py`` without updating the other.
   Resolution: copy the canonical version (nx's) over the stale one
   and re-run the test.
2. A new plugin shipped a third copy of ``_lib.py`` without being
   added to this test's coverage. Resolution: extend `_VENDOR_PATHS`
   below.

The framework contract is frozen per RDR-121 § Locked Contracts; the
rate of legitimate ``_lib.py`` changes is low by design.

Enforcement perimeter (RDR-125 § A3): this test runs in the nexus
monorepo CI pipeline against a single working tree. Inside the
monorepo, the guard is symmetric (it catches edits to either copy).
If nx and sn ever split into separate marketplaces or separate CI
pipelines, the guard becomes one-directional and the design needs
revisiting.
"""
from __future__ import annotations

import hashlib
import pathlib

REPO_ROOT = pathlib.Path(__file__).parent.parent

# Files vendored from nx into sibling plugins. Each entry is
# (canonical_path, [vendor_path, vendor_path, ...]). The canonical
# path lives in nx; vendored copies must be byte-identical.
_VENDORED_FILES: tuple[tuple[pathlib.Path, tuple[pathlib.Path, ...]], ...] = (
    (
        REPO_ROOT / "nx" / "hooks" / "scripts" / "routing" / "_lib.py",
        (
            REPO_ROOT / "sn" / "hooks" / "scripts" / "routing" / "_lib.py",
        ),
    ),
    (
        REPO_ROOT / "nx" / "hooks" / "scripts" / "_run_python_hook.sh",
        (
            REPO_ROOT / "sn" / "hooks" / "scripts" / "_run_python_hook.sh",
        ),
    ),
)


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_canonical_files_exist() -> None:
    """Every canonical (nx-side) file in the drift table must exist."""
    missing = [str(c) for c, _ in _VENDORED_FILES if not c.exists()]
    assert not missing, (
        "Canonical files missing from nx side; did the routing-hook "
        f"framework move? Update _VENDORED_FILES.\nMissing: {missing}"
    )


def test_vendored_copies_byte_equal_canonical() -> None:
    """Every vendored copy must be byte-identical to its canonical."""
    failures: list[str] = []
    for canonical, vendors in _VENDORED_FILES:
        canonical_sha = _sha256(canonical)
        for vendor in vendors:
            if not vendor.exists():
                failures.append(
                    f"Vendor copy missing: {vendor} "
                    "(the plugin that ships this copy hasn't been migrated "
                    "yet, or someone deleted it without updating this test)"
                )
                continue
            vendor_sha = _sha256(vendor)
            if vendor_sha != canonical_sha:
                failures.append(
                    f"Drift detected: {vendor} (sha256={vendor_sha[:16]}...) "
                    f"differs from {canonical} (sha256={canonical_sha[:16]}...). "
                    "Resolution: copy the canonical version over the vendored "
                    "one and commit, or update both locations atomically."
                )
    if failures:
        raise AssertionError(
            "Routing-hook framework drift:\n  - " + "\n  - ".join(failures)
        )
