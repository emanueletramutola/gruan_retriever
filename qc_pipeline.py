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
# Geometric altitude first (the same quantity as the `height` levels it is
# compared with); the older attributes are only fallbacks.
TROPOPAUSE_ATTRIBUTE_CANDIDATES = (
    "g.Measurement.TropopauseAltitude",
    "g.Measurement.TropopauseGeopotHeight",
    "g.Ascent.TropopauseHeight",
)
WVMR_PPMV_UNITS = {"ppmv"}
WVMR_FRACTION_UNITS = {"1", "fraction"}

# ==========================================
# PLAUSIBILITY THRESHOLDS
# ==========================================
# RH is stored either in percent (0-100) or as a fraction (0-1), depending on
# the product. It is always converted to a fraction right after reading,
# according to the `units` attribute (see load_netcdf_profile). All thresholds
# below are therefore expressed as fractions.
RH_PERCENT_UNITS = {"percent", "%"}
RH_FRACTION_UNITS = {"1", "fraction"}
RH_MIN_FRACTION = 0.0           # 0 % RH
RH_MAX_FRACTION = 1.0           # 100 % RH

# Pressure is expressed in hPa.
PRESSURE_MIN_HPA = 1.0
PRESSURE_MAX_HPA = 1080.0

# Plausibility checks are evaluated only on levels up to this altitude (m).
# Use None to evaluate every level of the profile.
PLAUSIBILITY_MAX_ALTITUDE_M = 47000.0

# ==========================================
# PLAUSIBILITY RULES
# ==========================================
# For every variable:
#   val_col         : column holding the value
#   unc_total_col   : column holding the total uncertainty (or None)
#   other_unc_cols  : additional uncertainty columns (random/systematic).
#                     They NEVER cause the value to be rejected: they are
#                     set to NULL together with the value when the level is
#                     rejected, and a single one is set to NULL on its own
#                     when it is outside `unc_range`
#   unc_range       : (lo, hi) plausible range applied to EVERY uncertainty
#                     of the variable (total, random, systematic), both
#                     bounds included; None to skip the check. Only the
#                     total uncertainty can reject the value
#   check_range     : (lo, hi) physical plausibility range of the value,
#                     evaluated together with the total uncertainty as
#                     [value - unc, value + unc]; None to skip the check
#   dep_vars        : list of parent variables; if a parent is invalid the
#                     variable becomes invalid too (dependencies are
#                     propagated until stability)
#   report_scale    : scale factor used when writing reason texts
#   report_unit     : unit label used when writing reason texts
PLAUSIBILITY_RULES = {
    "temperature": {
        "val_col": "temperature",
        "unc_total_col": "temperature_uc_tot",
        "other_unc_cols": ["temperature_uc_std", "temperature_uc_sys"],
        "unc_range": (0.0, 10.0),
        "check_range": (178.15, 323.15),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "K",
    },
    "relative_humidity": {
        "val_col": "relative_humidity",
        "unc_total_col": "relative_humidity_uc_tot",
        "other_unc_cols": ["relative_humidity_uc_std",
                           "relative_humidity_uc_sys"],
        "unc_range": (0.0, 0.25),
        "check_range": (RH_MIN_FRACTION, RH_MAX_FRACTION),
        "dep_vars": [],
        "report_scale": 100.0,
        "report_unit": "%",
    },
    "wind_speed": {
        "val_col": "wind_speed",
        "unc_total_col": "wind_speed_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 90.0),
        "check_range": (0.0, 180.0),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "m s-1",
    },
    "wind_direction": {
        "val_col": "wind_direction",
        "unc_total_col": "wind_direction_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 90.0),
        "check_range": (0.0, 360.0),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "degree",
    },
    "u": {
        "val_col": "u",
        "unc_total_col": "u_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 90.0),
        "check_range": (0.0, 180.0),
        "dep_vars": ["wind_speed"],
        "report_scale": 1.0,
        "report_unit": "m s-1",
    },
    "v": {
        "val_col": "v",
        "unc_total_col": "v_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 90.0),
        "check_range": (0.0, 180.0),
        "dep_vars": ["wind_speed"],
        "report_scale": 1.0,
        "report_unit": "m s-1",
    },
    "pressure": {
        "val_col": "pressure",
        "unc_total_col": "pressure_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 10.0),
        "check_range": (PRESSURE_MIN_HPA, PRESSURE_MAX_HPA),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "hPa",
    },
    "geopot": {
        "val_col": "geopot",
        "unc_total_col": "geopot_uc_tot",
        "other_unc_cols": ["geopot_uc_sys"],
        "unc_range": (0.0, 1000.0),
        "check_range": (-10.0, 47000.0),
        "dep_vars": ["pressure"],
        "report_scale": 1.0,
        "report_unit": "m",
    },
    "wvmr": {
        "val_col": "wvmr",
        "unc_total_col": "wvmr_uc_tot",
        "other_unc_cols": ["wvmr_uc_sys"],
        "unc_range": (0.0, 12500.0),
        "check_range": (0.0, 50000.0),
        "dep_vars": ["relative_humidity"],
        "report_scale": 1.0,
        "report_unit": "ppmv",
    },
    "frost_point": {
        "val_col": "frost_point",
        "unc_total_col": "frost_point_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 10.0),
        "check_range": (150.0, 330.0),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "K",
    },
    "shortwave_radiation": {
        "val_col": "shortwave_radiation",
        "unc_total_col": "shortwave_radiation_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 400.0),
        "check_range": (0.0, 1600.0),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "W m-2",
    },
    "vertical_speed": {
        "val_col": "vertical_speed",
        "unc_total_col": "vertical_speed_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 10.0),
        "check_range": (-10.0, 30.0),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "m s-1",
    },
    "height": {
        "val_col": "height",
        "unc_total_col": "height_uc_tot",
        "other_unc_cols": [],
        "unc_range": (0.0, 1000.0),
        "check_range": (-10.0, 47000.0),
        "dep_vars": [],
        "report_scale": 1.0,
        "report_unit": "m",
    },
    # "Air relative humidity effective vertical resolution": stored either
    # in `res_rh` (CF-1.4 files) or in `rh_res` (CF-1.7 files). Only one of
    # the two columns is present at a time; the loader picks the first
    # available. It has no own check: it only follows RH through `dep_vars`.
    "rh_resolution": {
        "val_col": "rh_resolution",
        "unc_total_col": None,
        "other_unc_cols": [],
        "unc_range": None,
        "check_range": None,
        "dep_vars": ["relative_humidity"],
        "report_scale": 1.0,
        "report_unit": "s",
    },
}

# ==========================================
# INPUT/OUTPUT CONFIGURATION
# ==========================================
INPUT_DIRECTORY = "/Data/GRUAN/backup/gfa/archive/prodata/RS41-GDP_001/POT/POT-RS-01/2025"
JAR_FILE_PATTERN = "*.jar"
OUTPUT_LOG_PATH = "qc_log.csv"
logger = logging.getLogger(__name__)


