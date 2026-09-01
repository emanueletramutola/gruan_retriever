#!/usr/bin/env python3
"""
GRUAN database QC scan
=======================

Purpose
-------
Scan a very large (order of ~570 GB), monthly-partitioned PostgreSQL database
of GRUAN radiosonde profiles ("header"/"station" + "data_YYYYMM" partitions
of "data"), in parallel, to:

  1. Compute per-station descriptive statistics of temperature (count, mean,
     std, min, max) restricted to a physically plausible range.
  2. Compute per-station temperature histograms over ALL pressure levels
     together, using shared bin edges so partial histograms from different
     partitions/workers can simply be summed.
  3. Extract and label anomalous records (e.g. temperature < 10 K, which is
     physically impossible for the Earth's atmosphere and is almost
     certainly a corrupted value, a missing-value sentinel, or a unit-
     conversion bug) for forensic inspection by the researcher.
  4. Check temporal continuity per station: missing months, missing launches
     relative to each station's own nominal cadence, and true duplicate rows
     (same station + report_timestamp + press stored more than once). Note:
     the row-level uniqueness key of "data" is
     (idstation_pk, report_timestamp, press) - a single launch normally has
     many rows, one per pressure level, so repeated report_timestamp values
     by themselves are expected, not an anomaly.

Design rationale
-----------------
- The "data" table is already partitioned monthly into physical tables
  "data_YYYYMM". Parallelism is applied at the partition level: each worker
  process opens its own DB connection and scans one partition at a time.
  This maps naturally onto the existing physical layout and lets PostgreSQL
  serve several independent sequential scans concurrently (I/O and CPU
  permitting).
- All heavy aggregation (GROUP BY, width_bucket histogram binning) is pushed
  down to the database. The Python client only ever receives small,
  per-partition, per-station aggregates - never raw rows - except for the
  (expected to be rare) anomalous records and the lightweight per-record
  timestamps needed for continuity analysis.
- Histogram bin edges are FIXED and shared across the whole run, so partial
  histograms computed independently on different partitions can be summed
  index-by-index in the reducer without any re-binning.
- width_bucket(x, low, high, nbins) returns:
      0            for x < low   -> "underflow" bucket
      1 .. nbins   for low <= x < high
      nbins + 1    for x >= high -> "overflow" bucket
  so out-of-range physical impossibilities (e.g. 3 K) automatically land in
  bin 0 of the histogram, without any pre-filtering, which is exactly what
  we want to *see* rather than silently discard.
- Temporal continuity is checked at two granularities:
    (a) per-station, per-calendar-month raw record counts (essentially free,
        reuses the partition boundaries themselves) -> a coverage heatmap
        that immediately shows whole missing months.
    (b) per-station sorted report_timestamp sequences (station, timestamp
        only - lightweight even over 20+ years of data) -> exact gap
        detection using each station's own empirical (median) launch
        cadence as the "nominal" reference, plus a hard absolute threshold
        for long outages regardless of nominal frequency, plus duplicate
        timestamp detection.

Requirements
------------
    pip install psycopg2-binary sqlalchemy numpy pandas matplotlib python-dotenv

Database credentials
---------------------
Connection parameters are NOT passed on the command line. They are read from
a .env file (project root by default, or a custom path via --env-file) with
the following variable names:

    DB_USER=gruan_user
    GRUAN_USER_PSW=xxx
    DB_HOST=150.145.73.252
    DB_PORT=5432
    DB_NAME=gruan

Typical usage
-------------
    # Quick smoke test on 3 partitions before committing to a full 570 GB scan
    python gruan_qc_scan.py --partitions-limit 3

    # Full scan
    python gruan_qc_scan.py --workers 12 --outdir ./gruan_qc_output

    # .env file in a non-default location
    python gruan_qc_scan.py --env-file /path/to/project/.env

Notes on server-side tuning
----------------------------
Running N worker processes means N concurrent PostgreSQL backends. Before a
full run, check that:
  - `max_connections` on the server comfortably exceeds --workers (+ margin
    for other clients).
  - `work_mem` is reasonable for GROUP BY / hashing (a few tens of MB per
    connection is usually enough here, since we group by only 34 stations).
  - Physical storage (local SSD/NVMe vs. network storage) can actually
    sustain N concurrent sequential scans; if I/O-bound, reduce --workers
    rather than increasing it blindly.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import math
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Optional
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import psycopg2
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

try:
    from tqdm import tqdm  # optional, nicer progress bar
except ImportError:  # pragma: no cover - tqdm is optional
    tqdm = None


# ---------------------------------------------------------------------------
# Configuration defaults (all overridable from the command line)
# ---------------------------------------------------------------------------

# Physically plausible bounds for atmospheric temperature (K) and pressure
# (Pa). These are used ONLY to classify / flag data, never to silently drop
# anything without a trace: every excluded record is written to the
# anomalies CSV with an explanatory reason.
DEFAULT_TEMP_MIN_PHYSICAL = 150.0    # ~ -123 degC: colder than any recorded
                                      # atmospheric temperature (surface or
                                      # stratosphere)
DEFAULT_TEMP_MAX_PHYSICAL = 340.0    # ~ 67 degC: hotter than any recorded
                                      # in-situ atmospheric observation
DEFAULT_PRESS_MIN_PHYSICAL = 0.0
DEFAULT_PRESS_MAX_PHYSICAL = 110000.0  # slightly above standard sea-level
                                        # pressure, as a generous upper bound

# Histogram configuration: shared, fixed bin edges (1 K bins by default).
DEFAULT_HIST_MIN = 150.0
DEFAULT_HIST_MAX = 340.0
DEFAULT_HIST_NBINS = 190

# Hard threshold explicitly called out by the researcher.
TEMP_BELOW_SENTINEL = 10.0

# Safety cap on the number of anomalous rows pulled per partition, so a
# partition that is catastrophically broken cannot exhaust client memory.
# A warning is logged if this cap is hit, so the researcher knows the count
# for that partition is a lower bound.
MAX_ANOMALY_ROWS_PER_PARTITION = 20000

# --- Temporal continuity parameters ----------------------------------------
# A gap is flagged as "relative" if it exceeds this multiple of the
# station's own median inter-launch interval (captures irregular sparsening
# even for high-frequency stations).
CONTINUITY_RELATIVE_GAP_FACTOR = 3.0
# A gap is always flagged as "absolute" if it is at least this long,
# regardless of the station's nominal cadence (captures real outages even
# for low-frequency stations where the relative threshold would be lax).
CONTINUITY_ABSOLUTE_GAP_DAYS = 30.0

PARTITION_NAME_RE = r"^data_\d{6}$"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(processName)s: %(message)s",
)
log = logging.getLogger("gruan_qc")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class ScanConfig:
    dsn: dict
    temp_min_physical: float
    temp_max_physical: float
    press_min_physical: float
    press_max_physical: float
    hist_min: float
    hist_max: float
    hist_nbins: int


@dataclasses.dataclass
class PartitionResult:
    partition: str
    stats: pd.DataFrame          # index: idstation_pk -> n, sum, sumsq, min, max
    hist: pd.DataFrame           # index: idstation_pk, columns: bin (0..nbins+1) -> counts
    anomalies: pd.DataFrame      # detailed anomalous rows
    n_anomalies_total: int       # true count, may exceed len(anomalies) if capped
    coverage_counts: pd.Series   # idstation_pk -> raw record count in this partition
    raw_temp_min: Optional[float]   # UNFILTERED min/max of temp and press in
    raw_temp_max: Optional[float]   # this partition (NULLs excluded only).
    raw_press_min: Optional[float]  # Diagnostic only: if these fall wildly
    raw_press_max: Optional[float]  # outside the expected physical bounds,
                                     # it usually means a unit mismatch
                                     # (e.g. temp stored in degC instead of
                                     # K, or press in hPa instead of Pa)
                                     # rather than genuinely bad data.
    timestamps: pd.DataFrame     # idstation_pk, report_timestamp, press (all
                                  # rows with a non-null report_timestamp).
                                  # NOTE: the row-level uniqueness key of
                                  # "data" is (idstation_pk, report_timestamp,
                                  # press) - one launch (report_timestamp)
                                  # normally has MANY rows, one per pressure
                                  # level, so repeated report_timestamp
                                  # values are expected and NOT a duplicate.
    elapsed_s: float


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def connect(dsn: dict):
    """Raw psycopg2 connection, used only for the simple, non-pandas
    catalog query in list_data_partitions()."""
    conn = psycopg2.connect(**dsn)
    conn.set_session(readonly=True, autocommit=True)
    return conn


# SQLAlchemy Engine cache: one per process (each worker process has its own
# separate memory space, so a plain module-level dict is safe here and lets
# a worker that handles several partitions reuse the same connection pool
# instead of reconnecting for every partition).
_ENGINE_CACHE: dict[tuple, Engine] = {}


def get_engine(dsn: dict) -> Engine:
    """Return a cached SQLAlchemy Engine for this process, creating it on
    first use. Using SQLAlchemy (instead of a bare psycopg2 connection)
    avoids pandas' "only supports SQLAlchemy connectable" UserWarning and
    gives connection pooling / recycling for free."""
    key = (dsn["host"], dsn["port"], dsn["dbname"], dsn["user"])
    engine = _ENGINE_CACHE.get(key)
    if engine is None:
        url = (
            f"postgresql+psycopg2://{quote_plus(dsn['user'])}:"
            f"{quote_plus(dsn['password'])}@{dsn['host']}:{dsn['port']}/"
            f"{dsn['dbname']}"
        )
        engine = create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        _ENGINE_CACHE[key] = engine
    return engine


def list_data_partitions(dsn: dict) -> list[str]:
    """Discover the real list of monthly partitions rather than assuming a
    fixed date range: robust against gaps or an evolving end date."""
    query = """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
          AND tablename ~ %s
        ORDER BY tablename;
    """
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(query, (PARTITION_NAME_RE,))
            partitions = [row[0] for row in cur.fetchall()]
    if not partitions:
        raise RuntimeError(
            "No partitions matching 'data_YYYYMM' were found in schema "
            "'public'. Check the DSN and the partition naming convention."
        )
    return partitions


# ---------------------------------------------------------------------------
# Worker function (runs in a separate process, own DB connection)
# ---------------------------------------------------------------------------

def scan_partition(partition: str, cfg: ScanConfig) -> PartitionResult:
    t0 = time.time()

    # Defense in depth: the partition name already comes from a regex-
    # filtered pg_tables query (list_data_partitions), but we re-validate
    # here too since this string is interpolated directly into SQL below
    # (SQLAlchemy's text() only binds VALUES, not identifiers/table names).
    if not re.match(PARTITION_NAME_RE, partition):
        raise ValueError(f"Refusing to scan implausible partition name: {partition!r}")
    quoted_table = f'"{partition}"'

    engine = get_engine(cfg.dsn)

    # 1) Per-station statistics, restricted to the "valid" physical range,
    #    so mean/std reported to the researcher are not blown up by the
    #    known gross outliers (those are tracked separately below).
    stats_query = text(f"""
        SELECT idstation_pk,
               count(*)          AS n,
               sum(temp)         AS sum_t,
               sum(temp * temp)  AS sumsq_t,
               min(temp)         AS min_t,
               max(temp)         AS max_t
        FROM {quoted_table}
        WHERE temp IS NOT NULL
          AND temp BETWEEN :temp_min AND :temp_max
          AND press IS NOT NULL
          AND press BETWEEN :press_min AND :press_max
        GROUP BY idstation_pk
    """)

    # 2) Per-station histogram over the FULL range of temp values
    #    (NULLs excluded, everything else included). width_bucket sends
    #    out-of-range values into bin 0 (underflow, e.g. the T < 10 K
    #    case) or bin nbins+1 (overflow), so anomalies show up in the
    #    histogram itself rather than being silently dropped.
    hist_query = text(f"""
        SELECT idstation_pk,
               width_bucket(temp, :hist_min, :hist_max, :hist_nbins) AS bin,
               count(*) AS n
        FROM {quoted_table}
        WHERE temp IS NOT NULL
        GROUP BY idstation_pk, bin
    """)

    # 3) Detailed, labeled extraction of anomalous rows for forensic
    #    inspection (which station, which exact timestamp, which value).
    anomaly_query = text(f"""
        SELECT idstation_pk,
               report_timestamp,
               temp,
               press,
               CASE
                   WHEN temp IS NULL THEN 'temp_null'
                   WHEN temp < :temp_sentinel THEN 'temp_below_10K'
                   WHEN temp < :temp_min OR temp > :temp_max
                        THEN 'temp_out_of_physical_range'
                   WHEN press IS NULL THEN 'press_null'
                   WHEN press <= :press_min OR press > :press_max
                        THEN 'press_out_of_physical_range'
                   ELSE 'other'
               END AS anomaly_reason
        FROM {quoted_table}
        WHERE temp IS NULL
           OR temp < :temp_min OR temp > :temp_max
           OR press IS NULL
           OR press <= :press_min OR press > :press_max
        LIMIT :row_cap
    """)

    # Exact count of anomalies (cheap: single scalar), independent of
    # the row cap above, so we always know the true magnitude even if
    # the detailed listing was truncated.
    count_query = text(f"""
        SELECT count(*)
        FROM {quoted_table}
        WHERE temp IS NULL
           OR temp < :temp_min OR temp > :temp_max
           OR press IS NULL
           OR press <= :press_min OR press > :press_max
    """)

    # 4) Raw, unfiltered record count per station for this calendar
    #    month, used to build the station x month coverage matrix.
    #    Deliberately NOT filtered by temp/press validity: a corrupted
    #    reading still proves the station launched that month.
    coverage_query = text(f"""
        SELECT idstation_pk, count(*) AS n
        FROM {quoted_table}
        GROUP BY idstation_pk
    """)

    # 5) Lightweight per-record (station, timestamp, pressure) triplets for
    #    continuity analysis. This is the row-level uniqueness key of
    #    "data" (idstation_pk, report_timestamp, press): including press
    #    lets us tell apart the normal case - many pressure levels sharing
    #    one report_timestamp within a single launch - from a genuine
    #    duplicated row (same station + timestamp + press twice).
    timestamps_query = text(f"""
        SELECT idstation_pk, report_timestamp, press
        FROM {quoted_table}
        WHERE report_timestamp IS NOT NULL
    """)

    # 6) Diagnostic only: TRUE min/max of temp and press with NO physical
    #    filter applied at all (only NULLs excluded). This is what lets us
    #    tell "genuinely corrupted data" apart from "wrong unit assumption
    #    in DEFAULT_TEMP_*/DEFAULT_PRESS_* / --temp-*-physical /
    #    --press-*-physical" - e.g. if raw temp values sit around -60..20,
    #    the column is almost certainly in degC, not K, and EVERY row would
    #    be (wrongly) flagged as an anomaly by the physical-range filters.
    raw_range_query = text(f"""
        SELECT min(temp) AS raw_temp_min, max(temp) AS raw_temp_max,
               min(press) AS raw_press_min, max(press) AS raw_press_max
        FROM {quoted_table}
    """)

    params = {
        "temp_min": cfg.temp_min_physical,
        "temp_max": cfg.temp_max_physical,
        "press_min": cfg.press_min_physical,
        "press_max": cfg.press_max_physical,
        "hist_min": cfg.hist_min,
        "hist_max": cfg.hist_max,
        "hist_nbins": cfg.hist_nbins,
        "temp_sentinel": TEMP_BELOW_SENTINEL,
        "row_cap": MAX_ANOMALY_ROWS_PER_PARTITION,
    }

    with engine.connect() as conn:
        stats_df = pd.read_sql_query(stats_query, conn, params=params)
        hist_df = pd.read_sql_query(hist_query, conn, params=params)
        anomalies_df = pd.read_sql_query(anomaly_query, conn, params=params)
        coverage_df = pd.read_sql_query(coverage_query, conn, params=params)
        timestamps_df = pd.read_sql_query(timestamps_query, conn, params=params)
        n_anomalies_total = conn.execute(count_query, params).scalar_one()
        raw_range_row = conn.execute(raw_range_query, params).mappings().one()

    if len(anomalies_df) >= MAX_ANOMALY_ROWS_PER_PARTITION:
        log.warning(
            "%s: anomaly row cap (%d) reached; true anomaly count is %d. "
            "The detailed listing for this partition is a sample, not "
            "exhaustive - the count is still exact.",
            partition, MAX_ANOMALY_ROWS_PER_PARTITION, n_anomalies_total,
        )

    stats_df = stats_df.set_index("idstation_pk")
    coverage_series = coverage_df.set_index("idstation_pk")["n"]
    anomalies_df.insert(0, "partition", partition)

    elapsed = time.time() - t0
    log.info("%s scanned in %.1f s (%d anomalies)", partition, elapsed, n_anomalies_total)

    return PartitionResult(
        partition=partition,
        stats=stats_df,
        hist=hist_df,
        anomalies=anomalies_df,
        n_anomalies_total=n_anomalies_total,
        coverage_counts=coverage_series,
        raw_temp_min=raw_range_row["raw_temp_min"],
        raw_temp_max=raw_range_row["raw_temp_max"],
        raw_press_min=raw_range_row["raw_press_min"],
        raw_press_max=raw_range_row["raw_press_max"],
        timestamps=timestamps_df,
        elapsed_s=elapsed,
    )


# ---------------------------------------------------------------------------
# Reducer: combine per-partition results into global, per-station results
# ---------------------------------------------------------------------------

class ResultAccumulator:
    def __init__(self, cfg: ScanConfig):
        self.cfg = cfg
        # Streaming per-station accumulators for mean/std (numerically stable
        # enough here given real4 precision of the source column).
        self._n = {}
        self._sum = {}
        self._sumsq = {}
        self._min = {}
        self._max = {}
        # Histogram: station -> np.array of length nbins + 2 (under/overflow)
        self._hist = {}
        self._anomaly_frames = []
        self._n_anomalies_total = 0
        self._partitions_done = 0
        # Coverage: (partition, station) -> raw record count
        self._coverage_rows = []
        # Diagnostic: TRUE min/max across the whole run, no physical filter
        # (see raw_range_query in scan_partition for why this matters).
        self._raw_temp_min = np.inf
        self._raw_temp_max = -np.inf
        self._raw_press_min = np.inf
        self._raw_press_max = -np.inf
        # Timestamps, one small DataFrame per partition, concatenated later.
        self._ts_frames = []

    def add(self, result: PartitionResult) -> None:
        for station, row in result.stats.iterrows():
            self._n[station] = self._n.get(station, 0) + row["n"]
            self._sum[station] = self._sum.get(station, 0.0) + row["sum_t"]
            self._sumsq[station] = self._sumsq.get(station, 0.0) + row["sumsq_t"]
            self._min[station] = min(self._min.get(station, np.inf), row["min_t"])
            self._max[station] = max(self._max.get(station, -np.inf), row["max_t"])

        nbins_total = self.cfg.hist_nbins + 2  # + underflow + overflow bins
        for station, sub in result.hist.groupby("idstation_pk"):
            arr = self._hist.setdefault(station, np.zeros(nbins_total, dtype=np.int64))
            bins = sub["bin"].to_numpy().astype(int)
            counts = sub["n"].to_numpy().astype(np.int64)
            np.add.at(arr, bins, counts)

        if len(result.anomalies) > 0:
            self._anomaly_frames.append(result.anomalies)
        self._n_anomalies_total += result.n_anomalies_total
        self._partitions_done += 1

        for station, n in result.coverage_counts.items():
            self._coverage_rows.append((result.partition, station, int(n)))

        if result.raw_temp_min is not None:
            self._raw_temp_min = min(self._raw_temp_min, result.raw_temp_min)
        if result.raw_temp_max is not None:
            self._raw_temp_max = max(self._raw_temp_max, result.raw_temp_max)
        if result.raw_press_min is not None:
            self._raw_press_min = min(self._raw_press_min, result.raw_press_min)
        if result.raw_press_max is not None:
            self._raw_press_max = max(self._raw_press_max, result.raw_press_max)

        if len(result.timestamps) > 0:
            self._ts_frames.append(result.timestamps)

    def summary_table(self) -> pd.DataFrame:
        rows = []
        for station in sorted(self._n):
            n = self._n[station]
            s = self._sum[station]
            sq = self._sumsq[station]
            mean = s / n if n else np.nan
            var = (sq / n - mean ** 2) if n else np.nan
            std = np.sqrt(var) if var and var > 0 else 0.0
            rows.append({
                "idstation_pk": station,
                "n_valid_samples": n,
                "mean_temp_K": mean,
                "std_temp_K": std,
                "min_temp_K": self._min[station],
                "max_temp_K": self._max[station],
            })
        df = pd.DataFrame(rows).set_index("idstation_pk")

        anomaly_counts = (
            pd.concat(self._anomaly_frames)["idstation_pk"].value_counts()
            if self._anomaly_frames else pd.Series(dtype=int)
        )
        df["n_anomalies_sampled"] = anomaly_counts.reindex(df.index).fillna(0).astype(int)
        df["pct_anomalies_of_valid"] = (
            100.0 * df["n_anomalies_sampled"] / df["n_valid_samples"].replace(0, np.nan)
        ).round(4)
        return df.sort_values("n_anomalies_sampled", ascending=False)

    def anomalies_table(self) -> pd.DataFrame:
        if not self._anomaly_frames:
            return pd.DataFrame(
                columns=["partition", "idstation_pk", "report_timestamp",
                         "temp", "press", "anomaly_reason"]
            )
        return pd.concat(self._anomaly_frames, ignore_index=True)

    def coverage_matrix(self) -> pd.DataFrame:
        """station (rows) x partition/year-month (columns) raw record counts,
        with any partition never observed for a given station filled as 0
        (rather than left as a gap the researcher has to notice)."""
        df = pd.DataFrame(self._coverage_rows, columns=["partition", "idstation_pk", "n"])
        matrix = df.pivot_table(index="idstation_pk", columns="partition",
                                 values="n", fill_value=0, aggfunc="sum")
        return matrix.reindex(sorted(matrix.columns), axis=1)

    def all_timestamps(self) -> pd.DataFrame:
        """idstation_pk, report_timestamp, press for every row with a
        non-null report_timestamp. One launch (report_timestamp) normally
        appears many times here, once per pressure level - that repetition
        is expected, not a duplicate."""
        if not self._ts_frames:
            return pd.DataFrame(columns=["idstation_pk", "report_timestamp", "press"])
        df = pd.concat(self._ts_frames, ignore_index=True)
        df["report_timestamp"] = pd.to_datetime(df["report_timestamp"], utc=True)
        return df

    def histograms(self) -> dict[int, np.ndarray]:
        return self._hist

    @property
    def n_anomalies_total(self) -> int:
        return self._n_anomalies_total

    @property
    def partitions_done(self) -> int:
        return self._partitions_done

    @property
    def raw_range(self) -> dict:
        """TRUE min/max of temp and press across everything scanned, with
        NO physical filter applied - diagnostic for spotting a unit
        mismatch (see raw_range_query in scan_partition)."""
        return {
            "temp_min": None if self._raw_temp_min == np.inf else self._raw_temp_min,
            "temp_max": None if self._raw_temp_max == -np.inf else self._raw_temp_max,
            "press_min": None if self._raw_press_min == np.inf else self._raw_press_min,
            "press_max": None if self._raw_press_max == -np.inf else self._raw_press_max,
        }


# ---------------------------------------------------------------------------
# Temporal continuity analysis
# ---------------------------------------------------------------------------

def analyze_continuity(
    ts_df: pd.DataFrame,
    relative_gap_factor: float = CONTINUITY_RELATIVE_GAP_FACTOR,
    absolute_gap_days: float = CONTINUITY_ABSOLUTE_GAP_DAYS,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Per-station temporal continuity check.

    IMPORTANT: the row-level uniqueness key of "data" is
    (idstation_pk, report_timestamp, press) - a single launch
    (report_timestamp) normally has many rows, one per pressure level.
    Repeated report_timestamp values are therefore NORMAL and are collapsed
    to one row per launch before computing inter-launch gaps. A genuine
    duplicate is the *same* (idstation_pk, report_timestamp, press) triplet
    appearing more than once.

    Returns three DataFrames:
      - continuity_summary: one row per station (span, cadence between
        launches, gap counts, duplicate-row count, estimated coverage
        fraction).
      - continuity_gaps: one row per individual gap between consecutive
        launches that was flagged, labeled as 'relative' (large compared to
        that station's own nominal cadence) and/or 'absolute'
        (>= absolute_gap_days regardless of cadence).
      - duplicate_rows: exact (station, timestamp, press) triplets that
        occur more than once - i.e. the same pressure level of the same
        launch stored more than once - which point at an ingestion bug
        rather than an atmospheric anomaly.
    """
    summary_rows = []
    gap_rows = []
    dup_rows = []

    for station, sub in ts_df.groupby("idstation_pk"):
        # Genuine row-level duplicates: same launch, same pressure level,
        # stored more than once.
        dup_counts = sub.groupby(["report_timestamp", "press"]).size()
        dups = dup_counts[dup_counts > 1]
        for (stamp, press), count in dups.items():
            dup_rows.append({
                "idstation_pk": station,
                "report_timestamp": stamp,
                "press": press,
                "n_occurrences": int(count),
            })

        # One row per LAUNCH (i.e. per distinct report_timestamp), since a
        # launch's many pressure levels must collapse to a single event for
        # inter-launch gap analysis.
        ts_unique = sub["report_timestamp"].drop_duplicates().sort_values().reset_index(drop=True)
        n_obs = len(ts_unique)

        if n_obs < 2:
            summary_rows.append({
                "idstation_pk": station,
                "n_launches": n_obs,
                "first_report": ts_unique.iloc[0] if n_obs else pd.NaT,
                "last_report": ts_unique.iloc[0] if n_obs else pd.NaT,
                "median_interval_hours": np.nan,
                "n_gaps_flagged": 0,
                "max_gap_days": 0.0,
                "n_duplicate_rows": int(len(dups)),
                "coverage_fraction_estimate": np.nan,
            })
            continue

        first_report = ts_unique.iloc[0]
        last_report = ts_unique.iloc[-1]
        deltas = ts_unique.diff().dropna()
        delta_hours = deltas.dt.total_seconds() / 3600.0
        median_interval_hours = float(delta_hours.median())

        relative_threshold_hours = relative_gap_factor * median_interval_hours
        absolute_threshold_hours = absolute_gap_days * 24.0

        n_gaps_flagged = 0
        max_gap_days = 0.0
        for i, gap_hours in enumerate(delta_hours):
            is_relative = median_interval_hours > 0 and gap_hours > relative_threshold_hours
            is_absolute = gap_hours > absolute_threshold_hours
            if is_relative or is_absolute:
                n_gaps_flagged += 1
                gap_days = gap_hours / 24.0
                max_gap_days = max(max_gap_days, gap_days)
                reasons = []
                if is_relative:
                    reasons.append("relative")
                if is_absolute:
                    reasons.append("absolute")
                gap_rows.append({
                    "idstation_pk": station,
                    "gap_start": ts_unique.iloc[i],
                    "gap_end": ts_unique.iloc[i + 1],
                    "gap_duration_days": round(gap_days, 2),
                    "station_median_interval_hours": round(median_interval_hours, 2),
                    "times_over_median": round(gap_hours / median_interval_hours, 1)
                                          if median_interval_hours > 0 else np.nan,
                    "reason": "+".join(reasons),
                })

        span_days = (last_report - first_report).total_seconds() / 86400.0
        expected_obs = span_days * 24.0 / median_interval_hours if median_interval_hours > 0 else np.nan
        coverage_fraction = n_obs / expected_obs if expected_obs and expected_obs > 0 else np.nan

        summary_rows.append({
            "idstation_pk": station,
            "n_launches": n_obs,
            "first_report": first_report,
            "last_report": last_report,
            "median_interval_hours": round(median_interval_hours, 2),
            "n_gaps_flagged": n_gaps_flagged,
            "max_gap_days": round(max_gap_days, 2),
            "n_duplicate_rows": int(len(dups)),
            "coverage_fraction_estimate": round(coverage_fraction, 4)
                                           if coverage_fraction == coverage_fraction else np.nan,
        })

    continuity_summary = pd.DataFrame(summary_rows).set_index("idstation_pk") \
        .sort_values("n_gaps_flagged", ascending=False)
    continuity_gaps = pd.DataFrame(gap_rows)
    if not continuity_gaps.empty:
        continuity_gaps = continuity_gaps.sort_values("gap_duration_days", ascending=False)
    duplicate_rows = pd.DataFrame(dup_rows)

    return continuity_summary, continuity_gaps, duplicate_rows


