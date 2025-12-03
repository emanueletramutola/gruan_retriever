#!/bin/bash
set -e

###############################################################################
# Script: init_database.sh
# Description: Initializes the GRUAN database by creating the database (if not exists),
#              running all migrations, loading configuration data, and optional sample data.
#              Uses peer authentication for local postgres superuser.
#
# Usage:
#   ./init_database.sh [db_name] [app_user] [host] [port] [superuser] [app_password]
#
# Parameters:
#   db_name      Database name (default: gruan)
#   app_user     Application database user (default: gruan_user)
#   host         Database host (default: localhost)
#   port         Database port (default: 5432)
#   superuser    PostgreSQL superuser for DB creation (default: postgres)
#   app_password Application user password (default: xxx)
#
# Examples:
#   # Use all default values (local postgres with peer auth)
#   ./init_database.sh
#
#   # Custom database name and user
#   ./init_database.sh my_database my_app_user
#
#   # Full custom configuration with password
#   ./init_database.sh gruan_test test_user localhost 5432 postgres test_password
#
# Notes:
#   - For local postgres superuser, uses peer authentication via sudo
#   - Application user always uses password authentication
#   - Requires create_database_user.sh to be run first
###############################################################################

# Database connection parameters
DB_NAME="${1:-gruan}"
DB_USER="${2:-gruan_user}"
DB_HOST="${3:-localhost}"
DB_PORT="${4:-5432}"

# Superuser parameters (for database creation only)
SUPER_USER="${5:-postgres}"

# Application user password
APP_PASSWORD="${6:-xxx}"

echo "=== Initializing database $DB_NAME ==="

# Check if PostgreSQL client tools are available
if ! command -v psql &> /dev/null; then
    echo "Error: psql command not found. Please install PostgreSQL client tools."
    exit 1
fi

# Test connection to PostgreSQL server as superuser
echo "1. Testing PostgreSQL connection..."
if [ "$DB_HOST" = "localhost" ] && [ "$SUPER_USER" = "postgres" ]; then
    # Use peer authentication for local postgres user
    if ! sudo -u postgres psql -d postgres -c "SELECT 1;" &> /dev/null; then
        echo "Error: Cannot connect to PostgreSQL server as local superuser"
        echo "Please verify that:"
        echo "1. PostgreSQL server is running"
        echo "2. You have sudo privileges for postgres user"
        exit 1
    fi
    echo "   ✓ Connected via peer authentication"
else
    # Use password authentication for remote or non-postgres users
    if ! psql -h "$DB_HOST" -U "$SUPER_USER" -p "$DB_PORT" -d postgres -c "SELECT 1;" &> /dev/null; then
        echo "Error: Cannot connect to PostgreSQL server at $DB_HOST:$DB_PORT"
        echo "Please verify that:"
        echo "1. PostgreSQL server is running"
        echo "2. User '$SUPER_USER' exists and has privileges"
        echo "3. Set PGPASSWORD environment variable if using password authentication"
        exit 1
    fi
    echo "   ✓ Connected via password authentication"
fi

# Create database as superuser (continue if it already exists)
echo "2. Creating database $DB_NAME..."
if [ "$DB_HOST" = "localhost" ] && [ "$SUPER_USER" = "postgres" ]; then
    # Use peer authentication for local postgres user
    if sudo -u postgres createdb "$DB_NAME" 2>/dev/null; then
        echo "   ✓ Database created successfully"
    else
        echo "   ⚠ Database already exists, continuing..."
    fi
else
    # Use password authentication for remote or non-postgres users
    if createdb -h "$DB_HOST" -U "$SUPER_USER" -p "$DB_PORT" "$DB_NAME" 2>/dev/null; then
        echo "   ✓ Database created successfully"
    else
        echo "   ⚠ Database already exists, continuing..."
    fi
fi

# Grant privileges to application user
echo "3. Granting privileges to $DB_USER..."
if [ "$DB_HOST" = "localhost" ] && [ "$SUPER_USER" = "postgres" ]; then
    sudo -u postgres psql -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

    sudo -u postgres psql -d "$DB_NAME" -c "
      GRANT ALL ON SCHEMA public TO $DB_USER;
      GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $DB_USER;
      GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;
      GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO $DB_USER;
      ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;
      ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $DB_USER;
      ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO $DB_USER;
    "
else
    psql -h "$DB_HOST" -U "$SUPER_USER" -p "$DB_PORT" -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"

    psql -h "$DB_HOST" -U "$SUPER_USER" -p "$DB_PORT" -d "$DB_NAME" -c "
        GRANT ALL ON SCHEMA public TO $DB_USER;
        GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $DB_USER;
        GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;
        GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO $DB_USER;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $DB_USER;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO $DB_USER;
    "
fi
echo "   ✓ Privileges granted"

# Set PGPASSWORD for application user connections
export PGPASSWORD=$APP_PASSWORD

# Test connection as application user
echo "4. Testing application user connection..."
if ! psql -h "$DB_HOST" -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" -c "SELECT 1;" &> /dev/null; then
    echo "Error: Cannot connect to database $DB_NAME as user $DB_USER"
    echo "Please verify that:"
    echo "1. The user has been created and has proper privileges"
    echo "2. The password is correct (trying: $APP_PASSWORD)"
    echo "3. Check the user was created with: sudo -u postgres psql -c '\du'"
    exit 1
fi
echo "   ✓ Application user connected successfully"

# Execute all function scripts files as application user
echo "5. Executing function scripts..."
if [ -d "database/functions" ]; then
    for function_to_insert in $(find database/functions -name "*.sql" | sort); do
        echo "   - Applying: $(basename "$function_to_insert")"
        psql -h "$DB_HOST" -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" -f "$function_to_insert"
    done
    echo "   ✓ Functions inserted"
else
    echo "   ⚠ No functions directory found, skipping"
fi

# Execute all migration files as application user
echo "6. Executing database migrations..."
if [ -d "database/migrations" ]; then
    for migration in $(find database/migrations -name "*.sql" | sort); do
        echo "   - Applying: $(basename "$migration")"
        psql -h "$DB_HOST" -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" -f "$migration"
    done
    echo "   ✓ Migrations completed"
else
    echo "   ⚠ No migrations directory found, skipping"
fi

# Load configuration data as application user
echo "7. Loading configuration data..."
if [ -d "database/config_data" ]; then
    for config_file in $(find database/config_data -name "*.sql" | sort); do
        echo "   - Loading: $(basename "$config_file")"
        psql -h "$DB_HOST" -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" -f "$config_file"
    done
    echo "   ✓ Configuration data loaded"
else
    echo "   ⚠ No configuration data directory found, skipping"
fi

# Load sample data as application user (optional)
echo "8. Loading sample data..."
if [ -f "database/seeds/01_base_data.sql" ]; then
    psql -h "$DB_HOST" -U "$DB_USER" -p "$DB_PORT" -d "$DB_NAME" -f database/seeds/01_base_data.sql
    echo "   ✓ Sample data loaded"
else
    echo "   ⚠ No sample data found, skipping"
fi

# Unset the password for security
unset PGPASSWORD

echo ""
echo "=== Database $DB_NAME initialized successfully! ==="
echo "Connection details:"
echo "  Host: $DB_HOST, Port: $DB_PORT"
echo "  Database: $DB_NAME, User: $DB_USER"
echo ""
echo "You can connect using:"
echo "  psql -h $DB_HOST -p $DB_PORT -U $DB_USER -d $DB_NAME"