# SPDX-License-Identifier: AGPL-3.0-or-later
"""Back-compat re-export. The implementation moved to
``nexus.db.pipeline_buffer`` per RDR-112 P0-gate (nexus-yqeu) so its
``sqlite3.connect`` calls live inside ``src/nexus/db/`` per the D3
invariant.

This shim preserves the public import path; new code should import
from ``nexus.db.pipeline_buffer`` directly.
"""
from nexus.db.pipeline_buffer import *  # noqa: F401,F403
from nexus.db.pipeline_buffer import (  # noqa: F401
    PIPELINE_DB_PATH,
    PipelineDB,
)
