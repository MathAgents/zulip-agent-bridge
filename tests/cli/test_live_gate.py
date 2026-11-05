from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("variable", [None, "ZAB_LIVE", "ZAB_LIVE_ZULIP"])
def test_global_gate_accepts_either_live_opt_in(monkeypatch, variable):
    monkeypatch.delenv("ZAB_LIVE", raising=False)
    monkeypatch.delenv("ZAB_LIVE_ZULIP", raising=False)
    if variable:
        monkeypatch.setenv(variable, "1")
    spec = spec_from_file_location("live_gate", Path(__file__).parents[1] / "conftest.py")
    hook = module_from_spec(spec)
    spec.loader.exec_module(hook)
    item = Mock()
    item.get_closest_marker = lambda name: name == "live" or None
    hook.pytest_collection_modifyitems([item])
    assert item.add_marker.called is (variable is None)


@pytest.mark.parametrize("value", [None, "1", "claude", "e2e"])
def test_the_e2e_suite_needs_zab_live_e2e(monkeypatch, value):
    """The end-to-end suite (issue #263) drives the test VM and AWS: only `ZAB_LIVE=e2e`."""
    monkeypatch.delenv("ZAB_LIVE_ZULIP", raising=False)
    if value:
        monkeypatch.setenv("ZAB_LIVE", value)
    else:
        monkeypatch.delenv("ZAB_LIVE", raising=False)
    spec = spec_from_file_location("live_gate", Path(__file__).parents[1] / "conftest.py")
    hook = module_from_spec(spec)
    spec.loader.exec_module(hook)
    item = Mock()
    item.get_closest_marker = lambda name: name == "e2e" or None
    hook.pytest_collection_modifyitems([item])
    assert item.add_marker.called is (value != "e2e")