# ==========================================
# CLOUD DETECTION (CBH / CTH)
# ==========================================
def find_consecutive_layers(rh_array, height_array, pressure_array,
                            rh_threshold, min_layers=3, direction="upward",
                            temp_array=None, temp_threshold=None):
    n = len(rh_array)
    condition = rh_array >= rh_threshold
    if temp_array is not None and temp_threshold is not None:
        condition = condition & (temp_array <= temp_threshold)
    indices = range(n) if direction == "upward" else range(n - 1, -1, -1)
    consecutive_count = 0
    for i in indices:
        if condition[i]:
            consecutive_count += 1
            if consecutive_count >= min_layers:
                target_idx = i - min_layers + 1 if direction == "upward" else i
                return {"index": target_idx, "height": height_array[target_idx],
                        "pressure": pressure_array[target_idx], "flag": True}
        else:
            consecutive_count = 0
    return {"index": np.nan, "height": np.nan, "pressure": np.nan,
            "flag": False}


def check_cloud_base(rh, height, pressure):
    """CBH: first level with RH >= 0.99 for 3 consecutive levels."""
    return find_consecutive_layers(rh, height, pressure, rh_threshold=0.99,
                                   min_layers=3, direction="upward")


def rh_over_ice(temp_k, rh_w):
    """Converts RH w.r.t. water into RH w.r.t. ice for T < 273.15 K (fraction)."""
    temp_c = temp_k - 273.15
    ew = 6.112 * np.exp((17.67 * temp_c) / (temp_c + 243.5))
    ei = 6.112 * np.exp((22.46 * temp_c) / (temp_c + 272.62))
    return np.where(temp_k < 273.15, rh_w * (ew / ei), rh_w)


def zhang_cold_cloud_layers(rh, height, temperature,
                            temp_max_k=CTH_TEMPERATURE_THR_K):
    """Zhang cold-cloud detection (applied only above 2 km AGL)."""
    valid = ~np.isnan(height) & ~np.isnan(temperature) & ~np.isnan(rh)
    if np.sum(valid) < 3:
        return []
    alt_msl = height[valid]
    temp_k = temperature[valid]
    rh_w = rh[valid]
    alt_agl = alt_msl - alt_msl[0]
    alt_km = alt_agl / 1000.0
    rh_calc = rh_over_ice(temp_k, rh_w)

    # OLD - START
    # min_rh = np.select(
    #     [(alt_km >= 2) & (alt_km < 6), (alt_km >= 6) & (alt_km <= 12),
    #      alt_km > 12], [0.90, 0.88, 0.80], default=np.nan)
    # max_rh = np.select(
    #     [(alt_km >= 2) & (alt_km < 6), (alt_km >= 6) & (alt_km <= 12),
    #      alt_km > 12], [0.93, 0.90, 0.70], default=np.nan)
    # inter_rh = np.select(
    #     [(alt_km >= 2) & (alt_km < 6), (alt_km >= 6) & (alt_km <= 12),
    #      alt_km > 12], [0.80, 0.75, 0.70], default=np.nan)
    # is_moist = ((alt_km >= 2.0) & ~np.isnan(min_rh)
    #             & (rh_calc >= min_rh) & (temp_k <= temp_max_k))
    # OLD - END

    # NEW - START
    min_rh = np.select(
        [alt_km < 6, (alt_km >= 6) & (alt_km <= 12), alt_km > 12],
        [0.90, 0.88, 0.80], default=np.nan)
    max_rh = np.select(
        [alt_km < 6, (alt_km >= 6) & (alt_km <= 12), alt_km > 12],
        [0.93, 0.90, 0.70], default=np.nan)
    inter_rh = np.select(
        [alt_km < 6, (alt_km >= 6) & (alt_km <= 12), alt_km > 12],
        [0.80, 0.75, 0.70], default=np.nan)
    is_moist = ((~np.isnan(min_rh)) & (rh_calc >= min_rh) & (temp_k <= temp_max_k))
    # NEW - END

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
        max_rh_layer = np.max(rh_calc[s: e + 1])
        req_max_rh = np.nanmax(max_rh[s: e + 1])
        if max_rh_layer >= req_max_rh:
            candidate_layers.append({
                "start_idx": s, "end_idx": e,
                "base_agl": base_agl, "top_agl": top_agl,
                "base_msl": alt_msl[s], "top_msl": alt_msl[e],
                "thickness": top_agl - base_agl,
                "mean_temp_c": mean_temp_k - 273.15,
                "max_rh_i": max_rh_layer,
            })
    if not candidate_layers:
        return []
    merged_layers = []
    curr = candidate_layers[0]
    for nxt in candidate_layers[1:]:
        gap_dist = nxt["base_agl"] - curr["top_agl"]
        gap_rh = rh_calc[curr["end_idx"]: nxt["start_idx"] + 1]
        gap_inter_rh = np.nanmean(
            inter_rh[curr["end_idx"]:nxt["start_idx"] + 1])
        min_rh_gap = np.nanmin(gap_rh) if len(gap_rh) > 0 else 0
        if gap_dist < 300 or min_rh_gap >= gap_inter_rh:
            curr["end_idx"] = nxt["end_idx"]
            curr["top_agl"] = nxt["top_agl"]
            curr["top_msl"] = nxt["top_msl"]
            curr["thickness"] = curr["top_agl"] - curr["base_agl"]
            curr["max_rh_i"] = max(curr["max_rh_i"], nxt["max_rh_i"])
            curr["mean_temp_c"] = (np.mean(
                temp_k[curr["start_idx"]:curr["end_idx"] + 1]) - 273.15)
        else:
            merged_layers.append(curr)
            curr = nxt
    merged_layers.append(curr)
    return [layer for layer in merged_layers if layer["thickness"] >= 100]


def check_cloud_top(rh, height, pressure, temperature):
    layers = zhang_cold_cloud_layers(rh, height, temperature,
                                     temp_max_k=CTH_TEMPERATURE_THR_K)
    if not layers:
        return {"index": np.nan, "height": np.nan, "pressure": np.nan,
                "flag": False}
    top_layer = max(layers, key=lambda l: l["top_msl"])
    valid = ~np.isnan(height) & ~np.isnan(temperature) & ~np.isnan(rh)
    valid_pressure = pressure[valid]
    top_pressure = (valid_pressure[top_layer["end_idx"]]
                    if top_layer["end_idx"] < len(valid_pressure) else np.nan)
    # `end_idx` refers to the compacted (valid-only) arrays used by the
    # Zhang algorithm: map it back to the index of the original profile.
    original_index = int(np.flatnonzero(valid)[top_layer["end_idx"]])
    return {"index": original_index, "height": top_layer["top_msl"],
            "pressure": top_pressure, "flag": True}


def check_pressure_flags(pressure):
    pmin = np.nanmin(pressure) if not np.all(np.isnan(pressure)) else np.nan
    if np.isnan(pmin):
        return pmin, False, False
    return pmin, bool(pmin <= 10.0), bool(pmin <= 5.0)


# ==========================================
# PLAUSIBILITY
# ==========================================
def _compose_reasons(n_levels, evaluated_levels, parts):
    """
    Builds the text of the reasons for one variable of one profile.

    `parts` is a list of (boolean mask over the EVALUATED levels, text).
    Returns an object array with one item per level of the profile
    ("" = no reason; several reasons are joined with "; "), or None if no
    level has any reason.
    """
    reason_array = None
    for mask, text in parts:
        if not mask.any():
            continue
        if reason_array is None:
            reason_array = np.full(n_levels, "", dtype=object)
        full_length = np.zeros(n_levels, dtype=bool)
        full_length[evaluated_levels] = mask
        current = reason_array[full_length]
        reason_array[full_length] = np.where(current == "", text,
                                             current + "; " + text)
    return reason_array


