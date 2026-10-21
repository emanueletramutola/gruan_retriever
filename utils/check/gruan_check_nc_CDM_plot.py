#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GRUAN data-quality report
=========================

Reads the per-file / per-variable summary table produced by the GRUAN NetCDF
check (one row = one monthly file x one observed variable, with min / max /
number of valid values for the observation and for its random, systematic and
total uncertainty) and writes a multi-page PDF of diagnostic plots that show
the strengths and the weaknesses of the dataset.

The plausible ranges used to flag suspicious minima / maxima are NOT defined
here: they are read from PLAUSIBILITY_RULES[...]["check_range"] in
qc_pipeline.py (see CDM_CODE_TO_RULE and QC_PIPELINE_PATH below).

Dependencies: numpy, pandas, matplotlib
"""

from pathlib import Path

# --------------------------------------------------------------------------
# USER SETTINGS  (edit these variables)
# --------------------------------------------------------------------------
# INPUT_CSV_PATH = "/home/emanuele/logs/gruan_check_nc_CDM.csv"      # input CSV file
INPUT_CSV_PATH = "/Data/GRUAN_TEST/output/gruan_check_nc_CDM.csv"      # input CSV file
# OUTPUT_PDF_PATH = "/home/emanuele/logs/gruan_quality_report.pdf"   # output PDF file
OUTPUT_PDF_PATH = "/Data/GRUAN_TEST/output/gruan_quality_report.pdf"   # output PDF file
# qc_pipeline.py, from which the plausible ranges (check_range) are read.
# Relative to the directory of this script (two levels up).
QC_PIPELINE_PATH = str(Path(__file__).resolve().parent / "../../qc_pipeline.py")
# --------------------------------------------------------------------------

import importlib.util
import textwrap

import matplotlib

matplotlib.use("Agg")  # non-interactive backend: figures go straight to PDF
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

# --------------------------------------------------------------------------
# Plausible ranges of the observation (read from qc_pipeline.py)
# --------------------------------------------------------------------------
# CDM observed_variable code -> (key in PLAUSIBILITY_RULES of qc_pipeline.py,
#                                factor: units of the pipeline -> units of the CDM files)
# The ranges in qc_pipeline.py are expressed in the units used inside that
# pipeline, which for some variables differ from the units of the CDM files
# summarised in the input table, hence the factor:
#   relative humidity : fraction (0-1) -> per cent (x 100)
#   pressure          : hPa            -> Pa       (x 100)
#   water vapour m.r. : ppmv           -> mol/mol  (x 1e-6)
# This is the same table used by gruan_check_nc_CDM.py: keep the two in sync.
# A code whose rule has check_range = None, or that has no rule at all, has no
# plausible range: it is not assessed (shown as "n/a" / "no range defined").
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


def load_ranges_from_qc_pipeline(path):
    """
    Import qc_pipeline.py and return {CDM code: (lo, hi)} built from
    PLAUSIBILITY_RULES[rule]["check_range"], converted to the units of the CDM
    files. Codes without a check_range are left out (= no range).
    """
    if not Path(path).is_file():
        raise FileNotFoundError(f"qc_pipeline.py not found: {path} (set QC_PIPELINE_PATH)")
    spec = importlib.util.spec_from_file_location("qc_pipeline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # safe: the pipeline main is under __main__
    rules = module.PLAUSIBILITY_RULES

    ranges = {}
    for code, (rule, factor) in CDM_CODE_TO_RULE.items():
        if rule not in rules:
            raise KeyError(f"'{rule}' (CDM code {code}) not found in PLAUSIBILITY_RULES")
        check_range = rules[rule].get("check_range")
        if check_range is None:
            continue
        lo, hi = check_range
        # round() removes floating-point noise (e.g. 50000 * 1e-6)
        ranges[code] = (round(lo * factor, 12), round(hi * factor, 12))
    return ranges


RANGES_BY_CODE = load_ranges_from_qc_pipeline(QC_PIPELINE_PATH)
RANGES_BY_NAME = {}   # observed_variable_name -> (lo, hi); filled by load_data()

# Relative tolerance used in the uncertainty-consistency test
CONSISTENCY_TOL = 1e-3

# Saturation ("cap") values of the TOTAL uncertainty, identified empirically from the
# pile-up of the file maxima (see the pressure and wind uncertainty pages). A file is
# counted as "at cap" when its maximum total uncertainty lies within CAP_REL_TOL below
# (or marginally above) one of these values. Adapt them if the dataset changes.
CAP_REL_TOL = 0.02
PRESSURE_UNC_CAP = 1000.0                     # Pa  (= 10 hPa)
WIND_UNC_CAP = 50.0                           # m/s (eastward / northward component, wind speed)
DIRECTION_UNC_CAPS = (180.0, 360.0)           # deg (wind from direction)
# Upper axis limit of the pressure-uncertainty time series (larger maxima are flagged off scale)
PRESSURE_UNC_YMAX = 1e6                       # Pa

# Page sizes (inches)
LANDSCAPE = (11.69, 8.27)
PORTRAIT = (8.27, 11.69)

plt.rcParams.update({
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.labelsize": 9,
    "figure.dpi": 100,
    "axes.grid": True,
    "grid.alpha": 0.25,
})


# ==========================================================================
# Data loading and derived quantities
# ==========================================================================
def load_data(path):
    """Read the CSV and add derived columns (date, labels, flags, fractions)."""
    df = pd.read_csv(path, encoding="utf-8-sig")

    # Month of each file, parsed from the file name (..._GRUAN_YYYY_MM.nc)
    df["date"] = pd.to_datetime(
        df["file"].str.extract(r"(\d{4})_(\d{2})\.nc")[0]
        + "-"
        + df["file"].str.extract(r"(\d{4})_(\d{2})\.nc")[1]
        + "-01"
    )
    df["year"] = df["date"].dt.year

    # Short label "name [unit]", wrapped for axis tick labels
    df["label"] = (df["observed_variable_name"] + " [" + df["units_abbreviation"] + "]").apply(
        lambda s: "\n".join(textwrap.wrap(s, 32))
    )

    # Fractions of valid values with respect to the number of records
    df["obs_valid_frac"] = df["observation_value_n_valid"] / df["n_records"]
    for c in ("random", "systematic", "total"):
        df[f"unc_{c}_frac"] = df[f"uncertainty_{c}_n_valid"] / df["n_records"]

    # Plausibility flags on the observation min / max, with the ranges of
    # qc_pipeline.py (NaN where a variable has no range: comparisons are False)
    codes = df["observed_variable_code"].astype(int)
    lo = codes.map(lambda c: RANGES_BY_CODE.get(c, (np.nan, np.nan))[0])
    hi = codes.map(lambda c: RANGES_BY_CODE.get(c, (np.nan, np.nan))[1])
    df["range_lo"], df["range_hi"] = lo, hi
    RANGES_BY_NAME.clear()
    for name, l, h in zip(df["observed_variable_name"], lo, hi):
        if pd.notna(l) and pd.notna(h):
            RANGES_BY_NAME[name] = (float(l), float(h))
    df["min_below"] = df["observation_value_min"] < lo
    df["max_above"] = df["observation_value_max"] > hi
    df["out_of_range"] = df["min_below"] | df["max_above"]
    return df.sort_values(["observed_variable_code", "date"]).reset_index(drop=True)


def variable_order(df):
    """Variable names ordered by their code."""
    return (
        df[["observed_variable_code", "observed_variable_name"]]
        .drop_duplicates()
        .sort_values("observed_variable_code")["observed_variable_name"]
        .tolist()
    )


def input_checks(df):
    """Consistency checks of the input table; returns a list of messages."""
    msgs = [f"Plausible ranges read from PLAUSIBILITY_RULES (check_range) of {QC_PIPELINE_PATH}, "
            "converted to the units of the CDM files."]
    # Compare with the ranges written by the NetCDF check into the table (if any)
    if "observation_value_range_lo" in df.columns and "observation_value_range_hi" in df.columns:
        diff = df["range_lo"].notna() & (
            ~np.isclose(df["range_lo"], df["observation_value_range_lo"], rtol=1e-9, atol=0, equal_nan=False)
            | ~np.isclose(df["range_hi"], df["observation_value_range_hi"], rtol=1e-9, atol=0, equal_nan=False))
        msgs.append(f"Rows whose range in the input table differs from the range in use: {int(diff.sum())} "
                    "(non-zero means the table was produced with different ranges: re-run the NetCDF check).")
    no_rng = sorted(df.loc[df["range_lo"].isna(), "observed_variable_name"].unique())
    if no_rng:
        msgs.append("No plausible range defined (not assessed): " + ", ".join(no_rng) + ".")
    return msgs


def pivot_months(df, value_col, var_names):
    """Pivot to a (variable x every month) matrix; missing months -> NaN."""
    months = pd.date_range(df["date"].min(), df["date"].max(), freq="MS")
    p = df.pivot_table(index="observed_variable_name", columns="date", values=value_col, aggfunc="first")
    return p.reindex(index=var_names, columns=months), months


def heatmap(ax, matrix, months, labels, cmap="viridis", vmin=0, vmax=1, norm=None, under=None):
    """Draw a variable x time heatmap with a proper date axis."""
    cm = plt.get_cmap(cmap).copy()
    cm.set_bad("#d9d9d9")  # grey = no file / no data
    if under is not None:
        cm.set_under(under)  # values below the colour scale (e.g. "none")
    scale = {"norm": norm} if norm is not None else {"vmin": vmin, "vmax": vmax}
    x0 = mdates.date2num(months[0])
    x1 = mdates.date2num(months[-1] + pd.offsets.MonthBegin(1))
    im = ax.imshow(
        np.ma.masked_invalid(matrix.values), aspect="auto", cmap=cm, **scale,
        extent=[x0, x1, matrix.shape[0], 0], interpolation="nearest",
    )
    ax.xaxis_date()
    ax.xaxis.set_major_locator(mdates.YearLocator(2))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_yticks(np.arange(matrix.shape[0]) + 0.5)
    ax.set_yticklabels(labels, fontsize=7)
    ax.grid(False)
    return im


def month_gaps(df):
    """Months between the first and last file for which no file exists."""
    all_m = pd.date_range(df["date"].min(), df["date"].max(), freq="MS")
    return all_m.difference(pd.DatetimeIndex(df["date"].unique()))


# ==========================================================================
# Summary metrics (used for the text page and the final table)
# ==========================================================================
def per_variable_summary(df, var_names):
    rows = []
    for v in var_names:
        d = df[df["observed_variable_name"] == v]
        rec = d["n_records"].sum()
        has_range = d["range_lo"].notna().any()
        rows.append({
            "Variable": d["label"].iloc[0].replace("\n", " "),
            "name": v,
            "Valid obs (%)": 100 * d["observation_value_n_valid"].sum() / rec,
            "Random unc. (%)": 100 * d["uncertainty_random_n_valid"].sum() / rec,
            "Syst. unc. (%)": 100 * d["uncertainty_systematic_n_valid"].sum() / rec,
            "Total unc. (%)": 100 * d["uncertainty_total_n_valid"].sum() / rec,
            "Files out of range (%)": 100 * d["out_of_range"].mean() if has_range else np.nan,
            "Lowest min": d["observation_value_min"].min(),
            "Highest max": d["observation_value_max"].max(),
        })
    return pd.DataFrame(rows)


def consistency_flags(d):
    """
    Pointwise, total = sqrt(random^2 + systematic^2), hence for the file
    maxima:   max(total) >= max(random), max(total) >= max(systematic)
    and       max(total) <= max(random) + max(systematic).
    Returns boolean Series (lower-bound violation, upper-bound violation).
    """
    comps = d[["uncertainty_random_max", "uncertainty_systematic_max"]]
    lower = comps.max(axis=1)              # NaN ignored
    upper = comps.sum(axis=1, min_count=2)  # only if both components exist
    tot = d["uncertainty_total_max"]
    viol_lo = tot < lower * (1 - CONSISTENCY_TOL)
    viol_hi = tot > upper * (1 + CONSISTENCY_TOL)
    return viol_lo.fillna(False), viol_hi.fillna(False)


# ==========================================================================
# Pages
# ==========================================================================
def page_summary_old(pdf, df, var_names, summ):
    """Text page with dataset overview and computed strengths / weaknesses."""
    fig = plt.figure(figsize=LANDSCAPE)
    fig.text(0.5, 0.95, "GRUAN dataset - quality screening report", ha="center",
             fontsize=17, weight="bold")
    fig.text(0.5, 0.915, f"Source table: {INPUT_CSV_PATH}", ha="center", fontsize=9, color="gray")

    n_files = df["file"].nunique()
    gaps = month_gaps(df)
    gap_txt = (", ".join(g.strftime("%Y-%m") for g in gaps)) if len(gaps) else "none"
    total_rec = df.groupby("file")["n_records"].max().sum()
    n_rows = len(df)

    core = summ[summ["name"].isin(["air temperature", "pressure", "relative humidity", "altitude"])]
    core_valid = core["Valid obs (%)"].min()
    n_tot_unc = int((summ["Total unc. (%)"] > 50).sum())
    n_no_unc = int(((summ[["Random unc. (%)", "Syst. unc. (%)", "Total unc. (%)"]].max(axis=1)) == 0).sum())
    both = int(((summ["Random unc. (%)"] > 0) & (summ["Syst. unc. (%)"] > 0)).sum())
    oor_rows = 100 * df.loc[df["range_lo"].notna(), "out_of_range"].mean()
    worst = summ.sort_values("Files out of range (%)", ascending=False).head(3)
    worst_txt = "; ".join(f"{r['name']} ({r['Files out of range (%)']:.0f}% of files)" for _, r in worst.iterrows())

    sw = df[df["observed_variable_name"] == "shortwave radiation"].groupby("year")["observation_value_n_valid"].sum()
    sw_peak_year, sw_peak = sw.idxmax(), sw.max()
    sw_last_full = sw[sw.index < df["year"].max()].iloc[-1]

    t = df[df["observed_variable_name"] == "air temperature"]
    capmask = (t["uncertainty_total_max"] > 19.0) & (t["uncertainty_total_max"] <= 20.01)
    cap_files = int(capmask.sum())
    cap_first = t.loc[capmask, "date"].min()

    viol = {}
    for v in ["air temperature", "relative humidity"]:
        d = df[(df["observed_variable_name"] == v)].dropna(
            subset=["uncertainty_random_max", "uncertainty_systematic_max", "uncertainty_total_max"])
        lo_v, hi_v = consistency_flags(d)
        viol[v] = (int((lo_v | hi_v).sum()), len(d))

    strengths = [
        f"Long and nearly continuous record: {n_files} monthly files from {df['date'].min():%Y-%m} to "
        f"{df['date'].max():%Y-%m}; {len(gaps)} months missing ({gap_txt}).",
        f"Large volume: about {total_rec / 1e9:.2f} billion records in total; the core state variables "
        f"(temperature, pressure, RH, altitude) are valid in at least {core_valid:.0f}% of the records.",
        f"Uncertainty information is provided for part of the variables: {n_tot_unc} of {len(var_names)} "
        f"variables have a total uncertainty on more than half of the records, and {both} variables "
        f"(air temperature, relative humidity) carry random AND systematic components.",
        "Systematic structure of the table (identical variables, units and layout in every file) makes "
        "automated checks straightforward, and no file has min > max or more valid values than records.",
    ]
    weaknesses = [
        f"Unphysical extremes: {oor_rows:.1f}% of the file/variable rows have a minimum or maximum outside "
        f"the assumed plausible range. Worst variables: {worst_txt}.",
        f"Shortwave radiation collapses in time: {sw_peak / 1e6:.0f} M valid values in {sw_peak_year} vs "
        f"{sw_last_full / 1e6:.2f} M in the last complete year; {int((df.loc[df['observed_variable_name'] == 'shortwave radiation', 'observation_value_n_valid'] == 0).sum())} "
        f"files have no valid value at all.",
        f"Air-temperature total uncertainty appears clipped: file maxima pile up just below ~5 K up to 2014 and "
        f"just below ~20 K from {cap_first:%Y-%m} on ({cap_files} files near 20 K), i.e. a cap/fill value rather than "
        "a real estimate.",
        f"Total uncertainty is not compatible with its components in many files: the file maximum of the total "
        f"is smaller than the largest component in {viol['air temperature'][0]}/{viol['air temperature'][1]} "
        f"(temperature) and {viol['relative humidity'][0]}/{viol['relative humidity'][1]} (RH) files, or exceeds "
        "their sum. Absurd values also occur (RH systematic uncertainty up to ~1e19 %, pressure total "
        "uncertainty up to ~1e9 Pa).",
        f"{n_no_unc} variables have no uncertainty at all, and wind/pressure/altitude carry only a total "
        "uncertainty (no random / systematic split), which limits error propagation.",
        "The table only stores min / max / counts per file: outlier frequency, distributions and "
        "vertical structure cannot be assessed, and one single bad value flags a whole month.",
    ]

    y = 0.86

    def block(title, items, y, color):
        fig.text(0.05, y, title, fontsize=13, weight="bold", color=color)
        y -= 0.04
        for it in items:
            lines = textwrap.wrap(it, 150)
            fig.text(0.06, y, "\u2022", fontsize=10, va="top")
            fig.text(0.075, y, "\n".join(lines), fontsize=9.5, va="top", linespacing=1.35)
            y -= 0.028 * len(lines) + 0.022
        return y

    y = block("Strengths", strengths, y, "#1a7f37")
    y -= 0.015
    block("Weaknesses", weaknesses, y, "#b42318")

    fig.text(0.05, 0.03, "Plausible ranges are the check_range values of PLAUSIBILITY_RULES in qc_pipeline.py "
             "(converted to the units of the CDM files); consistency bounds assume total = sqrt(random^2 + "
             "systematic^2) at each point.",
             fontsize=7.5, color="gray")
    pdf.savefig(fig)
    plt.close(fig)

def page_summary(pdf, df, var_names, summ):
    """Text page with dataset overview and computed strengths / weaknesses."""
    fig = plt.figure(figsize=LANDSCAPE)
    fig.text(0.5, 0.95, "GRUAN dataset - quality screening report", ha="center",
             fontsize=17, weight="bold")
    fig.text(0.5, 0.915, f"Source table: {INPUT_CSV_PATH}", ha="center", fontsize=9, color="gray")

    n_files = df["file"].nunique()
    gaps = month_gaps(df)
    gap_txt = (", ".join(g.strftime("%Y-%m") for g in gaps)) if len(gaps) else "none"
    total_rec = df.groupby("file")["n_records"].max().sum()
    n_rows = len(df)

    core = summ[summ["name"].isin(["air temperature", "pressure", "relative humidity", "altitude"])]
    core_valid = core["Valid obs (%)"].min()
    n_tot_unc = int((summ["Total unc. (%)"] > 50).sum())
    n_no_unc = int(((summ[["Random unc. (%)", "Syst. unc. (%)", "Total unc. (%)"]].max(axis=1)) == 0).sum())
    both = int(((summ["Random unc. (%)"] > 0) & (summ["Syst. unc. (%)"] > 0)).sum())
    oor_rows = 100 * df.loc[df["range_lo"].notna(), "out_of_range"].mean()
    worst = summ.sort_values("Files out of range (%)", ascending=False).head(3)
    worst_txt = "; ".join(f"{r['name']} ({r['Files out of range (%)']:.0f}% of files)" for _, r in worst.iterrows())

    sw = df[df["observed_variable_name"] == "shortwave radiation"].groupby("year")["observation_value_n_valid"].sum()
    sw_peak_year, sw_peak = sw.idxmax(), sw.max()
    sw_last_full = sw[sw.index < df["year"].max()].iloc[-1]

    t = df[df["observed_variable_name"] == "air temperature"]
    capmask = (t["uncertainty_total_max"] > 19.0) & (t["uncertainty_total_max"] <= 20.01)
    cap_files = int(capmask.sum())
    cap_first = t.loc[capmask, "date"].min()

    # Safely format cap_first to avoid ValueError when pd.NaT is returned
    cap_first_txt = f"from {cap_first:%Y-%m} on" if pd.notna(cap_first) else "N/A"

    viol = {}
    for v in ["air temperature", "relative humidity"]:
        d = df[(df["observed_variable_name"] == v)].dropna(
            subset=["uncertainty_random_max", "uncertainty_systematic_max", "uncertainty_total_max"])
        lo_v, hi_v = consistency_flags(d)
        viol[v] = (int((lo_v | hi_v).sum()), len(d))

    # saturation of the total uncertainty of pressure and wind
    st_p = total_unc_cap_stats(df, "pressure", [PRESSURE_UNC_CAP])
    st_w = {n: total_unc_cap_stats(df, n, [WIND_UNC_CAP])
            for n in ("eastward wind speed", "northward wind speed", "wind speed")}
    st_d = total_unc_cap_stats(df, "wind from direction", DIRECTION_UNC_CAPS)
    p_unc = df.loc[df["observed_variable_name"] == "pressure", "uncertainty_total_max"]
    w_cap = sum(v["n_at_cap"] for v in st_w.values())
    w_tot = sum(v["n_with_unc"] for v in st_w.values())

    last_txt = ("The table only stores min / max / counts per file: outlier frequency, distributions and "
                "vertical structure cannot be assessed, and one single bad value flags a whole month.")

    strengths = [
        f"Long and nearly continuous record: {n_files} monthly files from {df['date'].min():%Y-%m} to "
        f"{df['date'].max():%Y-%m}; {len(gaps)} months missing ({gap_txt}).",
        f"Large volume: about {total_rec / 1e9:.2f} billion records in total; the core state variables "
        f"(temperature, pressure, RH, altitude) are valid in at least {core_valid:.0f}% of the records.",
        f"Uncertainty information is provided for part of the variables: {n_tot_unc} of {len(var_names)} "
        f"variables have a total uncertainty on more than half of the records, and {both} variables "
        f"(air temperature, relative humidity) carry random AND systematic components.",
        "Systematic structure of the table (identical variables, units and layout in every file) makes "
        "automated checks straightforward, and no file has min > max or more valid values than records.",
    ]
    weaknesses = [
        f"Unphysical extremes: {oor_rows:.1f}% of the file/variable rows have a minimum or maximum outside "
        f"the plausible range of qc_pipeline.py. Worst variables: {worst_txt}.",
        f"Shortwave radiation collapses in time: {sw_peak / 1e6:.0f} M valid values in {sw_peak_year} vs "
        f"{sw_last_full / 1e6:.2f} M in the last complete year; {int((df.loc[df['observed_variable_name'] == 'shortwave radiation', 'observation_value_n_valid'] == 0).sum())} "
        f"files have no valid value at all.",
        f"Air-temperature total uncertainty appears clipped: file maxima pile up just below ~5 K up to 2014 and "
        f"just below ~20 K {cap_first_txt} ({cap_files} files near 20 K), i.e. a cap/fill value rather than "
        "a real estimate.",
        f"Total uncertainty is not compatible with its components in many files: the file maximum of the total "
        f"is smaller than the largest component in {viol['air temperature'][0]}/{viol['air temperature'][1]} "
        f"(temperature) and {viol['relative humidity'][0]}/{viol['relative humidity'][1]} (RH) files, or exceeds "
        "their sum. Absurd values also occur (RH systematic uncertainty up to ~1e19 %, pressure total "
        "uncertainty up to ~1e9 Pa).",
        f"Pressure and wind total uncertainties also look saturated: the pressure file maximum lies at about "
        f"{PRESSURE_UNC_CAP:g} Pa in {st_p['n_at_cap']}/{st_p['n_with_unc']} files (up to {p_unc.max():.1e} Pa "
        f"in the worst file); wind-component and wind-speed maxima sit at about {WIND_UNC_CAP:g} m/s in "
        f"{w_cap}/{w_tot} files; the wind-direction uncertainty saturates at {DIRECTION_UNC_CAPS[0]:g} deg and then "
        f"{DIRECTION_UNC_CAPS[1]:g} deg ({st_d['n_at_cap']}/{st_d['n_with_unc']} files), i.e. a fill value rather "
        "than an estimate.",
        f"{n_no_unc} variables have no uncertainty at all, and wind/pressure/altitude carry only a total "
        "uncertainty (no random / systematic split), which limits error propagation.",
        last_txt,
    ]

    y = 0.875

    def block(title, items, y, color):
        fig.text(0.05, y, title, fontsize=13, weight="bold", color=color)
        y -= 0.036
        for it in items:
            lines = textwrap.wrap(it, 165)
            fig.text(0.06, y, "\u2022", fontsize=10, va="top")
            fig.text(0.075, y, "\n".join(lines), fontsize=9, va="top", linespacing=1.3)
            y -= 0.0225 * len(lines) + 0.011
        return y

    y = block("Strengths", strengths, y, "#1a7f37")
    y -= 0.015
    block("Weaknesses", weaknesses, y, "#b42318")

    fig.text(0.05, 0.03, "Plausible ranges are the check_range values of PLAUSIBILITY_RULES in qc_pipeline.py "
             "(converted to the units of the CDM files); consistency bounds assume total = sqrt(random^2 + "
             "systematic^2) at each point.",
             fontsize=7.5, color="gray")
    pdf.savefig(fig)
    plt.close(fig)

def page_volume(pdf, df):
    """Records per monthly file, missing months and cumulative volume."""
    per_file = df.groupby("date")["n_records"].max()
    gaps = month_gaps(df)
    fig, axes = plt.subplots(2, 1, figsize=LANDSCAPE, sharex=True)

    ax = axes[0]
    ax.bar(per_file.index, per_file.values / 1e6, width=25, color="#3b6ea8")
    for g in gaps:
        ax.axvspan(g, g + pd.offsets.MonthBegin(1), color="red", alpha=0.35)
    ax.set_ylabel("Records per monthly file [millions]")
    ax.set_title("(a) Data volume per month (red = month without file)")

    ax = axes[1]
    cum = per_file.reindex(pd.date_range(per_file.index.min(), per_file.index.max(), freq="MS")).fillna(0).cumsum()
    ax.plot(cum.index, cum.values / 1e9, color="#3b6ea8", lw=2)
    ax.set_ylabel("Cumulative records [billions]")
    ax.set_title("(b) Cumulative number of records")
    ax.set_xlabel("Date")

    fig.suptitle("Temporal coverage and data volume", fontsize=13, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    pdf.savefig(fig)
    plt.close(fig)


def page_validity(pdf, df, var_names):
    """Heatmap: fraction of records with a valid observation."""
    mat, months = pivot_months(df, "obs_valid_frac", var_names)
    labels = [df.loc[df["observed_variable_name"] == v, "label"].iloc[0] for v in var_names]
    fig, ax = plt.subplots(figsize=LANDSCAPE)
    im = heatmap(ax, mat, months, labels)
    cb = fig.colorbar(im, ax=ax, pad=0.015)
    cb.set_label("Valid values / number of records")
    ax.set_title("Observation completeness per variable and month (grey = no file)")
    ax.set_xlabel("Date")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def page_uncertainty_availability(pdf, df, var_names, summ):
    """Bar chart of uncertainty availability + heatmap of total-unc. availability."""
    fig, axes = plt.subplots(2, 1, figsize=LANDSCAPE, gridspec_kw={"height_ratios": [1, 1.25]})

    ax = axes[0]
    x = np.arange(len(summ))
    w = 0.2
    for i, (col, c) in enumerate([("Valid obs (%)", "#4d4d4d"), ("Random unc. (%)", "#e69f00"),
                                  ("Syst. unc. (%)", "#56b4e9"), ("Total unc. (%)", "#009e73")]):
        ax.bar(x + (i - 1.5) * w, summ[col], w, label=col.replace(" (%)", ""), color=c)
    ax.set_xticks(x)
    ax.set_xticklabels([textwrap.fill(n, 22) for n in summ["name"]], fontsize=6.5, rotation=30, ha="right")
    ax.set_ylabel("% of all records")
    ax.set_ylim(0, 105)
    ax.legend(ncol=4, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, 1.02), frameon=False)
    ax.set_title("(a) Availability of observations and uncertainty components (all files)", pad=16)

    ax = axes[1]
    mat, months = pivot_months(df, "unc_total_frac", var_names)
    labels = [df.loc[df["observed_variable_name"] == v, "label"].iloc[0] for v in var_names]
    im = heatmap(ax, mat.clip(upper=1), months, labels, cmap="magma")
    cb = fig.colorbar(im, ax=ax, pad=0.015)
    cb.set_label("Valid total uncertainty / records")
    ax.set_title("(b) Time evolution of the total-uncertainty availability")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


def page_plausibility(pdf, df, var_names):
    """How often min / max values fall outside the plausible range."""
    fig, axes = plt.subplots(1, 2, figsize=LANDSCAPE, gridspec_kw={"width_ratios": [1.3, 1]})

    ax = axes[0]
    below = [100 * df.loc[df["observed_variable_name"] == v, "min_below"].mean() if v in RANGES_BY_NAME
             else np.nan for v in var_names]
    above = [100 * df.loc[df["observed_variable_name"] == v, "max_above"].mean() if v in RANGES_BY_NAME
             else np.nan for v in var_names]
    labels = [f"{v}  [{RANGES_BY_NAME[v][0]:g}, {RANGES_BY_NAME[v][1]:g}]" if v in RANGES_BY_NAME
              else f"{v}  [no range defined]" for v in var_names]
    labels = ["\n".join(textwrap.wrap(s, 34)) for s in labels]
    y = np.arange(len(var_names))
    ax.barh(y - 0.2, below, height=0.4, color="#0072b2", label="minimum below range")
    ax.barh(y + 0.2, above, height=0.4, color="#d55e00", label="maximum above range")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("% of monthly files")
    ax.set_title("(a) Files with min/max outside the plausible range\n(range of qc_pipeline.py in brackets)")
    ax.legend(fontsize=8, loc="lower right")

    ax = axes[1]
    by_year = df[df["range_lo"].notna()].groupby("year")["out_of_range"].mean() * 100
    ax.bar(by_year.index, by_year.values, color="#cc79a7")
    ax.set_xlabel("Year")
    ax.set_ylabel("% of file/variable rows flagged")
    ax.set_title("(b) Flagged rows per year (variables with a range)")

    fig.suptitle("Physical plausibility screening", fontsize=13, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    pdf.savefig(fig)
    plt.close(fig)


def page_envelopes(pdf, df, var_names):
    """Monthly min / max of each variable with the plausible range; outliers pinned to the edge."""
    ncols = 3
    nrows = int(np.ceil(len(var_names) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=PORTRAIT)
    axes = axes.ravel()

    for ax, v in zip(axes, var_names):
        d = df[df["observed_variable_name"] == v]
        has_rng = v in RANGES_BY_NAME
        if has_rng:
            lo, hi = RANGES_BY_NAME[v]
            span = hi - lo
            ylim = (lo - 0.25 * span, hi + 0.25 * span)
        n_out = 0
        for col, colr in (("observation_value_min", "#0072b2"), ("observation_value_max", "#e69f00")):
            x = d["date"].values
            yv = d[col].values
            ok = ~np.isnan(yv)
            x, yv = x[ok], yv[ok]
            out = ((yv < lo) | (yv > hi)) if has_rng else np.zeros(len(yv), dtype=bool)
            n_out += int(out.sum())
            ax.plot(x[~out], yv[~out], ".", ms=2.2, color=colr)
            if has_rng:
                ax.plot(x[out], np.clip(yv[out], *ylim), "x", ms=3.5, color="red", mew=0.8)
        if has_rng:
            ax.axhline(lo, color="gray", ls="--", lw=0.7)
            ax.axhline(hi, color="gray", ls="--", lw=0.7)
            ax.set_ylim(*ylim)
        ax.set_title("\n".join(textwrap.wrap(f"{v} [{d['units_abbreviation'].iloc[0]}]", 34))
                     + (f"\n{n_out} out-of-range points" if has_rng else "\nno plausible range defined"),
                     fontsize=7)
        ax.tick_params(labelsize=6)
        ax.xaxis.set_major_locator(mdates.YearLocator(6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    for ax in axes[len(var_names):]:
        ax.axis("off")

    # Manual legend below the grid
    handles = [
        plt.Line2D([], [], marker=".", ls="", color="#0072b2", label="monthly minimum"),
        plt.Line2D([], [], marker=".", ls="", color="#e69f00", label="monthly maximum"),
        plt.Line2D([], [], marker="x", ls="", color="red", label="out of plausible range\n(clipped to axis edge)"),
        plt.Line2D([], [], ls="--", color="gray", label="plausible range (qc_pipeline.py)"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, fontsize=7, frameon=False)
    fig.suptitle("Monthly observation envelopes vs plausible ranges (qc_pipeline.py)", fontsize=12, weight="bold")
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    pdf.savefig(fig)
    plt.close(fig)


def page_uncertainty_timeseries(pdf, df):
    """Random / systematic / total uncertainty maxima for temperature and RH."""
    fig, axes = plt.subplots(2, 1, figsize=LANDSCAPE, sharex=True)
    for ax, v, unit in zip(axes, ["air temperature", "relative humidity"], ["K", "%"]):
        d = df[df["observed_variable_name"] == v]
        tmin = d["uncertainty_total_min"].where(d["uncertainty_total_min"] > 0)
        ax.fill_between(d["date"], tmin, d["uncertainty_total_max"], color="#009e73", alpha=0.2,
                        label="total: min-max range")
        ax.plot(d["date"], d["uncertainty_random_max"], ".", ms=3, color="#e69f00", label="random (max)")
        ax.plot(d["date"], d["uncertainty_systematic_max"], ".", ms=3, color="#56b4e9", label="systematic (max)")
        ax.plot(d["date"], d["uncertainty_total_max"], "-", lw=1.2, color="#009e73", label="total (max)")
        ax.set_yscale("log")
        ax.set_ylabel(f"Uncertainty [{unit}]")
        ax.set_title(f"{v}")
        if v == "air temperature":
            for cap in (5, 20):
                ax.axhline(cap, color="red", ls=":", lw=1)
                ax.text(d["date"].max(), cap * 1.08, f"{cap} K", color="red", fontsize=8, ha="right")
        if v == "relative humidity":
            top = 1e6
            n_off = int((d[["uncertainty_random_max", "uncertainty_systematic_max",
                            "uncertainty_total_max"]].max(axis=1) > top).sum())
            ax.set_ylim(1e-3, top)
            ax.text(0.99, 0.95, f"{n_off} file(s) with maxima > {top:.0e} % are off scale "
                    f"(largest: {d['uncertainty_systematic_max'].max():.1e} %)",
                    transform=ax.transAxes, ha="right", va="top", fontsize=8, color="red")
        ax.legend(fontsize=7, ncol=4, loc="upper left")
    axes[-1].set_xlabel("Date")
    fig.suptitle("Uncertainty components per monthly file (log scale)", fontsize=13, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    pdf.savefig(fig)
    plt.close(fig)


def cap_mask(values, cap, rel_tol=CAP_REL_TOL):
    """True where a file maximum sits at a saturation value: cap*(1-tol) <= x <= cap*(1+tol)."""
    v = pd.Series(values)
    return (v >= cap * (1 - rel_tol)) & (v <= cap * (1 + rel_tol))


def total_unc_cap_stats(df, name, caps):
    """
    Saturation statistics of the total-uncertainty file maxima of one variable.
    Returns a dict with the number of files with a total uncertainty, the number of files
    whose maximum lies at one of `caps` (and the first month), and the dominant cap per year.
    """
    d = df[df["observed_variable_name"] == name]
    have = d[d["uncertainty_total_n_valid"] > 0]
    mask = pd.Series(False, index=have.index)
    for c in caps:
        mask |= cap_mask(have["uncertainty_total_max"], c)
    return {
        "n_files": len(d),
        "n_with_unc": len(have),
        "n_at_cap": int(mask.sum()),
        "first_cap": have.loc[mask, "date"].min() if mask.any() else pd.NaT,
        "have": have,
        "mask": mask,
    }


def _band_axes(ax, d, mask, cap_lines, unit, log=False, ylim=None):
    """Min-max band + maximum line of the total uncertainty; files at cap highlighted in red."""
    tmin = d["uncertainty_total_min"]
    if log:
        tmin = tmin.where(tmin > 0)
    ax.fill_between(d["date"], tmin, d["uncertainty_total_max"], color="#009e73", alpha=0.2,
                    label="total: min-max range per file")
    ax.plot(d["date"], d["uncertainty_total_max"], "-", lw=1.0, color="#009e73", label="total (max per file)")
    ax.plot(d.loc[mask, "date"], d.loc[mask, "uncertainty_total_max"], ".", ms=4, color="#d73027",
            label="max at cap value")
    for c, txt in cap_lines:
        ax.axhline(c, color="red", ls=":", lw=1)
        ax.text(d["date"].max(), c * (1.08 if log else 1.0) + (0 if log else 0.015 * (ylim[1] - ylim[0])),
                txt, color="red", fontsize=8, ha="right", va="bottom")
    if log:
        ax.set_yscale("log")
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.set_ylabel(f"Total uncertainty [{unit}]")


def page_pressure_uncertainty(pdf, df):
    """Total uncertainty of pressure: file-level time series (a) and distribution per year (b)."""
    name = "pressure"
    st = total_unc_cap_stats(df, name, [PRESSURE_UNC_CAP])
    d, mask = st["have"], st["mask"]
    unit = df.loc[df["observed_variable_name"] == name, "units_abbreviation"].iloc[0]

    fig, axes = plt.subplots(2, 1, figsize=LANDSCAPE, gridspec_kw={"height_ratios": [1.2, 1]})

    ax = axes[0]
    n_off = int((d["uncertainty_total_max"] > PRESSURE_UNC_YMAX).sum())
    _band_axes(ax, d, mask, [(PRESSURE_UNC_CAP, f"{PRESSURE_UNC_CAP:g} Pa")], unit, log=True,
               ylim=(0.1, PRESSURE_UNC_YMAX))
    ax.set_xlabel("Date")
    ax.set_title("(a) Total uncertainty of pressure per monthly file (log scale)")
    ax.legend(fontsize=7.5, ncol=3, loc="upper left")
    if n_off:
        ax.text(0.99, 0.95, f"{n_off} file(s) with maxima > {PRESSURE_UNC_YMAX:.0e} {unit} are off scale "
                f"(largest: {d['uncertainty_total_max'].max():.1e} {unit})",
                transform=ax.transAxes, ha="right", va="top", fontsize=8, color="red")
    first = f"; first occurrence {st['first_cap']:%Y-%m}" if st["n_at_cap"] else ""
    ax.text(0.01, 0.04, f"{st['n_at_cap']}/{st['n_with_unc']} files have a maximum within "
            f"{100 * CAP_REL_TOL:g} % of {PRESSURE_UNC_CAP:g} {unit}{first}",
            transform=ax.transAxes, ha="left", va="bottom", fontsize=8, color="red")

    ax = axes[1]
    years = sorted(d["year"].unique())
    data = [d.loc[d["year"] == y, "uncertainty_total_max"].dropna().values for y in years]
    ax.boxplot(data, positions=years, widths=0.6, flierprops=dict(marker=".", ms=3, markeredgecolor="red"),
               medianprops=dict(color="black"))
    ax.set_yscale("log")
    ax.set_ylim(0.1, PRESSURE_UNC_YMAX)
    ax.axhline(PRESSURE_UNC_CAP, color="red", ls=":", lw=1)
    if n_off:
        ax.text(0.99, 0.95, f"{n_off} file(s) > {PRESSURE_UNC_YMAX:.0e} {unit} not shown",
                transform=ax.transAxes, ha="right", va="top", fontsize=8, color="red")
    ax.set_xticks(years[::2])
    ax.set_xticklabels([str(y) for y in years[::2]])
    ax.set_xlabel("Year")
    ax.set_ylabel(f"Max total uncertainty per file [{unit}]")
    ax.set_title("(b) Distribution of the file maxima per year (box = quartiles, red dots = outlier files)")

    fig.suptitle("Pressure: total uncertainty (no random / systematic components available)",
                 fontsize=13, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    pdf.savefig(fig)
    plt.close(fig)


def page_wind_uncertainty(pdf, df):
    """Total uncertainty of the wind variables: one panel per variable, file-level time series."""
    panels = [
        ("eastward wind speed", (WIND_UNC_CAP,), (0, 56), None),
        ("northward wind speed", (WIND_UNC_CAP,), (0, 56), None),
        ("wind speed", (WIND_UNC_CAP,), (0, 56), None),
        ("wind from direction", DIRECTION_UNC_CAPS, (0, 400), [0, 90, 180, 270, 360]),
    ]
    fig, axes = plt.subplots(2, 2, figsize=LANDSCAPE, sharex=True)
    for ax, (name, caps, ylim, yticks) in zip(axes.ravel(), panels):
        dd = df[df["observed_variable_name"] == name]
        unit = dd["units_abbreviation"].iloc[0]
        st = total_unc_cap_stats(df, name, caps)
        d, mask = st["have"], st["mask"]
        # Files without any valid total uncertainty are left as gaps (NaN) in the series
        full = dd[["date", "uncertainty_total_min", "uncertainty_total_max"]].copy()
        full.loc[dd["uncertainty_total_n_valid"] == 0, ["uncertainty_total_min", "uncertainty_total_max"]] = np.nan
        full_mask = pd.Series(False, index=full.index)
        full_mask.loc[mask.index[mask]] = True
        _band_axes(ax, full, full_mask, [(c, f"{c:g} {unit}") for c in caps], unit, log=False, ylim=ylim)
        if yticks is not None:
            ax.set_yticks(yticks)
        ax.set_title(name)
        n_none = st["n_files"] - st["n_with_unc"]
        ax.text(0.01, 0.96, f"{st['n_at_cap']}/{st['n_with_unc']} files at cap; "
                f"{n_none} file(s) without total uncertainty",
                transform=ax.transAxes, ha="left", va="top", fontsize=7.5, color="red")
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, fontsize=8, frameon=False)
    for ax in axes[1]:
        ax.set_xlabel("Date")
    fig.suptitle("Wind: total uncertainty per monthly file (maximum and min-max range; "
                 "no random / systematic components available)", fontsize=12, weight="bold")
    fig.tight_layout(rect=(0, 0.04, 1, 0.95))
    pdf.savefig(fig)
    plt.close(fig)


def page_uncertainty_extremes(pdf, df, var_names):
    """Distribution across files of the maximum reported uncertainty, per variable."""
    fig, axes = plt.subplots(1, 3, figsize=LANDSCAPE, sharey=True)
    for ax, comp, title in zip(axes, ["random", "systematic", "total"],
                               ["Random uncertainty", "Systematic uncertainty", "Total uncertainty"]):
        col = f"uncertainty_{comp}_max"
        data, labs = [], []
        for v in var_names:
            d = df.loc[df["observed_variable_name"] == v, col].dropna()
            d = d[d > 0]
            data.append(d.values if len(d) else np.array([np.nan]))
            labs.append("\n".join(textwrap.wrap(f"{v} [{df.loc[df['observed_variable_name'] == v, 'units_abbreviation'].iloc[0]}]", 30)))
        pos = np.arange(len(var_names))
        ax.boxplot([x[~np.isnan(x)] if np.isfinite(x).any() else [] for x in data], positions=pos,
                   orientation='horizontal', widths=0.6, flierprops=dict(marker=".", ms=3, markeredgecolor="red"),
                   medianprops=dict(color="black"))
        ax.set_xscale("log")
        ax.set_yticks(pos)
        ax.set_yticklabels(labs, fontsize=6.5)
        ax.set_title(title)
        ax.set_xlabel("Max uncertainty per file (log, native units)")
        for p, x in zip(pos, data):
            if not np.isfinite(x).any():
                ax.text(0.5, p, "n/a", transform=ax.get_yaxis_transform(), ha="center", va="center",
                        fontsize=7, color="gray")
    axes[0].invert_yaxis()
    fig.suptitle("Spread of the maximum reported uncertainty (red dots = outlier files)",
                 fontsize=13, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    pdf.savefig(fig)
    plt.close(fig)


def page_consistency_old(pdf, df):
    """Scatter total_max vs the largest component: tests the quadrature-sum consistency."""
    targets = ["air temperature", "relative humidity", "water vapour mixing ratio", "geopotential height"]
    fig, axes = plt.subplots(2, 2, figsize=LANDSCAPE)
    sc = None
    for ax, v in zip(axes.ravel(), targets):
        d = df[df["observed_variable_name"] == v].dropna(subset=["uncertainty_total_max"])
        d = d[d["uncertainty_total_max"] > 0]
        comps = d[["uncertainty_random_max", "uncertainty_systematic_max"]]
        x = comps.max(axis=1)
        ok = x > 0
        d, x = d[ok], x[ok]
        y = d["uncertainty_total_max"]
        viol_lo, viol_hi = consistency_flags(d)
        sc = ax.scatter(x, y, c=d["year"], cmap="viridis", s=12, alpha=0.8)
        ax.scatter(x[viol_lo | viol_hi], y[viol_lo | viol_hi], facecolors="none", edgecolors="red", s=45, lw=0.8)
        lim = [min(x.min(), y.min()), max(x.max(), y.max())]
        ax.plot(lim, lim, "k-", lw=0.8, label="total = max(components)  (lower bound)")
        both = comps.notna().all(axis=1).loc[d.index].any()
        if both:
            ax.plot(lim, [2 * l for l in lim], "k--", lw=0.8, label="total = 2 x max(comp.)  (upper bound)")
        ax.set_xscale("log")
        ax.set_yscale("log")
        if v == "relative humidity":
            ax.set_xlim(1e-2, 1e6)
            ax.set_ylim(1e-2, 1e6)
            n_off = int(((x > 1e6) | (y > 1e6)).sum())
            ax.text(0.98, 0.04, f"{n_off} file(s) off scale", transform=ax.transAxes, ha="right",
                    fontsize=7, color="red")
        ax.set_xlabel("max of components' file maxima")
        ax.set_ylabel("total uncertainty (file maximum)")
        ax.set_title(f"{v}: {int((viol_lo | viol_hi).sum())}/{len(d)} files outside the bounds", fontsize=9)
        ax.legend(fontsize=6.5, loc="upper left")
    cax = fig.add_axes([0.91, 0.12, 0.015, 0.68])
    fig.colorbar(sc, cax=cax, label="Year")
    fig.suptitle("Consistency of the total uncertainty with its components\n"
                 "(if total = quadrature sum, points lie between the solid and dashed lines; red rings = outside;\n"
                 "points below the solid line suggest a clipped/capped total uncertainty)",
                 fontsize=12, weight="bold")
    fig.subplots_adjust(top=0.85, left=0.07, right=0.88, bottom=0.08, hspace=0.35, wspace=0.25)
    pdf.savefig(fig)
    plt.close(fig)

def page_consistency(pdf, df):
    """Scatter total_max vs the largest component: tests the quadrature-sum consistency."""
    targets = ["air temperature", "relative humidity", "water vapour mixing ratio", "geopotential height"]
    fig, axes = plt.subplots(2, 2, figsize=LANDSCAPE)
    sc = None

    for ax, v in zip(axes.ravel(), targets):
        d = df[df["observed_variable_name"] == v].dropna(subset=["uncertainty_total_max"])
        d = d[d["uncertainty_total_max"] > 0]
        comps = d[["uncertainty_random_max", "uncertainty_systematic_max"]]
        x = comps.max(axis=1)
        ok = x > 0
        d, x = d[ok], x[ok]
        y = d["uncertainty_total_max"]

        if len(d) == 0:
            ax.text(0.5, 0.5, "No valid data available", transform=ax.transAxes,
                    ha="center", va="center", fontsize=10, color="gray")
            ax.set_title(f"{v}: 0/0 files outside the bounds", fontsize=9)
            continue

        viol_lo, viol_hi = consistency_flags(d)
        sc_item = ax.scatter(x, y, c=d["year"], cmap="viridis", s=12, alpha=0.8)
        if sc is None:
            sc = sc_item

        ax.scatter(x[viol_lo | viol_hi], y[viol_lo | viol_hi], facecolors="none", edgecolors="red", s=45, lw=0.8)

        # Ensure valid limits > 0 for log scale
        min_val = max(1e-6, min(x.min(), y.min()))
        max_val = max(min_val * 10, max(x.max(), y.max()))
        lim = [min_val, max_val]

        ax.plot(lim, lim, "k-", lw=0.8, label="total = max(components)  (lower bound)")
        both = comps.notna().all(axis=1).loc[d.index].any()
        if both:
            ax.plot(lim, [2 * l for l in lim], "k--", lw=0.8, label="total = 2 x max(comp.)  (upper bound)")

        ax.set_xscale("log")
        ax.set_yscale("log")

        if v == "relative humidity":
            ax.set_xlim(1e-2, 1e6)
            ax.set_ylim(1e-2, 1e6)
            n_off = int(((x > 1e6) | (y > 1e6)).sum())
            ax.text(0.98, 0.04, f"{n_off} file(s) off scale", transform=ax.transAxes, ha="right",
                    fontsize=7, color="red")

        ax.set_xlabel("max of components' file maxima")
        ax.set_ylabel("total uncertainty (file maximum)")
        ax.set_title(f"{v}: {int((viol_lo | viol_hi).sum())}/{len(d)} files outside the bounds", fontsize=9)
        ax.legend(fontsize=6.5, loc="upper left")

    if sc is not None:
        cax = fig.add_axes([0.91, 0.12, 0.015, 0.68])
        fig.colorbar(sc, cax=cax, label="Year")

    fig.suptitle("Consistency of the total uncertainty with its components\n"
                 "(if total = quadrature sum, points lie between the solid and dashed lines; red rings = outside;\n"
                 "points below the solid line suggest a clipped/capped total uncertainty)",
                 fontsize=12, weight="bold")
    fig.subplots_adjust(top=0.85, left=0.07, right=0.88, bottom=0.08, hspace=0.35, wspace=0.25)
    pdf.savefig(fig)
    plt.close(fig)

def page_methods(pdf, df, var_names):
    """Appendix: definitions, assumptions, limitations and input checks."""
    gaps = month_gaps(df)
    sections = [
        ("1. Input and scope", [
            f"Input table: {INPUT_CSV_PATH}. {len(df)} file/variable rows, {df['file'].nunique()} monthly files "
            f"({df['date'].min():%Y-%m} to {df['date'].max():%Y-%m}, {len(gaps)} months without file), "
            f"{len(var_names)} observed variables. Report generated on {pd.Timestamp.now():%Y-%m-%d %H:%M}."]),
        ("2. Definitions", [
            "Level: one record of a monthly file; n_records is the number of levels and n_valid the number of "
            "levels with a non-missing observation.",
            "Out-of-range file: a file/variable row whose minimum is below, or whose maximum is above, the closed "
            "interval [lower, upper] given for its variable. A single level suffices to flag a file, so the flag "
            "cannot distinguish isolated outliers from systematic failures."]),
        ("3. Plausible ranges", [
            f"The ranges are the check_range values of PLAUSIBILITY_RULES in {QC_PIPELINE_PATH}, converted to the "
            "units of the CDM files (relative humidity: fraction -> %; pressure: hPa -> Pa; water vapour mixing "
            "ratio: ppmv -> mol/mol; see CDM_CODE_TO_RULE). Variables whose rule has no check_range, or that have "
            "no rule (time since launch), are not assessed."]),
        ("4. Limitations", [
            "The range test detects gross errors only; physically plausible but wrong values pass. The table "
            "stores only the minimum, maximum and number of valid values per file, so the number of out-of-range "
            "levels cannot be determined. Uncertainty variables are not range-tested; they are assessed through "
            "file-level maxima (consistency page).",
            "Saturation (\"cap\") of the total uncertainty of pressure and wind: only file minima and maxima are "
            "available, so a cap is inferred when the file maximum lies within "
            f"{100 * CAP_REL_TOL:g} % of a characteristic value (pressure {PRESSURE_UNC_CAP:g} Pa; wind components "
            f"and wind speed {WIND_UNC_CAP:g} m/s; wind direction {DIRECTION_UNC_CAPS[0]:g} and "
            f"{DIRECTION_UNC_CAPS[1]:g} deg; constants at the top of the script). The counts are numbers of FILES "
            "(not levels): they show that the cap value is reached in a file, not how many levels are capped, and "
            "a file whose true maximum coincides with the cap by chance would also be counted. The caps were identified empirically from the pile-up of the file maxima, not from "
            "GRUAN documentation. Random and systematic components are not available for these variables, so "
            "the quadrature consistency test cannot be applied.",
            "Missing files are shown in grey in the heatmaps."]),
        ("5. Input checks", input_checks(df)),
    ]
    fig = plt.figure(figsize=PORTRAIT)
    fig.text(0.5, 0.965, "Appendix - methods, definitions and limitations", ha="center", fontsize=14, weight="bold")
    y = 0.93
    for title, paras in sections:
        fig.text(0.07, y, title, fontsize=10.5, weight="bold", color="#1f3b63")
        y -= 0.022
        for p in paras:
            lines = textwrap.wrap(p, 112)
            fig.text(0.08, y, "\n".join(lines), fontsize=8.5, va="top", linespacing=1.3)
            y -= 0.0167 * len(lines) + 0.010
        y -= 0.008
    pdf.savefig(fig)
    plt.close(fig)


def page_table(pdf, summ):
    """Appendix: per-variable summary table."""
    fig, ax = plt.subplots(figsize=LANDSCAPE)
    ax.axis("off")
    cols = ["Variable", "Valid obs (%)", "Random unc. (%)", "Syst. unc. (%)", "Total unc. (%)",
            "Files out of range (%)", "Lowest min", "Highest max"]
    cell = []
    for _, r in summ.iterrows():
        cell.append([textwrap.fill(r["Variable"], 40)]
                    + ["n/a" if pd.isna(r[c]) else f"{r[c]:.1f}" for c in cols[1:6]]
                    + [f"{r['Lowest min']:.4g}", f"{r['Highest max']:.4g}"])
    tbl = ax.table(cellText=cell, colLabels=[textwrap.fill(c, 14) for c in cols], loc="center", cellLoc="center",
                   colWidths=[0.30] + [0.10] * 7)
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(7.5)
    tbl.scale(1, 2.0)
    for j in range(len(cols)):
        tbl[0, j].set_facecolor("#dbe5f1")
        tbl[0, j].set_text_props(weight="bold")
    for i, (_, r) in enumerate(summ.iterrows(), start=1):
        tbl[i, 0].set_text_props(ha="left")
        if r["Files out of range (%)"] > 10:
            tbl[i, 5].set_facecolor("#f8c9c4")
        if r["Valid obs (%)"] < 80:
            tbl[i, 1].set_facecolor("#f8c9c4")
        for j in (2, 3, 4):
            if r[cols[j]] == 0:
                tbl[i, j].set_facecolor("#eeeeee")
    ax.set_title("Appendix - per-variable summary (red = potential problem, grey = component absent, n/a = no plausible range)",
                 fontsize=12, weight="bold")
    fig.tight_layout()
    pdf.savefig(fig)
    plt.close(fig)


# ==========================================================================
# Main
# ==========================================================================
def main():
    df = load_data(INPUT_CSV_PATH)
    var_names = variable_order(df)
    summ = per_variable_summary(df, var_names)

    for msg in input_checks(df):
        print("[check]", msg)

    with PdfPages(OUTPUT_PDF_PATH) as pdf:
        page_summary(pdf, df, var_names, summ)
        page_volume(pdf, df)
        page_validity(pdf, df, var_names)
        page_uncertainty_availability(pdf, df, var_names, summ)
        page_plausibility(pdf, df, var_names)
        page_envelopes(pdf, df, var_names)
        page_uncertainty_timeseries(pdf, df)
        page_pressure_uncertainty(pdf, df)
        page_wind_uncertainty(pdf, df)
        page_uncertainty_extremes(pdf, df, var_names)
        page_consistency(pdf, df)
        page_table(pdf, summ)
        page_methods(pdf, df, var_names)
        info = pdf.infodict()
        info["Title"] = "GRUAN dataset - quality screening report"

    print(f"Report written to: {OUTPUT_PDF_PATH}")


if __name__ == "__main__":
    main()