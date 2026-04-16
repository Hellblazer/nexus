# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator pool — RDR-079 §P2.

The pool owns a dedicated T1 session (``pool-<uuid>.session``) and a set
of long-running ``claude`` worker subprocesses. Operators (extract, rank,
compare, summarize, generate) dispatch work to the pool via the MCP
``operator_*`` tools that land in RDR-079 P3.

Public surface kept narrow — import only what callers outside the
package actually need.
"""
