"""A fake `/proc` tree for `zab.tools.vmstat` tests (issue #204).

    root = fake_proc.write(tmp_path / "proc", load=(0.4, 0.6, 0.7), procs=[
        fake_proc.FakeProcess(pid=4242, argv=["sage", "-c", "x"], rss_kib=3_300_000,
                              ticks=7_200, start_ticks=1_000),
    ])
    stat = vmstat.collect(top=2, sample=0, proc=str(root))

`ticks` is the process's utime + stime and `start_ticks` its start after boot, both in clock
ticks (`os.sysconf("SC_CLK_TCK")`, 100 on Linux). `advance(root, pid, ticks)` adds CPU time, for a
`sleep` stand-in that makes a sampled `collect` see a busy process; `advance_cpus(root, busy,
idle)` does the same for `/proc/stat`'s all-CPU line. `write_sys(root, gib)` writes the memory
blocks of a `/sys` holding `gib` GiB of online memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FakeProcess:
    pid: int
    argv: list[str] = field(default_factory=list)  # empty: a kernel thread
    rss_kib: int = 0
    ticks: int = 0  # utime + stime
    start_ticks: int = 0
    comm: str = ""


def _stat_line(p: FakeProcess) -> str:
    comm = p.comm or (Path(p.argv[0]).name if p.argv else "kworker/0:1")
    utime, stime = p.ticks - p.ticks // 2, p.ticks // 2
    # fields 3.. after "(comm)": state ppid pgrp session tty tpgid flags minflt cminflt majflt
    # cmajflt utime stime cutime cstime priority nice threads itrealvalue starttime ...
    rest = ["S", "1", str(p.pid), str(p.pid), "0", "-1", "4194304", "0", "0", "0", "0"]
    rest += [str(utime), str(stime), "0", "0", "20", "0", "1", "0", str(p.start_ticks)]
    rest += ["0"] * 10
    return f"{p.pid} ({comm}) " + " ".join(rest) + "\n"


def write_process(root: Path, p: FakeProcess) -> None:
    d = root / str(p.pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "stat").write_text(_stat_line(p))
    (d / "status").write_text(f"Name:\t{p.comm or 'x'}\nVmRSS:\t{p.rss_kib} kB\n")
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in p.argv))


def advance(root: Path, pid: int, ticks: int) -> None:
    """Add `ticks` of CPU time to a process written by `write`."""
    path = Path(root) / str(pid) / "stat"
    head, _, tail = path.read_text().rpartition(")")
    fields = tail.split()
    fields[11] = str(int(fields[11]) + ticks)
    path.write_text(head + ") " + " ".join(fields) + "\n")


def advance_cpus(root: Path, busy: int, idle: int) -> None:
    """Add `busy` user ticks and `idle` idle ticks to `/proc/stat`'s `cpu` line."""
    path = Path(root) / "stat"
    first, _, rest = path.read_text().partition("\n")
    fields = first.split()
    fields[1] = str(int(fields[1]) + busy)
    fields[4] = str(int(fields[4]) + idle)
    path.write_text(" ".join(fields) + "\n" + rest)


def write_sys(root: Path, gib: float, *, offline: int = 0, block: int = 128 * 1024**2) -> Path:
    """`<root>/devices/system/memory` with `gib` GiB online (31.375: 251 blocks of 128 MiB)
    in blocks of `block` bytes."""
    base = Path(root) / "devices" / "system" / "memory"
    base.mkdir(parents=True, exist_ok=True)
    (base / "block_size_bytes").write_text(f"{block:x}\n")
    online = int(gib * 1024**3) // block
    for i in range(online + offline):
        (base / f"memory{i}").mkdir(exist_ok=True)
        (base / f"memory{i}" / "state").write_text("online\n" if i < online else "offline\n")
    (base / "auto_online_blocks").write_text("online\n")
    return Path(root)


def write(
    root: Path,
    *,
    load: tuple[float, float, float] = (0.4, 0.6, 0.7),
    cpu: tuple[int, ...] = (1000, 0, 500, 8000, 500, 0, 0, 0, 0, 0),  # user … guest_nice
    mem_total_kib: int = 31_457_280,  # 30 GiB
    mem_available_kib: int = 28_311_552,  # 27 GiB
    swap_total_kib: int = 8_388_608,  # 8 GiB
    swap_free_kib: int = 8_388_608,
    uptime: float = 180_000.0,  # 2 d 2 h
    procs: list[FakeProcess] | None = None,
) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "loadavg").write_text(" ".join(f"{x:.2f}" for x in load) + " 2/300 4242\n")
    (root / "stat").write_text(
        "cpu  " + " ".join(map(str, cpu)) + "\ncpu0 " + " ".join(map(str, cpu)) + "\nctxt 1\n"
    )
    (root / "meminfo").write_text(
        f"MemTotal:       {mem_total_kib} kB\n"
        f"MemFree:        1000 kB\n"
        f"MemAvailable:   {mem_available_kib} kB\n"
        f"SwapTotal:      {swap_total_kib} kB\n"
        f"SwapFree:       {swap_free_kib} kB\n"
    )
    (root / "uptime").write_text(f"{uptime:.2f} 1000000.00\n")
    for p in procs or []:
        write_process(root, p)
    return root
