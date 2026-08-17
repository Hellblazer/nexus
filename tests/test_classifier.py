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


# ── Binary asset extensions (WeakAuras2 misclassification, 2026-05-31) ──────────
#
# Game/media repos (WoW addons, Unity projects, etc.) carry binary assets
# tracked in git: textures, audio, fonts, compiled models. These have no
# code/prose extension, so the step-7 fall-through ("everything else → PROSE")
# routed them to voyage-context-3 embedding — 366 binary files in WeakAuras2
# registered as "prose", producing zero usable vectors. Binary media must SKIP.

@pytest.mark.parametrize("filename", [
    "texture.tga",
    "icon.blp",
    "image.png",
    "photo.jpg",
    "photo.jpeg",
    "anim.gif",
    "favicon.ico",
    "bitmap.bmp",
    "render.tiff",
    "sound.ogg",
    "music.mp3",
    "voice.wav",
    "stream.flac",
    "clip.m4a",
    "movie.mp4",
    "clip.webm",
    "font.ttf",
    "font.otf",
    "font.woff",
    "font.woff2",
    "archive.zip",
    "bundle.tar",
    "package.gz",
    "lib.so",
    "app.dll",
    "app.dylib",
    "program.exe",
    "module.wasm",
    "cache.bin",
])
def test_binary_assets_classified_as_skip(filename: str):
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path(filename)) == ContentClass.SKIP, f"{filename} should be SKIP"


def test_binary_skip_is_case_insensitive():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("TEXTURE.TGA")) == ContentClass.SKIP
    assert classify_file(Path("Sound.OGG")) == ContentClass.SKIP


