"""`zab.tools.vmstat` (issue #204) against a fake /proc and /sys."""

from __future__ import annotations

import os
from collections import namedtuple
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from zab.testing import fake_proc
from zab.testing.fake_proc import FakeProcess
from zab.tools import vmstat

HZ = os.sysconf("SC_CLK_TCK")
GIB = 1024**3
MIB = 1024**2
GIB_KIB = 1024**2  # one GiB in kB
Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def disks(monkeypatch):
    """`/` is 100 GiB, 41 used, 54 free (5 GiB root reserve); `/home/agent/projects` another
    file system; `/same` is on `/`'s."""
    usage = {
        "/": Usage(100 * 1024**3, 41 * 1024**3, 54 * 1024**3),
        "/home/agent/projects": Usage(200 * 1024**3, 12 * 1024**3, 188 * 1024**3),
        "/same": Usage(1, 1, 0),
    }
    devs = {"/": 1, "/home/agent/projects": 2, "/same": 1}

    def device(path):
        if path not in devs:
            raise FileNotFoundError(path)
        return devs[path]

    monkeypatch.setattr(vmstat, "_device", device)
    monkeypatch.setattr(vmstat, "_usage", lambda p: usage[p])
    monkeypatch.setattr(vmstat, "short_host", lambda: "ws-alice")
    monkeypatch.setattr(vmstat.os, "cpu_count", lambda: 8)


def sage(**kw) -> FakeProcess:
    """3.2 GiB, started 1 h 12 min before the fake's uptime, 7200 s of CPU so far."""
    base = {
        "pid": 4242,
        "argv": ["/home/agent/miniforge/envs/sage/bin/sage", "-c", "load('x.sage')"],
        "rss_kib": int(3.2 * GIB_KIB),
        "ticks": 7200 * HZ,
        "start_ticks": int((180_000 - 4320) * HZ),
    }
    return FakeProcess(**{**base, **kw})


def lake(**kw) -> FakeProcess:
    base = {
        "pid": 5151,
        "argv": ["/home/agent/.elan/bin/lake", "build"],
        "rss_kib": int(1.1 * GIB_KIB),
        "ticks": 228 * HZ,
        "start_ticks": int((180_000 - 240) * HZ),
    }
    return FakeProcess(**{**base, **kw})


def vm(**kw) -> vmstat.VmStat:
    """The PM's example on #204: 8 CPUs 18% busy, 32 GB of memory 30% used, a 100 GiB volume
    (97 GiB of ext4) 76% full, up 2 hours."""
    base = {
        "host": "ws-andrew-sutherland",
        "cpus": 8,
        "load": (1.4, 1.0, 0.9),
        "mem_total": int(31.3 * GIB),
        "mem_available": int(31.3 * GIB * 0.7),
        "uptime": 7300.0,
        "disks": (vmstat.Disk("/", 101_430_960 * 1024, 72 * GIB, 23 * GIB),),
        "heaviest": (
            vmstat.Proc(1, "lean", int(7.9 * GIB), 95.0, 600.0),
            vmstat.Proc(2, "sage -c load('x.sage')", 450 * 1024**2, 3.4, 60.0),
        ),
        "unavailable": (),
        "cpu": 0.18,
        "mem_installed": 32 * GIB,
    }
    return vmstat.VmStat(**{**base, **kw})


