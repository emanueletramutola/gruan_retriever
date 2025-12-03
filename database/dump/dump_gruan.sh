#!/bin/bash
# ==============================
# GRUAN Database Export Script
# ==============================
# Description: Exports partitioned data tables and header metadata table from the GRUAN database to compressed CSV files
# Usage: ./script.sh [OPTIONS]
# Options:
#   -s, --start-year YEAR    Start year for data partition export (default: 2004)
#   -e, --end-year YEAR      End year for data partition export (default: 2025)
#   -d, --database DB        Database name (default: gruan)
#   -U, --username USER      Database username (default: gruan_user)
#   -h, --host HOST          Database host (default: localhost)
#   -p, --port PORT          Database port (default: 5432)
#   --header-only            Export only the header table (skip data partitions)
#   --no-compress            Skip compression (keep files as CSV)
#   --help                   Display this help message
# ==============================

# ==============================
# Default Configuration
# ==============================
#BASE_PATH="/Users/emanuele/Data/GRUAN_DUMP"
BASE_PATH="/Users/emanuele/Library/CloudStorage/OneDrive-CNR/backup/GRUAN"
START_YEAR=2004
END_YEAR=2025
DB_NAME="gruan"
DB_USERNAME="gruan_user"
DB_HOST="localhost"
DB_PORT="5432"
HEADER_ONLY=false
COMPRESS=true

# ==============================
# Help Function
# ==============================
show_help() {
    cat << EOF
GRUAN Database Export Script

Exports partitioned data tables (data_YYYY) and header metadata table from GRUAN database.

Usage: $(basename "$0") [OPTIONS]

Options:
    -s, --start-year YEAR       Start year for data partition export (default: ${START_YEAR})
    -e, --end-year YEAR         End year for data partition export (default: ${END_YEAR})
    -d, --database DB           Database name (default: ${DB_NAME})
    -U, --username USER         Database username (default: ${DB_USERNAME})
    -h, --host HOST             Database host (default: ${DB_HOST})
    -p, --port PORT             Database port (default: ${DB_PORT})
    --header-only               Export only the header table (skip data partitions)
    --no-compress               Skip compression (keep files as CSV)
    --help                      Display this help message

Examples:
    $(basename "$0") -s 2018 -e 2020
    $(basename "$0") --start-year 2015 --end-year 2019
    $(basename "$0") -h localhost -p 5432 -U gruan_user -d gruan
    $(basename "$0") --header-only
    $(basename "$0") --header-only --no-compress

Notes:
    - Always exports the 'header' metadata table
    - Exports data partition tables in the format: data_YYYY
    - Uses PGPASSWORD environment variable for authentication
    - Default username: gruan_user (password from GRUAN_USER_PSW environment variable)

EOF
}

# ==============================
# Parse Command Line Arguments
# ==============================
while [[ $# -gt 0 ]]; do
    case "$1" in
        -s|--start-year)
            if [[ -z "${2:-}" ]] || [[ "$2" =~ ^- ]]; then
                echo "Error: --start-year requires a year argument" >&2
                exit 1
            fi
            START_YEAR="$2"
            shift 2
            ;;
        -e|--end-year)
            if [[ -z "${2:-}" ]] || [[ "$2" =~ ^- ]]; then
                echo "Error: --end-year requires a year argument" >&2
                exit 1
            fi
            END_YEAR="$2"
            shift 2
            ;;
        -d|--database)
            if [[ -z "${2:-}" ]] || [[ "$2" =~ ^- ]]; then
                echo "Error: --database requires a database name argument" >&2
                exit 1
            fi
            DB_NAME="$2"
            shift 2
            ;;
        -U|--username)
            if [[ -z "${2:-}" ]] || [[ "$2" =~ ^- ]]; then
                echo "Error: --username requires a username argument" >&2
                exit 1
            fi
            DB_USERNAME="$2"
            shift 2
            ;;
        -h|--host)
            if [[ -z "${2:-}" ]] || [[ "$2" =~ ^- ]]; then
                echo "Error: --host requires a host argument" >&2
                exit 1
            fi
            DB_HOST="$2"
            shift 2
            ;;
        -p|--port)
            if [[ -z "${2:-}" ]] || [[ "$2" =~ ^- ]]; then
                echo "Error: --port requires a port argument" >&2
                exit 1
            fi
            DB_PORT="$2"
            shift 2
            ;;
        --header-only)
            HEADER_ONLY=true
            shift
            ;;
        --no-compress)
            COMPRESS=false
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            echo "Use --help for usage information" >&2
            exit 1
            ;;
    esac
