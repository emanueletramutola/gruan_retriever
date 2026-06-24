#!/usr/bin/env python3
"""
Export GRUAN radiosounding data from PostgreSQL to CDM-compliant NetCDF4.

Output format: long/tidy table (one row per observation × CDM variable),
matching the GRUAN reference network file structure:
  - observed_variable  : uint8 CDM variable code
  - observation_value  : float32 measured value
  - z_coordinate       : float32 altitude [m]
  - z_coordinate_type  : uint8  (0 = altitude above MSL)
  - units / original_units : char
  - uncertainty_value1 / _type1 / _units1  (random,       type=1)
  - uncertainty_value2 / _type2 / _units2  (systematic,   type=2)
  - uncertainty_value5 / _type5 / _units5  (combined,     type=5)
  - cor_rh / cor_temp  : GRUAN-specific humidity/temperature corrections
  - report_timestamp   : int64, CF-units
  - report_id          : char
  - observation_id     : int64
  - primary_station_id : char
  - latitude|station_configuration  / longitude|station_configuration
  - height_of_station_above_sea_level
  - latitude|observations_table / longitude|observations_table
  - index              : int64, dummy coordinate

CDM variable codes used
-----------------------
 73  shortwave_radiation
106  wind_from_direction
107  wind_speed
116  frost_point_temperature
117  geopotential_height
122  vertical_speed_of_radiosonde
123  water_vapour_mixing_ratio
125  altitude
126  air_temperature
138  relative_humidity
139  eastward_wind_speed
140  northward_wind_speed
142  pressure
143  time_since_launch
"""

import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import netCDF4 as nc
import psycopg2
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine
from dotenv import load_dotenv
from typing import Optional
import time
from tqdm import tqdm

# ── tunables ──────────────────────────────────────────────────────────────────
DATA_CHUNK_SIZE = 50_000
DEFLATE_LEVEL   = 3
TIME_UNITS      = "seconds since 1970-01-01 00:00:00"
TIME_CALENDAR   = "proleptic_gregorian"

# ── CDM variable mapping ──────────────────────────────────────────────────────
# Each entry:
#   cdm_code   : int   CDM/OSCAR variable code (fits uint8 if ≤ 255)
#   value_col  : str   DB column for observation_value
#   units      : str   CF units for output
#   uc_rand    : str|None  DB column for random (uncorrelated) uncertainty  → type 1
#   uc_sys     : str|None  DB column for systematic (correlated) uncertainty → type 2
#   uc_tot     : str|None  DB column for combined uncertainty               → type 5
#
# NOTE on units: temperatures are stored in K in GRUAN NetCDF files.
#   If your DB stores °C, add 273.15 in the value_transform lambda below.
#   Pressures: GRUAN uses Pa; if your DB uses hPa multiply by 100.
#   Check/adjust UNIT_TRANSFORMS accordingly.

CDM_VARIABLES = [
    # code var_name_cf_1_4      var_name_cf_1_7       unit   units_str        uc_rand       uc_sys_cf_1_4  uc_sys_cf_1_7,      uc_tot_cf_1_4  uc_tot_cf_1_7
    (126, 'temp',               'temp',               5,     'K',         'u_std_temp', 'u_cor_temp',  'temp_uc_tcor',     'u_temp',      'temp_uc'),
    (138, 'rh',                 'rh',                 1016,  '1',         'u_std_rh',   'u_cor_rh',    'rh_uc_tcor',       'u_rh',        'rh_uc'),
    (106, 'wdir',               'wdir',               110,   'degree',    None,         None,          None,               'u_wdir',      'wdir_uc'),
    (107, 'wspeed',             'wspeed',             731,   'm s-1',     None,         None,          None,               'u_wspeed',    'wspeed_uc'),
    (104, 'u',                  'wzon',               731,   'm s-1',     None,         None,          None,               None,          'wzon_uc'),
    (105, 'v',                  'wmeri',              731,   'm s-1',     None,         None,          None,               None,          'wmeri_uc'),
    (123, 'wvmr',               'wvmr_vol',           788,   'mol mol-1', None,         None,          'wvmr_vol_uc_tcor', None,          'wvmr_vol_uc'),
    (122, 'asc',                'vspeed',             731,   'm s-1',     None,         None,          None,               None,          'vspeed_uc'),
    (117, 'geopot',             'alt_gph',            1,     'm',         None,         None,          'alt_gph_uc_tcor',  None,          'alt_gph_uc'),
    (116, 'fp',                 'fp',                 5,     'K',         None,         None,          None,               None,          'fp_uc'),
    (124, 'res_rh',             'rh_res',             3,     's',         None,         None,          None,               None,          None),
    (73,  'swrad',              None,                 811,   'W m-2',     None,         None,          None,               'u_swrad',     None),
    (125, 'alt',                'alt',                1,     'm',         None,         None,          None,               'u_alt',       'alt_uc'),
    (142, 'press',              'press',              32,    'Pa',        None,         None,          None,               'u_press',     'press_uc'),
    (143, '_time_since_launch', '_time_since_launch', 3,     's',         None,         None,          None,               None,          None),
]

