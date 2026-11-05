#!/usr/bin/env python3
"""Read the agents' CLIs, the shared Mathlib, and Oscar where used, into the page cache after
each boot (#461, #483).

    python3 warm_cache.py [--dry-run] [--wait-bridge SECONDS]

Installed by every release rollout of `push_config.py` as `/usr/local/lib/zab/warm_cache.py`, run
as root by `zab-warm-cache.service` after each boot, a start by the waker included. On the test
VM, one boot each, a woken VM's first `import Mathlib` took 110 s without it, 85 s started
at once with it, 10 s once it was done (about 3 minutes after boot), and 4.9 s warm
(docs/DECISIONS.md, 2026-10-18). Once the bridge is up, it reads, in 1 MiB reads and in this
order: the agents' CLIs, so that an agent's cold start after a boot gets faster instead of
slower (#483): the Claude Code version that `~agent/.local/bin/claude` links to and the Codex
package that `~agent/.local/bin/codex` runs from, as workshop/ami/build.sh installs them (both
are single executables: no node or other runtime to read); the toolchain's programs, libraries
and modules (Init, Std and Lean first); the shared
packages' modules, the import closure of `Mathlib.Tactic` first, each module after its imports
(from the sources' headers), then the rest; each module's files being those `import` opens
(`IMPORT`). Then, which no `import` opens: the `.ilean` files that the language server loads,
and Lake's `.trace` and `.hash` files. The unit caps its reads, so that the bridge and the agents
keep part of the volume, and it reads at most half the memory available at its start.

The shared copy is the newest `/opt/lean/packages/<tag>` with a template, as `lean-project` picks
it; without one it reads none.

**Oscar, after Lean, only where it was used (#475).** Drew, Oct 18: "worth doing for anyone who
has used Oscar, but maybe not worth burning 4GB on every VM". Oscar counts as used when one of
its cache files (`~/.julia/compiled/v*/Oscar/*`) was read or written in the last OSCAR_DAYS days,
and after the first boot's volume warm-up (../ami/README.md, "First boot"), which reads every file
of `~/.julia` once and whose end `/var/lib/zab-warm.done` records. Julia reads that `.ji` file at
every `using Oscar` or `import Oscar`, which sets its access time under the file system's
`relatime`, and often also touches it (`touch(path_to_try)` in Julia 1.12's `loading.jl`), which
sets its modification time under any mount option. A `Pkg.precompile()`, and so a `Pkg.add`,
`update` or `instantiate` in the default environment, reads and touches Oscar's cache too: it counts
as a use, and costs about 1.9 GB a boot for the week (review of #479). On such a VM it then reads
the files of Julia's install (the target of `/usr/local/bin/julia`) and of the depot `~/.julia` that
were read or written after that same moment, the least recent first: roughly the order Julia
opened them, since `relatime` moves an access time at most once a day.
That is what Oscar and the participant's other Julia work opened: traced on the development machine
with Julia 1.12.7 and Oscar 1.8.2 from the image's Manifest, `using Oscar` opens 5,058 files of 2.7
GB, 2.1 GB of a 4.3 GB depot and 0.6 GB of a 1.0 GB install, and a class group computation after it
nothing more. Its own reads leave access times alone (`O_NOATIME`, which root may ask for on any
file), so the warm-up never keeps Oscar "used" by itself.
The rule's limits: without `/var/lib/zab-warm.done` (a VM launched without the first-boot
script, or stopped before that warm-up ended) a use can't be told from the first boot's reads,
so Oscar is never read; under `relatime` an access time less than a day old is kept, so the
week is 7 days give or take one, and a use within a day of the first boot leaves no trace on
the files (its cache may still show the use, and then fewer files are read than were used);
under `noatime` only Julia's touch of the cache shows a use, and the files read are only those
written since. No walk starts at, or passes through, a link (`agent` owns these trees): a
depot, `compiled` or `compiled/v*` that is a link counts as no cache, and so do Lean's trees.

Its last own line in the journal, `zab-warm-cache: read …` (systemd's lines about the unit follow
it), says what it read, in how many seconds, the CLIs' share, Oscar's share or why it read
none, the cap in force, and how many files were read without `O_NOATIME` if any were (without
CAP_FOWNER the warm-up would keep Oscar "used" by itself). `--dry-run` counts instead, reading
only the sources' headers.
Standard library only: the VM's system Python runs it.
"""

