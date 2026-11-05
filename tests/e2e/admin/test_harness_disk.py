"""The admin e2e's checks of #552, offline: A6's full disk (`test_scenarios_2_bot`) and A11's
disks on the console (`test_scenarios_8_console`), against the product's own renderings."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2e.admin import test_scenarios_8_console as console
from e2e.admin.harness.rig import E2E
from e2e.admin.test_scenarios_2_bot import FILL, FULL_DISK, ON_VM, fill_kib, root_df
from e2e.admin.test_scenarios_8_console import disk_labels, pages_and_snapshot, row_of
from zab.admin import pages
from zab.admin.planning import status_reply
from zab.admin.scrub import Scrubber

GOLDEN = Path(__file__).resolve().parents[2] / "admin" / "golden" / "collector-snapshot.json"
NOW = 1_800_000_000.0 + 120
#: zab-test-a's `df -Pk /` on the fleet's 200 GiB root, as A6's part reads it first.
DF = (
    "Filesystem     1024-blocks     Used Available Capacity Mounted on\n"
    "/dev/root        203056560 60000000 134000000      31% /\n"
)


def test_the_fill_takes_the_root_past_90_percent():
    size, used, available, percent = root_df(DF)
    assert (size, used, available, percent) == (203_056_560, 60_000_000, 134_000_000, 31)
    fill = fill_kib(used, available)
    left, total = available - fill, used + available
    assert 0 < fill < available and left == int(total * 0.07)
    assert -(-100 * (total - left) // total) == 93  # df's Capacity, rounded up
    assert fill_kib(190_000_000, 4_000_000) == 0  # full already: nothing to add


def one_vm(**status) -> str:
    vm = {"slug": "zab-test-a", "bridge": "active", "load": {"load1": 0.1, "cpus": 8}, **status}
    return status_reply(json.dumps({"schema": 1, "vms": [vm]}), Scrubber(), limit=10000).text


def test_full_disk_is_the_bots_line_of_a_disk_under_strain():
    left = int((60_000_000 + 134_000_000) * 0.07)  # KiB, as the fill leaves them
    disk = {"size_bytes": 203_056_560 * 1024, "used_bytes": (194_000_000 - left) * 1024,
            "available_bytes": left * 1024}  # fmt: skip
    text = one_vm(disk_percent=93, disk=disk, strained=["disk"])
    found = FULL_DISK.search(text)
    assert found and (found[1], found[2]) == ("93", pages.disk_gb(left * 1024)) == ("93", "13")
    both = one_vm(disk_percent=93, disk=disk, strained=["load", "disk"])
    assert FULL_DISK.search(both)
    calm = one_vm(disk_percent=31, disk={**disk, "available_bytes": 134_000_000 * 1024},
                  strained=[])  # fmt: skip
    assert not FULL_DISK.search(calm) and "GB free" not in calm  # A6's check after the removal
    assert "disk 31%" in calm


def test_on_vm_runs_its_words_quoted_through_the_fleets_ssh(monkeypatch):
    """`ON_VM` hands `admin_common.ssh_argv` the words joined by `shlex.join`, with the
    workshop's `known_hosts`, and exits as ssh did."""
    seen = {}

    def ssh_argv(host, remote, *, known_hosts):
        seen.update(host=host, remote=remote, known_hosts=known_hosts)
        return ["ssh", host, "--", remote]

    def run(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(stdout="out", stderr="", returncode=1)

    monkeypatch.setitem(sys.modules, "admin_common", SimpleNamespace(ssh_argv=ssh_argv))
    monkeypatch.setattr(subprocess, "run", run)
    words = ["fallocate", "-l", "5KiB", "/home/ubuntu/a b; rm -rf /"]
    monkeypatch.setattr(sys, "argv", ["-c", E2E.name, "198.51.100.1", *words])
    monkeypatch.setattr(sys, "path", list(sys.path))
    with pytest.raises(SystemExit) as exited:
        exec(ON_VM, {"__name__": "__main__"})  # noqa: S102 - this repository's literal
    assert exited.value.code == 1
    assert seen["remote"] == shlex.join(words) and shlex.split(seen["remote"]) == words
    assert seen["known_hosts"] == f"/var/lib/zab-admin/ws/{E2E.name}/out/known_hosts"
    assert seen["argv"] == ["ssh", "198.51.100.1", "--", shlex.join(words)]
    assert FILL.startswith("/home/ubuntu/")  # never a directory other users can write


def snapshot() -> dict:
    found = json.loads(GOLDEN.read_text())
    return {"at": found["at"], "workshops": {E2E.name: found["workshops"]["harvard-2026"]}}


def test_a11_finds_each_vms_disk_in_its_own_row():
    snap = snapshot()
    labels = disk_labels(snap)
    assert labels == {"alice": "40 of 97 GB (42%)"}  # carol's read is of no VM
    page = pages.workshop(E2E.name, snap["workshops"][E2E.name], [], NOW, user="Drew")
    shown = '<span class="num">40 of 97 GB (42%)</span>'
    assert shown in row_of(page, "alice") and shown not in row_of(page, "bob")
    assert row_of(page, "bob") and row_of(page, "nobody") == ""
    assert row_of(page, "alice").count("<tr>") == 1  # one row, not the table
    own = pages.vm(E2E.name, snap["workshops"][E2E.name], "alice", NOW, user="Drew")
    assert shown in own


def test_a11_reads_the_pages_between_two_reads_of_the_same_snapshot(monkeypatch):
    """A snapshot written between the reads before and after the pages means another try."""
    ats = iter([1.0, 2.0, 2.0, 2.0])  # before, after (changed); before, after (the same)
    asked = []

    class Server:
        def read(self, path):
            assert path == console.SNAPSHOT
            return json.dumps({"at": next(ats), "workshops": {}})

    def ask(server, method, path, *, cookie=""):
        asked.append(path)
        return 200, {}, f"page {path}"

    monkeypatch.setattr(console, "ask", ask)
    status, _, page, own, snap = pages_and_snapshot(Server(), "c=1", ["zab-test-a"])
    assert (status, page, snap["at"]) == (200, f"page /w/{E2E.name}/", 2.0)
    assert own == {"zab-test-a": f"page /w/{E2E.name}/vm/zab-test-a"} and len(asked) == 4
