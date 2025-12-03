#!/bin/bash
set -e

###############################################################################
# Script: create_database_user.sh
# Description: Creates or updates a PostgreSQL database user for the GRUAN application
#              with appropriate privileges and permissions. Handles different auth methods.
#
# Usage:
#   ./create_database_user.sh [superuser] [host] [port] [app_user] [app_password]
#
# Parameters:
#   superuser    PostgreSQL superuser (default: postgres)
#   host         Database host (default: localhost)
#   port         Database port (default: 5432)
#   app_user     Application database user (default: gruan_user)
#   app_password Application user password (default: xxx)
#
# Examples:
#   # Use all default values
#   ./create_database_user.sh
#
#   # Custom superuser and host
#   ./create_database_user.sh myadmin 192.168.1.100
#
#   # Full custom configuration
#   ./create_database_user.sh postgres localhost 5432 my_app_user MySecure123!
#
# Notes:
#   - If postgres user has no password, use peer authentication via sudo
#   - For remote connections, set PGPASSWORD environment variable
###############################################################################

# Database superuser parameters (default: postgres)
SUPER_USER="${1:-postgres}"
DB_HOST="${2:-localhost}"
DB_PORT="${3:-5432}"

# Application database user parameters
APP_DB_USER="${4:-gruan_user}"
APP_DB_PASSWORD="${5:-xxx}"

echo "=== Creating database user: $APP_DB_USER ==="

# Check if PostgreSQL client tools are available
if ! command -v psql &> /dev/null; then
    echo "Error: psql command not found. Please install PostgreSQL client tools."
    exit 1
fi

# Function to run psql as superuser
run_psql_superuser() {
    local sql_command="$1"

    # For local connections to postgres user, use peer authentication via sudo
    if [ "$DB_HOST" = "localhost" ] && [ "$SUPER_USER" = "postgres" ]; then
        sudo -u postgres psql -d postgres -c "$sql_command"
    else
        # For remote connections or other users, use password authentication
        psql -h "$DB_HOST" -U "$SUPER_USER" -p "$DB_PORT" -d postgres -c "$sql_command"
    fi
}

# Function to test connection
test_superuser_connection() {
    if [ "$DB_HOST" = "localhost" ] && [ "$SUPER_USER" = "postgres" ]; then
        sudo -u postgres psql -d postgres -c "SELECT 1;" &> /dev/null
    else
        psql -h "$DB_HOST" -U "$SUPER_USER" -p "$DB_PORT" -d postgres -c "SELECT 1;" &> /dev/null
    fi
}

# Test connection to PostgreSQL server as superuser
if ! test_superuser_connection; then
    echo "Error: Cannot connect to PostgreSQL server at $DB_HOST:$DB_PORT as user $SUPER_USER"
    echo "Please verify that:"
    echo "1. PostgreSQL server is running"
    echo "2. User '$SUPER_USER' exists and has superuser privileges"
    echo "3. You have the necessary permissions"
    echo ""
    echo "For local postgres user without password, try:"
    echo "  sudo -u postgres psql -c 'SELECT 1;'"
    echo ""
    echo "For remote connections, set PGPASSWORD environment variable:"
    echo "  export PGPASSWORD=your_password"
    exit 1
fi

# Check if application user already exists
if run_psql_superuser "SELECT 1 FROM pg_roles WHERE rolname = '$APP_DB_USER';" | grep -q 1; then
    echo "User $APP_DB_USER already exists. Updating password..."
    run_psql_superuser "ALTER USER $APP_DB_USER WITH PASSWORD '$APP_DB_PASSWORD';"
else
    echo "Creating new user: $APP_DB_USER"
    run_psql_superuser "CREATE USER $APP_DB_USER WITH PASSWORD '$APP_DB_PASSWORD';"
fi

# Set user privileges
echo "Setting user privileges..."
run_psql_superuser "
    ALTER USER $APP_DB_USER WITH CREATEDB CREATEROLE LOGIN;
    GRANT pg_read_server_files TO $APP_DB_USER;
    GRANT pg_write_server_files TO $APP_DB_USER;
"

echo "=== Database user $APP_DB_USER created/updated successfully! ==="