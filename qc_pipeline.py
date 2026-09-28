"""
GRUAN Radiosonde Quality Control Script
Performs quality-control diagnostics on a single GRUAN NetCDF profile:
CBH, CTH, minimum pressure flags, completeness, and plausibility checks.
Produces a structured log file and an accompanying README.
"""
import glob
import logging
import os
import re
import zipfile
from datetime import datetime

import netCDF4 as nc4
import numpy as np
import pandas as pd
import xarray as xr

# ==========================================
# THRESHOLD CONFIGURATION
# ==========================================
COMPLETENESS_THR_HIGH = 0.10
COMPLETENESS_THR_GOOD = 0.20
CTH_TEMPERATURE_THR_K = 268.15
# ==========================================
# NETCDF PRODUCT-SPECIFIC NAMES AND UNITS
# ==========================================
# Global attributes that may contain the tropopause height, in order of
# preference (depending on the product).
TROPOPAUSE_ATTRIBUTE_CANDIDATES = (
    "g.Measurement.TropopauseGeopotHeight",
    "g.Ascent.TropopauseHeight",
)
# Recognised values (lower case) of the `units` attribute for WVMR. The
# pipeline works in ppmv; a dimensionless mixing ratio (mol/mol) is
# multiplied by 1e6.
WVMR_PPMV_UNITS = {"ppmv"}
WVMR_FRACTION_UNITS = {"1", "fraction"}
# ==========================================
# PLAUSIBILITY THRESHOLDS
# (physical ranges and maximum accepted
# uncertainties used by check_plausibility
# for RH and pressure)
# ==========================================
# Relative humidity is stored in the NetCDF files either in percent (0-100)
# or as a fraction (0-1), depending on the product. It is always converted to
# a fraction (0.0-1.0) right after reading, according to the `units`
# attribute (see load_netcdf_profile), consistently with the rest of the
# pipeline (see e.g. check_cloud_base, which uses rh_threshold=0.99). The
# thresholds below are therefore expressed as fractions (0% / 100% / 15% RH).
# Physical validity range and maximum accepted total uncertainty for RH,
# used by check_plausibility().
# Values of the NetCDF `units` attribute recognised for RH (lower case).
RH_PERCENT_UNITS = {"percent", "%"}
RH_FRACTION_UNITS = {"1", "fraction"}
RH_MIN_FRACTION = 0.0  # 0 % RH
RH_MAX_FRACTION = 1.0  # 100 % RH
RH_UNCERTAINTY_MAX_FRACTION = 0.15  # 15 % RH

# Pressure is expressed in hPa, as elsewhere in the pipeline.
# Physical validity range and maximum accepted total uncertainty for
# pressure, used by check_plausibility().
PRESSURE_MIN_HPA = 1.0
PRESSURE_MAX_HPA = 1080.0
PRESSURE_UNCERTAINTY_MAX_HPA = 3.0

# Plausibility checks are evaluated only on levels up to this altitude (m).
# Use None to evaluate every level of the profile.
PLAUSIBILITY_MAX_ALTITUDE_M = 40000.0

# Physical validity range (lower, upper) of each checked variable.
PLAUSIBILITY_LIMITS = {
    "temperature": (178.15, 323.15),
    "relative_humidity": (RH_MIN_FRACTION, RH_MAX_FRACTION),
    "wind_speed": (0.0, 180.0),
    "wind_direction": (0.0, 360.0),
    "pressure": (PRESSURE_MIN_HPA, PRESSURE_MAX_HPA),
    "wvmr": (0.0, 50000.0),
}

# variable -> (total uncertainty column, maximum accepted uncertainty).
# To extend the uncertainty-based check to other variables, add an entry.
PLAUSIBILITY_UNCERTAINTY_CHECKS = {
    "relative_humidity": (
        "relative_humidity_uc_tot", RH_UNCERTAINTY_MAX_FRACTION),
    "pressure": ("pressure_uc_tot", PRESSURE_UNCERTAINTY_MAX_HPA),
}
# ==========================================
# INPUT/OUTPUT CONFIGURATION
# ==========================================
# INPUT_DIRECTORY = "/Data/GRUAN/backup/gfa/archive/prodata/RS41-GDP_001/POT/POT-RS-02/2025"
INPUT_DIRECTORY = "/Data/GRUAN_TEST/ema"
# INPUT_DIRECTORY = "/Users/emanuele/Data/GRUAN/EUMETRAVES"
JAR_FILE_PATTERN = "*.jar"  # Glob pattern used to select the compressed archives within INPUT_DIRECTORY
OUTPUT_LOG_PATH = "qc_log.csv"  # Path of the resulting QC log CSV file
logger = logging.getLogger(__name__)


