import numpy as np
import pandas as pd
from netCDF4 import Dataset

# 1. Open the NetCDF file
# file_path_nc_CDM = ('/Data/GRUAN_TEST/output/insitu-observations-gruan'
#                     '-reference-network_GRUAN_2025_02.nc')
# file_path_nc_GRUAN = ('/Data/GRUAN_TEST/netcdf/SYO-RS-01_2_RS-11G'
#                       '-GDP_001_20250227T120000_1-000-001.nc')
file_path_nc_CDM = ('/Data/GRUAN_TEST/output/insitu-observations-gruan-reference-network_GRUAN_2026_03.nc')
file_path_nc_GRUAN = ('/Data/GRUAN_TEST/netcdf/LIN-RS-01_2_RS41-GDP_001_20260323T060000_1-000-001.nc')
nc_file_CDM = Dataset(file_path_nc_CDM, mode="r")
nc_file_GRUAN = Dataset(file_path_nc_GRUAN, mode="r")

# 2. Extract both variables and replace missing/masked values with NaN
obs_var_nc_CDM = nc_file_CDM.variables["observed_variable"]
obs_val_nc_CDM = nc_file_CDM.variables["observation_value"]
obs_unc_2_nc_CDM = nc_file_CDM.variables["uncertainty_value2"]
obs_unc_5_nc_CDM = nc_file_CDM.variables["uncertainty_value5"]
press_GRUAN = nc_file_GRUAN.variables["press"]
# u_press_GRUAN = nc_file_GRUAN.variables["u_press"]
press_uc_GRUAN = nc_file_GRUAN.variables["press_uc"]
wvmr_vol_GRUAN = nc_file_GRUAN.variables["wvmr_vol"]
wvmr_vol_uc_tcor_GRUAN = nc_file_GRUAN.variables["wvmr_vol_uc_tcor"]
wvmr_vol_uc_GRUAN = nc_file_GRUAN.variables["wvmr_vol_uc"]

# Convert masked arrays to numpy arrays with NaN for nulls
var_data_CDM = np.ma.filled(obs_var_nc_CDM[:].astype(object),
                            fill_value=np.nan).flatten()
val_data_CDM = np.ma.filled(obs_val_nc_CDM[:].astype(float),
                            fill_value=np.nan).flatten()
val_unc_2_data_CDM = np.ma.filled(obs_unc_2_nc_CDM[:].astype(float),
                                  fill_value=np.nan).flatten()
val_unc_5_data_CDM = np.ma.filled(obs_unc_5_nc_CDM[:].astype(float),
                                  fill_value=np.nan).flatten()

press_nc_GRUAN = np.ma.filled(press_GRUAN[:].astype(object),
                              fill_value=np.nan).flatten()
# u_press_nc_GRUAN = np.ma.filled(u_press_GRUAN[:].astype(object),
#                                 fill_value=np.nan).flatten()
press_uc_nc_GRUAN = np.ma.filled(press_uc_GRUAN[:].astype(object),
                                fill_value=np.nan).flatten()
wvmr_vol_GRUAN_nc_GRUAN = np.ma.filled(wvmr_vol_GRUAN[:].astype(object),
                                fill_value=np.nan).flatten()
wvmr_vol_uc_tcor_GRUAN_nc_GRUAN = np.ma.filled(wvmr_vol_uc_tcor_GRUAN[:].astype(object),
                                fill_value=np.nan).flatten()
wvmr_vol_uc_GRUAN_nc_GRUAN = np.ma.filled(wvmr_vol_uc_GRUAN[:].astype(object),
                                fill_value=np.nan).flatten()

# 3. Create a DataFrame containing both columns
df_CDM = pd.DataFrame(
    {"observed_variable": var_data_CDM,
     "observation_value": val_data_CDM,
     "uncertainty_value2": val_unc_2_data_CDM,
     "uncertainty_value5": val_unc_5_data_CDM}
)
df_GRUAN = pd.DataFrame(
    {
     "press": press_nc_GRUAN,
     # "u_press": u_press_nc_GRUAN,
     "press_uc": press_uc_nc_GRUAN,
     "wvmr_vol": wvmr_vol_GRUAN_nc_GRUAN,
     "wvmr_vol_uc_tcor": wvmr_vol_uc_tcor_GRUAN_nc_GRUAN,
     "wvmr_vol_uc": wvmr_vol_uc_GRUAN_nc_GRUAN
     }
)

# 4. Export the DataFrame to a CSV file
output_path_CDM = "/Data/GRUAN_TEST/output/CDM_data.csv"
output_path_GRUAN = "/Data/GRUAN_TEST/output/GRUAN_data.csv"
# na_rep='' leaves null values as empty cells in the CSV file
df_CDM.to_csv(output_path_CDM, index=False, na_rep="")
df_GRUAN.to_csv(output_path_GRUAN, index=False, na_rep="")

# Close the NetCDF dataset connection
nc_file_CDM.close()
nc_file_GRUAN.close()

print(f"Data successfully exported to {output_path_CDM}")
print(f"Data successfully exported to {output_path_GRUAN}")