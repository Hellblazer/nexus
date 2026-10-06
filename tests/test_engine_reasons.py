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


@pytest.mark.parametrize(
    "name",
    [
        "COLLECTION_MODEL_MISMATCH_REASON",
        "UNREGISTERED_EMBEDDING_MODEL_REASON",
        "MODEL_PARTITION_MISSING_REASON",
        "TENANT_PARTITION_MISSING_REASON",
        "TENANT_CREATION_BUSY_REASON",
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