def find_consecutive_layers(
        rh_array,
        height_array,
        pressure_array,
        rh_threshold,
        min_layers=3,
        direction="upward",
        temp_array=None,
        temp_threshold=None,
):
    n = len(rh_array)
    condition = rh_array >= rh_threshold
    if temp_array is not None and temp_threshold is not None:
        condition = condition & (temp_array <= temp_threshold)
    if direction == "upward":
        indices = range(n)
    else:
        indices = range(n - 1, -1, -1)
    consecutive_count = 0
    for i in indices:
        if condition[i]:
            consecutive_count += 1
            if consecutive_count >= min_layers:
                target_idx = i - min_layers + 1 if direction == "upward" else i
                return {
                    "index": target_idx,
                    "height": height_array[target_idx],
                    "pressure": pressure_array[target_idx],
                    "flag": True,
                }
        else:
            consecutive_count = 0
    return {"index": np.nan, "height": np.nan, "pressure": np.nan,
            "flag": False}


def check_cloud_base(rh, height, pressure):
    """CBH: first level with RH >= 0.99 for 3 consecutive levels."""
    return find_consecutive_layers(
        rh, height, pressure, rh_threshold=0.99, min_layers=3,
        direction="upward"
    )


def rh_over_ice(temp_k, rh_w):
    """
    Converts RH with respect to water into RH with respect to ice
    for temperatures below 273.15 K.
    RH is expressed as a fraction (0-1).
    """
    temp_c = temp_k - 273.15
    ew = 6.112 * np.exp((17.67 * temp_c) / (temp_c + 243.5))
    ei = 6.112 * np.exp((22.46 * temp_c) / (temp_c + 272.62))
    return np.where(temp_k < 273.15, rh_w * (ew / ei), rh_w)


def zhang_cold_cloud_layers(rh, height, temperature,
                            temp_max_k=CTH_TEMPERATURE_THR_K):
    """
    Zhang cold-cloud detection.
    Height is in metres MSL.
    The algorithm is applied only above 2 km AGL
    and only to cold clouds with T <= -5 degC.
    Altitude bands:
      2-6 km
      6-12 km
      >12 km
    """
    valid = ~np.isnan(height) & ~np.isnan(temperature) & ~np.isnan(rh)
    if np.sum(valid) < 3:
        return []
    alt_msl = height[valid]
    temp_k = temperature[valid]
    rh_w = rh[valid]
    # AGL in metres
    alt_agl = alt_msl - alt_msl[0]
    # km
    alt_km = alt_agl / 1000.0
    # RH with respect to ice
    rh_calc = rh_over_ice(temp_k, rh_w)
    # --------------------------------------------------
    # Zhang thresholds
    # --------------------------------------------------
    #
    # 0-2 km is deliberately excluded.
    #
    # 2-6 km:
    #   min RH = 0.90
    #   max RH = 0.93
    #   intermediate RH = 0.80
    #
    # 6-12 km:
    #   min RH = 0.88
    #   max RH = 0.90
    #   intermediate RH = 0.75
    #
    # >12 km:
    #   min RH = 0.80
    #   max RH = 0.70
    #   intermediate RH = 0.70
    #
    # --------------------------------------------------
    min_rh = np.select(
        [(alt_km >= 2) & (alt_km < 6), (alt_km >= 6) & (alt_km <= 12),
         alt_km > 12],
        [0.90, 0.88, 0.80],
        default=np.nan,
    )
    max_rh = np.select(
        [(alt_km >= 2) & (alt_km < 6), (alt_km >= 6) & (alt_km <= 12),
         alt_km > 12],
        [0.93, 0.90, 0.70],
        default=np.nan,
    )
    inter_rh = np.select(
        [(alt_km >= 2) & (alt_km < 6), (alt_km >= 6) & (alt_km <= 12),
         alt_km > 12],
        [0.80, 0.75, 0.70],
        default=np.nan,
    )
    # --------------------------------------------------
    # Only cold clouds below -5 C
    # --------------------------------------------------
    is_moist = (
            (alt_km >= 2.0)
            & ~np.isnan(min_rh)
            & (rh_calc >= min_rh)
            & (temp_k <= temp_max_k)
    )
    diffs = np.diff(is_moist.astype(int))
    starts = np.where(diffs == 1)[0] + 1
    ends = np.where(diffs == -1)[0]
    if is_moist[0]:
        starts = np.r_[0, starts]
    if is_moist[-1]:
        ends = np.r_[ends, len(is_moist) - 1]
    candidate_layers = []
    for s, e in zip(starts, ends):
        mean_temp_k = np.mean(temp_k[s: e + 1])
        if mean_temp_k >= temp_max_k:
            continue
        base_agl = alt_agl[s]
        top_agl = alt_agl[e]
        sub_rh = rh_calc[s: e + 1]
        max_rh_layer = np.max(sub_rh)
        req_max_rh = np.nanmax(max_rh[s: e + 1])
        if max_rh_layer >= req_max_rh:
            candidate_layers.append(
                {
                    "start_idx": s,
                    "end_idx": e,
                    "base_agl": base_agl,
                    "top_agl": top_agl,
                    "base_msl": alt_msl[s],
                    "top_msl": alt_msl[e],
                    "thickness": top_agl - base_agl,
                    "mean_temp_c": mean_temp_k - 273.15,
                    "max_rh_i": max_rh_layer,
                }
            )
    if not candidate_layers:
        return []
    # --------------------------------------------------
    # Merge adjacent/inter-saturated layers
    # --------------------------------------------------
    merged_layers = []
    curr = candidate_layers[0]
    for nxt in candidate_layers[1:]:
        gap_dist = nxt["base_agl"] - curr["top_agl"]
        gap_rh = rh_calc[curr["end_idx"]: nxt["start_idx"] + 1]
        gap_inter_rh = np.nanmean(
            inter_rh[curr["end_idx"]: nxt["start_idx"] + 1])
        min_rh_gap = np.nanmin(gap_rh) if len(gap_rh) > 0 else 0
        if gap_dist < 300 or min_rh_gap >= gap_inter_rh:
            curr["end_idx"] = nxt["end_idx"]
            curr["top_agl"] = nxt["top_agl"]
            curr["top_msl"] = nxt["top_msl"]
            curr["thickness"] = curr["top_agl"] - curr["base_agl"]
            curr["max_rh_i"] = max(curr["max_rh_i"], nxt["max_rh_i"])
            curr["mean_temp_c"] = (
                    np.mean(temp_k[curr["start_idx"]: curr[
                                                          "end_idx"] + 1]) - 273.15
            )
        else:
            merged_layers.append(curr)
            curr = nxt
    merged_layers.append(curr)
    # Minimum thickness 100 m
    return [layer for layer in merged_layers if layer["thickness"] >= 100]


