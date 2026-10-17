# SPDX-License-Identifier: AGPL-3.0-or-later
# cc-validation in a container (nexus-ueztp). Holds only the toolchain: Claude
# Code, tmux, uv, python. The harness files and the plugin trees are staged by
# run.sh and bind-mounted at run time, so editing a scenario never rebuilds the
# image. Same base and install route as tests/e2e/hook-surface-shakeout.
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1
# procps: the plugin hooks walk process ancestry with ps. tmux: the harness
# drives the session through a private socket. git: the scenario's own
# fallback fetch (unused when run.sh pre-stages the control tree).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps tmux \
        python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages uv
RUN useradd -m -s /bin/bash nexus
USER nexus
WORKDIR /home/nexus
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && /home/nexus/.local/bin/claude --version
USER root
# The harness points HOME at its sandbox, so ~/.local/bin/claude would no longer
# resolve inside a scenario; put claude on the system PATH.
RUN ln -s /home/nexus/.local/bin/claude /usr/local/bin/claude \
    && mkdir -p /evidence && chown nexus:nexus /evidence
USER nexus
ENV TMPDIR=/tmp
