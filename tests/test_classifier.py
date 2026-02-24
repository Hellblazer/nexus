"""Unit tests for file classification logic."""
from pathlib import Path

import pytest


def test_python_file_classified_as_code():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("main.py")) == ContentClass.CODE


def test_markdown_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("README.md")) == ContentClass.PROSE


def test_yaml_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("config.yaml")) == ContentClass.PROSE


def test_toml_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("pyproject.toml")) == ContentClass.PROSE


def test_json_file_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("package.json")) == ContentClass.PROSE


def test_pdf_file_classified_as_pdf():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("paper.pdf")) == ContentClass.PDF


def test_unknown_extension_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("notes.txt")) == ContentClass.PROSE


def test_no_extension_classified_as_prose():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("Makefile")) == ContentClass.PROSE


def test_code_extensions_set_matches_design():
    """The canonical set must match the design doc exactly."""
    from nexus.classifier import _CODE_EXTENSIONS
    expected = {
        ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
        ".cpp", ".cc", ".c", ".h", ".hpp", ".rb", ".cs", ".sh", ".bash",
        ".kt", ".swift", ".scala", ".r", ".m", ".php",
    }
    assert _CODE_EXTENSIONS == expected


def test_config_code_extensions_override():
    """code_extensions in config adds to the default set."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"code_extensions": [".sql", ".proto"]}
    assert classify_file(Path("schema.sql"), indexing_config=cfg) == ContentClass.CODE
    assert classify_file(Path("api.proto"), indexing_config=cfg) == ContentClass.CODE


def test_config_prose_extensions_override():
    """prose_extensions wins over both defaults and code_extensions."""
    from nexus.classifier import classify_file, ContentClass
    cfg = {"prose_extensions": [".sh"], "code_extensions": [".sql"]}
    assert classify_file(Path("deploy.sh"), indexing_config=cfg) == ContentClass.PROSE
    assert classify_file(Path("query.sql"), indexing_config=cfg) == ContentClass.CODE


def test_case_insensitive_extension():
    from nexus.classifier import classify_file, ContentClass
    assert classify_file(Path("Main.PY")) == ContentClass.CODE
    assert classify_file(Path("Doc.PDF")) == ContentClass.PDF
