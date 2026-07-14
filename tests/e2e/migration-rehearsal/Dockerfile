# syntax=docker/dockerfile:1
# Soup-to-nuts migration dress-rehearsal box (nexus-* harness).
#
# A single ephemeral container that mirrors a fresh user's machine: the
# conexus wheel + the native nexus-service binary (NO system PostgreSQL —
# nx provisions from the signed PG bundle, nexus-5qefg) (RDR-161: the
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

# python + curl for the wheel, and the build basics chromadb's ONNX runtime +
# tokenizers need at install time. No JRE: the native nexus-service binary is
# self-contained (RDR-161).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release \
        python3 python3-venv python3-pip \
        git \
    && rm -rf /var/lib/apt/lists/*
# nexus-5qefg: NO system PostgreSQL in this image. The always-install policy
# (nexus-yv5m4, GH #1381) makes nx provision from the signed PG bundle
# unconditionally; a baked-in host PG would mask the bundle-acquisition path
# this journey exists to exercise (the reason the yv5m4 bug survived the
# gate's maiden run). The cold images (Dockerfile.cold) established the
# bare-box posture; this brings the guided/shakeout images in line.

# Non-root user — initdb (run by `nx init --service`) refuses to run as root,
# and this is the faithful unprivileged-user posture.
RUN useradd -m -s /bin/bash nexus
# The conexus venv bin gives both `nx` and a `python` with the SAME chromadb the
# tool uses (so seed_legacy.py writes a byte-compatible legacy store).
ENV PATH="/home/nexus/nxenv/bin:${PATH}"

# uv (the project's package manager) for the wheel install.
RUN pip3 install --no-cache-dir --break-system-packages uv

USER nexus
WORKDIR /home/nexus

# Pre-bake the service bge-768 ONNX (RDR-160 standard export, ~416MB) as an
# image layer so `nx init --service --embedder bge-768` finds it and skips the
# per-run download (it re-fetched on EVERY container run before this — the
# single biggest avoidable cost in the rehearsal legs). Placed BEFORE the wheel
# COPY so the layer survives wheel rebuilds. fetch_service_bge_onnx() checks
# ~/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/{model.onnx,tokenizer.json}.
# BGE_MODEL_URL/BGE_TOKENIZER_URL (nexus-myk4e, nexus-5votw): default = the
# SELF-HOSTED GitHub release asset (ci-assets-bge-768-v1), same source the
# install-time fetch (service_bge_model.py) and .github/actions/prime-bge-onnx
# use — anonymous HuggingFace downloads 429 chronically (the dependency 5votw
# retired repo-wide). The ARGs remain overridable for a re-cut asset tag.
# --retry belts the transient classes either way.
ARG BGE_MODEL_URL=https://github.com/Hellblazer/nexus/releases/download/ci-assets-bge-768-v1/model.onnx
ARG BGE_TOKENIZER_URL=https://github.com/Hellblazer/nexus/releases/download/ci-assets-bge-768-v1/tokenizer.json
RUN mkdir -p /home/nexus/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx \
 && curl -fsSL --retry 4 --retry-all-errors -o /home/nexus/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/model.onnx \
      "$BGE_MODEL_URL" \
 && curl -fsSL --retry 4 --retry-all-errors -o /home/nexus/.cache/nexus/onnx_models/bge-base-en-v1.5/onnx/tokenizer.json \
      "$BGE_TOKENIZER_URL"

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
COPY rehearse_guided.sh /home/nexus/rehearse_guided.sh
COPY rehearse_shakeout.sh /home/nexus/rehearse_shakeout.sh
COPY seed_legacy.py /home/nexus/seed_legacy.py

# Default: the full manual rehearsal. run.sh --guided overrides the command to
# /home/nexus/rehearse_guided.sh (RDR-002 ez5.13 one-command MVV).
ENTRYPOINT ["/bin/bash", "/home/nexus/rehearse.sh"]
