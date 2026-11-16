# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 dchash spans: content-stable passage spans by document offsets.

Kept free of any import of ``http_catalog_client`` (that module imports
from here inside a method, and a top-level import back would be a cycle).
"""

from nexus.catalog.dchash.text import (
    DCHASH_PREFIX,
    canonical_text,
    parse_dchash_span,
    text_hash,
)

__all__ = ["DCHASH_PREFIX", "canonical_text", "parse_dchash_span", "text_hash"]
