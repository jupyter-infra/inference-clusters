"""Test configuration for the load/benchmark suite."""

import pytest


def pytest_collection_modifyitems(items: list) -> None:
    """Mark every test in this directory as an e2e test (it needs a live cluster)."""
    for item in items:
        if "load" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)
