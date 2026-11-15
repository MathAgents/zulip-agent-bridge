"""A26's reading of the bots' words (#998), offline: the growth's line the scenario waits for
is the one `autogrow.alert` writes, and the size it expects is `grow_disk.py --auto-cap`'s
step."""

from __future__ import annotations

import pytest

from e2e.admin import test_scenarios_4_grow_disk_auto as auto
from zab.admin import autogrow
from zab.admin.costs import GP3_GB_MONTH


def grown(**fields):
    r = {"outcome": "grown", "instance": "i-0abc", "from": 80, "to": 120, "fs": "grown",
         "used": 58, "again": None, "reason": ""} | fields  # fmt: skip
    return autogrow.alert(auto.B, 90.0, 1794700000.0, r, autogrow.Vm())


def test_the_growths_line_is_the_bots():
    found = auto.GREW.match(grown())
    assert found, grown()
    assert found.groups() == ("80", "120", "90", "58", f"{40 * GP3_GB_MONTH:,.2f}")
    assert auto.GREW.match(grown(**{"from": 100, "to": 150, "used": 61}))
    # Not the online growth: still above the threshold, the VM stopped meanwhile, or the file
    # system alone. Each is a failure of A26, which names the line.
    assert auto.GREW.match(grown(used=88)) is None
    assert auto.GREW.match(grown(fs="next boot")) is None
    assert auto.GREW.match(grown(**{"from": 120})) is None


@pytest.mark.parametrize(("size", "step"), [(80, 120), (100, 150), (81, 122), (300, 400)])
def test_the_expected_size_is_the_scripts_step(size, step):
    """Half again, rounded up to whole GB, at most `CAP_GB`, as `grow_disk.py --auto-cap`."""
    assert auto.grown_size(size) == step