# Optional unit conversions applied BEFORE writing.
# key = value_col, value = (scale, offset)  →  output = value * scale + offset
# Example: DB stores °C → output K: ('temp_corr', (1.0, 273.15))
#          DB stores hPa → output Pa: ('press', (100.0, 0.0))
UNIT_TRANSFORMS: dict[str, tuple[float, float]] = {
    # from ppmv to mol mol-1 (multiply by 1e-6, add 0)
    'wvmr_vol':         (1e-6, 0.0),
    'wvmr_vol_uc':      (1e-6, 0.0),  # Total uncertainty CF 1.7
    'wvmr_vol_uc_tcor': (1e-6, 0.0),  # Systematic uncertainty CF 1.7
    'press':            (100.0, 0.0),
    'u_press':          (100.0, 0.0),
    'press_uc':         (100.0, 0.0)
}

# ── helpers: DB ───────────────────────────────────────────────────────────────

def get_psycopg2_connection(conn_params):
    return psycopg2.connect(
        host=conn_params['host'], port=conn_params['port'],
        dbname=conn_params['dbname'], user=conn_params['user'],
        password=conn_params['password'], cursor_factory=RealDictCursor
    )

def get_sqlalchemy_engine(conn_params):
    url = (
        f"postgresql://{conn_params['user']}:{conn_params['password']}"
        f"@{conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}"
    )
    return create_engine(url)

def load_station_record_numbers(conn_params) -> dict:
    """
    Load the full station table (only 34 rows) into a dict
    { idstation -> station.id } for O(1) lookups across millions of rows.
    Called ONCE per run, result passed down to export_month / build_cdm_dataframe.
    """
    query = "SELECT id, idstation FROM station"
    conn = get_psycopg2_connection(conn_params)
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    finally:
        conn.close()
    mapping = {r['idstation']: r['id'] for r in rows}
    print(f"  Loaded station lookup: {len(mapping)} entries")
    return mapping


def get_available_months(conn_params, data_table='data'):
    query = (
        f"SELECT DISTINCT "
        f"  EXTRACT(YEAR  FROM report_timestamp)::int AS year, "
        f"  EXTRACT(MONTH FROM report_timestamp)::int AS month "
        f"FROM {data_table} ORDER BY year, month"
    )
    conn = get_psycopg2_connection(conn_params)
    try:
        with conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
    finally:
        conn.close()
    return [(r['year'], r['month']) for r in rows]


# ── helpers: NetCDF writing ───────────────────────────────────────────────────

def _ensure_string_dim(ncf: nc.Dataset, length: int) -> str:
    name = f"string{max(length, 1)}"
    if name not in ncf.dimensions:
        ncf.createDimension(name, max(length, 1))
    return name


def _write_char_var(ncf: nc.Dataset, name: str, arr, index_dim: str,
                    **attrs) -> nc.Variable:
    """Write a string column as 2-D char (index, stringN)."""
    def to_str(s):
        if s is None: return ''
        if isinstance(s, bytes): return s.decode('utf-8', errors='replace')
        try:
            if pd.isna(s): return ''
        except (TypeError, ValueError):
            pass
        return str(s)

    str_list = [to_str(s) for s in arr]
    maxlen   = max((len(s) for s in str_list), default=0)
    maxlen   = max(maxlen, 1)          # must be ≥ 1 even when all strings are empty
    sdim     = _ensure_string_dim(ncf, maxlen)

    var = ncf.createVariable(
        name, 'S1', (index_dim, sdim),
        zlib=True, complevel=DEFLATE_LEVEL, shuffle=True
    )
    data = np.full((len(str_list), maxlen), b'', dtype='S1')
    for i, s in enumerate(str_list):
        for j, ch in enumerate(s.encode('utf-8', errors='replace')[:maxlen]):
            data[i, j] = bytes([ch])
    var[:] = data
    for k, v in attrs.items():
        setattr(var, k, v)
    return var


