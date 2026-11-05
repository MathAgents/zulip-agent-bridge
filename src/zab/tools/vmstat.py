"""The VM's CPU, memory, disk and heaviest processes, for `!vm`, `!usage` and `zab sentinel`.

Issue #204. Standard library only (no psutil, SPEC §3.3): `/proc/stat`, `/proc/loadavg`,
`/proc/meminfo`, `/proc/uptime` and `/proc/<pid>/{stat,status,cmdline}`, the memory blocks in
`/sys/devices/system/memory`, plus `os.cpu_count()` and `shutil.disk_usage()`. Without `/proc`
(macOS) the CPU share comes from `os.getloadavg()` and the rest that needs `/proc` is
unavailable. Nothing here raises for a missing or unreadable file: the part is left as None and
named in `VmStat.unavailable`.

`collect` blocks: it reads `/proc/stat` twice, `sample` seconds apart, for the share of all CPUs
that was busy (with `top > 0` it reads every process's CPU time in the same window), so the
bridge calls it through `asyncio.to_thread`. `top=0` (channels and `!usage`) scans no process.

The report is something a participant can read at a glance (PM spec on #204, Oct 13): one bar
per resource from `zab.render.bars`, sizes in "GB" (binary gigabytes; Drew: "GiB" confuses
people), CPU use in CPUs, never a percentage (Drew, Oct 18: "2.1 CPUs", not "210% CPU"; #441),
no swap and no raw load averages.
"""

from __future__ import annotations

import math
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zab.render.bars import cells, whole_percent
from zab.tools.common import short_host

GIB = 1024**3
MIB = 1024**2
#: The longest process command shown, in characters.
COMMAND_MAX = 40


