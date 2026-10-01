#!/usr/bin/env python3

import os
import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import netCDF4 as nc
try:                                    # optional: faster DB reads (--read-method adbc)
    import pyarrow as pa
    import adbc_driver_postgresql.dbapi as adbc_dbapi
    _HAVE_ADBC = True
except ImportError:
    _HAVE_ADBC = False
try:                                    # optional: enables parallel compression
    import h5py
    _HAVE_H5PY = True
except ImportError:
    _HAVE_H5PY = False
import psycopg2
from psycopg2.extras import RealDictCursor
from sqlalchemy import create_engine, event
from dotenv import load_dotenv
from typing import Optional
import time
import platform
from urllib.parse import quote
import traceback
from contextlib import contextmanager
from tqdm import tqdm

import qc_pipeline

# ── tunables ──────────────────────────────────────────────────────────────────
DATA_CHUNK_SIZE = 50_000
DEFLATE_LEVEL   = 3
CHUNK_BYTES      = 1 << 20      # target HDF5 chunk size (~1 MB, uncompressed)
WRITE_BLOCK_ROWS = 1 << 21      # rows converted+written per call (sequential mode)


def _available_cpus() -> int:
    """CPUs this process may actually use (respects taskset/cgroup affinity)."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:                      # macOS / Windows
        return os.cpu_count() or 1


DEFAULT_THREADS  = min(_available_cpus(), 32)   # parallel compression threads
TIME_UNITS      = "seconds since 1970-01-01 00:00:00"
TIME_CALENDAR   = "proleptic_gregorian"

# Only sounding levels with a valid (not NULL, not NaN) "alt" value that is
# <= MAX_ALTITUDE_M are exported. The filter is applied directly in the SQL
# query so that discarded levels are never transferred nor loaded in memory.
MAX_ALTITUDE_M  = 47_000

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
    # code var_name_cf_1_4      var_name_cf_1_7       unit   units_str    uc_rand       uc_sys_cf_1_4  uc_sys_cf_1_7,      uc_tot_cf_1_4  uc_tot_cf_1_7
    (126, 'temp',               'temp',               5,     'K',         'u_std_temp', 'u_cor_temp',  'temp_uc_tcor',     'u_temp',      'temp_uc'),
    (138, 'rh',                 'rh',                 300,   '%',         'u_std_rh',   'u_cor_rh',    'rh_uc_tcor',       'u_rh',        'rh_uc'),
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
    (143, 'time',               'time',               3,     's',         None,         None,          None,               None,          None),
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

# ── plausibility QC configuration ─────────────────────────────────────────────
# Before writing the NetCDF, every level of every sounding (profile identified
# by g_product_id) is checked with the plausibility rules of qc_pipeline.py.
# Implausible values are replaced by NULL (NaN). Only the variables analysed by
# qc_pipeline are checked: temperature, RH, wind speed, wind direction,
# pressure and WVMR.
#
# The QC works on the DB values BEFORE UNIT_TRANSFORMS are applied, converted
# into the units used by qc_pipeline (K, RH as a fraction, m s-1, degrees,
# hPa, ppmv).
#
# QC variable name -> list of (DB column, scale factor to QC units), in order of
# preference: the first column that is not NULL wins (same CF-1.4 first, CF-1.7
# second logic used by _col_or_nan when the CDM dataframe is built).
#   * rh and its uncertainties are stored in % in the DB  -> x 0.01 = fraction
#   * press is stored in hPa in the DB (converted to Pa only at export time)
#   * wvmr (CF-1.4) is a dimensionless ratio  -> x 1e6 = ppmv
#     wvmr_vol (CF-1.7) is already in ppmv (converted to mol mol-1 at export)
# If the units of your DB differ, change the scale factors here.
QC_INPUT_COLUMNS = {
    'height':                   [('alt', 1.0)],
    'temperature':              [('temp', 1.0)],
    'relative_humidity':        [('rh', 0.01)],
    'relative_humidity_uc_tot': [('u_rh', 0.01), ('rh_uc', 0.01)],
    'wind_speed':               [('wspeed', 1.0)],
    'wind_direction':           [('wdir', 1.0)],
    'pressure':                 [('press', 1.0)],
    'pressure_uc_tot':          [('u_press', 1.0), ('press_uc', 1.0)],
    'wvmr':                     [('wvmr', 1e6), ('wvmr_vol', 1.0)],
    'frost_point':              [('fp', 1.0)],
    'shortwave_radiation':     [('swrad', 1.0)],
    'vertical_speed':           [('vspeed', 1.0), ('asc', 1.0)],
}

# Levels above this altitude (m) are not checked (same default as qc_pipeline).
# Use None to check every level.
QC_MAX_ALTITUDE_M = qc_pipeline.PLAUSIBILITY_MAX_ALTITUDE_M

# When True, the uncertainty columns (random / systematic / total) of a value
# that has been set to NULL are set to NULL as well.
QC_NULLIFY_UNCERTAINTIES = False

# A warning is printed when more than this fraction of the values of a
# variable is rejected in a month (usually a sign of a wrong unit).
QC_REJECTION_WARN_FRACTION = 0.5

# ── stage timer ───────────────────────────────────────────────────────────────
# Lightweight wall-clock profiler. Usage:
#     with TIMER('merge'): ...            # top-level stage
#     with TIMER('qc/flag_levels'): ...   # 'parent/child' = sub-stage of 'parent'
#     TIMER.add('db_read_data', seconds)  # when the code can't be wrapped
# TIMER collects one month; export_month prints its report and folds it into
# RUN_TIMER, which main() prints at the end (whole run).

LOG_PATH = None     # set by init_log(); None = no log file


def init_log(log_dir, header_lines, started=None):
    """Create '<log_dir>/export_netcdf_timing_YYYYmmdd_HHMMSS.log' (timestamp of
    the run start) and write a header. Returns the path."""
    global LOG_PATH
    started = started or datetime.now()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    LOG_PATH = log_dir / f"export_netcdf_timing_{started:%Y%m%d_%H%M%S}.log"
    log("=" * 70)
    log(f"export_netcdf timing log - run started {started:%Y-%m-%d %H:%M:%S}")
    for line in header_lines:
        log(line)
    log("=" * 70)
    return LOG_PATH


def log(text=''):
    """Append text to the log file (no console output)."""
    if LOG_PATH is None:
        return
    with open(LOG_PATH, 'a', encoding='utf-8') as fh:
        fh.write(text + '\n')


def emit(text=''):
    """Print to console AND append to the log file."""
    print(text)
    log(text)


class StageTimer:
    def __init__(self):
        self.reset()

    def reset(self):
        self.t = {}            # stage -> [seconds, calls]   (insertion = pipeline order)
        self.var_times = {}    # NetCDF variable -> seconds  (writer detail)

    def add(self, name, seconds):
        e = self.t.setdefault(name, [0.0, 0])
        e[0] += seconds
        e[1] += 1

    @contextmanager
    def __call__(self, name):
        self.t.setdefault(name, [0.0, 0])      # register on entry -> parent listed before children
        start = time.perf_counter()
        try:
            yield
        finally:
            self.add(name, time.perf_counter() - start)

    def merge(self, other):
        for k, (sec, calls) in other.t.items():
            e = self.t.setdefault(k, [0.0, 0])
            e[0] += sec
            e[1] += calls
        for k, sec in other.var_times.items():
            self.var_times[k] = self.var_times.get(k, 0.0) + sec

    def report(self, title, note=''):
        """Print the timing table to the console and append it to the log file."""
        top = {k: v[0] for k, v in self.t.items() if '/' not in k}
        total = sum(top.values())
        if not total:
            return
        slowest = max(top, key=top.get)
        out = []
        rule = '─' * 66
        out.append(f"\n── Timing: {title} {rule[:max(66 - len(title) - 11, 3)]}")
        if note:
            out.append(f"  {note}")
        out.append(f"  {'stage':<38}{'time':>9}{'%':>8}{'calls':>7}")

        def row(label, sec, calls, mark=''):
            out.append(f"  {label:<38}{sec:>8.2f}s{100 * sec / total:>7.1f}%"
                       f"{calls if calls else '':>7} {mark}".rstrip())

        for name, (sec, calls) in self.t.items():
            if '/' in name:
                continue
            row(name, sec, calls, '◄ slowest' if name == slowest else '')
            children = [(k.split('/', 1)[1], v) for k, v in self.t.items()
                        if k.startswith(name + '/')]
            for child, (csec, ccalls) in children:
                row(f"   └ {child}", csec, ccalls)
            if children:
                rest = sec - sum(v[0] for _, v in children)
                if rest > 0.005 * total:
                    row("   └ (other / not itemised)", rest, 0)
        out.append(f"  {'TOTAL (top-level stages)':<38}{total:>8.2f}s")
        if self.var_times:
            worst = sorted(self.var_times.items(), key=lambda kv: -kv[1])[:5]
            out.append("  slowest NetCDF variables: " +
                       ", ".join(f"{k} {v:.2f}s" for k, v in worst))
        emit('\n'.join(out))


TIMER     = StageTimer()   # current month
RUN_TIMER = StageTimer()   # whole run

# ── helpers: DB ───────────────────────────────────────────────────────────────

def _set_session_utc(dbapi_conn, *_unused):
    """Force the session time zone to UTC, whatever the server default is.

    Why it matters: with a non-UTC session (e.g. Europe/Rome) timestamptz values
    come back with different UTC offsets inside the same month (DST change) and
    pandas.read_sql + psycopg2 then shifts part of them by 1 hour. Partitions,
    month boundaries and EXTRACT(MONTH ...) are also UTC-based. A plain SET
    (not the 'options' startup parameter) also works behind connection poolers.
    """
    cur = dbapi_conn.cursor()
    try:
        cur.execute("SET TIME ZONE 'UTC'")
    finally:
        cur.close()
    dbapi_conn.commit()


def get_psycopg2_connection(conn_params):
    conn = psycopg2.connect(
        host=conn_params['host'], port=conn_params['port'],
        dbname=conn_params['dbname'], user=conn_params['user'],
        password=conn_params['password'], cursor_factory=RealDictCursor
    )
    _set_session_utc(conn)
    return conn

def get_sqlalchemy_engine(conn_params):
    url = (
        f"postgresql://{conn_params['user']}:{conn_params['password']}"
        f"@{conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}"
    )
    engine = create_engine(url)
    event.listen(engine, 'connect', _set_session_utc)   # every pooled connection
    return engine

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


def get_available_months(conn_params, data_table='header'):
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
#
# Why write_cdm_netcdf was slow, and what changed
# -----------------------------------------------
#   1. ~80% of the time is zlib compression of ~30 variables x 20M+ rows, done
#      by HDF5 on ONE core. The writer below can compress the HDF5 chunks in
#      parallel threads (zlib releases the GIL) and store them with h5py's
#      write_direct_chunk. The result is a perfectly standard NetCDF4/HDF5
#      file (same filters: shuffle + deflate), readable by any NetCDF reader.
#      Needs `h5py`; without it (or with threads=1) the sequential path is used.
#   2. String columns were processed row by row in Python (isinstance map,
#      astype(str), str.len ... on 20M rows, x4 columns). They are now
#      factorised (unique values -> codes) and expanded with one numpy fancy
#      index per block. A pandas Categorical input (see build_cdm_dataframe)
#      needs no hashing at all.
#   3. Data are converted and written BLOCK by BLOCK (aligned to the chunk
#      grid) instead of building full-size converted copies of every column.
#   4. Explicit ~1 MB chunks, and the HDF5 "shuffle" filter only where it
#      helps (4/8-byte types), float32 cast done once (no float64 detour).

def _chunk_rows(itemsize_bytes: int) -> int:
    return max(CHUNK_BYTES // max(itemsize_bytes, 1), 1024)


def _src_numeric(arr):
    """Series/array -> numpy array, zero-copy when it is already plain numpy."""
    if isinstance(arr, pd.Series):
        if isinstance(arr.dtype, np.dtype) and arr.dtype.kind in 'fiub':
            return arr.to_numpy()
        return arr.to_numpy(dtype=np.float64, na_value=np.nan)
    return np.asarray(arr)


def _src_integer(arr, sentinel, dtype):
    """Series/array -> numpy integer-like array; NULLs replaced by `sentinel`."""
    if isinstance(arr, pd.Series):
        if isinstance(arr.dtype, np.dtype) and arr.dtype.kind in 'iub':
            return arr.to_numpy()
        return arr.fillna(sentinel).to_numpy().astype(dtype)
    return np.asarray(arr)


def _factorize_strings(values):
    """Return (codes, table): codes has one int entry per row, table is the
    list of unique python str. Missing values map to ''."""
    s = values if isinstance(values, pd.Series) else pd.Series(values)

    if isinstance(s.dtype, pd.CategoricalDtype):
        codes = s.cat.codes.to_numpy()
        uniques = list(s.cat.categories)
    else:
        codes, uniques = pd.factorize(s, use_na_sentinel=True)
        uniques = list(uniques)

    def _to_str(x):
        if x is None:
            return ''
        if isinstance(x, bytes):
            return x.decode('utf-8', errors='replace')
        return str(x)

    table = [_to_str(x) for x in uniques]
    table.append('')                                     # slot for NULL / NaN
    codes = np.where(codes < 0, len(table) - 1, codes)   # -1 -> ''
    return codes, table


class _VarSpec:
    __slots__ = ('name', 'chunk', 'shuffle', 'get_block')

    def __init__(self, name, chunk, shuffle, get_block):
        self.name, self.chunk, self.shuffle, self.get_block = name, chunk, shuffle, get_block


class _NcWriter:
    """Writes 1-D (or 1-D + char) variables along one fixed 'index' dimension.

    threads <= 1 (or h5py missing) : classic sequential write via netCDF4.
    threads  > 1 (and h5py present): netCDF4 only creates the file structure;
        the chunks are then compressed by a thread pool and stored with h5py.
    """

    def __init__(self, path, n_rows, threads=1, index_dim='index'):
        self.path, self.n, self.dim = str(path), int(n_rows), index_dim
        self.threads = max(int(threads or 1), 1)
        if self.threads > 1 and not _HAVE_H5PY:
            print("  [writer] h5py not installed -> sequential write "
                  "(pip install h5py to enable parallel compression)")
        self.parallel = self.threads > 1 and _HAVE_H5PY
        self._pending = []
        self.ncf = nc.Dataset(self.path, 'w', format='NETCDF4')
        self.ncf.createDimension(self.dim, self.n)

    # context manager -------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        with TIMER('write_netcdf/hdf5_close (flush)'):
            self.ncf.close()
        if exc_type is None and self._pending:
            with TIMER('write_netcdf/parallel_compress_and_write'):
                self._fill_parallel()

    # variable creation -----------------------------------------------------
    def _add(self, name, dtype, dims, chunk, shuffle, get_block,
             fill_value=None, **attrs):
        kw = {} if fill_value is None else {'fill_value': fill_value}
        var = self.ncf.createVariable(
            name, dtype, dims, zlib=True, complevel=DEFLATE_LEVEL,
            shuffle=shuffle, chunksizes=chunk, **kw)
        for k, v in attrs.items():
            setattr(var, k, v)
        if self.parallel:
            self._pending.append(_VarSpec(name, chunk, shuffle, get_block))
        else:                                   # sequential: write right away
            cr = chunk[0]
            step = max(WRITE_BLOCK_ROWS // cr, 1) * cr      # multiple of chunk
            t_var = time.perf_counter()
            with TIMER('write_netcdf/seq_convert_compress_write'):
                for i in range(0, self.n, step):
                    j = min(i + step, self.n)
                    var[i:j] = get_block(i, j)
            # Writes are chunk-aligned, so HDF5 compresses each chunk as it is
            # written: this time includes conversion + compression.
            TIMER.var_times[name] = TIMER.var_times.get(name, 0.0) + time.perf_counter() - t_var
        return var

    def _numeric(self, name, dtype, src, conv, shuffle, fill_value=None, **attrs):
        chunk = (min(_chunk_rows(np.dtype(dtype).itemsize), max(self.n, 1)),)
        return self._add(name, dtype, (self.dim,), chunk, shuffle,
                         lambda i, j: conv(src[i:j]), fill_value, **attrs)

    def float32(self, name, arr, **attrs):
        return self._numeric(
            name, np.float32, _src_numeric(arr),
            lambda b: np.asarray(b, dtype=np.float32), shuffle=True,
            fill_value=np.float32('nan'), **attrs)

    def int64(self, name, arr, **attrs):
        sentinel = np.iinfo(np.int64).min
        def conv(b):
            if b.dtype.kind == 'f':
                return np.where(np.isnan(b), sentinel, b).astype(np.int64)
            return b.astype(np.int64, copy=False)
        return self._numeric(name, np.int64,
                             _src_integer(arr, sentinel, np.int64),
                             conv, shuffle=True, **attrs)

    def uint(self, name, arr, dtype, sentinel, **attrs):
        def conv(b):
            if b.dtype.kind == 'f':
                b = np.where(np.isnan(b), sentinel, b)
            return b.astype(dtype, copy=False)
        return self._numeric(name, dtype, _src_integer(arr, sentinel, dtype),
                             conv, shuffle=False, **attrs)

    def uint8(self, name, arr, **attrs):
        return self.uint(name, arr, np.uint8, 255, **attrs)

    def uint16(self, name, arr, **attrs):
        return self.uint(name, arr, np.uint16, 65535, **attrs)

    def char(self, name, arr, **attrs):
        with TIMER('write_netcdf/string_factorize'):
            codes, table = _factorize_strings(arr)
        enc = [t.encode('utf-8', errors='replace') for t in table]
        lens = np.fromiter((len(b) for b in enc), dtype=np.int64, count=len(enc))
        used = np.bincount(codes, minlength=len(enc)) > 0    # ignore unused categories
        maxlen = max(int(lens[used].max()) if used.any() else 1, 1)

        sdim = f"string{maxlen}"
        if sdim not in self.ncf.dimensions:
            self.ncf.createDimension(sdim, maxlen)

        # (n_unique, maxlen) matrix of single chars, NUL padded
        table_s1 = np.array(enc, dtype=f'S{maxlen}').view('S1').reshape(len(enc), maxlen)
        chunk = (min(_chunk_rows(maxlen), max(self.n, 1)), maxlen)
        return self._add(name, 'S1', (self.dim, sdim), chunk, False,
                         lambda i, j: table_s1[codes[i:j]], **attrs)

    # parallel phase --------------------------------------------------------
    def _fill_parallel(self):
        import zlib
        from collections import deque
        from concurrent.futures import ThreadPoolExecutor

        level, n = DEFLATE_LEVEL, self.n

        def compress_chunk(spec, i):
            cr = spec.chunk[0]
            j = min(i + cr, n)
            blk = np.ascontiguousarray(spec.get_block(i, j))
            if j - i < cr:                       # HDF5 edge chunks are stored full-size
                full = np.zeros((cr,) + blk.shape[1:], dtype=blk.dtype)
                full[:j - i] = blk
                blk = full
            raw = blk.reshape(-1).view(np.uint8)
            if spec.shuffle:                     # == HDF5 shuffle filter
                raw = np.ascontiguousarray(raw.reshape(-1, blk.dtype.itemsize).T)
            return zlib.compress(raw, level)     # == HDF5 deflate filter; releases the GIL

        with h5py.File(self.path, 'r+') as h5, \
                ThreadPoolExecutor(max_workers=self.threads) as pool:
            for spec in self._pending:
                t_var = time.perf_counter()
                ds = h5[spec.name]
                if (tuple(ds.chunks) != tuple(spec.chunk) or ds.compression != 'gzip'
                        or bool(ds.shuffle) != spec.shuffle):
                    raise RuntimeError(f"unexpected HDF5 layout for '{spec.name}'")
                cr, tail = spec.chunk[0], (0,) * (ds.ndim - 1)
                window = deque()                 # bounded in-flight chunks -> bounded RAM

                def flush_one():
                    i0, fut = window.popleft()
                    ds.id.write_direct_chunk((i0,) + tail, fut.result())

                for i in range(0, n, cr):
                    window.append((i, pool.submit(compress_chunk, spec, i)))
                    if len(window) >= 2 * self.threads:
                        flush_one()
                while window:
                    flush_one()
                TIMER.var_times[spec.name] = time.perf_counter() - t_var
        self._pending = []

# ── plausibility QC ───────────────────────────────────────────────────────────

def _qc_series(df: pd.DataFrame, sources: list) -> pd.Series:
    """Coalesce the DB columns in `sources` [(column, scale), ...] into a single
    series expressed in QC units. Missing columns are ignored."""
    result = None
    for column, scale in sources:
        if column not in df.columns:
            continue
        series = pd.to_numeric(df[column], errors='coerce') * scale
        result = series if result is None else result.fillna(series)
    if result is None:
        return pd.Series(np.nan, index=df.index, dtype=np.float64)
    return result


def _uncertainty_columns(value_columns: set) -> list:
    """All uncertainty columns (from CDM_VARIABLES) of the given value columns."""
    uc_columns = []
    for entry in CDM_VARIABLES:
        (_, name_cf_1_4, name_cf_1_7, _, _,
         uc_rand, uc_sys_cf_1_4, uc_sys_cf_1_7, uc_tot_cf_1_4, uc_tot_cf_1_7) = entry
        if name_cf_1_4 in value_columns or name_cf_1_7 in value_columns:
            uc_columns += [c for c in (uc_rand, uc_sys_cf_1_4, uc_sys_cf_1_7,
                                       uc_tot_cf_1_4, uc_tot_cf_1_7) if c]
    return uc_columns


def apply_plausibility_qc(df_merged: pd.DataFrame,
                          nullify_uncertainties: bool = QC_NULLIFY_UNCERTAINTIES,
                          max_altitude_m=QC_MAX_ALTITUDE_M) -> pd.DataFrame:
    """
    Replace implausible values with NULL, level by level, using qc_pipeline.

    df_merged holds all the soundings of a month stacked together; each
    sounding is identified by g_product_id and is evaluated on its own by
    qc_pipeline.flag_implausible_levels. Must be called BEFORE UNIT_TRANSFORMS.
    """
    start = time.perf_counter()

    # 1) DataFrame in the format/units expected by qc_pipeline
    with TIMER('plausibility_qc/build_qc_input'):
        qc_df = pd.DataFrame(
            {name: _qc_series(df_merged, sources).to_numpy(dtype=np.float64)
             for name, sources in QC_INPUT_COLUMNS.items()},
            index=df_merged.index,
        )
        qc_df['profile_id'] = df_merged['g_product_id'].to_numpy()

    # 2) level-by-level flags, profile by profile
    with TIMER('plausibility_qc/flag_implausible_levels'):
        implausible = qc_pipeline.flag_implausible_levels(
            qc_df, profile_id_column='profile_id', max_altitude_m=max_altitude_m)

    # 3) replace implausible values with NULL
    print(f"  Plausibility QC ({qc_df['profile_id'].nunique():,} profiles):")
    for var in qc_pipeline.PLAUSIBILITY_LIMITS:
        mask = implausible[var].to_numpy()
        n_valid = int(qc_df[var].notna().sum())
        n_rejected = int(mask.sum())
        fraction = n_rejected / n_valid if n_valid else 0.0
        print(f"    {var:<18}: {n_rejected:>10,} / {n_valid:>12,} values set to NULL"
              f" ({100 * fraction:.3f}%)")
        if fraction > QC_REJECTION_WARN_FRACTION:
            print(f"    WARNING: more than {100 * QC_REJECTION_WARN_FRACTION:.0f}% of "
                  f"'{var}' rejected - check the units in QC_INPUT_COLUMNS.")
        if n_rejected == 0:
            continue
        value_columns = {c for c, _ in QC_INPUT_COLUMNS[var]}
        columns = set(value_columns)
        if nullify_uncertainties:
            columns.update(_uncertainty_columns(value_columns))
        for column in columns:
            if column in df_merged.columns:
                df_merged[column] = df_merged[column].mask(mask)

    print(f"  Plausibility QC time: {time.perf_counter() - start:.2f}s")
    return df_merged


# ── CDM pivot logic ───────────────────────────────────────────────────────────
def _col_or_nan(df: pd.DataFrame,
                column_name_cf_1_4: Optional[str] = None,
                column_name_cf_1_7: Optional[str] = None) -> pd.Series:
    # 1. Retrieve the 1_4 series if the column exists, otherwise None
    s1 = None
    if column_name_cf_1_4 and column_name_cf_1_4 in df.columns:
        s1 = pd.to_numeric(df[column_name_cf_1_4], errors='coerce')

    # 2. Retrieve the 1_7 series if the column exists, otherwise None
    s2 = None
    if column_name_cf_1_7 and column_name_cf_1_7 in df.columns:
        s2 = pd.to_numeric(df[column_name_cf_1_7], errors='coerce')

    # 3. Combine the two series handling the different cases
    if s1 is not None and s2 is not None:
        return s1.fillna(s2)
    elif s1 is not None:
        return s1
    elif s2 is not None:
        return s2

    # 4. Fallback if neither column exists or was provided
    return pd.Series(np.nan, index=df.index, dtype=np.float32)

def build_cdm_dataframe(df_merged: pd.DataFrame,
                        station_lookup: dict) -> pd.DataFrame:
    ts_raw = df_merged['report_timestamp']
    if pd.api.types.is_datetime64_any_dtype(ts_raw):
        if ts_raw.dt.tz is None:
            ts_raw = ts_raw.dt.tz_localize('UTC')
        else:
            ts_raw = ts_raw.dt.tz_convert('UTC')

        ts_epoch = ts_raw.values.astype('datetime64[s]').astype('int64')
    else:
        ts_epoch = pd.to_datetime(ts_raw, utc=True).values.astype(
            'datetime64[s]').astype('int64')

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
    sensor_id = _str_col('g_product_code', 'g_product_key')
    # report_id_str      = df_merged['report_id'].fillna(0).astype(np.int64).astype(str)
    lat_station        = _col_or_nan(df_merged, 'g_measuringsystem_latitude', 'g_measurementsystem_latitude')
    lon_station        = _col_or_nan(df_merged, 'g_measuringsystem_longitude', 'g_measurementsystem_longitude')
    alt_station        = _col_or_nan(df_merged, 'g_measuringsystem_altitude', 'g_measurementsystem_altitude')
    lat_obs            = _col_or_nan(df_merged, 'lat', 'lat')
    lon_obs            = _col_or_nan(df_merged, 'lon', 'lon')
    z_coord            = _col_or_nan(df_merged, 'alt', 'alt')

    # # GRUAN-specific corrections (same value broadcast to all variable rows)
    # cor_temp_vals = _col_or_nan(df_merged, 'temp_corr_rad')
    # cor_rh_vals   = _col_or_nan(df_merged, 'rh_corr') - _col_or_nan(df_merged, 'rh')

    # Strings are repeated for every CDM variable (x15). Keeping them as pandas
    # Categoricals (built ONCE, same categories in every piece) makes the
    # pd.concat below cheap and lets the writer skip hashing entirely.
    primary_station_id_c = primary_station_id.astype('category')
    station_name_c       = station_name.astype('category')
    sensor_id_c          = sensor_id.astype('category')
    report_id_c          = df_merged['g_product_id'].astype('category')

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
            # 'report_id':                          report_id_str.values,
            'report_id':                          report_id_c,
            'report_duration':                    np.uint8(9),
            'observation_id':                     df_merged['observation_id'].values,
            'primary_station_id':                 primary_station_id_c,
            'station_name|station_configuration': station_name_c,
            # Station geometry
            'latitude|station_configuration':     lat_station.values,
            'longitude|station_configuration':    lon_station.values,
            'height_of_station_above_sea_level':  alt_station.values,
            # Balloon position
            'latitude|observations_table':        lat_obs.values,
            'longitude|observations_table':       lon_obs.values,
            # station.id lookup
            'record_number':                      record_number_vals,
            'sensor_id':                          sensor_id_c,
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


def write_cdm_netcdf(cdm: pd.DataFrame, output_file: Path, threads: int = None):
    """Write a CDM long-format DataFrame to a GRUAN-convention NetCDF4 file.

    threads: compression threads (default DEFAULT_THREADS). 1 = sequential.
    """
    N = len(cdm)
    threads = DEFAULT_THREADS if threads is None else threads
    print(f"  CDM rows to write: {N:,}")

    codes_used  = sorted(set(ALL_CDM_CODES))
    labels_used = [ALL_CDM_LABELS.get(c, str(c)) for c in codes_used]
    t0 = time.perf_counter()

    with _NcWriter(output_file, N, threads=threads) as w:

        # ── sensor / station geometry ─────────────────────────────────────────
        w.char('sensor_id', cdm['sensor_id'])
        w.float32('height_of_station_above_sea_level',
                  cdm['height_of_station_above_sea_level'])
        w.char('primary_station_id', cdm['primary_station_id'])
        w.char('station_name|station_configuration',
               cdm['station_name|station_configuration'])
        w.float32('latitude|station_configuration',
                  cdm['latitude|station_configuration'])
        w.float32('longitude|station_configuration',
                  cdm['longitude|station_configuration'])

        # ── sounding / report ids ─────────────────────────────────────────────
        w.char('report_id', cdm['report_id'])
        w.uint8('report_duration', cdm['report_duration'])
        w.int64('observation_id', cdm['observation_id'])

        # ── record_number: station.id from station table ──────────────────────
        # build_cdm_dataframe uses int32-min as "unknown station" sentinel. A
        # plain astype(int8) would turn it into 0 (a valid-looking id), so it
        # is mapped explicitly to the int8 fill value.
        rec = cdm['record_number'].to_numpy()
        rec = np.where(rec == np.iinfo(np.int32).min,
                       np.iinfo(np.int8).min, rec).astype(np.int8)
        w._numeric('record_number', np.int8, rec, lambda b: b, shuffle=False,
                   fill_value=np.iinfo(np.int8).min,
                   long_name='station record number',
                   comment='integer primary key (id) of the matching row '
                           'in the station table')

        # ── time ──────────────────────────────────────────────────────────────
        w.int64('report_timestamp', cdm['report_timestamp'],
                units=TIME_UNITS, calendar=TIME_CALENDAR)
        w.uint8('report_meaning_of_timestamp', cdm['report_meaning_of_timestamp'])

        # ── balloon position ──────────────────────────────────────────────────
        w.float32('latitude|observations_table', cdm['latitude|observations_table'])
        w.float32('longitude|observations_table', cdm['longitude|observations_table'])

        # ── z coordinate ──────────────────────────────────────────────────────
        w.float32('z_coordinate', cdm['z_coordinate'])
        w.uint8('z_coordinate_type', cdm['z_coordinate_type'])

        # ── CDM core: observed variable & value ───────────────────────────────
        w._numeric('observed_variable', np.int16,
                   cdm['observed_variable'].to_numpy(),
                   lambda b: b.astype(np.int16, copy=False), shuffle=False,
                   codes=np.array(codes_used, dtype=np.int32),
                   labels=', '.join(labels_used))
        w.float32('observation_value', cdm['observation_value'])

        # ── units ─────────────────────────────────────────────────────────────
        w.uint16('units', cdm['units'])

        # ── uncertainties ─────────────────────────────────────────────────────
        for idx in (1, 2, 5):
            w.float32(f'uncertainty_value{idx}', cdm[f'uncertainty_value{idx}'])
            w.uint8(f'uncertainty_type{idx}',    cdm[f'uncertainty_type{idx}'])
            w.uint16(f'uncertainty_units{idx}',  cdm[f'uncertainty_units{idx}'])

    size_mb = output_file.stat().st_size / 1e6
    mode = f"{w.threads} threads" if w.parallel else "sequential"
    print(f"  Written: {output_file}  ({size_mb:.1f} MB, {mode}, "
          f"{time.perf_counter() - t0:.1f}s)")


# ── Main export logic ─────────────────────────────────────────────────────────

COLUMNS_DATA_TABLE = [
    "g_product_id", "\"asc\"", "alt", "alt_gph", "alt_gph_uc_tcor", "alt_gph_uc", "alt_uc",
    "fp", "fp_uc", "geopot", "idstation_pk", "lat", "lon",
    "observation_id", "press", "press_uc", "report_timestamp", "rh", "rh_res", "rh_uc", "res_rh",
    "rh_uc_tcor", "swrad", "temp", "temp_uc", "temp_uc_tcor", "u", "u_alt",
    "u_cor_rh", "u_cor_temp", "u_press", "u_rh", "u_std_rh", "u_std_temp",
    "u_swrad", "u_temp", "u_wdir", "u_wspeed", "v", "vspeed", "vspeed_uc",
    "wdir", "wdir_uc", "wmeri", "wmeri_uc", "wspeed", "wspeed_uc", "wvmr",
    "wvmr_vol", "wvmr_vol_uc", "wvmr_vol_uc_tcor", "wzon", "wzon_uc", "time"
]


# ── reading the data table ────────────────────────────────────────────────────
# Why two readers: pd.read_sql + psycopg2 turns every one of the ~80M values of
# a month into a Python object (~2.5M values/s) and transfers text, so reading
# was ~65% of the whole export although PostgreSQL itself needs only a few
# seconds. The ADBC reader streams the result as Arrow record batches over the
# binary COPY protocol (no per-value Python objects). Select it with
# --read-method adbc (pip install adbc-driver-postgresql pyarrow) and check it
# on one month first with --verify-read YYYY-MM.

def _data_query(year: int, month: int, data_table: str, max_alt_sql: str) -> str:
    # The altitude filter is pushed down to PostgreSQL (server-side), so only
    # the levels we actually need are transferred and held in memory.
    # NOTE: "alt <= X" alone would already drop NULLs (comparison yields NULL)
    # and NaNs (PostgreSQL sorts NaN above every other value), but the checks
    # are kept explicit for clarity and robustness.
    return (
        f"SELECT {', '.join(COLUMNS_DATA_TABLE)} "
        f"FROM {data_table}_{year:04d}{month:02d} "
        f"WHERE alt IS NOT NULL "
        f"  AND alt <> 'NaN'::float8 "
        f"  AND alt <= {max_alt_sql} "
        f"ORDER BY report_timestamp, observation_id"
    )


def read_data_sqlalchemy(conn_params, year, month, data_table):
    """Original reader: pandas.read_sql in chunks. Returns None if no rows."""
    engine = get_sqlalchemy_engine(conn_params)
    chunks = []
    query = _data_query(year, month, data_table, '%(max_alt)s')
    print("data_query: ", query)
    try:
        chunk_iterator = pd.read_sql(query, engine,
                                     params={'max_alt': MAX_ALTITUDE_M},
                                     chunksize=DATA_CHUNK_SIZE)
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
        return None
    t_concat = time.perf_counter()
    df = pd.concat(chunks, ignore_index=True)
    TIMER.add('db_read_data/concat_chunks', time.perf_counter() - t_concat)
    return df


def _adbc_uri(conn_params) -> str:
    return (f"postgresql://{quote(str(conn_params['user']), safe='')}:"
            f"{quote(str(conn_params['password']), safe='')}"
            f"@{conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}")


# ── float4 (REAL) columns: reproduce what psycopg2 delivers ──────────────────
# PostgreSQL REAL columns are sent as TEXT by the classic reader: since PG 12 the
# SHORTEST decimal that round-trips to the float4 (e.g. 205.37), which Python
# parses into a float64. ADBC delivers the exact float32 instead, which widened
# to float64 is 205.3699951171875. After the final cast to float32 (NetCDF) the
# two coincide for pass-through columns, but arithmetic in float64 (unit
# transforms) or comparisons against decimal thresholds (QC) could differ in
# the last bit. To make both readers return the SAME float64, the shortest
# round-tripping decimal is recomputed here, vectorised (bisection over 1..9
# significant digits). Validated against PostgreSQL's own text output on
# ~5M values (0 mismatches); ~6M values/s per thread.

# 10**k is exactly representable in float64 for k <= 22
_POW10 = 10.0 ** np.arange(0, 23, dtype=np.float64)


def _round_sig(x, e, d):
    """x rounded to d significant digits (decimal exponent e), as float64."""
    k = d - 1 - e
    p = _POW10[np.abs(k)]
    pos = k >= 0
    n = np.rint(np.where(pos, x * p, x / p))
    return np.where(pos, n / p, n * p)


def float32_as_pg_text_float64(x32: np.ndarray, block: int = 1 << 18) -> np.ndarray:
    """float32 -> float64, giving for every value the float64 that psycopg2 would
    obtain from PostgreSQL's text output of the same REAL (PostgreSQL >= 12 prints
    the SHORTEST decimal that round-trips to the float4, and float() parses it)."""
    x32 = np.ascontiguousarray(x32, dtype=np.float32)
    out = x32.astype(np.float64)
    ax = np.abs(out)
    fast = (ax >= 1e-12) & (ax < 1e12)               # excludes 0, NaN, inf
    idx = np.flatnonzero(fast)
    bad = []
    for s in range(0, idx.size, block):
        ii = idx[s:s + block]
        x = out[ii]
        t = x32[ii]
        e = np.floor(np.log10(np.abs(x))).astype(np.int64)
        # midpoints to the neighbouring float32 values (exact in float64). PostgreSQL's
        # shortest-digits output does NOT accept a decimal lying exactly on such a midpoint.
        mid_up = (x + np.nextafter(t, np.float32(np.inf)).astype(np.float64)) / 2
        mid_dn = (x + np.nextafter(t, np.float32(-np.inf)).astype(np.float64)) / 2

        def roundtrips(r):
            return (r.astype(np.float32) == t) & (r != mid_up) & (r != mid_dn)

        lo = np.ones(ii.size, dtype=np.int64)
        hi = np.full(ii.size, 9, dtype=np.int64)
        for _ in range(4):                           # shortest digit count: monotonic -> bisection on 1..9
            mid = (lo + hi) // 2
            good = roundtrips(_round_sig(x, e, mid))
            hi = np.where(good, mid, hi)
            lo = np.where(good, lo, mid + 1)
        r = _round_sig(x, e, hi)
        ok = roundtrips(r)                           # safety net (e.g. log10 off by one at powers of ten)
        out[ii[ok]] = r[ok]
        bad.append(ii[~ok])
    # rare leftovers + values outside the fast window (tiny/huge): exact but slow, per element
    rest = np.concatenate(bad + [np.flatnonzero(np.isfinite(out) & (out != 0) & ~fast)]) if bad else \
        np.flatnonzero(np.isfinite(out) & (out != 0) & ~fast)
    for i in rest:
        out[i] = _shortest_exact(x32[i])
    return out


def _shortest_exact(v):
    """Slow, per-element version of the same rule (used for the rare values that fall
    outside the vectorised window): fewest digits whose decimal round-trips to v
    and does not lie exactly on the midpoint to a neighbouring float32."""
    x = float(v)
    mid_up = (x + float(np.nextafter(v, np.float32(np.inf)))) / 2
    mid_dn = (x + float(np.nextafter(v, np.float32(-np.inf)))) / 2
    for precision in range(0, 9):                    # digits after the point in scientific notation
        r = float(np.format_float_scientific(v, precision=precision, unique=False))
        if np.float32(r) == v and r != mid_up and r != mid_dn:
            return r
    return x


def read_data_adbc(conn_params, year, month, data_table):
    """Arrow-native reader (binary COPY, no per-value Python objects).
    Returns a DataFrame with the same columns/dtypes as read_data_sqlalchemy,
    or None if no rows."""
    if not _HAVE_ADBC:
        raise RuntimeError("--read-method adbc needs: "
                           "pip install adbc-driver-postgresql pyarrow")
    query = _data_query(year, month, data_table, repr(float(MAX_ALTITUDE_M)))
    print("data_query: ", query)
    with adbc_dbapi.connect(_adbc_uri(conn_params)) as conn, conn.cursor() as cur:
        with TIMER('db_read_data/arrow_fetch'):
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(query)
            table = cur.fetch_arrow_table()
    if table.num_rows == 0:
        return None
    # NUMERIC -> float64, as psycopg2 + pandas (coerce_float) would give.
    # ADBC returns NUMERIC as an *opaque* Arrow type (string storage); passing
    # that to to_pandas() fails, so convert it here. Any other opaque type is
    # unknown territory: stop with a clear message instead of guessing.
    for i, field in enumerate(table.schema):
        ftype = field.type
        if hasattr(ftype, 'type_name') and hasattr(ftype, 'vendor_name'):   # pa.OpaqueType
            if ftype.type_name != 'numeric':
                raise RuntimeError(
                    f"column '{field.name}': PostgreSQL type '{ftype.type_name}' is not "
                    f"supported by --read-method adbc; use --read-method sqlalchemy")
            col = table.column(i)
            storage = pa.chunked_array([c.storage for c in col.chunks],
                                       type=ftype.storage_type)
            table = table.set_column(i, field.name, storage.cast(pa.float64()))
        elif pa.types.is_decimal(ftype):
            table = table.set_column(i, field.name, table.column(i).cast(pa.float64()))
    with TIMER('db_read_data/arrow_to_pandas'):
        df = table.to_pandas(self_destruct=True, split_blocks=True)
    del table
    # psycopg2 hands over Python int / float: keep int64 / float64, and for REAL
    # (float32) columns the very same float64 values the classic reader produces.
    f32_cols = []
    for c in df.columns:
        dt = df[c].dtype
        if dt.kind in 'iu' and dt.itemsize < 8:
            df[c] = df[c].astype(np.int64)
        elif dt.kind == 'f' and dt.itemsize < 8:
            f32_cols.append(c)
    if f32_cols:
        with TIMER('db_read_data/float32_to_double'):
            from concurrent.futures import ThreadPoolExecutor
            def _convert(c):
                arr = df[c].to_numpy()
                if np.isnan(arr).all():              # entirely NULL: nothing to recompute
                    return arr.astype(np.float64)
                return float32_as_pg_text_float64(arr)

            with ThreadPoolExecutor(max_workers=max(DEFAULT_THREADS, 1)) as pool:
                converted = list(pool.map(_convert, f32_cols))
            for c, values in zip(f32_cols, converted):
                df[c] = values
    return df


READ_METHODS = {'sqlalchemy': read_data_sqlalchemy, 'adbc': read_data_adbc}


def _frame_differences(a: pd.DataFrame, b: pd.DataFrame, notes: list = None):
    """List of human readable differences between two frames (empty = same).
    Harmless findings (columns that are entirely NULL in both frames) go to
    `notes` instead: the classic reader gives them dtype object (None), the
    Arrow reader float64 (NaN); every downstream use goes through pd.to_numeric."""
    out = []
    if len(a) != len(b):
        return [f"row count differs: {len(a):,} vs {len(b):,}"]
    if list(a.columns) != list(b.columns):
        return [f"columns differ: {list(a.columns)} vs {list(b.columns)}"]
    for c in a.columns:
        x, y = a[c], b[c]
        if x.dtype != y.dtype and x.isna().all() and y.isna().all():
            if notes is not None:
                notes.append(c)
            continue
        if x.dtype != y.dtype:
            out.append(f"{c}: dtype {x.dtype} vs {y.dtype}")
        if x.dtype.kind in 'iufb' and y.dtype.kind in 'iufb':
            xv, yv = x.to_numpy(np.float64), y.to_numpy(np.float64)
            n_bad = int((~((xv == yv) | (np.isnan(xv) & np.isnan(yv)))).sum())
        elif isinstance(x.dtype, pd.DatetimeTZDtype) or x.dtype.kind == 'M':
            xv = pd.to_datetime(x, utc=True).astype('int64').to_numpy()
            yv = pd.to_datetime(y, utc=True).astype('int64').to_numpy()
            n_bad = int((xv != yv).sum())
        else:
            xn, yn = x.isna().to_numpy(), y.isna().to_numpy()
            both = ~xn & ~yn
            n_bad = int((xn != yn).sum()) + int(
                (x.astype(object).to_numpy()[both] != y.astype(object).to_numpy()[both]).sum())
        if n_bad:
            out.append(f"{c}: {n_bad:,} differing values")
    return out


def verify_read_methods(conn_params, year, month, data_table='data') -> bool:
    """Read one month with BOTH readers, report time and any difference."""
    emit(f"\n── Verifying read methods on {year:04d}-{month:02d} ──")
    frames, times = {}, {}
    for name in ('sqlalchemy', 'adbc'):
        TIMER.reset()
        t0 = time.perf_counter()
        frames[name] = READ_METHODS[name](conn_params, year, month, data_table)
        times[name] = time.perf_counter() - t0
        n = 0 if frames[name] is None else len(frames[name])
        emit(f"  {name:<11} {times[name]:8.2f}s   {n:,} rows")
        for stage, (sec, _calls) in TIMER.t.items():
            emit(f"      {stage.split('/', 1)[-1]:<24}{sec:8.2f}s")
    TIMER.reset()
    a, b = frames['sqlalchemy'], frames['adbc']
    if a is None or b is None:
        emit("  no rows returned by one of the readers - nothing to compare"
             if (a is None) == (b is None) else "  ONE READER RETURNED NO ROWS")
        return (a is None) == (b is None)
    notes = []
    diffs = _frame_differences(a, b, notes)
    if notes:
        emit(f"  note: {len(notes)} column(s) are entirely NULL in this month "
             f"(object/None vs float64/NaN, harmless): {', '.join(notes)}")
    if diffs:
        emit("  DIFFERENCES FOUND:")
        for d in diffs:
            emit(f"    - {d}")
    else:
        emit(f"  identical: same columns, dtypes and values  "
             f"(adbc is {times['sqlalchemy'] / times['adbc']:.1f}x faster)")
    return not diffs


def export_month(conn_params, year, month, output_dir,
                 data_table='data', header_table='header',
                 station_lookup: dict = None, apply_qc: bool = True,
                 threads: int = None, read_method: str = 'sqlalchemy'):
    start_date = datetime(year, month, 1, tzinfo=timezone.utc)
    end_date   = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    output_file = output_dir / f"insitu-observations-gruan-reference-network_GRUAN_{year:04d}_{month:02d}.nc"
    if output_file.exists():
        print(f"  {output_file} already exists, skipping.")
        log(f"\n{year:04d}-{month:02d}: output already exists, skipped ({output_file.name})")
        return
    print(f"\n── Exporting {year:04d}-{month:02d}  →  {output_file}")
    TIMER.reset()

    # ── fetch data ────────────────────────────────────────────────────────────
    # (data columns: module-level COLUMNS_DATA_TABLE; query: _data_query)
    print("max altitude (m): ", MAX_ALTITUDE_M)
    print("start: ", start_date)
    print("end: ", end_date)
    print("read method: ", read_method)

    start_fetch_time = time.perf_counter()
    df_data = READ_METHODS[read_method](conn_params, year, month, data_table)

    if df_data is None:
        print("  No data rows with valid alt <= "
              f"{MAX_ALTITUDE_M} m — skipping.")
        log(f"\n{year:04d}-{month:02d}: no data rows with valid alt <= {MAX_ALTITUDE_M} m, skipped")
        return

    total_fetch_time = time.perf_counter() - start_fetch_time
    TIMER.add('db_read_data', total_fetch_time)
    print(
        f"  Data rows   : {len(df_data):,} (Total read time: {total_fetch_time:.2f}s)")

    # ── fetch header ──────────────────────────────────────────────────────────
    COLUMNS_HEADER_TABLE = [
        "g_general_sitecode", "g_measurementsystem_altitude",
        "g_measurementsystem_latitude",
        "g_measurementsystem_longitude", "g_measuringsystem_altitude",
        "g_measuringsystem_latitude",
        "g_measuringsystem_longitude", "g_site_key", "idstation_pk",
        "g_product_id", "report_timestamp", "g_product_key", "g_product_code",
        "g_general_sitename", "g_site_name"
    ]

    header_columns_str = ", ".join(COLUMNS_HEADER_TABLE)

    header_query = (
        f"SELECT {header_columns_str} "
        f"FROM {header_table} "
        f"WHERE report_timestamp >= %(start)s AND report_timestamp < %(end)s"
    )
    t_hdr = time.perf_counter()
    engine2 = get_sqlalchemy_engine(conn_params)
    try:
        df_header = pd.read_sql(header_query, engine2,
                                params={'start': start_date, 'end': end_date})
    finally:
        engine2.dispose()
    TIMER.add('db_read_header', time.perf_counter() - t_hdr)
    print(f"  Header rows : {len(df_header):,}")

    with TIMER('merge_data_header'):
        df_merged = pd.merge(
            df_data, df_header,
            on=['g_product_id'],
            how='inner', suffixes=('', '_header')
        ).copy()
    print(f"  Merged rows : {len(df_merged):,}")

    # ── plausibility QC (must run BEFORE the unit transforms) ─────────────────
    if apply_qc:
        with TIMER('plausibility_qc'):
            df_merged = apply_plausibility_qc(df_merged)

    with TIMER('unit_transforms'):
        for col, (scale, offset) in UNIT_TRANSFORMS.items():
            if col in df_merged.columns:
                df_merged[col] = df_merged[col] * scale + offset

    # ── pivot to CDM long format ──────────────────────────────────────────────
    with TIMER('build_cdm_dataframe'):
        cdm = build_cdm_dataframe(df_merged, station_lookup or {})
    print(f"  CDM rows    : {len(cdm):,}  ({len(CDM_VARIABLES)} vars × {len(df_merged):,} levels)")

    # ── write NetCDF ──────────────────────────────────────────────────────────
    with TIMER('write_netcdf'):
        write_cdm_netcdf(cdm, output_file, threads=threads)

    TIMER.report(f"{year:04d}-{month:02d}",
                 note=f"{len(df_merged):,} levels -> {len(cdm):,} CDM rows")
    RUN_TIMER.merge(TIMER)
    TIMER.reset()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    start_time = time.perf_counter()

    parser = argparse.ArgumentParser(
        description='Export GRUAN PostgreSQL data to CDM-compliant monthly NetCDF4.'
    )
    parser.add_argument('--env-file',     default='.env')
    parser.add_argument('--output-dir',   required=True)
    parser.add_argument('--start-year',   type=int)
    parser.add_argument('--end-year',     type=int)
    parser.add_argument('--year',         type=int,
                         help='Export a single year. Combine with --month to export a single month.')
    parser.add_argument('--month',        type=int, choices=range(1, 13), metavar='[1-12]',
                         help='Month to export (1-12). Requires --year.')
    parser.add_argument('--data-table',   default='data')
    parser.add_argument('--header-table', default='header')
    parser.add_argument('--threads',      type=int, default=DEFAULT_THREADS,
                         help='Threads used to compress the NetCDF chunks '
                              '(needs h5py; 1 = sequential). Default: %(default)s')
    parser.add_argument('--read-method',  choices=sorted(READ_METHODS), default='sqlalchemy',
                         help="How the data table is read: 'sqlalchemy' (pandas.read_sql, "
                              "default) or 'adbc' (Arrow/binary COPY, much faster; needs "
                              "`pip install adbc-driver-postgresql pyarrow`).")
    parser.add_argument('--verify-read',  metavar='YYYY-MM', default=None,
                         help='Read that month with BOTH methods, compare them and exit '
                              '(no export). Run it once before using --read-method adbc.')
    parser.add_argument('--log-dir',      default=None,
                         help='Directory of the timing log '
                              '(export_netcdf_timing_<YYYYmmdd_HHMMSS>.log). '
                              'Default: the output directory.')
    parser.add_argument('--no-log', action='store_true',
                         help='Do not write the timing log file.')
    parser.add_argument('--skip-qc', action='store_true',
                        help='Do not replace implausible values with NULL.')
    args = parser.parse_args()

    if args.month and not args.year:
        sys.exit('--month requires --year to be set as well.')

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

    if not args.no_log:
        import pandas, numpy
        log_path = init_log(args.log_dir or output_dir, [
            f"command : {' '.join(sys.argv)}",
            f"host    : {platform.node()} ({platform.system()} {platform.release()}), "
            f"{os.cpu_count()} logical CPUs ({_available_cpus()} usable by this process)",
            f"python  : {platform.python_version()} | pandas {pandas.__version__} | "
            f"numpy {numpy.__version__} | netCDF4 {nc.__version__} | "
            f"h5py {h5py.__version__ if _HAVE_H5PY else 'NOT INSTALLED'}",
            f"settings: threads={args.threads} (parallel compression "
            f"{'ON' if args.threads > 1 and _HAVE_H5PY else 'OFF'}), "
            f"read={args.read_method}, "
            f"deflate={DEFLATE_LEVEL}, qc={'off' if args.skip_qc else 'on'}, "
            f"max_alt={MAX_ALTITUDE_M} m",
            f"output  : {output_dir}",
        ])
        print(f"Timing log: {log_path}")

    if args.verify_read:
        try:
            vy, vm = (int(x) for x in args.verify_read.split('-'))
            assert 1 <= vm <= 12
        except (ValueError, AssertionError):
            sys.exit('--verify-read expects YYYY-MM, e.g. 2020-10')
        ok = verify_read_methods(conn_params, vy, vm, args.data_table)
        sys.exit(0 if ok else 1)

    _t = time.perf_counter()
    months = get_available_months(conn_params, args.header_table)
    RUN_TIMER.add('startup: list_available_months', time.perf_counter() - _t)
    if not months:
        sys.exit('No data found.')
    if args.year and args.month:
        # Single-month export: overrides any --start-year/--end-year range.
        months = [(y, m) for y, m in months if y == args.year and m == args.month]
        if not months:
            sys.exit(f'No data found for {args.year:04d}-{args.month:02d}.')
    else:
        if args.year:
            months = [(y, m) for y, m in months if y == args.year]
        if args.start_year:
            months = [(y, m) for y, m in months if y >= args.start_year]
        if args.end_year:
            months = [(y, m) for y, m in months if y <= args.end_year]

    # print(f'Months to export: {len(months)}')
    _t = time.perf_counter()
    station_lookup = load_station_record_numbers(conn_params)
    RUN_TIMER.add('startup: station_lookup', time.perf_counter() - _t)
    try:
        for y, m in months:
            export_month(conn_params, y, m, output_dir, args.data_table, args.header_table,
                         station_lookup=station_lookup, apply_qc=not args.skip_qc,
                         threads=args.threads, read_method=args.read_method)
    except BaseException as exc:
        # keep what was measured so far, and record why the run stopped
        log(f"\n!! RUN INTERRUPTED after {time.perf_counter() - start_time:.2f}s: "
            f"{type(exc).__name__}: {exc}")
        if not isinstance(exc, KeyboardInterrupt):
            log(traceback.format_exc())
        RUN_TIMER.report("PARTIAL RUN (interrupted)")
        raise

    print('\nDone.')
    RUN_TIMER.report(f"WHOLE RUN ({len(months)} month(s))")

    end_time = time.perf_counter()
    elapsed_time = end_time - start_time

    emit(f"Total execution time: {elapsed_time:.2f} seconds")


if __name__ == '__main__':
    main()