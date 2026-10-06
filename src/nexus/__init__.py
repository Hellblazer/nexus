# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus package root.

On Windows, TLS is verified through the operating system (nexus-f9bgu.52). uv's
standalone CPython verifies with OpenSSL against the Windows ROOT store, and a
fresh Windows holds only ~17 roots, fetching the rest on demand through the OS's
own verifier. So on a new machine every download nx makes (PG bundle, engine,
models) failed ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer
certificate`` until something else happened to prime the store. ``truststore``
routes ``ssl`` verification (urllib, httpx) through CryptoAPI, which fetches
missing roots and honours enterprise roots. Done here, once, because every nx
entry point imports this package. POSIX is untouched.
"""
from __future__ import annotations

import sys


def _use_os_trust_store(platform: str | None = None) -> bool:
    """Route ``ssl`` verification through the OS on Windows. True when it did."""
    if (platform if platform is not None else sys.platform) != "win32":
        return False
    try:
        import truststore  # noqa: PLC0415 -- Windows-only dependency
    except ImportError:  # a dev environment without the dependency: OpenSSL as before
        return False
    truststore.inject_into_ssl()
    return True


_use_os_trust_store()
