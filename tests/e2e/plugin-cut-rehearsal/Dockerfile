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

# NON-ROOT: the cut's battery runs release-sandbox smoke, whose `nx init`
# provisions a bundled Postgres, and initdb refuses to run as root (the first
# container run failed exactly there). Same shape as the migration rehearsal.
# The cache and tmp mountpoints exist in the image, owned by this user, so
# the named volumes and the host-mounted TMPDIR inherit that ownership.
# PASSWD HOME == THE SANDBOX HOME, deliberately (nexus-ogccs). The cut's
# battery runs release-sandbox smoke with HOME=$HOME/nexus-sandbox, and the
# native engine resolves its ONNX models from Java's user.home (the passwd
# entry, not $HOME): Bge768Embedder.java / CrossEncoderReranker.java hardcode
# user.home + /.cache/nexus/onnx_models. With ENV HOME=/home/nexus below the
# smoke computes its sandbox at /home/nexus/nexus-sandbox, exactly this
# user's passwd home, so the engine finds what `nx init` provisioned. Remove
# the alignment once the engine honours an explicit model-dir env.
RUN useradd -u 1000 -d /home/nexus/nexus-sandbox -s /bin/bash nexus \
    && mkdir -p /home/nexus/.cache/uv /home/nexus/artifacts /etc/nexus-rehearsal \
    && chown -R nexus:nexus /home/nexus /etc/nexus-rehearsal
# Git config lives OUTSIDE /home: run.sh mounts a tmpfs over /home/nexus (the
# clone, its venv and the sandbox in RAM — on the overlay filesystem the
# engine's first-boot Liquibase walk took 47 s per step and readiness timed
# out) and the smoke `rm -rf`s the sandbox, so nothing durable lives there.
ENV HOME=/home/nexus \
    GIT_CONFIG_GLOBAL=/etc/nexus-rehearsal/gitconfig
# safe.directory: the bind-mounted source is owned by the host user. Identity:
# what the cut script's own `git commit` needs. The exclude mirrors the
# host's global one so `ls-files --others` matches the host's view.
RUN git config --global --add safe.directory '*' \
    && git config --global user.name plugin-cut-rehearsal \
    && git config --global user.email rehearsal@invalid \
    && printf '%s\n' '**/.claude/settings.local.json' '.DS_Store' > /etc/nexus-rehearsal/gitignore_global \
    && git config --global core.excludesFile /etc/nexus-rehearsal/gitignore_global \
    && chown nexus:nexus /etc/nexus-rehearsal/gitconfig /etc/nexus-rehearsal/gitignore_global
USER nexus
WORKDIR /home/nexus
