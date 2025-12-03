import logging
import multiprocessing
import re
import signal
import time

from processors.processor import GRUANProcessor

logger = logging.getLogger(__name__)


def init_worker():
    """
    Initialize worker process to ignore interrupt signals.
    This allows the main process to handle KeyboardInterrupt gracefully.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def process_file_wrapper(args):
    """
    Wrapper function for processing individual files in worker processes.

    Args:
        args (tuple): (jar_file, sonde_type, station_data) - File to process,
        sonde type, and preloaded station data

    Returns:
        tuple: (success, filename, error_message) - Processing result,
        filename, and error message if any
    """
    jar_file, sonde_type, station_data = args
    try:
        logger.debug(f"Starting processing of {jar_file.name}")
        processor = GRUANProcessor(sonde_type)

        # Preload station data into the processor's station manager
        if station_data is not None:
            processor.df_converter.station_manager._cache = station_data.copy()

        success = processor.process_single_jar_file(jar_file)

        logger.debug(
            f"Completed processing of {jar_file.name}: success={success}")
        return success, jar_file.name, None

    except Exception as e:
        error_msg = f"Process crash while processing {jar_file}: {e}"
        logger.error(error_msg, exc_info=True)

        # Try to update the database with the error even if the processor crashed
        try:
            processor = GRUANProcessor(sonde_type)
            processor._update_file_import_status(jar_file.name, False,
                                                 error_msg)
        except Exception as db_error:
            logger.error(
                f"Failed to update database status for {jar_file.name}: {db_error}")

        return False, jar_file.name, error_msg

def import_gruan_parallel(sonde_type):
    """
    Main function for parallel import of GRUAN radiosonde data.

    Args:
        sonde_type (str): Type of radiosonde ('RS41' or 'RS92')
    """
    processor = GRUANProcessor(sonde_type)

    # Validate that the data directory exists
    if not processor.path_files.exists():
        logger.warning(
            f"Path not found for sonde type {sonde_type}: "
            f"{processor.path_files}")
        return

    logger.info(f"Scanning for {sonde_type} files in {processor.path_files}")

    # Find all JAR files in the directory tree
    jar_files = list(processor.path_files.rglob('*.jar'))

    # Regular expression to extract key fields from GRUAN filename
    pattern = re.compile(
        r'(?P<station>[A-Z]{3})-'  # Station code (3 letters) before -RS-
        r'RS-\d+_'  # Skip RS-01_ or RS-02_ part
        r'(?P<extra>\d+_)?'
        r'(?P<product>(?:RS[0-9A-Z\-]+|IMS-\d+-GDP))'
        r'_(?P<version>\d{3})_'
        r'(?P<datetime>\d{8}T\d{6})_'
        r'(?P<group>\d-\d{3}-\d{3})_'
        r'(?P<id>[A-Z0-9]+)'
        r'(?:_rev(?P<rev>\d{3}))?\.jar$'
    )

    # Dictionary to maintain only the latest version of each sounding
    # Now also considering station folder priority (RS-02 > RS-01)
    latest_files = {}
    # List to track skipped files with their reasons
    skipped_files = []

    for f in jar_files:
        m = pattern.search(f.name)
        if not m:
            # Skip files that don't match the pattern and record reason
            skip_reason = "Filename does not match GRUAN pattern"
            skipped_files.append((f.name, skip_reason))
            logger.debug(f"Skipping {f.name}: {skip_reason}")
            continue

        # Extract station folder information from the path
        # Look for RS-01 or RS-02 in the path
        folder_priority = 2  # Default priority (RS-01 = lower priority)
        path_parts = f.parts
        for part in path_parts:
            if part.endswith('-RS-02'):
                folder_priority = 1  # Highest priority (RS-02)
                break
            elif part.endswith('-RS-01'):
                folder_priority = 2  # Lower priority (RS-01)
                # Don't break here, keep looking for RS-02

        # Extract station code (just the 3-letter code, e.g., "BAR")
        station_code = m.group('station')

        # Create unique key using station code and launch datetime
        key = (station_code, m.group('datetime'))

        logger.debug(f"File: {f.name}, Station: {station_code}, Folder Priority: {folder_priority}, Key: {key}")

        # Extract version information from group and revision
        group_main, group_mid, group_last = m.group('group').split('-')
        group_num = int(group_last)

        # Handle optional revision number - default to 0 if not present
        rev_str = m.group('rev')
        rev_num = int(rev_str) if rev_str is not None else 0

        # Keep only the most recent version considering both folder priority and revision
        if key not in latest_files:
            latest_files[key] = (folder_priority, group_num, rev_num, f, [])
            logger.debug(f"Added new file for key {key}: {f.name}")
        else:
            prev_folder_priority, prev_group, prev_rev, prev_file, prev_skipped = latest_files[key]

            # Determine which file has priority
            current_has_priority = False
            skip_reason = ""

            # First check folder priority (RS-02 has higher priority than RS-01)
            if folder_priority < prev_folder_priority:
                # Current file has better folder priority (lower number = higher priority)
                current_has_priority = True
                skip_reason = f"Superseded by file from higher priority folder (RS-0{folder_priority}): {f.name}"
                logger.debug(
                    f"Current file has better folder priority: {folder_priority} vs {prev_folder_priority}")
            elif folder_priority > prev_folder_priority:
                # Previous file has better folder priority
                skip_reason = f"Superseded by file from higher priority folder (RS-0{prev_folder_priority}): {prev_file.name}"
                logger.debug(
                    f"Previous file has better folder priority: {prev_folder_priority} vs {folder_priority}")
            else:
                # Same folder priority, check group and revision numbers
                if (group_num > prev_group) or (group_num == prev_group and rev_num > prev_rev):
                    # Current file is newer
                    current_has_priority = True
                    skip_reason = f"Superseded by newer version: {f.name} (group {group_num}, rev {rev_num} vs group {prev_group}, rev {prev_rev})"
                    logger.debug(
                        f"Current file has newer version: group {group_num}, rev {rev_num} vs group {prev_group}, rev {prev_rev}")
                else:
                    # Previous file is newer or same
                    skip_reason = f"Superseded by newer version: {prev_file.name} (group {prev_group}, rev {prev_rev} vs group {group_num}, rev {rev_num})"
                    logger.debug(
                        f"Previous file has newer version: group {prev_group}, rev {prev_rev} vs group {group_num}, rev {rev_num}")

            if current_has_priority:
                # Current file is better, skip the previous one
                prev_skipped.append((prev_file.name, skip_reason))
                latest_files[key] = (folder_priority, group_num, rev_num, f, prev_skipped)
                logger.debug(f"Replacing {prev_file.name} with {f.name}: {skip_reason}")
            else:
                # Previous file is better, skip the current one
                prev_skipped.append((f.name, skip_reason))
                latest_files[key] = (prev_folder_priority, prev_group, prev_rev, prev_file, prev_skipped)
                logger.debug(f"Skipping {f.name}: {skip_reason}")

    # Final list of files to process (only highest priority versions)
    jar_files_to_process = [info[3] for info in latest_files.values()]

    # Collect all skipped files from the latest_files dictionary
    all_skipped_files = []
    for folder_priority, group_num, rev_num, file, skipped in latest_files.values():
        all_skipped_files.extend(skipped)

    # Also add files that didn't match the pattern
    all_skipped_files.extend(skipped_files)

    # Log detailed information about what was selected
    logger.info(f"Total JAR files found: {len(jar_files)}")
    logger.info(f"Files to process (highest priority versions): {len(jar_files_to_process)}")
    logger.info(f"Files skipped (lower priority/older versions/invalid patterns): {len(all_skipped_files)}")

    # Log which files were selected and why
    for folder_priority, group_num, rev_num, file, skipped in latest_files.values():
        folder_type = "RS-02" if folder_priority == 1 else "RS-01"
        logger.info(f"Selected: {file.name} (from {folder_type}, group {group_num}, rev {rev_num})")
        for skipped_file, skip_reason in skipped:
            logger.info(f"  Skipped: {skipped_file} - {skip_reason}")

    if not jar_files_to_process and not all_skipped_files:
        logger.info(f"No JAR files found for {sonde_type}")
        return

    # Mark skipped files in the database
    if all_skipped_files:
        processor.db_ops.mark_files_as_skipped_bulk(all_skipped_files, sonde_type)
        logger.info(f"Marked {len(all_skipped_files)} files as skipped in database")

    if not jar_files_to_process:
        logger.info(f"No files to process for {sonde_type} (all files were skipped)")
        return

    # Check which files have already been imported from the ones we want to process
    filenames_to_check = [f.name for f in jar_files_to_process]
    imported_files = processor.check_files_imported_bulk(filenames_to_check)

    # Filter out already imported files
    jar_files_to_import = [f for f in jar_files_to_process
                           if f.name not in imported_files]

    # Mark newly skipped files (already imported)
    newly_skipped = []
    for f in jar_files_to_process:
        if f.name in imported_files:
            skip_reason = "File already imported"
            newly_skipped.append((f.name, skip_reason))
            logger.debug(f"Skipping {f.name}: {skip_reason}")

    if newly_skipped:
        processor.db_ops.mark_files_as_skipped_bulk(newly_skipped, sonde_type)
        logger.info(f"Marked {len(newly_skipped)} already imported files as skipped")

    logger.info(f"New files to import: {len(jar_files_to_import)}")

    if not jar_files_to_import:
        logger.info(f"No new files found for {sonde_type}")
        return

    # Mark files for import in the database
    processor.add_files_to_import_bulk([f.name for f in jar_files_to_import])

    # Preload station data to avoid multiple database queries
    station_data = None
    try:
        station_manager = processor.df_converter.station_manager
        station_manager.load_stations()
        station_data = station_manager._cache.copy()
        logger.info(
            f"Preloaded {len(station_data)} stations for parallel "
            f"processing")
    except Exception as e:
        logger.error(f"Failed to preload station data: {e}")
        # Continue without preloaded data

    # Prepare arguments for parallel processing
    task_args = [(jar_file, sonde_type, station_data) for jar_file in
                 jar_files_to_import]

    # Calculate optimal number of processes
    num_processes = min(multiprocessing.cpu_count() - 1,
                        len(jar_files_to_import))
    num_processes = max(1, num_processes)  # Ensure at least 1 process

    logger.info(
        f"Starting parallel import with {num_processes} processes for "
        f"{sonde_type}")
    logger.info(f"Processing {len(jar_files_to_import)} files")

    start_time = time.time()
    processed_count = 0
    failed_count = 0

    # Create process pool for parallel execution
    with (multiprocessing.Pool(processes=num_processes,
                               initializer=init_worker) as pool):
        try:
            # Process files in parallel using imap_unordered for better
            # performance
            for i, (success, filename, error_message) in enumerate(
                    pool.imap_unordered(process_file_wrapper, task_args)):
                processed_count += 1

                if success:
                    logger.debug(
                        f"Successfully processed {filename} ("
                        f"{processed_count}/{len(jar_files_to_import)})")
                else:
                    failed_count += 1
                    logger.error(
                        f"Failed to process {filename} ({processed_count}/"
                        f"{len(jar_files_to_import)})")
                    if error_message:
                        logger.error(f"Error details: {error_message}")

                # Log progress periodically or every 30 seconds
                if processed_count % 10 == 0 or time.time() - start_time > 30:
                    progress = (processed_count / len(
                        jar_files_to_import)) * 100
                    elapsed = time.time() - start_time
                    files_per_second = processed_count / elapsed \
                        if elapsed > 0 else 0

                    logger.info(
                        f"Progress: {processed_count}/"
                        f"{len(jar_files_to_import)} "
                        f"({progress:.1f}%) - Rate: {files_per_second:.2f} "
                        f"files/sec - "
                        f"Failed: {failed_count}"
                    )

        except KeyboardInterrupt:
            logger.warning(
                "Received interrupt signal, shutting down gracefully...")
            pool.terminate()
            pool.join()
            return
        except Exception as e:
            logger.error(f"Unexpected error in parallel processing: {e}")
            pool.terminate()
            pool.join()
            raise

    # Calculate and log final statistics
    end_time = time.time()
    total_time = end_time - start_time

    logger.info(f"Completed import for {sonde_type}. "
                f"Processed: {processed_count}, Failed: {failed_count}, "
                f"Total time: {total_time:.2f} seconds")

    if processed_count > 0:
        files_per_second = processed_count / total_time
        logger.info(
            f"Overall processing rate: {files_per_second:.2f} files/second")
