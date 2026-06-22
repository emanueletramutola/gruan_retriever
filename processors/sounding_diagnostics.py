"""
sounding_diagnostics.py
=======================
Thermodynamic and dynamic diagnostics computed from a GRUAN radiosonde
sounding using MetPy.

The module exposes a single public function:

    enrich_data_with_diagnostics(data, metadata)

which reads the relevant profile arrays from the ``data`` dict, computes
all diagnostics, and writes:

- **Profile variables** (1-D arrays of the same length as the sounding)
  back into ``data``.
- **Scalar variables** (single value per sounding) into ``metadata``.

Variable mapping from the GRUAN NetCDF / DB schema
----------------------------------------------------
- Pressure      → ``data['press']``   (hPa)
- Temperature   → ``data['temp']``    (K)
- Rel. humidity → ``data['rh']``      (0–100 %)
- U-wind (zonal) → ``data['wzon']``   (m/s)
- V-wind (merid.)→ ``data['wmeri']``  (m/s)
- Altitude      → ``data['alt']``     (m above launch point)
"""

import logging

import numpy as np
from scipy.ndimage import uniform_filter1d

from metpy.units import units
import metpy.calc as mpcalc

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal utilities
# ---------------------------------------------------------------------------

def _smooth(x, n=5):
    """Apply a uniform (boxcar) 1-D smoothing filter of width *n*."""
    return uniform_filter1d(x, size=n, mode='nearest')


def _ensure_monotonic(p, *args):
    """Sort all arrays so that pressure is monotonically decreasing."""
    idx = np.argsort(p)[::-1]
    return (p[idx],) + tuple(a[idx] for a in args)


# ---------------------------------------------------------------------------
# Core thermodynamics
# ---------------------------------------------------------------------------

def compute_basic_thermo(p, T, RH):
    Td = mpcalc.dewpoint_from_relative_humidity(T, RH)
    theta = mpcalc.potential_temperature(p, T)
    theta_e = mpcalc.equivalent_potential_temperature(p, T, Td)
    return Td, theta, theta_e


# ---------------------------------------------------------------------------
# Convection & parcels
# ---------------------------------------------------------------------------

def compute_parcel_diagnostics(p, T, Td):
    parcel_prof = mpcalc.parcel_profile(p, T[0], Td[0])

    lcl_p, lcl_T = mpcalc.lcl(p[0], T[0], Td[0])
    lfc_p, lfc_T = mpcalc.lfc(p, T, Td)
    el_p, el_T = mpcalc.el(p, T, Td)

    cape, cin = mpcalc.cape_cin(p, T, Td, parcel_prof)

    return {
        "parcel_profile": parcel_prof,
        "LCL": (lcl_p, lcl_T),
        "LFC": (lfc_p, lfc_T),
        "EL": (el_p, el_T),
        "CAPE": cape,
        "CIN": cin,
    }


# ---------------------------------------------------------------------------
# Instability indices
# ---------------------------------------------------------------------------

