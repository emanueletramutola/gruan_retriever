#!/usr/bin/env python3
"""
Compute min / max of observation_value and of the three uncertainty variables
(random, systematic, total) for each observed_variable, for every NetCDF file
found in a directory.

Variable mapping (CDM uncertainty types):
    uncertainty_value1 -> random
    uncertainty_value2 -> systematic
    uncertainty_value5 -> total

The files are read in chunks along the "index" dimension, so memory usage stays
low and constant regardless of file size (files of 1 GB or more are fine).

Numeric codes are decoded with the official CDM lookup tables
(https://github.com/ecmwf-projects/cdm-obs), downloaded at start-up.
Use --units-table / --variable-table to point to local copies (offline use).

Output: one CSV file with one row per (file, observed_variable).

Usage examples
--------------
    (edit INPUT_DIR / OUTPUT_CSV / N_WORKERS at the top of this file, then)
    python gruan_minmax_summary.py

    Any of them can be overridden from the command line:
    python gruan_minmax_summary.py /path/to/netcdf_dir
    python gruan_minmax_summary.py /path/to/netcdf_dir -o summary.csv
    python gruan_minmax_summary.py /path/to/netcdf_dir -r --pattern "*.nc"
    python gruan_minmax_summary.py /path/to/netcdf_dir --chunk-size 2000000
    python gruan_minmax_summary.py /path/to/netcdf_dir --workers 4

Requirements: numpy, pandas, netCDF4  (pip install numpy pandas netCDF4)
"""

import argparse
import logging
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import netCDF4
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# USER SETTINGS - edit these values to change the defaults.
# Each one can still be overridden from the command line.
# --------------------------------------------------------------------------- #
# INPUT_DIR = "/Data/GRUAN_CDS"         # directory containing the NetCDF files
INPUT_DIR = "/Data/GRUAN_TEST/output"         # directory containing the NetCDF files
# OUTPUT_CSV = "/home/emanuele/logs/gruan_check_nc_CDM.csv"  # output CSV: full path + file name
OUTPUT_CSV = "/Data/GRUAN_TEST/output/gruan_check_nc_CDM.csv"  # output CSV: full path + file name
N_WORKERS = 23                             # parallel worker processes (1 = serial)

# --------------------------------------------------------------------------- #
# Configuration (normally no need to change anything below this point)
# --------------------------------------------------------------------------- #
GROUP_VAR = "observed_variable"
UNITS_VAR = "units"  # numeric code of the units of observation_value

# NetCDF variable -> (label used in the CSV, matching "uncertainty_unitsN" var)
VALUE_VARS = {
    "observation_value": ("observation_value", None),
    "uncertainty_value1": ("uncertainty_random", "uncertainty_units1"),
    "uncertainty_value2": ("uncertainty_systematic", "uncertainty_units2"),
    "uncertainty_value5": ("uncertainty_total", "uncertainty_units5"),
}

CDM_BASE = "https://raw.githubusercontent.com/ecmwf-projects/cdm-obs/refs/heads/main/tables/"
UNITS_TABLE_URL = CDM_BASE + "units.csv"
VARIABLE_TABLE_URL = CDM_BASE + "observed_variable.csv"

DEFAULT_CHUNK_SIZE = 5_000_000  # rows read at once (~20 MB per float32 variable)

log = logging.getLogger("gruan_minmax")


