-- Function: create_partition_table
-- Purpose: Creates a new partition for a partitioned table based on specified date and interval
-- Parameters:
--   table_name: Name of the parent partitioned table
--   partition_date: Starting date for the partition
--   interval_unit: Time interval unit ('month' or 'year')
-- Example:
--   SELECT create_partition_table('table_test', '2023-01-01', 'month');
--   Creates a partition named 'table_test_202301' for January 2023 data

CREATE OR REPLACE FUNCTION create_partition_table(
    table_name VARCHAR,
    partition_date DATE,
    interval_unit TEXT
) RETURNS VOID AS
$$
DECLARE
    date_format TEXT;
BEGIN
    -- Determine the date format based on the interval unit
    IF interval_unit = 'month' THEN
        date_format := 'YYYYMM'; -- Format for monthly partitions (e.g., 202301)
    ELSIF interval_unit = 'year' THEN
        date_format := 'YYYY'; -- Format for yearly partitions (e.g., 2023)
    ELSE
        RAISE EXCEPTION 'Unsupported interval unit: %', interval_unit;
    END IF;

    -- Dynamically create the partition table
    EXECUTE FORMAT('
        CREATE TABLE %s_%s PARTITION OF %s
            FOR VALUES FROM (%L) TO (%L);',
                   table_name, -- Base table name
                   TO_CHAR(partition_date, date_format), -- Partition suffix (e.g., 202301)
                   table_name, -- Parent table name
                   partition_date, -- Partition start date (inclusive)
                   (partition_date + ('1 ' || interval_unit)::interval) -- Partition end date (exclusive)
            );
END;
$$ LANGUAGE plpgsql;