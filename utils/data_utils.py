import numpy as np
import pandas as pd


def fill_constant_values(data, key, value, length):
    """
    Fill a data array with constant values for a given key.
    Handles different data types (timestamps, strings, numerics) appropriately.

    Args:
        data (dict): Target dictionary to populate with the filled array
        key (str): Key under which to store the filled array
        value: Constant value to fill the array with
        length (int): Length of the array to create

    Note:
        Converts pandas Timestamps to Python datetime objects for database
        compatibility
        Attempts numeric conversion for numeric values, falls back to object
        dtype if needed
    """
    if length > 0:
        if isinstance(value, pd.Timestamp):
            # Convert pandas Timestamp to Python datetime for database
            # compatibility
            data[key] = np.full(length, value.to_pydatetime(), dtype=object)
        elif isinstance(value, str):
            # Store strings as object arrays
            data[key] = np.full(length, value, dtype=object)
        else:
            try:
                # Attempt to convert to numeric value (float)
                numeric_value = float(value) if value else np.nan
                data[key] = np.full(length, numeric_value, dtype=float)
            except (ValueError, TypeError):
                # Fallback to object dtype for non-convertible values
                data[key] = np.full(length, value, dtype=object)


def clean_numeric_value(value, col_type):
    """
    Clean and convert numeric values based on database column type
    requirements.
    Handles various numeric formats and extracts numeric values from strings.

    Args:
        value: Input value to clean (can be string, numeric, or NaN)
        col_type (str): Target database column type (e.g., 'integer', 'real')

    Returns:
        Cleaned numeric value as appropriate type, or NaN if conversion fails

    Examples:
        >>> clean_numeric_value('123.45', 'real')
        123.45
        >>> clean_numeric_value('123.45', 'integer')
        123
        >>> clean_numeric_value('abc', 'integer')
        nan
    """
    if isinstance(value, (np.ndarray, list)):
        if len(value) > 0:
            value = value[0]
        else:
            value = np.nan

    if pd.isna(value):
        return np.nan

    # Handle non-string values (already numeric)
    if not isinstance(value, str):
        if col_type in {'integer', 'bigint', 'smallint'}:
            try:
                # Convert to integer via float to handle scientific notation
                return int(float(value))
            except (ValueError, TypeError):
                return np.nan
        return value

    # Process string values
    value_stripped = value.strip()

    # Try direct conversion to float
    try:
        numeric_val = float(value_stripped)
        if col_type in {'integer', 'bigint', 'smallint'}:
            return int(numeric_val)  # Convert to integer for integer types
        return numeric_val
    except (ValueError, TypeError):
        pass  # Continue to regex extraction if direct conversion fails

    # Use regex to extract numeric part from strings with units or annotations
    import re
    match = re.match(r'^([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)',
                     value_stripped)
    if match:
        try:
            numeric_val = float(match.group(1))
            if col_type in {'integer', 'bigint', 'smallint'}:
                return int(numeric_val)
            return numeric_val
        except (ValueError, TypeError):
            pass

    # Return NaN if all conversion attempts fail
    return np.nan
