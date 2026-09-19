#!/bin/bash
#
# dump_gruan.sh
#
# Exports all GRUAN-related tables (header, files_to_import, station, and the
# monthly data_YYYYMM tables) from a PostgreSQL database to CSV files, then
# compresses each CSV with pbzip2.
#
# Connection parameters can be overridden via environment variables:
#   DB_NAME, DB_USER, DB_HOST, DB_PORT, PGPASSWORD
# The database password can be supplied via PGPASSWORD or via a ~/.pgpass file.
#
# Usage:
#   ./dump_gruan.sh [-b BASE_PATH] [-s START_YEAR] [-e END_YEAR] [-j COMPRESSION_THREADS]
#
# Exit codes:
#   0  - success (all exports and compressions completed)
#   1  - fatal error (missing dependency, cannot create output dir, DB unreachable)
#   2  - completed with one or more non-fatal export failures

set -Eeuo pipefail
IFS=$'\n\t'

# --------------------------------------------------------------------------
# Configuration (overridable via environment variables and/or CLI options)
# --------------------------------------------------------------------------
BASE_PATH="${BASE_PATH:-/backup/GRUAN}"
DB_NAME="${DB_NAME:-gruan}"
DB_USER="${DB_USER:-gruan_user}"
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
START_YEAR="${START_YEAR:-2004}"
END_YEAR="${END_YEAR:-2030}"
COMPRESSION_THREADS="${COMPRESSION_THREADS:-4}"

CURRENT_DATE="$(date +%Y%m%d)"
CURRENT_YEAR="$(date +%Y)"
CURRENT_MONTH="$(date +%-m)"
OUTPUT_DIR="${BASE_PATH}/${CURRENT_DATE}"
LOG_FILE="${OUTPUT_DIR}/dump_gruan.log"

FAILED_TABLES=()
EXPORTED_COUNT=0

# --------------------------------------------------------------------------
# Logging helpers
# --------------------------------------------------------------------------
log() {
    local level="$1"; shift
    local ts
    ts="$(date '+%Y-%m-%d %H:%M:%S')"
    local line="[${ts}] [${level}] $*"
    echo "${line}"
    # Log to file too, once OUTPUT_DIR exists
    if [ -d "${OUTPUT_DIR}" ]; then
        echo "${line}" >> "${LOG_FILE}"
    fi
}

log_info()  { log "INFO"  "$@"; }
log_warn()  { log "WARN"  "$@"; }
log_error() { log "ERROR" "$@" >&2; }

die() {
    log_error "$@"
    exit 1
}

# --------------------------------------------------------------------------
# Cleanup / trap handling
# --------------------------------------------------------------------------
on_error() {
    local exit_code=$?
    local line_no=$1
    log_error "Script aborted unexpectedly at line ${line_no} (exit code ${exit_code})."
    exit "${exit_code}"
}
trap 'on_error ${LINENO}' ERR

# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------
usage() {
    cat <<EOF
Usage: $(basename "$0") [-b BASE_PATH] [-s START_YEAR] [-e END_YEAR] [-j THREADS]

Options:
  -b BASE_PATH   Base backup directory (default: ${BASE_PATH})
  -s START_YEAR  First year to export data for (default: ${START_YEAR})
  -e END_YEAR    Last year to export data for (default: ${END_YEAR})
  -j THREADS     Number of threads for pbzip2 compression (default: ${COMPRESSION_THREADS})
  -h             Show this help message and exit

Database connection is configured via environment variables:
  DB_NAME (default: gruan)
  DB_USER (default: gruan_user)
  DB_HOST (default: localhost)
  DB_PORT (default: 5432)
  PGPASSWORD (optional; falls back to ~/.pgpass if unset)
EOF
}

while getopts ":b:s:e:j:h" opt; do
    case "${opt}" in
        b) BASE_PATH="${OPTARG}"; OUTPUT_DIR="${BASE_PATH}/${CURRENT_DATE}"; LOG_FILE="${OUTPUT_DIR}/dump_gruan.log" ;;
        s) START_YEAR="${OPTARG}" ;;
        e) END_YEAR="${OPTARG}" ;;
        j) COMPRESSION_THREADS="${OPTARG}" ;;
        h) usage; exit 0 ;;
        \?) echo "Invalid option: -${OPTARG}" >&2; usage; exit 1 ;;
        :) echo "Option -${OPTARG} requires an argument." >&2; usage; exit 1 ;;
    esac
