# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-bgvnx: the client's copy of the engine's typed-error ``reason``
vocabulary must equal the engine's, and exist once."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.db.engine_reasons import (
    OWNERLESS_CHUNK_WRITE_REASON,
    UNREGISTERED_COLLECTION_REASON,
    error_reason,
)

_HTTP_UTIL = (
    Path(__file__).resolve().parents[1]
    / "service/src/main/java/dev/nexus/service/http/HttpUtil.java"
)


def test_the_client_constant_equals_the_engines() -> None:
    match = re.search(
        r'UNREGISTERED_COLLECTION_REASON\s*=\s*"([a-z_]+)"', _HTTP_UTIL.read_text(),
    )
    assert match, "HttpUtil no longer declares UNREGISTERED_COLLECTION_REASON"
    assert match.group(1) == UNREGISTERED_COLLECTION_REASON


def test_the_ownerless_write_reason_equals_the_engines() -> None:
    match = re.search(
        r'OWNERLESS_CHUNK_WRITE_REASON\s*=\s*"([a-z_]+)"', _HTTP_UTIL.read_text(),
    )
    assert match, "HttpUtil no longer declares OWNERLESS_CHUNK_WRITE_REASON"
    assert match.group(1) == OWNERLESS_CHUNK_WRITE_REASON


def test_the_quarantine_restore_busy_reason_equals_the_engines() -> None:
    from nexus.db.engine_reasons import QUARANTINE_RESTORE_BUSY_REASON  # noqa: PLC0415 — test-local import

    match = re.search(
        r'QUARANTINE_RESTORE_BUSY_REASON\s*=\s*"([a-z_]+)"', _HTTP_UTIL.read_text(),
    )
    assert match, "HttpUtil no longer declares QUARANTINE_RESTORE_BUSY_REASON"
    assert match.group(1) == QUARANTINE_RESTORE_BUSY_REASON


# TODO(RDR-229 P3, engine 0.1.202 on develop): pin COLLECTION_BUSY_REASON and RESTORE_ORIGIN_UNREGISTERED_REASON
# against HttpUtil.java in the list below; develop's HttpUtil does not declare them yet, so a pin now would fail.
@pytest.mark.parametrize(
    "name",
    [
        "COLLECTION_MODEL_MISMATCH_REASON",
        "UNREGISTERED_EMBEDDING_MODEL_REASON",
        "MODEL_PARTITION_MISSING_REASON",
        "TENANT_PARTITION_MISSING_REASON",
        "TENANT_CREATION_BUSY_REASON",
        "COLLECTION_SUPERSEDED_REASON",
        "UNKNOWN_COLLECTION_ID_REASON",
        "COLLECTION_ID_MISMATCH_REASON",
        "RENAME_REFUSED_REASON",
        "RENAME_PATH_MISMATCH_REASON",
        "RENAME_NOT_IN_REGISTRY_REASON",
        "RENAME_BUSY_REASON",
        "COLLECTION_LABEL_AMBIGUOUS_REASON",
        "RESTORE_ORIGIN_UNREGISTERED_REASON",
    ],
)
def test_the_rdr225_partition_reasons_equal_the_engines(name: str) -> None:
    from nexus.db import engine_reasons  # noqa: PLC0415 — test-local import

    match = re.search(rf'{name}\s*=\s*"([a-z_]+)"', _HTTP_UTIL.read_text())
    assert match, f"HttpUtil no longer declares {name}"
    assert match.group(1) == getattr(engine_reasons, name)


def test_every_reader_uses_the_one_module() -> None:
    from nexus import corpus  # noqa: PLC0415 — test-local import
    from nexus.db import http_vector_client as hv  # noqa: PLC0415 — test-local import

    assert hv.UNREGISTERED_COLLECTION_REASON is UNREGISTERED_COLLECTION_REASON
    assert hv.error_reason is error_reason
    src = Path(corpus.__file__).read_text()
    assert '"unregistered_collection"' not in src, "corpus keeps its own copy of the literal"


def test_error_reason_reads_only_a_non_empty_string() -> None:
    assert error_reason({"reason": "x"}) == "x"
    for body in ({}, {"reason": ""}, {"reason": 3}, None, "reason", ["reason"]):
        assert error_reason(body) is None


def test_a_collection_id_mismatch_409_is_not_a_superseded_write() -> None:
    """The engine's 409 ``collection_id_mismatch`` (an id and a label the request itself sent no longer name one collection) is no
    retired collection: the client's typed superseded error must not claim it, so it reaches the caller as the generic 409 it is."""
    from nexus.collection_errors import superseded_write_error_from_body  # noqa: PLC0415 — test-local import
    from nexus.db.engine_reasons import COLLECTION_ID_MISMATCH_REASON  # noqa: PLC0415 — test-local import

    body = {
        "error": "collection_id 7 names 'c', not 'b'",
        "reason": COLLECTION_ID_MISMATCH_REASON,
        "collection_id": 7,
        "requested_label": "b",
        "current_label": "c",
        # the fields a superseded body carries, so the test fails if the reader ever matches on them instead of the reason
        "collection": "b",
        "superseded_by": "c",
    }
    assert superseded_write_error_from_body(409, body) is None
