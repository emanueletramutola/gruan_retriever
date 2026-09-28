# GRUAN Quality Control Pipeline
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
