# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-208 local-mode MVV image (beads nexus-galkv.19, nexus-kdxyv). Built by
# run.sh from a staging directory holding wheel/ (a wheel, or SPEC naming a
# published release), plugin/ (the conexus plugin of the version under test,
# its hooks.json trimmed to the hooks under test), settings.json, and the two
# journey files. The Claude Code sessions inside are REAL: run.sh mounts a
# fresh oauth credential read-only at run time.
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1
# procps: nexus.session.find_immediate_claude_pid walks ancestry with ps.
# tmux: the sessions are driven through a private tmux socket.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps tmux \
        python3 python3-venv python3-pip \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages uv
RUN useradd -m -s /bin/bash nexus
USER nexus
WORKDIR /home/nexus
ENV PATH="/home/nexus/nxenv/bin:/home/nexus/.local/bin:${PATH}" \
    NX_HOOK_PYTHON=/home/nexus/nxenv/bin/python
# The real Linux `claude` CLI (the Dockerfile.fullstack recipe).
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && /home/nexus/.local/bin/claude --version
RUN uv venv --python 3.12 /home/nexus/nxenv
COPY --chown=nexus:nexus wheel/ /home/nexus/wheel/
RUN if ls /home/nexus/wheel/*.whl > /dev/null 2>&1; then \
        uv pip install --python /home/nexus/nxenv /home/nexus/wheel/*.whl; \
    else \
        uv pip install --python /home/nexus/nxenv "$(cat /home/nexus/wheel/SPEC)"; \
    fi
COPY --chown=nexus:nexus plugin/ /home/nexus/plugin/
COPY --chown=nexus:nexus settings.json /home/nexus/.claude/settings.json
COPY --chown=nexus:nexus mvv_in_container.sh send.py assistant_said.py turn_end.py channel_wakes.py /home/nexus/
ENTRYPOINT ["/bin/bash", "/home/nexus/mvv_in_container.sh"]