from __future__ import annotations

import argparse
import itertools
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LEAN = Path("/opt/lean")  # lean-project's ROOT
ELAN = Path("/home/agent/.elan/toolchains")
FIRST_CLOSURE = "Mathlib.Tactic"
TOOLCHAIN_FIRST = ("Init", "Std", "Lean")  # every Mathlib import needs them; Lake comes later
#: What `import` opens for each module, in the toolchain and the packages alike: traced with
#: `openat` on Lean v4.34.1's `import Mathlib` (#464). The `.ir` and `.ir.sig` files, about 22,000
#: small ones, were what an import after a warm-up without them still read from disk (27 s).
IMPORT = (".olean", ".olean.private", ".olean.server", ".ir", ".ir.sig")
#: Read after every module's IMPORT files, since no `import` opens them: the language server's
#: `.ilean` files, then Lake's `.trace` and `.hash` (for `lake build`), tens of thousands of small
#: files whose reads the IOPS cap paces.
LATER = (".ilean", ".trace", ".hash")
CHUNK = 1 << 20
HEADER_MAX = 1 << 20  # read of a source; the largest, Mathlib.lean, is 451 KB
THREADS = 4  # enough reads in flight to reach the cap, which bounds the rate
SETTLE_S = 10  # after the bridge is up, for the answers that a wake is for
POLL_S = 2
BRIDGE_UP = ["systemctl", "--user", "--machine=agent@", "is-active", "--quiet", "zab.service"]
HEADER_WORDS = frozenset({"module", "prelude", "public", "meta", "all"})
#: Oscar (#475): Julia's install (the link's target, `/opt/julia-<version>`) and agent's depot;
#: `/var/lib/zab-warm.done` marks the end of the first boot's volume warm-up
#: (../ami/README.md, "First boot").
JULIA = Path("/usr/local/bin/julia")
#: The agents' CLIs (#483): the links on agent's PATH, the native installer's versions of Claude
#: Code (a single executable each), and the directory of the Codex packages.
AGENT_BIN = Path("/home/agent/.local/bin")
CLAUDE_VERSIONS = Path("/home/agent/.local/share/claude/versions")
CODEX_LIB = Path("/home/agent/.local/lib")
DEPOT = Path("/home/agent/.julia")
FIRST_BOOT = Path("/var/lib/zab-warm.done")
OSCAR_DAYS = 7
#: Every read leaves the file's access time alone, so the warm-up never counts as a use; root
#: may ask for it on any file (CAP_FOWNER), another user only on its own files.
NOATIME = getattr(os, "O_NOATIME", 0)
#: How many files were opened without NOATIME, since the kernel refused it (`regular`).
_atime_lock = threading.Lock()
atime_fallbacks = 0
_TOKEN = re.compile(r"\s+|--[^\n]*|/-|\S+")
_COMMENT = re.compile(r"/-|-/")


def version_key(tag: str) -> tuple:
    """lean-project's: `v4.9.0` < `v4.34.1-rc2` < `v4.34.1`."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-(.+))?", tag)
    if not m:
        return ((), 0, (tag,))
    pre = m.group(2) or ""
    parts = tuple(int(x) if x.isdigit() else x for x in re.split(r"(\d+)", pre))
    return (tuple(int(n) for n in m.group(1).split(".")), 0 if pre else 1, parts)


def shared() -> tuple[str, Path, Path | None] | None:
    """(tag, packages, toolchain directory or None if unknown) of the newest shared copy."""
    tags = [
        t.name
        for t in (LEAN / "template").glob("*")
        if (t / "lake-manifest.json").is_file() and (LEAN / "packages" / t.name).is_dir()
    ]
    if not tags:
        return None
    tag = max(tags, key=version_key)
    # elan's directory name: `leanprover/lean4:v4.34.1` -> `leanprover--lean4---v4.34.1`
    name = head(str(LEAN / "template" / tag / "lean-toolchain"), 4096).strip()
    name = name.replace("/", "--").replace(":", "---")
    return tag, LEAN / "packages" / tag, ELAN / name if name.strip(".") else None


def regular(path: str) -> int | None:
    """A descriptor of `path` if it is a regular file, else None: a final symlink is never
    followed, a FIFO never waited on (`agent` owns these trees; this runs as root), and the
    access time is left alone wherever the kernel allows it (NOATIME)."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags | NOATIME)
    except PermissionError:  # O_NOATIME on another user's file, without CAP_FOWNER
        try:
            fd = os.open(path, flags)
        except OSError:
            return None
        global atime_fallbacks
        with _atime_lock:
            atime_fallbacks += 1
    except OSError:
        return None
    try:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            return fd
    except OSError:
        pass
    os.close(fd)
    return None


