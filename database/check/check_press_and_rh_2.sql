DO $$
DECLARE
    r RECORD;
    inserted_count INTEGER;
BEGIN
    FOR r IN
        SELECT table_name
        FROM public.data_summary_press_rh
        WHERE min_rh < -5
    LOOP
        -- Print a message before processing
        RAISE NOTICE 'Processing table: % ...', r.table_name;

        -- Execute the dynamic INSERT query
        EXECUTE format(
            'INSERT INTO ema_min_rh SELECT DISTINCT g_product_id FROM %I WHERE rh < -5',
            r.table_name
        );

        -- Get the number of rows inserted by the last EXECUTE statement
        GET DIAGNOSTICS inserted_count = ROW_COUNT;

        -- Print confirmation with the row count
        RAISE NOTICE 'Completed %: % row(s) inserted.', r.table_name, inserted_count;
    END LOOP;

    RAISE NOTICE 'Operation completed successfully!';
END $$;