done

# ==============================
# Validation
# ==============================
# Validate year format and range
if ! [[ "$START_YEAR" =~ ^[0-9]{4}$ ]] || ! [[ "$END_YEAR" =~ ^[0-9]{4}$ ]]; then
    echo "Error: Years must be in YYYY format" >&2
    exit 1
fi

if [[ "$START_YEAR" -gt "$END_YEAR" ]]; then
    echo "Error: Start year cannot be greater than end year" >&2
    exit 1
fi

# Validate database connection parameters
if [[ -z "$DB_NAME" ]]; then
    echo "Error: Database name cannot be empty" >&2
    exit 1
fi

if [[ -z "$DB_USERNAME" ]]; then
    echo "Error: Database username cannot be empty" >&2
    exit 1
fi

# Check if password environment variable is set
if [[ -z "$GRUAN_USER_PSW" ]]; then
    echo "Error: GRUAN_USER_PSW environment variable is not set" >&2
    echo "Please set the password using: export GRUAN_USER_PSW='your_password'" >&2
    exit 1
fi

# Set PostgreSQL password environment variable
export PGPASSWORD="$GRUAN_USER_PSW"

# Validate database connection
if ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" -c "SELECT 1" >/dev/null 2>&1; then
    echo "Error: Cannot connect to database '${DB_NAME}' as user '${DB_USERNAME}' on ${DB_HOST}:${DB_PORT}" >&2
    echo "Please check your database connection parameters and ensure the database is running." >&2
    unset PGPASSWORD
    exit 1
fi

# ==============================
# Setup
# ==============================
# Get the current date in YYYYMMDD format
CURRENT_DATE=$(date +%Y%m%d)

# Create the output directory
OUTPUT_DIR="${BASE_PATH}/${CURRENT_DATE}"
mkdir -p "${OUTPUT_DIR}"

# Validate output directory permissions
if [[ ! -w "$OUTPUT_DIR" ]]; then
    echo "Error: Output directory '${OUTPUT_DIR}' is not writable" >&2
    unset PGPASSWORD
    exit 1
fi

echo "========================================="
echo "GRUAN Database Export"
echo "========================================="
echo "Date: ${CURRENT_DATE}"
echo "Database: ${DB_NAME}@${DB_HOST}:${DB_PORT} (user: ${DB_USERNAME})"
echo "Header-only mode: ${HEADER_ONLY}"
echo "Compression: ${COMPRESS}"
if [[ "$HEADER_ONLY" == "false" ]]; then
    echo "Data tables year range: ${START_YEAR} to ${END_YEAR}"
    echo "Table format: data_YYYY"
fi
echo "Metadata table: header"
echo "Output directory: ${OUTPUT_DIR}"
echo "========================================="

