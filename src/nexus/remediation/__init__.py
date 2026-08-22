# SPDX-License-Identifier: AGPL-3.0-or-later
"""General-purpose remediation-adjacent infrastructure.

The RDR-182 guided-remediation surface (the ``Playbook`` DSL, the
``forensics``/``remediate`` MCP tools, ``nx remediate``/``nx forensics``,
and their consent gate) was deleted at nexus-lgdel: the chash-rekey
upgrade rung every rendering steered operators toward no longer exists,
and per the legacy-identity-deletion arc there is no replacement.

``sql_lint`` is NOT part of that deleted surface — it is a general
read-only-diagnostics guard consumed directly by
:mod:`nexus.db.diag_connection` and :mod:`nexus.health`, imported as
``nexus.remediation.sql_lint`` (no re-export needed here).
"""
