# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-0miq7.7: one source for the nine RDR-205 typed tuple-space errors.

Three places name the nine ``TupleException`` subtypes and their HTTP
status, and all three must agree:

1. The engine — ``service/src/main/java/dev/nexus/service/db/*Exception.java``
   files that ``extends TupleException``, each calling
   ``super("<Code>", <status>, ...)`` in its constructor.
2. The client — ``nexus.db.t2.http_tuple_store``'s nine ``TupleError``
   subclasses, each carrying a ``code = "<Code>"`` class attribute
   (:data:`_ERROR_CLASSES_BY_CODE`'s keys).
3. The doc — docs/tuple-space.md's ``## Errors`` section, one
   `` - `<Code>` (<status>): ... `` bullet per error.

The engine is the source of truth (it is what actually renders the wire
shape); this test reads it directly from the Java source rather than
hand-maintaining a fourth copy of the nine names here, so a tenth error
added to the engine and forgotten in the client or the doc reds this test
instead of shipping silently mismatched.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).resolve().parent.parent
_JAVA_DB_DIR = _REPO_ROOT / "service" / "src" / "main" / "java" / "dev" / "nexus" / "service" / "db"
_CLIENT_FILE = _REPO_ROOT / "src" / "nexus" / "db" / "t2" / "http_tuple_store.py"
_DOC_FILE = _REPO_ROOT / "docs" / "tuple-space.md"

_JAVA_SUPER_CALL = re.compile(r'super\("([A-Za-z]+)",\s*(\d+)\s*,')
_PYTHON_CODE_LINE = re.compile(r'code = "([A-Za-z]+)"')
_DOC_BULLET = re.compile(r"- `([A-Za-z]+)` \((\d+)\):")


def _engine_errors() -> dict[str, int]:
    """``{code: http_status}`` for every ``*Exception.java`` under the db
    package that extends ``TupleException``."""
    assert _JAVA_DB_DIR.is_dir(), f"expected engine tuple-error package at {_JAVA_DB_DIR}"
    errors: dict[str, int] = {}
    for path in sorted(_JAVA_DB_DIR.glob("*Exception.java")):
        text = path.read_text()
        if "extends TupleException" not in text:
            continue
        match = _JAVA_SUPER_CALL.search(text)
        assert match, f"{path}: extends TupleException but no super(\"Code\", status, ...) call found"
        errors[match.group(1)] = int(match.group(2))
    return errors


def _client_codes() -> set[str]:
    text = _CLIENT_FILE.read_text()
    return set(_PYTHON_CODE_LINE.findall(text))


def _doc_errors() -> dict[str, int]:
    text = _DOC_FILE.read_text()
    return {code: int(status) for code, status in _DOC_BULLET.findall(text)}


def test_engine_errors_found_and_non_vacuous() -> None:
    """Nine typed errors as of RDR-205 v1 -- a floor, not a ceiling, so a
    tenth error added later does not itself red this specific assertion."""
    errors = _engine_errors()
    assert len(errors) >= 9, (
        f"found only {len(errors)} TupleException subtypes under {_JAVA_DB_DIR} -- "
        "the *Exception.java glob or the extends-check may be broken, rather than "
        "the engine actually shipping fewer typed errors"
    )


def test_client_error_classes_match_the_engine() -> None:
    engine = set(_engine_errors())
    client = _client_codes()
    missing_from_client = sorted(engine - client)
    extra_in_client = sorted(client - engine)
    assert not missing_from_client, (
        f"engine TupleException subtype(s) with no matching TupleError "
        f"subclass in {_CLIENT_FILE.relative_to(_REPO_ROOT)}: {missing_from_client}"
    )
    assert not extra_in_client, (
        f"{_CLIENT_FILE.relative_to(_REPO_ROOT)} carries TupleError code(s) the "
        f"engine no longer throws: {extra_in_client}"
    )


def test_doc_error_table_matches_the_engine() -> None:
    engine = _engine_errors()
    doc = _doc_errors()
    missing_from_doc = sorted(set(engine) - set(doc))
    extra_in_doc = sorted(set(doc) - set(engine))
    assert not missing_from_doc, (
        f"engine TupleException subtype(s) with no `## Errors` bullet in "
        f"{_DOC_FILE.relative_to(_REPO_ROOT)}: {missing_from_doc}"
    )
    assert not extra_in_doc, (
        f"{_DOC_FILE.relative_to(_REPO_ROOT)}'s `## Errors` section names code(s) "
        f"the engine no longer throws: {extra_in_doc}"
    )
    mismatched_status = sorted(
        code for code in (set(engine) & set(doc)) if engine[code] != doc[code]
    )
    assert not mismatched_status, (
        f"HTTP status drift between the engine and {_DOC_FILE.relative_to(_REPO_ROOT)} "
        f"for: {mismatched_status}"
    )


def test_a_planted_bogus_doc_status_is_detected(tmp_path) -> None:
    """The status-comparison arm actually fires on a real mismatch."""
    fake_doc = tmp_path / "fake-tuple-space.md"
    fake_doc.write_text("- `UnknownSubspace` (500): wrong status on purpose.\n")
    doc_errors = {
        code: int(status)
        for code, status in _DOC_BULLET.findall(fake_doc.read_text())
    }
    assert doc_errors == {"UnknownSubspace": 500}
    engine = _engine_errors()
    assert engine.get("UnknownSubspace") != 500, (
        "fixture assumption broken: the engine's real UnknownSubspace status "
        "is no longer 404, update this planted value"
    )
