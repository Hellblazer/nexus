# SPDX-License-Identifier: AGPL-3.0-or-later
# cc-validation in a container (nexus-ueztp). Holds only the toolchain: Claude
# Code, tmux, uv, python. The harness files and the plugin trees are staged by
# run.sh and bind-mounted at run time, so editing a scenario never rebuilds the
# image. Same base and install route as tests/e2e/hook-surface-shakeout.
FROM debian:trixie-slim
# The Claude Code build the harness measures. run.sh reads this line and hands
# it to the scenario as the expected `claude --version`, so a drifted image
# fails the verdict instead of silently testing another build. The updater is
# off so a pinned install stays pinned at run time.
ARG CLAUDE_CODE_VERSION=2.1.302
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1
# procps: the plugin hooks walk process ancestry with ps. tmux: the harness
# drives the session through a private socket. git: the scenario's own
# fallback fetch (unused when run.sh pre-stages the control tree).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps tmux \
        python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*
# mcp: the MCP stub scenarios launch tests/cc-validation/fixtures/stub_server.py with
# .venv/bin/python, which the entrypoint links to this python3. Without the
# package every stub scenario (11, 14, ...) reads "server never spawned" and
# nexus-spwp2's 14b could not tell a plugin agent's mcpServers from a stub that
# cannot start. Version = uv.lock's.
RUN pip3 install --no-cache-dir --break-system-packages uv mcp==1.27.1
RUN useradd -m -s /bin/bash nexus
USER nexus
WORKDIR /home/nexus
RUN curl -fsSL https://claude.ai/install.sh | bash -s "${CLAUDE_CODE_VERSION}" \
    && /home/nexus/.local/bin/claude --version \
    && /home/nexus/.local/bin/claude --version | grep -q "^${CLAUDE_CODE_VERSION} "
USER root
# The Nexus CLI the conexus plugin's servers and mcp_tool hooks run (nx-mcp,
# nx-hook). Without it the plugin loads but its SubagentStart hook fails with
# "Unknown tool: 'hook_subagent_start'. Nexus is not installed." and scenario 12
# measures nothing (nexus-spwp2). It is built from cli-src/, which run.sh stages
# from the SAME source tree whose plugins are under test (src/, pyproject.toml,
# the package data the wheel force-includes), never fetched from PyPI by version:
# a develop plugin then runs against the develop CLI, and a release branch works
# before its version is published. Installed system-wide (the harness points HOME
# at its sandbox) and found by the plugin's launcher through PATH.
COPY cli-src /tmp/cli-src
RUN mkdir -p /opt/uv-tools /opt/uv-python \
    && UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin UV_PYTHON_INSTALL_DIR=/opt/uv-python \
       uv tool install /tmp/cli-src \
    && chmod -R a+rX /opt/uv-tools /opt/uv-python \
    && test -x /usr/local/bin/nx-mcp \
    && rm -rf /tmp/cli-src
# The harness points HOME at its sandbox, so ~/.local/bin/claude would no longer
# resolve inside a scenario; put claude on the system PATH.
RUN ln -s /home/nexus/.local/bin/claude /usr/local/bin/claude \
    && mkdir -p /evidence && chown nexus:nexus /evidence
USER nexus
ENV TMPDIR=/tmp
