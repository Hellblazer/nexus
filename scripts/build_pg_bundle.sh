#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Build a COMPLETE, RELOCATABLE PostgreSQL + pgvector + pg_trgm bundle from
# source (RDR-157 P3.1, bead nexus-vwvv5.10 — Strategy B).
#
# Single source of truth for the bundle build, extracted from the proven CA-3
# CI jobs (.github/workflows/ci.yml). CA-3 falsified Strategy A (zonky reduced
# bundle): zonky ships only initdb/pg_ctl/postgres — no pg_config/psql/createdb/
# headers/pgxs — so pgvector cannot be built against it and nx's provisioner
# cannot discover it (RF-157-9). This builds a complete tree instead.
#
# Lean configure flags (safe for nexus's loopback-only, --no-locale PG):
#   --without-icu      provision runs `initdb --no-locale` (C collation).
#   --without-readline psql is driven headlessly (subprocess).
#   --without-zlib     no wire compression / pg_dump -Fc needed.
#   --without-openssl  loopback-only; EXPLICIT so an image that later ships
#                      openssl-devel cannot silently link libssl.
#
# Produces a tree whose internal layout (bin/ lib/ share/) is relocation-stable:
# PostgreSQL is relocatable by design — its programs (including pg_config)
# resolve share/lib relative to the executable's own location via find_my_exec,
# so after extraction to a new prefix pg_config reports the NEW paths, not this
# build prefix. nexus additionally re-anchors the sharedir on bin_dir
# (pg_provision._candidate_sharedirs) as belt-and-suspenders. The tree therefore
# works after extraction to any directory (proven by
# tests/db/test_pg_bundle_relocation.py).
#
# Required env:
#   BUNDLE_PREFIX   absolute install prefix (configure --prefix); also the tree root.
# Optional env:
#   PG_VERSION              default 17.5   (PG major aligned on 17; nexus-41bso)
#   PGVECTOR_VERSION        default v0.8.2 (>=0.8 is the RDR-155 iterative_scan floor)
#   WORK_DIR                scratch dir for sources; default `mktemp -d`
#   MACOSX_DEPLOYMENT_TARGET (darwin only) default 13.0 — Mach-O minos floor
#   SKIP_PREREQS            when "1", do not attempt to install flex/bison/perl
#
# Usage:
#   BUNDLE_PREFIX=/opt/nexus-pg scripts/build_pg_bundle.sh
set -euo pipefail

: "${BUNDLE_PREFIX:?BUNDLE_PREFIX (absolute install prefix) is required}"
PG_VERSION="${PG_VERSION:-17.5}"
PGVECTOR_VERSION="${PGVECTOR_VERSION:-v0.8.2}"
# Default to a private scratch dir we own (and clean up). If the caller supplies
# WORK_DIR we leave it untouched on exit — it's theirs.
if [ -z "${WORK_DIR:-}" ]; then
    WORK_DIR="$(mktemp -d)"
    trap 'rm -rf "$WORK_DIR"' EXIT
fi
SKIP_PREREQS="${SKIP_PREREQS:-0}"

uname_s="$(uname -s)"

log() { echo "=== $* ==="; }

install_prereqs() {
    if [ "$SKIP_PREREQS" = "1" ]; then
        log "SKIP_PREREQS=1 — assuming flex/bison/perl present"
        return
    fi
    case "$uname_s" in
        Linux)
            # manylinux_2_28 base toolchain lacks these.
            if command -v dnf >/dev/null 2>&1; then
                dnf -y -q install flex bison perl >/dev/null
            elif command -v apt-get >/dev/null 2>&1; then
                apt-get -y -q install flex bison perl >/dev/null
            else
                echo "WARN: no dnf/apt-get found — assuming flex/bison/perl present" >&2
            fi
            ;;
        Darwin)
            # PG's build needs a newer flex/bison than macOS ships; Xcode CLT
            # provides clang/make. curl/perl/otool are present on the runner.
            brew install -q flex bison >/dev/null
            export PATH="$(brew --prefix bison)/bin:$(brew --prefix flex)/bin:$PATH"
            ;;
    esac
}

