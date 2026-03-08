# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Emerge

Emerge (emerge-viz) is an interactive code analysis tool that scans source code to calculate metrics, build dependency/filesystem graphs, and generate interactive D3-based web visualizations. It supports 13 languages: C, C++, Groovy, Java, JavaScript, TypeScript, Kotlin, ObjC, Ruby, Swift, Python, Go, Rust.

## Common Commands

```bash
# Install dependencies
pip install -e .

# Run all unit tests with verbose output
python -m unittest discover -v -s ./emerge -p "test_*.py"

# Run tests via the test runner (also checks docstring coverage)
python run_tests.py

# Run a single test file
python -m unittest emerge.tests.parsers.test_java_parser -v

# Run emerge from source
python emerge.py -c configs/emerge.yaml

# Generate a config template for a language
python emerge.py -a java
```

## Architecture

### Entry Points
- `emerge.py` - Standalone CLI entry point
- `emerge/main.py` - pip-installed entry point (`emerge` console command), calls `Emerge.start()`

### Core Pipeline: Config -> Parse -> Analyze -> Export
1. **`emerge/appear.py`** - `Emerge` class: top-level orchestrator. Registers all language parsers, reads config, launches the analyzer. Contains `__version__`.
2. **`emerge/config.py`** - `Configuration` class: parses YAML config files, validates settings, creates `Analysis` objects with their metrics.
3. **`emerge/analyzer.py`** - `Analyzer` class: iterates analyses, coordinates scanning (file/entity), graph construction, and metric calculation.
4. **`emerge/analysis.py`** - `Analysis` class: holds per-analysis state including metrics, graph representations, results, statistics, and export settings. Performs the actual file scanning, entity extraction, metric calculation, and export orchestration.
5. **`emerge/export.py`** - Exporters: GraphML, table, JSON, D3 HTML web app.

### Language Parsers (`emerge/languages/`)
- All parsers extend `AbstractParser` from `abstractparser.py`
- `AbstractParser` defines the contract: `generate_file_results()`, `generate_entity_results()`, parsing keywords, file extensions
- Each parser (e.g., `javaparser.py`, `pyparser.py`) implements language-specific import/dependency extraction using regex and line-by-line parsing
- Entity scan (extracting classes/structs) is only supported for: Java, Kotlin, Swift, Groovy

### Metrics (`emerge/metrics/`)
- All metrics extend `AbstractMetric` (with subtypes `AbstractCodeMetric` and `AbstractGraphMetric`) from `abstractmetric.py`
- Available metrics in subdirectories: `sloc`, `numberofmethods`, `faninout`, `modularity` (Louvain), `tfidf`, `whitespace`, `git`
- Code metrics operate on individual results; graph metrics operate on `GraphRepresentation` objects

### Supporting Modules
- `emerge/graph.py` - `GraphRepresentation` using networkx, filesystem graph construction
- `emerge/abstractresult.py` - Base result classes (`AbstractFileResult`, `AbstractEntityResult`)
- `emerge/files.py` - File scanning, language extension mapping, path utilities
- `emerge/stats.py` - `Statistics` collection
- `emerge/log.py` - Custom `Logger` wrapper with emoji-formatted log levels

### Tests (`emerge/tests/`)
- `parsers/` - Test files for language parsers (usually one per parser), test data in `testdata/` as Python string constants
- `metrics/` - Tests for number_of_methods and tfidf metrics
- `config/` - Configuration parsing tests
- Test data files in `testdata/` contain source code snippets as Python multiline strings (not actual source files)

## Configuration
YAML-based config at project level. Key structure: `project_name` + `analyses[]`, each with `source_directory`, `only_permit_languages`, `file_scan`/`entity_scan` metric lists, and `export` settings. See README.md for full config reference.