def test_collect_reads_everything_from_proc(tmp_path, disks):
    root = fake_proc.write(
        tmp_path / "proc",
        procs=[sage(), lake(), FakeProcess(pid=2, argv=[], comm="kthreadd"), FakeProcess(pid=1)],
    )
    stat = vmstat.collect(
        ("/", "/home/agent/projects", "/same", "/gone"),
        sample=0,
        proc=str(root),
        sysfs=str(tmp_path / "no-sys"),
    )
    assert stat.host == "ws-alice" and stat.cpus == 8 and stat.load == (0.4, 0.6, 0.7)
    assert stat.cpu == pytest.approx(0.4 / 8)  # unsampled: the load average over the CPUs
    assert stat.mem_total == 30 * 1024**3 and stat.mem_available == 27 * 1024**3
    assert stat.mem_used == pytest.approx(0.1) and stat.mem_installed is None
    assert stat.uptime == 180_000.0
    assert [d.path for d in stat.disks] == ["/", "/home/agent/projects"]  # one per file system
    assert stat.unavailable == ("disk /gone",)  # named, not silently dropped (PM on #223)
    assert [p.command for p in stat.heaviest] == ["sage -c load('x.sage')", "lake build"]
    first = stat.heaviest[0]
    assert first.pid == 4242 and first.rss == int(3.2 * GIB_KIB) * 1024
    assert round(first.elapsed) == 4320 and round(first.cpu) == round(100 * 7200 / 4320)


@pytest.mark.parametrize("top", [0, 3])
def test_a_sampled_collect_measures_cpu_over_the_window(tmp_path, disks, top):
    """The CPU share is what a task manager shows, from `/proc/stat` over the same sleep as the
    processes' CPU time (PM spec on #204: about a second)."""
    root = fake_proc.write(tmp_path / "proc", procs=[sage(), lake()])
    slept = []
    now = [1000.0]

    def sleep(seconds):  # a loaded machine: 1 s passes, sage uses 2.1 CPUs, lake one
        slept.append(seconds)
        now[0] += 1.0
        fake_proc.advance(root, 4242, int(2.1 * HZ))
        fake_proc.advance(root, 5151, int(1.0 * HZ))
        fake_proc.advance_cpus(root, busy=144, idle=656)  # 18% of 800 ticks (8 CPUs, 1 s)

    stat = vmstat.collect(top=top, proc=str(root), sleep=sleep, clock=lambda: now[0])
    assert slept == [1.0]  # one sample for both
    assert stat.cpu == pytest.approx(0.18)
    if top:  # divided by the measured second, not the requested one (codex on #223)
        assert [(p.command, round(p.cpu)) for p in stat.heaviest] == [
            ("sage -c load('x.sage')", 210),
            ("lake build", 100),
        ]
    else:
        assert stat.heaviest == () and stat.unavailable == ()


def test_iowait_counts_as_idle_and_steal_as_busy(tmp_path):
    (tmp_path / "stat").write_text("cpu  10 5 20 100 40 1 2 3 7 0\ncpu0 1 1 1 1 1 1 1 1\n")
    assert vmstat._cpu_times(str(tmp_path)) == (10 + 5 + 20 + 1 + 2 + 3, 181)
    (tmp_path / "stat").write_text("intr 1\n")
    assert vmstat._cpu_times(str(tmp_path)) is None


def test_a_stalled_proc_stat_falls_back_to_the_load_average(tmp_path, disks):
    root = fake_proc.write(tmp_path / "proc", load=(12.0, 1, 1))  # overloaded: capped at 100%
    stat = vmstat.collect(top=0, proc=str(root), sleep=lambda s: None)
    assert stat.cpu == 1.0


def test_installed_memory_counts_online_blocks_only(tmp_path, disks):
    """`lsmem`'s total: the VM's 32 GiB, where MemTotal says about 31."""
    sysfs = fake_proc.write_sys(tmp_path / "sys", 32, offline=4)
    root = fake_proc.write(tmp_path / "proc", mem_total_kib=int(30.7 * 1024**2))
    stat = vmstat.collect(top=0, sample=0, proc=str(root), sysfs=str(sysfs))
    assert stat.mem_installed == 32 * 1024**3
    (sysfs / "devices/system/memory/block_size_bytes").write_text("not hex\n")
    stat = vmstat.collect(top=0, sample=0, proc=str(root), sysfs=str(sysfs))
    assert stat.mem_installed is None


