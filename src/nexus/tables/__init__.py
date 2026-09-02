"""nexus.tables — closed-vocabulary tables as checked state machines and
decision tables (RDR-201).

Load a TOML table with :func:`load_table` (explicit path) or
:func:`load_packaged_table` (``importlib.resources``, for a table shipped
inside a package), then prove its coverage and overlap with
:func:`check_table`. :func:`resolve` (RDR-201 P1.2) evaluates a single row
for a concrete assignment of every declared dimension.
"""

from __future__ import annotations

from nexus.tables.check import (
    BLOCKING_CODES,
    CLOSED_BY_ESCAPE,
    COVERAGE_GAP,
    OVERLAP,
    PRODUCT_BOUND,
    UNKNOWN_LITERAL,
    UNMATCHED_ASSIGNMENT,
    UNUSED_DIMENSION,
    UNPROVABLE_COVERAGE,
    Finding,
    Group,
    ProductTooLargeError,
    check_table,
    dimensions_of,
    exit_code,
    groups_of,
)
from nexus.tables.load import (
    Dimension,
    DuplicateRowIdError,
    FrozenMapping,
    MatchKeysMismatchError,
    MultipleEscapesInGroupError,
    MultipleOutcomesError,
    Row,
    Table,
    TableLoadError,
    UndeclaredDimensionError,
    UnknownLiteralError,
    load_packaged_table,
    load_table,
)
from nexus.tables.resolve import (
    AMBIGUOUS_MATCH,
    NO_MATCH,
    REFUSAL_CODES,
    UNKNOWN_VALUE,
    Resolution,
    resolve,
)

__all__ = [
    "AMBIGUOUS_MATCH",
    "BLOCKING_CODES",
    "CLOSED_BY_ESCAPE",
    "COVERAGE_GAP",
    "Dimension",
    "DuplicateRowIdError",
    "FrozenMapping",
    "Finding",
    "Group",
    "MatchKeysMismatchError",
    "MultipleEscapesInGroupError",
    "MultipleOutcomesError",
    "NO_MATCH",
    "OVERLAP",
    "PRODUCT_BOUND",
    "ProductTooLargeError",
    "REFUSAL_CODES",
    "Resolution",
    "Row",
    "Table",
    "TableLoadError",
    "UNKNOWN_LITERAL",
    "UNKNOWN_VALUE",
    "UNMATCHED_ASSIGNMENT",
    "UNUSED_DIMENSION",
    "UNPROVABLE_COVERAGE",
    "UndeclaredDimensionError",
    "UnknownLiteralError",
    "check_table",
    "dimensions_of",
    "exit_code",
    "groups_of",
    "load_packaged_table",
    "load_table",
    "resolve",
]
