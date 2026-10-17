# SPDX-License-Identifier: AGPL-3.0-or-later
# nexus-4mptw prefix-capture image: real Claude Code at a pinned version plus
# the published conexus CLI, so the plugins' MCP server and hooks run as on a
# user's box. No credential is baked in; run.sh passes the automation token at
# run time through tests/e2e/lib/claude_credentials.py.
FROM debian:trixie-slim
ARG CC_VERSION=2.1.300
ARG CONEXUS_VERSION=7.79.0
# NX_NO_TELEMETRY: every container is a fresh install, and a hand-typed docker run
# names no -e flag; the image opts out whoever starts it (nexus-ca5rw).
ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 PYTHONUNBUFFERED=1 NX_NO_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps python3 python3-venv python3-pip libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages uv
RUN useradd -m -s /bin/bash nexus
USER nexus
WORKDIR /home/nexus
ENV PATH="/home/nexus/.local/bin:${PATH}"
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CC_VERSION} \
    && claude --version
RUN uv tool install --python 3.12 "conexus==${CONEXUS_VERSION}" && nx --version
COPY --chown=nexus:nexus proxy.py capture_in_container.sh analyse.py /home/nexus/
ENTRYPOINT ["/bin/bash", "/home/nexus/capture_in_container.sh"]
