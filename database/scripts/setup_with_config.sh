#!/bin/bash
set -e

###############################################################################
# Script: setup_with_config.sh
# Description: Environment-aware database setup that reads configuration from
#              environment-specific configuration files. Automatically handles
#              authentication method based on environment and host.
#
# Usage:
#   ./setup_with_config.sh [environment]
#
# Parameters:
#   environment  Target environment (default: development)
#                Valid values: development, testing, production, staging
#
# Examples:
#   # Development environment (default)
#   ./setup_with_config.sh
#   ./setup_with_config.sh development
#
#   # Production environment
#   ./setup_with_config.sh production
#
#   # Custom environment
#   ./setup_with_config.sh staging
#
# Notes:
#   - For development (localhost), uses peer authentication for postgres user
#   - For remote hosts, uses password authentication
#   - Requires configuration files in database/config/[environment].conf
###############################################################################

ENVIRONMENT="${1:-development}"
CONFIG_FILE="database/config/${ENVIRONMENT}.conf"

echo "=== Database Setup for Environment: $ENVIRONMENT ==="

# Validate configuration file exists
if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: Configuration file $CONFIG_FILE not found"
    echo "Available environments:"
    ls database/config/*.conf 2>/dev/null | sed 's|database/config/||' | sed 's|\.conf||' || echo "  No configuration files found"
    exit 1
fi

echo "Using configuration: $CONFIG_FILE"

# Source the configuration file
set -a
source "$CONFIG_FILE"
set +a

# Validate required variables are set
required_vars=("DB_NAME" "DB_USER" "DB_PASSWORD" "DB_HOST" "DB_PORT" "SUPER_USER")
for var in "${required_vars[@]}"; do
    if [ -z "${!var}" ]; then
        echo "Error: Required variable $var is not set in $CONFIG_FILE"
        exit 1
    fi
done

echo "Configuration loaded:"
echo "  Database: $DB_NAME"
echo "  User: $DB_USER"
echo "  Host: $DB_HOST:$DB_PORT"
echo "  Superuser: $SUPER_USER"

# Execute the complete setup with configured parameters
./database/scripts/setup_database.sh \
    "$DB_NAME" \
    "$SUPER_USER" \
    "$DB_HOST" \
    "$DB_PORT" \
    "$DB_USER" \
    "$DB_PASSWORD"

echo ""
echo "=== $ENVIRONMENT database setup completed successfully! ==="