def evaluate_level_plausibility(profile_data,
                                max_altitude_m=PLAUSIBILITY_MAX_ALTITUDE_M,
                                return_reasons=False,
                                rules=None,
                                expected_uncertainties=None):
    """
    Level-by-level plausibility evaluation of a SINGLE profile.

    `profile_data` is a DataFrame holding the levels of one profile with the
    columns `height`, the checked variables and their uncertainty columns
    (see PLAUSIBILITY_RULES). Only levels up to `max_altitude_m` are
    evaluated (None = all levels); levels with unknown height are never
    evaluated.

    `expected_uncertainties` is an optional dict {qc_unc_key: bool} telling
    the evaluator which uncertainty columns the source file format of this
    profile is expected to provide. The missing-uncertainty rule concerns
    the TOTAL uncertainty only and is applied only when the total
    uncertainty of the variable is marked True. A total uncertainty the
    format does not provide is not flagged as missing, and only the
    value-range check applies to the variable. When None, every declared
    total uncertainty is expected (backwards-compatible behaviour).

    Returns (evaluated_levels, rejected, uncertainty_rejected,
    column_rejected):
      - evaluated_levels    : boolean array, True for evaluated levels;
      - rejected            : {variable: boolean array | None}; True where
                              the level is rejected, i.e. the value must be
                              set to NULL. None = variable could not be
                              evaluated (missing column, error, no valid
                              data);
      - uncertainty_rejected: {variable: boolean array | None}; True where
                              the TOTAL uncertainty of the variable is
                              unusable: NaN (when the file format is
                              expected to provide it) or outside its
                              plausible `unc_range`. These levels are also
                              True in `rejected`. The value AND all the
                              uncertainties of the variable (total, random,
                              systematic) must be set to NULL. None means
                              there is no total uncertainty to evaluate;
      - column_rejected     : {uncertainty key: boolean array | None} for
                              the random/systematic uncertainty columns
                              (`other_unc_cols`); True where that single
                              uncertainty is outside `unc_range`. ONLY that
                              uncertainty must be set to NULL: the value and
                              the other uncertainties are not affected, and
                              these levels are NOT in `rejected`. None means
                              the check could not be performed (column not
                              available, or no `unc_range`).
    With return_reasons=True, a 5th item is returned:
      - reasons             : {variable: object array | None} with the text
                              explaining each rejection ("" for accepted
                              levels). None if no level of the variable has
                              a reason.

    Rules applied (per variable):
      1. Missing total uncertainty: if the file format is expected to
         provide the total uncertainty (see `expected_uncertainties`), it
         must be non-NaN at the level; otherwise the level is rejected.
         Random and systematic uncertainties that are NaN never reject a
         level.
      2. Uncertainty range: every available uncertainty column (total,
         random, systematic) is compared with `unc_range`.
           * total uncertainty outside the range: the level is rejected;
           * random/systematic uncertainty outside the range: only that
             uncertainty is flagged in `column_rejected`.
      3. Value plausibility: for out-of-range values the interval
         [value - unc, value + unc] is compared with `check_range`:
             value - unc > hi   OR   value + unc < lo
         If the whole interval falls outside the valid range, the value is
         rejected. When the total uncertainty is NaN at a level (e.g. the
         source file format does not provide it) the interval is undefined
         and the level is checked directly with `value vs [lo, hi]`.
      4. Dependency propagation: if any parent in `dep_vars` is invalid
         (NaN or rejected), the variable itself is rejected. Dependencies
         are propagated until stability.
    """
    if rules is None:
        rules = PLAUSIBILITY_RULES

    n_levels = len(profile_data)
    height = profile_data["height"].to_numpy()
    if max_altitude_m is None:
        evaluated_levels = np.ones(n_levels, dtype=bool)
    else:
        evaluated_levels = height <= max_altitude_m

    if not evaluated_levels.any():
        early_rejected = {var: np.zeros(n_levels, dtype=bool)
                          for var in rules}
        early_unc = {var: np.zeros(n_levels, dtype=bool)
                     for var, info in rules.items()
                     if info.get("unc_total_col") is not None}
        early_columns = {col: np.zeros(n_levels, dtype=bool)
                         for info in rules.values()
                         if info.get("unc_range") is not None
                         for col in info.get("other_unc_cols", [])}
        if return_reasons:
            return (evaluated_levels, early_rejected, early_unc,
                    early_columns, {var: None for var in rules})
        return evaluated_levels, early_rejected, early_unc, early_columns

    n_evaluated = int(evaluated_levels.sum())
    rejected = {}
    uncertainty_rejected = {}
    column_rejected = {}
    reasons = {}

    # -------------------------------------------------------------
    # Phase 1: missing total uncertainty + uncertainty range + value check
    # -------------------------------------------------------------
    for var, info in rules.items():
        val_col = info["val_col"]
        unc_col = info.get("unc_total_col")
        other_unc_cols = list(info.get("other_unc_cols", []))
        unc_range = info.get("unc_range")
        check_range = info.get("check_range")
        scale = info.get("report_scale", 1.0)
        unit = info.get("report_unit", "")

        value_parts = []
        missing_parts = []
        total_range_parts = []
        # Over the evaluated levels: True where the TOTAL uncertainty is
        # unusable (missing or out of range). None = nothing to evaluate.
        total_unusable = None

        # --- Missing total uncertainty check ---
        # A measurement value is invalid whenever its TOTAL uncertainty is
        # missing (NaN). Random and systematic uncertainties never reject
        # the value. Whether the total uncertainty is "expected" depends on
        # the file format (CF-1.4 / CF-1.7): the caller passes
        # expected_uncertainties to restrict the rule to the profiles whose
        # format is supposed to provide it. A total uncertainty the format
        # does not provide is not flagged as missing; one the format does
        # provide and that carries NaN at an individual level is.
        if unc_col is not None:
            total_expected = (expected_uncertainties is None
                              or expected_uncertainties.get(unc_col, True))
            if total_expected:
                if unc_col not in profile_data.columns:
                    missing_total = np.ones(n_evaluated, dtype=bool)
                    missing_text = (f"no total uncertainty column available "
                                    f"(expected: '{unc_col}')")
                else:
                    missing_total = np.isnan(
                        profile_data[unc_col].to_numpy()[evaluated_levels])
                    missing_text = (f"missing total uncertainty (NaN) in "
                                    f"'{unc_col}'")
                total_unusable = missing_total
                if return_reasons:
                    missing_parts.append((missing_total, missing_text))

        # --- Uncertainty range check on EVERY available uncertainty ---
        if unc_range is not None:
            unc_lo, unc_hi = unc_range
            declared = ([unc_col] if unc_col is not None else []) \
                + other_unc_cols
            missing = [c for c in declared if c not in profile_data.columns]
            if missing:
                logging.warning(
                    f"Missing uncertainty column(s) for '{var}': "
                    f"{missing}; their range check is skipped")

            for col in declared:
                if col not in profile_data.columns:
                    continue
                vals = profile_data[col].to_numpy()[evaluated_levels]
                rej_col = (vals < unc_lo) | (vals > unc_hi)
                if col == unc_col:
                    # Total uncertainty out of range: the level is rejected.
                    total_unusable = (rej_col if total_unusable is None
                                      else total_unusable | rej_col)
                    if return_reasons:
                        total_range_parts.append((
                            rej_col,
                            f"total uncertainty outside plausible range "
                            f"[{unc_lo * scale:g}, {unc_hi * scale:g}] "
                            f"{unit}"))
                else:
                    # Random/systematic uncertainty out of range: only this
                    # uncertainty is flagged, the value is not rejected.
                    full_col = np.zeros(n_levels, dtype=bool)
                    full_col[evaluated_levels] = rej_col
                    column_rejected[col] = full_col
        for col in other_unc_cols:
            column_rejected.setdefault(col, None)

        if total_unusable is not None:
            full = np.zeros(n_levels, dtype=bool)
            full[evaluated_levels] = total_unusable
            uncertainty_rejected[var] = full
        else:
            uncertainty_rejected[var] = None

        # --- Value check ---
        if val_col not in profile_data.columns:
            logging.error(f"Missing column: '{val_col}'")
            rejected[var] = None
            if return_reasons:
                reasons[var] = None
            continue

        var_data = profile_data[val_col].to_numpy()[evaluated_levels]

        if np.all(np.isnan(var_data)):
            rejected[var] = None
            if return_reasons:
                reasons[var] = _compose_reasons(
                    n_levels, evaluated_levels,
                    missing_parts + total_range_parts)
            continue

        level_rejected = np.zeros_like(var_data, dtype=bool)

        # An unusable total uncertainty (missing or out of range) rejects
        # the level: it is not evaluated further.
        if total_unusable is not None:
            level_rejected |= total_unusable

        if check_range is not None:
            c_min, c_max = check_range
            if unc_col is not None and unc_col in profile_data.columns:
                unc = profile_data[unc_col].to_numpy()[evaluated_levels]
                val_minus_unc = var_data - unc
                val_plus_unc = var_data + unc
                invalid = (val_minus_unc > c_max) | (val_plus_unc < c_min)
                # Where the total uncertainty is NaN the interval
                # [value - unc, value + unc] is undefined and the
                # comparisons above silently return False (any
                # comparison with NaN is False). Those levels would
                # escape the check. This happens when the source file
                # format does not provide the total uncertainty (e.g.
                # CF-1.4 files for vertical_speed, where 'vspeed_uc'
                # exists only in CF-1.7), or when the format does
                # provide it but the level already failed the
                # missing-total-uncertainty rule above. In both cases fall
                # back to a plain value check, so out-of-range values
                # are still caught.
                missing_unc = np.isnan(unc)
                if missing_unc.any():
                    invalid = np.where(
                        missing_unc,
                        (var_data < c_min) | (var_data > c_max),
                        invalid)
                if return_reasons:
                    value_parts.append((
                        invalid & ~missing_unc,
                        f"value \u00b1 uncertainty outside plausible range "
                        f"[{c_min * scale:g}, {c_max * scale:g}] {unit}"
                    ))
                    value_parts.append((
                        invalid & missing_unc,
                        f"value outside plausible range "
                        f"[{c_min * scale:g}, {c_max * scale:g}] {unit} (no total "
                        f"uncertainty available)"
                    ))
            else:
                invalid = (var_data < c_min) | (var_data > c_max)
                if return_reasons:
                    value_parts.append((
                        invalid,
                        f"value outside plausible range "
                        f"[{c_min * scale:g}, {c_max * scale:g}] {unit}"
                    ))
            level_rejected |= invalid

        full = np.zeros(n_levels, dtype=bool)
        full[evaluated_levels] = level_rejected
        rejected[var] = full

        if return_reasons:
            reasons[var] = _compose_reasons(
                n_levels, evaluated_levels,
                missing_parts + value_parts + total_range_parts)

    # -------------------------------------------------------------
    # Phase 2: dependency propagation (until stability)
    # -------------------------------------------------------------
    changed = True
    while changed:
        changed = False
        for var, info in rules.items():
            dep_vars = info.get("dep_vars", [])
            if not dep_vars:
                continue
            val_col = info["val_col"]
            if val_col not in profile_data.columns:
                continue

            parent_null = np.zeros(n_levels, dtype=bool)
            for parent in dep_vars:
                parent_info = rules.get(parent)
                if parent_info is None:
                    continue
                parent_col = parent_info["val_col"]
                if parent_col not in profile_data.columns:
                    continue
                p_null = profile_data[parent_col].isna().to_numpy()
                if rejected.get(parent) is not None:
                    p_null = p_null | rejected[parent]
                parent_null |= p_null

            parent_null &= evaluated_levels
            if not parent_null.any():
                continue

            if rejected.get(var) is None:
                rejected[var] = np.zeros(n_levels, dtype=bool)

            new_rej = rejected[var] | parent_null
            if not np.array_equal(new_rej, rejected[var]):
                rejected[var] = new_rej
                changed = True
                if return_reasons:
                    reason_array = reasons.get(var)
                    if reason_array is None:
                        reason_array = np.full(n_levels, "", dtype=object)
                    current = reason_array[parent_null]
                    text = f"parent variable '{parent}' is not valid"
                    reason_array[parent_null] = np.where(
                        current == "", text, current + "; " + text)
                    reasons[var] = reason_array

    if return_reasons:
        for var in rules:
            reasons.setdefault(var, None)
        return (evaluated_levels, rejected, uncertainty_rejected,
                column_rejected, reasons)

    return evaluated_levels, rejected, uncertainty_rejected, column_rejected


