# Contributing to GRUAN Radiosonde Data Processing System

Thank you for your interest in contributing to the GRUAN Data Processing System! This document provides guidelines and instructions for contributing to this project.

## Table of Contents

- [Getting Started](#getting-started)
- [Development Environment](#development-environment)
- [Project Structure](#project-structure)
- [Coding Standards](#coding-standards)
- [Testing](#testing)
- [Commit Guidelines](#commit-guidelines)
- [Pull Request Process](#pull-request-process)
- [Bug Reports](#bug-reports)
- [Feature Requests](#feature-requests)
- [Documentation](#documentation)
- [Release Process](#release-process)

## Getting Started

### Prerequisites
- Python 3.8+
- PostgreSQL 12+
- Git

### First-time Setup
1. Fork the repository on GitHub
2. Clone your fork locally:
   ```bash
   git clone https://github.com/your-username/gruan-data-processor.git
   cd gruan-data-processor
   ```
3. Set up the development environment (see below)

## Development Environment

### Python Environment
```bash
# Create virtual environment
python -m venv venv

# Activate virtual environment
# On Linux/Mac:
source venv/bin/activate
# On Windows:
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Database Setup
```bash
# Set up development database
export GRUAN_USER_PSW="your_development_password"
./database/scripts/setup_with_config.sh development
```

### Environment Variables
Create a `.env` file for development:
```bash
GRUAN_USER_PSW="your_development_password"
```

## Project Structure

```
gruan-data-processor/
├── config/                 # Configuration management
├── converters/            # Data transformation logic
├── database/              # Database operations and migrations
│   ├── config/           # Environment configurations
│   ├── functions/        # PostgreSQL functions
│   ├── migrations/       # Database schema
│   └── scripts/          # Deployment scripts
├── processors/           # Data processing pipelines
├── readers/              # File format readers
├── tests/                # Comprehensive test suite
│   ├── integration/      # End-to-end tests
│   ├── performance/      # Performance tests
│   └── security/         # Security tests
├── utils/                # Utility functions
└── docs/                 # Documentation
```

## Coding Standards

### Python Code Style
We follow PEP 8 with the following specific guidelines:

- **Line length**: 88 characters (Black formatter)
- **Imports**: Grouped and sorted (isort)
- **Naming**:
  - Classes: `PascalCase`
  - Functions/Variables: `snake_case`
  - Constants: `UPPER_SNAKE_CASE`

### Code Quality Tools
```bash
# Format code
black .

# Sort imports
isort .

# Check code style
flake8

# Type checking
mypy .

# Security scanning
bandit -r .
```

### Documentation
- Use Google-style docstrings for all public functions and classes
- Include type hints for function parameters and return values
- Update README.md for user-facing changes
- Update docstrings for API changes

Example docstring:
```python
def process_data(data: Dict[str, Any], metadata: Dict[str, Any]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Process raw data and metadata into structured DataFrames.

    Args:
        data: Dictionary containing measurement data arrays
        metadata: Dictionary containing header metadata

    Returns:
        tuple: (header_df, data_df) - DataFrames for database insertion

    Raises:
        ValueError: If data validation fails
        ProcessingError: If data transformation fails
    """
```

## Testing

### Test Structure
- Unit tests: `tests/test_*.py`
- Integration tests: `tests/integration/`
- Performance tests: `tests/performance/`
- Security tests: `tests/security/`

### Running Tests
```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test categories
pytest tests/unit/ -v
pytest tests/integration/ -v
pytest -m "not slow"  # Exclude slow tests

# Run performance tests
pytest tests/performance/ -v --durations=0
```

### Writing Tests
- Follow AAA pattern (Arrange, Act, Assert)
- Use descriptive test names
- Mock external dependencies
- Include both positive and negative test cases
- Test edge cases and error conditions

Example test:
```python
def test_dataframe_converter_with_valid_data():
    """Test DataFrame conversion with valid input data."""
    # Arrange
    converter = DataFrameConverter('RS41', cleaner, header_cols, data_cols)
    sample_data = {'time': [1, 2, 3], 'alt': [100, 200, 300]}
    sample_metadata = {'g.Site.Key': 'TEST'}
    
    # Act
    header_df, data_df, skip_reason = converter.convert_to_dataframe(
        sample_data, sample_metadata
    )
    
    # Assert
    assert not header_df.empty
    assert not data_df.empty
    assert skip_reason is None
```

## Commit Guidelines

### Commit Message Format
We use conventional commits format:
```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Test additions/modifications
- `chore`: Maintenance tasks

Examples:
```
feat(processor): add support for RS41/IMS100 sonde type

- Implement RS41/IMS100 data processing pipeline
- Add sonde-specific validation rules
- Update configuration for multi-sonde support

Closes #123
```

```
fix(database): resolve connection pool memory leak

- Properly close database connections in connection pool
- Add connection health checks
- Update connection timeout settings

Fixes #456
```

### Pre-commit Hooks
We recommend setting up pre-commit hooks:
```bash
# Install pre-commit
pip install pre-commit

# Install hooks
pre-commit install
```

## Pull Request Process

1. **Create a Feature Branch**
   ```bash
   git checkout -b feat/your-feature-name
   ```

2. **Make Your Changes**
   - Follow coding standards
   - Write or update tests
   - Update documentation
   - Ensure all tests pass

3. **Submit Pull Request**
   - Fill out the PR template
   - Reference related issues
   - Request reviews from maintainers

4. **Code Review**
   - Address review comments
   - Keep PR focused and manageable
   - Squash commits if requested

### PR Template
```markdown
## Description
Brief description of the changes and the problem being solved.

## Related Issues
Fixes # (issue number)

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] All tests pass

## Documentation
- [ ] Documentation updated
- [ ] README updated
- [ ] Code comments added/updated

## Checklist
- [ ] Code follows project standards
- [ ] Self-review completed
- [ ] All checks pass
```

## Bug Reports

When reporting bugs, please include:

### Bug Report Template
```markdown
## Description
Clear and concise description of the bug.

## Steps to Reproduce
1. Configuration used
2. Command executed
3. Error observed

## Expected Behavior
What you expected to happen.

## Actual Behavior
What actually happened.

## Environment
- OS: [e.g., Ubuntu 20.04]
- Python Version: [e.g., 3.9.7]
- PostgreSQL Version: [e.g., 13.4]

## Additional Context
Logs, screenshots, or any other relevant information.
```

## Feature Requests

### Feature Request Template
```markdown
## Problem Statement
Clear description of the problem this feature would solve.

## Proposed Solution
Description of the proposed feature.

## Alternatives Considered
Other solutions you've considered.

## Additional Context
Any other context about the feature request.
```

## Documentation

We maintain several types of documentation:

### User Documentation
- `README.md`: Getting started and basic usage
- `docs/installation.md`: Detailed installation instructions
- `docs/configuration.md`: Configuration guide

### Developer Documentation
- Code docstrings
- Architecture documentation
- API documentation

### Operational Documentation
- Deployment guides
- Monitoring and troubleshooting
- Backup and recovery procedures

## Release Process

### Versioning
We use Semantic Versioning (SemVer): `MAJOR.MINOR.PATCH`

- `MAJOR`: Breaking changes
- `MAJOR`: Breaking changes
- `MINOR`: New features, backwards compatible
- `PATCH`: Bug fixes, backwards compatible

### Release Steps
1. Update version in `pyproject.toml`
2. Update CHANGELOG.md
3. Create release branch
4. Run full test suite
5. Create GitHub release
6. Update documentation

## Getting Help

- Create an issue for bugs or feature requests
- Use GitHub Discussions for questions
- Contact maintainers for security issues

## Recognition

Contributors will be acknowledged in:
- Release notes
- Contributor list in README
- Project documentation

---

Thank you for contributing to the GRUAN Data Processing System! Your efforts help advance scientific research and climate monitoring.
