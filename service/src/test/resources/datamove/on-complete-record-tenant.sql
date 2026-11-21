INSERT INTO dm_test.on_complete_seen (setting) SELECT current_setting('nexus.tenant', true)
