# Soup-to-nuts migration dress-rehearsal box (nexus-* harness).
#
# A single ephemeral container that mirrors a fresh user's machine: PG16 +
# pgvector + a JRE + the conexus wheel + the nexus-service JAR at the well-known
# location. The driver (rehearse.sh) then runs the full operator path:
# init --service (provision PG) -> service start (migrate + serve) -> seed legacy
# Chroma -> migrate-to-service -> validate parity -> rollback.
#
# NOT DinD: PG is provisioned IN this box by `nx init --service` (initdb), exactly
# as an installed user would; nothing talks to the host docker.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

# PG16 (PGDG — bookworm ships PG15) + pgvector for PG16, a headless JRE for the
# service JAR, python + uv for the wheel, and the build basics chromadb's ONNX
# runtime + tokenizers need at install time.
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
    # Temurin 25 JRE — the service JAR is compiled for Java 25 (pom
    # maven.compiler.release=25); bookworm ships no Java 25.
    && curl -fsSL https://packages.adoptium.net/artifactory/api/gpg/key/public \
        | gpg --dearmor -o /etc/apt/keyrings/adoptium.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/adoptium.gpg] \
https://packages.adoptium.net/artifactory/deb bookworm main" \
        > /etc/apt/sources.list.d/adoptium.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-16 postgresql-16-pgvector \
        temurin-25-jre \
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
COPY nexus-service.jar /opt/nexus-service.jar

# Install the wheel into a venv (the faithful pip-into-venv user path) so `nx`
# AND a matching `python` (with chromadb for seed_legacy.py) share one env.
# Debian bookworm ships Python 3.11; conexus needs 3.12+, so uv fetches a
# managed standalone 3.12 for the venv.
RUN uv venv --python 3.12 /home/nexus/nxenv \
    && uv pip install --python /home/nexus/nxenv /tmp/conexus-*.whl \
    && nx --version

# Driver scripts last: editing them rebuilds only these trivial layers, not the
# multi-minute wheel install above.
COPY rehearse.sh /home/nexus/rehearse.sh
COPY seed_legacy.py /home/nexus/seed_legacy.py

ENTRYPOINT ["/bin/bash", "/home/nexus/rehearse.sh"]