done

# --------------------------------------------------------------------------
# Pre-flight checks
# --------------------------------------------------------------------------
check_dependencies() {
    local missing=()
    for cmd in psql pbzip2 date seq; do
        if ! command -v "${cmd}" >/dev/null 2>&1; then
            missing+=("${cmd}")
        fi
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        die "Missing required command(s): ${missing[*]}. Please install them and retry."
    fi
}

check_db_connection() {
    log_info "Checking connectivity to database '${DB_NAME}' on ${DB_HOST}:${DB_PORT} as user '${DB_USER}'..."
    if ! psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -Atc "SELECT 1;" >/dev/null 2>&1; then
        die "Unable to connect to database '${DB_NAME}' on ${DB_HOST}:${DB_PORT} as user '${DB_USER}'. Check DB_HOST/DB_PORT/DB_USER/DB_NAME, PGPASSWORD, or ~/.pgpass."
    fi
    log_info "Database connection OK."
}

table_exists() {
    local table_name="$1"
    local result
    result="$(psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -Atc \
        "SELECT to_regclass('public.${table_name}') IS NOT NULL;" 2>/dev/null || echo "f")"
    [ "${result}" = "t" ]
}

# --------------------------------------------------------------------------
# Export function
# --------------------------------------------------------------------------
export_and_compress() {
    local table_name="$1"
    local output_file="${OUTPUT_DIR}/${table_name}.csv"
    local compressed_file="${output_file}.bz2"

    if ! table_exists "${table_name}"; then
        log_warn "Table '${table_name}' does not exist. Skipping."
        return 0
    fi

    log_info "Exporting '${table_name}' to ${output_file}..."
    if ! psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
            -c "COPY ${table_name} TO STDOUT WITH CSV DELIMITER ',' HEADER" \
            > "${output_file}" 2>>"${LOG_FILE}"; then
        log_error "Failed to export table '${table_name}'."
        rm -f "${output_file}"
        FAILED_TABLES+=("${table_name} (export)")
        return 1
    fi

    if [ ! -s "${output_file}" ]; then
        log_warn "Export of '${table_name}' produced an empty file."
    fi

    log_info "Compressing ${output_file}..."
    if ! pbzip2 -f -9 -p"${COMPRESSION_THREADS}" "${output_file}" 2>>"${LOG_FILE}"; then
        log_error "Failed to compress '${output_file}'."
        FAILED_TABLES+=("${table_name} (compression)")
        return 1
    fi

    log_info "Compressed ${output_file} -> ${compressed_file}"
    EXPORTED_COUNT=$((EXPORTED_COUNT + 1))
    return 0
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
main() {
    check_dependencies

    mkdir -p "${OUTPUT_DIR}" || die "Could not create output directory '${OUTPUT_DIR}'."

    log_info "Starting GRUAN export. Output directory: ${OUTPUT_DIR}"

    export PGPASSWORD="${PGPASSWORD:-}"
    if [ -z "${PGPASSWORD}" ]; then
        log_info "PGPASSWORD is not set; relying on ~/.pgpass for authentication."
        unset PGPASSWORD
    fi

    check_db_connection

    # Fixed lookup / metadata tables
    export_and_compress "header" || true
    export_and_compress "files_to_import" || true
    export_and_compress "station" || true

    # Monthly data tables
    for year in $(seq "${START_YEAR}" "${END_YEAR}"); do
        # Skip years in the future
        if [ "${year}" -gt "${CURRENT_YEAR}" ]; then
            continue
        fi
        for month in $(seq -w 1 12); do
            # Skip future months in the current year
            if [ "${year}" -eq "${CURRENT_YEAR}" ] && [ "${month#0}" -gt "${CURRENT_MONTH}" ]; then
                continue
            fi
            export_and_compress "data_${year}${month}" || true
        done
    done

    log_info "Export run finished. Successfully exported ${EXPORTED_COUNT} table(s)."

    if [ "${#FAILED_TABLES[@]}" -gt 0 ]; then
        log_error "The following table(s) failed: ${FAILED_TABLES[*]}"
        exit 2
    fi

    log_info "All tables have been exported and compressed successfully."
    exit 0
}

main "$@"