def test_prose_override_wins_over_binary_skip():
    """An operator can still force a binary extension to PROSE if they
    have a genuine reason — prose_extensions wins over the skip list."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"prose_extensions": [".tga"]}
    assert classify_file(Path("texture.tga"), indexing_config=cfg) == ContentClass.PROSE


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


# ── nexus-rqsh1 round 2 (substantive-critic Critical, 2026-08-17):
# looks_like_binary_content's 8192-byte prefix sniff must not
# false-positive on a valid multi-byte UTF-8 character straddling the
# truncation boundary. ──────────────────────────────────────────────


class TestLooksLikeBinaryContent:
    def test_short_valid_utf8_text_is_not_binary(self, tmp_path: Path):
        from nexus.classifier import looks_like_binary_content
        p = tmp_path / "small.md"
        p.write_text("# Hello\n\nOrdinary short prose.\n", encoding="utf-8")
        assert looks_like_binary_content(p) is False

    def test_nul_byte_is_binary(self, tmp_path: Path):
        from nexus.classifier import looks_like_binary_content
        p = tmp_path / "fixture.npz"
        p.write_bytes(b"prefix\x00suffix")
        assert looks_like_binary_content(p) is True

    def test_genuine_binary_over_8192_bytes_is_binary(self, tmp_path: Path):
        """A byte sequence that is invalid UTF-8 well within the first
        8192-byte sample (not near the truncation boundary), padded past
        8192 bytes total, must still be classified as binary — the fix
        for the boundary false-positive must not blind the sniff to
        genuinely binary content."""
        from nexus.classifier import looks_like_binary_content
        p = tmp_path / "fixture.bin"
        # 0xFF is never a valid UTF-8 lead byte -- invalid at position 0.
        p.write_bytes(b"\xff\xfe" * 5000)  # 10000 bytes, well over 8192
        assert looks_like_binary_content(p) is True

    def test_long_valid_utf8_multibyte_char_straddles_sample_boundary_is_text(
        self, tmp_path: Path,
    ):
        """A real prose file whose ONLY non-ASCII character happens to
        land astride the 8192-byte sniff-sample cut must be classified
        as text, not binary. Pre-fix: sample.decode("utf-8") on the
        truncated 8192-byte prefix raises UnicodeDecodeError (the lead
        byte of a 2-byte char with its continuation byte cut off) even
        though the FULL file decodes cleanly -- silently and
        permanently excluding ordinary prose (accented text, em/en
        dashes, curly quotes, CJK, emoji) from indexing.
        """
        from nexus.classifier import looks_like_binary_content
        multibyte = "é".encode("utf-8")
        assert len(multibyte) == 2
        # Position the 2-byte char's first byte at index 8191 so the
        # 8192-byte sniff sample ends mid-character (second byte cut off).
        content = b"a" * 8191 + multibyte + b"b" * 71
        assert len(content) == 8264, "deterministic critic-repro shape"
        assert content[:8192][-1:] == multibyte[:1], (
            "the 8192-byte sample must end on the multibyte char's first byte"
        )
        content.decode("utf-8")  # sanity: the FULL file is valid UTF-8
        p = tmp_path / "prose_with_boundary_char.md"
        p.write_bytes(content)
        assert looks_like_binary_content(p) is False, (
            "a multibyte UTF-8 character straddling the 8192-byte sniff "
            "boundary must not be misclassified as binary content"
        )

    def test_critic_exact_repro_8264_byte_boundary_char(self, tmp_path: Path):
        """The substantive-critic's own repro shape, pinned verbatim:
        an 8264-byte fully-valid-UTF-8 file with a 2-byte character
        landing exactly at the 8192-byte cut."""
        from nexus.classifier import looks_like_binary_content
        content = ("a" * 8191 + "é" + "b" * 71).encode("utf-8")
        assert len(content) == 8264
        p = tmp_path / "critic_repro.md"
        p.write_bytes(content)
        assert looks_like_binary_content(p) is False

    def test_read_failure_is_not_binary(self, tmp_path: Path):
        from nexus.classifier import looks_like_binary_content
        assert looks_like_binary_content(tmp_path / "does_not_exist.md") is False

    def test_exactly_8192_bytes_ending_mid_multibyte_char_is_binary(
        self, tmp_path: Path,
    ):
        """nexus-ih383: when the file is EXACTLY ``sample_bytes`` (8192)
        long, the read returns exactly 8192 bytes -- there are no more
        bytes to defer to. A truncated multibyte sequence at true EOF
        here is genuine corruption, not a sampling artifact, and must
        be classified as binary.

        Pre-fix: ``final = len(sample) < sample_bytes`` is False
        whenever ``len(sample) == sample_bytes`` (True in both the
        "file is larger, sample is a truncated prefix" case AND the
        "file is exactly sample_bytes, sample IS the whole file" case)
        -- so this exactly-8192-byte corrupt file was misclassified as
        text (``final=False`` deferred judgment on a sequence with no
        more bytes coming).
        """
        from nexus.classifier import looks_like_binary_content
        # 0xC3 is the lead byte of a 2-byte UTF-8 sequence (e.g. "é" is
        # 0xC3 0xA9); alone at true EOF it is an incomplete/invalid
        # sequence with no continuation byte anywhere in the file.
        content = b"a" * 8191 + b"\xc3"
        assert len(content) == 8192, "deterministic exactly-sample_bytes shape"
        p = tmp_path / "exactly_8192_corrupt.md"
        p.write_bytes(content)
        assert looks_like_binary_content(p) is True, (
            "an exactly-8192-byte file truncated mid-multibyte-char at "
            "true EOF is genuine corruption and must be classified as "
            "binary, not deferred as if more bytes existed"
        )

    def test_exactly_8192_bytes_valid_utf8_is_text(self, tmp_path: Path):
        """The disambiguation fix must not regress the exactly-8192-byte
        case where the sample IS the whole file AND it decodes cleanly
        (no straddling character, no corruption) -- must stay text."""
        from nexus.classifier import looks_like_binary_content
        content = b"a" * 8190 + "é".encode("utf-8")
        assert len(content) == 8192, "deterministic exactly-sample_bytes shape"
        content.decode("utf-8")  # sanity: fully valid UTF-8
        p = tmp_path / "exactly_8192_valid.md"
        p.write_bytes(content)
        assert looks_like_binary_content(p) is False
