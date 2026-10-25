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
    import importlib, importlib.abc, json, os, pkgutil, sys

    HIDDEN = set(json.loads(os.environ["NX_SLIM_HIDDEN"]))
    STACK = {"docling", "mineru", "torch", "torchvision", "pdftext"}

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
    print(json.dumps({"hider_works": hider_works, "imported": imported, "failed": failed, "leaked": leaked,
                      "hidden": len(HIDDEN)}))
    """
)

_FLOOR = 250  # nexus has ~370 importable modules; a floor, not a count
_HIDDEN_FLOOR = 40  # the pdf extra's lock closure beyond the default closure is ~60 distributions

_STACK_NAMES = ("docling", "mineru", "torch", "torchvision", "pdftext")


def _norm(name: str) -> str:
    import re

    return re.sub(r"[-_.]+", "-", name.lower())


def _closure(packages: dict[str, dict], roots: list[dict]) -> set[str]:
    """Distribution names reachable from *roots* (``{"name", "extra"?}`` entries
    of uv.lock), following the extras each edge asks for."""
    seen: set[tuple[str, str]] = set()
    todo = [(r["name"], tuple(r.get("extra", ()))) for r in roots]
    names: set[str] = set()
    while todo:
        name, extras = todo.pop()
        names.add(name)
        pkg = packages.get(name, {})
        edges = list(pkg.get("dependencies", []))
        for extra in extras:
            edges += pkg.get("optional-dependencies", {}).get(extra, [])
        for edge in edges:
            key = (edge["name"], ",".join(edge.get("extra", ())))
            if key not in seen:
                seen.add(key)
                todo.append((edge["name"], tuple(edge.get("extra", ()))))
    return names


def pdf_only_import_names() -> tuple[set[str], int]:
    """Top-level import names of every distribution the ``pdf`` extra brings and
    the default install does not, read from uv.lock's closures and mapped to
    import names through the installed distributions' metadata. Names a default
    dependency or the stdlib also owns are left visible: hiding ``google`` for a
    pdf-only package would break a slim import that is legitimate.

    Returns ``(names, pdf_only_distribution_count)``."""
    import tomllib
    from importlib.metadata import packages_distributions
    from pathlib import Path

    lock = tomllib.loads((Path(__file__).resolve().parents[1] / "uv.lock").read_text(encoding="utf-8"))
    packages = {p["name"]: p for p in lock["package"]}
    conexus = packages["conexus"]
    slim = _closure(packages, conexus["dependencies"])
    with_pdf = _closure(packages, conexus["optional-dependencies"]["pdf"])
    only = {_norm(n) for n in with_pdf} - {_norm(n) for n in slim}
    owners = packages_distributions()  # top-level import name -> [distribution names]
    hidden: set[str] = set(_STACK_NAMES)
    for top, dists in owners.items():
        normalised = {_norm(d) for d in dists}
        if normalised & only and not normalised & {_norm(n) for n in slim}:
            hidden.add(top)
    hidden -= set(sys.stdlib_module_names)
    return hidden, len(only)


def test_every_nexus_module_imports_without_the_pdf_stack() -> None:
    """Hides the five stack packages AND every import name of the pdf extra's
    lock-closure-only distributions (opencv, scipy-or-whatever MinerU pulls,
    transformers, the layout models ...): a module-scope ``import cv2`` breaks a
    slim install exactly as ``import torch`` does."""
    import os

    hidden, only_dists = pdf_only_import_names()
    assert only_dists >= _HIDDEN_FLOOR, f"only {only_dists} pdf-only distributions found in uv.lock: the closure scan is vacuous"
    assert set(_STACK_NAMES) <= hidden
    assert len(hidden) >= _HIDDEN_FLOOR, f"only {len(hidden)} import names hidden: the mapping is vacuous"
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, check=False, timeout=600,
        env={**os.environ, "NX_SLIM_HIDDEN": json.dumps(sorted(hidden))},
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
    assert report["hidden"] == len(hidden)


def test_the_hidden_set_covers_more_than_the_five_names_and_spares_what_slim_imports() -> None:
    hidden, _ = pdf_only_import_names()
    assert len(hidden) > len(_STACK_NAMES) + 20, sorted(hidden)
    # something the default install legitimately imports must stay visible
    for visible in ("click", "structlog", "numpy", "onnxruntime", "mcp"):
        assert visible not in hidden, visible
