# ECMWF Software with Documentation (SD) Deliverable

**Project / Activity Name:** GRUAN Radiosonde Data Processing System
**Version:** 1.0.0
**Date:** 2025-12-03
**Author(s):** Emanuele Tramutola - CNR, C.I.A.O. Research Group, IMAA Institute, Tito Scalo (PZ), Italy

---

## 1. Purpose

The purpose of this software is to provide a high-performance, parallel data processing system for importing and managing GRUAN (GCOS Reference Upper-Air Network) radiosonde data from multiple instruments (Vaisala RS41, RS92 and Meisei RS11G, IMS-100) into a PostgreSQL database. It ensures that scientific data is efficiently extracted, transformed, and stored for analysis.

---

## 2. Audience

**External (public) audience.** The software and its documentation are intended for meteorologists, climate researchers, and data managers who need to process and analyze GRUAN radiosonde data.

---

## 3. Software Repository

**Repository URL:** https://github.com/emanueletramutola/gruan_retriever

**Clone URL:**
```bash
# HTTPS
git clone https://github.com/emanueletramutola/gruan_retriever.git

# SSH
git clone git@github.com:emanueletramutola/gruan_retriever.git
```

**Repository Structure:**

- `config/` - Configuration files
- `converters/` - Data transformation logic
- `database/` - Database operations and migration scripts
- `processors/` - Core data processing logic
- `readers/` - NetCDF file readers
- `tests/` - Unit and integration tests
- `utils/` - Utility functions (logging, station management)
- `main.py` - Main entry point
- `requirements.txt` - Python dependencies
- `README.md` - Quick start guide
- `SoftwareUserGuide.md` - Detailed user manual

**License:** CC-BY-4.0

---

## 4. Technical Specifications

**Programming Language:** Python 3.12+ (minimum required version)

**Package Manager / Environment:** pip / venv

**Supported Operating Systems:**

- Linux (tested on Ubuntu/Debian)
- macOS
- Windows

**System Requirements:**

- **CPU:** Multi-core processor recommended for parallel processing
- **RAM:** 8GB minimum recommended
- **Storage:** Dependent on data volume (PostgreSQL database size)
- **Database:** PostgreSQL 12+

**Software Dependencies:**

| Dependency | Purpose |
|------------|---------|
| `beautifulsoup4` | Web scraping for station data |
| `requests` | HTTP requests for station data |
| `numpy` | Numerical data processing |
| `pandas` | Data manipulation and transformation |
| `netCDF4` | Reading NetCDF data files |
| `sqlalchemy` | Database ORM and connection management |
| `psycopg2` | PostgreSQL adapter |
| `pyyaml` | Configuration file parsing |
| `psutil` | System resource monitoring |

**External Data Requirements:**

- GRUAN data files (NetCDF format, typically packaged in JAR/ZIP archives)
- Access to GRUAN website for station metadata (optional, for updates)

---

## 5. Installation Instructions

### Prerequisites

- **No system administrator privileges required** (for Python setup)
- Git installed (optional, for cloning repository)
- **Python 3.12 or higher** (minimum required version)
- PostgreSQL 12+ database server (requires admin privileges for initial setup)

### Installation Steps

1. **Clone the repository:**
   ```bash
   git clone https://github.com/emanueletramutola/gruan_retriever.git
   cd gruan_retriever
   ```

2. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Linux/Mac
   # OR
   .venv\Scripts\activate    # Windows
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure Environment Variables:**
   Set the database password environment variable:
   ```bash
   export GRUAN_USER_PSW="your_database_password"
   ```

5. **Verify installation:**
   ```bash
   python -c "import processors; print('Installation successful')"
   ```

### Troubleshooting

**Issue:** `ModuleNotFoundError`
**Solution:** Ensure the virtual environment is activated (`source .venv/bin/activate`).

**Issue:** Database connection failed
**Solution:** Verify `config.yaml` settings and ensure `GRUAN_USER_PSW` is set. Check if PostgreSQL is running.

---

## 6. Quick Start Guide

For users who want to get started immediately:

```bash
# 1. Setup environment
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GRUAN_USER_PSW="your_password"

# 2. Configure paths
# Edit config.yaml to point to your data directories

# 3. Run the processor
python main.py
```

**Expected output:** Logs indicating the start of the import process, progress updates for each file, and a final execution summary.

---

## 7. Usage Instructions

### Input/Output Formats

**Supported Input Formats:**
- GRUAN Data Products (NetCDF files inside JAR/ZIP archives)
- Supported Sondes:
  - **RS92**: Vaisala RS92 radiosonde
  - **RS41**: Vaisala RS41 radiosonde
  - **RS11G**: MEISEI RS-11G radiosonde
  - **IMS100**: MEISEI IMS-100 radiosonde

**Output Formats:**
- PostgreSQL Database Tables (configurable table names, default: `header` and `data` tables for each sonde type)
- Note: Table names can be customized in `config.yaml` under `database.table_names`

### Configuration

Configuration is managed via `config.yaml`. Key sections:

| Parameter | Description |
|-----------|-------------|
| `paths` | Directory paths for each sonde type (e.g., `rs92`, `rs41`, `ims100`, `rs11g`) |
| `database` | Connection details (host, port, user, dbname) |
| `logging` | Log levels and file paths |

**Example `config.yaml` snippet:**
```yaml
paths:
  rs92: "/path/to/rs92/data"
  rs41: "/path/to/rs41/data"
  ims100: "/path/to/ims100/data"
  rs11g: "/path/to/rs11g/data"
  
database:
  configuration:
    dbname: "gruan"
    user: "gruan_user"
    host: "localhost"
```

### Command-Line Interface

**Basic usage:**
```bash
python main.py
```

The script automatically processes all configured sonde types in parallel.