configure_flags=( "--prefix=${BUNDLE_PREFIX}"
                  --without-icu --without-zlib --without-readline --without-openssl )

build_pg() {
    local njobs target
    log "build PostgreSQL ${PG_VERSION} -> ${BUNDLE_PREFIX} (${uname_s})"
    cd "$WORK_DIR"
    curl -fsSL "https://ftp.postgresql.org/pub/source/v${PG_VERSION}/postgresql-${PG_VERSION}.tar.bz2" -o pg.tar.bz2
    tar -xjf pg.tar.bz2
    cd "postgresql-${PG_VERSION}"

    if [ "$uname_s" = "Darwin" ]; then
        # Make the deployment-target floor explicit in CFLAGS rather than relying
        # on apple-clang implicitly honouring MACOSX_DEPLOYMENT_TARGET — pgvector's
        # Makefile invokes the compiler directly (code-review M2, carried from CA-3).
        target="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
        export MACOSX_DEPLOYMENT_TARGET="$target"
        export CFLAGS="-mmacosx-version-min=${target}${CFLAGS:+ ${CFLAGS}}"
        njobs="$(sysctl -n hw.ncpu)"
    else
        njobs="$(nproc)"
    fi

    ./configure "${configure_flags[@]}" >/dev/null
    make -s -j"${njobs}" >/dev/null
    make -s install >/dev/null
    # Contrib extensions the schema needs (pg_trgm, RDR-155). Core `make install`
    # does NOT build contrib — the bundle would be missing pg_trgm and the full
    # migration would fail.
    make -s -C contrib/pg_trgm install >/dev/null
}

build_pgvector() {
    log "build pgvector ${PGVECTOR_VERSION} against ${BUNDLE_PREFIX}/bin/pg_config"
    cd "$WORK_DIR"
    git clone --depth 1 --branch "${PGVECTOR_VERSION}" https://github.com/pgvector/pgvector.git
    cd pgvector
    make -s PG_CONFIG="${BUNDLE_PREFIX}/bin/pg_config"
    make -s PG_CONFIG="${BUNDLE_PREFIX}/bin/pg_config" install
}

verify_and_mark() {
    log "verify complete tool set + injected extension"
    local b vector_lib pkglib sharedir
    for b in initdb pg_ctl postgres psql createdb pg_config; do
        test -x "${BUNDLE_PREFIX}/bin/${b}" || { echo "missing ${BUNDLE_PREFIX}/bin/${b}"; exit 1; }
    done
    if [ "$uname_s" = "Darwin" ]; then vector_lib="vector.dylib"; else vector_lib="vector.so"; fi
    pkglib="$("${BUNDLE_PREFIX}/bin/pg_config" --pkglibdir)"
    sharedir="$("${BUNDLE_PREFIX}/bin/pg_config" --sharedir)"
    test -f "${pkglib}/${vector_lib}" || { ls -la "${pkglib}"; exit 1; }
    test -f "${sharedir}/extension/vector.control" || { ls -la "${sharedir}/extension"; exit 1; }
    test -f "${sharedir}/extension/pg_trgm.control" || { ls -la "${sharedir}/extension"; exit 1; }

    # Record the configure --prefix so the relocation smoke can prove that an
    # extracted root differs from where the tree was built
    # (tests/db/test_pg_bundle_relocation.py::TestActuallyRelocated).
    python3 -c "import os,sys; print(os.path.realpath(sys.argv[1]))" "${BUNDLE_PREFIX}" \
        > "${BUNDLE_PREFIX}/.build_prefix"

    echo "Built: $("${BUNDLE_PREFIX}/bin/initdb" --version)"
    echo "  ${vector_lib} at ${pkglib}"
    echo "  build prefix recorded at ${BUNDLE_PREFIX}/.build_prefix"
}

mkdir -p "$BUNDLE_PREFIX"
install_prereqs
build_pg
build_pgvector
verify_and_mark
log "bundle complete: ${BUNDLE_PREFIX}"
