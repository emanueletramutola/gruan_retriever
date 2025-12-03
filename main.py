import logging.config
import multiprocessing
import time

from config import LOGGING_CONFIG
from processors.parallel_import import import_gruan_parallel
from utils.station_manager import update_station_database

# Configure logging using the settings from config.yaml
logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger(__name__)


def main():
    """
    Main function that orchestrates the parallel import of GRUAN radiosonde
    data.
    """
    total_start_time = time.time()
    logger.info("Starting GRUAN data import process with multiprocessing")

    try:
        # Update station database from GRUAN website
        logger.info("Updating station database from GRUAN website...")
        update_station_database()

        # Process each sonde type sequentially
        for sonde_type in ['RS92', 'RS41', 'RS11G', 'IMS100']:
            start_time = time.time()
            logger.info(f"Starting import for {sonde_type}")

            # Execute parallel import for the current sonde type
            import_gruan_parallel(sonde_type)

            end_time = time.time()
            execution_time = end_time - start_time
            logger.info(
                f"Completed {sonde_type} in {execution_time:.2f} seconds")

    except Exception as e:
        logger.exception(f"Critical error in main process: {e}")
        raise

    finally:
        # Calculate and log total execution time regardless of success/failure
        total_end_time = time.time()
        total_execution_time = total_end_time - total_start_time

        # Convert total time to human-readable format (HH:MM:SS)
        hours, remainder = divmod(total_execution_time, 3600)
        minutes, seconds = divmod(remainder, 60)

        # Log total execution time in both formatted and raw formats
        logger.info(
            f"Total execution time: {int(hours):02d}:{int(minutes):02d}:"
            f"{seconds:06.3f} (HH:MM:SS)")
        logger.info(
            f"Total execution time: {total_execution_time:.3f} seconds")

        # Print execution summary to console for immediate visibility
        print("\n=== EXECUTION SUMMARY ===")
        print(
            f"Hours: {int(hours)}, Minutes: {int(minutes)}, Seconds: "
            f"{seconds:.3f}")
        print(f"Total seconds: {total_execution_time:.3f}")
        print("Check files_to_import table for detailed import status")


if __name__ == '__main__':
    # Set multiprocessing start method to 'spawn' for better compatibility
    # This creates fresh Python processes rather than forking
    multiprocessing.set_start_method('spawn', force=True)

    # Execute main function
    main()