# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A slim install imports. nexus-2k66t.4.2.

Search and store never use the PDF stack, and the slim install does not ship it.
So no nexus module may need docling, MinerU, torch, torchvision or pdftext at
IMPORT time: a top-level `import torch` anywhere would break `nx` for every
fresh install, and nothing in the dev environment (which has the stack) would
notice. This imports every module under `nexus` in a fresh interpreter whose
meta path hides those five packages.

Non-vacuity: the census must import hundreds of modules and must prove the hider
works (a hidden package really is unimportable), or a pass says nothing.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.lint

_PROBE = textwrap.dedent(
    """
    import importlib, importlib.abc, json, pkgutil, sys

    HIDDEN = {"docling", "mineru", "torch", "torchvision", "pdftext"}

    class _Hide(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in HIDDEN:
                raise ModuleNotFoundError(f"hidden: {name}", name=name)
            return None

    sys.meta_path.insert(0, _Hide())

    try:
        importlib.import_module("torch")
        hider_works = False
    except ModuleNotFoundError:
        hider_works = True

    import nexus

    imported, failed = 0, {}
    for mod in pkgutil.walk_packages(nexus.__path__, "nexus."):
        try:
            importlib.import_module(mod.name)
            imported += 1
        except BaseException as exc:  # noqa: BLE001 - report every module, not the first
            failed[mod.name] = f"{type(exc).__name__}: {str(exc)[:160]}"
    leaked = sorted(m for m in HIDDEN if m in sys.modules)
    print(json.dumps({"hider_works": hider_works, "imported": imported, "failed": failed, "leaked": leaked}))
    """
)

_FLOOR = 250  # nexus has ~370 importable modules; a floor, not a count


def test_every_nexus_module_imports_without_the_pdf_stack() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=False, timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["hider_works"], "the hiding finder did not hide torch: this census proves nothing"
    assert report["imported"] >= _FLOOR, f"only {report['imported']} modules imported: the walk is vacuous"
    assert report["failed"] == {}, (
        "these modules cannot be imported on a slim install (a top-level import of the PDF stack?): "
        f"{report['failed']}"
    )
    assert report["leaked"] == [], f"the stack was imported anyway: {report['leaked']}"