def check_plausibility(profile_data,
                       max_altitude_m=PLAUSIBILITY_MAX_ALTITUDE_M):
    """
    Returns {variable: True | False | np.nan} for the whole profile. A
    variable is plausible (True) only if NO level is rejected; np.nan means
    the variable has no valid data at all.
    """
    evaluated_levels, rejected = evaluate_level_plausibility(
        profile_data, max_altitude_m)[:2]
    if not evaluated_levels.any():
        return {var: False for var in PLAUSIBILITY_RULES}
    return {
        var: (np.nan if level_mask is None else not np.any(level_mask))
        for var, level_mask in rejected.items()
    }


def apply_plausibility_nulling(df_profile, rejected, uncertainty_rejected,
                               column_rejected=None):
    """
    Sets to NULL:
      - the value, the total uncertainty and every other uncertainty column
        (random/systematic) of every level rejected by the plausibility
        checks (`rejected`) or whose total uncertainty is unusable, i.e.
        NaN or out of range (`uncertainty_rejected`);
      - ONLY the single random/systematic uncertainty column of the levels
        where it is outside its plausible range (`column_rejected`). The
        value and the other uncertainties are kept.

    `rejected`, `uncertainty_rejected` and `column_rejected` are the outputs
    of evaluate_level_plausibility.
    """
    df = df_profile.copy()
    for var, info in PLAUSIBILITY_RULES.items():
        mask = None
        for level_mask in (rejected.get(var), uncertainty_rejected.get(var)):
            if level_mask is not None:
                mask = level_mask if mask is None else mask | level_mask
        if mask is None:
            continue
        cols = [info["val_col"]]
        if info.get("unc_total_col"):
            cols.append(info["unc_total_col"])
        cols.extend(info.get("other_unc_cols", []))
        for col in cols:
            if col in df.columns:
                df.loc[mask, col] = np.nan

    for col, mask in (column_rejected or {}).items():
        if mask is not None and col in df.columns:
            df.loc[mask, col] = np.nan
    return df


