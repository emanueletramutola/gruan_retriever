import glob
import logging
import multiprocessing
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandas as pd
from netCDF4 import Dataset

# -----------------------------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)

# -----------------------------------------------------------------------------
# Export Configuration
# -----------------------------------------------------------------------------
EXPORT_CDM = True
EXPORT_GRUAN = False

# INPUT_DIR_CDM = '/storage/baron-data/GRUAN_CDS'
INPUT_DIR_CDM = '/Data/GRUAN_TEST/ema'
INPUT_DIR_GRUAN = '/Data/GRUAN_TEST/netcdf'

# INTERMEDIATE_DIR_CDM = '/storage/baron-data/tmp'
INTERMEDIATE_DIR_CDM = '/Data/GRUAN_TEST/ema/tmp'
INTERMEDIATE_DIR_GRUAN = '/Data/GRUAN_TEST/output/csv_parts_gruan'

# OUTPUT_PATH_CDM = '/storage/baron-data/tmp/CDM_data.csv'
OUTPUT_PATH_CDM = '/Data/GRUAN_TEST/ema/CDM_data.csv'
OUTPUT_PATH_GRUAN = '/Data/GRUAN_TEST/output/GRUAN_data.csv'

NETCDF_PATTERN = "*.nc"

# -----------------------------------------------------------------------------
# Columns to export
# -----------------------------------------------------------------------------
CDM_COLUMNS = [
    "primary_station_id",
    "report_timestamp",
    "observed_variable",
    "observation_value",
    "uncertainty_value5",
]

GRUAN_COLUMNS = [
    "press",
    "press_uc",
    "wvmr_vol",
    "wvmr_vol_uc_tcor",
    "wvmr_vol_uc",
]

# -----------------------------------------------------------------------------
# Row filter
# -----------------------------------------------------------------------------
CDM_FILTER_COLUMN = "observed_variable"
CDM_FILTER_VALUES = [126]

GRUAN_FILTER_COLUMN = None
GRUAN_FILTER_VALUES = None

# -----------------------------------------------------------------------------
# Timestamp conversion
# -----------------------------------------------------------------------------
# Columns expressed as "seconds since 1970-01-01 00:00:00" (UTC) that must be
# written to the CSV as "YYYY-MM-DD HH:mm:ss".
# Each name listed here MUST also appear in the corresponding *_COLUMNS list,
# otherwise it will simply not be exported (a warning is logged at startup).
CDM_TIMESTAMP_COLUMNS = ["report_timestamp"]
GRUAN_TIMESTAMP_COLUMNS = []

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# -----------------------------------------------------------------------------
# Sort Configuration
# -----------------------------------------------------------------------------
CDM_SORT_COLUMNS = ["primary_station_id", "report_timestamp"]
GRUAN_SORT_COLUMNS = ["press"]

# -----------------------------------------------------------------------------
# Parallelism & memory
# -----------------------------------------------------------------------------
N_WORKERS = min(os.cpu_count() or 8, 16)
KEEP_PARTIAL_CSV = False


def extract_flat(var) -> np.ndarray:
    """Convert a netCDF4 variable (possibly a masked array) into a flat 1-D array.

    NC_CHAR variables with a trailing 'string length' dimension, i.e. 2-D
    arrays of dtype 'S1' (e.g. primary_station_id with shape (n, 3)), are
    collapsed into a 1-D array of strings, one per record.
    """
    data = var[:]

    # NC_CHAR with a trailing string-length dimension -> collapse to 1-D strings
    if isinstance(data, np.ndarray) and data.dtype.kind == 'S' and data.ndim == 2:
        if np.ma.is_masked(data):
            data = data.filled(b'\x00')
        data = np.ascontiguousarray(data)
        strlen = data.shape[1]
        # view (n, strlen) of 'S1' as (n,) of 'S<strlen>' (no deprecation, pure view)
        strings = data.view(f'S{strlen}').ravel()
        # strip NUL padding and decode to Python str
        return np.array(
            [s.rstrip(b'\x00').decode('utf-8', errors='replace') for s in strings],
            dtype=object,
        )

    # Original behaviour for everything else
    if np.ma.is_masked(data):
        return data.filled(np.nan).ravel()
    return np.asarray(data).ravel()


def _epoch_seconds_to_string(series: pd.Series) -> pd.Series:
    """Converts an epoch-seconds series (float/int, possibly with NaN) into a
    'YYYY-MM-DD HH:mm:ss' string series (NaT/NaN → empty string at CSV write).

    The epoch is interpreted as UTC, consistent with the NetCDF/CF convention
    'seconds since 1970-01-01 00:00:00'."""
    dt = pd.to_datetime(series, unit="s", errors="coerce")
    return dt.dt.strftime(TIMESTAMP_FORMAT)