@dataclass(frozen=True)
class Disk:
    path: str  # the path asked for: "/" or the project directory
    total: int  # bytes
    used: int  # bytes
    free: int | None = None  # bytes available to unprivileged users, as `df` counts them

    @property
    def percent(self) -> int:
        """Use as `df` shows it: used / (used + available), rounded up, so root's reserve counts
        as full and 82.1% reads 83% (codex on #223)."""
        room = self.used + self.free if self.free is not None else self.total
        return -(-100 * self.used // room) if room else 0


@dataclass(frozen=True)
class Proc:
    pid: int
    command: str  # argv[0]'s basename plus up to two arguments, at most COMMAND_MAX characters
    rss: int  # bytes
    cpu: float  # percent of one CPU over the sample window (210.0 = 2.1 CPUs)
    elapsed: float  # seconds since the process started


@dataclass(frozen=True)
class VmStat:
    host: str  # short host name, "ws-alice"
    cpus: int | None
    load: tuple[float, float, float] | None  # 1, 5 and 15 minutes (not shown: `cpu` is)
    mem_total: int | None  # bytes, MemTotal; None without /proc
    mem_available: int | None
    uptime: float | None  # seconds
    disks: tuple[Disk, ...]  # one per file system, in the order asked
    heaviest: tuple[Proc, ...]  # () when top=0 or without /proc
    unavailable: tuple[str, ...]  # what couldn't be read, e.g. ("memory", "uptime") on macOS
    cpu: float | None = None  # busy share of all CPUs, 0..1: sampled, else 1-minute load / CPUs
    # bytes in online memory blocks (what `lsmem` totals); None when /proc/meminfo is lxcfs's
    mem_installed: int | None = None

    @property
    def mem_used(self) -> float | None:
        """(MemTotal - MemAvailable) / MemTotal."""
        if not self.mem_total or self.mem_available is None:
            return None
        return (self.mem_total - self.mem_available) / self.mem_total


# ------------------------------------------------------------------------------- collection


def _meminfo(proc: str) -> dict[str, int]:
    """`/proc/meminfo` in bytes, by field name."""
    values = {}
    for line in Path(proc, "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0]) * (1024 if parts[1:2] == ["kB"] else 1)
    return values


def _load(proc: str) -> tuple[float, float, float]:
    try:
        a, b, c = Path(proc, "loadavg").read_text().split()[:3]
        return float(a), float(b), float(c)
    except OSError:
        return os.getloadavg()  # macOS and other systems without /proc


def _cpu_times(proc: str) -> tuple[int, int] | None:
    """(busy, total) clock ticks of all CPUs since boot, from `/proc/stat`'s `cpu` line, counted
    as psutil and task managers do: idle and iowait are idle, steal is busy, and guest time is
    already inside user and nice."""
    try:
        fields = Path(proc, "stat").read_text().split("\n", 1)[0].split()
        values = [int(x) for x in fields[1:9]] if fields[0] == "cpu" else []
    except (OSError, ValueError, IndexError):
        return None
    if len(values) < 4:
        return None
    total = sum(values)
    return total - sum(values[3:5]), total


def _installed(sysfs: str) -> int | None:
    """Bytes in the online memory blocks, as `lsmem` totals them: the size the VM was sold with
    (32 GiB), where MemTotal leaves out what the kernel keeps for itself (about 31)."""
    base = Path(sysfs, "devices", "system", "memory")
    try:
        block = int(Path(base, "block_size_bytes").read_text().strip(), 16)
        online = sum(
            Path(e.path, "state").read_text().strip() == "online"
            for e in os.scandir(base)
            if e.name.startswith("memory") and e.name[6:].isdigit()
        )
    except (OSError, ValueError):
        return None
    return block * online or None


def _lxcfs(proc: str) -> bool:
    """Whether `/proc/meminfo` is a file mounted over procfs's, as lxcfs does to give a container
    its own limit as MemTotal. `/sys` still lists the host's memory blocks then, whatever the
    limit, so they say nothing about this machine (review on #280)."""
    try:
        return os.stat(Path(proc, "meminfo")).st_dev != os.stat(proc).st_dev
    except OSError:
        return False


def _device(path: str) -> int:
    return os.stat(path).st_dev


def _usage(path: str) -> Any:  # shutil.disk_usage's (total, used, free)
    return shutil.disk_usage(path)


def _disks(paths: Sequence[str]) -> tuple[tuple[Disk, ...], list[str]]:
    """The file systems of `paths` (one per device) and the paths that couldn't be read."""
    seen: set[int] = set()
    disks, failed = [], []
    for path in paths:
        try:
            dev = _device(path)
            if dev in seen:
                continue
            usage = _usage(path)
        except OSError:
            failed.append(path)  # named in `unavailable`, not silently dropped (PM on #223)
            continue
        seen.add(dev)
        disks.append(Disk(path=path, total=usage.total, used=usage.used, free=usage.free))
    return tuple(disks), failed


def short_command(argv: Sequence[str]) -> str:
    """`argv[0]`'s basename plus up to two arguments (paths reduced to their basenames); never
    empty, so a report's code spans always pair up (PM on #223: an empty pair of backticks made
    Zulip pair the next command's opening backtick instead, and its text rendered as Markdown)."""
    if not argv:
        return "?"
    # a rewritten process title (`sshd: ubuntu@pts/0`) is not a path: keep it whole
    first = argv[0] if " " in argv[0] else (os.path.basename(argv[0]) or argv[0])
    words = [first]
    for arg in argv[1:3]:
        words.append(os.path.basename(arg) if "/" in arg and not arg.endswith("/") else arg)
    # one line, always: an argument holding blank lines would otherwise end the report's code
    # span and put whatever follows, `@**all**` say, into the message as Markdown (codex on #223)
    text = " ".join("".join(c if c.isprintable() else " " for c in " ".join(words)).split())
    if not text:
        return "?"
    return text if len(text) <= COMMAND_MAX else text[: COMMAND_MAX - 1] + "…"


def _cpu_ticks(proc: str, pid: str) -> tuple[int, int] | None:
    """(utime + stime, starttime) in clock ticks, from `/proc/<pid>/stat`."""
    try:
        stat = Path(proc, pid, "stat").read_bytes()
    except OSError:
        return None
    # the command between the parentheses may hold spaces, parentheses and any byte at all
    # (codex on #223: a strict UTF-8 read of `bad\xffname` lost the whole report)
    fields = stat.rsplit(b")", 1)[-1].split()
    try:
        return int(fields[11]) + int(fields[12]), int(fields[19])
    except (IndexError, ValueError):
        return None


def _rss(proc: str, pid: str) -> int:
    try:
        for line in Path(proc, pid, "status").read_bytes().splitlines():  # `Name:` is any bytes
            if line.startswith(b"VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def _processes(
    proc: str,
    *,
    top: int,
    sample: float,
    uptime: float,
    cpus: int,
    mem_total: int,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> tuple[Proc, ...]:
    """The `top` processes with the largest share of all CPUs or of memory.

    Each process's CPU time is read twice and divided by the time measured between its two
    reads, not by `sample`: scanning and a loaded scheduler stretch the interval (codex on #223).
    """
    pids = [e.name for e in os.scandir(proc) if e.name.isdigit()]
    first = {pid: (*t, clock()) for pid in pids if (t := _cpu_ticks(proc, pid))}
    if sample > 0:
        sleep(sample)
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    found = []
    for pid, (ticks0, start, read_at) in first.items():
        try:
            argv = [a for a in Path(proc, pid, "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if not argv:  # a kernel thread
            continue
        elapsed = max(0.0, uptime - start / hz)
        if sample > 0:
            later = _cpu_ticks(proc, pid)
            interval = clock() - read_at
            if later is None:  # gone during the sample
                continue
            cpu = 100.0 * (later[0] - ticks0) / hz / interval if interval > 0 else 0.0
        else:
            cpu = 100.0 * ticks0 / hz / elapsed if elapsed else 0.0
        command = short_command([a.decode(errors="replace") for a in argv])
        found.append(Proc(int(pid), command, _rss(proc, pid), max(0.0, cpu), elapsed))

    def weight(p: Proc) -> float:
        return max(p.cpu / 100 / max(cpus, 1), p.rss / mem_total if mem_total else 0.0)

    return tuple(sorted(found, key=weight, reverse=True)[:top])


def collect(
    paths: Sequence[str] = ("/",),
    *,
    top: int = 3,
    sample: float = 1.0,
    proc: str = "/proc",
    sysfs: str = "/sys",
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> VmStat:
    """Everything `format_report` and `summary_line` show. Blocks `sample` seconds to measure
    the CPU share (with `sample=0`, the load average stands in); never raises for `/proc`."""
    unavailable: list[str] = []
    cpus = os.cpu_count()
    before = _cpu_times(proc) if sample > 0 else None
    try:
        load: tuple[float, float, float] | None = _load(proc)
    except (OSError, ValueError):
        load = None
        unavailable.append("load")
    try:
        mem = _meminfo(proc)
    except OSError:
        mem = {}
    mem_total, mem_available = mem.get("MemTotal"), mem.get("MemAvailable")
    if mem_total is None or mem_available is None:
        mem_total = mem_available = None
        unavailable.append("memory")
    try:
        uptime: float | None = float(Path(proc, "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        uptime = None
        unavailable.append("uptime")
    slept = False

    def nap(seconds: float) -> None:  # the process sample's sleep is the CPU sample's too
        nonlocal slept
        slept = True
        sleep(seconds)

    heaviest: tuple[Proc, ...] = ()
    if top > 0:
        if uptime is None or not os.path.isdir(proc):
            unavailable.append("processes")
        else:
            try:
                heaviest = _processes(
                    proc,
                    top=top,
                    sample=sample,
                    uptime=uptime,
                    cpus=cpus or 1,
                    mem_total=mem_total or 0,
                    sleep=nap,
                    clock=clock,
                )
            except OSError:
                unavailable.append("processes")
    if before is not None and not slept:
        sleep(sample)
    after = _cpu_times(proc) if before is not None else None
    cpu: float | None = None
    if before is not None and after is not None and after[1] > before[1]:
        cpu = (after[0] - before[0]) / (after[1] - before[1])
    elif load is not None and cpus:  # no sample: the load average, capped at all CPUs
        cpu = load[0] / cpus
    disks, failed = _disks(paths)
    unavailable.extend(f"disk {path}" for path in failed)
    return VmStat(
        host=short_host(),
        cpus=cpus,
        load=load,
        mem_total=mem_total,
        mem_available=mem_available,
        uptime=uptime,
        disks=disks,
        heaviest=heaviest,
        unavailable=tuple(unavailable),
        cpu=None if cpu is None else min(1.0, max(0.0, cpu)),
        mem_installed=None if mem_total is None or _lxcfs(proc) else _installed(sysfs),
    )


# ------------------------------------------------------------------------------- formatting


def gb(n: int, *, up: bool = False) -> int:
    """Whole (binary) gigabytes: to the nearest, or `up`."""
    return -(-n // GIB) if up else math.floor(n / GIB + 0.5)


#: The online memory blocks count as the VM's size only while MemTotal is at least this share
#: of them. The kernel keeps a few percent (30.65 of 31.375 GiB of blocks on the m7a.2xlarge,
#: 125.1 of 128 on the development machine), while a balloon driver lowers MemTotal well below the
#: blocks. (An lxcfs container's MemTotal is its own limit, whatever its share of the host's blocks;
#: `collect` sees lxcfs's /proc/meminfo and leaves the blocks out.)
INSTALLED_SHARE = 0.75
#: A machine's online memory blocks fall short of its sold size by less than this. EC2 leaves
#: part of the memory below 4 GiB out of them: the 32 GiB m7a.2xlarge lists 251 blocks of 128 MiB
#: (31.375 GiB, 0.625 short), and `lsmem` says 31.4G. A whole number of GiB hides nothing: 1, 3
#: or 31 GiB of blocks stays so. Nothing unprivileged gives the sold size exactly (the DMI
#: tables are root's). Known limit, outside the workshop's EC2 VMs: on bare metal with 64 GiB
#: or more the blocks are 2 GiB and one partly filled with RAM counts whole, so the label can
#: say a GB or two too many; /sys/firmware/memmap's "System RAM" total (127.3 GiB on
#: the development machine's 128), which never exceeds the RAM, is the better input for that case.
HIDDEN_MAX = GIB


def sold_gb(installed: int) -> int:
    """The memory size a machine was sold with, from its online blocks: the next multiple of
    2 GiB (of 4 GiB above 16), which is how VMs are sized, when that is less than `HIDDEN_MAX`
    more; otherwise the blocks to the nearest GB. So 31.375 GiB says 32, 128 says 128, 1 GiB
    says 1 (codex on #281), and a stray partial block (32 GiB + 128 MiB) still says 32 rather
    than 36."""
    step = (4 if installed > 16 * GIB else 2) * GIB
    up = -(-installed // step) * step
    return up // GIB if up - installed < HIDDEN_MAX else gb(installed)


def memory_gb(stat: VmStat) -> int | None:
    """The memory size shown, so a 32 GB VM says 32: MemTotal rounded up, or the size sold
    (`sold_gb`) from the online memory blocks (`lsmem`'s total) when MemTotal is close enough
    to them to be the same machine's, whichever is larger."""
    if stat.mem_total is None:
        return None
    shown = gb(stat.mem_total, up=True)
    installed = stat.mem_installed
    if installed and stat.mem_total >= INSTALLED_SHARE * installed:
        shown = max(shown, sold_gb(installed))
    return shown


def size(n: int) -> str:
    """`7.9 GB`, `450 MB`."""
    return f"{n / GIB:.1f} GB" if n >= 0.95 * GIB else f"{n / MIB:.0f} MB"


def words(seconds: float) -> str:
    """The largest whole unit, in words: `40 seconds`, `1 minute`, `2 hours`, `3 days`."""
    s = max(0, int(seconds))
    for unit, length in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if s >= length:
            n = s // length
            return f"{n} {unit}{'' if n == 1 else 's'}"
    return f"{s} second{'' if s == 1 else 's'}"


def uptime_text(stat: VmStat) -> str | None:
    """`up 2 hours`, or None when the uptime couldn't be read."""
    if stat.uptime is None:
        return None
    return "up less than a minute" if stat.uptime < 60 else "up " + words(stat.uptime)


def _code(text: str) -> str:
    """A one-line code span that its text can't close."""
    text = "".join(c if c.isprintable() else " " for c in text)
    return "`" + text.replace("`", "'") + "`"


def _line(label: str, fraction: float | None) -> str:
    if fraction is None:
        return f"{label}: unavailable"
    return f"{cells(fraction)} {whole_percent(fraction)}% of {label}"


def _count(cpus: int) -> str:
    """`8 CPUs`, `1 CPU`: a count agrees with its number."""
    return f"{cpus} CPU{'' if cpus == 1 else 's'}"


def cpus_text(used: float, of: int | None = None) -> str:
    """CPU use in CPUs, never a percentage (Drew, Oct 18; issue #441): `2.1 CPUs`, or with the
    count `1.4 of 8 CPUs`. The amount has one decimal, halves up as `whole_percent` rounds, and
    is always plural (`1.0 CPUs`, `0.0 CPUs`); only the count can be singular (`0.5 of 1 CPU`)."""
    amount = f"{math.floor(max(0.0, used) * 10 + 0.5) / 10:.1f}"
    return f"{amount} CPUs" if of is None else f"{amount} of {_count(of)}"


def resources(stat: VmStat) -> list[str]:
    """One line per resource, the bar first: `🟩🟩⬜⬜⬜⬜⬜⬜⬜⬜ 1.4 of 8 CPUs`. The ten cells
    are as wide on every line, so the bars line up in Zulip's proportional font, and the text
    after them may wrap on a phone without hiding anything (the spec's fallback: a table did not
    fit 340 px, review on #280). Then memory and the root's disk, and a line for each project on
    another file system, labelled with its path. A line whose numbers couldn't be read says
    "unavailable" instead of a bar. CPU use is in CPUs (#441), memory and disk in whole
    percent."""
    if stat.cpu is not None and stat.cpus:
        cpu = f"{cells(stat.cpu)} {cpus_text(stat.cpu * stat.cpus, stat.cpus)}"
    else:  # unavailable, or the share without a count (`os.cpu_count()` None): `18% of CPU`
        cpu = _line(_count(stat.cpus) if stat.cpus else "CPU", stat.cpu)
    memory = memory_gb(stat)
    lines = [
        cpu,
        _line(f"{memory} GB memory" if memory is not None else "memory", stat.mem_used),
    ]
    found: list[tuple[str, Disk | None]] = [(d.path, d) for d in stat.disks]
    found += [(name[5:], None) for name in stat.unavailable if name.startswith("disk ")]
    for path, disk in sorted(found, key=lambda pair: pair[0] != "/"):  # the root's line first
        label = "disk" if path == "/" else "disk at " + _code(path)
        if disk is None:
            lines.append(_line(label, None))
        else:  # the total to the nearest GB, the percent as `df` shows it
            lines.append(_line(f"{gb(disk.total)} GB {label}", disk.percent / 100))
    return lines


def heaviest_line(stat: VmStat) -> str | None:
    """`Using the most: `lean` 7.9 GB memory, 1.0 CPUs · …`, CPU in CPUs (#441: what `top`
    shows as 210% is 2.1 CPUs); None when no process was asked for."""
    if not stat.heaviest:
        return "Using the most: unavailable" if "processes" in stat.unavailable else None
    shown = [
        f"{_code(p.command)} {size(p.rss)} memory, {cpus_text(p.cpu / 100)}" for p in stat.heaviest
    ]
    return "Using the most: " + " · ".join(shown)


def format_report(stat: VmStat, *, status: Sequence[str] = (), totals_only: bool = False) -> str:
    """Zulip markdown: the host name in bold, a line per resource, a blank line, `status`
    joined into one line, then the processes using the most, unless `totals_only` (for a channel
    where anyone besides the owner can read the reply: command lines can reveal someone's
    work)."""
    lines = [f"**{stat.host}**"] if stat.host else []
    lines += [*resources(stat), ""]
    if status:
        lines.append(" · ".join(status))
    heaviest = None if totals_only else heaviest_line(stat)
    if heaviest:
        lines.append(heaviest)
    return "\n".join(lines).rstrip("\n")


def summary_line(stat: VmStat) -> str:
    """The `!usage` line: `VM: 0.4 of 8 CPUs · memory 10% · disk 41%`, CPU in CPUs as in
    `resources` (`CPU 5%` without a count, `CPU unavailable` without a share)."""

    def part(name: str, fraction: float | None) -> str:
        return f"{name} unavailable" if fraction is None else f"{name} {whole_percent(fraction)}%"

    root = next((d for d in stat.disks if d.path == "/"), None)
    disk = part("disk", root.percent / 100 if root else None)
    if stat.cpu is not None and stat.cpus:
        cpu = cpus_text(stat.cpu * stat.cpus, stat.cpus)
    else:
        cpu = part("CPU", stat.cpu)
    return f"VM: {cpu} · {part('memory', stat.mem_used)} · {disk}"
