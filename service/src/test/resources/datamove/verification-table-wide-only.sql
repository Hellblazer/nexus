/* nexus:table-wide-only */
SELECT 1 FROM dm_test.tenant_plain t WHERE t.n = 0 AND $1 IS NULL
