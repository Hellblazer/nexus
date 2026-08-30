# tests/e2e/plugin-cut-rehearsal/Dockerfile — the container run.sh re-enters
# itself in (default mode). Same base as tests/e2e/migration-rehearsal: bookworm
# ships Python 3.11, conexus needs 3.12+, so uv installs a managed 3.12.
# The source checkout is bind-mounted read-only at /src by run.sh; the
# rehearsal clones it into the container's own tmp and never writes to /src.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git python3 python3-pip tar xz-utils zstd \
    && rm -rf /var/lib/apt/lists/*

# uv from PyPI, not `COPY --from=ghcr.io/astral-sh/uv`: a build-time ghcr pull
# fails on a box whose Docker credsStore cannot reach its keychain (the same
# reason tests/e2e/migration-rehearsal installs uv this way).
RUN pip3 install --no-cache-dir --break-system-packages uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-managed
RUN uv python install 3.12 \
    && ln -s "$(uv python find 3.12)" /usr/local/bin/python3.12

# The bind-mounted source is owned by the host user; git refuses to read it
# otherwise. Identity is what the cut script's own `git commit` needs.
RUN git config --global --add safe.directory '*' \
    && git config --global user.name plugin-cut-rehearsal \
    && git config --global user.email rehearsal@invalid

WORKDIR /work