def _write_int64_var(ncf: nc.Dataset, name: str, arr, index_dim: str,
                     **attrs) -> nc.Variable:
    sentinel = np.iinfo(np.int64).min
    if isinstance(arr, pd.Series):
        data = arr.fillna(sentinel).astype(np.int64).values
    else:
        data = np.where(np.isnan(arr.astype(float)), sentinel, arr).astype(np.int64)
    var = ncf.createVariable(
        name, np.int64, (index_dim,),
        zlib=True, complevel=DEFLATE_LEVEL, shuffle=True
    )
    var[:] = data
    for k, v in attrs.items():
        setattr(var, k, v)
    return var


def _write_float32_var(ncf: nc.Dataset, name: str, arr, index_dim: str,
                       **attrs) -> nc.Variable:
    if isinstance(arr, pd.Series):
        data = arr.to_numpy(dtype=np.float64, na_value=np.nan)
    else:
        data = np.asarray(arr, dtype=np.float64)
    var = ncf.createVariable(
        name, np.float32, (index_dim,),
        zlib=True, complevel=DEFLATE_LEVEL, shuffle=True,
        fill_value=np.float32('nan')
    )
    var[:] = data.astype(np.float32)
    for k, v in attrs.items():
        setattr(var, k, v)
    return var


def _write_uint8_var(ncf: nc.Dataset, name: str, arr, index_dim: str,
                     **attrs) -> nc.Variable:
    if isinstance(arr, pd.Series):
        data = arr.fillna(255).astype(np.uint8).values
    else:
        data = np.asarray(arr, dtype=np.uint8)
    var = ncf.createVariable(
        name, np.uint8, (index_dim,),
        zlib=True, complevel=DEFLATE_LEVEL, shuffle=True
    )
    var[:] = data
    for k, v in attrs.items():
        setattr(var, k, v)
    return var

def _write_uint16_var(ncf: nc.Dataset, name: str, arr, index_dim: str,
                      **attrs) -> nc.Variable:
    if isinstance(arr, pd.Series):
        data = arr.fillna(65535).astype(np.uint16).values
    else:
        data = np.asarray(arr, dtype=np.uint16)
    var = ncf.createVariable(
        name, np.uint16, (index_dim,),
        zlib=True, complevel=DEFLATE_LEVEL, shuffle=True
    )
    var[:] = data
    for k, v in attrs.items():
        setattr(var, k, v)
    return var

# ── CDM pivot logic ───────────────────────────────────────────────────────────

def _col_or_nan_orig(df: pd.DataFrame, col: str) -> pd.Series:
    """Return column if it exists, else a Series of NaN."""
    if col and col in df.columns:
        return pd.to_numeric(df[col], errors='coerce')
    return pd.Series(np.nan, index=df.index, dtype=np.float32)


def _col_or_nan_orig(df: pd.DataFrame, column_name_cf_1_4: str,
                column_name_cf_1_7: str) -> pd.Series:
    if (column_name_cf_1_4 and column_name_cf_1_4 in df.columns) and \
            (column_name_cf_1_7 and column_name_cf_1_7 in df.columns):
        series_cf_1_4 = pd.to_numeric(df[column_name_cf_1_4], errors='coerce')
        series_cf_1_7 = pd.to_numeric(df[column_name_cf_1_7], errors='coerce')

        return series_cf_1_4.fillna(series_cf_1_7)

    # Fallback if columns are missing
    return pd.Series(np.nan, index=df.index, dtype=np.float32)


