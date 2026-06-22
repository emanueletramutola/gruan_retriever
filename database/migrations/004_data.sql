DROP TABLE IF EXISTS data CASCADE;

CREATE TABLE data
(
    -- Common identifier
    observation_id                     serial,
    idstation_pk                       int,

    -- Timestamps
--     g_measurement_standardtime         timestamp with time zone,
--     g_ascent_standardtime              timestamp with time zone,
    report_timestamp                   timestamp with time zone,

    -- Position and coordinates
    lat                                real,       -- Latitude
    lat_raw                            real,       -- Raw latitude
    lat_raw_flags                      int,        -- Flags of raw latitude
    lat_sm                             double precision, -- Strong smoothed trajectory latitude
    lat_uc                             real,       -- Uncertainty of latitude
    lat_uc_tcor                        real,       -- Temporal correlated uncertainty of latitude
    lat_uc_ucor                        real,       -- Uncorrelated uncertainty of latitude
    latmf                              double precision, -- Latitude conversion factor
    lon                                real,       -- Longitude
    lon_raw                            real,       -- Raw longitude
    lon_raw_flags                      int,        -- Flags of raw longitude
    lon_sm                             double precision, -- Strong smoothed trajectory longitude
    lon_uc                             real,       -- Uncertainty of longitude
    lon_uc_tcor                        real,       -- Temporal correlated uncertainty of longitude
    lon_uc_ucor                        real,       -- Uncorrelated uncertainty of longitude
    lonmf                              double precision, -- Longitude conversion factor
    posx_raw                           double precision, -- Raw X coordinate
    posx_raw_flags                     real,       -- Flags of raw X coordinate
    posy_raw                           double precision, -- Raw Y coordinate
    posy_raw_flags                     real,       -- Flags of raw Y coordinate
    posz_raw                           double precision, -- Raw Z coordinate
    posz_raw_flags                     real,       -- Flags of raw Z coordinate

    -- Altitude and height measurements
    alt                                real,       -- Altitude
    alt_uc                             real,       -- Uncertainty of default altitude
    alt_amsl                           real,       -- Altitude above mean sea level
    alt_amsl_uc                        real,       -- Uncertainty of altitude above mean sea level
    alt_amsl_uc_cor                    real,
    alt_amsl_uc_tcor                   real,       -- Temporal correlated uncertainty of altitude above mean sea level
    alt_amsl_uc_ucor                   real,       -- Uncorrelated uncertainty of altitude above mean sea level
    alt_raw                            real,       -- Raw altitude
    alt_raw_flags                      int,        -- Flags of raw altitude
    alt_amsl_uc_cor_vdop               real,
    alt_amsl_uc_cor_release_transition real,
    alt_amsl_uc_cor_dac_timing         real,
    alt_amsl_uc_cor_geoid              real,       -- Geoid uncertainty
    alt_amsl_uc_ucor_ma                real,
    alt_flag_release_transition        boolean,
    alt_amsl_corr_release_transition   real,
    alt_amsl_corr_ma                   real,
    alt_gph                            real,       -- Geopotential height
    alt_gph_uc_cor                     real,       -- Correlated uncertainty of geopotential height
    alt_gph_uc_tcor                    real,       -- Temporal correlated uncertainty of geopotential height
    alt_gph_uc_ucor                    real,       -- Uncorrelated uncertainty of geopotential height
    alt_gph_uc                         real,       -- Uncertainty of geopotential height
    alt_wgs84                          real,       -- Altitude WGS84
    alt_wgs84_uc                       real,       -- Uncertainty of altitude WGS84
    alt_wgs84_uc_tcor                  real,       -- Temporal correlated uncertainty of altitude WGS84
    alt_wgs84_uc_ucor                  real,       -- Uncorrelated uncertainty of altitude WGS84
    geoid                              real,       -- Geoid difference
    geoid_corr                         real,       -- Geoid correction

    -- Temperature measurements
    temp                               real,       -- Temperature
    temp_corr                          real,       -- Correction of air_temperature
    temp_corr_spike_filtered           real,
    temp_uc_ucor_spike_filter          real,
    temp_corr_ma                       real,
    temp_uc_ucor_ma                    real,
    temp_corr_rad                      real,       -- Radiation correction of air_temperature
    temp_corr_rad_heating              real,       -- Radiation correction of air_temperature
    temp_corr_rad_orientation          real,       -- Radiation correction of air_temperature
    temp_uc                            real,       -- Uncertainty of air_temperature
    temp_uc_cor                        real,       -- Correlated uncertainty of air_temperature
    temp_uc_ucor                       real,       -- Uncorrelated uncertainty of air_temperature
    temp_uc_cor_rad_corr_albedo        real,
    temp_uc_cor_rad_corr_vent          real,
    temp_uc_cor_calibration            real,       -- Temperature
    temp_uc_cor_chamber                real,       -- Temperature
    temp_uc_ucor_rad_corr_orientation  real,
    temp_flag_spike_filter             boolean,
    temp_flag_corr_rad                 boolean,
    temp_corr_sm                       real,       -- Smoothing correction of temperature
    temp_raw                           real,       -- Raw temperature
    temp_raw_flags                     real,       -- Flags of raw temperature
    temp_res                           real,       -- Time resolution of temperature
    temp_uc_rad                        real,       -- Uncertainty of temperature related to TRC
    temp_uc_scor                       real,       -- Spatial correlated uncertainty of temperature
    temp_uc_scor_rad                   real,       -- Spatial correlated uncertainty of temperature related to TRC
    temp_uc_tcor                       real,       -- Temporal correlated uncertainty of temperature
    temp_uc_tcor_cal                   real,       -- Uncertainty of temperature related to calibration
    temp_uc_tcor_rad                   real,       -- Temporal correlated uncertainty of temperature related to TRC
    temp_uc_ucor_rad                   real,       -- Uncorrelated uncertainty of temperature related to TRC
    temp_uc_ucor_sm                    real,       -- Uncertainty of temperature related to smoothing

    -- Humidity measurements
    rh                                 real,       -- Relative humidity
    rh_corr                            real,       -- Correction of relative humidity
    rh_uc                              real,       -- Uncertainty of relative humidity
    rh_uc_cor                          real,       -- Correlated uncertainty of relative_humidity
    rh_uc_ucor                         real,       -- Uncorrelated uncertainty of relative humidity
    rh_res                             real,       -- Time resolution of relative humidity
    res_rh                             real,       -- time-resolution of relative_humidity
    negative_rh_flg                    boolean,
    rh_corr_tl                         real,       -- Timelag correction of relative humidity
    rh_corr_contamination              real,
    rh_corr_hysteresis                 real,
    rh_corr_tud                        real,
    rh_corr_tsta                       real,
    rh_uc_cor_sensor_calib             real,
    rh_tl_corrected                    real,
    rh_uc_cor_tl_correction            real,
    rh_contamination_filtered          real,
    rh_uc_cor_contamination_filter     real,
    rh_uc_ucor_contamination_filter    real,
    rh_hysteresis_corrected            real,
    rh_uc_cor_hysteresis_correction    real,
    rh_uc_ucor_hysteresis_correction   real,
    rh_tud_corrected                   real,
    rh_uc_cor_tud_correction           real,
    rh_uc_ucor_tud_correction          real,
    rh_uc_cor_tsta_correction          real,
    rh_uc_ucor_tsta_correction         real,
    rh_corr_sm                         real,       -- Smoothing correction of relative humidity
    rh_corr_trc                        real,       -- Radiation temperature correction of relative humidity
    rh_raw                             real,       -- Raw humidity
    rh_raw_flags                       real,       -- Flags for raw relative humidity
    rh_uc_tcor                         real,       -- Temporal correlated uncertainty of relative humidity
    rh_uc_tcor_cal                     real,       -- Uncertainty of relative humidity related to calibration
    rh_uc_tcor_tint                    real,       -- Uncertainty of relative humidity related to internal T sensor
    rh_uc_tcor_tlc                     real,       -- Temporal correlated uncertainty of relative humidity related to TLC
    rh_uc_tlc                          real,       -- Uncertainty of relative humidity related to TLC
    rh_uc_ucor_sm                      real,       -- Uncertainty of relative humidity related to smoothing
    rh_uc_ucor_tair                    real,       -- Uncertainty of relative humidity related to air T sensor
    rh_uc_ucor_tlc                     real,       -- Uncorrelated uncertainty of relative humidity related to TLC

    -- Pressure measurements
    press                              real,       -- Pressure
    press_uc                           real,       -- Uncertainty of air pressure
    press_uc_cor                       real,       -- Correlated uncertainty of air pressure
    press_uc_ucor                      real,       -- Uncorrelated uncertainty of air pressure
    press_uc_cor_tv                    real,       -- Correlated uncertainty of air pressure
    press_uc_psurf_cor                 real,       -- Correlated uncertainty of air pressure
    press_uc_cor_height_from_rp        real,       -- Correlated uncertainty of air pressure
    press_uc_ucor_tv                   real,       -- Uncorrelated uncertainty of air pressure
    press_uc_ucor_thickness            real,       -- Uncorrelated uncertainty of air pressure
    press_gnss                         real,       -- GNSS pressure
    press_gnss_uc                      real,       -- Uncertainty of GNSS pressure
    press_gnss_uc_tcor                 real,       -- Temporal correlated uncertainty of GNSS pressure
    press_gnss_uc_ucor                 real,       -- Uncorrelated uncertainty of GNSS pressure
    press_sens                         real,       -- Sensor pressure
    press_sens_corr                    real,       -- Correction of sensor pressure
    press_sens_corr_cal                real,       -- Calibration correction of sensor pressure
    press_sens_corr_sm                 real,       -- Smoothing correction of sensor pressure
    press_sens_raw                     real,       -- Raw sensor pressure
    press_sens_raw_flags               real,       -- Flags of raw sensor pressure
    press_sens_uc                      real,       -- Uncertainty of sensor pressure
    press_sens_uc_tcor                 real,       -- Temporal correlated uncertainty of sensor pressure
    press_sens_uc_tcor_cal             real,       -- Uncertainty of sensor pressure related to calibration
    press_sens_uc_ucor                 real,       -- Uncorrelated uncertainty of sensor pressure
    press_sens_uc_ucor_sm              real,       -- Uncertainty of sensor pressure related to smoothing

    -- Wind measurements (main)
    u                                  real,       -- Zonal Wind
    v                                  real,       -- Meridional Wind
    wzon                               real,       -- Zonal wind component
    wzon_uc                            real,       -- Uncertainty of zonal wind component
    wzon_uc_ucor                       real,       -- Uncorrelated uncertainty of zonal wind component
    wzon_uc_ucor_filter                real,
    wmeri                              real,       -- Meridional wind component
    wmeri_uc                           real,       -- Uncertainty of meridional wind component
    wmeri_uc_ucor                      real,       -- Uncorrelated uncertainty of meridional wind component
    wmeri_uc_ucor_filter               real,
    wspeed                             real,       -- Wind speed
    wspeed_uc                          real,       -- Uncertainty of wind speed
    wspeed_uc_ucor                     real,       -- Uncorrelated uncertainty of wind speed
    wdir                               real,       -- Wind direction
    wdir_uc                            real,       -- Uncertainty of wind direction
    wdir_uc_ucor                       real,       -- Uncorrelated uncertainty of wind direction
    wzon_perturb                       real,
    wmeri_perturb                      real,
    wspeed_perturb                     real,

    -- Wind measurements (MV)
    wzon_mv                            real,       -- Zonal wind component
    wzon_mv_uc                         real,
    wzon_mv_uc_ucor                    real,
    wzon_mv_uc_ucor_filter             real,
    wzon_mv_raw_flags                  int,        -- Flags of raw zonal wind component
    wmeri_mv                           real,       -- Meridional wind component
    wmeri_mv_uc                        real,
    wmeri_mv_uc_ucor                   real,
    wmeri_mv_uc_ucor_filter            real,
    wmeri_mv_raw_flags                 int,        -- Flags of raw meridional wind component
    wspeed_mv                          real,       -- Wind speed
    wspeed_mv_uc                       real,       -- Uncertainty of wind speed
    wspeed_mv_uc_ucor                  real,       -- Uncertainty of wind speed
    wdir_mv                            real,       -- Wind direction
    wdir_mv_uc                         real,       -- Uncertainty of wind direction
    wdir_mv_uc_ucor                    real,       -- Uncertainty of wind direction
    wzon_mv_perturb                    real,
    wmeri_mv_perturb                   real,
    wspeed_mv_perturb                  real,

    -- Wind measurements (DS)
    wzon_ds                            real,       -- Zonal wind component
    wzon_ds_uc                         real,
    wzon_ds_uc_ucor                    real,
    wzon_ds_uc_ucor_filter             real,
    wzon_ds_raw_flags                  int,        -- Flags of raw zonal wind component
    wmeri_ds                           real,       -- Meridional wind component
    wmeri_ds_uc                        real,
    wmeri_ds_uc_ucor                   real,
    wmeri_ds_uc_ucor_filter            real,
    wmeri_ds_raw_flags                 int,        -- Flags of raw meridional wind component
    wspeed_ds                          real,       -- Wind speed
    wspeed_ds_uc                       real,       -- Uncertainty of wind speed
    wspeed_ds_uc_ucor                  real,       -- Uncertainty of wind speed
    wdir_ds                            real,       -- Wind direction
    wdir_ds_uc                         real,       -- Uncertainty of wind direction
    wdir_ds_uc_ucor                    real,       -- Uncertainty of wind direction
    wzon_ds_perturb                    real,
    wmeri_ds_perturb                   real,
    wspeed_ds_perturb                  real,

    -- Additional wind corrected/smoothed
    wzon_corr_sm                       real,       -- Smoothing correction of zonal wind component
    wzon_raw                           real,       -- Raw zonal wind component
    wzon_raw_flags                     real,       -- Flags of raw zonal wind component
    wzon_uc_ucor_gnss                  real,       -- Uncertainty of zonal wind component related to GNSS
    wzon_uc_ucor_sm                    real,       -- Uncertainty of zonal wind component related to smoothing
    wmeri_corr_sm                      real,       -- Smoothing correction of meridional wind component
    wmeri_raw                          real,       -- Raw meridional wind component
    wmeri_raw_flags                    real,       -- Flags of raw meridional wind component
    wmeri_uc_ucor_gnss                 real,       -- Uncertainty of meridional wind component related to GNSS
    wmeri_uc_ucor_sm                   real,       -- Uncertainty of meridional wind component related to smoothing
    wspeed_corr_sm                     real,       -- Smoothing correction of wind speed
    wspeed_uc_ucor_gnss                real,       -- Uncertainty of wind speed related to GNSS
    wspeed_uc_ucor_sm                  real,       -- Uncertainty of wind speed related to smoothing
    wdir_corr_sm                       real,       -- Smoothing correction of wind direction
    wdir_uc_ucor_gnss                  real,       -- Uncertainty of wind direction related to GNSS
    wdir_uc_ucor_sm                    real,       -- Uncertainty of wind direction related to smoothing
    wdir_usm                           real,       -- Unsmoothed wind direction
    wspeed_usm                         real,       -- Unsmoothed wind speed

    -- Vertical speed
    vspeed_raw                         real,
    vspeed                             real,       -- Ascent speed
    vspeed_uc                          real,       -- Uncertainty of ascent speed
    vspeed_uc_ucor                     real,        -- Uncorrelated uncertainty of ascent speed

    -- Moisture and water vapor
    fp                                 real,       -- Frost point temperature
    fp_uc                              real,       -- Frost point temperature
    mr_mass                            real,       -- Water vapor mass mixing ratio
    mr_mass_uc                         real,       -- Uncertainty of water vapor mass mixing ratio
    mr_vol                             real,       -- Water vapor volume mixing ratio
    mr_vol_uc                          real,       -- Uncertainty of water vapor volume mixing ratio
    wvpp_sat                           real,       -- Water vapour saturation pressure
    wvpp_sat_uc                        real,       -- Uncertainty of water vapour saturation pressure
    wvpp                               real,       -- Water vapour partial pressure
    wvpp_uc                            real,       -- Uncertainty of water vapour partial pressure
    wvpp_uc_scor                       real,       -- Spatial correlated uncertainty of water vapour partial pressure
    wvpp_uc_tcor                       real,       -- Temporal correlated uncertainty of water vapour partial pressure
    wvpp_uc_ucor                       real,       -- Uncorrelated uncertainty of water vapour partial pressure
    wvmr_mass                          real,       -- Water vapour mass mixing ratio
    wvmr_mass_uc                       real,       -- Uncertainty of water vapour mass mixing ratio
    wvmr_mass_uc_scor                  real,       -- Spatial correlated uncertainty of water vapour mass mixing ratio
    wvmr_mass_uc_tcor                  real,       -- Temporal correlated uncertainty of water vapour mass mixing ratio
    wvmr_mass_uc_ucor                  real,       -- Uncorrelated uncertainty of water vapour mass mixing ratio
    wvmr_vol                           real,       -- Water vapour volume mixing ratio
    wvmr_vol_uc                        real,       -- Uncertainty of water vapour volume mixing ratio
    wvmr_vol_uc_scor                   real,       -- Spatial correlated uncertainty of water vapour volume mixing ratio
    wvmr_vol_uc_tcor                   real,       -- Temporal correlated uncertainty of water vapour volume mixing ratio
    wvmr_vol_uc_ucor                   real,       -- Uncorrelated uncertainty of water vapour volume mixing ratio
    wvsp                               real,       -- Water vapour saturation pressure
    wvsp_uc                            real,       -- Uncertainty of water vapour saturation pressure
    wvsp_uc_scor                       real,       -- Spatial correlated uncertainty of water vapour saturation pressure
    wvsp_uc_tcor                       real,       -- Temporal correlated uncertainty of water vapour saturation pressure
    wvsp_uc_ucor                       real,       -- Uncorrelated uncertainty of water vapour saturation pressure

    -- Radiation and solar
    swdir                              real,       -- Solar direct flux
    swdir_uc                           real,       -- Uncertainty of solar direct flux
    swdir_uc_scor                      real,       -- Spatial correlated uncertainty of solar direct flux
    swdif                              real,       -- Solar diffuse flux
    swdif_uc                           real,       -- Uncertainty of solar diffuse flux
    swdif_uc_scor                      real,       -- Spatial correlated uncertainty of solar diffuse flux
    swrad                              real,       -- Short wave radiation from model
    penper                             real,       -- Pendulum period
    penrad                             real,       -- Pendulum radius
    penrad_uc                          real,       -- Uncertainty of pendulum radius
    penrad_uc_ucor                     real,       -- Uncorrelated uncertainty of pendulum radius
    penspeed                           real,       -- Pendulum speed
    penspeed_uc                        real,       -- Uncertainty of pendulum speed
    penspeed_uc_ucor                   real,       -- Uncorrelated uncertainty of pendulum speed
    sza                                real,       -- Solar zenith angle
    saa                                real,       -- Solar azimuth angle

    -- Miscellaneous measurements
    thum                               real,       -- Temperature of humidity sensor
    thum_uc                            real,       -- Uncertainty of temperature of humidity sensor
    thum_raw                           real,       -- Raw temperature of humidity sensor
    thum_raw_flags                     real,       -- Flags of raw internal humidity temperature
    sea                                real,       -- Solar elevation angle
    vent                               real,       -- Ascent speed
    vent_uc                            real,       -- Uncertainty of ventilation speed
    vent_uc_ucor                       real,       -- Uncorrelated uncertainty of ventilation speed
    band                               real,       -- Measurement band flag
    ciwv                               real,       -- Cumulated integrated water vapour
    ciwv_uc                            real,       -- Uncertainty of cumulated integrated water vapour
    dorn                               real,       -- Day or night
    dp                                 real,       -- Dew point temperature
    dp_uc                              real,       -- Uncertainty of dew point temperature
    dp_uc_scor                         real,       -- Spatial correlated uncertainty of dew point temperature
    dp_uc_tcor                         real,       -- Temporal correlated uncertainty of dew point temperature
    dp_uc_ucor                         real,       -- Uncorrelated uncertainty of dew point temperature
    grav                               real,       -- Acceleration of gravity
    hca                                real,       -- Horizon correction angle
    hdop_raw                           real,       -- HDOP
    icesat                             real,       -- Humidity of ice saturation
    ihum_raw                           real,       -- Raw internal humidity
    ihum_raw_flags                     real,       -- Flags of raw internal humidity
    tau_rh                             real,       -- Timelag of humidity sensor
    tau_rh_uc                          real,       -- Uncertainty of timelag of humidity sensor
    tau_rh_uc_tcor                     real,       -- Temporal correlated uncertainty of timelag of humidity sensor
    vdop_raw                           real,       -- VDOP - Vertical Dilution of Precision

    -- Combined/corrected variables
    "asc"                              real,       -- Ascent/Descent Speed
    cor_rh                             real,       -- Correction of relative_humidity
    cor_temp                           real,       -- Correction of air_temperature
    geopot                             real,       -- Geopotential Altitude
    wvmr                               real,       -- Water Vapor Volume Mixing Ratio

    -- Uncertainty fields
    u_alt                              real,       -- Uncertainty of altitude
    u_cor_rh                           real,       -- Correlated uncertainty of relative_humidity
    u_cor_temp                         real,       -- Correlated uncertainty of air_temperature
    u_press                            real,       -- Uncertainty of air_pressure
    u_rh                               real,       -- Uncertainty of relative_humidity
    u_std_rh                           real,       -- Standard deviation of relative_humidity
    u_std_temp                         real,       -- Standard deviation of air_temperature
    u_swrad                            real,       -- Uncertainty of short_wave_radiation
    u_temp                             real,       -- Uncertainty of air_temperature
    u_wdir                             real,       -- Uncertainty of wind_from_direction
    u_wspeed                           real,       -- Uncertainty of wind_speed

    td                                 real,
    theta                              real,
    theta_e                            real,
    parcel_profile                     real
) PARTITION BY RANGE (report_timestamp);

DO
$$
    DECLARE
        start_date    date := '2004-01-01'::date;
        end_date      date := '2030-12-31'::date;
        step_date     date := start_date;
        interval_unit text := 'month';
    BEGIN
        -- Loop through each year from start_date to end_date
        WHILE step_date <= end_date
            LOOP
                -- Create partition for radiosonde_data table for current year
                PERFORM create_partition_table('data', step_date, interval_unit);

                -- Move to the next partition interval (increment by 1 year)
                step_date := date_trunc(interval_unit, step_date) + ('1 ' || interval_unit)::interval;
            END LOOP;
    END
$$;