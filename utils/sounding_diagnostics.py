import metpy.calc as mpcalc
import numpy as np
from metpy.units import units
from scipy.ndimage import uniform_filter1d


# -------------------------------------------------------
# Utility
# -------------------------------------------------------

def smooth(x, n=5):
    return uniform_filter1d(x, size=n, mode='nearest')


def ensure_monotonic(p, *args):
    idx = np.argsort(p)[::-1]
    return (p[idx],) + tuple(a[idx] for a in args)


# -------------------------------------------------------
# Core thermodynamics
# -------------------------------------------------------

def compute_basic_thermo(p, T, RH):
    Td = mpcalc.dewpoint_from_relative_humidity(T, RH)
    theta = mpcalc.potential_temperature(p, T)
    theta_e = mpcalc.equivalent_potential_temperature(p, T, Td)
    return Td, theta, theta_e


# -------------------------------------------------------
# Convection & parcels
# -------------------------------------------------------

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
        "CIN": cin
    }


# -------------------------------------------------------
# Instability indices
# -------------------------------------------------------

def compute_indices(p, T, Td):
    return {
        "LiftedIndex": mpcalc.lifted_index(p, T, mpcalc.parcel_profile(p, T[0],
                                                                       Td[0])),
        "KIndex": mpcalc.k_index(p, T, Td),
        "TotalTotals": mpcalc.total_totals_index(p, T, Td),
        "Showalter": mpcalc.showalter_index(p, T, Td)
    }


# -------------------------------------------------------
# Tropopause
# -------------------------------------------------------

def compute_tropopause(p, T):
    trop_p, trop_T = mpcalc.tropopause(p, T)
    return trop_p, trop_T


# -------------------------------------------------------
# Wind diagnostics
# -------------------------------------------------------

def compute_wind(p, z, u, v):
    shear_0_1 = mpcalc.bulk_shear(p, u, v, height=z, depth=1000 * units.m)
    shear_0_3 = mpcalc.bulk_shear(p, u, v, height=z, depth=3000 * units.m)
    shear_0_6 = mpcalc.bulk_shear(p, u, v, height=z, depth=6000 * units.m)

    srh_0_3 = mpcalc.storm_relative_helicity(
        z, u, v, depth=3000 * units.m
    )

    return {
        "Shear_0_1km": shear_0_1,
        "Shear_0_3km": shear_0_3,
        "Shear_0_6km": shear_0_6,
        "SRH_0_3km": srh_0_3
    }


# -------------------------------------------------------
# Other diagnostics
# -------------------------------------------------------

def compute_misc(p, T, Td):
    pwat = mpcalc.precipitable_water(p, Td)

    freezing_idx = np.where(T <= 0 * units.degC)[0]
    freezing_level = None
    if len(freezing_idx) > 0:
        freezing_level = freezing_idx[0]

    return {
        "PWAT": pwat,
        "FreezingLevelIndex": freezing_level
    }


# -------------------------------------------------------
# Cloud base height
# -------------------------------------------------------

def cloud_base_low(z, RH, zmax=3000 * units.m, rh_crit=0.95):
    for zi, rhi in zip(z, RH):
        if zi <= zmax and rhi >= rh_crit:
            return zi
    return None


def cloud_base_zhang(z, RH, T, zmin=4000 * units.m, rh_min=0.75):
    RHs = smooth(RH.magnitude, 5)

    for i in range(1, len(z) - 1):
        if z[i] > zmin:
            if RHs[i] > RHs[i - 1] and RHs[i] > RHs[i + 1]:
                if RHs[i] >= rh_min:
                    return z[i]
    return None


def cloud_base_height(z, RH, T):
    cb_low = cloud_base_low(z, RH)
    if cb_low is not None:
        return cb_low, "low cloud (RH)"

    cb_high = cloud_base_zhang(z, RH, T)
    if cb_high is not None:
        return cb_high, "high cloud (Zhang)"

    return None, "clear"


# -------------------------------------------------------
# MASTER FUNCTION
# -------------------------------------------------------

def full_sounding_diagnostics(p, T, RH, u, v, z):
    # monotonic pressure
    p, T, RH, u, v, z = ensure_monotonic(p, T, RH, u, v, z)

    # basic thermo
    Td, theta, theta_e = compute_basic_thermo(p, T, RH)

    # convection
    parcel = compute_parcel_diagnostics(p, T, Td)

    # indices
    indices = compute_indices(p, T, Td)

    # tropopause
    trop = compute_tropopause(p, T)

    # wind
    wind = compute_wind(p, z, u, v)

    # misc
    misc = compute_misc(p, T, Td)

    # clouds
    cbh, cbh_type = cloud_base_height(z, RH, T)

    return {
        "Thermo": {
            "Td": Td,
            "Theta": theta,
            "Theta_e": theta_e
        },
        "Parcel": parcel,
        "Indices": indices,
        "Tropopause": trop,
        "Wind": wind,
        "Misc": misc,
        "CloudBase": {
            "Height": cbh,
            "Type": cbh_type
        }
    }
