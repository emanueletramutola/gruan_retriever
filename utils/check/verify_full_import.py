import os
import re
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

# ==========================================================
# ENVIRONMENT SETUP
# ==========================================================
project_root = Path(__file__).resolve().parent.parent
os.chdir(project_root)
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from readers.netcdf_reader import read_netcdf
from processors.sounding_diagnostics import enrich_data_with_diagnostics
from converters.dataframe_converter import DataFrameConverter
from config import DB_CONFIG

# ==========================================================
# CONFIGURATION & TEST CASES
# ==========================================================
# Define all files to be validated in a single run
TEST_CASES = [
    {
        "sonde_type": "RS41",
        "jar_path": "/Data/GRUAN_TEST/RS41/LIN-RS-01_2_RS41-GDP_001_20260323T060000_1-000-001_AI1163482_rev001.jar"
    },
    {
        "sonde_type": "RS92",
        "jar_path": "/Data/GRUAN_TEST/RS92/LIN-RS-01_2_RS92-GDP_002_20240528T212000_1-002-001_AI1021532.jar"
    },
    {
        "sonde_type": "RS-11G",
        "jar_path": "/Data/GRUAN_TEST/RS-11G/SYO-RS-01_2_RS-11G-GDP_001_20250227T120000_1-000-001_AI1131479_rev001.jar"
    },
    {
        "sonde_type": "IMS-100",
        "jar_path": "/Data/GRUAN_TEST/IMS-100/SYO-RS-01_2_IMS-100-GDP_002_20260322T000000_1-000-001_AI1163461_rev001.jar"
    }
]

# Standard cleaner for database column names[cite: 5, 11]
COLUMN_CLEANER = str.maketrans({'.': '_', '-': '_', ' ': '_'})


# ----------------------------------------------------------
# UTILITIES
# ----------------------------------------------------------

def try_parse_float(value):
    """
    Extracts a float from mixed types (strings with units, MetPy arrays, etc.).
    Returns None if conversion is not possible.[cite: 11]
    """
    if isinstance(value, (np.ndarray, list)):
        if len(np.atleast_1d(value)) > 0:
            value = np.atleast_1d(value)[0]
        else:
            return None

    if value is None or pd.isna(value):
        return None

    if isinstance(value, (int, float, np.number)):
        return float(value)

    if isinstance(value, str):
        clean_val = re.sub(r'[^0-9.\-]', '', value.split(' ')[0])
        try:
            return float(clean_val)
        except ValueError:
            return None
    return None


def get_sqlalchemy_engine():
    """Creates and returns the SQLAlchemy engine based on project config.[cite: 11]"""
    url = f"postgresql://{DB_CONFIG['user']}:{DB_CONFIG['password']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['dbname']}"
    return create_engine(url)


# ----------------------------------------------------------
# CORE VALIDATION LOGIC
# ----------------------------------------------------------

