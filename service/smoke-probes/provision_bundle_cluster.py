#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Provision one nexus PostgreSQL cluster from a PG bundle, for native-data-move-smoke.sh (nexus-f3yqd.2.22).

Run as ``NEXUS_PG_BIN=<bundle>/bin NEXUS_CONFIG_DIR=<dir> python provision_bundle_cluster.py <dir>``. It is
``nexus.db.pg_provision.provision``, the path ``nx init --service`` takes: initdb, the roles ``nexus_admin`` and
``nexus_svc`` (two roles, so the engine has a separate admin role), the vector extension, and the RDR-246
dml_utils bootstrap, which creates pg_background and the three library roles when the bundle carries
pg_background and skips with a reason when it does not. The cluster is left running; the credentials land in
``<dir>/pg_credentials``. Prints the port and the credentials path, never a password.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: provision_bundle_cluster.py <config-dir>", file=sys.stderr)
        return 2
    config_dir = Path(argv[1]).resolve()
    if not os.environ.get("NEXUS_PG_BIN"):
        print("provision_bundle_cluster: NEXUS_PG_BIN must name the bundle's bin directory", file=sys.stderr)
        return 2
    if Path(os.environ.get("NEXUS_CONFIG_DIR", "")).resolve() != config_dir:
        print("provision_bundle_cluster: NEXUS_CONFIG_DIR must equal the config-dir argument", file=sys.stderr)
        return 2
    from nexus.db.pg_provision import CREDENTIALS_FILENAME, provision  # noqa: PLC0415 — after the env checks

    result = provision(config_dir, force_new_port=True)
    creds = config_dir / CREDENTIALS_FILENAME
    if not creds.is_file():
        print(f"provision_bundle_cluster: provision returned but {creds} is missing", file=sys.stderr)
        return 1
    print(f"provisioned: port={result.port} credentials={creds}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
