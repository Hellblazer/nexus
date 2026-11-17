# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171 dchash spans: content-stable passage spans by document offsets.

Kept free of any import of ``http_catalog_client`` (that module imports
from here inside a method, and a top-level import back would be a cycle).
"""

from nexus.catalog.dchash.text import (
    DCHASH_PREFIX,
    canonical_text,
    check_link_span,
    parse_dchash_span,
    text_hash,
)
from nexus.catalog.dchash.source import (
    STATUSES,
    dchash_source,
    dchash_status,
    read_canonical,
    read_doc,
    resolve_dchash_text,
    span_status,
)

__all__ = [
    "DCHASH_PREFIX",
    "STATUSES",
    "canonical_text",
    "check_link_span",
    "dchash_source",
    "dchash_status",
    "parse_dchash_span",
    "read_canonical",
    "read_doc",
    "resolve_dchash_text",
    "span_status",
    "text_hash",
]
