"""A21's reading of `grow_disk.py`'s words (#553), offline: what the scenario matches in a plan
and a refusal is what the script prints."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2e.admin import test_scenarios_4_grow_disk as grow

WORKSHOP = Path(__file__).resolve().parents[3] / "workshop"
for sub in ("aws", "admin"):
    if str(WORKSHOP / sub) not in sys.path:
        sys.path.insert(0, str(WORKSHOP / sub))

import grow_disk  # noqa: E402


def target(state: str) -> grow_disk.Target:
    return grow_disk.Target("zab-test-a", "198.51.100.1", "i-0abc", state, "vol-0def", "gp3",
                            200, 202)  # fmt: skip


@pytest.mark.parametrize("state", ["running", "stopped"])
def test_the_plan_check_matches_the_scripts_volume_line(state):
    line = grow_disk.volume_line(target(state))
    plan = SimpleNamespace(id="P-1", targets=["zab-test-a"], phrase="", text=f"x\n{line}\ny")
    grow.dry_run_names(plan, "zab-test-a", 200, 202, state)
    with pytest.raises(AssertionError):
        grow.dry_run_names(plan, "zab-test-a", 200, 203, state)


def test_the_six_hour_refusal_is_the_scripts():
    class Ec2:
        def find_instances(self, tags, states):
            tagged = [{"Key": k, "Value": v} for k, v in tags.items()]
            root = {"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": "vol-0def"}}
            return [{"InstanceId": "i-0abc", "State": {"Name": "running"},
                     "PublicIpAddress": "198.51.100.1", "RootDeviceName": "/dev/sda1",
                     "BlockDeviceMappings": [root], "Tags": tagged}]  # fmt: skip

        def volumes(self, ids):
            return [{"VolumeId": "vol-0def", "Size": 202, "VolumeType": "gp3",
                     "Attachments": [{"InstanceId": "i-0abc"}],
                     "Tags": [{"Key": "workshop", "Value": "zab-admin-e2e"}]}]  # fmt: skip

        def volume_modifications(self, vid):
            return [{"StartTime": now - dt.timedelta(hours=1), "ModificationState": "optimizing"}]

    now = dt.datetime(2026, 10, 22, 12, tzinfo=dt.UTC)
    out: list[str] = []
    rows = [{"slug": "zab-test-a", "ip": "198.51.100.1"}]
    code = grow_disk.run(rows, Ec2(), slug="zab-test-a", size=204, dry_run=True, log=out.append,
                         scope={"workshop": "zab-admin-e2e"}, now=lambda: now)  # fmt: skip
    assert code == 1 and grow.SIX_HOURS in out[0], out