def flag_implausible_levels(data, profile_id_column="profile_id",
                            max_altitude_m=PLAUSIBILITY_MAX_ALTITUDE_M,
                            return_reasons=False,
                            expected_uncertainties_by_profile=None):
    """
    Level-by-level plausibility flags for a DataFrame stacking MANY profiles.

    Returns (implausible, implausible_uncertainty, implausible_columns):
      - implausible            : one column per checked variable, True where
                                 the level is NOT plausible (the value must
                                 be set to NULL);
      - implausible_uncertainty: one column per variable with a total
                                 uncertainty, True where the TOTAL
                                 uncertainty is unusable (NaN when the file
                                 format is expected to provide it, or
                                 outside its plausible range). These levels
                                 are also True in `implausible`: value and
                                 all the uncertainties of the variable must
                                 be set to NULL;
      - implausible_columns    : one column per random/systematic
                                 uncertainty (named by its QC key, e.g.
                                 'temperature_uc_std'), True where that
                                 single uncertainty is outside its plausible
                                 range. ONLY that uncertainty must be set to
                                 NULL: these levels do not make the value
                                 implausible.
    Levels that could not be evaluated are False.
    With return_reasons=True, a dict {variable: Series} is returned as 4th
    item with the text of the reason for the rejected levels only (indexed
    by positional row numbers of `data`).

    expected_uncertainties_by_profile:
        Optional dict {profile_id: {qc_unc_key: bool} | None}. It tells the
        evaluator, for every profile, which uncertainty columns the file
        format of that profile is expected to provide. The missing-
        uncertainty rule (total uncertainty only) is applied only when the
        total uncertainty is marked True; a total uncertainty that the
        format does not provide is not flagged as missing.
        A per-profile value of None (or a profile_id absent from the dict)
        means "expect all" (backwards-compatible behaviour).
    """
    n_rows = len(data)
    implausible = {var: np.zeros(n_rows, dtype=bool)
                   for var in PLAUSIBILITY_RULES}
    implausible_uncertainty = {
        var: np.zeros(n_rows, dtype=bool)
        for var, info in PLAUSIBILITY_RULES.items()
        if info.get("unc_total_col") is not None
    }
    implausible_columns = {
        col: np.zeros(n_rows, dtype=bool)
        for info in PLAUSIBILITY_RULES.values()
        if info.get("unc_range") is not None
        for col in info.get("other_unc_cols", [])
    }
    rows_by_profile = data.groupby(profile_id_column, sort=False).indices
    reason_rows = {var: [] for var in PLAUSIBILITY_RULES}
    reason_texts = {var: [] for var in PLAUSIBILITY_RULES}

    for profile_id, rows in rows_by_profile.items():
        expected_unc = None
        if expected_uncertainties_by_profile is not None:
            expected_unc = expected_uncertainties_by_profile.get(profile_id)

        result = evaluate_level_plausibility(
            data.iloc[rows], max_altitude_m,
            return_reasons=return_reasons,
            expected_uncertainties=expected_unc)
        _, rejected, uncertainty_rejected, column_rejected = result[:4]
        if return_reasons:
            for var, reason_array in result[4].items():
                if reason_array is None:
                    continue
                has_reason = reason_array != ""
                reason_rows[var].append(rows[has_reason])
                reason_texts[var].append(reason_array[has_reason])
        for var, level_mask in rejected.items():
            if level_mask is not None:
                implausible[var][rows] = level_mask
        for var, level_mask in uncertainty_rejected.items():
            if level_mask is not None:
                implausible_uncertainty[var][rows] = level_mask
        for col, level_mask in column_rejected.items():
            if level_mask is not None:
                implausible_columns[col][rows] = level_mask

    flags = (pd.DataFrame(implausible, index=data.index),
             pd.DataFrame(implausible_uncertainty, index=data.index),
             pd.DataFrame(implausible_columns, index=data.index))
    if not return_reasons:
        return flags
    reasons = {}
    for var in PLAUSIBILITY_RULES:
        if reason_rows[var]:
            positions = np.concatenate(reason_rows[var])
            texts = np.concatenate(reason_texts[var])
            order = np.argsort(positions, kind="stable")
            reasons[var] = pd.Series(texts[order], index=positions[order],
                                     dtype=object)
        else:
            reasons[var] = pd.Series([], index=pd.Index([], dtype=np.int64),
                                     dtype=object)
    return flags + (reasons,)


# ==========================================
# COMPLETENESS
# ==========================================
def check_variable_completeness(var_array, height, zmin=None, zmax=None,
                                thr_high=COMPLETENESS_THR_HIGH,
                                thr_good=COMPLETENESS_THR_GOOD):
    if zmin is None and zmax is None:
        n_total = len(var_array)
        if n_total == 0:
            return 0, 0, np.nan, "FAIL"
        n_missing = int(np.sum(np.isnan(var_array) | np.isnan(height)))
    else:
        layer_lo = zmin if zmin is not None else -np.inf
        layer_hi = zmax if zmax is not None else np.inf
        layer_mask = (~np.isnan(height) & (height >= layer_lo)
                      & (height <= layer_hi))
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
    return n_total, n_missing, completeness, flag_complete


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


def parse_altitude_attribute_m(attr_value):
    """
    Parses an altitude attribute such as '8995.3 gpm', '9042.2 m' or
    '9.0 km' and returns it in metres. Missing values, unknown units,
    sentinels (e.g. -999) and values outside (0, PLAUSIBILITY_MAX_ALTITUDE_M)
    return NaN.
    """
    if attr_value is None:
        return np.nan
    match = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?\s*([A-Za-z]*)",
                      str(attr_value))
    if not match:
        return np.nan
    value = parse_numeric_attribute(attr_value)
    unit = match.group(1).strip().lower()
    if unit in ("", "m", "gpm"):
        scale = 1.0
    elif unit == "km":
        scale = 1000.0
    else:
        print(f"[WARNING] Unrecognised unit '{unit}' in the tropopause "
              f"attribute '{attr_value}': tropopause set to NaN.")
        return np.nan
    value *= scale
    if not (0.0 < value < PLAUSIBILITY_MAX_ALTITUDE_M):
        print(f"[WARNING] Tropopause attribute '{attr_value}' is outside "
              f"the valid range: tropopause set to NaN.")
        return np.nan
    return value


