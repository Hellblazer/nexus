# syntax=docker/dockerfile:1
# Soup-to-nuts migration dress-rehearsal box (nexus-* harness).
#
# A single ephemeral container that mirrors a fresh user's machine: PG16 +
# pgvector + the conexus wheel + the native nexus-service binary (RDR-161: the
# sole launch artifact; no JRE). The driver (rehearse.sh) then runs the full
# operator path:
# init --service (provision PG) -> service start (migrate + serve) -> seed legacy
# Chroma -> migrate-to-service -> validate parity -> rollback.
#
# NOT DinD: PG is provisioned IN this box by `nx init --service` (initdb), exactly
# as an installed user would; nothing talks to the host docker.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

# PG16 (PGDG — bookworm ships PG15) + pgvector for PG16, python + uv for the
# wheel, and the build basics chromadb's ONNX runtime + tokenizers need at
# install time. No JRE: the native nexus-service binary is self-contained
# (RDR-161).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release \
        python3 python3-venv python3-pip \
        git \
    && install -d /etc/apt/keyrings /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-16 postgresql-16-pgvector \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — initdb (run by `nx init --service`) refuses to run as root,
# and this is the faithful unprivileged-user posture.
RUN useradd -m -s /bin/bash nexus
# PG16 client/server binaries on PATH so pg_provision's discovery finds them
# (it also probes /usr/lib/postgresql/16/bin directly, but PATH is belt+braces).
# The conexus venv bin gives both `nx` and a `python` with the SAME chromadb the
# tool uses (so seed_legacy.py writes a byte-compatible legacy store).
ENV PATH="/home/nexus/nxenv/bin:/usr/lib/postgresql/16/bin:${PATH}"

# uv (the project's package manager) for the wheel install.
RUN pip3 install --no-cache-dir --break-system-packages uv

USER nexus
WORKDIR /home/nexus

# The build context is a STAGING dir assembled by run.sh (the repo .dockerignore
# excludes dist/, and the wheel + JAR live in different trees). run.sh flattens
# them to fixed names at the context root so COPY needs no globs.
# Wheel keeps its real PEP 427 name (uv requires the version in the filename);
# it's at the context root so the glob resolves without the repo .dockerignore.
COPY conexus-*.whl /tmp/
# Install the wheel into a venv (the faithful pip-into-venv user path) so `nx`
# AND a matching `python` (with chromadb for seed_legacy.py) share one env.
# Debian bookworm ships Python 3.11; conexus needs 3.12+, so uv fetches a
# managed standalone 3.12 for the venv.
#
# The BuildKit uv cache mount persists uv's download/build cache across builds:
# the conexus wheel changes every run, but its heavy dependency closure
# (chromadb, onnxruntime, tokenizers) is stable, so a fresh wheel reuses the
# already-downloaded/compiled deps instead of a cold 10-20m install. The mount
# is keyed by nexus's uid (1000) so the unprivileged install can write it.
RUN --mount=type=cache,target=/home/nexus/.cache/uv,uid=1000,gid=1000 \
    uv venv --python 3.12 /home/nexus/nxenv \
    && uv pip install --python /home/nexus/nxenv /tmp/conexus-*.whl \
    && nx --version

# The native binary + its native-image .so siblings, kept together so the
# executable resolves its dlopen'd JDK libs from its own directory. rehearse.sh
# positions them at the well-known service location. AFTER the wheel install so a
# per-run binary change does not invalidate the cached dependency layer above.
COPY --chown=nexus:nexus native/ /opt/nexus-service-native/

# Driver scripts last: editing them rebuilds only these trivial layers, not the
# multi-minute wheel install above.
COPY rehearse.sh /home/nexus/rehearse.sh
COPY seed_legacy.py /home/nexus/seed_legacy.py

ENTRYPOINT ["/bin/bash", "/home/nexus/rehearse.sh"]
