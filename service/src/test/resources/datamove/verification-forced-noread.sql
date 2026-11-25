SELECT 1 FROM dm_test.forced_noread t WHERE t.n = 0 AND (t.tenant_id = $1 OR $1 IS NULL)