def test_top_zero_scans_no_process_and_sample_zero_does_not_sleep(tmp_path, disks):
    root = fake_proc.write(tmp_path / "proc", procs=[sage()])
    stat = vmstat.collect(top=0, sample=0, proc=str(root), sleep=lambda s: pytest.fail("slept"))
    assert stat.heaviest == () and stat.unavailable == ()


def test_the_heaviest_is_whichever_resource_dominates(tmp_path, disks):
    big = lake(pid=7, argv=["magma"], rss_kib=20 * GIB_KIB, ticks=0)  # 2/3 of memory, idle
    busy = lake(pid=8, argv=["gp"], rss_kib=GIB_KIB // 10, ticks=240 * 4 * HZ)  # 4 of 8 CPUs
    small = lake(pid=9, argv=["bash"], rss_kib=4096, ticks=1)
    root = fake_proc.write(tmp_path / "proc", procs=[small, busy, big])
    stat = vmstat.collect(top=2, sample=0, proc=str(root))
    assert [p.command for p in stat.heaviest] == ["magma", "gp"]


def test_without_proc_only_load_and_disk_are_reported(tmp_path, disks, monkeypatch):
    monkeypatch.setattr(vmstat.os, "getloadavg", lambda: (1.2, 1.5, 1.6))
    stat = vmstat.collect(proc=str(tmp_path / "no-proc"), sleep=lambda s: pytest.fail("slept"))
    assert stat.load == (1.2, 1.5, 1.6) and stat.cpu == pytest.approx(0.15) and stat.disks
    assert stat.unavailable == ("memory", "uptime", "processes")
    assert stat.mem_total is None and stat.mem_installed is None and stat.heaviest == ()
    assert vmstat.format_report(stat).splitlines()[1:] == [
        "🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 1.2 of 8 CPUs",
        "memory: unavailable",
        "🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 44% of 100 GB disk",
        "",
        "Using the most: unavailable",
    ]
    assert vmstat.summary_line(stat) == "VM: 1.2 of 8 CPUs · memory unavailable · disk 44%"


def test_format_report_matches_the_issue():
    """The PM's spec on #204, in its fallback layout for a phone (review on #280: a table was
    about 355-395 px against 318-335 px): the host name in bold, one line per resource with the
    bar first, so the bars line up and the text after them may wrap, a blank line, the status
    line, then in a DM the processes using the most. CPU use is in CPUs, never a percentage
    (Drew, Oct 18; #441)."""
    status = ["1 turn running", "up 2 hours", "Codex and Claude signed in", "zab 0.2.1"]
    assert vmstat.format_report(vm(), status=status) == (
        "**ws-andrew-sutherland**\n"
        "🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 1.4 of 8 CPUs\n"
        "🟩🟩🟩⬜⬜⬜⬜⬜⬜⬜ 30% of 32 GB memory\n"
        "🟨🟨🟨🟨🟨🟨🟨🟨⬜⬜ 76% of 97 GB disk\n"
        "\n"
        "1 turn running · up 2 hours · Codex and Claude signed in · zab 0.2.1\n"
        "Using the most: `lean` 7.9 GB memory, 1.0 CPUs"
        " · `sage -c load('x.sage')` 450 MB memory, 0.0 CPUs"
    )


@pytest.mark.parametrize(
    ("used", "text"),
    [(2.1, "2.1 CPUs"), (0.95, "1.0 CPUs"), (1.0, "1.0 CPUs"), (1.25, "1.3 CPUs"),
     (0.04, "0.0 CPUs"), (0.05, "0.1 CPUs"), (0.0, "0.0 CPUs"), (-0.1, "0.0 CPUs")],
)  # fmt: skip
def test_cpu_use_is_in_cpus_to_one_decimal(used, text):
    """Drew, Oct 18 (#441): "2.1 CPUs", not "210% CPU". One decimal, halves up as the percents
    round; an amount is always plural ("1.0 CPUs"), only a count agrees with its number."""
    assert vmstat.cpus_text(used) == text
    assert vmstat.cpus_text(used, 8) == text.replace(" CPUs", " of 8 CPUs")
    assert vmstat.cpus_text(0.5, 1) == "0.5 of 1 CPU"


def test_every_cpu_figure_is_in_cpus():
    """The bar's line, each process (`top`'s 210% is 2.1 CPUs) and `!usage`'s line (#441)."""
    sage = vmstat.Proc(3, "sage", 3 * GIB, 210.0, 60.0)
    stat = vm(cpus=1, cpu=0.5, heaviest=(sage,))
    assert vmstat.resources(stat)[0] == "🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 0.5 of 1 CPU"
    assert vmstat.heaviest_line(stat) == "Using the most: `sage` 3.0 GB memory, 2.1 CPUs"
    assert vmstat.summary_line(stat) == "VM: 0.5 of 1 CPU · memory 30% · disk 76%"
    assert vmstat.resources(vm(cpu=1.0))[0] == "🟥🟥🟥🟥🟥🟥🟥🟥🟥🟥 8.0 of 8 CPUs"
    # no count to multiply by (`os.cpu_count()` None): the share, as before
    assert vmstat.resources(vm(cpus=None))[0] == "🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 18% of CPU"
    assert vmstat.summary_line(vm(cpus=None)).startswith("VM: CPU 18% · ")
    assert vmstat.resources(vm(cpu=None))[0] == "8 CPUs: unavailable"
    assert vmstat.summary_line(vm(cpu=None)).startswith("VM: CPU unavailable · ")


def test_totals_only_shows_no_command_line(tmp_path, disks):
    root = fake_proc.write(tmp_path / "proc", procs=[sage()])
    stat = vmstat.collect(("/", "/home/agent/projects"), sample=0, proc=str(root))
    report = vmstat.format_report(stat, status=["up 2 days"], totals_only=True)
    assert report.endswith(" disk at `/home/agent/projects`\n\nup 2 days")
    assert "sage" not in report and "Using" not in report
    # a project on another file system gets its own line, labelled with its path
    assert report.splitlines()[3:5] == [
        "🟩🟩🟩🟩⬜⬜⬜⬜⬜⬜ 44% of 100 GB disk",
        "🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜ 6% of 200 GB disk at `/home/agent/projects`",
    ]


@pytest.mark.parametrize(
    ("mem_total", "installed", "shown"),
    [
        (int(31.3 * GIB), None, 32),  # MemTotal rounded up: a 32 GB VM says 32, not 31
        (int(30.7 * GIB), None, 31),
        (int(30.7 * GIB), 32 * GIB, 32),  # the online memory blocks, as lsmem counts them
        (int(31.2 * GIB), 32 * GIB + 128 * MIB, 32),  # a partial block: to the nearest GB
        (32 * GIB, None, 32),
        (32 * GIB + 1, None, 33),
        # the blocks up to the size sold (a multiple of 2 GiB, 4 above 16) within 1 GiB: EC2
        # leaves some of the memory below 4 GiB out of them (the data on #280)
        (32_135_348 * 1024, 251 * 128 * MIB, 32),  # the m7a.2xlarge, 31 by MemTotal alone
        (int(125.08 * GIB), 128 * GIB, 128),  # the development machine
        (int(15.2 * GIB), int(15.375 * GIB), 16),
        (int(189.5 * GIB), int(191.375 * GIB), 192),
        (int(29.2 * GIB), int(29.875 * GIB), 30),  # 32 is 2.1 GiB away: no such size sold
        (int(0.95 * GIB), 1 * GIB, 1),  # codex on #281: a 1 GiB VM says 1, not 2
        # review on #280: an lxcfs container's MemTotal is its limit, /sys lists the host's
        # blocks; a balloon driver lowers MemTotal too. Below 3/4 of the blocks, MemTotal wins.
        (8 * GIB, 128 * GIB, 8),
        (20 * GIB, 32 * GIB, 20),
        (24 * GIB, 32 * GIB, 32),
    ],
)
def test_memory_is_rounded_up_to_whole_gb(mem_total, installed, shown):
    stat = vm(mem_total=mem_total, mem_available=mem_total // 2, mem_installed=installed)
    assert vmstat.memory_gb(stat) == shown
    assert vmstat.resources(stat)[1] == f"🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50% of {shown} GB memory"


def test_the_test_vm_says_32_gb(tmp_path, disks):
    """The numbers from the m7a.2xlarge test VM (32 GiB sold): MemTotal
    32135348 kB (30.65 GiB), 251 online blocks of 128 MiB (31.375 GiB; `lsmem` says 31.4G)."""
    root = fake_proc.write(tmp_path / "proc", mem_total_kib=32_135_348)
    sysfs = fake_proc.write_sys(tmp_path / "sys", 31.375)
    assert len(list((sysfs / "devices/system/memory").glob("memory*"))) == 251
    stat = vmstat.collect(top=0, sample=0, proc=str(root), sysfs=str(sysfs))
    assert stat.mem_installed == 251 * 128 * MIB
    assert vmstat.gb(stat.mem_total, up=True) == 31 and vmstat.gb(stat.mem_installed) == 31
    assert vmstat.memory_gb(stat) == 32
    assert vmstat.resources(stat)[1].endswith(" of 32 GB memory")


@pytest.mark.parametrize(
    ("installed", "sold"),
    [
        (251 * 128 * MIB, 32),
        (32 * GIB, 32),
        (int(31.125 * GIB), 32),
        # codex on #281: a whole number of GiB hides nothing, even 1 GiB short of a size
        (1 * GIB, 1),
        (3 * GIB, 3),
        (7 * GIB, 7),
        (31 * GIB, 31),
        (30 * GIB, 30),  # 32 is 2 GiB away: the blocks to the nearest GB
        (int(7.375 * GIB), 8),
        (int(15.375 * GIB), 16),
        (int(16.125 * GIB), 16),  # just above 16: the next size, 20, is too far
        (int(191.375 * GIB), 192),
        (int(0.875 * GIB), 1),
    ],
)
def test_sold_size_from_the_online_blocks(installed, sold):
    assert vmstat.sold_gb(installed) == sold


def test_an_lxcfs_container_shows_its_own_memory(tmp_path, disks):
    """The reviewer's repro on #280: an 8 GiB limit, 4 GiB used, a 128 GiB host."""
    root = fake_proc.write(
        tmp_path / "proc", mem_total_kib=8 * GIB_KIB, mem_available_kib=4 * GIB_KIB
    )
    sysfs = fake_proc.write_sys(tmp_path / "sys", 128)
    stat = vmstat.collect(top=0, sample=0, proc=str(root), sysfs=str(sysfs))
    assert stat.mem_installed == 128 * GIB
    assert vmstat.resources(stat)[1] == "🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50% of 8 GB memory"


@pytest.mark.parametrize("limit", [8, 96, 100, 120])
def test_lxcfs_meminfo_leaves_the_host_blocks_out(tmp_path, disks, monkeypatch, limit):
    """Review on #280: a container limited to 3/4 of a 128 GiB host or more said "128 GB".
    lxcfs mounts its own file over /proc/meminfo, so its device differs from /proc's; then the
    host's blocks in /sys say nothing about the container, whatever its limit."""
    root = fake_proc.write(
        tmp_path / "proc", mem_total_kib=limit * GIB_KIB, mem_available_kib=limit * GIB_KIB // 2
    )
    sysfs = fake_proc.write_sys(tmp_path / "sys", 128)
    real_stat = os.stat

    def lxcfs_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        return SimpleNamespace(st_dev=st.st_dev + 1) if Path(path).name == "meminfo" else st

    assert not vmstat._lxcfs(str(root))  # a plain /proc: one device
    monkeypatch.setattr(vmstat.os, "stat", lxcfs_stat)
    assert vmstat._lxcfs(str(root))
    stat = vmstat.collect(top=0, sample=0, proc=str(root), sysfs=str(sysfs))
    assert stat.mem_installed is None
    assert vmstat.resources(stat)[1] == f"🟩🟩🟩🟩🟩⬜⬜⬜⬜⬜ 50% of {limit} GB memory"


@pytest.mark.parametrize(
    ("total", "shown"),
    [(101_430_960 * 1024, 97), (int(96.4 * GIB), 96), (int(96.5 * GIB), 97), (200 * GIB, 200)],
)
def test_disk_total_is_rounded_to_the_nearest_gb(total, shown):
    stat = vm(disks=(vmstat.Disk("/", total, 41 * GIB, 54 * GIB),))
    assert vmstat.resources(stat)[2].endswith(f"% of {shown} GB disk")


def test_what_cannot_be_read_says_unavailable():
    stat = vm(
        cpus=None,
        load=None,
        cpu=None,
        mem_total=None,
        mem_available=None,
        mem_installed=None,
        disks=(vmstat.Disk("/work`x\ny", 10 * GIB, GIB, 9 * GIB),),
        unavailable=("load", "memory", "disk /"),
    )
    assert vmstat.resources(stat) == [
        "CPU: unavailable",
        "memory: unavailable",
        "disk: unavailable",  # the root's line stays first
        "🟩⬜⬜⬜⬜⬜⬜⬜⬜⬜ 10% of 10 GB disk at `/work'x y`",
    ]


def test_summary_line_for_usage(tmp_path, disks):
    root = fake_proc.write(tmp_path / "proc")
    stat = vmstat.collect(top=0, sample=0, proc=str(root))
    assert vmstat.summary_line(stat) == "VM: 0.4 of 8 CPUs · memory 10% · disk 44%"
    root_gone = replace(stat, cpu=None, mem_total=None, disks=stat.disks[1:])
    assert vmstat.summary_line(root_gone) == (
        "VM: CPU unavailable · memory unavailable · disk unavailable"
    )


def test_disk_percent_counts_roots_reserve_and_rounds_up_like_df():
    """41 used of 100, 54 available: `df` shows 44% (43.2 rounded up), not 41% (PM on #208,
    codex on #223)."""
    assert vmstat.Disk("/", 100, 41, 54).percent == 44
    assert vmstat.Disk("/", 100, 41).percent == 41  # without `free`, of the total
    assert vmstat.Disk("/", 1000, 821, 179).percent == 83
    assert vmstat.Disk("/", 0, 0, 0).percent == 0


def test_backticks_in_a_command_cannot_break_out_of_its_code_span():
    stat = vmstat.VmStat(
        host="ws-x",
        cpus=1,
        load=None,
        mem_total=None,
        mem_available=None,
        uptime=None,
        disks=(),
        heaviest=(vmstat.Proc(1, "echo `id`", 1024**2, 1.0, 5.0),),
        unavailable=(),
    )
    assert vmstat.heaviest_line(stat) == "Using the most: `echo 'id'` 1 MB memory, 0.0 CPUs"


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (35, "up less than a minute"),
        (60, "up 1 minute"),
        (720, "up 12 minutes"),
        (3600, "up 1 hour"),
        (7300, "up 2 hours"),
        (86_400, "up 1 day"),
        (183_600, "up 2 days"),
        (None, None),
    ],
)
def test_uptime_is_in_words(seconds, text):
    """Words, not unit letters (PM spec on #204)."""
    assert vmstat.uptime_text(vm(uptime=seconds)) == text
    assert [vmstat.words(s) for s in (0, 1, 40, 420)] == [
        "0 seconds",
        "1 second",
        "40 seconds",
        "7 minutes",
    ]
    assert (vmstat.size(int(7.9 * GIB)), vmstat.size(450 * 1024**2)) == ("7.9 GB", "450 MB")


