# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration package (RDR-155).

Hosts the Phase-5 Chroma→pgvector ETL machinery. ``chroma_read`` is the
ONLY module in ``src/nexus`` allowed to construct Chroma clients after
the Phase-4a serving retire (locked by
``tests/test_rdr155_p4a_serving_retire.py``).
"""