def compute_indices(p, T, Td):
    # Ensure all profile arrays are strictly 1-D pint Quantities.
    # np.squeeze works correctly on pint Quantities (unlike .squeeze() which
    # may not propagate units in all MetPy/pint versions).
    p   = np.squeeze(p)
    T   = np.squeeze(T)
    Td  = np.squeeze(Td)
    parcel_prof = np.squeeze(mpcalc.parcel_profile(p, T[0], Td[0]))

    logger.debug(
        "compute_indices shapes — p:%s T:%s Td:%s parcel_prof:%s",
        np.shape(p), np.shape(T), np.shape(Td), np.shape(parcel_prof),
    )

    result = {}

    try:
        result["LiftedIndex"] = mpcalc.lifted_index(p, T, parcel_prof)
    except Exception as e:
        logger.warning("compute_indices LiftedIndex failed (%s: %s) — shapes p:%s T:%s parcel:%s",
                       type(e).__name__, e, np.shape(p), np.shape(T), np.shape(parcel_prof))
        result["LiftedIndex"] = None

    try:
        result["KIndex"] = mpcalc.k_index(p, T, Td)
    except Exception as e:
        logger.warning("compute_indices KIndex failed (%s: %s) — shapes p:%s T:%s Td:%s",
                       type(e).__name__, e, np.shape(p), np.shape(T), np.shape(Td))
        result["KIndex"] = None

    try:
        result["TotalTotals"] = mpcalc.total_totals_index(p, T, Td)
    except Exception as e:
        logger.warning("compute_indices TotalTotals failed (%s: %s) — shapes p:%s T:%s Td:%s",
                       type(e).__name__, e, np.shape(p), np.shape(T), np.shape(Td))
        result["TotalTotals"] = None

    try:
        result["Showalter"] = mpcalc.showalter_index(p, T, Td)
    except Exception as e:
        logger.warning(
            "compute_indices: mpcalc.showalter_index failed (%s: %s) — "
            "falling back to manual computation",
            type(e).__name__, e,
        )
        # mpcalc.showalter_index internally produces a 2-D parcel_profile
        # that clashes with 1-D inputs in some MetPy versions.
        # Manual implementation: lift a parcel from 850 hPa to 500 hPa and
        # compare its temperature with the environment at 500 hPa.
        try:
            # Use np.interp for interpolation (pressure is decreasing,
            # so reverse arrays; np.interp requires increasing xp).
            p_mag  = p.magnitude
            T_mag  = T.magnitude
            Td_mag = Td.magnitude

            T850_mag  = float(np.interp(850.0, p_mag[::-1], T_mag[::-1]))
            Td850_mag = float(np.interp(850.0, p_mag[::-1], Td_mag[::-1]))
            T500_mag  = float(np.interp(500.0, p_mag[::-1], T_mag[::-1]))

            T850_q  = T850_mag  * T.units
            Td850_q = Td850_mag * Td.units

            # Sub-profile 850 → 500 hPa for parcel lifting
            mask    = (p_mag <= 850.0) & (p_mag >= 500.0)
            p_sub   = np.squeeze(p[mask])
            parcel_sub = np.squeeze(mpcalc.parcel_profile(p_sub, T850_q, Td850_q))

            p_sub_mag      = p_sub.magnitude
            parcel_sub_mag = parcel_sub.magnitude
            T500_parcel_mag = float(np.interp(500.0, p_sub_mag[::-1], parcel_sub_mag[::-1]))

            result["Showalter"] = (T500_mag - T500_parcel_mag) * T.units
        except Exception as e2:
            logger.warning("compute_indices Showalter manual fallback also failed (%s: %s)",
                           type(e2).__name__, e2)
            result["Showalter"] = None

    return result


# ---------------------------------------------------------------------------
# Tropopause
# ---------------------------------------------------------------------------