# ==============================
# Export Function
# ==============================
# Function to export data from a table to a CSV file and optionally compress it
export_table() {
    local table_name=$1
    local output_file="${OUTPUT_DIR}/${table_name}.csv"

    echo "Exporting ${table_name}..."

    # Check if table exists
    local table_exists
    table_exists=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" -tAc "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='${table_name}')" 2>/dev/null || echo "false")

    if [[ "$table_exists" != "t" ]]; then
        echo "Warning: Table ${table_name} does not exist. Skipping..." >&2
        return 1
    fi

    # Export the table data to CSV
    if ! psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" -c "COPY ${table_name} TO STDOUT WITH (FORMAT CSV, DELIMITER ',', HEADER)" > "${output_file}" 2>/dev/null; then
        echo "Error: Failed to export ${table_name}" >&2
        [[ -f "${output_file}" ]] && rm -f "${output_file}"  # Clean up partial file
        return 1
    fi

    # Check if file was created and has content
    if [[ ! -f "${output_file}" ]]; then
        echo "Error: Output file was not created for ${table_name}" >&2
        return 1
    fi

    if [[ ! -s "${output_file}" ]]; then
        echo "Warning: Table ${table_name} is empty" >&2
        # We don't return error for empty tables, just warn and continue
    fi

    # Check if we can write to the output file
    if [[ ! -w "${output_file}" ]]; then
        echo "Error: Cannot write to output file ${output_file}" >&2
        rm -f "${output_file}"
        return 1
    fi

    # Compress the CSV file if compression is enabled
    if [[ "$COMPRESS" == "true" ]]; then
        # Test compression tools availability
        if command -v pbzip2 >/dev/null 2>&1; then
            echo "Compressing with pbzip2..."
            if pbzip2 -9 "${output_file}" 2>/dev/null; then
                # Verify compressed file was created
                local compressed_file="${output_file}.bz2"
                if [[ -f "$compressed_file" ]]; then
                    echo "✓ Successfully exported and compressed ${table_name}"
                    return 0
                else
                    echo "Warning: pbzip2 completed but compressed file not found for ${table_name}" >&2
                    # Continue with uncompressed file
                fi
            else
                echo "Warning: pbzip2 failed for ${table_name}, keeping uncompressed file" >&2
                # Continue with uncompressed file
            fi
        elif command -v bzip2 >/dev/null 2>&1; then
            echo "Compressing with bzip2..."
            if bzip2 -9 "${output_file}" 2>/dev/null; then
                # Verify compressed file was created
                local compressed_file="${output_file}.bz2"
                if [[ -f "$compressed_file" ]]; then
                    echo "✓ Successfully exported and compressed ${table_name}"
                    return 0
                else
                    echo "Warning: bzip2 completed but compressed file not found for ${table_name}" >&2
                    # Continue with uncompressed file
                fi
            else
                echo "Warning: bzip2 failed for ${table_name}, keeping uncompressed file" >&2
                # Continue with uncompressed file
            fi
        else
            echo "Warning: No compression tools available (pbzip2 or bzip2), keeping uncompressed file" >&2
        fi
    fi

    # If we reach here, either compression is disabled or compression failed
    echo "✓ Successfully exported ${table_name} (uncompressed)"
    return 0
}

# ==============================
# Main Export Process
# ==============================
export_count=0
error_count=0
skipped_count=0

echo "Starting export process..."
echo "Progress:"

# Export header metadata table (always attempt)
echo "Exporting header metadata table..."
if export_table "header"; then
    ((export_count++))
    echo "✓ Header metadata table exported successfully"
else
    # Check if the failure was due to table not existing
    table_exists=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" -tAc "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='header')" 2>/dev/null || echo "false")
    if [[ "$table_exists" != "t" ]]; then
        ((skipped_count++))
        echo "SKIP: Header table not found"
    else
        ((error_count++))
        echo "ERROR: Header table export failed"
    fi
fi

# Export partitioned data tables only if not in header-only mode
if [[ "$HEADER_ONLY" == "false" ]]; then
    echo "-----------------------------------------"
    echo "Exporting partitioned data tables..."

    # Use while loop for better control
    year=$START_YEAR
    while [[ $year -le $END_YEAR ]]; do
        table_name="data_${year}"

        printf "Year %4d: " "$year"

        if export_table "$table_name"; then
            ((export_count++))
        else
            # Check if the failure was due to table not existing
            table_exists=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USERNAME" -d "$DB_NAME" -tAc "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name='${table_name}')" 2>/dev/null || echo "false")
            if [[ "$table_exists" != "t" ]]; then
                ((skipped_count++))
                echo "SKIP (table not found)"
            else
                ((error_count++))
                echo "ERROR (export failed)"
            fi
        fi

        ((year++))
    done
else
    echo "-----------------------------------------"
    echo "Skipping data partition tables (header-only mode)"
fi

# ==============================
# Cleanup and Summary
# ==============================
# Clear the password from environment for security
unset PGPASSWORD

echo "========================================="
echo "Export Summary"
echo "========================================="
echo "Mode: $([[ "$HEADER_ONLY" == "true" ]] && echo "Header-only" || echo "Full export")"
echo "Compression: ${COMPRESS}"
echo "Successfully exported: ${export_count} table(s)"
echo "Skipped (not found): ${skipped_count} table(s)"
echo "Errors encountered: ${error_count} table(s)"
echo "Output directory: ${OUTPUT_DIR}"
echo "========================================="

if [[ $error_count -gt 0 ]]; then
    echo "Warning: Some exports failed. Check errors above." >&2
    exit 1
fi

if [[ $export_count -eq 0 ]]; then
    echo "Warning: No tables were exported." >&2
    exit 1
fi

echo "Export process completed successfully."