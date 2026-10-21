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

For the observation it also writes the plausible range of the variable
(observation_value_range_lo / _hi), so that the report script can compare it
with the file minimum / maximum. The ranges are NOT defined here: they
are read from PLAUSIBILITY_RULES[...]["check_range"] in qc_pipeline.py and
converted to the units of the CDM files (see CDM_CODE_TO_RULE).

Output: two CSV files with the same columns (the total one without "file"):
    - OUTPUT_CSV       : one row per (file, observed_variable), i.e. per month
    - OUTPUT_CSV_TOTAL : one row per observed_variable, over all the files

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
import importlib.util
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
INPUT_DIR = "/Data/GRUAN_TEST/output"         # directory containing the NetCDF files
# INPUT_DIR = "/storage/baron-data/GRUAN_CDS"         # directory containing the NetCDF files
OUTPUT_CSV = "/Data/GRUAN_TEST/output/gruan_check_nc_CDM.csv"  # output CSV: full path + file name
# OUTPUT_CSV = "/storage/baron-data/GRUAN_CDS/gruan_check_nc_CDM.csv"  # output CSV: full path + file name

# Total summary over all the files (same columns as OUTPUT_CSV)
OUTPUT_CSV_TOTAL = "/Data/GRUAN_TEST/output/gruan_check_nc_CDM_total.csv"
# OUTPUT_CSV_TOTAL = "/storage/baron-data/GRUAN_CDS/gruan_check_nc_CDM_total.csv"

# qc_pipeline.py, from which the plausibility ranges (check_range) are read.
# Relative to the directory of this script (two levels up).
QC_PIPELINE_PATH = str(Path(__file__).resolve().parent / "../../qc_pipeline.py")

N_WORKERS = 23                             # parallel worker processes (1 = serial)

# --------------------------------------------------------------------------- #
# Configuration (normally no need to change anything below this point)
# --------------------------------------------------------------------------- #
GROUP_VAR = "observed_variable"
UNITS_VAR = "units"  # numeric code of the units of observation_value

# NetCDF variable -> label used in the CSV
VALUE_VARS = {
    "observation_value": "observation_value",
    "uncertainty_value1": "uncertainty_random",
    "uncertainty_value2": "uncertainty_systematic",
    "uncertainty_value5": "uncertainty_total",
}

# CDM observed_variable code -> (key in PLAUSIBILITY_RULES of qc_pipeline.py,
#                                factor: units of the pipeline -> units of the CDM files)
# The ranges in qc_pipeline.py are expressed in the units used inside that
# pipeline, which for some variables differ from the units written to the CDM
# NetCDF files, hence the factor:
#   relative humidity : fraction (0-1) -> per cent (x 100)
#   pressure          : hPa            -> Pa       (x 100)
#   water vapour m.r. : ppmv           -> mol/mol  (x 1e-6)
# A code whose rule has check_range = None, or that has no rule at all, is NOT
# range-checked (its counters and bounds are left empty in the CSV).
CDM_CODE_TO_RULE = {
    126: ("temperature", 1.0),            # air temperature                  [K]
    138: ("relative_humidity", 100.0),    # relative humidity                [%]
    106: ("wind_direction", 1.0),         # wind from direction              [deg]
    107: ("wind_speed", 1.0),             # wind speed                       [m s-1]
    104: ("u", 1.0),                      # eastward wind speed              [m s-1]
    105: ("v", 1.0),                      # northward wind speed             [m s-1]
    123: ("wvmr", 1e-6),                  # water vapour mixing ratio        [mol mol-1]
    122: ("vertical_speed", 1.0),         # vertical speed of radiosonde     [m s-1]
    117: ("geopot", 1.0),                 # geopotential height              [m]
    116: ("frost_point", 1.0),            # frost point temperature          [K]
    124: ("rh_resolution", 1.0),          # RH effective vertical resolution [s]
    73:  ("shortwave_radiation", 1.0),    # shortwave radiation              [W m-2]
    125: ("height", 1.0),                 # altitude                         [m]
    142: ("pressure", 100.0),             # pressure                         [Pa]
    # 143 (time since launch) has no counterpart in PLAUSIBILITY_RULES
}