# -----------------------------------------------------------------------------
# Per-file worker
# -----------------------------------------------------------------------------
def _process_one_file(args):
    """Reads ONE NetCDF file, filters, sorts, converts timestamps and writes
    a per-file CSV.

    Returns (status, file_name, message, n_in, n_out)."""
    (file_path, out_csv, columns, sort_columns,
     filter_column, filter_values, timestamp_columns) = args
    fname = os.path.basename(file_path)
    try:
        with Dataset(file_path, mode="r") as nc_file:
            # The filter column may need to be read even if not exported
            read_columns = list(columns)
            if filter_column and filter_values and filter_column not in read_columns:
                read_columns.append(filter_column)

            available = set(nc_file.variables.keys())
            missing = [c for c in read_columns if c not in available]
            if missing:
                return ("skip", fname, f"missing columns: {missing}", 0, 0)
            data = {col: extract_flat(nc_file.variables[col][:])
                    for col in read_columns}

        # Sanity: all selected columns must have the same number of elements
        lengths = {k: len(v) for k, v in data.items()}
        if len(set(lengths.values())) > 1:
            return ("skip", fname, f"mismatched column lengths: {lengths}", 0, 0)

        df = pd.DataFrame(data)
        n_in = len(df)

        # --- Filter (before sort, minimizes sort cost) ---
        if filter_column and filter_values:
            mask = df[filter_column].isin(filter_values)
            df = df.loc[mask].reset_index(drop=True)
            if filter_column not in columns:
                df = df.drop(columns=[filter_column])
            if df.empty:
                return ("ok", fname, "0 rows after filter", n_in, 0)

        n_out = len(df)

        # --- Sort (local to this file, on original numeric values) ---
        if sort_columns:
            df.sort_values(by=sort_columns, inplace=True,
                           ignore_index=True, kind="mergesort")

        # --- Timestamp conversion (after sort: only output representation) ---
        if timestamp_columns:
            for col in timestamp_columns:
                if col in df.columns:
                    df[col] = _epoch_seconds_to_string(df[col])

        # Atomic write: tmp + rename
        tmp = out_csv + ".tmp"
        df.to_csv(tmp, index=False, na_rep="")
        os.replace(tmp, out_csv)
        return ("ok", fname, "", n_in, n_out)

    except Exception as exc:
        return ("error", fname, str(exc), 0, 0)


# -----------------------------------------------------------------------------
# Streaming concat
# -----------------------------------------------------------------------------
def _concat_csvs(partial_csvs, output_path):
    with open(output_path, "wb") as out_f:
        for i, p in enumerate(partial_csvs):
            with open(p, "rb") as in_f:
                if i > 0:
                    in_f.readline()  # drop header of subsequent files
                shutil.copyfileobj(in_f, out_f, length=8 * 1024 * 1024)


