# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for file classification logic."""
from pathlib import Path

import pytest


def test_python_file_classified_as_code():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("main.py")) == ContentClass.CODE


def test_markdown_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("README.md")) == ContentClass.PROSE


def test_yaml_file_classified_as_skip():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("config.yaml")) == ContentClass.SKIP


def test_toml_file_classified_as_skip():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("pyproject.toml")) == ContentClass.SKIP


def test_json_file_classified_as_skip():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("package.json")) == ContentClass.SKIP


def test_pdf_file_classified_as_pdf():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("paper.pdf")) == ContentClass.PDF


def test_unknown_extension_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("notes.rtf")) == ContentClass.PROSE


def test_data_files_classified_as_skip():
    from nexus.classifier import classify_file, ContentClass
    for ext in (".txt", ".csv", ".tsv", ".dat", ".log"):
        assert classify_file(Path(f"data{ext}")) == ContentClass.SKIP, f"{ext} should be SKIP"


def test_no_extension_classified_as_skip(tmp_path: Path):
    """Extensionless file with no shebang → SKIP."""
    f = tmp_path / "Makefile"
    f.write_bytes(b"all:\n\techo done\n")
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(f) == ContentClass.SKIP


def test_extensionless_with_shebang_classified_as_code(tmp_path: Path):
    """Extensionless file with shebang → CODE."""
    f = tmp_path / "myscript"
    f.write_bytes(b"#!/usr/bin/env python\nprint('hi')\n")
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(f) == ContentClass.CODE


def test_extensionless_shebang_bash(tmp_path: Path):
    """Extensionless bash script → CODE."""
    f = tmp_path / "run"
    f.write_bytes(b"#!/bin/bash\necho hi\n")
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(f) == ContentClass.CODE


def test_code_extensions_derived_from_registry():
    """_CODE_EXTENSIONS contains all LANGUAGE_REGISTRY keys plus GPU shader extensions."""
    from nexus.languages import LANGUAGE_REGISTRY, GPU_SHADER_EXTENSIONS
    from nexus.classifier import _CODE_EXTENSIONS
    expected = frozenset(LANGUAGE_REGISTRY.keys()) | GPU_SHADER_EXTENSIONS
    assert _CODE_EXTENSIONS == expected


@pytest.mark.parametrize("filename", [
    "script.lua", "main.cxx", "build.kts", "app.sc",
])
def test_previously_missing_code_extensions(filename: str):
    """Extensions that were missing from _CODE_EXTENSIONS now classify as CODE."""
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path(filename)) == ContentClass.CODE, f"{filename} should be CODE"


# ── SKIP extension coverage ────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", [
    "settings.xml",
    "data.json",
    "config.yml",
    "config.yaml",
    "pyproject.toml",
    "settings.properties",
    "app.ini",
    "app.cfg",
    "app.conf",
    "build.gradle",
    "index.html",
    "page.htm",
    "styles.css",
    "logo.svg",
    "run.cmd",
    "build.bat",
    "deploy.ps1",
    "uv.lock",
])
def test_skip_extensions(filename: str):
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path(filename)) == ContentClass.SKIP, f"{filename} should be SKIP"


# ── New code extensions ────────────────────────────────────────────────────────

@pytest.mark.parametrize("filename", [
    "api.proto",
    "kernel.cl",
    "shader.comp",
    "color.frag",
    "position.vert",
    "render.metal",
    "lighting.glsl",
    "compute.wgsl",
    "pixel.hlsl",
])
def test_new_code_extensions(filename: str):
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path(filename)) == ContentClass.CODE, f"{filename} should be CODE"


# ── Config overrides ───────────────────────────────────────────────────────────

