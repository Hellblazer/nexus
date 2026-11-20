#!/usr/bin/env bash
# RDR-229 P2.10 sizing on a PITR fork (nexus-p2m1x.15; Sam 2026-11-20, T2 nexus/sam-ruling-p210-moves-on-spike-fork-2026-11-20).
#
# Times the three residue repair moves that catalog-049-2 registers (rdr229-residue-catalog-documents, then
# -document-aspects and -document-highlights) on a FORK, with no engine: dml_utils needs pg_background workers, which
# the cloud database does not have yet (nexus-f3yqd.2.23), so this driver walks each driving table by its primary key
# in committed chunks the way dml_utils' primary_key strategy does, runs the move's own template on each chunk, and
# sleeps sleep_ms between chunks. The templates below are copied verbatim from catalog-049-note-uri-residue.xml
# (<driving_table> rendered as "nexus.<table> AS t", <chunking_clause> as the chunk's key range on t).
#
# While each sweep point runs, a second session calls nexus.plain_search_1024 with p_exclude_sensitive true (its
# sensitivity subquery reads catalog_documents) in a loop and records each call's latency; an idle baseline of the same
# loop runs first. Everything it writes goes to schema p210_timing on the fork, plus the moves' own repairs.
#
# Run as postgres on the FORK host only:  DB=nexus scripts/spikes/rdr229-p210-fork-timing/run.sh
# Refuses unless FORK_CONFIRM=i-am-on-the-fork is set.
#
# The fork carries P3.2's schema (wren's 00-04: <name>_id columns, NOT VALID CHECKs, FK twins, extra unique indexes),
# so write costs are a little above production's: record that beside the numbers. The moves only UPDATE source_uri,
# and 02 stamped every row's <name>_id, so the NOT VALID CHECKs pass on these updates.
#
# Note: the first sweep point of each move does the repair writes; later points walk the same rows and find nothing
# left to change, so they time the walk. That is the cost that follows table size (T2 229-research-66).
set -euo pipefail

DB="${DB:-nexus}"
[[ "${FORK_CONFIRM:-}" == "i-am-on-the-fork" ]] || { echo "refusing: set FORK_CONFIRM=i-am-on-the-fork (this writes)" >&2; exit 2; }
PSQL=(psql -X -q -v ON_ERROR_STOP=1 -d "$DB")
BASELINE_S="${BASELINE_S:-60}"
# "table:chunk_size:sleep_ms" sweep points, run in order.
SWEEP="${SWEEP:-catalog_documents:1000:0 catalog_documents:1000:100 catalog_documents:5000:100 document_aspects:1000:50 document_highlights:1000:50}"