def head(path: str, limit: int = HEADER_MAX) -> str:
    """At most the first `limit` bytes of a regular file, as text; "" for anything else."""
    fd = regular(path)
    if fd is None:
        return ""
    try:
        return os.read(fd, limit).decode(errors="replace")
    except OSError:
        return ""
    finally:
        os.close(fd)


def header_imports(text: str) -> list[str]:
    """The modules a Lean file's header imports: `[module] [prelude]`, then lines such as
    `public meta import all A.B`. Comments are skipped; the first other word ends the header."""
    out: list[str] = []
    pending, i = False, 0
    while m := _TOKEN.match(text, i):
        word, i = m[0], m.end()
        if word == "/-":  # block comments nest
            depth = 1
            while depth and (c := _COMMENT.search(text, i)):
                depth, i = depth + (1 if c[0] == "/-" else -1), c.end()
            if depth:
                break
        elif word.isspace() or word.startswith("--") or word in HEADER_WORDS:
            continue
        elif word == "import":
            pending = True
        elif pending:
            out.append(word)
            pending = False
        else:
            break
    return out


def unlinked(path: Path) -> bool:
    """Whether `path` is reached through no link at all, itself included (review of #479:
    `os.walk` follows a top that is a link, and a path through a linked directory)."""
    return os.path.realpath(path) == os.path.abspath(path)


def files_under(top: Path, suffixes: tuple[str, ...]) -> list[str]:
    """The files under `top` named `*<suffix>`, in path order; symlinks aren't followed, and a
    `top` reached through one is not walked."""
    found: list[str] = []
    if not unlinked(top):
        return found
    for d, dirs, names in os.walk(top):
        dirs.sort()
        found += [os.path.join(d, name) for name in sorted(names) if name.endswith(suffixes)]
    return found


def sized(paths: Iterable[str]) -> Iterator[tuple[str, int]]:
    """Each path that is a regular file, with its size."""
    for path in paths:
        try:
            st = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            yield path, st.st_size


def closure(root: str, modules: dict[str, tuple[Path, str]]) -> Iterator[str]:
    """`root`'s import closure among `modules`, each module after its imports, as the package
    sources' headers say (`head`: a bounded read of a regular file); a module without a
    readable source is a leaf."""
    seen: set[str] = set()
    stack = [(root, False)]
    while stack:
        module, imports_done = stack.pop()
        if imports_done:
            yield module
        elif module not in seen and module in modules:
            seen.add(module)
            source = modules[module][0] / (module.replace(".", "/") + ".lean")
            imports = header_imports(head(str(source)))
            stack += [(module, True), *((m, False) for m in reversed(imports))]


