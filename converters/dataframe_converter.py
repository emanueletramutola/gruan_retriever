import logging

import numpy as np
import pandas as pd

from utils.data_utils import fill_constant_values
from utils.station_manager import get_station_manager

logger = logging.getLogger(__name__)


class DataFrameConverter:
    """
    Converts raw radiosonde data to pandas DataFrames with proper
    column
    mapping
    and data cleaning for database insertion.
    """

    def __init__(self, sonde_type, column_cleaner,
                 columns_header_table,
                 columns_data_table):
        """
        Initialize the DataFrame converter.

        Args:
            sonde_type (str): Type of radiosonde
            column_cleaner: Translator object for cleaning column names
            columns_header_table (list): Expected columns for header table
            columns_data_table (list): Expected columns for data table
        """
        self.sonde_type = sonde_type
        self.column_cleaner = column_cleaner
        self.columns_header_table = columns_header_table
        self.columns_data_table = columns_data_table

        # Initialize station manager for both RS41 and RS92
        self.station_manager = get_station_manager()
        # Cache will be loaded either by parallel_import (for parallel
        # processing)
        # or by the first call to convert_to_dataframe (for sequential
        # processing)

    def clean_column_name(self, column_name):
        """
        Clean and standardize column names for database compatibility.

        Args:
            column_name (str): Original column name

        Returns:
            str: Cleaned column name in lowercase without special
            characters
        """
        column_rename_map = {
            'comment': 'cmt',
            'references': 'refs',
            'group': 'grp',
            'user': 'usr',
            'default': 'def'
        }
        # Remove special characters and convert to lowercase
        cleaned = column_name.lower().translate(self.column_cleaner)
        # Apply specific renaming for reserved keywords
        return column_rename_map.get(cleaned, cleaned)

    def _get_station_id_and_validate(self, station_identifier):
        """
        Get station ID and validate if station exists in database.

        Args:
            station_identifier (str): Station identifier to look up

        Returns:
            tuple: (station_id, skip_reason) - Station ID and reason for skipping if any
        """
        if not station_identifier:
            return None, "Missing station identifier"

        # Look up station ID from station manager
        station_id = self.station_manager.get_station_id(
            station_identifier,
            'GRUAN')

        if station_id is None:
            return None, f"Station '{station_identifier}' not found in GRUAN station database"

        return station_id, None

    def convert_to_dataframe(self, data, metadata):
        """
        Convert raw data and metadata to pandas DataFrames.

        Args:
            data (dict): Measurement data with time series
            metadata (dict): Header information and metadata

        Returns:
            tuple: (metadata_df, data_df, skip_reason) - DataFrames for header and data tables, and reason for skipping if any
        """
        skip_reason = None

        if not data:
            return pd.DataFrame(), pd.DataFrame(), "No measurement data provided"

        try:
            time_len = len(data.get('time', []))
            if time_len == 0:
                return pd.DataFrame(), pd.DataFrame(), "No time data found in measurements"

            station_id = None

            # Process based on file conventions
            if metadata['Conventions'] == 'CF-1.7':
                # Get site key from metadata
                station_identifier = metadata.get('g.Site.Key', '')
                if not station_identifier:
                    return pd.DataFrame(), pd.DataFrame(), "Missing station identifier (g.Site.Key) in metadata"

                station_id, skip_reason = self._get_station_id_and_validate(
                    station_identifier)
                if skip_reason:
                    return pd.DataFrame(), pd.DataFrame(), skip_reason

                standard_time = metadata.get('g.Measurement.StandardTime', '')
                try:
                    report_timestamp = pd.to_datetime(standard_time,
                                                      errors='coerce')
                    if pd.isna(report_timestamp):
                        return pd.DataFrame(), pd.DataFrame(), f"Invalid timestamp format for {self.sonde_type}: '{standard_time}'"
                except Exception as e:
                    return pd.DataFrame(), pd.DataFrame(), f"Error parsing timestamp '{standard_time}': {e}"

                fill_constant_values(data, 'report_timestamp',
                                     report_timestamp, time_len)

            elif metadata['Conventions'] == 'CF-1.4' or metadata[
                'Conventions'] == 'RS-11G':
                # Get site code from metadata
                station_identifier = metadata.get('g.General.SiteCode', '')
                if not station_identifier:
                    return pd.DataFrame(), pd.DataFrame(), "Missing station identifier (g.General.SiteCode) in metadata"

                station_id, skip_reason = self._get_station_id_and_validate(
                    station_identifier)
                if skip_reason:
                    return pd.DataFrame(), pd.DataFrame(), skip_reason

                ascent_time = metadata.get('g.Ascent.StandardTime', '')
                try:
                    report_timestamp = pd.to_datetime(ascent_time,
                                                      errors='coerce')
                    if pd.isna(report_timestamp):
                        return pd.DataFrame(), pd.DataFrame(), f"Invalid timestamp format for {self.sonde_type}: '{ascent_time}'"
                except Exception as e:
                    return pd.DataFrame(), pd.DataFrame(), f"Error parsing timestamp '{ascent_time}': {e}"

                fill_constant_values(data, 'report_timestamp',
                                     report_timestamp, time_len)
                fill_constant_values(data, 'g_general_sitewmoid',
                                     station_identifier, time_len)

            else:
                return pd.DataFrame(), pd.DataFrame(), f"Unsupported file conventions: {metadata.get('Conventions', 'Unknown')}"

            # Set station ID in both metadata and data for all sonde types
            if station_id is not None:
                metadata['idstation_pk'] = station_id
                fill_constant_values(data, 'idstation_pk', station_id,
                                     time_len)

            metadata['report_timestamp'] = report_timestamp

            # Create DataFrames from processed data
            metadata_df = pd.DataFrame([metadata])
            data_df = pd.DataFrame(data)

            # Clean column names for both DataFrames
            data_df.columns = [self.clean_column_name(col) for col in
                               data_df.columns]
            metadata_df.columns = [self.clean_column_name(col) for col in
                                   metadata_df.columns]

            # Filter columns to match expected database schema
            available_data_cols = [c for c in self.columns_data_table if
                                   c in data_df.columns]
            available_header_cols = [c for c in self.columns_header_table if
                                     c in metadata_df.columns]

            # Reindex DataFrames to match expected column order, fill missing with NaN
            data_df = data_df.reindex(columns=available_data_cols,
                                      fill_value=np.nan)
            metadata_df = metadata_df.reindex(columns=available_header_cols,
                                              fill_value=np.nan)

            return metadata_df, data_df, skip_reason

        except Exception as e:
            error_msg = f"Error converting to dataframe: {e}"
            logger.error(error_msg)
            return pd.DataFrame(), pd.DataFrame(), error_msg
