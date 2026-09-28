-- ============================================================
-- Target summary table
-- Using CREATE TABLE IF NOT EXISTS instead of DROP + CREATE
-- avoids losing existing data, grants, and indexes on rerun.
-- ============================================================
CREATE TABLE IF NOT EXISTS public.data_summary_press_rh (
    table_name   text PRIMARY KEY,
    row_count    bigint,
    min_press    numeric,
    min_rh       numeric,
    max_press    numeric,
    max_rh       numeric,
    mean_press   numeric,
    mean_rh      numeric,
    stddev_press numeric,
    stddev_rh    numeric,
    computed_at  timestamptz DEFAULT clock_timestamp()
);

-- If you really want a clean rebuild each run, use TRUNCATE
-- instead of DROP, so the table definition/permissions persist:
-- TRUNCATE TABLE public.data_summary_press_rh;

-- ============================================================
-- Loop over monthly partitions and (re)compute stats
-- ============================================================
DO $$
DECLARE
    curr_date        date := '2004-07-01'::date;
    end_date         date := '2026-09-01'::date;
    target_table_name text;
    started_at       timestamptz;
    processed_count  int := 0;
    skipped_count    int := 0;
    error_count      int := 0;
BEGIN
    started_at := clock_timestamp();

    WHILE curr_date <= end_date LOOP
        target_table_name := 'data_' || to_char(curr_date, 'YYYYMM');

        -- to_regclass is faster than an information_schema lookup
        -- and returns NULL if the table doesn't exist.
        IF to_regclass('public.' || target_table_name) IS NOT NULL THEN

            -- Wrap each table in its own block so one bad/locked
            -- partition doesn't abort the whole batch.
            BEGIN
                EXECUTE format(
                    'INSERT INTO public.data_summary_press_rh (
                        table_name, row_count,
                        min_press, min_rh, max_press, max_rh,
                        mean_press, mean_rh, stddev_press, stddev_rh
                     )
                     SELECT %L,
                            COUNT(*),
                            MIN(press), MIN(rh),
                            MAX(press), MAX(rh),
                            AVG(press), AVG(rh),
                            STDDEV(press), STDDEV(rh)
                     FROM public.%I
                     ON CONFLICT (table_name) DO UPDATE SET
                        row_count    = EXCLUDED.row_count,
                        min_press    = EXCLUDED.min_press,
                        min_rh       = EXCLUDED.min_rh,
                        max_press    = EXCLUDED.max_press,
                        max_rh       = EXCLUDED.max_rh,
                        mean_press   = EXCLUDED.mean_press,
                        mean_rh      = EXCLUDED.mean_rh,
                        stddev_press = EXCLUDED.stddev_press,
                        stddev_rh    = EXCLUDED.stddev_rh,
                        computed_at  = clock_timestamp();',
                    target_table_name, target_table_name
                );

                processed_count := processed_count + 1;
                RAISE NOTICE 'Processed table: %', target_table_name;

            EXCEPTION WHEN OTHERS THEN
                error_count := error_count + 1;
                RAISE WARNING 'Failed to process table % : %',
                    target_table_name, SQLERRM;
            END;

        ELSE
            skipped_count := skipped_count + 1;
            RAISE NOTICE 'Table not found (skipped): %', target_table_name;
        END IF;

        curr_date := curr_date + interval '1 month';
    END LOOP;

    RAISE NOTICE 'Done in %. Processed: %, skipped: %, errors: %',
        clock_timestamp() - started_at, processed_count, skipped_count, error_count;
END $$;