def _col_or_nan(df: pd.DataFrame,
                column_name_cf_1_4: Optional[str] = None,
                column_name_cf_1_7: Optional[str] = None) -> Optional[
    pd.Series]:
    # 1. If both are None, return None
    # if column_name_cf_1_4 is None and column_name_cf_1_7 is None:
    #     return None

    # 2. If only cf_1_4 has a value and cf_1_7 is None
    if column_name_cf_1_4 is not None and column_name_cf_1_7 is None:
        if column_name_cf_1_4 in df.columns:
            return pd.to_numeric(df[column_name_cf_1_4], errors='coerce')
        # Fallback if the column is missing from the DataFrame
        return pd.Series(np.nan, index=df.index, dtype=np.float32)

    # 3. If only cf_1_7 has a value and cf_1_4 is None
    if column_name_cf_1_7 is not None and column_name_cf_1_4 is None:
        if column_name_cf_1_7 in df.columns:
            return pd.to_numeric(df[column_name_cf_1_7], errors='coerce')
        # Fallback if the column is missing from the DataFrame
        return pd.Series(np.nan, index=df.index, dtype=np.float32)

    # 4. If both have a value (original behavior)
    if (column_name_cf_1_4 and column_name_cf_1_4 in df.columns) and \
            (column_name_cf_1_7 and column_name_cf_1_7 in df.columns):
        series_cf_1_4 = pd.to_numeric(df[column_name_cf_1_4], errors='coerce')
        series_cf_1_7 = pd.to_numeric(df[column_name_cf_1_7], errors='coerce')

        return series_cf_1_4.fillna(series_cf_1_7)

    return pd.Series(np.nan, index=df.index, dtype=np.float32)

