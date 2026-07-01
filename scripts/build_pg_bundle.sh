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

fixup_macos_relocatability() {
    # macOS ONLY. PostgreSQL's own "relocatable by design" claim (see the file
    # header) covers find_my_exec-based internal path resolution (sharedir,
    # pkglibdir) — it says NOTHING about the dynamic linker's dylib load paths.
    # `configure`/`make install` bakes the literal absolute --prefix build path
    # into every libpq-linked client binary's LC_LOAD_DYLIB (and into
    # libpq.5.dylib's own LC_ID_DYLIB). Linux gets $ORIGIN-relative RPATH from
    # PG's own build system by default; macOS does not — this is a well-known
    # PG/macOS packaging gap. Confirmed 2026-07-01 (GH issue-equivalent: a
    # released bundle's psql/createdb/pg_dump/... all dyld-abort with "Library
    # not loaded: <literal CI runner build path>/libpq.5.dylib" on ANY machine
    # other than the exact CI runner path — every prior macOS release shipped
    # broken, undetected because nothing in the release pipeline ever ran a
    # real macOS relocation check against the published artifact).
    #
    # Fix: rewrite every shared lib's own ID to @rpath/<name>, rewrite every
    # LC_LOAD_DYLIB reference to those libs (in both executables and other
    # dylibs) to @rpath/<name>, and add an @loader_path-relative LC_RPATH so
    # @rpath resolves regardless of where the tree is extracted (bin/ -> ../lib,
    # lib/ -> its own directory for lib-to-lib deps like libecpg -> libpq).
    [ "$uname_s" = "Darwin" ] || return 0
    log "macOS: rewriting absolute libpq/libecpg/libpgtypes install names -> @rpath"
    local libs=(libpq.5.dylib libecpg.6.dylib libpgtypes.3.dylib libecpg_compat.3.dylib)
    local lib f
    for lib in "${libs[@]}"; do
        [ -f "${BUNDLE_PREFIX}/lib/${lib}" ] || continue
        install_name_tool -id "@rpath/${lib}" "${BUNDLE_PREFIX}/lib/${lib}"
    done
    while IFS= read -r -d '' f; do
        for lib in "${libs[@]}"; do
            install_name_tool -change "${BUNDLE_PREFIX}/lib/${lib}" "@rpath/${lib}" "$f" 2>/dev/null || true
        done
        case "$f" in
            "${BUNDLE_PREFIX}/bin/"*) install_name_tool -add_rpath "@loader_path/../lib" "$f" 2>/dev/null || true ;;
            "${BUNDLE_PREFIX}/lib/"*) install_name_tool -add_rpath "@loader_path" "$f" 2>/dev/null || true ;;
        esac
    done < <(find "${BUNDLE_PREFIX}/bin" "${BUNDLE_PREFIX}/lib" -type f \
             \( -perm -u+x -o -name "*.dylib" \) -print0 2>/dev/null)

    # Fail loud at BUILD time, not at a user's `nx init` months later: relocate
    # a COPY to a scratch dir distinct from BUNDLE_PREFIX and prove psql itself
    # (the exact binary that broke in production) actually runs from there. This
    # is deliberately independent of/in addition to the release-pipeline gate —
    # belt-and-suspenders per this bug's blast radius.
    local smoke_dir; smoke_dir="$(mktemp -d)"
    cp -R "${BUNDLE_PREFIX}" "${smoke_dir}/relocated"
    if ! "${smoke_dir}/relocated/bin/psql" --version >/dev/null 2>&1; then
        echo "FATAL: psql still not relocatable after the @rpath fixup — aborting build" >&2
        "${smoke_dir}/relocated/bin/psql" --version || true
        rm -rf "$smoke_dir"
        exit 1
    fi
    rm -rf "$smoke_dir"
    log "macOS relocatability smoke PASSED (psql runs from a distinct copy)"
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
fixup_macos_relocatability
verify_and_mark
log "bundle complete: ${BUNDLE_PREFIX}"
