from netCDF4 import Dataset
import numpy as np
import pandas as pd

# 1. Open the NetCDF file
file_path = '/Data/GRUAN_TEST/output/insitu-observations-gruan-reference-network_GRUAN_2025_02.nc'
nc_file = Dataset(file_path, mode="r")

# 2. Extract both variables and replace missing/masked values with NaN
obs_var = nc_file.variables["observed_variable"]
obs_val = nc_file.variables["observation_value"]

# Convert masked arrays to numpy arrays with NaN for nulls
var_data = np.ma.filled(obs_var[:].astype(object), fill_value=np.nan).flatten()
val_data = np.ma.filled(obs_val[:].astype(float), fill_value=np.nan).flatten()

# 3. Create a DataFrame containing both columns
df = pd.DataFrame(
    {"observed_variable": var_data, "observation_value": val_data}
)

# 4. Export the DataFrame to a CSV file
output_path = "/Data/GRUAN_TEST/output/observed_variables_and_values.csv"
# na_rep='' leaves null values as empty cells in the CSV file
df.to_csv(output_path, index=False, na_rep="")

# Close the NetCDF dataset connection
nc_file.close()

print(f"Data successfully exported to {output_path}")