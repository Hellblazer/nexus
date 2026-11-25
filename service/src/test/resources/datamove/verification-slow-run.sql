SELECT 1 FROM dm_test.tenant_plain t WHERE t.n = 0 AND (t.tenant_id = $1 OR $1 IS NULL) UNION ALL SELECT 1 WHERE pg_sleep(0.4)::text = 'never'
