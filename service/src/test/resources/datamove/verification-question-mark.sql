SELECT 1 FROM dm_test.tenant_rows t WHERE t.payload LIKE '%?%' AND (t.tenant_id = $1 OR $1 IS NULL)