def entries(packages: Path, toolchain: Path | None) -> Iterator[tuple[str, int]]:
    """Every file to read, with its size, in the module docstring's order. Lazy, so that the
    toolchain is read while the packages are listed and their headers parsed."""
    ilean: list[str] = []
    lake: list[str] = []

    def split(lib: Path) -> list[str]:
        """The modules under `lib`, as build paths less `.olean`; its LATER files go aside."""
        bases = []
        for path in files_under(lib, (".olean", *LATER)):
            if path.endswith(".olean"):
                bases.append(path[: -len(".olean")])
            else:
                (ilean if path.endswith(".ilean") else lake).append(path)
        return bases

    if toolchain is not None:
        lib = toolchain / "lib" / "lean"
        tops = [(toolchain / "bin", "*"), (lib, "*.so*")]
        found = [p for top, glob in tops if unlinked(top) for p in top.glob(glob)]
        yield from sized(sorted(map(str, found)))

        def rank(base: str) -> tuple[int, str]:
            top = os.path.relpath(base, lib).split(os.sep)[0]
            first = TOOLCHAIN_FIRST.index(top) if top in TOOLCHAIN_FIRST else len(TOOLCHAIN_FIRST)
            return first, base

        yield from sized(base + ext for base in sorted(split(lib), key=rank) for ext in IMPORT)
    modules: dict[str, tuple[Path, str]] = {}  # module -> (package, build path less `.olean`)
    for package in sorted(p for p in packages.iterdir() if p.is_dir()):
        build = package / ".lake" / "build" / "lib" / "lean"
        for base in split(build):
            modules.setdefault(os.path.relpath(base, build).replace(os.sep, "."), (package, base))
    order = list(closure(FIRST_CLOSURE, modules))
    order += sorted(set(modules) - set(order), key=lambda m: modules[m][1])
    yield from sized(modules[m][1] + ext for m in order for ext in IMPORT)
    yield from sized(ilean)
    yield from sized(lake)


def touched(st: os.stat_result) -> float:
    """When the file was last read or written, as far as its times say."""
    return max(st.st_atime, st.st_mtime)


def ago(seconds: float) -> str:
    hours = round(max(seconds, 0.0) / 3600)
    n, unit = (hours, "hour") if hours < 48 else (round(hours / 24), "day")
    return f"{n} {unit}" + ("" if n == 1 else "s")


def oscar_since(now: float) -> tuple[float | None, str]:
    """The moment after which a file read or written counts as used, if Oscar was used in the
    last OSCAR_DAYS days, else None; and what was found, for the journal line (the rule is the
    module docstring's)."""
    try:
        boot = os.stat(FIRST_BOOT).st_mtime
    except OSError:
        return None, f"Oscar not read: no {FIRST_BOOT}, so a use can't be told from the first boot"
    since = max(boot, now - OSCAR_DAYS * 86400)
    compiled = DEPOT / "compiled"
    ok = unlinked(compiled) and compiled.is_dir()
    caches = [
        compiled / version / "Oscar"
        for version in (sorted(os.listdir(compiled)) if ok else [])
        if version.startswith("v")
    ]
    last = None
    for cache in caches:
        if not unlinked(cache) or not cache.is_dir():  # nor `compiled/v*` a link (#479 review)
            continue
        for path in files_under(cache, ("",)):
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                last = touched(st) if last is None else max(last, touched(st))
    if last is None:
        return None, f"Oscar not read: no cache under {compiled}"
    if last <= since:
        return None, f"Oscar not read: not used in the last {OSCAR_DAYS} days"
    return since, f"Oscar, used {ago(now - last)} ago"


def installed(link: Path) -> Path | None:
    """What the program `link` runs: itself if it's a regular file, else the link's target, read
    and never followed (`agent` owns it); None for anything else, or a directory reached through a
    link."""
    if not unlinked(link.parent):
        return None
    try:
        st = os.lstat(link)
        if stat.S_ISREG(st.st_mode):
            return link
        target = os.readlink(link) if stat.S_ISLNK(st.st_mode) else None
    except OSError:
        return None
    return Path(os.path.normpath(link.parent / target)) if target else None