# -----------------------------------------------------------------------------
# Main driver
# -----------------------------------------------------------------------------
def process_directory(input_dir: str,
                      intermediate_dir: str,
                      output_path: str,
                      columns: list,
                      sort_columns: list,
                      filter_column: str = None,
                      filter_values: list = None,
                      timestamp_columns: list = None,
                      n_workers: int = N_WORKERS,
                      tag: str = "NETCDF",
                      pattern: str = NETCDF_PATTERN) -> None:
    start_total = time.perf_counter()
    logging.info(f"[{tag}] Input dir: {input_dir}")

    timestamp_columns = timestamp_columns or []

    # ---- Config validation ----
    bad_sort = [c for c in sort_columns if c not in columns]
    if bad_sort:
        logging.error(f"[{tag}] Sort columns not in export columns: {bad_sort}. Aborting.")
        return

    bad_ts = [c for c in timestamp_columns if c not in columns]
    if bad_ts:
        logging.warning(
            f"[{tag}] Timestamp columns not in export columns, will be ignored: {bad_ts}")

    if (filter_values is not None and len(filter_values) > 0) and not filter_column:
        logging.error(f"[{tag}] Filter values set but filter_column is empty. Aborting.")
        return
    filter_enabled = bool(filter_column and filter_values)
    if filter_enabled:
        logging.info(
            f"[{tag}] Filter active: {filter_column} in {sorted(set(filter_values))}")
    if timestamp_columns:
        logging.info(
            f"[{tag}] Timestamp conversion: {timestamp_columns} → '{TIMESTAMP_FORMAT}' (UTC)")

    files = sorted(glob.glob(os.path.join(input_dir, pattern)))
    files = [f for f in files if os.path.abspath(f) != os.path.abspath(output_path)]
    if not files:
        logging.error(f"[{tag}] No files matching '{pattern}' in {input_dir}. Aborting.")
        return

    logging.info(f"[{tag}] Found {len(files)} file(s) "
                 f"(sort columns: {sort_columns or 'disabled'})")

    os.makedirs(intermediate_dir, exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    jobs = []
    partial_csvs = []
    for i, fp in enumerate(files):
        base = os.path.splitext(os.path.basename(fp))[0]
        out_csv = os.path.join(intermediate_dir, f"{i:05d}_{base}.csv")
        partial_csvs.append(out_csv)
        jobs.append((fp, out_csv, columns, sort_columns,
                     filter_column, filter_values if filter_enabled else None,
                     timestamp_columns))

    # ---- Phase 1: per-file, in parallel ----
    ctx = multiprocessing.get_context("fork")
    ok = skipped = failed = 0
    total_in = total_out = 0
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as executor:
        for status, fname, msg, n_in, n_out in executor.map(_process_one_file, jobs):
            if status == "ok":
                ok += 1
                total_in += n_in
                total_out += n_out
                if n_in != n_out:
                    logging.info(f"[{tag}] [{fname}] OK "
                                 f"({n_out:,} kept / {n_in:,} read)")
                else:
                    logging.info(f"[{tag}] [{fname}] OK ({n_out:,} rows)")
            elif status == "skip":
                skipped += 1
                logging.warning(f"[{tag}] [{fname}] SKIP - {msg}")
            else:
                failed += 1
                logging.error(f"[{tag}] [{fname}] FAIL - {msg}")

    logging.info(
        f"[{tag}] Phase 1 (per-file): {ok} ok, {skipped} skip, {failed} fail. "
        f"Rows: {total_out:,} kept / {total_in:,} read. "
        f"Elapsed {time.perf_counter() - t0:.1f}s")

    # ---- Phase 2: concat ----
    existing = [p for p in partial_csvs if os.path.exists(p)]
    if not existing:
        logging.error(f"[{tag}] No partial CSV to merge. Aborting.")
        return

    t0 = time.perf_counter()
    _concat_csvs(existing, output_path)
    logging.info(f"[{tag}] Phase 2 (concat {len(existing)} file(s)) in "
                 f"{time.perf_counter() - t0:.1f}s → {output_path}")

    if not KEEP_PARTIAL_CSV:
        shutil.rmtree(intermediate_dir, ignore_errors=True)
        logging.info(f"[{tag}] Removed intermediate directory: {intermediate_dir}")

    logging.info(f"[{tag}] Total {time.perf_counter() - start_total:.1f}s")


# -----------------------------------------------------------------------------
# Public wrappers
# -----------------------------------------------------------------------------
def process_cdm(input_dir, intermediate_dir, output_path, n_workers=N_WORKERS):
    process_directory(input_dir, intermediate_dir, output_path,
                      CDM_COLUMNS, CDM_SORT_COLUMNS,
                      filter_column=CDM_FILTER_COLUMN,
                      filter_values=CDM_FILTER_VALUES,
                      timestamp_columns=CDM_TIMESTAMP_COLUMNS,
                      n_workers=n_workers, tag="CDM")


def process_gruan(input_dir, intermediate_dir, output_path, n_workers=N_WORKERS):
    process_directory(input_dir, intermediate_dir, output_path,
                      GRUAN_COLUMNS, GRUAN_SORT_COLUMNS,
                      filter_column=GRUAN_FILTER_COLUMN,
                      filter_values=GRUAN_FILTER_VALUES,
                      timestamp_columns=GRUAN_TIMESTAMP_COLUMNS,
                      n_workers=n_workers, tag="GRUAN")


if __name__ == "__main__":
    script_start = time.perf_counter()

    if EXPORT_CDM:
        process_cdm(INPUT_DIR_CDM, INTERMEDIATE_DIR_CDM, OUTPUT_PATH_CDM,
                    n_workers=N_WORKERS)
    else:
        logging.info("[CDM] Export skipped by configuration.")

    if EXPORT_GRUAN:
        process_gruan(INPUT_DIR_GRUAN, INTERMEDIATE_DIR_GRUAN, OUTPUT_PATH_GRUAN,
                      n_workers=N_WORKERS)
    else:
        logging.info("[GRUAN] Export skipped by configuration.")

    logging.info(
        f"All active tasks executed in {time.perf_counter() - script_start:.1f}s")