def check_cloud_top(rh, height, pressure, temperature):
    layers = zhang_cold_cloud_layers(
        rh, height, temperature, temp_max_k=CTH_TEMPERATURE_THR_K
    )
    if not layers:
        return {"index": np.nan, "height": np.nan, "pressure": np.nan,
                "flag": False}
    top_layer = max(layers, key=lambda l: l["top_msl"])
    valid = ~np.isnan(height) & ~np.isnan(temperature) & ~np.isnan(rh)
    valid_pressure = pressure[valid]
    top_pressure = (
        valid_pressure[top_layer["end_idx"]]
        if top_layer["end_idx"] < len(valid_pressure)
        else np.nan
    )
    return {
        "index": top_layer["end_idx"],
        "height": top_layer["top_msl"],
        "pressure": top_pressure,
        "flag": True,
    }


def check_pressure_flags(pressure):
    pmin = np.nanmin(pressure) if not np.all(np.isnan(pressure)) else np.nan
    if np.isnan(pmin):
        return pmin, False, False
    flag_p10 = bool(pmin <= 10.0)
    flag_p5 = bool(pmin <= 5.0)
    return pmin, flag_p10, flag_p5


def evaluate_level_plausibility(profile_data,
                                max_altitude_m=PLAUSIBILITY_MAX_ALTITUDE_M):
    """
    Level-by-level plausibility evaluation of a SINGLE profile.

    `profile_data` is a DataFrame holding the levels of one profile, with the
    columns `height`, the variables listed in PLAUSIBILITY_LIMITS and the
    uncertainty columns listed in PLAUSIBILITY_UNCERTAINTY_CHECKS. Only levels
    up to `max_altitude_m` are evaluated (None = all levels); levels with an
    unknown height are never evaluated.

    Returns a tuple (evaluated_levels, rejected):
      - evaluated_levels: boolean array (one item per level of the profile),
        True for the levels that were evaluated;
      - rejected: dict {variable: boolean array | None}. The array has one
        item per level of the profile and is True where the level is
        rejected (never True for levels that were not evaluated). None means
        that the variable could not be evaluated (missing column, error, or
        no valid data at all).

    Variables WITHOUT an entry in PLAUSIBILITY_UNCERTAINTY_CHECKS: a level is
    rejected as soon as its value falls outside the physical range [lo, hi].

    Variables WITH an entry in PLAUSIBILITY_UNCERTAINTY_CHECKS (currently RH
    and pressure): values outside the physical range are evaluated taking
    their total uncertainty into account, level by level, with these steps:

      Step 1 - Range check:
          values inside [lo, hi] are plausible; no further check is needed.

      Step 2 - Consistency within uncertainty (only for out-of-range
      values):
          the uncertainty interval is [value - uncertainty,
          value + uncertainty]. If the whole interval lies outside the
          valid range, i.e.
              (value + uncertainty) <= lo   OR   (value - uncertainty) >= hi
          the value is inconsistent.

      Step 3 - Inconsistent values are rejected. Values whose interval
          overlaps the valid range are consistent within their uncertainty
          and move on to step 4.

      Step 4 - Uncertainty magnitude check: a consistent value is accepted
          only if its uncertainty is smaller than the maximum accepted
          uncertainty for that variable; otherwise it is rejected.

    An out-of-range value with a missing (NaN) uncertainty cannot be
    evaluated and is therefore rejected.
    """
    n_levels = len(profile_data)
    height = profile_data["height"].to_numpy()
    if max_altitude_m is None:
        evaluated_levels = np.ones(n_levels, dtype=bool)
    else:
        # NaN heights compare as False, i.e. they are not evaluated.
        evaluated_levels = height <= max_altitude_m
    if not evaluated_levels.any():
        return evaluated_levels, {
            var: np.zeros(n_levels, dtype=bool)
            for var in PLAUSIBILITY_LIMITS
        }
    rejected = {}
    for var, (lo, hi) in PLAUSIBILITY_LIMITS.items():
        try:
            var_data = profile_data[var].to_numpy()[evaluated_levels]
            if np.all(np.isnan(var_data)):
                rejected[var] = None
                continue
            # Step 1: levels outside the physical range (NaN never counts
            # as out of range: comparisons with NaN are False).
            level_rejected = (var_data < lo) | (var_data > hi)
            if var in PLAUSIBILITY_UNCERTAINTY_CHECKS:
                uc_column, uc_max = PLAUSIBILITY_UNCERTAINTY_CHECKS[var]
                uncertainty = profile_data[uc_column].to_numpy()[
                    evaluated_levels]
                # Steps 2-3: whole uncertainty interval outside the valid
                # range.
                inconsistent = level_rejected & (
                    (var_data + uncertainty <= lo)
                    | (var_data - uncertainty >= hi)
                )
                # Step 4: consistent values are accepted only if the
                # uncertainty is small enough (a NaN uncertainty fails this
                # comparison, so the value is rejected).
                consistent = level_rejected & ~inconsistent
                oversized_uncertainty = consistent & ~(uncertainty < uc_max)
                level_rejected = inconsistent | oversized_uncertainty
            full_length = np.zeros(n_levels, dtype=bool)
            full_length[evaluated_levels] = level_rejected
            rejected[var] = full_length
        except KeyError:
            logging.error(f"Missing column: '{var}'")
            rejected[var] = None
        except Exception as e:
            logging.error(f"Error checking '{var}': {e}")
            rejected[var] = None
    return evaluated_levels, rejected


