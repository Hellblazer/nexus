#!/usr/bin/env bash
# nexus-ywts8: apply the service's FULL Liquibase changelog against the
# from-source CA-3 bundle PG, confirming the lean (ICU-less, --no-locale,
# no-zlib/readline/openssl) bundle handles the real schema — the
# "complete local-distro proof" the CA-3 gate alone does not give.
#
# The changelogs are pure SQL (no Java <customChange>), so the liquibase CLI
# applies them faithfully — no need to build the JVM service. We reuse
# pg_provision so the real nexus_admin / nexus_svc roles + nexus DB exist before
# the migration; role-001's `IF NOT EXISTS` then no-ops, mirroring the production
# "DBA pre-creates the role, the changelog is self-contained" path.
#
# linux-only: uses `docker --network host` to let the liquibase container reach
# the provisioned PG on 127.0.0.1. Requires NEXUS_CA3_BUNDLE (the built bundle),
# docker, and a uv-installed nexus.
set -euo pipefail

LIQUIBASE_IMAGE="${LIQUIBASE_IMAGE:-liquibase/liquibase:4.31}"
bundle="${NEXUS_CA3_BUNDLE:?NEXUS_CA3_BUNDLE must point at the built PG+pgvector bundle}"
export NEXUS_PG_BIN="$bundle/bin"
cfg="$(mktemp -d)"
export NEXUS_CONFIG_DIR="$cfg"

cleanup() { "$bundle/bin/pg_ctl" -D "$cfg/postgres" -m immediate stop >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "==> provisioning bundle cluster (nexus_admin + nexus_svc + nexus DB)"
uv run python -c "from nexus.db.pg_provision import provision; provision()"

# shellcheck disable=SC1091
set -a; . "$cfg/pg_credentials"; set +a
echo "==> provisioned: port=${PG_PORT} admin=${NX_DB_ADMIN_USER}"

# Mount the resources ROOT (not just db/changelog), because the master
# changelog's <include file="db/changelog/...">s are relative to the classpath
# root. searchPath=/cl + changeLogFile=db/changelog/db.changelog-master.xml.
resources_dir="$PWD/service/src/main/resources"
test -f "$resources_dir/db/changelog/db.changelog-master.xml" \
  || { echo "FAIL: master changelog not found under $resources_dir"; exit 1; }

echo "==> applying master changelog via liquibase CLI ($LIQUIBASE_IMAGE)"
docker run --rm --network host -v "$resources_dir:/cl:ro" "$LIQUIBASE_IMAGE" \
  --searchPath=/cl \
  --changeLogFile=db/changelog/db.changelog-master.xml \
  --url="jdbc:postgresql://127.0.0.1:${PG_PORT}/nexus" \
  --username="${NX_DB_ADMIN_USER}" \
  --password="${NX_DB_ADMIN_PASS}" \
  update

# Confirm a representative set of objects actually landed (non-vacuous).
applied=$(PGPASSWORD="${NX_DB_ADMIN_PASS}" "$bundle/bin/psql" \
  -h 127.0.0.1 -p "${PG_PORT}" -U "${NX_DB_ADMIN_USER}" -d nexus -tAc \
  "SELECT count(*) FROM databasechangelog")
echo "==> databasechangelog rows: ${applied}"
[ "${applied:-0}" -gt 0 ] || { echo "FAIL: no changesets recorded"; exit 1; }

echo "LIQUIBASE-AGAINST-BUNDLE SMOKE PASS (${applied} changesets on the lean bundle)"