def build_cdm_dataframe(df_merged: pd.DataFrame,
                        station_lookup: dict) -> pd.DataFrame:
    """
    Pivot the wide merged DataFrame into a CDM long-format DataFrame.

    Each physical level of the sounding generates one row per CDM variable.
    The output has exactly the CDM columns needed by the NetCDF writer.
    """

    # # INIZIO ORIG
    # # Ensure report_timestamp is datetime for time_since_launch computation
    # if not pd.api.types.is_datetime64_any_dtype(df_merged['report_timestamp']):
    #     df_merged = df_merged.copy()
    #     df_merged['report_timestamp'] = pd.to_datetime(
    #         df_merged['report_timestamp'], utc=True, errors='coerce'
    #     )
    #
    # # Compute time_since_launch (seconds from sounding start per report_id)
    # launch_time = (
    #     df_merged.groupby('report_id')['report_timestamp']
    #     .transform('min')
    # )
    # df_merged = df_merged.copy()
    # df_merged['_time_since_launch'] = (
    #     (df_merged['report_timestamp'] - launch_time)
    #     .dt.total_seconds()
    # )
    #
    # for col, (scale, offset) in UNIT_TRANSFORMS.items():
    #     if col in df_merged.columns:
    #         df_merged[col] = df_merged[col] * scale + offset
    #
    # # report_timestamp as int64 seconds-since-epoch
    # ts_epoch = df_merged['report_timestamp'].astype('int64') // 10**9
    # # FINE ORIG

    # INIZIO NEW - NON FUNZIONANTE
    # Normalizza sempre a UTC, sia che arrivi tz-aware che tz-naive
    # ts_raw = pd.to_datetime(df_merged['report_timestamp'], errors='coerce')
    # if ts_raw.dt.tz is None:
    #     ts_raw = ts_raw.dt.tz_localize('UTC')
    # else:
    #     ts_raw = ts_raw.dt.tz_convert('UTC')
    # df_merged = df_merged.copy()
    # df_merged['report_timestamp'] = ts_raw
    #
    # # Poi usa ts_raw anche per il calcolo di time_since_launch
    # launch_time = df_merged.groupby('report_id')['report_timestamp'].transform(
    #     'min')
    # df_merged['_time_since_launch'] = (
    #             df_merged['report_timestamp'] - launch_time).dt.total_seconds()
    #
    # # Infine converti in secondi interi (coerente con TIME_UNITS = "seconds since 1970-01-01 00:00:00")
    # ts_epoch = df_merged['report_timestamp'].astype('int64') // 10 ** 9
    # print("DEBUG ts_epoch sample:", ts_epoch.iloc[:3].values)
    # print("DEBUG decoded back:",
    #       pd.to_datetime(ts_epoch.iloc[:3].values, unit='s', utc=True))
    # print("DEBUG original timestamp:",
    #       df_merged['report_timestamp'].iloc[:3].values)
    # ema = ""
    # FINE NEW - NON FUNZIONANTE

    # INIZIO NEW
    # Normalizza sempre a UTC, sia che arrivi tz-aware che tz-naive
    # Determina la risoluzione e converti sempre in secondi interi
    ts_raw = df_merged['report_timestamp']
    if pd.api.types.is_datetime64_any_dtype(ts_raw):
        if ts_raw.dt.tz is None:
            ts_raw = ts_raw.dt.tz_localize('UTC')
        else:
            ts_raw = ts_raw.dt.tz_convert('UTC')
        # Converti a datetime64[s] esplicitamente prima di astype int64
        ts_epoch = ts_raw.values.astype('datetime64[s]').astype('int64')
    else:
        ts_epoch = pd.to_datetime(ts_raw, utc=True).values.astype(
            'datetime64[s]').astype('int64')
    # FINE NEW

    # Station metadata (constant per report_id, comes from header after merge)
    def _str_col(column_name_cf_1_4, column_name_cf_1_7):
        if column_name_cf_1_4 in df_merged.columns and column_name_cf_1_7 in df_merged.columns:
            series_cf_1_4 = df_merged[column_name_cf_1_4]
            series_cf_1_7 = df_merged[column_name_cf_1_7]

            combined_series = series_cf_1_4.fillna(series_cf_1_7)

            return combined_series.fillna('').astype(str)

        # Fallback if the columns are not in the DataFrame
        return pd.Series('', index=df_merged.index)

    primary_station_id = _str_col('g_general_sitecode', 'g_site_key')

    # ── record_number: O(1) in-memory lookup per row (station table has only 34 rows)
    # Map primary_station_id → station.id using the pre-loaded dict
    _MISSING_REC = np.iinfo(np.int32).min   # sentinel for unknown stations
    record_number_vals = (
        primary_station_id
        .map(station_lookup)
        .fillna(_MISSING_REC)
        .astype(np.int32)
        .values
    )
    station_name = _str_col('g_general_sitename', 'g_site_name')
    report_id_str      = df_merged['report_id'].fillna(0).astype(np.int64).astype(str)
    lat_station        = _col_or_nan(df_merged, 'g_measuringsystem_latitude', 'g_measurementsystem_latitude')
    lon_station        = _col_or_nan(df_merged, 'g_measuringsystem_longitude', 'g_measurementsystem_longitude')
    alt_station        = _col_or_nan(df_merged, 'g_measuringsystem_altitude', 'g_measurementsystem_altitude')
    lat_obs            = _col_or_nan(df_merged, 'lat', 'lat')
    lon_obs            = _col_or_nan(df_merged, 'lon', 'lon')
    z_coord            = _col_or_nan(df_merged, 'alt', 'alt')   # altitude [m]

    # # GRUAN-specific corrections (same value broadcast to all variable rows)
    # cor_temp_vals = _col_or_nan(df_merged, 'temp_corr_rad')
    # cor_rh_vals   = _col_or_nan(df_merged, 'rh_corr') - _col_or_nan(df_merged, 'rh')

    pieces = []

    for entry in CDM_VARIABLES:
        cdm_code, var_name_cf_1_4, var_name_cf_1_7, units, units_str, uc_rand_col, uc_sys_cf_1_4, uc_sys_cf_1_7, uc_tot_cf_1_4, uc_tot_cf_1_7 = entry

        obs_val = _col_or_nan(df_merged, var_name_cf_1_4, var_name_cf_1_7).copy()

        uc_rand = _col_or_nan(df_merged, uc_rand_col, None)
        uc_sys  = _col_or_nan(df_merged, uc_sys_cf_1_4, uc_sys_cf_1_7)
        uc_tot  = _col_or_nan(df_merged, uc_tot_cf_1_4, uc_tot_cf_1_7)

        piece = pd.DataFrame({
            # CDM core columns
            'observed_variable':                  np.uint16(cdm_code),
            'observation_value':                  obs_val.values,
            'units':                              units,
            'z_coordinate':                       z_coord.values,
            'z_coordinate_type':                  np.uint8(0),        # 0 = altitude above MSL
            # Uncertainties
            'uncertainty_value1':                 uc_rand.values,
            'uncertainty_type1':                  np.uint8(1),
            'uncertainty_units1':                 units,
            'uncertainty_value2':                 uc_sys.values,
            'uncertainty_type2':                  np.uint8(2),
            'uncertainty_units2':                 units,
            'uncertainty_value5':                 uc_tot.values,
            'uncertainty_type5':                  np.uint8(5),
            'uncertainty_units5':                 units,
            # GRUAN corrections
            # 'cor_rh':                             cor_rh_vals.values,
            # 'cor_temp':                           cor_temp_vals.values,
            # Identifiers
            'report_timestamp':                   ts_epoch,
            'report_meaning_of_timestamp':        np.uint8(1),
            'report_id':                          report_id_str.values,
            'report_duration':                    np.uint8(9),
            'observation_id':                     df_merged['observation_id'].values,
            'primary_station_id':                 primary_station_id.values,
            'station_name|station_configuration': station_name.values,
            # Station geometry
            'latitude|station_configuration':     lat_station.values,
            'longitude|station_configuration':    lon_station.values,
            'height_of_station_above_sea_level':  alt_station.values,
            # Balloon position
            'latitude|observations_table':        lat_obs.values,
            'longitude|observations_table':       lon_obs.values,
            # station.id lookup
            'record_number':                      record_number_vals,
        }, index=df_merged.index)

        pieces.append(piece)

    cdm = pd.concat(pieces, ignore_index=True)
    return cdm


