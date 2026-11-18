CREATE OR REPLACE FUNCTION ddl_utils_lib.alter_table(
    i_schema_name ddl_utils.non_null_text,
    i_table_name ddl_utils.non_null_text,
    i_alter_table_fragment ddl_utils.non_null_text,
    i_ddl_lock_timeout ddl_utils.non_negative_integer,
    i_sleep_time ddl_utils.non_negative_integer,
    i_statement_duration ddl_utils.non_negative_integer
)
    RETURNS void
    LANGUAGE plpgsql
    SECURITY INVOKER
AS
$$
DECLARE
    l_statement             text;
    l_started_at            timestamptz;
    l_previous_lock_timeout text;
BEGIN
    -- The EXECUTE below is intentionally not sanitised: the fragment is
    -- caller-provided DDL and the function is SECURITY INVOKER (see the guard
    -- below). PlpgsqlCheckTest allow-lists its security warning.
    IF pg_catalog.btrim(i_alter_table_fragment) = '' THEN
        RAISE EXCEPTION 'ddl_utils_lib.alter_table: the alter table fragment must not be blank'
            USING ERRCODE = '22023';
    END IF;

    -- The fragment is arbitrary DDL, so it cannot be parameterised. Reject a
    -- statement separator or comment that appears *outside* quoted text, where
    -- it could terminate the statement or swallow the rest of it. Quoted
    -- literals (DEFAULT 'a;b'), quoted identifiers ("x--y") and dollar-quoted
    -- values are data and must pass. The scanner validates the exact
    -- text that is executed and is shared with has_top_level_comma.
    --
    -- The function is SECURITY INVOKER, so this is a best-effort guard: the
    -- caller already holds the privileges the fragment would use. It is not an
    -- authorization boundary.
    IF ddl_utils_lib.has_unquoted_token(
            i_value => i_alter_table_fragment,
            i_tokens => ARRAY [';', '--', '/*', '*/'],
            i_top_level_only => false
       ) THEN
        RAISE EXCEPTION 'ddl_utils_lib.alter_table: the alter table fragment contains a statement separator or comment'
            USING ERRCODE = '22023';
    END IF;

    l_statement := pg_catalog.format(
            'ALTER TABLE %I.%I %s',
            i_schema_name,
            i_table_name,
            pg_catalog.btrim(i_alter_table_fragment)
                   );
    l_started_at := pg_catalog.clock_timestamp();
    -- Remember the caller's lock_timeout so a successful call does not change
    -- the setting for the rest of the caller's transaction.
    l_previous_lock_timeout := pg_catalog.current_setting('lock_timeout');

    LOOP
        -- lock_timeout is transaction scoped; set it outside the exception
        -- block on every attempt so a caught failure cannot leave it unset.
        -- (Setting it inside the block would be rolled back with the
        -- subtransaction.) A ddl_lock_timeout of 0 waits with no timeout (and
        -- a sleep_time of 0 busy-waits); the statement budget then cannot
        -- apply.
        --
        -- i_statement_duration is a retry budget, not a bound on a single
        -- wait: it is compared after each failed attempt and the loop raises
        -- 55P03 once the elapsed time reaches it. One attempt can therefore
        -- block for up to i_ddl_lock_timeout, and a retry can overshoot the
        -- budget by that plus one i_sleep_time.
        PERFORM pg_catalog.set_config(
                'lock_timeout',
                pg_catalog.format('%sms', i_ddl_lock_timeout),
                true
                );

        BEGIN
            EXECUTE l_statement;
            -- Only the success path needs to restore: set_config(..., true) is
            -- transaction-local and a raised error rolls back the enclosing
            -- (sub)transaction, which reverts the GUC anyway.
            PERFORM pg_catalog.set_config('lock_timeout', l_previous_lock_timeout, true);
            RETURN;
        EXCEPTION
            WHEN lock_not_available THEN
                IF pg_catalog.clock_timestamp() - l_started_at
                    >= i_statement_duration * interval '1 millisecond' THEN
                    RAISE EXCEPTION
                        'ddl_utils_lib.alter_table: could not acquire a lock on %.% within % ms',
                        i_schema_name, i_table_name, i_statement_duration
                        USING ERRCODE = '55P03';
                END IF;
        END;

        PERFORM pg_catalog.pg_sleep(i_sleep_time::double precision / 1000);
    END LOOP;
END;
$$;

COMMENT ON FUNCTION ddl_utils_lib.alter_table IS
    'Internal runner: runs a caller-provided ALTER TABLE fragment with a bounded '
        'lock_timeout and a retry budget (i_statement_duration).';
