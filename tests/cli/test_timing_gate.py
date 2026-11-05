from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("worker", [None, "gw0"])
@pytest.mark.parametrize("marker", ["timing", None])
def test_a_timing_test_skips_in_an_xdist_worker_only(monkeypatch, worker, marker):
    """Beside another xdist worker on the core's second thread, a `timing` test's ratio fails
    shipped code now and then (#485, #492): a worker skips it, with the command that runs it
    alone, and a serial run, CI's `timing` jobs among them, runs it. Any other test runs in a
    worker too: a skip of everything there would leave a green job that ran nothing."""
    monkeypatch.delenv("ZAB_LIVE", raising=False)
    monkeypatch.delenv("ZAB_LIVE_ZULIP", raising=False)
    if worker:
        monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
    else:
        monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    spec = spec_from_file_location("timing_gate", Path(__file__).parents[1] / "conftest.py")
    hook = module_from_spec(spec)
    spec.loader.exec_module(hook)
    item = Mock()
    item.get_closest_marker = lambda name: (marker is not None and name == marker) or None
    hook.pytest_collection_modifyitems([item])
    skipped = worker is not None and marker == "timing"
    assert item.add_marker.called is skipped
    if skipped:
        (mark,), _ = item.add_marker.call_args
        assert mark.name == "skip"
        assert "uv run pytest -m timing -p no:xdist" in mark.kwargs["reason"]
