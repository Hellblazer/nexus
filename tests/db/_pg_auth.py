# SPDX-License-Identifier: AGPL-3.0-or-later
"""Let a test's raw libpq clients reach a provisioned (scram) cluster (nexus-ja4pq).

``provision()`` now creates a cluster that demands a password. A test that
provisions one and then drives it with a bare ``psql -U <os user>`` would be
refused, exactly as any other local account is meant to be. The tests are not
trying to model an attacker, so they present the superuser's recorded password
the ordinary libpq way: a ``PGPASSFILE`` naming the cluster's port.

Every libpq client inherits it, the product's own ``_psql`` included, so the
existing test bodies stay as they were. A role other than the superuser is
authenticated by the test itself (``PGPASSWORD`` in its env), as before.
"""
from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

from nexus.db import pg_provision as pp


@contextlib.contextmanager
def superuser_pgpass(config_dir: Path) -> Iterator[None]:
    """Scope in which ``PGPASSFILE`` carries the superuser password of the cluster
    provisioned under *config_dir*."""
    creds = pp._read_credentials(config_dir / pp.CREDENTIALS_FILENAME)
    line = f"127.0.0.1:{creds['PG_PORT']}:*:{pp.bootstrap_superuser()}:{creds['PG_SUPERUSER_PASS']}\n"
    fd, path = tempfile.mkstemp(prefix="nx_pgpass_", dir=str(config_dir))
    with os.fdopen(fd, "w") as fh:
        fh.write(line)
    os.chmod(path, 0o600)  # libpq ignores a pgpass file other accounts can read
    previous = os.environ.get("PGPASSFILE")
    os.environ["PGPASSFILE"] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PGPASSFILE", None)
        else:
            os.environ["PGPASSFILE"] = previous
        with contextlib.suppress(OSError):
            os.unlink(path)
