# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit test (no real PG) for heal_diag_view_grants_and_ownership's os_user
identifier quoting (code-review LOW, nexus-cfgo9).

The real, PG-backed integration coverage for this function lives in
tests/db/test_pg_provision.py::TestHealDiagViewGrantsAndOwnership. This file
covers ONE narrow thing those integration tests cannot cheaply exercise: an
os_user value containing a literal double-quote must not break out of the
``ALTER VIEW ... OWNER TO "..."`` quoted identifier. os_user is OS-controlled
(the box's own username), not attacker input, so this is defense-in-depth,
not a real vulnerability under the threat model -- hence a mocked unit test
rather than provisioning a real cluster with a deliberately-quoted role name.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from nexus.db.pg_provision import heal_diag_view_grants_and_ownership


def _fake_psql_tuples(bins, port, db, user, sql):  # noqa: ANN001, ANN202 — test double
    if "SELECT 1 FROM pg_class" in sql:
        return "1"  # the view exists
    if "SELECT r.rolname" in sql:
        return "nexus_admin|f"  # owned by a non-RLS-exempt role
    if "has_table_privilege" in sql:
        return "t"  # grant already present -- isolate the ownership branch
    raise AssertionError(f"unexpected _psql_tuples call: {sql!r}")


def _fake_psql(bins, port, db, user, sql):  # noqa: ANN001, ANN202 — test double
    if "SELECT 1 FROM pg_roles" in sql:
        return MagicMock(stdout="rolname\n----\nnexus_diag\n(1 row)\n")
    return MagicMock(stdout="")


def test_os_user_with_embedded_quote_is_escaped_in_owner_to():
    """A belt-and-suspenders escape: an os_user containing a literal ``"``
    must not break out of the ALTER VIEW ... OWNER TO "..." quoted
    identifier -- the embedded quote is doubled, the standard Postgres
    quoted-identifier escape."""
    calls: list[str] = []

    def recording_psql(bins, port, db, user, sql):  # noqa: ANN001, ANN202 — test double
        calls.append(sql)
        return _fake_psql(bins, port, db, user, sql)

    with patch(
        "nexus.db.pg_provision._psql_tuples", side_effect=_fake_psql_tuples,
    ), patch(
        "nexus.db.pg_provision._psql", side_effect=recording_psql,
    ):
        actions = heal_diag_view_grants_and_ownership(
            MagicMock(), 5432, 'weird"user',
        )

    owner_stmts = [c for c in calls if "OWNER TO" in c]
    assert len(owner_stmts) == 1
    assert 'OWNER TO "weird""user"' in owner_stmts[0]
    assert any("ownership fragmentation" in a for a in actions)


def test_os_user_without_quote_is_unaffected():
    """Control case: a normal os_user (e.g. an OS account with a dot, like
    'hal.hildebrand') is passed through unchanged -- the escape is a no-op
    when there is nothing to escape."""
    calls: list[str] = []

    def recording_psql(bins, port, db, user, sql):  # noqa: ANN001, ANN202 — test double
        calls.append(sql)
        return _fake_psql(bins, port, db, user, sql)

    with patch(
        "nexus.db.pg_provision._psql_tuples", side_effect=_fake_psql_tuples,
    ), patch(
        "nexus.db.pg_provision._psql", side_effect=recording_psql,
    ):
        heal_diag_view_grants_and_ownership(MagicMock(), 5432, "hal.hildebrand")

    owner_stmts = [c for c in calls if "OWNER TO" in c]
    assert len(owner_stmts) == 1
    assert 'OWNER TO "hal.hildebrand"' in owner_stmts[0]
