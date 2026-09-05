"""Guard against GraalVM native-image config JSON carrying `_comment` keys.

nexus-1yqac: GraalVM's reflection/resource config JSON schema is a closed
set of known attributes (``name``, ``allDeclaredConstructors``,
``allDeclaredMethods``, ``allDeclaredFields``, ``methods``, ``fields``, ...).
An unrecognized ``_comment`` key anywhere in a descriptor object makes the
native-image build fail with "Unknown attribute(s) [_comment] in reflection
class descriptor object" — every native build, not just some.

Explanatory prose for these descriptors lives in the sibling
``README.md`` instead (see ``service/src/main/resources/META-INF/native-image/README.md``),
keyed by the class name each note applies to.

This test also asserts every native-image JSON file is valid JSON, so a
future edit that strips a ``_comment`` key incorrectly (e.g. leaves a
trailing comma) fails loud here instead of at native-image build time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

NATIVE_IMAGE_DIR = (
    Path(__file__).resolve().parent.parent
    / "service"
    / "src"
    / "main"
    / "resources"
    / "META-INF"
    / "native-image"
)


def _iter_native_image_json_files() -> list[Path]:
    return sorted(NATIVE_IMAGE_DIR.rglob("*.json"))


def _find_comment_keys(obj: object, path: str = "$") -> list[str]:
    """Recursively collect JSON paths where a `_comment` key is present."""
    found: list[str] = []
    if isinstance(obj, dict):
        if "_comment" in obj:
            found.append(path)
        for key, value in obj.items():
            found.extend(_find_comment_keys(value, f"{path}.{key}"))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(_find_comment_keys(item, f"{path}[{index}]"))
    return found


def test_native_image_dir_has_json_files_to_check() -> None:
    """Non-vacuity: fail loud if the directory or its json files go missing."""
    assert NATIVE_IMAGE_DIR.is_dir(), f"expected directory at {NATIVE_IMAGE_DIR}"
    files = _iter_native_image_json_files()
    assert files, f"expected at least one *.json file under {NATIVE_IMAGE_DIR}"


@pytest.mark.parametrize(
    "json_path",
    _iter_native_image_json_files(),
    ids=lambda p: str(p.relative_to(NATIVE_IMAGE_DIR)),
)
def test_native_image_json_file_parses(json_path: Path) -> None:
    content = json_path.read_text(encoding="utf-8")
    json.loads(content)  # raises json.JSONDecodeError on malformed JSON


@pytest.mark.parametrize(
    "json_path",
    _iter_native_image_json_files(),
    ids=lambda p: str(p.relative_to(NATIVE_IMAGE_DIR)),
)
def test_native_image_json_file_has_no_comment_keys(json_path: Path) -> None:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    hits = _find_comment_keys(data)
    assert not hits, (
        f"{json_path.relative_to(NATIVE_IMAGE_DIR)} carries `_comment` key(s) "
        f"GraalVM's native-image config parser rejects at build time: {hits}. "
        f"Move the explanation to the sibling README.md instead."
    )
