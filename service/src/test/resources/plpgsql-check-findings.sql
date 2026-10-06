-- The plpgsql_check findings query, from PlpgsqlCheck.java in
-- /Users/hal.hildebrand/git/liquibase_validation (MIT, Copyright (c) 2026 eyupmiduck). See PlpgsqlCheck.java.
SELECT DISTINCT n.nspname, p.proname, (issue).lineno, (issue).level,
       (issue).statement, (issue).message
FROM pg_catalog.pg_proc AS p
JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
JOIN pg_catalog.pg_language AS l ON l.oid = p.prolang
CROSS JOIN LATERAL (
    -- A regular routine is analysed with no trigger relation;
    -- a trigger function is analysed once per attached
    -- relation, and skipped when it has none.
    -- Changed from the source: fatal_errors is false, so one error does not hide the findings after it (a
    -- temp-table reference early in a body would otherwise hide a missing column later in the same body); and
    -- the trigger's REFERENCING transition-table names are passed as well, so a
    -- statement trigger that reads old_rows or new_rows resolves them instead of being reported as an error.
    SELECT tg.tgrelid AS relid, tg.tgoldtable AS oldtable, tg.tgnewtable AS newtable
    FROM pg_catalog.pg_trigger AS tg
    WHERE tg.tgfoid = p.oid
    UNION ALL
    SELECT 0::oid, NULL::name, NULL::name
    WHERE p.prorettype <> 'pg_catalog.trigger'::pg_catalog.regtype
) AS r
CROSS JOIN LATERAL plpgsql_check_function_tb(
        p.oid::regprocedure, r.relid::regclass, oldtable => r.oldtable, newtable => r.newtable, fatal_errors => false, all_warnings => true
    ) AS issue
WHERE n.nspname = ANY (?)
    AND p.prokind IN ('f', 'p')
    AND l.lanname = 'plpgsql'
ORDER BY 1, 2, 3