def validate_sounding(case, engine, db_header_cols, db_data_cols):
    """
    Performs full validation for a single test case.
    Compares local NetCDF processing results with database records.[cite: 11]
    """
    jar_path = Path(case["jar_path"])
    sonde_type = case["sonde_type"]

    if not jar_path.exists():
        print(f"\n[!] SKIP: File not found: {jar_path}")
        return False

    print(f"\n>>> VALIDATING {sonde_type}: {jar_path.name}")
    print("-" * 60)

    converter = DataFrameConverter(sonde_type, COLUMN_CLEANER, db_header_cols,
                                   db_data_cols)

    try:
        with zipfile.ZipFile(jar_path, 'r') as zip_ref:
            nc_files = [f for f in zip_ref.namelist() if f.endswith('.nc')]
            if not nc_files:
                print(f"Error: No NetCDF found in JAR")
                return False

            internal_nc_path = nc_files[0]
            data, metadata = read_netcdf(zip_ref, internal_nc_path)

            # --- RH Scaling Logic (CF-1.4 / RS-11G compatibility)[cite: 11] ---
            conventions = metadata.get('Conventions', '')
            if 'rh' in data:
                raw_rh = np.array(data['rh'])
                if (conventions in ['CF-1.4', 'RS-11G']) or (
                        np.nanmax(raw_rh) <= 1.1):
                    data['rh'] = raw_rh * 100.0

            # Map specific wind components for older formats if necessary[cite: 11]
            if 'u' in data: data['wzon'] = data['u']
            if 'v' in data: data['wmeri'] = data['v']

            # Process diagnostics[cite: 5, 11]
            data, metadata = enrich_data_with_diagnostics(data, metadata)

            # Ensure metadata values are scalars for comparison[cite: 11]
            for k, v in metadata.items():
                if isinstance(v, (np.ndarray, list)) and len(
                        np.atleast_1d(v)) > 0:
                    metadata[k] = np.atleast_1d(v)[0]

            # Determine lookup keys[cite: 4, 11]
            station_code = metadata.get('g.Site.Key') or metadata.get(
                'g.General.SiteCode') or jar_path.name.split('-')[0]
            report_ts_str = metadata.get(
                'g.Measurement.StandardTime') or metadata.get(
                'g.Ascent.StandardTime')
            report_ts = pd.to_datetime(report_ts_str)

    except Exception as e:
        print(f"FAILED TO PROCESS NETCDF: {e}")
        return False

    # Database Lookup[cite: 11]
    with engine.connect() as conn:
        station_res = conn.execute(
            text("SELECT id FROM station WHERE idstation = :code"),
            {"code": station_code}).fetchone()
        if not station_res:
            print(f"ERROR: Station {station_code} not found in DB.")
            return False

        station_pk = int(station_res[0])
        df_header_db = pd.read_sql(text(
            "SELECT * FROM header WHERE idstation_pk = :pk AND report_timestamp = :ts"),
            conn,
            params={"pk": station_pk, "ts": report_ts})
        df_data_db = pd.read_sql(text(
            "SELECT * FROM data WHERE idstation_pk = :pk AND report_timestamp = :ts ORDER BY observation_id"),
            conn,
            params={"pk": station_pk, "ts": report_ts})

    if df_header_db.empty:
        print(f"ERROR: No DB records found for {station_code} at {report_ts}")
        return False

    # 1. Profile Data Validation[cite: 11]
    print(f"Validating 'data' table ({len(df_data_db)} rows)...")
    d_ok, d_err = 0, 0
    for col, vals in data.items():
        clean = converter.clean_column_name(col)
        if clean in df_data_db.columns:
            exp = np.array(vals, dtype=float)
            act = np.array(df_data_db[clean].values, dtype=float)

            mask = np.isfinite(exp) & np.isfinite(act)
            if not np.any(mask): continue

            if np.allclose(exp[mask], act[mask], atol=1e-2):
                d_ok += 1
            else:
                d_err += 1
                diff = np.nanmax(np.abs(exp[mask] - act[mask]))
                print(
                    f"   [!] Discrepancy in data.{clean} (Max Diff: {diff:.4f})")

    # 2. Metadata (Header) Validation[cite: 11]
    print(f"Validating 'header' table...")
    m_ok, m_err = 0, 0
    for key, val in metadata.items():
        clean = converter.clean_column_name(key)
        if clean in df_header_db.columns:
            actual = df_header_db[clean].iloc[0]
            num_exp, num_act = try_parse_float(val), try_parse_float(actual)

            is_match = False
            if num_exp is None and num_act is None:
                is_match = True
            elif num_exp is not None and num_act is not None:
                is_match = np.isclose(num_exp, num_act, atol=1e-4)
            elif any(sub in clean.lower() for sub in
                     ["time", "timestamp", "datetime"]):
                try:
                    is_match = abs((pd.to_datetime(val).replace(
                        tzinfo=None) - pd.to_datetime(actual).replace(
                        tzinfo=None)).total_seconds()) < 1.0
                except:
                    is_match = str(val).strip() == str(actual).strip()
            else:
                is_match = str(val).strip() == str(actual).strip()

            if is_match:
                m_ok += 1
            else:
                m_err += 1
                print(
                    f"   [!] DISCREPANCY in header.{clean} -> Exp: {val} | DB: {actual}")

    print(
        f"RESULT: Profile {d_ok} OK, {d_err} ERR | Header {m_ok} OK, {m_err} ERR")
    return d_err == 0 and m_err == 0


# ----------------------------------------------------------
# MAIN EXECUTION
# ----------------------------------------------------------

def run_all_tests():
    """Main entry point to run all test cases sequentially."""
    print("=" * 60)
    print("GRUAN IMPORT FULL VALIDATION SUITE")
    print("=" * 60)

    engine = get_sqlalchemy_engine()

    # Fetch DB schema once to optimize[cite: 11]
    with engine.connect() as conn:
        db_header_cols = pd.read_sql(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'header'"),
            conn)['column_name'].tolist()
        db_data_cols = pd.read_sql(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'data'"),
            conn)['column_name'].tolist()

    overall_success = True
    for case in TEST_CASES:
        success = validate_sounding(case, engine, db_header_cols, db_data_cols)
        if not success:
            overall_success = False

    print("\n" + "=" * 60)
    if overall_success:
        print("✅ GLOBAL SUCCESS: All soundings match the database.")
    else:
        print("❌ GLOBAL FAILURE: One or more soundings have discrepancies.")
    print("=" * 60)

    engine.dispose()


if __name__ == "__main__":
    run_all_tests()
