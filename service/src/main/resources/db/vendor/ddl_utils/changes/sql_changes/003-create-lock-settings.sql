CREATE TABLE ddl_utils.database_lock_settings
(
    id                 integer PRIMARY KEY NOT NULL,
    ddl_lock_timeout   integer             NOT NULL,
    sleep_time         integer             NOT NULL,
    statement_duration integer             NOT NULL,
    created_at         timestamptz         NOT NULL DEFAULT pg_catalog.now(),
    updated_at         timestamptz         NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT database_lock_settings_id_check CHECK (id = 1),
    CONSTRAINT database_lock_settings_ddl_lock_timeout_check CHECK (ddl_lock_timeout >= 0),
    CONSTRAINT database_lock_settings_sleep_time_check CHECK (sleep_time >= 0),
    CONSTRAINT database_lock_settings_statement_duration_check CHECK (statement_duration >= 0)
);

COMMENT ON TABLE ddl_utils.database_lock_settings IS
    'Database-wide lock settings: a single seeded row (id = 1) used as the fallback.';

COMMENT ON COLUMN ddl_utils.database_lock_settings.id IS
    'Fixed identifier, constrained to 1 to keep the table a singleton.';
COMMENT ON COLUMN ddl_utils.database_lock_settings.ddl_lock_timeout IS
    'Per-attempt lock_timeout in milliseconds; 0 disables the timeout.';
COMMENT ON COLUMN ddl_utils.database_lock_settings.sleep_time IS
    'Milliseconds to sleep between lock acquisition attempts; 0 busy-waits.';
COMMENT ON COLUMN ddl_utils.database_lock_settings.statement_duration IS
    'Retry budget: after each failed lock attempt, alter_table gives up once '
        'the elapsed time reaches this value; a single attempt may first wait '
        'up to ddl_lock_timeout.';
COMMENT ON COLUMN ddl_utils.database_lock_settings.created_at IS
    'Row creation time.';
COMMENT ON COLUMN ddl_utils.database_lock_settings.updated_at IS
    'Last update time, maintained by the set_updated_at() trigger.';

INSERT INTO ddl_utils.database_lock_settings (id, ddl_lock_timeout, sleep_time, statement_duration)
VALUES (1, 100, 1000, 30000);

CREATE TABLE ddl_utils.schema_lock_settings
(
    schema_name        text PRIMARY KEY NOT NULL,
    ddl_lock_timeout   integer          NOT NULL,
    sleep_time         integer          NOT NULL,
    statement_duration integer          NOT NULL,
    created_at         timestamptz      NOT NULL DEFAULT pg_catalog.now(),
    updated_at         timestamptz      NOT NULL DEFAULT pg_catalog.now(),
    CONSTRAINT schema_lock_settings_ddl_lock_timeout_check CHECK (ddl_lock_timeout >= 0),
    CONSTRAINT schema_lock_settings_sleep_time_check CHECK (sleep_time >= 0),
    CONSTRAINT schema_lock_settings_statement_duration_check CHECK (statement_duration >= 0)
);

COMMENT ON TABLE ddl_utils.schema_lock_settings IS
    'Per-schema lock settings that override the database defaults.';

COMMENT ON COLUMN ddl_utils.schema_lock_settings.schema_name IS
    'Schema name; stored as given, not resolved against pg_namespace.';
COMMENT ON COLUMN ddl_utils.schema_lock_settings.ddl_lock_timeout IS
    'Per-attempt lock_timeout in milliseconds; 0 disables the timeout.';
COMMENT ON COLUMN ddl_utils.schema_lock_settings.sleep_time IS
    'Milliseconds to sleep between lock acquisition attempts; 0 busy-waits.';
COMMENT ON COLUMN ddl_utils.schema_lock_settings.statement_duration IS
    'Retry budget: after each failed lock attempt, alter_table gives up once '
        'the elapsed time reaches this value; a single attempt may first wait '
        'up to ddl_lock_timeout.';
COMMENT ON COLUMN ddl_utils.schema_lock_settings.created_at IS
    'Row creation time.';
COMMENT ON COLUMN ddl_utils.schema_lock_settings.updated_at IS
    'Last update time, maintained by the set_updated_at() trigger.';

CREATE TABLE ddl_utils.table_lock_settings
(
    schema_name        text        NOT NULL,
    table_name         text        NOT NULL,
    ddl_lock_timeout   integer     NOT NULL,
    sleep_time         integer     NOT NULL,
    statement_duration integer     NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT pg_catalog.now(),
    updated_at         timestamptz NOT NULL DEFAULT pg_catalog.now(),
    PRIMARY KEY (schema_name, table_name),
    CONSTRAINT table_lock_settings_ddl_lock_timeout_check CHECK (ddl_lock_timeout >= 0),
    CONSTRAINT table_lock_settings_sleep_time_check CHECK (sleep_time >= 0),
    CONSTRAINT table_lock_settings_statement_duration_check CHECK (statement_duration >= 0)
);

COMMENT ON TABLE ddl_utils.table_lock_settings IS
    'Per-table lock settings that override the schema and database defaults.';

COMMENT ON COLUMN ddl_utils.table_lock_settings.schema_name IS
    'Schema name; stored as given, not resolved against pg_class.';
COMMENT ON COLUMN ddl_utils.table_lock_settings.table_name IS
    'Table name; stored as given, not resolved against pg_class.';
COMMENT ON COLUMN ddl_utils.table_lock_settings.ddl_lock_timeout IS
    'Per-attempt lock_timeout in milliseconds; 0 disables the timeout.';
COMMENT ON COLUMN ddl_utils.table_lock_settings.sleep_time IS
    'Milliseconds to sleep between lock acquisition attempts; 0 busy-waits.';
COMMENT ON COLUMN ddl_utils.table_lock_settings.statement_duration IS
    'Retry budget: after each failed lock attempt, alter_table gives up once '
        'the elapsed time reaches this value; a single attempt may first wait '
        'up to ddl_lock_timeout.';
COMMENT ON COLUMN ddl_utils.table_lock_settings.created_at IS
    'Row creation time.';
COMMENT ON COLUMN ddl_utils.table_lock_settings.updated_at IS
    'Last update time, maintained by the set_updated_at() trigger.';