# --------------------------------------------------------------------------- #
# Lookup tables
# --------------------------------------------------------------------------- #
def load_lookups(units_src: str, variable_src: str):
    """
    Return two dicts:
        units_lookup[code]    -> (name, abbreviation)
        variable_lookup[code] -> name
    If a table cannot be loaded, the corresponding dict is empty and the
    CSV will contain empty name columns (numeric codes are always kept).
    """
    units_lookup, variable_lookup = {}, {}

    try:
        u = pd.read_csv(units_src, dtype=str, keep_default_na=False)
        for _, r in u.iterrows():
            abbr = r["abbreviation"].replace("\\%", "%")  # table escapes '%'
            units_lookup[int(r["units"])] = (r["name"], abbr)
        log.info("Units table loaded (%d entries)", len(units_lookup))
    except Exception as exc:
        log.warning("Could not load units table (%s): %s", units_src, exc)

    try:
        v = pd.read_csv(variable_src, dtype=str, keep_default_na=False)
        for _, r in v.iterrows():
            variable_lookup[int(r["variable"])] = r["name"]
        log.info("Observed-variable table loaded (%d entries)", len(variable_lookup))
    except Exception as exc:
        log.warning("Could not load observed_variable table (%s): %s", variable_src, exc)

    return units_lookup, variable_lookup


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #
def analyse_file(path: Path, chunk_size: int) -> pd.DataFrame:
    """
    Analyse a single NetCDF file. Returns one row per observed_variable with
    raw codes and, for every variable in VALUE_VARS: n_valid, min, max.
    NaN / masked / infinite values are ignored.
    """
    with netCDF4.Dataset(path, "r") as ds:
        needed = [GROUP_VAR] + list(VALUE_VARS)
        missing = [v for v in needed if v not in ds.variables]
        if missing:
            raise KeyError(f"missing variable(s): {', '.join(missing)}")

        # Plain numpy arrays instead of masked arrays (masked floats become NaN)
        for name in ds.variables:
            if name in needed or name == UNITS_VAR or name.startswith("uncertainty_units"):
                ds.variables[name].set_auto_mask(False)
                ds.variables[name].set_auto_scale(False)

        group_var = ds.variables[GROUP_VAR]
        group_fill = getattr(group_var, "_FillValue", None)
        has_units = UNITS_VAR in ds.variables
        n_total = group_var.shape[0]

        partial = []           # per-chunk aggregated DataFrames
        units_seen = {}        # observed_variable code -> set of units codes
        unc_mismatch = set()   # observed_variable codes with uncertainty units != obs units

        for start in range(0, n_total, chunk_size):
            stop = min(start + chunk_size, n_total)
            data = {GROUP_VAR: group_var[start:stop]}
            for v in VALUE_VARS:
                arr = np.asarray(ds.variables[v][start:stop], dtype="float64")
                arr[~np.isfinite(arr)] = np.nan  # drop NaN and +/-inf
                data[v] = arr
            if has_units:
                data[UNITS_VAR] = ds.variables[UNITS_VAR][start:stop]
                # Uncertainty units, needed only to verify they match the obs units
                for v, (_, uvar) in VALUE_VARS.items():
                    if uvar and uvar in ds.variables:
                        data[uvar] = ds.variables[uvar][start:stop]

            df = pd.DataFrame(data)
            if group_fill is not None:
                df = df[df[GROUP_VAR] != group_fill]

            if has_units:
                for code, u in df.groupby(GROUP_VAR)[UNITS_VAR].unique().items():
                    units_seen.setdefault(int(code), set()).update(int(x) for x in u)
                for v, (_, uvar) in VALUE_VARS.items():
                    if uvar and uvar in df.columns:
                        bad = df[df[v].notna() & (df[uvar] != df[UNITS_VAR])]
                        unc_mismatch.update(int(c) for c in bad[GROUP_VAR].unique())

            grouped = df.groupby(GROUP_VAR)
            agg = grouped[list(VALUE_VARS)].agg(["min", "max", "count"])
            agg.columns = [f"{v}_{s}" for v, s in agg.columns]
            agg["n_records"] = grouped.size()
            partial.append(agg)

    if not partial:
        return pd.DataFrame()

    # Combine the chunks
    by_code = pd.concat(partial).groupby(level=0)
    result = pd.DataFrame(index=by_code.size().index)
    result["n_records"] = by_code["n_records"].sum()
    for v, (label, _) in VALUE_VARS.items():
        result[f"{label}_n_valid"] = by_code[f"{v}_count"].sum()
        result[f"{label}_min"] = by_code[f"{v}_min"].min()
        result[f"{label}_max"] = by_code[f"{v}_max"].max()

    result.index.name = "observed_variable_code"
    result = result.reset_index()
    result["units_code"] = result["observed_variable_code"].map(
        lambda c: ";".join(str(u) for u in sorted(units_seen.get(int(c), [])))
    )
    result["uncertainty_units_match"] = result["observed_variable_code"].map(
        lambda c: "NO" if int(c) in unc_mismatch else "yes"
    )
    result.insert(0, "file", path.name)
    return result.sort_values("observed_variable_code").reset_index(drop=True)


def decorate(summary: pd.DataFrame, units_lookup: dict, variable_lookup: dict) -> pd.DataFrame:
    """Add human-readable names and put the columns in a friendly order."""
    summary = summary.copy()
    summary["observed_variable_name"] = summary["observed_variable_code"].map(
        lambda c: variable_lookup.get(int(c), "")
    )

    def unit_field(codes: str, idx: int) -> str:
        parts = [units_lookup[int(c)][idx] for c in codes.split(";") if c and int(c) in units_lookup]
        return ";".join(parts)

    summary["units_name"] = summary["units_code"].map(lambda s: unit_field(s, 0))
    summary["units_abbreviation"] = summary["units_code"].map(lambda s: unit_field(s, 1))

    ordered = [
        "file", "observed_variable_code", "observed_variable_name",
        "units_code", "units_name", "units_abbreviation", "n_records",
    ]
    for label, _ in VALUE_VARS.values():
        ordered += [f"{label}_min", f"{label}_max", f"{label}_n_valid"]
    ordered.append("uncertainty_units_match")
    return summary[ordered]


