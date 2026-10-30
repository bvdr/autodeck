import pytest


@pytest.fixture
def tmp_library(tmp_path, monkeypatch):
    """Point the library cache at a temp dir so tests never touch ~/autodeck-library."""
    monkeypatch.setenv("AUTODECK_LIBRARY", str(tmp_path))
    return tmp_path
