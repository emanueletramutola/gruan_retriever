import io
import logging
from functools import lru_cache

from psycopg2.extras import execute_batch

from database import get_connection
from utils.data_utils import clean_numeric_value

logger = logging.getLogger(__name__)


class DatabaseOperations:
    """
    Handles database operations including bulk inserts, column mapping,
    and file import tracking for radiosonde data.
    """

    def __init__(self):
        # Cache for storing column types to avoid repeated database queries
        self._column_types_cache = {}

        # Map for renaming Python keywords to database-safe column names
        self.column_rename_map = {
            'comment': 'cmt',
            'references': 'refs',
            'group': 'grp',
            'user': 'usr',
            'default': 'def'
        }

        # Reverse mapping to convert back from database names to original names
        self.reverse_rename_map = {v: k for k, v in
                                   self.column_rename_map.items()}

    @lru_cache(maxsize=2)
    def get_columns_excluding_serial_pk_cached(self, table_name):
        """
        Get column names for a table excluding the primary key columns.
        For header tables, exclude 'report_id'. For data tables, exclude
        'observation_id'.
        """

        try:
            with get_connection() as conn:
                with conn.cursor() as cursor:
                    # Execute a dummy query to get all column names from the
                    # table
                    cursor.execute(f"SELECT * FROM {table_name} LIMIT 0")
                    columns = [desc[0] for desc in cursor.description]
                    logger.debug(
                        f"All columns for table {table_name}: {columns}")

                    # Define which columns to exclude based on table type
                    if table_name.endswith('_header'):
                        columns_to_exclude = ['report_id']
                    elif table_name.endswith('_data'):
                        columns_to_exclude = ['observation_id']
                    else:
                        columns_to_exclude = []

                    # Remove excluded columns from the list
                    for col in columns_to_exclude:
                        if col in columns:
                            columns.remove(col)
                            logger.debug(
                                f"Removed column '{col}' from column list for "
                                f"table {table_name}")
                        else:
                            logger.warning(
                                f"Column '{col}' not found in table "
                                f"{table_name}")

                    return columns

        except Exception as e:
            logger.error(
                f"Error retrieving columns for table {table_name}: {str(e)}",
                exc_info=True)
            raise

    def get_column_types_excluding_serial_pk_cached(self, table_name):
        """
        Get column data types for a table excluding the primary key columns.
        For header tables, exclude 'report_id'. For data tables, exclude
        'observation_id'.
        """
        cache_key = f"{table_name}_types_excluding_serial_pk"

        if cache_key not in self._column_types_cache:
            try:
                with get_connection() as conn:
                    with conn.cursor() as cursor:
                        # Query information schema to get all column data types
                        cursor.execute("""
                                       SELECT column_name, data_type
                                       FROM information_schema.columns
                                       WHERE table_name = %s
                                       """, (table_name,))

                        # Convert to dictionary
                        columns_dict = dict(cursor.fetchall())
                        logger.debug(
                            f"All column types for table {table_name}: "
                            f"{columns_dict}")

                        # Define which columns to exclude based on table type
                        if table_name.endswith('_header'):
                            columns_to_exclude = ['report_id']
                        elif table_name.endswith('_data'):
                            columns_to_exclude = ['observation_id']
                        else:
                            columns_to_exclude = []

                        # Remove excluded columns from the dictionary
                        for col in columns_to_exclude:
                            if col in columns_dict:
                                del columns_dict[col]
                                logger.debug(
                                    f"Removed column '{col}' from column "
                                    f"types for table {table_name}")
                            else:
                                logger.warning(
                                    f"Column '{col}' not found in column "
                                    f"types for table {table_name}")

                        self._column_types_cache[cache_key] = columns_dict

            except Exception as e:
                logger.error(
                    f"Error retrieving column types for table {table_name}: "
                    f"{str(e)}",
                    exc_info=True)
                raise

        return self._column_types_cache[cache_key]

    def bulk_insert_copy(self, cursor, df, table_name):
        """
        Perform bulk insert using PostgreSQL COPY command for high performance.

        Args:
            cursor: Database cursor
            df (pd.DataFrame): DataFrame containing data to insert
            table_name (str): Target table name

        Raises:
            Exception: If COPY operation fails
        """
        if df.empty:
            return

        # Get database schema information
        db_columns = self.get_columns_excluding_serial_pk_cached(table_name)
        column_types = self.get_column_types_excluding_serial_pk_cached(
            table_name)

        # Map DataFrame columns to database columns
        df_columns_mapped = {}
        for df_col in df.columns:
            original_col = self.reverse_rename_map.get(df_col, df_col)
            if original_col in db_columns:
                df_columns_mapped[df_col] = original_col

        if not df_columns_mapped:
            logger.warning(f"No matching columns for table {table_name}")
            return

        # Reorder DataFrame columns to match database schema
        df_reordered = df[list(df_columns_mapped.keys())].copy()
        df_reordered.columns = list(df_columns_mapped.values())

        # Clean numeric values based on database column types
        numeric_types = {'real', 'double precision', 'integer', 'bigint',
                         'smallint', 'numeric'}
        for col in df_reordered.columns:
            col_type = column_types.get(col, '').lower()
            if col_type in numeric_types:
                df_reordered[col] = df_reordered[col].apply(
                    lambda x: clean_numeric_value(x, col_type))

        # Convert DataFrame to CSV format for COPY command
        sio = io.StringIO()
        df_reordered.to_csv(sio, index=False, header=False, na_rep="\\N",
                            sep='\t')
        sio.seek(0)

        # Execute PostgreSQL COPY command for bulk insertion
        columns_str = ', '.join(
            [f'"{col}"' for col in df_columns_mapped.values()])
        copy_sql = (f"COPY {table_name} ({columns_str}) FROM STDIN WITH ("
                    f"DELIMITER E'\\t', NULL '\\N')")

        try:
            cursor.copy_expert(copy_sql, sio)
        except Exception as e:
            # logger.error(f"COPY failed for table {table_name}: {e}")
            # raise

            sio.seek(0)
            content = sio.read()
            lines = content.split('\n')

            logger.error(f"COPY failed for table {table_name}: {e}")
            logger.error(
                f"Total rows: {len(lines)}, Total chars: {len(content)}")
            logger.error(f"First 10 rows:\n{chr(10).join(lines[:10])}")
            logger.error(f"Last 10 rows:\n{chr(10).join(lines[-10:])}")

            # Salva il contenuto completo su file
            with open(f"failed_copy_{table_name}.csv", "w") as f:
                f.write(content)

            raise

    def check_files_imported_bulk(self, filenames, sonde_type):
        """
        Check which files have already been imported in bulk.

        Args:
            filenames (list): List of filenames to check
            sonde_type (str): Type of radiosonde ('RS41' or 'RS92')

        Returns:
            set: Set of filenames that have already been imported
        """
        if not filenames:
            return set()

        with get_connection() as conn:
            with conn.cursor() as cursor:
                placeholders = ', '.join(['%s'] * len(filenames))
                cursor.execute(
                    f"SELECT filename FROM files_to_import WHERE "
                    f"filename IN ({placeholders}) "
                    f"AND sonde = %s "
                    f"AND date_of_import IS NOT NULL",
                    filenames + [sonde_type]
                )
                return {row[0] for row in cursor.fetchall()}

    def add_files_to_import_bulk(self, filenames, sonde_type):
        """
        Add files to import tracking table.

        Args:
            filenames (list): List of filenames to mark for import
            sonde_type (str): Type of radiosonde ('RS41' or 'RS92')
        """
        if not filenames:
            return

        with get_connection() as conn:
            with conn.cursor() as cursor:
                # Prepare records for batch insertion
                records = [(sonde_type, fname) for fname in filenames]
                execute_batch(
                    cursor,
                    "INSERT INTO files_to_import (sonde, filename) VALUES (%s, %s) "
                    "ON CONFLICT (sonde, filename) DO NOTHING",
                    records,
                    page_size=500  # Optimize batch size for performance
                )

    def mark_files_as_skipped_bulk(self, skipped_files_info, sonde_type):
        """
        Mark files as skipped with specific reasons in bulk.

        Args:
            skipped_files_info (list): List of tuples (filename, skip_reason)
            sonde_type (str): Type of radiosonde
        """
        if not skipped_files_info:
            return

        with get_connection() as conn:
            with conn.cursor() as cursor:
                # First ensure files exist in the table
                filenames = [info[0] for info in skipped_files_info]
                placeholders = ', '.join(['%s'] * len(filenames))
                cursor.execute(
                    f"INSERT INTO files_to_import (sonde, filename) "
                    f"SELECT %s, unnest(%s::text[]) "
                    f"ON CONFLICT (sonde, filename) DO NOTHING",
                    (sonde_type, filenames)
                )

                # Now update the skip reasons
                for filename, skip_reason in skipped_files_info:
                    truncated_reason = skip_reason[
                        :255] if skip_reason and len(
                        skip_reason) > 255 else skip_reason
                    cursor.execute(
                        "UPDATE files_to_import SET note = %s, date_of_import = NULL "
                        "WHERE filename = %s AND sonde = %s",
                        (truncated_reason, filename, sonde_type)
                    )

    def update_import_status(self, cursor, filename, sonde_type, success=True,
                             error_message=None):
        """
        Update import status by setting the date_of_import for successful imports
        or updating the note column for failed imports.

        Args:
            cursor: Database cursor
            filename (str): Name of the imported file
            sonde_type (str): Type of radiosonde ('RS41' or 'RS92')
            success (bool): Whether the import was successful
            error_message (str): Error message to record in note column for failed imports
        """
        if success:
            cursor.execute(
                "UPDATE files_to_import SET date_of_import = CURRENT_TIMESTAMP, note = NULL "
                "WHERE filename = %s AND sonde = %s",
                (filename, sonde_type),
            )
        else:
            # Truncate error message if too long for the database column
            truncated_error = error_message[:255] if error_message and len(
                error_message) > 255 else error_message

            cursor.execute(
                "UPDATE files_to_import SET date_of_import = NULL, note = %s "
                "WHERE filename = %s AND sonde = %s",
                (truncated_error, filename, sonde_type),
            )
        logger.debug(
            f"Executed update for {filename}: success={success}, error={error_message}")