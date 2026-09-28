-- Create the target table if it doesn't exist
CREATE TABLE IF NOT EXISTS public.data_summary (
    table_name text PRIMARY KEY,
    min_res_rh numeric,
    min_rh_res numeric,
    max_res_rh numeric,
    max_rh_res numeric,
    computed_at timestamp DEFAULT clock_timestamp()
);

-- Execute the loop to calculate metrics for each child table
DO $$
DECLARE
    curr_date date := '2004-07-01'::date;
    end_date  date := '2026-09-01'::date;
    t_name    text;
    query_str text;
BEGIN
    WHILE curr_date <= end_date LOOP
        -- Build the child table name (e.g., data_200407)
        t_name := 'data_' || to_char(curr_date, 'YYYYMM');

        -- Verify if the partition table actually exists in the current schema
        IF EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name = t_name
        ) THEN
            -- Build dynamic SQL query with UPSERT (INSERT ... ON CONFLICT)
            query_str := format(
                'INSERT INTO public.data_summary (table_name, min_res_rh, min_rh_res, max_res_rh, max_rh_res)
                 SELECT %L,
                        MIN(res_rh),
                        MIN(rh_res),
                        MAX(res_rh),
                        MAX(rh_res)
                 FROM public.%I
                 ON CONFLICT (table_name) DO UPDATE SET
                    min_res_rh = EXCLUDED.min_res_rh,
                    min_rh_res = EXCLUDED.min_rh_res,
                    max_res_rh = EXCLUDED.max_res_rh,
                    max_rh_res = EXCLUDED.max_rh_res,
                    computed_at = clock_timestamp();',
                t_name, t_name
            );

            EXECUTE query_str;
            RAISE NOTICE 'Processed table: %', t_name;
        ELSE
            RAISE NOTICE 'Table not found (skipped): %', t_name;
        END IF;

        -- Move to the next month
        curr_date := curr_date + interval '1 month';
    END LOOP;
END $$;