def test_a_command_is_always_one_line_inside_its_code_span():
    """Codex on #223: blank lines in an argument would end the code span."""
    command = vmstat.short_command(["python", "\n\n@**all**\n\n", "a\tb\x1bc"])
    assert command == "python @**all** a b c"
    stat = vmstat.VmStat(
        host="ws-x",
        cpus=1,
        load=None,
        mem_total=None,
        mem_available=None,
        uptime=None,
        disks=(),
        heaviest=(vmstat.Proc(1, command, 1024**2, 1.0, 5.0),),
        unavailable=(),
    )
    assert (
        vmstat.heaviest_line(stat)
        == "Using the most: `python @**all** a b c` 1 MB memory, 0.0 CPUs"
    )


def test_an_empty_command_still_pairs_its_code_span():
    """PM on #223: `exec -a ' '` gave an empty pair of backticks, so Zulip paired the next
    command's opening backtick instead and rendered its text (a live `@**all**`) as Markdown."""
    assert vmstat.short_command([" "]) == "?"
    assert vmstat.short_command(["\x01\x02"]) == "?"
    assert vmstat.short_command([]) == "?"
    stat = vmstat.VmStat(
        host="ws-x",
        cpus=1,
        load=None,
        mem_total=None,
        mem_available=None,
        uptime=None,
        disks=(),
        heaviest=(
            vmstat.Proc(1, vmstat.short_command([" "]), 1024**2, 1.0, 5.0),
            vmstat.Proc(2, "x @**all**", 1024**2, 1.0, 5.0),
        ),
        unavailable=(),
    )
    line = vmstat.heaviest_line(stat)
    assert line == "Using the most: `?` 1 MB memory, 0.0 CPUs · `x @**all**` 1 MB memory, 0.0 CPUs"
    assert line.count("`") % 2 == 0