def compute_tropopause(p, T):
    """
    Detect the thermal tropopause following the WMO definition:
    the lowest level above 500 hPa where the lapse rate drops to
    <= 2 K/km, provided the average lapse rate between that level
    and all levels within 2 km above it also does not exceed 2 K/km.

    Falls back to mpcalc.tropopause if available (newer MetPy versions).
    Returns (pressure, temperature) as pint Quantities, or (None, None).
    """
    # Try the native MetPy function first (MetPy >= 1.5)
    if hasattr(mpcalc, 'tropopause'):
        return mpcalc.tropopause(p, T)

    # Manual WMO thermal tropopause
    try:
        p_mag = np.asarray(p.magnitude, dtype=float)
        T_mag = np.asarray(T.magnitude, dtype=float)

        # Work on a copy sorted surface → top (decreasing pressure)
        order  = np.argsort(p_mag)[::-1]
        p_s    = p_mag[order]
        T_s    = T_mag[order]

        # Convert temperature to Kelvin if in degC (offset unit check)
        try:
            T_K = T_s + 273.15 if str(T.units) in ('degC', 'degree_Celsius') else T_s
        except Exception:
            T_K = T_s

        # Build geometric height via hypsometric approximation (in km)
        # z_{i+1} = z_i + (Rd/g) * T_mean * ln(p_i/p_{i+1})
        Rd_over_g = 29.27       # m/K  →  /1000 for km
        z_km = np.zeros_like(p_s)
        for i in range(1, len(p_s)):
            T_mean    = 0.5 * (T_K[i - 1] + T_K[i])
            dz        = (Rd_over_g * T_mean * np.log(p_s[i - 1] / p_s[i])) / 1000.0
            z_km[i]   = z_km[i - 1] + dz

        LAPSE_CRIT = 2.0   # K/km
        P_MIN      = 500.0 # hPa – tropopause must be above this level

        for i in range(1, len(p_s) - 1):
            if p_s[i] > P_MIN:
                continue

            dz = z_km[i] - z_km[i - 1]
            if dz <= 0:
                continue

            lapse = -(T_K[i] - T_K[i - 1]) / dz  # positive = cooling with height

            if lapse <= LAPSE_CRIT:
                # Check average lapse rate over the 2 km above this level
                z_top  = z_km[i] + 2.0
                j      = i
                while j < len(p_s) - 1 and z_km[j] < z_top:
                    j += 1
                dz2 = z_km[j] - z_km[i]
                if dz2 <= 0:
                    continue
                avg_lapse = -(T_K[j] - T_K[i]) / dz2
                if avg_lapse <= LAPSE_CRIT:
                    trop_p = p_s[i] * p.units
                    trop_T = T_s[i] * T.units
                    return trop_p, trop_T

    except Exception as exc:
        logger.warning("compute_tropopause manual algorithm failed (%s: %s)", type(exc).__name__, exc)

    return None, None


# ---------------------------------------------------------------------------
# Wind diagnostics
# ---------------------------------------------------------------------------

def compute_wind(p, z, u, v):
    shear_0_1 = mpcalc.bulk_shear(p, u, v, height=z, depth=1000 * units.m)
    shear_0_3 = mpcalc.bulk_shear(p, u, v, height=z, depth=3000 * units.m)
    shear_0_6 = mpcalc.bulk_shear(p, u, v, height=z, depth=6000 * units.m)
    srh_0_3 = mpcalc.storm_relative_helicity(z, u, v, depth=3000 * units.m)

    return {
        "Shear_0_1km": shear_0_1,
        "Shear_0_3km": shear_0_3,
        "Shear_0_6km": shear_0_6,
        "SRH_0_3km": srh_0_3,
    }


# ---------------------------------------------------------------------------
# Other diagnostics
# ---------------------------------------------------------------------------

def compute_misc(p, T, Td):
    pwat = mpcalc.precipitable_water(p, Td)

    freezing_idx = np.where(T <= 0 * units.degC)[0]
    freezing_level = int(freezing_idx[0]) if len(freezing_idx) > 0 else None

    return {
        "PWAT": pwat,
        "FreezingLevelIndex": freezing_level,
    }


# ---------------------------------------------------------------------------
# Cloud base height
# ---------------------------------------------------------------------------

def _cloud_base_low(z, RH, zmax=3000 * units.m, rh_crit=0.95):
    for zi, rhi in zip(z, RH):
        if zi <= zmax and rhi >= rh_crit:
            return zi
    return None


def _cloud_base_zhang(z, RH, T, zmin=4000 * units.m, rh_min=0.75):
    RHs = _smooth(RH.magnitude, 5)
    for i in range(1, len(z) - 1):
        if z[i] > zmin:
            if RHs[i] > RHs[i - 1] and RHs[i] > RHs[i + 1]:
                if RHs[i] >= rh_min:
                    return z[i]
    return None


def _cloud_base_height(z, RH, T):
    cb_low = _cloud_base_low(z, RH)
    if cb_low is not None:
        return cb_low, "low cloud (RH)"
    cb_high = _cloud_base_zhang(z, RH, T)
    if cb_high is not None:
        return cb_high, "high cloud (Zhang)"
    return None, "clear"


# ---------------------------------------------------------------------------
# Master diagnostic function (colleague's original interface)
# ---------------------------------------------------------------------------