# ==========================================
# README
# ==========================================
def generate_readme():
    readme_content = """# GRUAN Quality Control Pipeline

## Input
The pipeline processes GRUAN NetCDF radiosonde profiles packaged inside
.jar archives.

## CBH
Cloud Base Height is identified as the first level belonging to at least
three consecutive levels with RH >= 0.99.

## CTH - Zhang cold cloud detection
Cold-cloud detection is applied only above 2 km AGL. Altitude bands:
2-6 km, 6-12 km, >12 km. Only clouds with temperature <= -5 degC are
considered. RH is converted from w.r.t. water to w.r.t. ice for
temperatures below 0 degC.

## Completeness
Despite its name, `Completeness_*` is the FRACTION OF MISSING data
records: 0 means a fully complete variable, 1 means that every record
is missing. Lower values are therefore better.
HIGH: < 0.10
GOOD: 0.10 <= C < 0.20
FAIL: >= 0.20

Notes:
- It is computed after the plausibility check, so levels rejected by
  the plausibility rules (set to NULL) count as missing.
- `Ntotal_<var>` is the number of levels of the whole profile.
  `Ntotal_<var>_troposphere` / `_stratosphere` count only the levels
  with a known height, split at the tropopause altitude, so their sum
  can be lower than `Ntotal_<var>`. If the tropopause is unknown the
  two layers are reported as N/A with Ntotal = 0.
- For the whole profile a level is missing if the variable or its
  height is missing; for the two layers only the variable is checked.
- The tropopause altitude (metres) is read from the file attributes:
  the geometric altitude of the tropopause
  (g.Measurement.TropopauseAltitude), which is the same quantity as the
  profile heights it is compared with. Only if it is missing, the
  geopotential height or the older g.Ascent.TropopauseHeight attribute
  is used instead.

## Plausibility
A variable is plausible (1) if no level up to 47 km is rejected, not
plausible (0) otherwise. If no level can be evaluated (e.g. all heights
are unknown) the flag is NaN. For each variable the following rules are
applied level by level:

0. Missing total uncertainty: a level is rejected if the TOTAL
   uncertainty of the variable is NaN at that level and the file format
   (CF-1.4 or CF-1.7) is expected to provide it. A total uncertainty that
   exists in the other format only is not treated as missing. The
   expectations per format are the same used by the NetCDF exporter. If
   the format cannot be determined, the total uncertainty is expected.
   Random and systematic uncertainties that are NaN never reject a level.
1. Uncertainty range: every available uncertainty column of the variable
   (total, random, systematic) is compared with the plausible range of
   the variable.
   - Total uncertainty outside the range: the level is rejected,
     regardless of the value.
   - Random or systematic uncertainty outside the range: only that
     uncertainty is set to NULL; the value and the other uncertainties
     are kept.
2. Value plausibility: for out-of-range values the interval
   [value - unc, value + unc] is compared with the physical range. The
   level is rejected only if the whole interval is outside the range:
       value - unc > hi   OR   value + unc < lo
   Otherwise the value is accepted (consistent within its uncertainty).
3. Dependencies: if a parent variable is invalid, the variable itself is
   invalid too. Dependencies:
   - u             -> wind_speed
   - v             -> wind_speed
   - geopot        -> pressure
   - wvmr          -> relative_humidity
   - rh_resolution -> relative_humidity

When the data is exported, the value, its random, systematic and total
uncertainties are set to NULL for the rejected levels and for the levels
where the total uncertainty is NaN or out of range. A random or systematic
uncertainty that is out of range is set to NULL on its own.
"""
    readme_path = os.path.join(os.path.dirname(OUTPUT_LOG_PATH), "README.md")
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)
    print(f"[INFO] README.md written to: {readme_path}")


# ==========================================
# NETCDF LOADING
# ==========================================
def extract_netcdf_bytes_from_jar(jar_path):
    """
    Reads the single embedded NetCDF (.nc) file from a .jar archive fully
    in memory, without writing to disk.
    Returns (nc_bytes, internal_nc_name).
    """
    with zipfile.ZipFile(jar_path, "r") as zf:
        nc_entries = [info for info in zf.infolist()
                      if info.filename.lower().endswith(".nc")]
        if not nc_entries:
            raise FileNotFoundError(
                f"No .nc file found inside jar archive: {jar_path}")
        if len(nc_entries) > 1:
            logging.warning(
                f"Multiple .nc files found inside '{jar_path}'; using the "
                f"first one found: {nc_entries[0].filename}")
        nc_entry = nc_entries[0]
        nc_bytes = zf.read(nc_entry.filename)
    return nc_bytes, nc_entry.filename


# ------------------------------------------------------------------
# Uncertainty columns expected from each file format (CF-1.4 / CF-1.7).
# This mirrors the logic of gruan_exporter.py (QC_UNCERTAINTIES_BY_FORMAT,
# _detect_profile_formats), so that the log and the exported data apply
# the missing-total-uncertainty rule in the same way. Keep it in sync with
# the exporter: CDM_VARIABLES / QC_INPUT_COLUMNS are the source of truth.
# ------------------------------------------------------------------
# NetCDF/DB columns holding uncertainties that exist in one format only.
CF14_UNC_FILE_COLUMNS = frozenset({
    "u_alt", "u_cor_rh", "u_cor_temp", "u_press", "u_rh", "u_std_rh",
    "u_std_temp", "u_swrad", "u_temp", "u_wdir", "u_wspeed",
})
CF17_UNC_FILE_COLUMNS = frozenset({
    "alt_gph_uc", "alt_gph_uc_tcor", "alt_uc", "fp_uc", "press_uc",
    "rh_uc", "rh_uc_tcor", "temp_uc", "temp_uc_tcor", "vspeed_uc",
    "wdir_uc", "wmeri_uc", "wspeed_uc", "wvmr_vol_uc", "wvmr_vol_uc_tcor",
    "wzon_uc",
})
# QC uncertainty key -> file columns it can be read from.
QC_UNCERTAINTY_SOURCES = {
    "height_uc_tot":              ("u_alt", "alt_uc",),
    "temperature_uc_tot":         ("u_temp", "temp_uc"),
    "relative_humidity_uc_tot":   ("u_rh", "rh_uc"),
    "wind_speed_uc_tot":          ("u_wspeed", "wspeed_uc"),
    "wind_direction_uc_tot":      ("u_wdir", "wdir_uc"),
    "u_uc_tot":                   ("wzon_uc",),
    "v_uc_tot":                   ("wmeri_uc",),
    "pressure_uc_tot":            ("u_press", "press_uc"),
    "geopot_uc_tot":              ("alt_gph_uc",),
    "wvmr_uc_tot":                ("wvmr_vol_uc",),
    "frost_point_uc_tot":         ("fp_uc",),
    "shortwave_radiation_uc_tot": ("u_swrad",),
    "vertical_speed_uc_tot":      ("vspeed_uc",),
    "temperature_uc_std":         ("u_std_temp",),
    "relative_humidity_uc_std":   ("u_std_rh",),
    "temperature_uc_sys":         ("temp_uc_tcor", "u_cor_temp"),
    "relative_humidity_uc_sys":   ("rh_uc_tcor", "u_cor_rh"),
    "wvmr_uc_sys":                ("wvmr_vol_uc_tcor",),
    "geopot_uc_sys":              ("alt_gph_uc_tcor",),
}
QC_UNCERTAINTIES_BY_FORMAT = {
    "CF-1.4": {k: bool(set(v) & CF14_UNC_FILE_COLUMNS)
               for k, v in QC_UNCERTAINTY_SOURCES.items()},
    "CF-1.7": {k: bool(set(v) & CF17_UNC_FILE_COLUMNS)
               for k, v in QC_UNCERTAINTY_SOURCES.items()},
}
UNC_FORMAT_COLUMN = "uncertainty_format"