def plot_coverage_heatmap(coverage_matrix: pd.DataFrame, station_names: Optional[dict],
                           outpath: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = coverage_matrix.to_numpy()
    # Log-scale color mapping so both "zero" (missing month, most important
    # signal) and normal variability in launch counts remain visible.
    with np.errstate(divide="ignore"):
        log_data = np.where(data > 0, np.log10(data), np.nan)

    fig_height = max(4, 0.35 * len(coverage_matrix.index))
    fig_width = max(10, 0.06 * len(coverage_matrix.columns))
    fig, ax = plt.subplots(figsize=(fig_width, fig_height))

    cmap = plt.get_cmap("viridis").copy()
    cmap.set_bad(color="firebrick")  # missing months stand out in red
    im = ax.imshow(log_data, aspect="auto", cmap=cmap)

    ax.set_yticks(range(len(coverage_matrix.index)))
    labels = [
        f"{sid} - {station_names.get(sid, '')}" if station_names else str(sid)
        for sid in coverage_matrix.index
    ]
    ax.set_yticklabels(labels, fontsize=7)

    n_cols = len(coverage_matrix.columns)
    step = max(1, n_cols // 40)
    ax.set_xticks(range(0, n_cols, step))
    ax.set_xticklabels(coverage_matrix.columns[::step], rotation=90, fontsize=6)

    ax.set_title(
        "GRUAN data availability per station and month\n"
        "Color = log10(record count); red = month with zero records",
        fontsize=11,
    )
    fig.colorbar(im, ax=ax, label="log10(records)")
    fig.tight_layout()
    fig.savefig(outpath, dpi=150)
    plt.close(fig)
    log.info("Coverage heatmap written to %s", outpath)


# ---------------------------------------------------------------------------
# Plotting: temperature histograms
# ---------------------------------------------------------------------------

def plot_histograms(histograms: dict[int, np.ndarray], cfg: ScanConfig,
                     station_names: Optional[dict] = None, outpath: str = "histograms.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    edges = np.linspace(cfg.hist_min, cfg.hist_max, cfg.hist_nbins + 1)
    centers = (edges[:-1] + edges[1:]) / 2.0

    stations = sorted(histograms)
    ncols = 6
    nrows = math.ceil(len(stations) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4 * ncols, 3 * nrows), squeeze=False)

    for i, station in enumerate(stations):
        ax = axes[i // ncols][i % ncols]
        arr = histograms[station]
        underflow, in_range, overflow = arr[0], arr[1:-1], arr[-1]
        ax.bar(centers, in_range, width=(edges[1] - edges[0]), color="steelblue",
               edgecolor="none")
        ax.set_yscale("log")
        title = station_names.get(station, str(station)) if station_names else str(station)
        anomaly_note = ""
        if underflow or overflow:
            anomaly_note = f"\nunderflow={underflow}, overflow={overflow}"
        ax.set_title(f"Station {title}{anomaly_note}", fontsize=9)
        ax.set_xlabel("Temperature (K)", fontsize=8)
        ax.set_ylabel("Count (log)", fontsize=8)
        ax.tick_params(labelsize=7)

    for j in range(len(stations), nrows * ncols):
        axes[j // ncols][j % ncols].axis("off")

    fig.suptitle(
        "GRUAN per-station temperature histograms (all pressure levels combined)\n"
        f"Bin range [{cfg.hist_min}, {cfg.hist_max}] K, {cfg.hist_nbins} bins; "
        "log-scale y-axis to expose rare anomalies",
        fontsize=12,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(outpath, dpi=150)
    plt.close(fig)
    log.info("Histogram figure written to %s", outpath)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env-file", default=".env",
                    help="Path to the .env file holding DB_USER, "
                         "GRUAN_USER_PSW, DB_HOST, DB_PORT, DB_NAME. "
                         "Connection parameters are never passed on the "
                         "command line.")
    p.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 4),
                    help="Number of parallel worker processes / DB connections.")
    p.add_argument("--partitions-limit", type=int, default=None,
                    help="Only scan the first N discovered partitions (smoke test).")
    p.add_argument("--month", type=str, default=None,
                    help="Only scan a single monthly partition, given as "
                         "YYYYMM (e.g. --month 202301 scans only "
                         "'data_202301'). Useful to isolate/debug anomalies "
                         "on one month before committing to a full scan. "
                         "Takes precedence over --partitions-limit.")
    p.add_argument("--outdir", default="./gruan_qc_output")
    p.add_argument("--temp-min-physical", type=float, default=DEFAULT_TEMP_MIN_PHYSICAL)
    p.add_argument("--temp-max-physical", type=float, default=DEFAULT_TEMP_MAX_PHYSICAL)
    p.add_argument("--press-min-physical", type=float, default=DEFAULT_PRESS_MIN_PHYSICAL)
    p.add_argument("--press-max-physical", type=float, default=DEFAULT_PRESS_MAX_PHYSICAL)
    p.add_argument("--hist-min", type=float, default=DEFAULT_HIST_MIN)
    p.add_argument("--hist-max", type=float, default=DEFAULT_HIST_MAX)
    p.add_argument("--hist-nbins", type=int, default=DEFAULT_HIST_NBINS)
    p.add_argument("--continuity-relative-gap-factor", type=float,
                    default=CONTINUITY_RELATIVE_GAP_FACTOR,
                    help="Flag a gap if it exceeds this multiple of the "
                         "station's own median inter-launch interval.")
    p.add_argument("--continuity-absolute-gap-days", type=float,
                    default=CONTINUITY_ABSOLUTE_GAP_DAYS,
                    help="Always flag a gap of at least this many days, "
                         "regardless of the station's nominal cadence.")
    return p.parse_args()


def load_dsn_from_env(env_file: str) -> dict:
    """Build the psycopg2 connection dict from a .env file.

    Expected variable names (matching the project's existing .env):
        DB_USER, GRUAN_USER_PSW, DB_HOST, DB_PORT, DB_NAME
    """
    if os.path.isfile(env_file):
        load_dotenv(dotenv_path=env_file, override=False)
    else:
        log.warning(".env file not found at '%s' - falling back to whatever "
                    "is already present in the process environment.", env_file)

    required = ["DB_USER", "GRUAN_USER_PSW", "DB_HOST", "DB_PORT", "DB_NAME"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"Missing required DB connection variable(s) {missing} - check "
            f"'{env_file}' or the process environment."
        )

    return dict(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["GRUAN_USER_PSW"],
    )


def fetch_station_metadata(dsn: dict) -> pd.DataFrame:
    """Load station metadata, keyed by station.id (== data.idstation_pk)."""
    query = text("""
        SELECT id, idstation, name, network, wmoid, latitude, longitude, elevation
        FROM station
    """)
    try:
        engine = get_engine(dsn)
        with engine.connect() as conn:
            df = pd.read_sql_query(query, conn)
        return df.set_index("id")
    except Exception as exc:  # pragma: no cover - best-effort only
        log.warning("Could not fetch station metadata from 'station': %s", exc)
        return pd.DataFrame(columns=["idstation", "name", "network", "wmoid",
                                      "latitude", "longitude", "elevation"])


def main() -> None:
    args = parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    dsn = load_dsn_from_env(args.env_file)
    log.info("Connecting to %s:%s/%s as %s (credentials loaded from %s)",
              dsn["host"], dsn["port"], dsn["dbname"], dsn["user"], args.env_file)

    cfg = ScanConfig(
        dsn=dsn,
        temp_min_physical=args.temp_min_physical,
        temp_max_physical=args.temp_max_physical,
        press_min_physical=args.press_min_physical,
        press_max_physical=args.press_max_physical,
        hist_min=args.hist_min,
        hist_max=args.hist_max,
        hist_nbins=args.hist_nbins,
    )

    log.info("Discovering data_YYYYMM partitions ...")
    partitions = list_data_partitions(dsn)

    if args.month:
        if not re.match(r"^\d{6}$", args.month):
            raise SystemExit(
                f"--month must be YYYYMM (6 digits), got: {args.month!r}"
            )
        target = f"data_{args.month}"
        if target not in partitions:
            raise SystemExit(
                f"Partition '{target}' not found. Available partitions: "
                f"{partitions[0]}..{partitions[-1]} ({len(partitions)} total)."
            )
        partitions = [target]
        log.info("--month %s given: restricting scan to partition '%s' only.",
                  args.month, target)
    elif args.partitions_limit:
        partitions = partitions[: args.partitions_limit]

    log.info("Will scan %d partitions with %d parallel workers.",
              len(partitions), args.workers)

    accumulator = ResultAccumulator(cfg)
    t_start = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(scan_partition, partition, cfg): partition
            for partition in partitions
        }
        iterator = as_completed(futures)
        if tqdm is not None:
            iterator = tqdm(iterator, total=len(futures), desc="Scanning partitions")
        for future in iterator:
            partition = futures[future]
            try:
                result = future.result()
            except Exception:
                log.exception("Partition %s failed - skipping it.", partition)
                continue
            accumulator.add(result)

    elapsed = time.time() - t_start
    log.info(
        "Scan complete: %d/%d partitions processed in %.1f s "
        "(%d total anomalous records found across the DB).",
        accumulator.partitions_done, len(partitions), elapsed,
        accumulator.n_anomalies_total,
    )

    # --- Unit-mismatch diagnostic -------------------------------------------
    # Printed BEFORE the QC results below because if this looks wrong,
    # every anomaly count downstream is likely an artifact of the wrong
    # --temp-*-physical / --press-*-physical bounds, not real bad data.
    raw = accumulator.raw_range
    print("\n=== Raw value range across scanned partition(s) (NO physical filter) ===")
    print(f"  temp : min={raw['temp_min']!r}  max={raw['temp_max']!r}   "
          f"(expected roughly within [{cfg.temp_min_physical}, {cfg.temp_max_physical}] "
          f"if the column is really in Kelvin)")
    print(f"  press: min={raw['press_min']!r}  max={raw['press_max']!r}   "
          f"(expected roughly within [{cfg.press_min_physical}, {cfg.press_max_physical}] "
          f"if the column is really in Pa)")
    if raw["temp_min"] is not None and raw["temp_max"] is not None:
        if raw["temp_max"] < 100.0:
            print("  WARNING: raw temp max is well under 100 - this column looks like "
                  "it's in degrees Celsius, not Kelvin. That alone would make almost "
                  "every row fail the [150, 340] K physical-range filter and show up "
                  "as an anomaly. Consider adding 273.15 in the query, or rerunning "
                  "with --temp-min-physical / --temp-max-physical set for degC.")
    if raw["press_min"] is not None and raw["press_max"] is not None:
        if raw["press_max"] < 2000.0:
            print("  WARNING: raw press max is well under 2000 - this column looks like "
                  "it's in hPa (millibar), not Pa. Consider multiplying by 100 in the "
                  "query, or rerunning with --press-min-physical / --press-max-physical "
                  "set for hPa (e.g. 0 and 1100).")

    station_meta = fetch_station_metadata(dsn)
    station_names = station_meta["name"].to_dict() if not station_meta.empty else {}

    # --- Temperature / QC outputs ------------------------------------------
    summary_df = accumulator.summary_table()
    summary_df = summary_df.join(station_meta[["idstation", "name", "network", "wmoid"]],
                                  how="left") if not station_meta.empty else summary_df
    summary_path = os.path.join(args.outdir, "per_station_summary.csv")
    summary_df.to_csv(summary_path)
    log.info("Per-station summary written to %s", summary_path)
    print("\n=== Per-station QC summary (worst offenders first) ===")
    print(summary_df.to_string())

    anomalies_df = accumulator.anomalies_table()
    anomalies_path = os.path.join(args.outdir, "anomalies_detail.csv")
    anomalies_df.to_csv(anomalies_path, index=False)
    log.info("Detailed anomaly listing (%d sampled rows) written to %s",
              len(anomalies_df), anomalies_path)

    if not anomalies_df.empty:
        print("\n=== Anomaly reason breakdown (sampled rows) ===")
        print(anomalies_df["anomaly_reason"].value_counts().to_string())

    hist_path = os.path.join(args.outdir, "temperature_histograms_per_station.png")
    plot_histograms(accumulator.histograms(), cfg, station_names, hist_path)

    # --- Temporal continuity outputs ---------------------------------------
    coverage_matrix = accumulator.coverage_matrix()
    coverage_path = os.path.join(args.outdir, "coverage_matrix_station_x_month.csv")
    coverage_matrix.to_csv(coverage_path)
    log.info("Station x month coverage matrix written to %s", coverage_path)

    heatmap_path = os.path.join(args.outdir, "coverage_heatmap.png")
    plot_coverage_heatmap(coverage_matrix, station_names, heatmap_path)

    n_zero_months = int((coverage_matrix == 0).sum().sum())
    if n_zero_months:
        print(f"\n{n_zero_months} (station, month) cells with ZERO records "
              f"found - see {heatmap_path} and {coverage_path}.")

    ts_df = accumulator.all_timestamps()
    continuity_summary, continuity_gaps, duplicate_rows = analyze_continuity(
        ts_df,
        relative_gap_factor=args.continuity_relative_gap_factor,
        absolute_gap_days=args.continuity_absolute_gap_days,
    )
    if not station_meta.empty:
        continuity_summary = continuity_summary.join(
            station_meta[["idstation", "name", "network", "wmoid"]], how="left"
        )

    continuity_summary_path = os.path.join(args.outdir, "continuity_summary.csv")
    continuity_summary.to_csv(continuity_summary_path)
    print("\n=== Temporal continuity summary (most gaps first) ===")
    print(continuity_summary.to_string())

    continuity_gaps_path = os.path.join(args.outdir, "continuity_gaps.csv")
    continuity_gaps.to_csv(continuity_gaps_path, index=False)
    log.info("%d individual gaps flagged, written to %s",
              len(continuity_gaps), continuity_gaps_path)

    duplicates_path = os.path.join(args.outdir, "duplicate_rows.csv")
    duplicate_rows.to_csv(duplicates_path, index=False)
    if not duplicate_rows.empty:
        log.warning("%d duplicate (idstation_pk, report_timestamp, press) "
                    "rows found - written to %s. These are TRUE duplicates "
                    "of the same pressure level of the same launch, and "
                    "typically point at an ingestion bug rather than a "
                    "genuine atmospheric anomaly.",
                    len(duplicate_rows), duplicates_path)


    print(f"\nAll outputs written to: {os.path.abspath(args.outdir)}")


if __name__ == "__main__":
    main()