### Use Cases

#### Use Case 1: Full Data Import

**Description:** Import all available data for all configured sonde types.

**Command:**
```bash
python main.py
```

**Expected output:** The system iterates through RS92, RS41, etc., processes files in parallel, and updates the database.

---

#### Use Case 2: Update Station Database

**Description:** Fetch latest station metadata from GRUAN website.

**Command:**
```python
# Create a script or use python shell
from utils.station_manager import update_station_database
update_station_database()
```

---

## 8. Automatic Tests

### Purpose

Verify that the data processing logic, file reading, and database operations work as expected.

### Test Suite Structure

The test suite includes 3 main test modules with 19 total tests:

1. **`tests/test_netcdf_reader.py`** (8 tests)
   - NetCDF file reading and processing
   - Data validation and transformation
   - Error handling for invalid inputs

2. **`tests/test_processor.py`** (6 tests)
   - JAR file processing logic
   - Single file processing workflow
   - Database save operations

3. **`tests/test_station_manager.py`** (5 tests)
   - Station database management
   - Web scraping and data updates
   - Singleton pattern implementation

### Running Tests

```bash
# Run all tests (from project root)
./.venv/bin/python -m pytest

# If virtual environment is activated
pytest

# Run with verbose output
pytest -v

# Run specific test module
pytest tests/test_netcdf_reader.py

# Run with coverage report
pytest --cov=. --cov-report=html
```

### Test Output Snapshot

```
============ test session starts =============
platform linux -- Python 3.12.3, pytest-8.4.2, pluggy-1.6.0
cachedir: .pytest_cache
rootdir: /Data/owncloud/src/gruan_retriever
configfile: pytest.ini

tests/test_netcdf_reader.py::TestNetCDFReader::test_process_attribute_value PASSED [  5%]
tests/test_netcdf_reader.py::TestNetCDFReader::test_process_variable_data_masked PASSED [ 10%]
tests/test_netcdf_reader.py::TestNetCDFReader::test_process_variable_data_small_values PASSED [ 15%]
tests/test_netcdf_reader.py::TestNetCDFReader::test_process_variable_data_strings PASSED [ 21%]
tests/test_netcdf_reader.py::TestNetCDFReader::test_read_netcdf_file_not_found PASSED [ 26%]
tests/test_netcdf_reader.py::TestNetCDFReader::test_read_netcdf_invalid_filename PASSED [ 31%]
tests/test_netcdf_reader.py::TestNetCDFReader::test_read_netcdf_none_zip_ref PASSED [ 36%]
tests/test_netcdf_reader.py::TestNetCDFReader::test_read_netcdf_success PASSED [ 42%]
tests/test_processor.py::TestGRUANProcessor::test_process_single_jar_file_no_files PASSED [ 47%]
tests/test_processor.py::TestGRUANProcessor::test_process_single_jar_file_root_files PASSED [ 52%]
tests/test_processor.py::TestGRUANProcessor::test_process_single_jar_file_with_revisions PASSED [ 57%]
tests/test_processor.py::TestGRUANProcessor::test_process_single_nc_file_skip PASSED [ 63%]
tests/test_processor.py::TestGRUANProcessor::test_process_single_nc_file_success PASSED [ 68%]
tests/test_processor.py::TestGRUANProcessor::test_save_data_copy_method PASSED [ 73%]
tests/test_station_manager.py::TestStationManager::test_load_stations PASSED [ 78%]
tests/test_station_manager.py::TestStationManager::test_scrape_gruan_sites_empty_table PASSED [ 84%]
tests/test_station_manager.py::TestStationManager::test_scrape_gruan_sites_success PASSED [ 89%]
tests/test_station_manager.py::TestStationManager::test_singleton PASSED [ 94%]
tests/test_station_manager.py::TestStationManager::test_update_station_database PASSED [100%]

============= 19 passed in 2.76s =============
```

---

## 9. Known Limitations

### Current Limitations

1. **Database Dependency**
   - Description: The system is tightly coupled with PostgreSQL.
   - Workaround: None currently.

2. **Memory Usage**
   - Description: Parallel processing of large NetCDF files can consume significant RAM.
   - Workaround: Adjust the number of parallel workers if memory is constrained.

### Known Issues

See the issue tracker for a complete list of known bugs.

---

## 10. Documentation Summary

### Available Documentation

- **README.md** - Quick start and overview.
- **SoftwareUserGuide.md** - This document.

### Code Documentation

- Docstrings are provided for all major classes and functions (e.g., `GRUANProcessor`, `StationManager`).

---

## 11. Support and Contact

### Reporting Issues

To report bugs, please contact the development team or open an issue in the repository.

### Contributing

Contributions are welcome. Please ensure tests pass before submitting changes.

**Maintainer(s):**

- Emanuele Tramutola
  - Organization: CNR (National Research Council of Italy)
  - Research Group: C.I.A.O. (CNR-IMAA Atmospheric Observatory)
  - Institute: IMAA (Institute of Methodologies for Environmental Analysis)
  - Location: Tito Scalo (PZ), Italy

---

## 12. Deliverable Compliance

- [x] Software placed in public repository
- [x] Complete source code included
- [x] Documentation included in repository
- [x] Installation instructions provided
- [x] Installation does not require administrator privileges
- [x] Automatic tests included and verified
- [x] Snapshot of test output provided
- [x] License specified
- [x] Software includes version number/release tag
- [x] Code follows ECMWF coding standards (if applicable)
- [x] README.md with quick start instructions
- [x] Example datasets or data access instructions provided
- [x] Citation information provided (if applicable)
- [x] Technical specifications documented

---

**Document Version:** 1.0.0
**Date:** 2025-12-03
**Last Updated:** 2025-12-03
