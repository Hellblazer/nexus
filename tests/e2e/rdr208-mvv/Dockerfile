# SPDX-License-Identifier: AGPL-3.0-or-later
# RDR-208 local-mode MVV image (bead nexus-galkv.19). Built by run.sh from a
# staging directory holding wheel/ (a wheel, or SPEC naming a published
# release), hooks/ (conexus/hooks/scripts of the version under test) and the
# three journey files.
FROM debian:trixie-slim
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1
# procps: nexus.session.find_immediate_claude_pid walks ancestry with ps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git jq procps \
        python3 python3-venv python3-pip \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN pip3 install --no-cache-dir --break-system-packages uv
RUN useradd -m -s /bin/bash nexus
USER nexus
WORKDIR /home/nexus
ENV PATH="/home/nexus/nxenv/bin:/home/nexus/.local/bin:${PATH}"
RUN uv venv --python 3.12 /home/nexus/nxenv
COPY --chown=nexus:nexus wheel/ /home/nexus/wheel/
RUN if ls /home/nexus/wheel/*.whl > /dev/null 2>&1; then \
        uv pip install --python /home/nexus/nxenv /home/nexus/wheel/*.whl; \
    else \
        uv pip install --python /home/nexus/nxenv "$(cat /home/nexus/wheel/SPEC)"; \
    fi
COPY --chown=nexus:nexus hooks/ /home/nexus/hooks/
COPY --chown=nexus:nexus mvv_in_container.sh session_server.sh send.py /home/nexus/
# The stand-in Claude Code process: a bash whose command name is "claude".
RUN mkdir -p /home/nexus/bin && cp /bin/bash /home/nexus/bin/claude
ENTRYPOINT ["/bin/bash", "/home/nexus/mvv_in_container.sh"]