# ── NetCDF writer ─────────────────────────────────────────────────────────────

# All CDM variable codes exported  (used for observed_variable labels attr)
ALL_CDM_CODES   = [e[0] for e in CDM_VARIABLES]
ALL_CDM_LABELS = {
    126: 'air_temperature',
    138: 'relative_humidity',
    106: 'wind_from_direction',
    107: 'wind_speed',
    104: 'eastward_wind_speed',
    105: 'northward_wind_speed',
    123: 'water_vapour_mixing_ratio',
    122: 'vertical_speed_of_radiosonde',
    117: 'geopotential_height',
    116: 'frost_point_temperature',
    124: 'air_relative_humidity_effective_vertical_resolution',
    73: 'shortwave_radiation',
    125: 'altitude',
    142: 'pressure',
    143: 'time_since_launch',
}


def write_cdm_netcdf(cdm: pd.DataFrame, output_file: Path):
    """Write a CDM long-format DataFrame to a GRUAN-convention NetCDF4 file."""
    N = len(cdm)
    print(f"  CDM rows to write: {N:,}")

    codes_used  = sorted(set(ALL_CDM_CODES))
    labels_used = [ALL_CDM_LABELS.get(c, str(c)) for c in codes_used]

    with nc.Dataset(str(output_file), 'w', format='NETCDF4') as ncf:

        # ── dimensions ────────────────────────────────────────────────────────
        ncf.createDimension('index', N)
        index_dim = 'index'

        # ── index ───────────────────────────────────────────────────────
        # _write_int64_var(ncf, 'index', np.arange(N, dtype=np.int64), index_dim)

        # ── station geometry ──────────────────────────────────────────────────
        _write_float32_var(ncf, 'height_of_station_above_sea_level',
                           cdm['height_of_station_above_sea_level'], index_dim)
        _write_char_var(ncf, 'primary_station_id',
                        cdm['primary_station_id'].values, index_dim)
        _write_char_var(ncf, 'station_name|station_configuration',
                        cdm['station_name|station_configuration'].values, index_dim)
        _write_float32_var(ncf, 'latitude|station_configuration',
                           cdm['latitude|station_configuration'], index_dim)
        _write_float32_var(ncf, 'longitude|station_configuration',
                           cdm['longitude|station_configuration'], index_dim)

        # ── sounding / report ids ─────────────────────────────────────────────
        _write_char_var(ncf, 'report_id',
                        cdm['report_id'].values, index_dim)
        _write_char_var(ncf, 'report_duration',
                        cdm['report_duration'].values, index_dim)
        _write_int64_var(ncf, 'observation_id',
                         cdm['observation_id'], index_dim)

        # ── record_number: station.id from station table ──────────────────────
        rec_num_var = ncf.createVariable(
            'record_number', np.int8, (index_dim,),
            zlib=True, complevel=DEFLATE_LEVEL, shuffle=True,
            fill_value=np.iinfo(np.int8).min
        )
        rec_num_var[:] = cdm['record_number'].values.astype(np.int8)
        rec_num_var.long_name = 'station record number'
        rec_num_var.comment   = 'integer primary key (id) of the matching row in the station table'

        # ── time ──────────────────────────────────────────────────────────────
        _write_int64_var(ncf, 'report_timestamp',
                         cdm['report_timestamp'], index_dim,
                         units=TIME_UNITS, calendar=TIME_CALENDAR)
        _write_uint8_var(ncf, 'report_meaning_of_timestamp',
                         cdm['report_meaning_of_timestamp'], index_dim)

        # ── balloon position ──────────────────────────────────────────────────
        _write_float32_var(ncf, 'latitude|observations_table',
                           cdm['latitude|observations_table'], index_dim)
        _write_float32_var(ncf, 'longitude|observations_table',
                           cdm['longitude|observations_table'], index_dim)

        # ── z coordinate ──────────────────────────────────────────────────────
        _write_float32_var(ncf, 'z_coordinate',
                           cdm['z_coordinate'], index_dim)
        _write_uint8_var(ncf, 'z_coordinate_type',
                         cdm['z_coordinate_type'], index_dim)

        # ── CDM core: observed variable & value ───────────────────────────────
        ov_var = ncf.createVariable(
            'observed_variable', np.int16, (index_dim,),
            zlib=True, complevel=DEFLATE_LEVEL, shuffle=True
        )
        ov_var[:] = cdm['observed_variable'].values.astype(np.int16)
        ov_var.codes  = np.array(codes_used, dtype=np.int32)
        ov_var.labels = ', '.join(labels_used)

        _write_float32_var(ncf, 'observation_value',
                           cdm['observation_value'], index_dim)

        # ── units ─────────────────────────────────────────────────────────────
        _write_uint16_var(ncf, 'units',
                        cdm['units'].values, index_dim)
        # _write_char_var(ncf, 'original_units',
        #                 cdm['original_units'].values, index_dim)

        # ── uncertainties ─────────────────────────────────────────────────────
        for idx in (1, 2, 5):
            _write_float32_var(ncf, f'uncertainty_value{idx}',
                               cdm[f'uncertainty_value{idx}'], index_dim)
            _write_uint8_var(  ncf, f'uncertainty_type{idx}',
                               cdm[f'uncertainty_type{idx}'], index_dim)
            _write_char_var(   ncf, f'uncertainty_units{idx}',
                               cdm[f'uncertainty_units{idx}'].values, index_dim)

        # ── GRUAN-specific corrections ────────────────────────────────────────
        # _write_float32_var(ncf, 'cor_rh',   cdm['cor_rh'],   index_dim)
        # _write_float32_var(ncf, 'cor_temp', cdm['cor_temp'], index_dim)

    size_mb = output_file.stat().st_size / 1e6
    print(f"  Written: {output_file}  ({size_mb:.1f} MB)")


