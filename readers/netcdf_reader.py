import numpy as np
from netCDF4 import Dataset
import logging
import struct

# Configure logging
logger = logging.getLogger(__name__)

# PostgreSQL real (float4) minimum positive value
# Using the smallest positive normalized value for float4
POSTGRES_REAL_MIN = 1.175494351e-38


def read_netcdf(zip_ref, filename):
    """
    Read NetCDF file from a ZIP archive and extract data variables and global attributes.

    Args:
        zip_ref: ZipFile object reference for reading the compressed file
        filename (str): Name of the NetCDF file within the ZIP archive

    Returns:
        tuple: (data, metadata) where:
            - data (dict): Dictionary of variable names to numpy arrays with
              masked values filled as NaN and very small values replaced with 0
            - metadata (dict): Dictionary of global attributes from the NetCDF file

    Raises:
        ValueError: If the filename is invalid or the file cannot be found in the archive
        IOError: If there are issues reading the file from the ZIP archive
        RuntimeError: If there are issues parsing the NetCDF file

    Example:
        >>> with zipfile.ZipFile('archive.zip', 'r') as zip_ref:
        ...     data, metadata = read_netcdf(zip_ref, 'data.nc')
    """
    # Validate input parameters
    if not filename or not isinstance(filename, str):
        raise ValueError("Filename must be a non-empty string")

    if zip_ref is None:
        raise ValueError("ZipFile reference cannot be None")

    try:
        # Open the NetCDF file from the ZIP archive
        with zip_ref.open(filename) as file:
            return _process_netcdf_file(file)

    except KeyError as e:
        error_msg = f"File '{filename}' not found in ZIP archive: {e}"
        logger.error(error_msg)
        raise ValueError(error_msg) from e


def _process_netcdf_file(file):
    """
    Process NetCDF file and extract data and metadata.

    Args:
        file: File-like object containing NetCDF data

    Returns:
        tuple: (data, metadata) containing extracted data and metadata

    Raises:
        RuntimeError: If NetCDF file processing fails
    """
    try:
        # Create in-memory NetCDF dataset from file bytes
        # Uses 'inmemoryfile' as dummy filename since we're reading from memory
        with Dataset('inmemoryfile', mode='r', memory=file.read()) as nc:
            data = _extract_variables(nc)
            metadata = _extract_global_attributes(nc)

        return data, metadata

    except Exception as e:
        error_msg = f"Error processing NetCDF file: {e}"
        logger.error(error_msg)
        raise RuntimeError(error_msg) from e


def _extract_variables(nc_dataset):
    """
    Extract all variables from NetCDF dataset.

    Args:
        nc_dataset: NetCDF Dataset object

    Returns:
        dict: Dictionary of variable names to processed numpy arrays
    """
    data = {}
    for var_name in nc_dataset.variables:
        try:
            var_data = nc_dataset.variables[var_name][:]
            processed_data = _process_variable_data(var_data)
            data[var_name] = processed_data
        except Exception as e:
            logger.warning(f"Failed to process variable '{var_name}': {e}")
            # Continue processing other variables even if one fails
            continue

    return data


def _process_variable_data(var_data):
    """
    Process variable data by handling masked arrays, string formatting,
    and replacing very small values with 0.

    Args:
        var_data: Raw variable data from NetCDF file

    Returns:
        numpy.ndarray: Processed array with proper formatting
    """
    # Convert masked arrays to regular arrays with NaN fill
    if np.ma.is_masked(var_data):
        processed_data = np.ma.filled(var_data, np.nan)
    else:
        processed_data = var_data[:]  # Ensure we get a numpy array

    # Handle string data types
    if processed_data.dtype.kind in ['U', 'S',
                                     'O']:  # Unicode, byte string, or object
        if processed_data.ndim == 0:  # Scalar case
            return str(processed_data).replace('\n', ' ')
        else:  # Array case
            return np.char.replace(processed_data.astype(str), '\n', ' ')

    # Replace very small values with 0 for numeric arrays
    if processed_data.dtype.kind in ['f', 'c']:  # Floating point or complex
        # Create a mask for values that are too small but not NaN or infinite
        small_value_mask = (
                np.isfinite(processed_data) &
                (np.abs(processed_data) < POSTGRES_REAL_MIN) &
                (np.abs(processed_data) > 0)
        )

        # Replace very small values with 0
        if np.any(small_value_mask):
            logger.debug(
                f"Replacing {np.sum(small_value_mask)} very small values with 0")
            processed_data = processed_data.copy()  # Ensure we don't modify the original
            processed_data[small_value_mask] = 0

    return processed_data


def _extract_global_attributes(nc_dataset):
    """
    Extract global attributes from NetCDF dataset.

    Args:
        nc_dataset: NetCDF Dataset object

    Returns:
        dict: Dictionary of global attributes
    """
    metadata = {}
    for attr_name in nc_dataset.ncattrs():
        try:
            attr_value = nc_dataset.getncattr(attr_name)
            metadata[attr_name] = _process_attribute_value(attr_value)
        except Exception as e:
            logger.warning(f"Failed to extract attribute '{attr_name}': {e}")
            # Continue processing other attributes even if one fails
            continue

    return metadata


def _process_attribute_value(value):
    """
    Process attribute value by handling string formatting.

    Args:
        value: Raw attribute value

    Returns:
        Processed attribute value
    """
    if isinstance(value, str):
        return value.replace('\n', ' ')
    return value