def full_sounding_diagnostics(p, T, RH, u, v, z):
    """
    Compute all sounding diagnostics.

    Parameters
    ----------
    p, T, RH, u, v, z : pint Quantity arrays
        Pressure (hPa), Temperature (°C), Relative humidity (fraction 0-1),
        U-wind (m/s), V-wind (m/s), Altitude (m).

    Returns
    -------
    dict
        Nested dictionary with keys: Thermo, Parcel, Indices, Tropopause,
        Wind, Misc, CloudBase.
    """
    # Ensure monotonically decreasing pressure
    p, T, RH, u, v, z = _ensure_monotonic(p, T, RH, u, v, z)

    Td, theta, theta_e = compute_basic_thermo(p, T, RH)
    parcel = compute_parcel_diagnostics(p, T, Td)
    indices = compute_indices(p, T, Td)
    trop = compute_tropopause(p, T)
    wind = compute_wind(p, z, u, v)
    misc = compute_misc(p, T, Td)
    cbh, cbh_type = _cloud_base_height(z, RH, T)

    return {
        "Thermo": {"Td": Td, "Theta": theta, "Theta_e": theta_e},
        "Parcel": parcel,
        "Indices": indices,
        "Tropopause": trop,
        "Wind": wind,
        "Misc": misc,
        "CloudBase": {"Height": cbh, "Type": cbh_type},
    }


# ---------------------------------------------------------------------------
# Integration helper
# ---------------------------------------------------------------------------

def _magnitude_or_none(q):
    """Return the plain numeric magnitude of a MetPy Quantity, or None."""
    if q is None:
        return None
    try:
        return float(q.magnitude)
    except Exception:
        return None


def _magnitude_array(q):
    """Return the plain numpy array of a MetPy Quantity."""
    try:
        return np.asarray(q.magnitude, dtype=np.float32)
    except Exception:
        return np.asarray(q, dtype=np.float32)