# ── Main export logic ─────────────────────────────────────────────────────────

def export_month(conn_params, year, month, output_dir,
                 data_table='data', header_table='header',
                 station_lookup: dict = None):
    start_date = datetime(year, month, 1, tzinfo=timezone.utc)
    end_date   = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    output_file = output_dir / f"insitu-observations-gruan-reference-network_GRUAN_{year:04d}_{month:02d}.nc"
    if output_file.exists():
        print(f"  {output_file} already exists, skipping.")
        return
    print(f"\n── Exporting {year:04d}-{month:02d}  →  {output_file}")

    # ── fetch data ────────────────────────────────────────────────────────────
    COLUMNS_DATA_TABLE = [
        "'asc'", "alt", "alt_gph", "alt_gph_uc_tcor", "alt_gph_uc", "alt_uc",
        "fp", "fp_uc", "geopot", "idstation_pk", "lat", "lon",
        "observation_id", "press", "press_uc", "report_timestamp", "rh", "rh_res", "rh_uc",
        "rh_uc_tcor", "swrad", "temp", "temp_uc", "temp_uc_tcor", "u", "u_alt",
        "u_cor_rh", "u_cor_temp", "u_press", "u_rh", "u_std_rh", "u_std_temp",
        "u_swrad", "u_temp", "u_wdir", "u_wspeed", "v", "vspeed", "vspeed_uc",
        "wdir", "wdir_uc", "wmeri", "wmeri_uc", "wspeed", "wspeed_uc", "wvmr",
        "wvmr_vol", "wvmr_vol_uc", "wvmr_vol_uc_tcor", "wzon", "wzon_uc"
    ]

    engine = get_sqlalchemy_engine(conn_params)
    chunks = []

    columns_str = ", ".join(COLUMNS_DATA_TABLE)

    data_query = (
        f"SELECT {columns_str} "
        # f"FROM {data_table} "
        f"FROM {data_table}_{year:04d}{month:02d} "
        # f"WHERE report_timestamp >= %(start)s AND report_timestamp < %(end)s "
        f"ORDER BY report_timestamp, observation_id"
    )

    print("data_query: ", data_query)
    print("start: ", start_date)
    print("end: ", end_date)

    start_fetch_time = time.perf_counter()

    try:
        chunk_iterator = pd.read_sql(
            data_query, engine,
            params={'start': start_date, 'end': end_date},
            chunksize=DATA_CHUNK_SIZE
        )

        with tqdm(chunk_iterator, desc="  Reading chunks", unit=" chunk",
                  leave=True) as pbar:
            for chunk in pbar:
                chunks.append(chunk)
                pbar.set_postfix(
                    {"total_rows": f"{sum(len(c) for c in chunks):,}"},
                    refresh=False)

    finally:
        engine.dispose()

    if not chunks:
        print("  No data rows — skipping.")
        return

    df_data = pd.concat(chunks, ignore_index=True)
    total_fetch_time = time.perf_counter() - start_fetch_time
    print(
        f"  Data rows   : {len(df_data):,} (Total read time: {total_fetch_time:.2f}s)")

    # ── fetch header ──────────────────────────────────────────────────────────
    COLUMNS_HEADER_TABLE = [
        "g_general_sitecode", "g_measurementsystem_altitude",
        "g_measurementsystem_latitude",
        "g_measurementsystem_longitude", "g_measuringsystem_altitude",
        "g_measuringsystem_latitude",
        "g_measuringsystem_longitude", "g_site_key", "idstation_pk",
        "report_id",
        "report_timestamp"
    ]

    header_columns_str = ", ".join(COLUMNS_HEADER_TABLE)

    header_query = (
        f"SELECT {header_columns_str} "
        f"FROM {header_table} "
        f"WHERE report_timestamp >= %(start)s AND report_timestamp < %(end)s"
    )
    engine2 = get_sqlalchemy_engine(conn_params)
    try:
        df_header = pd.read_sql(header_query, engine2,
                                params={'start': start_date, 'end': end_date})
    finally:
        engine2.dispose()
    print(f"  Header rows : {len(df_header):,}")

    # ── merge data + header ───────────────────────────────────────────────────
    if df_header.empty:
        df_merged = df_data.copy()
    else:
        df_merged = pd.merge(
            df_data, df_header,
            on=['idstation_pk', 'report_timestamp'],
            how='left', suffixes=('', '_header')
        ).copy()
    print(f"  Merged rows : {len(df_merged):,}")

    # ── pivot to CDM long format ──────────────────────────────────────────────
    cdm = build_cdm_dataframe(df_merged, station_lookup or {})
    print(f"  CDM rows    : {len(cdm):,}  ({len(CDM_VARIABLES)} vars × {len(df_merged):,} levels)")

    # ── write NetCDF ──────────────────────────────────────────────────────────
    write_cdm_netcdf(cdm, output_file)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Export GRUAN PostgreSQL data to CDM-compliant monthly NetCDF4.'
    )
    parser.add_argument('--env-file',     default='.env')
    parser.add_argument('--output-dir',   required=True)
    parser.add_argument('--start-year',   type=int)
    parser.add_argument('--end-year',     type=int)
    parser.add_argument('--data-table',   default='data')
    parser.add_argument('--header-table', default='header')
    args = parser.parse_args()

    load_dotenv(args.env_file)
    missing = [e for e in ['DB_USER','GRUAN_USER_PSW','DB_HOST','DB_PORT','DB_NAME']
               if not os.getenv(e)]
    if missing:
        sys.exit(f'Missing environment variables: {missing}')

    conn_params = {
        'host':     os.getenv('DB_HOST'),
        'port':     os.getenv('DB_PORT'),
        'dbname':   os.getenv('DB_NAME'),
        'user':     os.getenv('DB_USER'),
        'password': os.getenv('GRUAN_USER_PSW'),
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    months = get_available_months(conn_params, args.header_table)
    if not months:
        sys.exit('No data found.')
    if args.start_year:
        months = [(y, m) for y, m in months if y >= args.start_year]
    if args.end_year:
        months = [(y, m) for y, m in months if y <= args.end_year]

    print(f'Months to export: {len(months)}')
    station_lookup = load_station_record_numbers(conn_params)
    for y, m in months:
        export_month(conn_params, y, m, output_dir, args.data_table, args.header_table,
                     station_lookup=station_lookup)
    print('\nDone.')


if __name__ == '__main__':
    main()