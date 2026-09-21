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

FILE_PATH_CDM = '/Data/GRUAN_TEST/output/insitu-observations-gruan-reference-network_GRUAN_2021_09.nc'
FILE_PATH_GRUAN = '/Data/GRUAN_TEST/netcdf/LIN-RS-01_2_RS41-GDP_001_20260323T060000_1-000-001.nc'

OUTPUT_PATH_CDM = "/Data/GRUAN_TEST/output/CDM_data.csv"
OUTPUT_PATH_GRUAN = "/Data/GRUAN_TEST/output/GRUAN_data.csv"

# Number of worker processes used for the parallel CSV export.
# Only relevant on Linux/macOS, where the "fork" start method lets worker
# processes access the parent's arrays without copying them.
N_WORKERS = min(24, os.cpu_count() or 24)


def extract_flat(var) -> np.ndarray:
    """Converts a netCDF4 variable (possibly a masked array) into a flat numpy array."""
    if np.ma.is_masked(var):
        return var.filled(np.nan).ravel()
    return np.asarray(var).ravel()


# -----------------------------------------------------------------------------
# Parallel CSV export
# -----------------------------------------------------------------------------
# These module-level globals are populated in the parent process *before* the
# worker pool is created. Because we use the "fork" start method, each worker
# process inherits a copy-on-write view of the parent's memory, so the (large)
# arrays are never pickled/copied across processes - only the small
# (start, end, tmp_path) tuples are sent to each worker.
_shared_columns = None
_shared_arrays = None


def _write_chunk(start: int, end: int, tmp_path: str) -> str:
    """Writes rows [start:end) of the shared arrays to a headerless CSV chunk."""
    chunk = {col: arr[start:end] for col, arr in zip(_shared_columns, _shared_arrays)}
    pd.DataFrame(chunk).to_csv(tmp_path, index=False, header=False, na_rep="")
    return tmp_path


def export_parallel_csv(columns_data: dict, output_path: str, n_workers: int, tag: str) -> None:
    """Writes `columns_data` (dict[column_name -> 1D numpy array]) to `output_path`
    as a single CSV file, splitting the work across `n_workers` processes and
    merging the resulting chunk files at the end."""
    global _shared_columns, _shared_arrays

    _shared_columns = list(columns_data.keys())
    _shared_arrays = [columns_data[col] for col in _shared_columns]
    n_rows = len(_shared_arrays[0])

    n_workers = max(1, min(n_workers, n_rows))
    chunk_bounds = np.linspace(0, n_rows, n_workers + 1, dtype=np.int64)

    out_dir = os.path.dirname(os.path.abspath(output_path))
    tmp_paths = [os.path.join(out_dir, f".{tag}_chunk_{i}.csv.part") for i in range(n_workers)]

    t0 = time.perf_counter()
    ctx = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=n_workers, mp_context=ctx) as executor:
        futures = []
        for i in range(n_workers):
            start, end = int(chunk_bounds[i]), int(chunk_bounds[i + 1])
            if start == end:
                continue
            futures.append(executor.submit(_write_chunk, start, end, tmp_paths[i]))
        for future in futures:
            future.result()
    logging.info(
        f"[{tag}] {n_workers} chunks written in parallel in {time.perf_counter() - t0:.2f}s")

    t0 = time.perf_counter()
    with open(output_path, "wb") as out_f:
        out_f.write((",".join(_shared_columns) + "\n").encode("utf-8"))
        for p in tmp_paths:
            if os.path.exists(p):
                with open(p, "rb") as in_f:
                    shutil.copyfileobj(in_f, out_f, length=1024 * 1024)
                os.remove(p)
    logging.info(f"[{tag}] Chunks merged into final CSV in {time.perf_counter() - t0:.2f}s")

    _shared_columns = None
    _shared_arrays = None


