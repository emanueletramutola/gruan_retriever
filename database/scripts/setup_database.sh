#!/bin/bash
set -e

###############################################################################
# Script: setup_database.sh
# Description: Complete database setup script that orchestrates the entire process.
#              Creates the database user and initializes the database in sequence.
#              Uses peer authentication for local postgres superuser.
#
# Usage:
#   ./setup_database.sh [db_name] [superuser] [host] [port] [app_user] [app_password]
#
# Parameters:
#   db_name      Database name (default: gruan)
#   superuser    PostgreSQL superuser (default: postgres)
#   host         Database host (default: localhost)
#   port         Database port (default: 5432)
#   app_user     Application database user (default: gruan_user)
#   app_password Application user password (default: xxx)
#
# Examples:
#   # Use all default values (local postgres with peer auth)
#   ./setup_database.sh
#
#   # Custom database name and user
#   ./setup_database.sh my_database postgres localhost 5432 my_user my_pass
#
# Notes:
#   - For local postgres superuser, uses peer authentication via sudo
#   - Application user always uses password authentication
#   - Make sure to run this script with appropriate sudo privileges
###############################################################################

# Configuration parameters
DB_NAME="${1:-gruan}"
SUPER_USER="${2:-postgres}"
DB_HOST="${3:-localhost}"
DB_PORT="${4:-5432}"
APP_DB_USER="${5:-gruan_user}"
APP_DB_PASSWORD="${6:-xxx}"

echo "=== Complete Database Setup for GRUAN ==="
echo "Database: $DB_NAME"
echo "Application User: $APP_DB_USER"
echo "Host: $DB_HOST:$DB_PORT"
echo "Superuser: $SUPER_USER"

if [ "$DB_HOST" = "localhost" ] && [ "$SUPER_USER" = "postgres" ]; then
    echo "Authentication: Using peer authentication for superuser (via sudo)"
else
    echo "Authentication: Using password authentication for superuser"
fi

# Step 1: Create database user
echo ""
echo "Step 1: Creating database user..."
./database/scripts/create_database_user.sh "$SUPER_USER" "$DB_HOST" "$DB_PORT" "$APP_DB_USER" "$APP_DB_PASSWORD"

# Step 2: Initialize database
echo ""
echo "Step 2: Initializing database..."
./database/scripts/init_database.sh "$DB_NAME" "$APP_DB_USER" "$DB_HOST" "$DB_PORT" "$SUPER_USER" "$APP_DB_PASSWORD"

echo ""
echo "=== Database setup completed successfully! ==="
echo "You can now connect using:"
echo "  psql -h $DB_HOST -p $DB_PORT -U $APP_DB_USER -d $DB_NAME"
echo ""
echo "Application user password: $APP_DB_PASSWORD"