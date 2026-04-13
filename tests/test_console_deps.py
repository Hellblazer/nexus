# SPDX-License-Identifier: AGPL-3.0-or-later


def test_fastapi_importable():
    import fastapi

    assert fastapi.__version__ >= "0.115"


def test_uvicorn_importable():
    import uvicorn

    assert uvicorn is not None


def test_sse_starlette_importable():
    """sse-starlette is transitive via mcp — verify it's available."""
    import sse_starlette

    assert sse_starlette is not None
