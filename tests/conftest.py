"""Shared pytest fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _no_arxiv_throttle(monkeypatch):
    """Disable the arXiv min-interval throttle in tests so the suite never sleeps
    (real-network behaviour is exercised separately, not under pytest)."""
    import app.discover as discover
    monkeypatch.setattr(discover, "_MIN_INTERVAL", 0.0)