def check_plausibility(profile_data, max_altitude_m=PLAUSIBILITY_MAX_ALTITUDE_M):
    """
    Checks whether each variable of the profile is plausible (True) or not
    (False), considering only levels up to `max_altitude_m`.

    Returns a dict {variable: True | False | np.nan}; np.nan means the
    variable has no valid data at all. A variable is plausible only if NO
    level is rejected (see evaluate_level_plausibility for the rules applied
    to each level).
    """
    evaluated_levels, rejected = evaluate_level_plausibility(
        profile_data, max_altitude_m)
    if not evaluated_levels.any():
        return {var: False for var in PLAUSIBILITY_LIMITS}
    return {
        var: (np.nan if level_mask is None else not np.any(level_mask))
        for var, level_mask in rejected.items()
    }


def flag_implausible_levels(data, profile_id_column="profile_id",
                            max_altitude_m=PLAUSIBILITY_MAX_ALTITUDE_M):
    """
    Level-by-level plausibility flags for a DataFrame that contains MANY
    profiles stacked together (e.g. a whole month of soundings).

    Each profile is identified by the value of `profile_id_column` and is
    evaluated on its own with evaluate_level_plausibility, exactly as
    process_gruan_profiles does for a single sounding. `data` needs the same
    columns as evaluate_level_plausibility (`height`, the checked variables
    and their uncertainties, in the units used by this pipeline: K, fraction,
    m s-1, degrees, hPa, ppmv).

    Returns a boolean DataFrame with the same index and row order as `data`
    and one column per checked variable (PLAUSIBILITY_LIMITS): True where the
    level is NOT plausible. Levels that could not be evaluated (unknown
    height, above `max_altitude_m`, variable without valid data in the
    profile) are False, i.e. never reported as implausible.
    """
    n_rows = len(data)
    implausible = {var: np.zeros(n_rows, dtype=bool)
                   for var in PLAUSIBILITY_LIMITS}
    # Positional row indices of each profile (works with any index type).
    rows_by_profile = data.groupby(profile_id_column, sort=False).indices
    for rows in rows_by_profile.values():
        _, rejected = evaluate_level_plausibility(
            data.iloc[rows], max_altitude_m)
        for var, level_mask in rejected.items():
            if level_mask is not None:
                implausible[var][rows] = level_mask
    return pd.DataFrame(implausible, index=data.index)


def check_variable_completeness(
        var_array,
        height,
        zmin=None,
        zmax=None,
        thr_high=COMPLETENESS_THR_HIGH,
        thr_good=COMPLETENESS_THR_GOOD,
):
    if zmin is None and zmax is None:
        n_total = len(var_array)
        if n_total == 0:
            return 0, 0, np.nan, "FAIL"
        n_missing = int(np.sum(np.isnan(var_array) | np.isnan(height)))
    else:
        layer_lo = zmin if zmin is not None else -np.inf
        layer_hi = zmax if zmax is not None else np.inf
        layer_mask = ~np.isnan(height) & (height >= layer_lo) & (
                    height <= layer_hi)
        n_total = int(np.sum(layer_mask))
        if n_total == 0:
            return 0, 0, np.nan, "FAIL"
        n_missing = int(np.sum(np.isnan(var_array[layer_mask])))
    completeness = n_missing / n_total
    if completeness < thr_high:
        flag_complete = "HIGH"
    elif completeness < thr_good:
        flag_complete = "GOOD"
    else:
        flag_complete = "FAIL"
    return (n_total, n_missing, completeness, flag_complete)


