import os
from pathlib import Path

import yaml


def load_config_with_env_vars(config_path='config.yaml'):
    """
    Load YAML configuration file and resolve environment variables.
    Environment variables should be in the format: ${VAR_NAME}
    """
    with open(config_path, 'r') as config_file:
        config_content = config_file.read()

    # Replace environment variables in the YAML content
    config_content = os.path.expandvars(config_content)

    return yaml.safe_load(config_content)


# Load configuration from YAML file with environment variable support
config = load_config_with_env_vars('config.yaml')

# Path configurations for different radiosonde types
PATH_CONFIG = {
    'RS92': Path(config['paths']['rs92']),
    'RS41': Path(config['paths']['rs41']),
    'RS11G': Path(config['paths']['rs11g']),
    'IMS100': Path(config['paths']['ims100']),
}

# Database connection configuration
DB_CONFIG = config['database']['configuration']

# Database table names for different radiosonde types (read from YAML but
# kept in settings.py)
TABLE_NAMES = {
    'RS41': tuple(config['database']['table_names']['rs41']),
    'RS92': tuple(config['database']['table_names']['rs92']),
    'RS11G': tuple(config['database']['table_names']['rs11g']),
    'IMS100': tuple(config['database']['table_names']['ims100']),
}

# Logging configuration dictionary
LOGGING_CONFIG = config['logging']