def clis() -> tuple[list[tuple[str, int]], str]:
    """The agents' CLIs, with their sizes, and what they are, for the journal line (#483): the
    Claude Code version that `claude` runs, then the whole Codex package that `codex` runs from
    (`codex-code-mode-host`, `rg` and `bwrap` with it). Only there, or the programs themselves."""
    files: list[tuple[str, int]] = []
    names: list[str] = []

    def add(paths: Iterable[str], name: str) -> None:
        found = list(sized(paths))
        files.extend(found)
        names.extend([name] if found else [])

    claude = installed(AGENT_BIN / "claude")
    if claude is not None and claude.parent == AGENT_BIN:
        add([str(claude)], "claude")
    elif claude is not None and claude.parent == CLAUDE_VERSIONS and unlinked(CLAUDE_VERSIONS):
        add([str(claude)], f"claude {claude.name}")
    codex = installed(AGENT_BIN / "codex")
    if codex is not None and codex.parent == AGENT_BIN:
        add([str(codex)], "codex")
    elif codex is not None and codex.parts[-2:] == ("bin", "codex"):
        package = codex.parent.parent  # `codex-<version>`, read whole, never through a link
        if package.parent == CODEX_LIB and package.name.startswith("codex-"):
            add(files_under(package, ("",)), package.name)
    if not names:
        return files, f"no agent CLI under {AGENT_BIN}"
    return files, "the CLIs " + " and ".join(names)


def julia_root() -> Path | None:
    """`/opt/julia-<version>`, the install that `/usr/local/bin/julia` runs; None without one."""
    real = Path(os.path.realpath(JULIA))
    root = real.parent.parent
    if real.name != "julia" or real.parent.name != "bin" or root == Path("/"):
        return None
    return root if real.is_file() else None


def recent(tops: Iterable[Path], since: float) -> Iterator[tuple[str, int]]:
    """The regular files under `tops` read or written after `since`, with their sizes, the
    least recent first; a symlink is never followed, and a top reached through one is skipped
    (`files_under`). Lazy: the walk starts once Lean's files are all handed out."""
    found = []
    for top in tops:
        for path in files_under(top, ("",)):
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and touched(st) > since:
                found.append((touched(st), path, st.st_size))
    found.sort()
    yield from ((path, size) for _, path, size in found)


class Tally:
    """Counts the files and bytes of `items` that a Budget took."""

    def __init__(self, items: Iterable[tuple[str, int]]) -> None:
        self.items, self.files, self.bytes = items, 0, 0

    def __iter__(self) -> Iterator[tuple[str, int]]:
        for path, size in self.items:
            yield path, size
            self.files, self.bytes = self.files + 1, self.bytes + size  # taken, not cut


class Budget:
    """Passes files on until their sizes would pass `limit` bytes, and counts them."""

    def __init__(self, limit: int) -> None:
        self.limit, self.files, self.bytes, self.cut = limit, 0, 0, False

    def take(self, items: Iterable[tuple[str, int]]) -> Iterator[str]:
        for path, size in items:
            if self.bytes + size > self.limit:
                self.cut = True
                return
            self.files, self.bytes = self.files + 1, self.bytes + size
            yield path


_local = threading.local()


def read(path: str) -> int:
    """Read `path` through once, into the page cache; its bytes (0 if it isn't a regular file
    any more, or can't be read: a symlink or a FIFO swapped in is never followed or waited on)."""
    buf = getattr(_local, "buf", None)
    if buf is None:  # one buffer per thread, never one per file
        buf = _local.buf = bytearray(CHUNK)
    fd = regular(path)
    if fd is None:
        return 0
    total = 0
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        while n := os.readv(fd, [buf]):
            total += n
    except OSError:
        pass
    finally:
        os.close(fd)
    return total


def available() -> int:
    """MemAvailable in bytes; 0, which reads nothing, when unreadable."""
    try:
        found = re.search(r"^MemAvailable:\s+(\d+) kB", Path("/proc/meminfo").read_text(), re.M)
    except OSError:
        return 0
    return int(found[1]) * 1024 if found else 0


def read_cap(cgroup_root: Path = Path("/sys/fs/cgroup")) -> str:
    """This process's cgroup read limits (`io.max`), such as `259:0 rbps=80000000 riops=1500`."""
    try:
        cgroup = Path("/proc/self/cgroup").read_text().strip().rsplit(":", 1)[-1]
        lines = (cgroup_root / cgroup.lstrip("/") / "io.max").read_text().splitlines()
    except OSError:
        return "none (no io.max)"
    limits = []
    for device, *pairs in (line.split() for line in lines if line.strip()):
        reads = [p for p in pairs if p.startswith(("rbps=", "riops=")) and not p.endswith("=max")]
        limits += [" ".join([device, *reads])] if reads else []
    return ", ".join(limits) or "none"