def enrich_data_with_diagnostics(data, metadata):
    """
    Compute sounding diagnostics and add them to *data* and *metadata*.

    Profile variables (1-D arrays) are added to *data*; scalar diagnostics
    are added to *metadata*.  If any required variable is missing or the
    computation fails the original dicts are returned unchanged.

    Parameters
    ----------
    data : dict
        Profile arrays keyed by GRUAN/DB column names.
    metadata : dict
        Sounding-level attributes.

    Returns
    -------
    tuple[dict, dict]
        Updated (data, metadata).
    """
    required = {'press', 'temp', 'rh'}
    missing = required - data.keys()
    if missing:
        logger.warning(
            "sounding_diagnostics: skipping – missing variables: %s", missing
        )
        return data, metadata

    try:
        # ------------------------------------------------------------------
        # Build MetPy quantities from raw arrays
        # ------------------------------------------------------------------
        p = np.asarray(data['press'], dtype=float) * units.hPa
        # data['temp'] is stored in Kelvin; convert to degC for MetPy, which
        # performs internal arithmetic (e.g. index differences) using degC and
        # would raise OffsetUnitCalculusError if given kelvin directly.
        T = (np.asarray(data['temp'], dtype=float) * units.kelvin).to(units.degC)
        # RH in the DB is stored as 0-100 %; MetPy expects a dimensionless
        # fraction (0-1) for dewpoint_from_relative_humidity when using
        # percent units, so we attach the 'percent' unit directly.
        RH = np.asarray(data['rh'], dtype=float) * units.percent

        # Altitude: prefer 'alt_amsl' (m above MSL), fall back to 'alt'
        if 'alt_amsl' in data:
            z = np.asarray(data['alt_amsl'], dtype=float) * units.m
        elif 'alt' in data:
            z = np.asarray(data['alt'], dtype=float) * units.m
        else:
            logger.warning(
                "sounding_diagnostics: skipping – no altitude variable found"
            )
            return data, metadata

        # if 'u' in data and 'v' in data:
        #     u = np.asarray(data['u'], dtype=float) * units('m/s')
        #     v = np.asarray(data['v'], dtype=float) * units('m/s')
        # else:
        #     logger.warning(
        #         "sounding_diagnostics: wind components (u/v) not found "
        #         "– wind diagnostics will be skipped"
        #     )
        #     u = v = None
        u_key = 'u' if 'u' in data else 'wzon' if 'wzon' in data else None
        v_key = 'v' if 'v' in data else 'wmeri' if 'wmeri' in data else None

        if u_key and v_key:
            u = np.asarray(data[u_key], dtype=float) * units('m/s')
            v = np.asarray(data[v_key], dtype=float) * units('m/s')
        else:
            logger.warning(
                "sounding_diagnostics: wind components not found (tried u/v and wzon/wmeri) "
                "– wind diagnostics will be skipped"
            )
            u = v = None

        # ------------------------------------------------------------------
        # Drop NaN rows so MetPy calculations don't fail
        # ------------------------------------------------------------------
        valid = (
            np.isfinite(p.magnitude)
            & np.isfinite(T.magnitude)
            & np.isfinite(RH.magnitude)
            & np.isfinite(z.magnitude)
        )
        if u is not None:
            valid &= np.isfinite(u.magnitude) & np.isfinite(v.magnitude)

        if valid.sum() < 10:
            logger.warning(
                "sounding_diagnostics: too few valid levels (%d) – skipping",
                valid.sum(),
            )
            return data, metadata

        p_v, T_v, RH_v, z_v = p[valid], T[valid], RH[valid], z[valid]
        u_v = u[valid] if u is not None else None
        v_v = v[valid] if v is not None else None

        # Ensure monotonically decreasing pressure
        p_v, T_v, RH_v, z_v = _ensure_monotonic(p_v, T_v, RH_v, z_v)
        if u_v is not None:
            # Re-sort wind with the same index
            idx = np.argsort(p_v.magnitude)[::-1]
            # already sorted above – redo properly
            sort_idx = np.argsort(p[valid].magnitude)[::-1]
            p_v = p[valid][sort_idx]
            T_v = T[valid][sort_idx]
            RH_v = RH[valid][sort_idx]
            z_v = z[valid][sort_idx]
            u_v = u[valid][sort_idx]
            v_v = v[valid][sort_idx]

        # ------------------------------------------------------------------
        # Compute basic thermo (profile)
        # ------------------------------------------------------------------
        Td, theta, theta_e = compute_basic_thermo(p_v, T_v, RH_v)

        # ------------------------------------------------------------------
        # Compute parcel / convection (scalar)
        # ------------------------------------------------------------------
        parcel = compute_parcel_diagnostics(p_v, T_v, Td)

        # ------------------------------------------------------------------
        # Compute instability indices (scalar)
        # ------------------------------------------------------------------
        indices = compute_indices(p_v, T_v, Td)

        # ------------------------------------------------------------------
        # Compute tropopause (scalar)
        # ------------------------------------------------------------------
        trop_p, trop_T = compute_tropopause(p_v, T_v)

        # ------------------------------------------------------------------
        # Compute wind diagnostics (scalar) – only when wind is available
        # ------------------------------------------------------------------
        wind = None
        if u_v is not None:
            wind = compute_wind(p_v, z_v, u_v, v_v)

        # ------------------------------------------------------------------
        # Compute misc (scalar)
        # ------------------------------------------------------------------
        misc = compute_misc(p_v, T_v, Td)

        # ------------------------------------------------------------------
        # Compute cloud base (scalar)
        # ------------------------------------------------------------------
        cbh, cbh_type = _cloud_base_height(z_v, RH_v, T_v)

        original_size = len(p)
        valid_indices = np.where(valid)[0]
        if u_v is not None:
            final_mapping_indices = valid_indices[sort_idx]
        else:
            final_mapping_indices = valid_indices

        def _map_to_full_length(subset_array):
            full_array = np.full(original_size, np.nan)
            full_array[final_mapping_indices] = subset_array
            return full_array

        # ==================================================================
        # Write PROFILE variables → data
        # NOTE: these arrays are computed on the valid/sorted sub-set; we
        #       write them as new keys so they don't collide with existing
        #       raw variables and don't need to be the exact same length.
        # ==================================================================
        data['diag_dewpoint'] = _map_to_full_length(_magnitude_array(Td))          # °C
        data['diag_theta'] = _map_to_full_length(_magnitude_array(theta) )         # K
        data['diag_theta_e'] = _map_to_full_length(_magnitude_array(theta_e))      # K
        data['diag_parcel_profile'] = _map_to_full_length(_magnitude_array(parcel['parcel_profile']))  # °C

        # ==================================================================
        # Write SCALAR variables → metadata
        # ==================================================================
        lcl_p, lcl_T = parcel['LCL']
        lfc_p, lfc_T = parcel['LFC']
        el_p, el_T = parcel['EL']

        metadata['diag_lcl_pressure'] = _map_to_full_length(_magnitude_or_none(lcl_p))    # hPa
        metadata['diag_lcl_temperature'] = _map_to_full_length(_magnitude_or_none(lcl_T))  # °C
        metadata['diag_lfc_pressure'] = _map_to_full_length(_magnitude_or_none(lfc_p))
        metadata['diag_lfc_temperature'] = _map_to_full_length(_magnitude_or_none(lfc_T))
        metadata['diag_el_pressure'] = _map_to_full_length(_magnitude_or_none(el_p))
        metadata['diag_el_temperature'] = _map_to_full_length(_magnitude_or_none(el_T))
        metadata['diag_cape'] = _map_to_full_length(_magnitude_or_none(parcel['CAPE']))     # J/kg
        metadata['diag_cin'] = _map_to_full_length(_magnitude_or_none(parcel['CIN']))        # J/kg

        metadata['diag_lifted_index'] = _map_to_full_length(_magnitude_or_none(indices['LiftedIndex']))  # K
        metadata['diag_k_index'] = _map_to_full_length(_magnitude_or_none(indices['KIndex']))             # °C
        metadata['diag_total_totals'] = _map_to_full_length(_magnitude_or_none(indices['TotalTotals']))
        metadata['diag_showalter'] = _map_to_full_length(_magnitude_or_none(indices['Showalter']))

        metadata['diag_tropopause_pressure'] = _map_to_full_length(_magnitude_or_none(trop_p))    # hPa
        metadata['diag_tropopause_temperature'] = _map_to_full_length(_magnitude_or_none(trop_T))  # °C

        metadata['diag_pwat'] = _map_to_full_length(_magnitude_or_none(misc['PWAT']))               # kg/m²
        metadata['diag_freezing_level_index'] = misc['FreezingLevelIndex']

        metadata['diag_cloud_base_height'] = _map_to_full_length(_magnitude_or_none(cbh))           # m
        metadata['diag_cloud_base_type'] = cbh_type

        if wind is not None:
            sh_u_1, sh_v_1 = wind['Shear_0_1km']
            sh_u_3, sh_v_3 = wind['Shear_0_3km']
            sh_u_6, sh_v_6 = wind['Shear_0_6km']
            srh_pos, srh_neg, srh_tot = wind['SRH_0_3km']

            metadata['diag_shear_0_1km_u'] = _map_to_full_length(_magnitude_or_none(sh_u_1))   # m/s
            metadata['diag_shear_0_1km_v'] = _map_to_full_length(_magnitude_or_none(sh_v_1))
            metadata['diag_shear_0_3km_u'] = _map_to_full_length(_magnitude_or_none(sh_u_3))
            metadata['diag_shear_0_3km_v'] = _map_to_full_length(_magnitude_or_none(sh_v_3))
            metadata['diag_shear_0_6km_u'] = _map_to_full_length(_magnitude_or_none(sh_u_6))
            metadata['diag_shear_0_6km_v'] = _map_to_full_length(_magnitude_or_none(sh_v_6))
            metadata['diag_srh_0_3km_positive'] = _map_to_full_length(_magnitude_or_none(srh_pos))  # m²/s²
            metadata['diag_srh_0_3km_negative'] = _map_to_full_length(_magnitude_or_none(srh_neg))
            metadata['diag_srh_0_3km_total'] = _map_to_full_length(_magnitude_or_none(srh_tot))

        logger.debug("sounding_diagnostics: diagnostics computed successfully")

    except Exception as exc:
        logger.warning(
            "sounding_diagnostics: calculation failed – skipping (%s: %s)",
            type(exc).__name__, exc,
        )

    return data, metadata