def test_a_rewritten_process_title_is_kept_whole():
    assert vmstat.short_command(["sshd: ubuntu@pts/0"]) == "sshd: ubuntu@pts/0"
    assert vmstat.short_command(["/usr/bin/python3", "x.py"]) == "python3 x.py"


def test_short_command_keeps_it_short():
    assert vmstat.short_command(["/usr/bin/python3", "/home/a/run.py", "--n", "5"]) == (
        "python3 run.py --n"
    )
    long = vmstat.short_command(["x", "y" * 80])
    assert len(long) == vmstat.COMMAND_MAX and long.endswith("…")


def test_a_process_name_that_is_not_utf8_parses(tmp_path, disks):
    """Codex on #223: a strict UTF-8 read of `bad\xffname` lost the whole report."""
    root = fake_proc.write(tmp_path / "proc", procs=[sage()])
    stat_file = root / "4242" / "stat"
    stat_file.write_bytes(stat_file.read_bytes().replace(b"(sage)", b"(bad\xffname)"))
    (root / "4242" / "cmdline").write_bytes(b"bad\xffname\0")
    status = root / "4242" / "status"
    status.write_bytes(status.read_bytes().replace(b"Name:\tx", b"Name:\tbad\xffname"))
    stat = vmstat.collect(sample=0, proc=str(root))
    first = stat.heaviest[0]
    assert (first.pid, first.command) == (4242, "bad\ufffdname")
    assert first.rss == int(3.2 * GIB_KIB) * 1024


def test_a_comm_with_spaces_and_parentheses_parses(tmp_path, disks):
    odd = sage(comm="a) b (c")
    root = fake_proc.write(tmp_path / "proc", procs=[odd])
    assert vmstat.collect(sample=0, proc=str(root)).heaviest[0].pid == 4242


def test_the_real_proc_if_there_is_one():
    if not os.path.isdir("/proc/self"):
        pytest.skip("no /proc")
    stat = vmstat.collect(top=1, sample=0.05)
    assert stat.unavailable == () and stat.load is not None and stat.disks
    assert stat.cpu is not None and len(stat.heaviest) == 1
    if os.path.isdir("/sys/devices/system/memory"):
        assert stat.mem_installed is not None and stat.mem_installed >= stat.mem_total
    assert "unavailable" not in vmstat.format_report(stat)