# -----------------------------------------------------------------------------
# CDM export
# -----------------------------------------------------------------------------
def process_cdm(file_path: str, output_path: str, n_workers: int = N_WORKERS) -> None:
    """Processes the large CDM NetCDF file and exports it to CSV, in parallel."""
    start_total = time.perf_counter()
    logging.info("[CDM] Starting processing...")

    t0 = time.perf_counter()
    with Dataset(file_path, mode="r") as nc_file:
        logging.info(f"[CDM] File opened in {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        obs_var = nc_file.variables["observed_variable"][:]
        obs_val = nc_file.variables["observation_value"][:]
        obs_unc_2 = nc_file.variables["uncertainty_value2"][:]
        obs_unc_5 = nc_file.variables["uncertainty_value5"][:]
        logging.info(
            f"[CDM] Raw variables read into memory in {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        var_data = extract_flat(obs_var)
        val_data = extract_flat(obs_val)
        val_unc_2 = extract_flat(obs_unc_2)
        val_unc_5 = extract_flat(obs_unc_5)
        logging.info(
            f"[CDM] Masked array conversion and flattening in {time.perf_counter() - t0:.2f}s")

    logging.info(f"[CDM] {len(var_data):,} rows ready for export")

    columns_data = {
        "observed_variable": var_data,
        "observation_value": val_data,
        "uncertainty_value2": val_unc_2,
        "uncertainty_value5": val_unc_5,
    }
    export_parallel_csv(columns_data, output_path, n_workers, tag="CDM")

    logging.info(
        f"[CDM] Total processing completed in {time.perf_counter() - start_total:.2f}s")


# -----------------------------------------------------------------------------
# GRUAN export
# -----------------------------------------------------------------------------
def process_gruan(file_path: str, output_path: str, n_workers: int = N_WORKERS) -> None:
    """Processes the GRUAN NetCDF file and exports it to CSV, in parallel."""
    start_total = time.perf_counter()
    logging.info("[GRUAN] Starting processing...")

    t0 = time.perf_counter()
    with Dataset(file_path, mode="r") as nc_file:
        logging.info(f"[GRUAN] File opened in {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        press = nc_file.variables["press"][:]
        press_uc = nc_file.variables["press_uc"][:]
        wvmr_vol = nc_file.variables["wvmr_vol"][:]
        wvmr_vol_uc_tcor = nc_file.variables["wvmr_vol_uc_tcor"][:]
        wvmr_vol_uc = nc_file.variables["wvmr_vol_uc"][:]
        logging.info(
            f"[GRUAN] Raw variables read into memory in {time.perf_counter() - t0:.2f}s")

        t0 = time.perf_counter()
        press_data = extract_flat(press)
        press_uc_data = extract_flat(press_uc)
        wvmr_vol_data = extract_flat(wvmr_vol)
        wvmr_vol_uc_tcor_data = extract_flat(wvmr_vol_uc_tcor)
        wvmr_vol_uc_data = extract_flat(wvmr_vol_uc)
        logging.info(
            f"[GRUAN] Masked array conversion and flattening in {time.perf_counter() - t0:.2f}s")

    logging.info(f"[GRUAN] {len(press_data):,} rows ready for export")

    columns_data = {
        "press": press_data,
        "press_uc": press_uc_data,
        "wvmr_vol": wvmr_vol_data,
        "wvmr_vol_uc_tcor": wvmr_vol_uc_tcor_data,
        "wvmr_vol_uc": wvmr_vol_uc_data,
    }
    export_parallel_csv(columns_data, output_path, n_workers, tag="GRUAN")

    logging.info(
        f"[GRUAN] Total processing completed in {time.perf_counter() - start_total:.2f}s")


if __name__ == "__main__":
    script_start = time.perf_counter()

    # CDM and GRUAN are processed sequentially at the top level: each one
    # already uses all N_WORKERS cores internally for the CSV export, so
    # running them concurrently as well would just make them fight over
    # the same cores instead of speeding anything up.
    if EXPORT_CDM:
        process_cdm(FILE_PATH_CDM, OUTPUT_PATH_CDM, n_workers=N_WORKERS)
    else:
        logging.info("[CDM] Export skipped by configuration.")

    if EXPORT_GRUAN:
        process_gruan(FILE_PATH_GRUAN, OUTPUT_PATH_GRUAN, n_workers=N_WORKERS)
    else:
        logging.info("[GRUAN] Export skipped by configuration.")

    logging.info(
        f"All active tasks executed in {time.perf_counter() - script_start:.2f}s")