def _worker(path_str: str, chunk_size: int):
    """Process-pool entry point. Never raises: errors are returned to the parent."""
    t0 = time.time()
    try:
        df = analyse_file(Path(path_str), chunk_size)
        return path_str, df, None, time.time() - t0
    except Exception as exc:
        return path_str, None, str(exc), time.time() - t0


def run_all(files, chunk_size: int, workers: int):
    """
    Analyse all files, serially (workers == 1) or with a pool of worker
    processes. Returns (results, failed) with results in the same order as
    `files`. Processes (not threads) are used because the HDF5 library is not
    thread-safe.
    """
    done, failed = {}, []
    n = len(files)
    counter = 0

    def handle(path_str, df, err, elapsed):
        nonlocal counter
        counter += 1
        name = Path(path_str).name
        if err is not None:
            log.error("[%d/%d] %s FAILED: %s", counter, n, name, err)
            failed.append((name, err))
            return
        log.info("[%d/%d] %s done in %.1f s (%d observed variables)",
                 counter, n, name, elapsed, len(df))
        if (df["uncertainty_units_match"] == "NO").any():
            log.warning("  %s: uncertainty units differ from observation units for some "
                        "variables (see column 'uncertainty_units_match')", name)
        done[path_str] = df

    if workers <= 1:
        for f in files:
            log.info("Processing %s (%.1f MB)", f.name, f.stat().st_size / 1e6)
            handle(*_worker(str(f), chunk_size))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_worker, str(f), chunk_size) for f in files]
            for fut in as_completed(futures):
                handle(*fut.result())

    results = [done[str(f)] for f in files if str(f) in done]
    return results, failed


def find_files(directory: Path, pattern: str, recursive: bool):
    globber = directory.rglob if recursive else directory.glob
    return sorted(p for p in globber(pattern) if p.is_file())


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Min/max of observation and uncertainty values per "
        "observed_variable, for all NetCDF files in a directory."
    )
    parser.add_argument(
        "directory", type=Path, nargs="?", default=Path(INPUT_DIR),
        help=f"Directory with the NetCDF files (default: {INPUT_DIR})",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path(OUTPUT_CSV),
        help=f"Output CSV path (default: {OUTPUT_CSV})",
    )
    parser.add_argument("-p", "--pattern", default="*.nc", help='File name pattern (default: "*.nc")')
    parser.add_argument("-r", "--recursive", action="store_true", help="Search sub-directories too")
    parser.add_argument(
        "--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
        help=f"Rows read per chunk (default: {DEFAULT_CHUNK_SIZE:,}). Lower it if memory is tight.",
    )
    parser.add_argument(
        "-w", "--workers", type=int, default=N_WORKERS,
        help=f"Number of parallel worker processes (default: {N_WORKERS}; 1 = serial). "
        f"This machine has {os.cpu_count()} CPU cores. Each worker holds one chunk in "
        "memory, so lower --chunk-size if you raise this a lot.",
    )
    parser.add_argument("--units-table", default=UNITS_TABLE_URL,
                        help="URL or local path of the CDM units.csv table")
    parser.add_argument("--variable-table", default=VARIABLE_TABLE_URL,
                        help="URL or local path of the CDM observed_variable.csv table")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.directory.is_dir():
        log.error("Not a directory: %s", args.directory)
        return 1

    files = find_files(args.directory, args.pattern, args.recursive)
    if not files:
        log.error("No files matching '%s' found in %s", args.pattern, args.directory)
        return 1
    log.info("Found %d file(s) to analyse", len(files))

    units_lookup, variable_lookup = load_lookups(args.units_table, args.variable_table)

    workers = max(1, min(args.workers, len(files)))
    log.info("Using %d worker process(es)", workers)
    t_start = time.time()
    results, failed = run_all(files, args.chunk_size, workers)
    log.info("Analysis finished in %.1f s", time.time() - t_start)

    if results:
        summary = decorate(pd.concat(results, ignore_index=True), units_lookup, variable_lookup)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        # 'utf-8-sig' lets Excel open the file directly with correct encoding
        summary.to_csv(args.output, index=False, encoding="utf-8-sig")
        log.info("Summary written to %s (%d rows)", args.output, len(summary))
    else:
        log.error("No file could be analysed successfully")

    if failed:
        log.warning("%d file(s) failed:", len(failed))
        for name, msg in failed:
            log.warning("  %s -> %s", name, msg)

    return 0 if results and not failed else 2


if __name__ == "__main__":
    sys.exit(main())