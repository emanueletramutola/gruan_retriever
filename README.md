# GRUAN Radiosonde Data Processing System

![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-12+-336791.svg)
[![License: CC-BY-4.0 licence](https://img.shields.io/badge/Licenses-CC_BY_4.0-green)](https://creativecommons.org/licenses/by/4.0/deed.en)

A high-performance, parallel data processing system for importing and managing GRUAN (GCOS Reference Upper-Air Network) radiosonde data from multiple instruments (Vaisala RS92, RS41 and MEISEI RS11G, IMS-100) into a PostgreSQL database.

## Overview

The GRUAN Data Processing System handles large volumes of scientific radiosonde data from the GRUAN network, providing:

- **Parallel processing** of compressed data files
- **Robust error handling** and comprehensive logging
- **High-performance database operations** using PostgreSQL COPY
- **Automated station management** with web scraping capabilities
- **Multi-environment deployment** support

The system processes NetCDF files contained within JAR archives, extracts atmospheric measurement data, and stores it in a partitioned PostgreSQL database optimized for scientific queries.

## Features

### Multi-Sonde Support
Process data from four radiosonde types:
- **Vaisala**: RS92, RS41
- **MEISEI**: RS11G, IMS-100

### Core Capabilities
- **Parallel Processing**: Multi-core data import with configurable workers
- **Bulk Database Operations**: High-speed PostgreSQL COPY commands
- **Automatic Station Management**: Web scraping and database synchronization
- **Comprehensive Error Handling**: Failed imports tracking and reporting
- **Data Validation**: Station existence and timestamp validation
- **Database Partitioning**: Automated time-based table partitioning
- **Idempotent Operations**: Safe re-runs and duplicate prevention

## Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/emanueletramutola/gruan_retriever.git
cd gruan_retriever

# 2. Set up Python environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# .venv\\Scripts\\activate  # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set environment variable
export GRUAN_USER_PSW="your_database_password"

# 5. Initialize database
./database/scripts/setup_with_config.sh development

# 6. Configure data paths in config.yaml
# Edit paths to point to your GRUAN data directories

# 7. Run the processor
python main.py
```

## System Requirements

- **Python**: 3.12+ (minimum required version)
- **PostgreSQL**: 12+
- **Memory**: 8GB minimum, 16GB+ recommended for production
- **Storage**: Dependent on data volume
- **OS**: Linux (recommended), macOS, or Windows

## Documentation

📚 **Complete documentation is available in [SoftwareUserGuide.md](SoftwareUserGuide.md)**

The Software User Guide includes:
- Detailed installation instructions
- Complete configuration guide
- Usage examples and workflows
- Test suite documentation
- Troubleshooting guide
- Known limitations and support information

### Additional Documentation
- **[Contributing Guidelines](CONTRIBUTING.md)** - How to contribute to this project
- **[Code of Conduct](CODE_OF_CONDUCT.md)** - Community standards
- **[Changelog](CHANGELOG.md)** - Version history and release notes

## Project Structure

```
gruan_retriever/
├── config/           # Configuration management
├── converters/       # Data transformation
├── database/         # Database operations and migrations
├── processors/       # Data processing logic
├── readers/          # File format readers
├── tests/            # Test suite
├── utils/            # Utility functions
├── main.py           # Application entry point
└── config.yaml       # Main configuration file
```

## Testing

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run with coverage report
pytest --cov=.
```

## License

This project is licensed under the CC-BY-4.0 License.

## Acknowledgments

- GRUAN Network for providing reference-quality radiosonde data
- Vaisala for RS41 and RS92 instrument documentation and specifications
- MEISEI for RS11G and IMS-100 instrument documentation and specifications
- PostgreSQL community for excellent database support
- Python scientific computing community for essential libraries

## Support

For support and questions:
- Review the [Software User Guide](SoftwareUserGuide.md)
- Check the [troubleshooting section](SoftwareUserGuide.md#troubleshooting)
- Create an issue on the repository
- Contact the development team

## Maintainer

**Emanuele Tramutola**
- Organization: CNR (National Research Council of Italy)
- Research Group: C.I.A.O. (CNR-IMAA Atmospheric Observatory)
- Institute: IMAA (Institute of Methodologies for Environmental Analysis)
- Location: Tito Scalo (PZ), Italy

---

**Note**: This system is designed for processing scientific data and should be operated by personnel with appropriate meteorological data handling experience.