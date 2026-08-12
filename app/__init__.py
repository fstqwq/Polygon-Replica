"""Polygon-Replica application package."""

import sys


if sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 14):
    raise RuntimeError("Polygon-Replica requires CPython 3.14")
