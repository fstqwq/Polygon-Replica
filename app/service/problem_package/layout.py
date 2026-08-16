"""Filesystem names owned by the native Polygon Replica package."""

from pathlib import Path


TEST_DATA_DIR = Path("test-data")
STATEMENT_BUILD_DIR = Path("statement-build")
PACKAGE_DERIVED_ROOT_NAMES = frozenset(
    {TEST_DATA_DIR.name, STATEMENT_BUILD_DIR.name}
)
