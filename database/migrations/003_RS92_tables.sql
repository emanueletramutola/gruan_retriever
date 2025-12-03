-- Table: rs92_header
-- Purpose: Stores comprehensive header metadata for GRUAN RS92 radiosonde observations
-- Description: Contains detailed instrument metadata, station information, processing history,
--              and quality control data for Vaisala RS92 radiosondes in the GRUAN network

DROP TABLE IF EXISTS rs92_header CASCADE;

CREATE TABLE rs92_header
(
    report_id                         serial,
    comment                           character varying(255),
    conventions                       character varying(255),
    g_ascent_balloonnumber            character varying(255),
    g_ascent_balloontype              character varying(255),
    g_ascent_burstpointaltitude       character varying(255),
    g_ascent_burstpointpressure       character varying(255),
    g_ascent_comment                  character varying(255),
    g_ascent_fillingweight            character varying(255),
    g_ascent_grossweight              character varying(255),
    g_ascent_id                       character varying(255),
    g_ascent_includedescent           character varying(255),
    g_ascent_payload                  character varying(255),
    g_ascent_precipitablewatercolumn  character varying(255),
    g_ascent_precipitablewatercolumnu character varying(255),
    g_ascent_standardtime             timestamp with time zone,
    g_ascent_starttime                character varying(255),
    g_ascent_tropopauseheight         character varying(255),
    g_ascent_tropopausepottemperature character varying(255),
    g_ascent_tropopausepressure       character varying(255),
    g_ascent_tropopausetemperature    character varying(255),
    g_ascent_unwindertype             character varying(255),
    g_general_filetypeversion         character varying(255),
    g_general_sitecode                character varying(255),
    g_general_siteinstitution         character varying(255),
    g_general_sitename                character varying(255),
    g_general_sitewmoid               character varying(255),
    g_general_timestamp               character varying(255),
    g_instrument_comment              character varying(255),
    g_instrument_manufacturer         character varying(255),
    g_instrument_serialnumber         character varying(255),
    g_instrument_softwareversion      character varying(255),
    g_instrument_telemetrysonde       character varying(255),
    g_instrument_type                 character varying(255),
    g_instrument_typefamily           character varying(255),
    g_instrument_weight               character varying(255),
    g_measuringsystem_altitude        real,
    g_measuringsystem_id              character varying(255),
    g_measuringsystem_latitude        real,
    g_measuringsystem_longitude       real,
    g_measuringsystem_type            character varying(255),
    g_product_code                    character varying(255),
    g_product_description             character varying(255),
    g_product_history                 character varying(255),
    g_product_id                      character varying(255),
    g_product_level                   character varying(255),
    g_product_leveldescription        character varying(255),
    g_product_name                    character varying(255),
    g_product_orgresolution           character varying(255),
    g_product_processingcode          character varying(255),
    g_product_producer                character varying(255),
    g_product_references              character varying(255),
    g_product_status                  character varying(255),
    g_product_statusdescription       character varying(255),
    g_product_version                 character varying(255),
    g_surfaceobs_pressure             character varying(255),
    g_surfaceobs_relativehumidity     character varying(255),
    g_surfaceobs_temperature          character varying(255),
    history                           character varying(255),
    institution                       character varying(255),
    "references"                      character varying(255),
    source                            character varying(255),
    title                             character varying(255),
    idstation_pk                      int
);

-- Table: rs92_data
-- Purpose: Stores atmospheric measurement data from GRUAN RS92 radiosonde observations
-- Description: Contains vertical profiles of temperature, humidity, pressure, wind, and
--              radiation data with uncertainty estimates for Vaisala RS92 instruments

DROP TABLE IF EXISTS rs92_data CASCADE;

CREATE TABLE rs92_data
(
    observation_id        serial,
    alt                   real,
    "asc"                 real,
    cor_rh                real,
    cor_temp              real,
    fp                    real,
    geopot                real,
    lat                   real,
    lon                   real,
    press                 real,
    res_rh                real,
    rh                    real,
    swrad                 real,
    temp                  real,
    time                  real,
    u                     real,
    u_alt                 real,
    u_cor_rh              real,
    u_cor_temp            real,
    u_press               real,
    u_rh                  real,
    u_std_rh              real,
    u_std_temp            real,
    u_swrad               real,
    u_temp                real,
    u_wdir                real,
    u_wspeed              real,
    v                     real,
    wdir                  real,
    wspeed                real,
    wvmr                  real,
    g_ascent_standardtime timestamp with time zone,
    idstation_pk          int
) PARTITION BY RANGE (g_ascent_standardtime);

-- Anonymous code block to create yearly partitions for RS92 data table
-- Creates partitions from 2004 (RS92 introduction) to 2025
DO
$$
    DECLARE
        start_date    date := '2004-01-01'::date;
        end_date      date := '2025-12-31'::date;
        step_date     date := start_date;
        interval_unit text := 'year';
    BEGIN
        -- Loop through each year from start_date to end_date
        WHILE step_date <= end_date
            LOOP
                -- Create partition for rs92_data table for current year
                PERFORM create_partition_table('rs92_data', step_date, interval_unit);

                -- Move to the next partition interval (increment by 1 year)
                step_date := date_trunc(interval_unit, step_date) + ('1 ' || interval_unit)::interval;
            END LOOP;
    END
$$;