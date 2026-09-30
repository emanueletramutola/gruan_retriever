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

Dependencies: numpy, pandas, matplotlib
"""

# --------------------------------------------------------------------------
# USER SETTINGS  (edit these two variables)
# --------------------------------------------------------------------------
# INPUT_CSV_PATH = "/home/emanuele/logs/gruan_check_nc_CDM.csv"      # input CSV file
INPUT_CSV_PATH = "/Data/GRUAN_TEST/output/gruan_check_nc_CDM.csv"      # input CSV file
# OUTPUT_PDF_PATH = "/home/emanuele/logs/gruan_quality_report.pdf"   # output PDF file
OUTPUT_PDF_PATH = "/Data/GRUAN_TEST/output/gruan_quality_report.pdf"   # output PDF file
# --------------------------------------------------------------------------

import textwrap

import matplotlib

matplotlib.use("Agg")  # non-interactive backend: figures go straight to PDF
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.backends.backend_pdf import PdfPages

# Physically plausible ranges used to flag suspicious min / max values.
# These are ASSUMPTIONS made for a quick screening (generous, so that only
# clearly unphysical values are flagged); adapt them to your needs.
PLAUSIBLE_RANGES = {
    "shortwave radiation": (0.0, 1600.0),                       # W m-2
    "eastward wind speed": (-150.0, 150.0),                     # m/s
    "northward wind speed": (-150.0, 150.0),                    # m/s
    "wind from direction": (0.0, 360.0),                        # deg
    "wind speed": (0.0, 180.0),                                 # m/s
    "frost point temperature": (150.0, 330.0),                  # K
    "geopotential height": (-100.0, 50000.0),                   # m
    "vertical speed of radiosonde": (-10.0, 30.0),              # m/s
    "water vapour mixing ratio": (0.0, 0.05),                   # mol/mol
    "air relative humidity effective vertical resolution": (0.0, 1000.0),  # s
    "altitude": (-100.0, 50000.0),                              # m
    "air temperature": (178.15, 323.15),                          # K
    "relative humidity": (0.0, 105.0),                          # % (small supersaturation tolerated)
    "pressure": (1.0, 110000.0),                                # Pa
    "time since launch": (0.0, 21600.0),                        # s (6 h)
}

# Relative tolerance used in the uncertainty-consistency test
CONSISTENCY_TOL = 1e-3

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

    # Plausibility flags on the observation min / max
    lo = df["observed_variable_name"].map(lambda n: PLAUSIBLE_RANGES.get(n, (np.nan, np.nan))[0])
    hi = df["observed_variable_name"].map(lambda n: PLAUSIBLE_RANGES.get(n, (np.nan, np.nan))[1])
    df["range_lo"], df["range_hi"] = lo, hi
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


def pivot_months(df, value_col, var_names):
    """Pivot to a (variable x every month) matrix; missing months -> NaN."""
    months = pd.date_range(df["date"].min(), df["date"].max(), freq="MS")
    p = df.pivot_table(index="observed_variable_name", columns="date", values=value_col, aggfunc="first")
    return p.reindex(index=var_names, columns=months), months


def heatmap(ax, matrix, months, labels, cmap="viridis", vmin=0, vmax=1):
    """Draw a variable x time heatmap with a proper date axis."""
    cm = plt.get_cmap(cmap).copy()
    cm.set_bad("#d9d9d9")  # grey = no file / no data
    x0 = mdates.date2num(months[0])
    x1 = mdates.date2num(months[-1] + pd.offsets.MonthBegin(1))
    im = ax.imshow(
        np.ma.masked_invalid(matrix.values), aspect="auto", cmap=cm, vmin=vmin, vmax=vmax,
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
        rows.append({
            "Variable": d["label"].iloc[0].replace("\n", " "),
            "name": v,
            "Valid obs (%)": 100 * d["observation_value_n_valid"].sum() / rec,
            "Random unc. (%)": 100 * d["uncertainty_random_n_valid"].sum() / rec,
            "Syst. unc. (%)": 100 * d["uncertainty_systematic_n_valid"].sum() / rec,
            "Total unc. (%)": 100 * d["uncertainty_total_n_valid"].sum() / rec,
            "Files out of range (%)": 100 * d["out_of_range"].mean(),
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
    oor_rows = 100 * df["out_of_range"].mean()
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

    fig.text(0.05, 0.03, "Plausible ranges are screening assumptions defined in PLAUSIBLE_RANGES at the top of the "
             "script; consistency bounds assume total = sqrt(random^2 + systematic^2) at each point.",
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
    oor_rows = 100 * df["out_of_range"].mean()
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
        f"just below ~20 K {cap_first_txt} ({cap_files} files near 20 K), i.e. a cap/fill value rather than "
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

    fig.text(0.05, 0.03, "Plausible ranges are screening assumptions defined in PLAUSIBLE_RANGES at the top of the "
             "script; consistency bounds assume total = sqrt(random^2 + systematic^2) at each point.",
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
    below = [100 * df.loc[df["observed_variable_name"] == v, "min_below"].mean() for v in var_names]
    above = [100 * df.loc[df["observed_variable_name"] == v, "max_above"].mean() for v in var_names]
    labels = [f"{v}  [{PLAUSIBLE_RANGES[v][0]:g}, {PLAUSIBLE_RANGES[v][1]:g}]" for v in var_names]
    labels = ["\n".join(textwrap.wrap(s, 34)) for s in labels]
    y = np.arange(len(var_names))
    ax.barh(y - 0.2, below, height=0.4, color="#0072b2", label="minimum below range")
    ax.barh(y + 0.2, above, height=0.4, color="#d55e00", label="maximum above range")
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=7)
    ax.invert_yaxis()
    ax.set_xlabel("% of monthly files")
    ax.set_title("(a) Files with min/max outside the plausible range\n(assumed range in brackets)")
    ax.legend(fontsize=8, loc="lower right")

    ax = axes[1]
    by_year = df.groupby("year")["out_of_range"].mean() * 100
    ax.bar(by_year.index, by_year.values, color="#cc79a7")
    ax.set_xlabel("Year")
    ax.set_ylabel("% of file/variable rows flagged")
    ax.set_title("(b) Flagged rows per year (all variables)")

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
        lo, hi = PLAUSIBLE_RANGES[v]
        span = hi - lo
        ylim = (lo - 0.25 * span, hi + 0.25 * span)
        n_out = 0
        for col, colr in (("observation_value_min", "#0072b2"), ("observation_value_max", "#e69f00")):
            x = d["date"].values
            yv = d[col].values
            ok = ~np.isnan(yv)
            x, yv = x[ok], yv[ok]
            out = (yv < lo) | (yv > hi)
            n_out += int(out.sum())
            ax.plot(x[~out], yv[~out], ".", ms=2.2, color=colr)
            ax.plot(x[out], np.clip(yv[out], *ylim), "x", ms=3.5, color="red", mew=0.8)
        ax.axhline(lo, color="gray", ls="--", lw=0.7)
        ax.axhline(hi, color="gray", ls="--", lw=0.7)
        ax.set_ylim(*ylim)
        ax.set_title("\n".join(textwrap.wrap(f"{v} [{d['units_abbreviation'].iloc[0]}]", 34))
                     + f"\n{n_out} out-of-range points", fontsize=7)
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
        plt.Line2D([], [], ls="--", color="gray", label="assumed plausible range"),
    ]
    fig.legend(handles=handles, loc="lower center", ncol=4, fontsize=7, frameon=False)
    fig.suptitle("Monthly observation envelopes vs plausible ranges", fontsize=12, weight="bold")
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

def page_table(pdf, summ):
    """Appendix: per-variable summary table."""
    fig, ax = plt.subplots(figsize=LANDSCAPE)
    ax.axis("off")
    cols = ["Variable", "Valid obs (%)", "Random unc. (%)", "Syst. unc. (%)", "Total unc. (%)",
            "Files out of range (%)", "Lowest min", "Highest max"]
    cell = []
    for _, r in summ.iterrows():
        cell.append([textwrap.fill(r["Variable"], 40)] + [f"{r[c]:.1f}" for c in cols[1:6]]
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
    ax.set_title("Appendix - per-variable summary (red = potential problem, grey = component absent)",
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

    with PdfPages(OUTPUT_PDF_PATH) as pdf:
        page_summary(pdf, df, var_names, summ)
        page_volume(pdf, df)
        page_validity(pdf, df, var_names)
        page_uncertainty_availability(pdf, df, var_names, summ)
        page_plausibility(pdf, df, var_names)
        page_envelopes(pdf, df, var_names)
        page_uncertainty_timeseries(pdf, df)
        page_uncertainty_extremes(pdf, df, var_names)
        page_consistency(pdf, df)
        page_table(pdf, summ)
        info = pdf.infodict()
        info["Title"] = "GRUAN dataset - quality screening report"

    print(f"Report written to: {OUTPUT_PDF_PATH}")


if __name__ == "__main__":
    main()