# nexus-f3yqd.2.8 (RDR-246 Phase 1 Step 3c): the product's test Postgres image plus pg_background, built from the
# same pinned commit as the bundle (scripts/build_pg_bundle.sh pg_background_pinned_commit, and
# scripts/build_pg_bundle_windows.py). Built at test time by dev.nexus.service.PgBackgroundImage, never pushed.
# BASE and PG_BACKGROUND_COMMIT come from PgContainerRecipe.IMAGE and PgBackgroundImage.PG_BACKGROUND_COMMIT.
ARG BASE
FROM ${BASE} AS build
ARG PG_BACKGROUND_COMMIT
# The build tools stay in this stage; the final image carries only the installed files. A commit, not a tag, is
# fetched, and the checkout is verified: a tag can be moved, a commit cannot.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential git ca-certificates libkrb5-dev postgresql-server-dev-17 \
 && git init -q /src \
 && cd /src \
 && git fetch -q --depth 1 https://github.com/vibhorkum/pg_background.git "${PG_BACKGROUND_COMMIT}" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "${PG_BACKGROUND_COMMIT}" \
 && make -s \
 && make -s install DESTDIR=/out

FROM ${BASE}
COPY --from=build /out/ /
