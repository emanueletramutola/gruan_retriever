import os
from pathlib import Path
import yaml
from dotenv import load_dotenv


def load_config_with_env_vars(config_path='config.yaml', dotenv_path='.env',):
    """
    Load YAML configuration file and resolve environment variables.
    Environment variables should be in the format: ${VAR_NAME}
    """
    with open(config_path, 'r') as config_file:
        config_content = config_file.read()

    # Step 1: Replace environment variables in the YAML content
    config_content = os.path.expandvars(config_content)

    # Step 2: check for unresolved placeholders (still in ${VAR_NAME} form)
    import re
    unresolved = re.findall(r'\$\{([^}]+)\}', config_content)

    if unresolved:
        # Load the .env file into a temporary dict without polluting os.environ
        env_from_file: dict[str, str] = {}
        dotenv_file = Path(dotenv_path)
        if dotenv_file.is_file():
            load_dotenv(dotenv_path=dotenv_file, override=False)
            # Re-read from os.environ — load_dotenv sets them there
            env_from_file = {var: os.environ[var]
                             for var in unresolved
                             if var in os.environ}
        else:
            import warnings
            warnings.warn(
                f".env file not found at '{dotenv_path}'. "
                f"Unresolved variables: {unresolved}",
                stacklevel=2,
            )

        # Manually substitute only the still-unresolved placeholders
        for var in unresolved:
            if var in env_from_file:
                config_content = config_content.replace(
                    f'${{{var}}}', env_from_file[var]
                )

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
