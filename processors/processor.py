import logging
import re
import zipfile

from config import PATH_CONFIG, TABLE_NAMES
from converters.dataframe_converter import DataFrameConverter
from database import get_connection
from database.operations import DatabaseOperations
from readers.netcdf_reader import read_netcdf

logger = logging.getLogger(__name__)


class GRUANProcessor:
    """
    Main processor class for handling GRUAN radiosonde data import pipeline.
    Manages the complete workflow from JAR file extraction to database
    insertion.
    """

    def __init__(self, sonde_type):
        """
        Initialize the processor for a specific radiosonde type.

        Args:
            sonde_type (str): Type of radiosonde
        """
        self.sonde_type = sonde_type
        self.path_files = PATH_CONFIG[sonde_type]  # Base path for data files
        self.header_table_name, self.data_table_name = TABLE_NAMES[
            sonde_type]  # Database table names

        # Create translator for cleaning column names (replace special
        # characters with underscores)
        self.column_cleaner = str.maketrans({'.': '_', '-': '_', ' ': '_'})

        # Initialize database operations helper
        self.db_ops = DatabaseOperations()

        # Cache database column names for performance
        self.columns_header_table = (
            self.db_ops.get_columns_excluding_serial_pk_cached(
                self.header_table_name))
        self.columns_data_table = (
            self.db_ops.get_columns_excluding_serial_pk_cached(
                self.data_table_name))

        # Initialize DataFrame converter for data transformation
        self.df_converter = DataFrameConverter(
            sonde_type,
            self.column_cleaner,
            self.columns_header_table,
            self.columns_data_table
        )

    def process_single_jar_file(self, jar_file):
        """
        Process a single JAR file containing NetCDF data files.
        Processes the latest revision (revXXX) directory if present, otherwise
        processes NetCDF files directly from root or selects the latest revision
        based on filename patterns.
        """
        try:
            # Open the JAR file as a zip archive
            with zipfile.ZipFile(jar_file, 'r') as zip_ref:
                # Regular expression pattern to match revision directories (revXXX)
                rev_pattern = re.compile(r'rev(\d{3})/')
                # Pattern to extract revision from NetCDF filenames (optional)
                nc_rev_pattern = re.compile(r'_rev(\d{3})\.nc$')

                rev_numbers = []
                rev_files = {}  # Track files by revision
                root_nc_files = []  # Track NetCDF files in root directory

                # Iterate through all files in the JAR to find revision directories
                # and NetCDF files
                for name in zip_ref.namelist():
                    # Check for revision directories
                    match = rev_pattern.search(name)
                    if match:
                        rev_num = int(match.group(1))
                        rev_numbers.append(rev_num)

                        # Group files by revision
                        if rev_num not in rev_files:
                            rev_files[rev_num] = []
                        rev_files[rev_num].append(name)

                    # Check for NetCDF files in root directory (no rev folder)
                    elif name.endswith('.nc') and '/' not in name:
                        root_nc_files.append(name)

                        # Try to extract revision from filename (optional)
                        nc_match = nc_rev_pattern.search(name)
                        if nc_match:
                            rev_num = int(nc_match.group(1))
                            rev_numbers.append(rev_num)
                            if rev_num not in rev_files:
                                rev_files[rev_num] = []
                            rev_files[rev_num].append(name)

                # Determine the latest revision (highest number)
                latest_rev = max(rev_numbers) if rev_numbers else None

                nc_files_to_process = []
                skipped_nc_files = []

                if latest_rev is not None:
                    # Case 1: We have revision information (from folders)
                    logger.debug(
                        f"Found revisions in {jar_file.name}: {sorted(rev_numbers)}, using rev{latest_rev:03d}")

                    # Find all NetCDF files for the latest revision
                    # Look for files in the latest revision directory
                    rev_prefix = f"rev{latest_rev:03d}/"
                    for name in zip_ref.namelist():
                        # Files in the latest revision directory
                        if name.startswith(rev_prefix) and name.endswith(
                                '.nc'):
                            nc_files_to_process.append(name)

                        # Also include files with matching revision in filename (if any)
                        nc_match = nc_rev_pattern.search(name)
                        if nc_match and int(nc_match.group(
                                1)) == latest_rev and name.endswith('.nc'):
                            if name not in nc_files_to_process:  # Avoid duplicates
                                nc_files_to_process.append(name)

                    # Track skipped files from older revisions
                    for rev_num, files in rev_files.items():
                        if rev_num < latest_rev:
                            for file_path in files:
                                if file_path.endswith('.nc'):
                                    skip_reason = f"Older revision (rev{rev_num:03d}) - using latest revision rev{latest_rev:03d}"
                                    skipped_nc_files.append(
                                        (file_path, skip_reason))
                                    logger.debug(
                                        f"Skipping NetCDF file {file_path} in {jar_file.name}: {skip_reason}")

                elif root_nc_files:
                    # Case 2: No revision info, but we have NetCDF files in root
                    logger.debug(
                        f"No revision directories found in {jar_file.name}, processing {len(root_nc_files)} NetCDF files from root")
                    nc_files_to_process = root_nc_files

                    # Try to determine if there are multiple versions based on filename patterns
                    # Look for files with similar names but different revisions in filename
                    file_versions = {}
                    for nc_file in root_nc_files:
                        # Extract base name without revision
                        base_match = re.search(r'(.+?)(?:_rev\d{3})?\.nc$',
                                               nc_file)
                        if base_match:
                            base_name = base_match.group(1)
                            # Extract revision from filename if present
                            rev_match = nc_rev_pattern.search(nc_file)
                            rev_num = int(
                                rev_match.group(1)) if rev_match else 0

                            if base_name not in file_versions:
                                file_versions[base_name] = []
                            file_versions[base_name].append((nc_file, rev_num))

                    # For files with multiple versions, keep only the latest revision
                    final_files_to_process = []
                    for base_name, versions in file_versions.items():
                        if len(versions) > 1:
                            # Multiple versions found, select the one with highest revision
                            versions.sort(key=lambda x: x[1], reverse=True)
                            latest_file, latest_rev_num = versions[0]
                            final_files_to_process.append(latest_file)

                            # Skip older versions
                            for skipped_file, skipped_rev in versions[1:]:
                                skip_reason = f"Older revision in filename (rev{skipped_rev:03d}) - using rev{latest_rev_num:03d}"
                                skipped_nc_files.append(
                                    (skipped_file, skip_reason))
                                logger.debug(
                                    f"Skipping NetCDF file {skipped_file} in {jar_file.name}: {skip_reason}")
                        else:
                            # Single version, include it
                            final_files_to_process.append(versions[0][0])

                    nc_files_to_process = final_files_to_process

                else:
                    # Case 3: No NetCDF files found at all
                    warning_msg = f"No NetCDF files found in {jar_file}"
                    logger.warning(warning_msg)
                    self._update_file_import_status(jar_file.name, False,
                                                    warning_msg)
                    return False

                # Log information about files found
                logger.debug(
                    f"Found {len(nc_files_to_process)} NetCDF files to process in {jar_file.name}")
                logger.debug(f"Files to process: {nc_files_to_process}")
                if skipped_nc_files:
                    logger.debug(
                        f"Skipped {len(skipped_nc_files)} NetCDF files from older revisions")

                # If no files to process after all filtering, mark as failed
                if not nc_files_to_process:
                    error_msg = f"No valid NetCDF files to process after revision filtering in {jar_file}. Found revisions: {rev_numbers}, latest: {latest_rev}"
                    logger.warning(error_msg)
                    self._update_file_import_status(jar_file.name, False,
                                                    error_msg)
                    return False

                # Process each selected NetCDF file
                all_successful = True
                processed_count = 0
                specific_errors = []  # Collect specific error messages

                for filename in nc_files_to_process:
                    success, specific_error = self.process_single_nc_file(
                        zip_ref, filename, jar_file.name)
                    if success:
                        processed_count += 1
                    else:
                        all_successful = False
                        if specific_error:
                            specific_errors.append(specific_error)

                # Update database with processing results
                if all_successful and processed_count > 0:
                    # Successful processing - update note with summary
                    note_parts = [
                        f"Successfully processed {processed_count} NetCDF files"]
                    if latest_rev is not None:
                        note_parts.append(f"from revision rev{latest_rev:03d}")
                    if skipped_nc_files:
                        note_parts.append(
                            f"Skipped {len(skipped_nc_files)} files from older revisions")

                    note = ". ".join(note_parts) + "."
                    self._update_file_import_status(jar_file.name, True, note)
                    logger.info(
                        f"Successfully processed {jar_file.name}: {note}")
                    return True
                else:
                    # Partial or complete failure - create detailed error message
                    if specific_errors:
                        # Use the first specific error for the main note (truncated if too long)
                        main_error = specific_errors[0]
                        if len(main_error) > 200:
                            main_error = main_error[:197] + "..."

                        if len(specific_errors) > 1:
                            error_note = f"{main_error} (+ {len(specific_errors) - 1} other errors)"
                        else:
                            error_note = main_error
                    else:
                        # Fallback to generic message
                        if processed_count == 0:
                            error_note = f"Failed to process all {len(nc_files_to_process)} NetCDF files"
                        else:
                            error_note = f"Partially processed {processed_count}/{len(nc_files_to_process)} files"

                    # Add skipped files info if any
                    if skipped_nc_files:
                        error_note += f". Skipped {len(skipped_nc_files)} files from older revisions"

                    self._update_file_import_status(jar_file.name, False,
                                                    error_note)

                    # Log all specific errors for debugging
                    for i, error in enumerate(specific_errors):
                        logger.error(
                            f"Error {i + 1} in {jar_file.name}: {error}")

                    return all_successful

        except zipfile.BadZipFile as e:
            # Handle corrupted zip files
            error_msg = f"Corrupted zip file: {jar_file} - {str(e)}"
            logger.error(error_msg)
            self._update_file_import_status(jar_file.name, False, error_msg)
            return False
        except Exception as e:
            # Handle any other unexpected errors during JAR processing
            error_msg = f"Unexpected error processing JAR file {jar_file}: {str(e)}"
            logger.exception(error_msg)
            self._update_file_import_status(jar_file.name, False, error_msg)
            return False

    def process_single_nc_file(self, zip_ref, filename, jar_name):
        """
        Process a single NetCDF file from a JAR archive.

        Args:
            zip_ref: ZipFile reference for reading the file
            filename (str): Name of the NetCDF file within the JAR
            jar_name (str): Name of the containing JAR file for logging

        Returns:
            tuple: (bool, str) - Processing result and specific error message if any
        """
        try:
            # Read NetCDF data and metadata
            data, metadata = read_netcdf(zip_ref, filename)

            # Convert to structured DataFrames
            metadata_df, data_df, skip_reason = (
                self.df_converter.convert_to_dataframe(
                    data, metadata))

            if skip_reason:
                # File should be skipped due to station not found or other validation issues
                full_reason = f"{skip_reason} - File: {filename} in {jar_name}"
                logger.warning(f"Skipping file: {full_reason}")
                # Return both False and the specific reason
                return False, full_reason

            if data_df.empty and metadata_df.empty:
                warning_msg = (f"No valid data extracted from {filename} in "
                               f"{jar_name}")
                logger.warning(warning_msg)
                return False, warning_msg

            # Save data to database
            success = self.save_data_copy_method(metadata_df, data_df,
                                                 jar_name)
            return success, None if success else "Database save failed"

        except Exception as e:
            error_msg = f"Error processing {filename} in {jar_name}: {e}"
            logger.error(error_msg)
            return False, error_msg

    def save_data_copy_method(self, df_header, df_data, filename):
        """
        Save header and data DataFrames to database using PostgreSQL COPY
        method.

        Args:
            df_header (pd.DataFrame): Header/metadata DataFrame
            df_data (pd.DataFrame): Measurement data DataFrame
            filename (str): Source filename for logging and tracking

        Returns:
            bool: True if save operation succeeded, False otherwise
        """
        if df_header.empty and df_data.empty:
            warning_msg = f"No data to save for {filename}"
            logger.warning(warning_msg)
            self._update_file_import_status(filename, False, warning_msg)
            return False

        with get_connection() as conn:
            try:
                with conn.cursor() as cursor:
                    # Insert header data if available
                    if not df_header.empty:
                        logger.debug(
                            f"Saving {len(df_header)} header rows for "
                            f"{filename}")
                        self.db_ops.bulk_insert_copy(cursor, df_header,
                                                     "header")

                    # Insert measurement data if available
                    if not df_data.empty:
                        logger.debug(
                            f"Saving {len(df_data)} data rows for {filename}")

                        self.db_ops.bulk_insert_copy(cursor, df_data,
                                                     "data")
                    else:
                        warning_msg = f"Empty data DataFrame for {filename}"
                        logger.warning(warning_msg)
                        self._update_file_import_status(filename, False,
                                                        warning_msg)
                        return False

                    # Update import tracking table for successful import
                    self.db_ops.update_import_status(cursor, filename,
                                                     self.sonde_type,
                                                     success=True)
                    logger.info(f"Successfully saved {filename}")
                    return True

            except Exception as e:
                error_msg = f"Database error saving {filename}: {e}"
                logger.error(error_msg, exc_info=True)
                self._update_file_import_status(filename, False, error_msg)
                return False

    def _update_file_import_status(self, filename, success,
                                   error_message=None):
        """
        Update the file import status in the database.

        Args:
            filename (str): Name of the file being processed
            success (bool): Whether the processing was successful
            error_message (str): Error message for failed imports
        """
        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    self.db_ops.update_import_status(cursor, filename,
                                                     self.sonde_type,
                                                     success,
                                                     error_message)
                    logger.debug(
                        f"Updated import status for {filename}: success={success}, error={error_message}")
        except Exception as e:
            logger.error(f"Failed to update import status for {filename}: {e}")

    def check_files_imported_bulk(self, filenames):
        """
        Check which files have already been imported.

        Args:
            filenames (list): List of filenames to check

        Returns:
            set: Set of filenames that are already imported
        """
        return self.db_ops.check_files_imported_bulk(filenames,
                                                     self.sonde_type)

    def add_files_to_import_bulk(self, filenames):
        """
        Add files to import tracking table.

        Args:
            filenames (list): List of filenames to mark for import
        """
        self.db_ops.add_files_to_import_bulk(filenames, self.sonde_type)