def load_ranges_from_qc_pipeline(path: str):
    """
    Import qc_pipeline.py and return two dicts {CDM code: (lo, hi)}, converted
    to the units of the CDM files:
        obs_ranges -> PLAUSIBILITY_RULES[rule]["check_range"]  (observation)
        unc_ranges -> PLAUSIBILITY_RULES[rule]["unc_range"]    (uncertainties)
    The uncertainties of a variable have the same units as its observation, so
    the same conversion factor applies. Codes whose range is None (or that have
    no rule) are left out (= not checked).
    """
    if not Path(path).is_file():
        raise FileNotFoundError(f"qc_pipeline.py not found: {path} (set QC_PIPELINE_PATH)")
    spec = importlib.util.spec_from_file_location("qc_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # safe: the pipeline main is under __main__
    rules = module.PLAUSIBILITY_RULES

    obs_ranges, unc_ranges = {}, {}
    for code, (rule, factor) in CDM_CODE_TO_RULE.items():
        if rule not in rules:
            raise KeyError(f"'{rule}' (CDM code {code}) not found in PLAUSIBILITY_RULES")
        for key, target in (("check_range", obs_ranges), ("unc_range", unc_ranges)):
            rng = rules[rule].get(key)
            if rng is None:
                continue
            lo, hi = rng
            # round() removes floating-point noise (e.g. 50000 * 1e-6)
            target[code] = (round(lo * factor, 12), round(hi * factor, 12))
    return obs_ranges, unc_ranges


# Plausible range of observation_value for each CDM observed_variable code, as
# a closed interval (lower, upper) in the units written to the NetCDF files.
# The bounds are written to the CSV (observation_value_range_lo / _hi).
# Codes not listed are not range-checked. The same is done for the plausible
# range of the uncertainties (unc_range), written as uncertainty_value_range_lo
# / _hi.
PLAUSIBLE_RANGES_BY_CODE, PLAUSIBLE_UNC_RANGES_BY_CODE = load_ranges_from_qc_pipeline(
    QC_PIPELINE_PATH)
RANGE_LO = {c: lo for c, (lo, hi) in PLAUSIBLE_RANGES_BY_CODE.items()}
RANGE_HI = {c: hi for c, (lo, hi) in PLAUSIBLE_RANGES_BY_CODE.items()}
UNC_RANGE_LO = {c: lo for c, (lo, hi) in PLAUSIBLE_UNC_RANGES_BY_CODE.items()}
UNC_RANGE_HI = {c: hi for c, (lo, hi) in PLAUSIBLE_UNC_RANGES_BY_CODE.items()}

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
    raw codes, n_records, n_records_missing (rows where observation_value is
    NaN / NULL) and, for every variable in VALUE_VARS: n_valid, min, max.
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

        for start in range(0, n_total, chunk_size):
            stop = min(start + chunk_size, n_total)
            data = {GROUP_VAR: group_var[start:stop]}
            for v in VALUE_VARS:
                arr = np.asarray(ds.variables[v][start:stop], dtype="float64")
                arr[~np.isfinite(arr)] = np.nan  # drop NaN and +/-inf
                data[v] = arr
            if has_units:
                data[UNITS_VAR] = ds.variables[UNITS_VAR][start:stop]

            df = pd.DataFrame(data)
            if group_fill is not None:
                df = df[df[GROUP_VAR] != group_fill]

            if has_units:
                for code, u in df.groupby(GROUP_VAR)[UNITS_VAR].unique().items():
                    units_seen.setdefault(int(code), set()).update(int(x) for x in u)

            df["_missing"] = df["observation_value"].isna().astype("int64")  # NaN / NULL / inf

            grouped = df.groupby(GROUP_VAR)
            agg = grouped[list(VALUE_VARS)].agg(["min", "max", "count"])
            agg.columns = [f"{v}_{s}" for v, s in agg.columns]
            agg["n_records"] = grouped.size()
            agg["n_missing"] = grouped["_missing"].sum()
            partial.append(agg)

    if not partial:
        return pd.DataFrame()

    # Combine the chunks
    by_code = pd.concat(partial).groupby(level=0)
    result = pd.DataFrame(index=by_code.size().index)
    result["n_records"] = by_code["n_records"].sum()
    result["n_records_missing"] = by_code["n_missing"].sum()
    for v, label in VALUE_VARS.items():
        result[f"{label}_n_valid"] = by_code[f"{v}_count"].sum()
        result[f"{label}_min"] = by_code[f"{v}_min"].min()
        result[f"{label}_max"] = by_code[f"{v}_max"].max()

    result.index.name = "observed_variable_code"
    result = result.reset_index()
    # Bounds used (NaN for codes that are not range-checked)
    codes = result["observed_variable_code"].astype(int)
    result["observation_value_range_lo"] = codes.map(RANGE_LO)
    result["observation_value_range_hi"] = codes.map(RANGE_HI)
    result["uncertainty_value_range_lo"] = codes.map(UNC_RANGE_LO)
    result["uncertainty_value_range_hi"] = codes.map(UNC_RANGE_HI)
    result["units_code"] = result["observed_variable_code"].map(
        lambda c: ";".join(str(u) for u in sorted(units_seen.get(int(c), [])))
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

    # The "file" column exists only in the per-file (monthly) table
    ordered = ["file"] if "file" in summary.columns else []
    ordered += [
        "observed_variable_code", "observed_variable_name",
        "units_code", "units_name", "units_abbreviation",
        "n_records", "observation_value_n_valid", "n_records_missing",
        "observation_value_min", "observation_value_max",
        "observation_value_range_lo", "observation_value_range_hi",
        "uncertainty_value_range_lo", "uncertainty_value_range_hi",
    ]
    for label in list(VALUE_VARS.values())[1:]:
        ordered += [f"{label}_min", f"{label}_max", f"{label}_n_valid"]
    return summary[ordered]


def summarise_total(monthly: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate the per-file rows (before decorate()) into one row per
    observed_variable over ALL the files: counts are summed, min / max are the
    overall min / max. Same columns as the monthly table, except "file".
    """
    g = monthly.groupby("observed_variable_code")
    total = pd.DataFrame(index=g.size().index)
    total["n_records"] = g["n_records"].sum()
    total["n_records_missing"] = g["n_records_missing"].sum()
    for label in VALUE_VARS.values():
        total[f"{label}_min"] = g[f"{label}_min"].min()
        total[f"{label}_max"] = g[f"{label}_max"].max()
        total[f"{label}_n_valid"] = g[f"{label}_n_valid"].sum()
    total["observation_value_range_lo"] = g["observation_value_range_lo"].first()
    total["observation_value_range_hi"] = g["observation_value_range_hi"].first()
    total["uncertainty_value_range_lo"] = g["uncertainty_value_range_lo"].first()
    total["uncertainty_value_range_hi"] = g["uncertainty_value_range_hi"].first()
    # Union of the units codes found in the different files (normally just one)
    total["units_code"] = g["units_code"].agg(
        lambda s: ";".join(sorted({u for x in s for u in str(x).split(";") if u},
                                  key=int))
    )
    total = total.reset_index()
    return total.sort_values("observed_variable_code").reset_index(drop=True)


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
    parser.add_argument(
        "--output-total", type=Path, default=Path(OUTPUT_CSV_TOTAL),
        help=f"Output CSV of the summary over all files (default: {OUTPUT_CSV_TOTAL})",
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
        monthly = pd.concat(results, ignore_index=True)
        summary = decorate(monthly, units_lookup, variable_lookup)
        total = decorate(summarise_total(monthly), units_lookup, variable_lookup)
        # 'utf-8-sig' lets Excel open the files directly with correct encoding
        args.output.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(args.output, index=False, encoding="utf-8-sig")
        log.info("Summary written to %s (%d rows)", args.output, len(summary))
        args.output_total.parent.mkdir(parents=True, exist_ok=True)
        total.to_csv(args.output_total, index=False, encoding="utf-8-sig")
        log.info("Total summary written to %s (%d rows)", args.output_total, len(total))
    else:
        log.error("No file could be analysed successfully")

    if failed:
        log.warning("%d file(s) failed:", len(failed))
        for name, msg in failed:
            log.warning("  %s -> %s", name, msg)

    return 0 if results and not failed else 2


if __name__ == "__main__":
    sys.exit(main())