def test_config_code_extensions_override():
    """code_extensions in config adds to the default set."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"code_extensions": [".sql"]}
    assert classify_file(Path("schema.sql"), indexing_config=cfg) == ContentClass.CODE


def test_config_prose_extensions_override():
    """prose_extensions wins over both defaults and code_extensions."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"prose_extensions": [".sh"], "code_extensions": [".sql"]}
    assert classify_file(Path("deploy.sh"), indexing_config=cfg) == ContentClass.PROSE
    assert classify_file(Path("query.sql"), indexing_config=cfg) == ContentClass.CODE


def test_prose_override_wins_over_skip():
    """prose_extensions config can force a normally-SKIP extension to PROSE."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"prose_extensions": [".json"]}
    assert classify_file(Path("data.json"), indexing_config=cfg) == ContentClass.PROSE


def test_case_insensitive_extension():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("Main.PY")) == ContentClass.CODE
    assert classify_file(Path("Doc.PDF")) == ContentClass.PDF


# ── nexus-haet: minified-bundle skip ─────────────────────────────────────


class TestMinifiedBundleSkip:
    """nexus-haet (2026-05-08 chunk-size audit): minified bundle
    filenames (``htmx.min.js``, ``react.min.css``) are extension-wise
    indexable code but produce ~zero search signal (mangled
    identifiers, no whitespace) and historically generated chunks
    larger than Voyage MAX_DOCUMENT_BYTES. Default classify as SKIP;
    operators opt back in via ``index_minified``.
    """

    def test_min_js_classified_as_skip(self):
        from nexus.classifier import classify_file, ContentClass
        assert (
            classify_file(Path("htmx.min.js"))
            == ContentClass.SKIP
        )

    def test_min_mjs_classified_as_skip(self):
        from nexus.classifier import classify_file, ContentClass
        assert (
            classify_file(Path("vendor.min.mjs"))
            == ContentClass.SKIP
        )

    def test_min_cjs_classified_as_skip(self):
        from nexus.classifier import classify_file, ContentClass
        assert (
            classify_file(Path("lib.min.cjs"))
            == ContentClass.SKIP
        )

    def test_min_css_classified_as_skip(self):
        from nexus.classifier import classify_file, ContentClass
        # .css is already in _SKIP_EXTENSIONS but lock the
        # min-pattern path explicitly so a future .css removal from
        # the skip list (e.g. promoting CSS to prose) doesn't
        # silently re-enable .min.css indexing.
        assert (
            classify_file(Path("react.min.css"))
            == ContentClass.SKIP
        )

    def test_bundle_js_classified_as_skip(self):
        """Webpack / Rollup produce ``vendor.bundle.js``-shape names;
        same ~zero-signal class as min.js.
        """
        from nexus.classifier import classify_file, ContentClass
        assert (
            classify_file(Path("vendor.bundle.js"))
            == ContentClass.SKIP
        )

    def test_non_minified_js_still_classified_as_code(self):
        """Regression guard: a regular ``.js`` file is NOT swept up
        by the minified-pattern check.
        """
        from nexus.classifier import classify_file, ContentClass
        assert classify_file(Path("app.js")) == ContentClass.CODE
        assert classify_file(Path("src/lib/util.js")) == ContentClass.CODE

    def test_index_minified_opt_in(self):
        """``indexing_config["index_minified"] = True`` opts back into
        indexing minified files. The file then routes through normal
        extension-based classification (``.min.js`` -> CODE because
        ``.js`` is in ``_CODE_EXTENSIONS``).
        """
        from nexus.classifier import classify_file, ContentClass
        cfg = {"index_minified": True}
        assert (
            classify_file(Path("htmx.min.js"), indexing_config=cfg)
            == ContentClass.CODE
        )

    def test_case_insensitive_minified_match(self):
        """Operators sometimes have ``HTMX.MIN.JS`` (case-insensitive
        repos / Windows-derived names). Match on lowercase.
        """
        from nexus.classifier import classify_file, ContentClass
        assert (
            classify_file(Path("HTMX.MIN.JS"))
            == ContentClass.SKIP
        )