"${PSQL[@]}" <<'SQL'
CREATE SCHEMA IF NOT EXISTS p210_timing;
CREATE TABLE IF NOT EXISTS p210_timing.chunk (run text, tbl text, chunk_no int, rows_changed bigint, ms double precision, at timestamptz DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS p210_timing.search (run text, ms double precision, at timestamptz DEFAULT clock_timestamp());
CREATE TABLE IF NOT EXISTS p210_timing.flag (run text PRIMARY KEY, done boolean NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS p210_timing.template (tbl text PRIMARY KEY, sql text NOT NULL);
TRUNCATE p210_timing.template;
INSERT INTO p210_timing.template VALUES
('catalog_documents', $t$UPDATE nexus.catalog_documents AS t
   SET source_uri = 'chroma://' || c.content_type || '__' || substr(t.source_uri, 10)
  FROM nexus.catalog_collections c
 WHERE <chunking_clause>
   AND starts_with(t.source_uri, 'chroma://')
   AND strpos(substr(t.source_uri, 10), '/') > 1
   AND split_part(substr(t.source_uri, 10), '/', 1) = t.physical_collection
   AND NOT (starts_with(t.physical_collection, 'rdr__') OR starts_with(t.physical_collection, 'docs__') OR starts_with(t.physical_collection, 'code__'))
   AND c.tenant_id = t.tenant_id AND c.name = t.physical_collection
   AND c.content_type IN ('code', 'docs', 'rdr')
   AND NOT EXISTS (SELECT 1 FROM nexus.catalog_documents h
                   WHERE h.tenant_id = t.tenant_id AND h.deleted_at IS NULL AND h.tumbler <> t.tumbler
                     AND h.source_uri = 'chroma://' || c.content_type || '__' || substr(t.source_uri, 10))$t$),
('document_aspects', $t$UPDATE nexus.document_aspects AS t
   SET source_uri = 'chroma://' || c.content_type || '__' || substr(t.source_uri, 10)
  FROM nexus.catalog_collections c, nexus.catalog_documents d
 WHERE <chunking_clause>
   AND starts_with(t.source_uri, 'chroma://')
   AND strpos(substr(t.source_uri, 10), '/') > 1
   AND split_part(substr(t.source_uri, 10), '/', 1) = t.collection
   AND NOT (starts_with(t.collection, 'rdr__') OR starts_with(t.collection, 'docs__') OR starts_with(t.collection, 'code__'))
   AND c.tenant_id = t.tenant_id AND c.name = t.collection
   AND c.content_type IN ('code', 'docs', 'rdr')
   AND d.tenant_id = t.tenant_id AND d.tumbler = t.doc_id
   AND starts_with(d.source_uri, 'chroma://' || c.content_type || '__' || t.collection || '/')$t$),
('document_highlights', $t$UPDATE nexus.document_highlights AS t
   SET source_uri = 'chroma://' || c.content_type || '__' || substr(t.source_uri, 10)
  FROM nexus.catalog_collections c, nexus.catalog_documents d
 WHERE <chunking_clause>
   AND starts_with(t.source_uri, 'chroma://')
   AND strpos(substr(t.source_uri, 10), '/') > 1
   AND split_part(substr(t.source_uri, 10), '/', 1) = t.collection
   AND NOT (starts_with(t.collection, 'rdr__') OR starts_with(t.collection, 'docs__') OR starts_with(t.collection, 'code__'))
   AND c.tenant_id = t.tenant_id AND c.name = t.collection
   AND c.content_type IN ('code', 'docs', 'rdr')
   AND d.tenant_id = t.tenant_id AND d.tumbler = t.doc_id
   AND starts_with(d.source_uri, 'chroma://' || c.content_type || '__' || t.collection || '/')$t$);

-- One chunked pass of a move: keyset over the driving table's primary key, one COMMIT per chunk.
CREATE OR REPLACE PROCEDURE p210_timing.run_move(p_run text, p_tbl text, p_chunk int, p_sleep_ms int)
LANGUAGE plpgsql AS $p$
DECLARE
    v_cols   text;   -- t.a, t.b
    v_bare   text;   -- a, b
    v_lits   text;   -- quote_literal(a::text) || ', ' || quote_literal(b::text)
    v_tmpl   text;
    v_last   text := NULL;   -- the last key done, as literals: 'x', '1.2' (untyped, so they take the columns' types)
    v_upper  text;
    v_clause text;
    v_n      bigint;
    v_t0     timestamptz;
    v_no     int := 0;
BEGIN
    SELECT string_agg('t.' || quote_ident(a.attname), ', ' ORDER BY k.ord), string_agg(quote_ident(a.attname), ', ' ORDER BY k.ord),
           string_agg(format('quote_literal(%I::text)', a.attname), $$ || ', ' || $$ ORDER BY k.ord)
      INTO v_cols, v_bare, v_lits
      FROM pg_index i
      CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
      JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
     WHERE i.indrelid = ('nexus.' || p_tbl)::regclass AND i.indisprimary;
    IF v_cols IS NULL THEN RAISE EXCEPTION 'no primary key on nexus.%', p_tbl; END IF;
    SELECT sql INTO v_tmpl FROM p210_timing.template WHERE tbl = p_tbl;
    LOOP
        EXECUTE format('SELECT %s FROM nexus.%I WHERE %s ORDER BY %s OFFSET %s LIMIT 1',
                       v_lits, p_tbl,
                       CASE WHEN v_last IS NULL THEN 'true' ELSE format('ROW(%s) > ROW(%s)', v_bare, v_last) END,
                       v_bare, p_chunk - 1)
           INTO v_upper;
        v_clause := CASE WHEN v_last IS NULL THEN 'true' ELSE format('ROW(%s) > ROW(%s)', v_cols, v_last) END
                 || CASE WHEN v_upper IS NULL THEN '' ELSE format(' AND ROW(%s) <= ROW(%s)', v_cols, v_upper) END;
        v_t0 := clock_timestamp();
        EXECUTE replace(v_tmpl, '<chunking_clause>', v_clause);
        GET DIAGNOSTICS v_n = ROW_COUNT;
        v_no := v_no + 1;
        INSERT INTO p210_timing.chunk(run, tbl, chunk_no, rows_changed, ms)
             VALUES (p_run, p_tbl, v_no, v_n, extract(epoch FROM clock_timestamp() - v_t0) * 1000);
        COMMIT;
        EXIT WHEN v_upper IS NULL;
        v_last := v_upper;
        IF p_sleep_ms > 0 THEN PERFORM pg_sleep(p_sleep_ms / 1000.0); END IF;
    END LOOP;
END
$p$;

-- The search load: plain_search_1024 on the first 1024-dimension chunk's tenant and collection, with the sensitivity
-- subquery on (no ORDER BY: sorting nexus.chunks would cost more than the run).
CREATE OR REPLACE PROCEDURE p210_timing.search_loop(p_run text, p_max_s int)
LANGUAGE plpgsql AS $p$
DECLARE
    v_tenant text; v_coll text; v_model text; v_q nexus.vector; v_t0 timestamptz; v_end timestamptz;
BEGIN
    SELECT tenant_id, collection, embedding_model, embedding_1024 INTO v_tenant, v_coll, v_model, v_q
      FROM nexus.chunks WHERE embedding_1024 IS NOT NULL LIMIT 1;
    IF v_q IS NULL THEN RAISE EXCEPTION 'no 1024-dimension chunk on this database to search with'; END IF;
    v_end := clock_timestamp() + make_interval(secs => p_max_s);
    -- The driver sets this run's flag to false before it starts the loop and to true when the move ends; the loop
    -- only reads it (a missing row means "not stopped", so the baseline runs to its time limit).
    WHILE clock_timestamp() < v_end
          AND NOT COALESCE((SELECT done FROM p210_timing.flag WHERE run = p_run), false) LOOP
        v_t0 := clock_timestamp();
        PERFORM count(*) FROM nexus.plain_search_1024(v_q, ARRAY[v_coll], NULL, NULL, 10, v_model, v_tenant, true);
        INSERT INTO p210_timing.search(run, ms) VALUES (p_run, extract(epoch FROM clock_timestamp() - v_t0) * 1000);
        COMMIT;
    END LOOP;
END
$p$;
SQL

echo "== census (read-only)"
"${PSQL[@]}" -c "SELECT 'catalog_documents' AS tbl, count(*) FROM nexus.catalog_documents UNION ALL SELECT 'document_aspects', count(*) FROM nexus.document_aspects UNION ALL SELECT 'document_highlights', count(*) FROM nexus.document_highlights;"

SEARCH="${SEARCH:-1}"   # SEARCH=0 skips the search load (a dry run on a store with no 1024-dimension chunks)
if [[ "$SEARCH" == 1 ]]; then
    echo "== idle search baseline (${BASELINE_S}s)"
    "${PSQL[@]}" -c "CALL p210_timing.search_loop('baseline', ${BASELINE_S});"
fi

for point in $SWEEP; do
    IFS=: read -r tbl chunk sleep_ms <<<"$point"
    run="${tbl}-c${chunk}-s${sleep_ms}"
    echo "== $run"
    "${PSQL[@]}" -c "DELETE FROM p210_timing.chunk WHERE run = '$run'; DELETE FROM p210_timing.search WHERE run = '$run'; INSERT INTO p210_timing.flag(run, done) VALUES ('$run', false) ON CONFLICT (run) DO UPDATE SET done = false;"
    load_pid=""
    if [[ "$SEARCH" == 1 ]]; then
        "${PSQL[@]}" -c "CALL p210_timing.search_loop('$run', 7200);" &
        load_pid=$!
    fi
    start=$(date +%s)
    "${PSQL[@]}" -c "CALL p210_timing.run_move('$run', '$tbl', $chunk, $sleep_ms);"
    echo "   wall $(( $(date +%s) - start )) s"
    "${PSQL[@]}" -c "INSERT INTO p210_timing.flag(run, done) VALUES ('$run', true) ON CONFLICT (run) DO UPDATE SET done = true;"
    [[ -z "$load_pid" ]] || wait "$load_pid"
done

echo "== results"
"${PSQL[@]}" <<'SQL'
SELECT run, tbl, count(*) AS chunks, sum(rows_changed) AS rows_changed,
       round(sum(ms)::numeric, 0) AS chunk_ms_total,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY ms)::numeric, 1) AS chunk_p50_ms,
       round(max(ms)::numeric, 1) AS chunk_max_ms
  FROM p210_timing.chunk GROUP BY run, tbl ORDER BY min(at);
SELECT run, count(*) AS calls,
       round(percentile_cont(0.5)  WITHIN GROUP (ORDER BY ms)::numeric, 1) AS p50_ms,
       round(percentile_cont(0.99) WITHIN GROUP (ORDER BY ms)::numeric, 1) AS p99_ms
  FROM p210_timing.search GROUP BY run ORDER BY min(at);
SQL
echo "Rule for the INSERT: pick the (chunk_size, sleep_ms) whose search p99 stays within 20% of the baseline's; statement_timeout_ms = 3 x that point's chunk_max_ms (round up)."