def bridge_up() -> bool:
    """Until agent's user manager is up, systemctl says "Failed to connect to bus": not logged."""
    try:
        done = subprocess.run(BRIDGE_UP, check=False, timeout=30, stderr=subprocess.DEVNULL)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def wait_bridge(limit: float) -> str:
    """Wait until zab.service is active (READY: its catch-up dispatched), then SETTLE_S more."""
    start = time.monotonic()
    while time.monotonic() - start < limit:
        if bridge_up():
            up = time.monotonic() - start
            time.sleep(SETTLE_S)
            return f"waited {up:.0f} s for the bridge, then {SETTLE_S} s"
        time.sleep(POLL_S)
    return f"the bridge wasn't up after {limit:.0f} s"


def plan(now: float) -> tuple[list[Tally], list[tuple[str, Tally | None]]]:
    """What to read, in order (the CLIs, Lean's files, then Oscar's), and the journal line's
    account of it, part by part, with the CLIs' and Oscar's shares where they are read."""
    sources: list[Tally] = []
    what: list[tuple[str, Tally | None]] = []
    files, note = clis()
    agents = Tally(files) if files else None
    what.append((note, agents))
    sources += [agents] if agents else []
    found = shared()
    if found is None:
        what.append((f"no shared Mathlib under {LEAN}", None))
    else:
        tag, packages, toolchain = found
        missing = "" if toolchain and toolchain.is_dir() else " (missing)"
        name = toolchain.name if toolchain else "?"
        what.append((f"Mathlib {tag} and its toolchain {name}{missing}", None))
        sources.append(Tally(entries(packages, toolchain)))
    try:
        since, note = oscar_since(now)
        root = julia_root() if since is not None else None
    except OSError as exc:  # a race at boot, say: never a reason to skip Mathlib's reads
        since, note = None, f"Oscar not read: {type(exc).__name__}: {exc}"[:200]
    oscar = None
    if since is not None:
        oscar = Tally(recent([*([root] if root else []), DEPOT], since))
        sources.append(oscar)
    what.append((note, oscar))
    return sources, what


def fallbacks() -> str:
    return f"; {atime_fallbacks} files read without O_NOATIME" if atime_fallbacks else ""


def describe(what: list[tuple[str, Tally | None]]) -> str:
    """The journal line's account of what was read: each part, with its share once the budget
    has taken it."""
    return "; ".join(
        note if part is None else f"{note} ({part.bytes / 1e9:.2f} GB in {part.files} files)"
        for note, part in what
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="warm_cache.py", description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="count the files and bytes it would read")
    p.add_argument("--wait-bridge", type=float, default=120.0, metavar="SECONDS")
    args = p.parse_args(argv)
    sources, what = plan(time.time())
    if not sources:
        print(f"zab-warm-cache: nothing to read: {describe(what)}", flush=True)
        return 0
    if args.dry_run:
        budget = Budget(available() // 2)
        for _ in budget.take(itertools.chain(*sources)):
            pass
        cut = " (half the available memory)" if budget.cut else ""
        print(f"zab-warm-cache: would read {budget.bytes / 1e9:.2f} GB{cut} in {budget.files} "
              f"files: {describe(what)}{fallbacks()}")  # fmt: skip
        return 0
    waited = wait_bridge(args.wait_bridge)
    budget = Budget(available() // 2)  # once the bridge and its agents have taken theirs
    start = time.monotonic()
    with ThreadPoolExecutor(THREADS) as pool:
        total = sum(pool.map(read, budget.take(itertools.chain(*sources))))
    took = time.monotonic() - start
    cut = "; stopped at half the available memory" if budget.cut else ""
    print(
        f"zab-warm-cache: read {total / 1e9:.2f} GB in {budget.files} files in {took:.1f} s "
        f"({total / 1e6 / max(took, 0.001):.0f} MB/s): {describe(what)}{cut}; {waited}; "
        f"done {time.clock_gettime(time.CLOCK_BOOTTIME):.0f} s after boot; "
        f"read cap {read_cap()}{fallbacks()}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