def as_binary_flag(flag_value):
    if flag_value is None:
        return np.nan
    try:
        if np.isnan(flag_value):
            return np.nan
    except TypeError:
        pass
    return int(bool(flag_value))


def parse_numeric_attribute(attr_value):
    if attr_value is None:
        return np.nan
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", str(attr_value))
    return float(match.group()) if match else np.nan


def generate_readme():
    readme_content = """# GRUAN Quality Control Pipeline
## Input
The pipeline processes a single GRUAN NetCDF radiosonde profile.
## CBH
Cloud Base Height is identified as the first level belonging to at least
three consecutive levels with RH >= 0.99.
The altitude variable `alt` is expressed in metres.
## CTH - Zhang cold cloud detection
Cold-cloud detection is applied only above 2 km AGL.
Altitude bands:
- 2-6 km
- 6-12 km
- >12 km
Only clouds with temperature <= -5 degC are considered.
Relative humidity is stored in the NetCDF files either in percent (0-100)
or as a fraction (0-1), depending on the product. According to the `units`
attribute of each variable it is converted, when the data is loaded, to a
fraction in the range 0-1 (the same applies to its uncertainties). Files
with unrecognised RH units are rejected with an error.
Relative humidity is converted from RH with respect to water to RH with
respect to ice for temperatures below 0 degC.
## Product-specific names and units
Variable names and units differ between GRUAN products (e.g. CF-1.7 and
CF-1.4 files): for each quantity the first available variable name is used
(e.g. `rh_uc` or `u_rh` for the RH uncertainty, `press_uc` or `u_press`
for the pressure uncertainty, `wvmr_vol` or `WVMR` for the water vapour
mixing ratio). The tropopause height is read from the first available
global attribute (`g.Measurement.TropopauseGeopotHeight` or
`g.Ascent.TropopauseHeight`). WVMR is converted to ppmv when it is stored
as a dimensionless ratio.
## Pressure
Pmin is the minimum pressure recorded in the profile.
## Completeness
Completeness is the fraction of missing data records.
HIGH: < 0.10
GOOD: 0.10 <= C < 0.20
FAIL: >= 0.20
## Plausibility (FLAG_PLAUSIBILITY_*)
A variable is plausible (1) if no level up to 40 km is rejected, not
plausible (0) otherwise.
For most variables a level is rejected when it falls outside the physical
range.
For RH (0-100%) and pressure (1-1080 hPa) the total uncertainty is also
taken into account, level by level:
Step 1: a value inside the physical range is accepted.
Step 2: an out-of-range value is inconsistent if its uncertainty interval
[value - uncertainty, value + uncertainty] does not overlap the valid range.
Step 3: inconsistent values are rejected; the others are consistent within
their uncertainty and move to step 4.
Step 4: a consistent value is accepted only if its uncertainty is below the
maximum accepted uncertainty (RH: 15%; pressure: 3 hPa), otherwise it is
rejected.
"""
    readme_path = os.path.join(os.path.dirname(OUTPUT_LOG_PATH), "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)
    print(f"[INFO] README.md written to: {readme_path}")

def extract_netcdf_bytes_from_jar(jar_path):
    """
    Opens a .jar archive (a standard zip container) and reads the single embedded
    NetCDF (.nc) file entirely into memory, without ever writing to disk.
    The .nc entry may be located at the archive root or inside a subfolder.

    Returns a tuple (nc_bytes, internal_nc_name).
    Raises FileNotFoundError if no .nc entry is found inside the archive.
    """
    with zipfile.ZipFile(jar_path, 'r') as zf:
        nc_entries = [info for info in zf.infolist()
                      if info.filename.lower().endswith('.nc')]

        if not nc_entries:
            raise FileNotFoundError(
                f"No .nc file found inside jar archive: {jar_path}")

        if len(nc_entries) > 1:
            logging.warning(
                f"Multiple .nc files found inside '{jar_path}'; "
                f"using the first one found: {nc_entries[0].filename}")

        nc_entry = nc_entries[0]
        nc_bytes = zf.read(nc_entry.filename)

    return nc_bytes, nc_entry.filename

def load_netcdf_profile(jar_file_path):
    print(f"[INFO] Opening jar archive: {os.path.basename(jar_file_path)}")

    try:
        nc_bytes, internal_nc_name = extract_netcdf_bytes_from_jar(jar_file_path)

        nc4_dataset = nc4.Dataset(internal_nc_name, memory=nc_bytes)

        ds = xr.open_dataset(xr.backends.NetCDF4DataStore(nc4_dataset))
        station = ds.attrs.get("g.Site.Key", "UNKNOWN")
        if station == "UNKNOWN":
            station = ds.attrs.get("g.General.SiteCode", "UNKNOWN")
        dt_str = ds.attrs.get("g.Measurement.StandardTime", "UNKNOWN")
        if dt_str == "UNKNOWN":
            dt_str = ds.attrs.get("g.Ascent.StandardTime", "UNKNOWN")
        try:
            dt = datetime.strptime(dt_str.split(".")[0],
                                   "%Y-%m-%dT%H:%M:%S").strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except Exception:
            dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # ------------------------------------------
        # Tropopause
        # ------------------------------------------
        # The global attribute name depends on the product (e.g. CF-1.7 vs
        # CF-1.4 files): the first one found is used.
        tropopause_attr = None
        for attr_name in TROPOPAUSE_ATTRIBUTE_CANDIDATES:
            if attr_name in ds.attrs:
                tropopause_attr = ds.attrs[attr_name]
                break
        tropopause_altitude = parse_numeric_attribute(tropopause_attr)
        # ------------------------------------------
        # Variable mapping
        # ------------------------------------------
        mapping = {
            "height": ["alt"],
            "height_uc_tot": ["alt_uc"],
            "pressure": ["press"],
            "pressure_uc_tot": ["press_uc", "u_press"],
            "temperature": ["temp"],
            "temperature_uc_sys": ["temp_uc_tcor"],
            "temperature_uc_tot": ["temp_uc"],
            "relative_humidity": ["rh"],
            "relative_humidity_uc_sys": ["rh_uc_tcor"],
            "relative_humidity_uc_tot": ["rh_uc", "u_rh"],
            "wind_speed": ["wspeed"],
            "wind_speed_uc_tot": ["wspeed_uc"],
            "wind_direction": ["wdir"],
            "wind_direction_uc_tot": ["wdir_uc"],
            "wvmr": ["wvmr_vol", "WVMR", "wvmr"],
            "wvmr_uc_sys": ["wvmr_vol_uc_tcor"],
            "wvmr_uc_tot": ["wvmr_vol_uc"],
        }
        extracted_data = {}
        extracted_units = {}
        for internal_key, nc_candidates in mapping.items():
            found_var = None
            for candidate in nc_candidates:
                if candidate in ds.variables:
                    found_var = candidate
                    break
            if found_var:
                extracted_data[internal_key] = ds[found_var].values.flatten()
                extracted_units[internal_key] = ds[found_var].attrs.get("units")
            else:
                extracted_data[internal_key] = None
        # ------------------------------------------
        # Reference length
        # ------------------------------------------
        ref_len = 0
        for k in ["pressure", "height", "temperature"]:
            if k in extracted_data and isinstance(extracted_data[k],
                                                  np.ndarray):
                ref_len = len(extracted_data[k])
                break
        if ref_len == 0:
            raise ValueError(
                "No core profile data " "(pressure/height) could be extracted."
            )
        # ------------------------------------------
        # Fill missing variables
        # ------------------------------------------
        for k, v in extracted_data.items():
            if v is None:
                extracted_data[k] = np.full(ref_len, np.nan)
        # ------------------------------------------
        # RH unit conversion
        # ------------------------------------------
        # Depending on the file (e.g. CF-1.7 vs CF-1.4 GRUAN products), RH
        # and its uncertainties are stored either in percent (0-100) or as
        # a fraction (0-1). The whole pipeline (cloud detection, RH over
        # ice, QC thresholds) works with fractions, so each variable is
        # converted according to the `units` attribute declared in the file.
        for rh_key in ("relative_humidity",
                       "relative_humidity_uc_sys",
                       "relative_humidity_uc_tot"):
            rh_units = extracted_units.get(rh_key)
            if rh_units is None:
                # Variable not present in the file (already filled with NaN).
                continue
            rh_units_norm = str(rh_units).strip().lower()
            if rh_units_norm in RH_PERCENT_UNITS:
                extracted_data[rh_key] = extracted_data[rh_key] / 100.0
            elif rh_units_norm not in RH_FRACTION_UNITS:
                raise ValueError(
                    f"Unrecognised units '{rh_units}' for '{rh_key}': "
                    f"cannot determine whether RH is a percentage or a "
                    f"fraction."
                )
        # ------------------------------------------
        # WVMR unit conversion
        # ------------------------------------------
        # Some products store the water vapour volume mixing ratio in ppmv
        # (e.g. `wvmr_vol`), others as a dimensionless ratio (mol/mol,
        # units '1', e.g. `WVMR`). The pipeline works in ppmv (see the
        # plausibility limits), so the values are converted according to
        # the `units` attribute declared in the file.
        for wv_key in ("wvmr", "wvmr_uc_sys", "wvmr_uc_tot"):
            wv_units = extracted_units.get(wv_key)
            if wv_units is None:
                # Variable not present in the file (already filled with NaN).
                continue
            wv_units_norm = str(wv_units).strip().lower()
            if wv_units_norm in WVMR_FRACTION_UNITS:
                extracted_data[wv_key] = extracted_data[wv_key] * 1.0e6
            elif wv_units_norm not in WVMR_PPMV_UNITS:
                raise ValueError(
                    f"Unrecognised units '{wv_units}' for '{wv_key}': "
                    f"cannot convert to ppmv."
                )
        df_profile = pd.DataFrame(extracted_data)
        df_profile["datetime"] = dt
        df_profile["station"] = station
        df_profile["source_file"] = os.path.basename(jar_file_path)
        df_profile["tropopause_altitude"] = tropopause_altitude
        ds.close()
        try:
            nc4_dataset.close()
        except Exception:
            pass
        return df_profile
    except Exception as e:
        print(f"[ERROR] Failed to process NetCDF " f"{jar_file_path}: {e}")
        return pd.DataFrame()


def process_gruan_profiles(data_source, output_log_path=OUTPUT_LOG_PATH):
    log_records = []
    group_keys = ["datetime", "station"]
    if "source_file" in data_source.columns:
        group_keys.append("source_file")
    grouped = data_source.groupby(group_keys)
    for group_key, profile in grouped:
        if "source_file" in group_keys:
            dt, station, source_file = group_key
        else:
            dt, station = group_key
            source_file = np.nan
        # ------------------------------------------
        # Sort profile from bottom to top
        # ------------------------------------------
        profile = profile.sort_values(by="height").reset_index(drop=True)
        p_arr = profile["pressure"].to_numpy()
        z_arr = profile["height"].to_numpy()
        rh_arr = profile["relative_humidity"].to_numpy()
        t_arr = profile["temperature"].to_numpy()
        ws_arr = profile["wind_speed"].to_numpy()
        wd_arr = profile["wind_direction"].to_numpy()
        wvmr_arr = profile["wvmr"].to_numpy()
        # ------------------------------------------
        # CBH
        # ------------------------------------------
        cbh_res = check_cloud_base(rh_arr, z_arr, p_arr)
        # ------------------------------------------
        # CTH / Zhang
        # ------------------------------------------
        cth_res = check_cloud_top(rh_arr, z_arr, p_arr, t_arr)
        # ------------------------------------------
        # Pressure
        # ------------------------------------------
        pmin, f_p10, f_p5 = check_pressure_flags(p_arr)
        # ------------------------------------------
        # Plausibility
        # ------------------------------------------
        plaus_flags = check_plausibility(profile)
        tropopause_altitude = profile["tropopause_altitude"].iloc[0]
        # ------------------------------------------
        # CBH / CTH T and RH
        # Vector index starts from the bottom:
        # 0 = lowest altitude level
        # ------------------------------------------
        cbh_index = int(cbh_res["index"]) if not np.isnan(
            cbh_res["index"]) else np.nan
        cth_index = int(cth_res["index"]) if not np.isnan(
            cth_res["index"]) else np.nan
        # ------------------------------------------
        # RH with respect to ice for the full profile
        # ------------------------------------------
        rh_ice_arr = rh_over_ice(t_arr, rh_arr)
        # ------------------------------------------
        # CBH values
        # ------------------------------------------
        if not np.isnan(cbh_index):
            cbh_temperature = t_arr[cbh_index]
            # Original RH from NetCDF
            cbh_rh = rh_arr[cbh_index]
            # Calculated RH with respect to ice
            cbh_rh_ice = rh_ice_arr[cbh_index]
        else:
            cbh_temperature = np.nan
            cbh_rh = np.nan
            cbh_rh_ice = np.nan
        # ------------------------------------------
        # CTH values
        # ------------------------------------------
        if not np.isnan(cth_index):
            cth_temperature = t_arr[cth_index]
            # Original RH from NetCDF
            cth_rh = rh_arr[cth_index]
            # Calculated RH with respect to ice
            cth_rh_ice = rh_ice_arr[cth_index]
        else:
            cth_temperature = np.nan
            cth_rh = np.nan
            cth_rh_ice = np.nan
        # ------------------------------------------
        # Completeness
        # ------------------------------------------
        completeness_vars = {
            "Press": p_arr,
            "Temp": t_arr,
            "RH": rh_arr,
            "Wspeed": ws_arr,
            "Wdir": wd_arr,
            "WVMR": wvmr_arr,
        }
        completeness_record = {}
        for label, var_arr in completeness_vars.items():
            # Whole profile
            (
                n_tot_full,
                n_miss_full,
                compl_full,
                f_compl_full,
            ) = check_variable_completeness(var_arr, z_arr)
            completeness_record[f"Ntotal_{label}"] = n_tot_full
            completeness_record[f"Completeness_{label}"] = (
                round(compl_full, 4) if not np.isnan(compl_full) else np.nan
            )
            completeness_record[f"FLAG_COMPLETENESS_{label}"] = f_compl_full
            # Troposphere
            if not np.isnan(tropopause_altitude):
                (
                    n_tot_tropo,
                    n_miss_tropo,
                    compl_tropo,
                    f_compl_tropo,
                ) = check_variable_completeness(
                    var_arr, z_arr, zmax=tropopause_altitude
                )
            else:
                (n_tot_tropo, n_miss_tropo, compl_tropo, f_compl_tropo) = (
                    0,
                    0,
                    np.nan,
                    "N/A",
                )
            completeness_record[f"Ntotal_{label}_troposphere"] = n_tot_tropo
            completeness_record[f"Completeness_{label}_troposphere"] = (
                round(compl_tropo, 4) if not np.isnan(compl_tropo) else np.nan
            )
            completeness_record[
                f"FLAG_COMPLETENESS_{label}_troposphere"
            ] = f_compl_tropo
            # Stratosphere
            if not np.isnan(tropopause_altitude):
                (
                    n_tot_strato,
                    n_miss_strato,
                    compl_strato,
                    f_compl_strato,
                ) = check_variable_completeness(
                    var_arr, z_arr, zmin=tropopause_altitude
                )
            else:
                (n_tot_strato, n_miss_strato, compl_strato, f_compl_strato) = (
                    0,
                    0,
                    np.nan,
                    "N/A",
                )
            completeness_record[f"Ntotal_{label}_stratosphere"] = n_tot_strato
            completeness_record[f"Completeness_{label}_stratosphere"] = (
                round(compl_strato, 4) if not np.isnan(
                    compl_strato) else np.nan
            )
            completeness_record[
                f"FLAG_COMPLETENESS_{label}_stratosphere"
            ] = f_compl_strato
        # ------------------------------------------
        # Final record
        # ------------------------------------------
        record = {
            "Date": dt,
            "Station": station,
            "File": source_file,
            "CBH (m)": cbh_res["height"],
            "CBH_Index": cbh_index,
            "CBH_Temperature (K)": cbh_temperature,
            "CBH_RH": cbh_rh,
            "CBH_RH_Ice": cbh_rh_ice,
            "CBH_Flag": as_binary_flag(cbh_res["flag"]),
            "CTH (m)": cth_res["height"],
            "CTH_Index": cth_index,
            "CTH_Temperature (K)": cth_temperature,
            "CTH_RH": cth_rh,
            "CTH_RH_Ice": cth_rh_ice,
            "CTH_Flag": as_binary_flag(cth_res["flag"]),
            "Pmin (hPa)": pmin,
            "FLAG_P10": as_binary_flag(f_p10),
            "FLAG_P5": as_binary_flag(f_p5),
            "FLAG_PLAUSIBILITY_TEMPERATURE": as_binary_flag(
                plaus_flags["temperature"]),
            "FLAG_PLAUSIBILITY_RH": as_binary_flag(
                plaus_flags["relative_humidity"]),
            "FLAG_PLAUSIBILITY_WIND_SPEED": as_binary_flag(
                plaus_flags["wind_speed"]),
            "FLAG_PLAUSIBILITY_WIND_DIRECTION": as_binary_flag(
                plaus_flags["wind_direction"]
            ),
            "FLAG_PLAUSIBILITY_PRESSURE": as_binary_flag(
                plaus_flags["pressure"]),
            "FLAG_PLAUSIBILITY_WVMR": as_binary_flag(plaus_flags["wvmr"]),
            "Tropopause_Altitude (m)": tropopause_altitude,
        }
        record.update(completeness_record)
        log_records.append(record)
    df_log = pd.DataFrame(log_records)
    df_log.to_csv(output_log_path, index=False, na_rep="NaN")
    print(f"[SUCCESS] QC complete. " f"Log written to: {output_log_path}")
    generate_readme()


# ==========================================
# MAIN
# ==========================================
# if __name__ == "__main__":
#     print("[INFO] Starting GRUAN Quality Control Pipeline...")
#     if not os.path.isfile(INPUT_NC_FILE):
#         print(f"[ERROR] NetCDF file not found: " f"{INPUT_NC_FILE}")
#     else:
#         profile_df = load_netcdf_profile(INPUT_NC_FILE)
#         if profile_df is not None and not profile_df.empty:
#             process_gruan_profiles(
#                 data_source=profile_df, output_log_path=OUTPUT_LOG_PATH
#             )
#         else:
#             print("[ERROR] No valid NetCDF profile " "was successfully loaded.")
#             print("[INFO] Generating documentation file anyway...")
#             generate_readme()

def load_all_profiles_from_directory(input_directory,
                                     file_pattern=JAR_FILE_PATTERN):
    """
    Scans input_directory for compressed .jar archives matching file_pattern,
    loads the embedded NetCDF profile from each one (fully in memory) via
    load_netcdf_profile, and concatenates the results into a single DataFrame.
    """
    search_path = os.path.join(input_directory, file_pattern)
    jar_files = sorted(glob.glob(search_path))

    if not jar_files:
        print(f"[WARNING] No jar archives found in '{input_directory}' "
              f"matching pattern '{file_pattern}'.")
        return pd.DataFrame()

    print(
        f"[INFO] Found {len(jar_files)} jar archive(s) in '{input_directory}'.")

    profile_frames = []
    for jar_file_path in jar_files:
        profile_df = load_netcdf_profile(jar_file_path)
        if profile_df is not None and not profile_df.empty:
            profile_frames.append(profile_df)
        else:
            print(f"[WARNING] Skipping empty/invalid profile: {jar_file_path}")

    if not profile_frames:
        return pd.DataFrame()

    return pd.concat(profile_frames, ignore_index=True)


if __name__ == "__main__":
    print("[INFO] Starting GRUAN Quality Control Pipeline...")

    all_profiles_df = load_all_profiles_from_directory(INPUT_DIRECTORY,
                                                       JAR_FILE_PATTERN)

    if all_profiles_df is not None and not all_profiles_df.empty:
        process_gruan_profiles(data_source=all_profiles_df,
                               output_log_path=OUTPUT_LOG_PATH)
    else:
        print(
            "[ERROR] No valid jar archives were successfully loaded. Execution aborted.")
        print("[INFO] Generating documentation file anyway...")
        generate_readme()