def detect_uncertainty_format(columns_with_data):
    """
    'CF-1.7' if any CF-1.7-only uncertainty column carries data, else
    'CF-1.4' if any CF-1.4-only one does, else None (undetermined).
    Same precedence as the exporter.
    """
    cols = set(columns_with_data)
    if cols & CF17_UNC_FILE_COLUMNS:
        return "CF-1.7"
    if cols & CF14_UNC_FILE_COLUMNS:
        return "CF-1.4"
    return None


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
            dt = datetime.strptime(
                str(dt_str).split(".")[0].rstrip("Zz"),
                "%Y-%m-%dT%H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            # Never substitute the processing time for the measurement
            # time: flag the profile as having an unknown date instead.
            print(f"[WARNING] Cannot parse the profile date "
                  f"'{dt_str}' in {os.path.basename(jar_file_path)}: "
                  f"Date set to UNKNOWN.")
            dt = "UNKNOWN"

        tropopause_attr = None
        for attr_name in TROPOPAUSE_ATTRIBUTE_CANDIDATES:
            if attr_name in ds.attrs:
                tropopause_attr = ds.attrs[attr_name]
                break
        tropopause_altitude = parse_altitude_attribute_m(tropopause_attr)

        # Variable mapping: internal name -> candidate NetCDF names.
        # The first candidate found wins (CF-1.4 first, then CF-1.7).
        mapping = {
            # Core profile
            "height":                      ["alt"],
            "height_uc_tot":               ["u_alt", "alt_uc"],
            "pressure":                    ["press"],
            "pressure_uc_tot":             ["press_uc", "u_press"],
            "temperature":                 ["temp"],
            "temperature_uc_std":          ["u_std_temp"],
            "temperature_uc_sys":          ["temp_uc_tcor", "u_cor_temp"],
            "temperature_uc_tot":          ["temp_uc", "u_temp"],
            "relative_humidity":           ["rh"],
            "relative_humidity_uc_std":    ["u_std_rh"],
            "relative_humidity_uc_sys":    ["rh_uc_tcor", "u_cor_rh"],
            "relative_humidity_uc_tot":    ["rh_uc", "u_rh"],
            # Wind
            "wind_speed":                  ["wspeed"],
            "wind_speed_uc_tot":           ["wspeed_uc", "u_wspeed"],
            "wind_direction":              ["wdir"],
            "wind_direction_uc_tot":       ["wdir_uc", "u_wdir"],
            "u":                           ["u", "wzon"],
            "u_uc_tot":                    ["wzon_uc"],
            "v":                           ["v", "wmeri"],
            "v_uc_tot":                    ["wmeri_uc"],
            # WVMR
            "wvmr":                        ["wvmr_vol", "WVMR", "wvmr"],
            "wvmr_uc_sys":                 ["wvmr_vol_uc_tcor"],
            "wvmr_uc_tot":                 ["wvmr_vol_uc"],
            # Geopotential
            "geopot":                      ["geopot", "alt_gph"],
            "geopot_uc_sys":               ["alt_gph_uc_tcor"],
            "geopot_uc_tot":               ["alt_gph_uc"],
            # Other
            "frost_point":                 ["fp", "FP"],
            "frost_point_uc_tot":          ["fp_uc"],
            "shortwave_radiation":         ["swrad", "SWrad"],
            "shortwave_radiation_uc_tot":  ["u_swrad", "u_SWrad"],
            "vertical_speed":              ["vspeed", "asc"],
            "vertical_speed_uc_tot":       ["vspeed_uc"],
            # "Air relative humidity effective vertical resolution":
            # CF-1.4 files store it in `res_rh`, CF-1.7 in `rh_res`.
            "rh_resolution":               ["res_rh", "rh_res"],
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

        # File format (CF-1.4 / CF-1.7), from the format-specific
        # uncertainty columns that carry data. It drives the
        # `expected_uncertainties` argument of the plausibility check.
        cols_with_data = set()
        lower_to_real = {name.lower(): name for name in ds.variables}
        for _col in CF14_UNC_FILE_COLUMNS | CF17_UNC_FILE_COLUMNS:
            _real = lower_to_real.get(_col)
            if _real is not None:
                try:
                    if np.isfinite(ds[_real].values.astype(float)).any():
                        cols_with_data.add(_col)
                except (TypeError, ValueError):
                    pass
        uncertainty_format = detect_uncertainty_format(cols_with_data)

        ref_len = 0
        for k in ["pressure", "height", "temperature"]:
            if isinstance(extracted_data.get(k), np.ndarray):
                ref_len = len(extracted_data[k])
                break
        if ref_len == 0:
            raise ValueError(
                "No core profile data (pressure/height) could be extracted.")

        for k, v in extracted_data.items():
            if v is None:
                extracted_data[k] = np.full(ref_len, np.nan)

        # RH unit conversion (percent -> fraction).
        for rh_key in ("relative_humidity",
                       "relative_humidity_uc_std",
                       "relative_humidity_uc_sys",
                       "relative_humidity_uc_tot"):
            if rh_key not in extracted_units:
                continue  # variable absent from the file
            rh_units = extracted_units[rh_key]
            if rh_units is None:
                raise ValueError(
                    f"Missing 'units' attribute for '{rh_key}': cannot "
                    f"determine whether RH is a percentage or a fraction.")
            rh_units_norm = str(rh_units).strip().lower()
            if rh_units_norm in RH_PERCENT_UNITS:
                extracted_data[rh_key] = extracted_data[rh_key] / 100.0
            elif rh_units_norm not in RH_FRACTION_UNITS:
                raise ValueError(
                    f"Unrecognised units '{rh_units}' for '{rh_key}': "
                    f"cannot determine whether RH is a percentage or a "
                    f"fraction.")

        # WVMR unit conversion (fraction -> ppmv).
        for wv_key in ("wvmr", "wvmr_uc_sys", "wvmr_uc_tot"):
            if wv_key not in extracted_units:
                continue  # variable absent from the file
            wv_units = extracted_units[wv_key]
            if wv_units is None:
                raise ValueError(
                    f"Missing 'units' attribute for '{wv_key}': cannot "
                    f"convert to ppmv.")
            wv_units_norm = str(wv_units).strip().lower()
            if wv_units_norm in WVMR_FRACTION_UNITS:
                extracted_data[wv_key] = extracted_data[wv_key] * 1.0e6
            elif wv_units_norm not in WVMR_PPMV_UNITS:
                raise ValueError(
                    f"Unrecognised units '{wv_units}' for '{wv_key}': "
                    f"cannot convert to ppmv.")

        df_profile = pd.DataFrame(extracted_data)
        df_profile["datetime"] = dt
        df_profile["station"] = station
        df_profile["source_file"] = os.path.basename(jar_file_path)
        df_profile["tropopause_altitude"] = tropopause_altitude
        df_profile[UNC_FORMAT_COLUMN] = (uncertainty_format
                                         if uncertainty_format else "")

        ds.close()
        try:
            nc4_dataset.close()
        except Exception:
            pass
        return df_profile
    except Exception as e:
        print(f"[ERROR] Failed to process NetCDF {jar_file_path}: {e}")
        return pd.DataFrame()


# ==========================================
# MAIN PROCESSING
# ==========================================
def expected_uncertainties_from_profile(profile):
    """
    Builds the `expected_uncertainties` dict {uncertainty key: bool} for one
    profile, from the file format of the profile (see
    QC_UNCERTAINTIES_BY_FORMAT, same rule as gruan_exporter.py). Returns
    None ("expect all", the evaluator's default) when the format is
    undetermined or not available.
    """
    if UNC_FORMAT_COLUMN not in profile.columns:
        return None
    fmt = profile[UNC_FORMAT_COLUMN].iloc[0]
    if not isinstance(fmt, str) or fmt not in QC_UNCERTAINTIES_BY_FORMAT:
        return None
    return dict(QC_UNCERTAINTIES_BY_FORMAT[fmt])


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

        profile = profile.sort_values(by="height").reset_index(drop=True)

        # -------------------------------------------------------------
        # Plausibility: level-by-level evaluation and NULLing
        # -------------------------------------------------------------
        (evaluated_levels, rejected, uncertainty_rejected,
         column_rejected) = evaluate_level_plausibility(
            profile,
            expected_uncertainties=expected_uncertainties_from_profile(
                profile))
        profile = apply_plausibility_nulling(profile, rejected,
                                             uncertainty_rejected,
                                             column_rejected)

        # Re-read arrays after NULLing for downstream checks.
        p_arr = profile["pressure"].to_numpy()
        z_arr = profile["height"].to_numpy()
        rh_arr = profile["relative_humidity"].to_numpy()
        t_arr = profile["temperature"].to_numpy()
        ws_arr = profile["wind_speed"].to_numpy()
        wd_arr = profile["wind_direction"].to_numpy()
        wvmr_arr = profile["wvmr"].to_numpy()

        # -------------------------------------------------------------
        # CBH / CTH / Pmin
        # -------------------------------------------------------------
        cbh_res = check_cloud_base(rh_arr, z_arr, p_arr)
        cth_res = check_cloud_top(rh_arr, z_arr, p_arr, t_arr)
        pmin, f_p10, f_p5 = check_pressure_flags(p_arr)

        # -------------------------------------------------------------
        # Per-variable plausibility flags
        # -------------------------------------------------------------
        # With no evaluable level (e.g. all heights unknown) nothing was
        # checked: the flags are NaN ("not evaluated"), not 1 ("plausible").
        any_level_evaluated = bool(np.any(evaluated_levels))
        plaus_flags = {
            var: (np.nan if (level_mask is None or not any_level_evaluated)
                  else not np.any(level_mask))
            for var, level_mask in rejected.items()
        }
        tropopause_altitude = profile["tropopause_altitude"].iloc[0]

        cbh_index = (int(cbh_res["index"])
                     if not np.isnan(cbh_res["index"]) else np.nan)
        cth_index = (int(cth_res["index"])
                     if not np.isnan(cth_res["index"]) else np.nan)

        rh_ice_arr = rh_over_ice(t_arr, rh_arr)

        if not np.isnan(cbh_index):
            cbh_temperature = t_arr[cbh_index]
            cbh_rh = rh_arr[cbh_index]
            cbh_rh_ice = rh_ice_arr[cbh_index]
        else:
            cbh_temperature = cbh_rh = cbh_rh_ice = np.nan

        if not np.isnan(cth_index):
            cth_temperature = t_arr[cth_index]
            cth_rh = rh_arr[cth_index]
            cth_rh_ice = rh_ice_arr[cth_index]
        else:
            cth_temperature = cth_rh = cth_rh_ice = np.nan

        # -------------------------------------------------------------
        # Completeness
        # -------------------------------------------------------------
        completeness_vars = {
            "Press": p_arr, "Temp": t_arr, "RH": rh_arr,
            "Wspeed": ws_arr, "Wdir": wd_arr, "WVMR": wvmr_arr,
        }
        completeness_record = {}
        for label, var_arr in completeness_vars.items():
            (n_tot_full, n_miss_full, compl_full, f_compl_full) = (
                check_variable_completeness(var_arr, z_arr))
            completeness_record[f"Ntotal_{label}"] = n_tot_full
            completeness_record[f"Completeness_{label}"] = (
                round(compl_full, 4) if not np.isnan(compl_full) else np.nan)
            completeness_record[f"FLAG_COMPLETENESS_{label}"] = f_compl_full

            if not np.isnan(tropopause_altitude):
                (n_tot_tropo, _, compl_tropo, f_compl_tropo) = (
                    check_variable_completeness(
                        var_arr, z_arr, zmax=tropopause_altitude))
            else:
                n_tot_tropo, compl_tropo, f_compl_tropo = 0, np.nan, "N/A"
            completeness_record[f"Ntotal_{label}_troposphere"] = n_tot_tropo
            completeness_record[f"Completeness_{label}_troposphere"] = (
                round(compl_tropo, 4)
                if not np.isnan(compl_tropo) else np.nan)
            completeness_record[
                f"FLAG_COMPLETENESS_{label}_troposphere"] = f_compl_tropo

            if not np.isnan(tropopause_altitude):
                (n_tot_strato, _, compl_strato, f_compl_strato) = (
                    check_variable_completeness(
                        var_arr, z_arr, zmin=tropopause_altitude))
            else:
                n_tot_strato, compl_strato, f_compl_strato = 0, np.nan, "N/A"
            completeness_record[f"Ntotal_{label}_stratosphere"] = n_tot_strato
            completeness_record[f"Completeness_{label}_stratosphere"] = (
                round(compl_strato, 4)
                if not np.isnan(compl_strato) else np.nan)
            completeness_record[
                f"FLAG_COMPLETENESS_{label}_stratosphere"] = f_compl_strato

        # -------------------------------------------------------------
        # Final record
        # -------------------------------------------------------------
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
            "Tropopause_Altitude (m)": tropopause_altitude,
        }
        # One plausibility flag per variable in PLAUSIBILITY_RULES.
        for var in PLAUSIBILITY_RULES:
            key = f"FLAG_PLAUSIBILITY_{var.upper()}"
            record[key] = as_binary_flag(plaus_flags.get(var))

        record.update(completeness_record)
        log_records.append(record)

    df_log = pd.DataFrame(log_records)
    df_log.to_csv(output_log_path, index=False, na_rep="NaN")
    print(f"[SUCCESS] QC complete. Log written to: {output_log_path}")
    generate_readme()


# ==========================================
# BATCH LOADING
# ==========================================
def load_all_profiles_from_directory(input_directory,
                                     file_pattern=JAR_FILE_PATTERN):
    """Loads every .jar archive matching `file_pattern` into one DataFrame."""
    search_path = os.path.join(input_directory, file_pattern)
    jar_files = sorted(glob.glob(search_path))

    if not jar_files:
        print(f"[WARNING] No jar archives found in '{input_directory}' "
              f"matching pattern '{file_pattern}'.")
        return pd.DataFrame()

    print(f"[INFO] Found {len(jar_files)} jar archive(s) in "
          f"'{input_directory}'.")

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


# ==========================================
# MAIN
# ==========================================
if __name__ == "__main__":
    print("[INFO] Starting GRUAN Quality Control Pipeline...")

    all_profiles_df = load_all_profiles_from_directory(INPUT_DIRECTORY,
                                                       JAR_FILE_PATTERN)

    if all_profiles_df is not None and not all_profiles_df.empty:
        process_gruan_profiles(data_source=all_profiles_df,
                               output_log_path=OUTPUT_LOG_PATH)
    else:
        print("[ERROR] No valid jar archives were successfully loaded. "
              "Execution aborted.")
        print("[INFO] Generating documentation file anyway...")
        generate_readme()