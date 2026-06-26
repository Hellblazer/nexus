# SQLite-T2 migration E2E — a clean, fully-isolated container with ONLY the
# develop conexus wheel. No service stack, no host contact. Validates the
# RDR-170 / RDR-142 / nexus-3lbhb migration-runner work end-to-end through the
# real wheel-install path (which resolves version-gated migrations differently
# from the editable pytest install).
#
# Build context is THIS directory (run.sh stages the wheel here as conexus.whl,
# because the repo-root .dockerignore excludes dist/).
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /work
COPY conexus-*.whl /work/
RUN pip install --no-cache-dir /work/conexus-*.whl

COPY seed_gated.py /work/seed_gated.py
COPY rehearse_t2.sh /work/rehearse_t2.sh
RUN chmod +x /work/rehearse_t2.sh

ENTRYPOINT ["/work/rehearse_t2.sh"]
