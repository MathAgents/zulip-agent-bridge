"""`workshop/admin/warm_cache.py` (issue #461): the shared Mathlib read into the page cache after
each boot, on a fake `/opt/lean` and elan directory; then Oscar's files, where it was used
(#475), on a fake Julia install and depot; before both, the agents' CLIs (#483), on a fake
`~agent/.local`."""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import pytest
import warm_cache as wc

TAG = "v4.34.1"
TOOLCHAIN = "leanprover--lean4---v4.34.1"
HEADER = """/-
Copyright (c) 2026 Someone. All rights reserved.
Released under Apache 2.0 license as described in the file LICENSE.
import Not.This
-/
module

public import {a} -- a comment, then
/- a /- nested -/ comment: import Not.This.Either -/
public meta import {b}
import all {c}
import {d}

/-!
# A module doc, after which nothing is a header

import Not.Here
-/

namespace X
import Not.There
"""


#: Oscar's paths, nowhere unless a test gives them (#475): no first-boot marker, no depot
NOWHERE = pathlib.Path("/nonexistent-zab-test")
NO_OSCAR = f"Oscar not read: no {NOWHERE}/zab-warm.done, so a use can't be told from the first boot"
#: nor any agent CLI unless a test gives them (#483)
NO_CLIS = f"no agent CLI under {NOWHERE}/bin"


@pytest.fixture(autouse=True)
def _no_oscar(monkeypatch):
    monkeypatch.setattr(wc, "FIRST_BOOT", NOWHERE / "zab-warm.done")
    monkeypatch.setattr(wc, "DEPOT", NOWHERE / ".julia")
    monkeypatch.setattr(wc, "JULIA", NOWHERE / "julia")
    monkeypatch.setattr(wc, "AGENT_BIN", NOWHERE / "bin")


def touch(path: pathlib.Path, size: int = 1) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def module(lib: pathlib.Path, name: str, size: int = 10) -> None:
    """A built module: what `import` opens (three .olean files, .ir and .ir.sig), its .ilean,
    Lake's trace and hashes, and its C file."""
    base = lib / name.replace(".", "/")
    for ext in (*wc.IMPORT, ".ilean", ".trace", ".olean.hash", ".ir.hash", ".c"):
        touch(base.with_name(base.name + ext), size)


@pytest.fixture
def lean(tmp_path, monkeypatch):
    """A shared copy as lean-project installs it, with an older one beside it, and its
    toolchain: Mathlib.Tactic imports A, which imports Batteries.B; Mathlib imports both."""
    root, elan = tmp_path / "opt-lean", tmp_path / "toolchains"
    for tag in ("v4.9.0", TAG):
        touch(root / "template" / tag / "lake-manifest.json")
        (root / "template" / tag / "lean-toolchain").write_text(f"leanprover/lean4:{tag}\n")
        (root / "packages" / tag).mkdir(parents=True)
    packages = root / "packages" / TAG
    tc = elan / TOOLCHAIN
    touch(tc / "bin" / "lean")
    touch(tc / "lib" / "lean" / "libleanshared.so", 5)
    touch(tc / "lib" / "libLLVM.so.22.1")  # not lean's: never read
    for name in ("Lake.Build", "Init.Core", "Lean.Elab", "Std.Data", "Init"):
        module(tc / "lib" / "lean", name)
    mathlib = packages / "mathlib"
    lib = mathlib / ".lake" / "build" / "lib" / "lean"
    sources = {
        "Mathlib.Tactic": "import Mathlib.A\nimport Lean.Elab\n",
        "Mathlib.A": "module\n\npublic import Batteries.B\n",
        "Mathlib": "import Mathlib.Tactic\nimport Mathlib.Z\n",
        "Mathlib.Z": "",
    }
    for name, text in sources.items():
        module(lib, name)
        touch(mathlib / (name.replace(".", "/") + ".lean")).write_text(text)
    batteries = packages / "batteries"
    module(batteries / ".lake" / "build" / "lib" / "lean", "Batteries.B")
    touch(batteries / "Batteries" / "B.lean").write_text("")
    touch(mathlib / ".git" / "objects" / "pack.olean", 99)  # outside the build: never read
    monkeypatch.setattr(wc, "LEAN", root)
    monkeypatch.setattr(wc, "ELAN", elan)
    return packages, tc


def short(path: str) -> str:
    """`Mathlib/A.olean` and `toolchain:lib/lean/Init.olean`, for readable expectations."""
    if f"/{TOOLCHAIN}/" in path:
        return "toolchain:" + path.split(f"/{TOOLCHAIN}/", 1)[1]
    return path.split("/lib/lean/", 1)[1]


def test_header_imports_skip_comments_and_stop_at_the_first_command():
    text = HEADER.format(a="Mathlib.A", b="Mathlib.B", c="Mathlib.C", d="Batteries.D")
    assert wc.header_imports(text) == ["Mathlib.A", "Mathlib.B", "Mathlib.C", "Batteries.D"]
    assert wc.header_imports("prelude\nimport Init.Core\n\ndef x := 1\nimport Y\n") == ["Init.Core"]
    assert wc.header_imports("") == [] and wc.header_imports("/- unclosed") == []
    assert wc.header_imports("-- only a comment") == []


def test_the_newest_shared_copy_and_its_toolchain(lean):
    packages, tc = lean
    assert wc.shared() == (TAG, packages, tc)
    (wc.LEAN / "template" / TAG / "lean-toolchain").unlink()
    assert wc.shared() == (TAG, packages, None)
    (wc.LEAN / "template" / TAG / "lean-toolchain").write_text("..\n")
    assert wc.shared() == (TAG, packages, None)  # never a path outside elan's directory


def test_no_shared_copy_reads_nothing_and_never_waits(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wc, "LEAN", tmp_path / "none")
    monkeypatch.setattr(wc, "bridge_up", lambda: pytest.fail("waited for the bridge"))
    assert wc.main([]) == 0 and wc.main(["--dry-run"]) == 0
    assert (
        capsys.readouterr().out
        == (
            f"zab-warm-cache: nothing to read: {NO_CLIS}; no shared Mathlib under "
            f"{tmp_path / 'none'}; {NO_OSCAR}\n"
        )
        * 2
    )
    touch(tmp_path / "none" / "template" / TAG / "lake-manifest.json")  # no packages
    assert wc.shared() is None


def test_the_order_toolchain_then_the_tactic_closure_then_the_rest(lean):
    packages, tc = lean
    order = [short(p) for p, _ in wc.entries(packages, tc)]
    # each module's import files together, its .olean files first, then .ir and .ir.sig, which
    # an import opens too (live round 1 on #464: 27 s of an import after the warm-up)
    olean = lambda name: [f"{name}{ext}" for ext in wc.IMPORT]  # noqa: E731
    expected = [
        "toolchain:bin/lean",
        "toolchain:lib/lean/libleanshared.so",
        # the toolchain's modules: Init, Std and Lean before the others (Lake)
        *(f"toolchain:lib/lean/{p}" for p in olean("Init")),
        *(f"toolchain:lib/lean/{p}" for p in olean("Init/Core")),
        *(f"toolchain:lib/lean/{p}" for p in olean("Std/Data")),
        *(f"toolchain:lib/lean/{p}" for p in olean("Lean/Elab")),
        *(f"toolchain:lib/lean/{p}" for p in olean("Lake/Build")),
        # Mathlib.Tactic's closure, each module after its imports; then the rest, by path
        *olean("Batteries/B"),
        *olean("Mathlib/A"),
        *olean("Mathlib/Tactic"),
        *olean("Mathlib"),
        *olean("Mathlib/Z"),
    ]
    assert order[: len(expected)] == expected
    later = order[len(expected) :]
    # then, which no import opens: every .ilean (the language server's), then Lake's traces
    # and hashes; nothing else
    ileans = [p for p in later if p.endswith(".ilean")]
    assert later[: len(ileans)] == ileans and len(ileans) == 5 + 5
    assert {p.rsplit(".", 1)[1] for p in later[len(ileans) :]} == {"trace", "hash"}
    assert len(later) == 10 * 4  # each module's .ilean, .trace and two hashes
    assert not [p for p in order if p.endswith((".c", "libLLVM.so.22.1"))]
    assert not [p for p in order if "/.git/" in p]


def test_an_unknown_toolchain_still_reads_the_packages(lean):
    packages, _ = lean
    order = [short(p) for p, _ in wc.entries(packages, None)]
    assert order[0] == "Batteries/B.olean" and not any("toolchain:" in p for p in order)


def test_symlinks_and_special_files_are_never_read(lean, tmp_path):
    packages, tc = lean
    lib = packages / "mathlib" / ".lake" / "build" / "lib" / "lean"
    secret = touch(tmp_path / "secret", 7)
    (lib / "Mathlib" / "Link.olean").symlink_to(secret)
    os.mkfifo(lib / "Mathlib" / "Fifo.olean")
    (lib / "Linked").symlink_to(tmp_path, target_is_directory=True)  # not followed by the walk
    paths = [p for p, _ in wc.entries(packages, tc)]
    assert not [p for p in paths if "Link" in p or "Fifo" in p]
    assert wc.read(str(lib / "Mathlib" / "Link.olean")) == 0  # O_NOFOLLOW
    done: list[int] = []
    reader = threading.Thread(target=lambda: done.append(wc.read(str(lib / "Mathlib/Fifo.olean"))))
    reader.start()
    reader.join(5)
    assert done == [0], "a FIFO must not block the warm-up"
    assert wc.read(str(secret)) == 7


def listed(packages, tc) -> list[str]:
    """`entries()` in a thread: a test fails, never hangs, if it blocks."""
    out: list[list[str]] = []
    lister = threading.Thread(
        target=lambda: out.append([short(p) for p, _ in wc.entries(packages, tc)]), daemon=True
    )
    lister.start()
    lister.join(10)
    assert out, "entries() blocked"
    return out[0]


@pytest.mark.parametrize("kind", ["fifo", "dev-zero", "huge"])
def test_a_source_is_read_as_read_reads_files_and_bounded(lean, kind):
    """Review round 1 (#464): the headers were read with `read_text()`, as root, which waited
    on a FIFO forever and read a symlink to /dev/zero until memory ran out. A FIFO or a
    symlink makes the module a leaf; a huge file is read for its first MiB only."""
    packages, tc = lean
    tactic = packages / "mathlib" / "Mathlib" / "Tactic.lean"
    tactic.unlink()
    if kind == "fifo":
        os.mkfifo(tactic)
    elif kind == "dev-zero":
        tactic.symlink_to("/dev/zero")
    else:
        with open(tactic, "wb") as f:  # sparse: 16 GiB, of which only the header is written
            f.write(b"import Mathlib.A\n")
            f.truncate(1 << 34)
    assert len(wc.head(str(tactic))) == (wc.HEADER_MAX if kind == "huge" else 0)
    first = next(p for p in listed(packages, tc) if not p.startswith("toolchain:"))
    # a leaf comes first, its imports among the rest; the huge file's header still counts
    assert first == ("Batteries/B.olean" if kind == "huge" else "Mathlib/Tactic.olean")


def test_the_toolchain_name_is_read_bounded_too(lean):
    packages, _ = lean
    toolchain = wc.LEAN / "template" / TAG / "lean-toolchain"
    toolchain.unlink()
    os.mkfifo(toolchain)
    assert wc.shared() == (TAG, packages, None)


def test_the_budget_stops_before_a_file_that_would_pass_it():
    budget = wc.Budget(25)
    taken = list(budget.take([("a", 10), ("b", 10), ("c", 10), ("d", 1)]))
    assert taken == ["a", "b"] and (budget.files, budget.bytes, budget.cut) == (2, 20, True)
    budget = wc.Budget(100)
    assert list(budget.take([("a", 10)])) == ["a"] and not budget.cut


def test_a_run_waits_for_the_bridge_reads_everything_and_says_so(lean, monkeypatch, capsys):
    packages, tc = lean
    asked: list[int] = []
    monkeypatch.setattr(wc, "bridge_up", lambda: asked.append(1) or len(asked) >= 3)
    monkeypatch.setattr(wc, "POLL_S", 0)
    monkeypatch.setattr(wc, "SETTLE_S", 0)
    monkeypatch.setattr(wc, "read_cap", lambda: "259:0 rbps=80000000 riops=1500")
    total = sum(size for _, size in wc.entries(packages, tc))
    count = len(list(wc.entries(packages, tc)))
    assert wc.main([]) == 0
    out = capsys.readouterr().out
    assert len(asked) == 3
    assert out.startswith(f"zab-warm-cache: read {total / 1e9:.2f} GB in {count} files in ")
    what = f": {NO_CLIS}; Mathlib {TAG} and its toolchain {TOOLCHAIN}; {NO_OSCAR}"
    assert f"{what}; waited 0 s for the bridge, " in out
    assert out.rstrip().endswith("s after boot; read cap 259:0 rbps=80000000 riops=1500")
    assert wc.main(["--dry-run"]) == 0
    assert capsys.readouterr().out == (
        f"zab-warm-cache: would read {total / 1e9:.2f} GB in {count} files{what}\n"
    )


def test_a_small_vm_reads_at_most_half_its_available_memory(lean, monkeypatch, capsys):
    monkeypatch.setattr(wc, "available", lambda: 100)
    monkeypatch.setattr(wc, "bridge_up", lambda: True)
    monkeypatch.setattr(wc, "SETTLE_S", 0)
    assert wc.main([]) == 0
    out = capsys.readouterr().out
    # 50 bytes: the toolchain's two files (1 + 5), then four modules' files of 10, not a fifth
    assert "read 0.00 GB in 6 files in" in out and "; stopped at half the available memory" in out
    assert wc.main(["--dry-run"]) == 0
    assert "would read 0.00 GB (half the available memory) in 6 files" in capsys.readouterr().out


def test_a_bridge_that_never_comes_up_delays_the_reads_by_the_limit_only(lean, monkeypatch, capsys):
    monkeypatch.setattr(wc, "bridge_up", lambda: False)
    monkeypatch.setattr(wc, "POLL_S", 0)
    assert wc.wait_bridge(0.05) == "the bridge wasn't up after 0 s"
    assert wc.main(["--wait-bridge", "0"]) == 0
    assert "; the bridge wasn't up after 0 s; done " in capsys.readouterr().out


def test_bridge_up_asks_the_agents_user_manager(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3)

    monkeypatch.setattr(wc.subprocess, "run", run)
    assert not wc.bridge_up()
    assert calls == [
        ["systemctl", "--user", "--machine=agent@", "is-active", "--quiet", "zab.service"]
    ]

    def hangs(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)

    monkeypatch.setattr(wc.subprocess, "run", hangs)
    assert not wc.bridge_up()


def test_the_read_cap_comes_from_the_cgroups_io_max(tmp_path):
    cgroup = pathlib.Path("/proc/self/cgroup").read_text().strip().rsplit(":", 1)[-1]
    io_max = tmp_path / cgroup.lstrip("/") / "io.max"
    assert wc.read_cap(tmp_path) == "none (no io.max)"
    io_max.parent.mkdir(parents=True)
    io_max.write_text("")
    assert wc.read_cap(tmp_path) == "none"
    io_max.write_text(
        "259:0 rbps=80000000 wbps=max riops=1500 wiops=max\n8:0 rbps=max wbps=1 riops=max\n"
    )
    assert wc.read_cap(tmp_path) == "259:0 rbps=80000000 riops=1500"


def test_the_script_runs_as_the_vm_runs_it(lean):
    """Standard library only, by path, with the system Python's isolation (`-I`): the dry run
    on the fake tree goes through the real entry point."""
    script = pathlib.Path(wc.__file__)
    code = (
        "import runpy, sys, pathlib;"
        f"main = runpy.run_path({str(script)!r}, run_name='not_main')['main'];"
        f"main.__globals__.update(LEAN=pathlib.Path({str(wc.LEAN)!r}), "
        f"ELAN=pathlib.Path({str(wc.ELAN)!r}), FIRST_BOOT=pathlib.Path({str(wc.FIRST_BOOT)!r}));"
        "sys.exit(main(['--dry-run']))"
    )
    argv = [sys.executable, "-I", "-c", code]
    done = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("zab-warm-cache: would read ")


def test_each_thread_reads_into_one_buffer(tmp_path):
    """A buffer per file would zero 1 MiB for each of about 96,000 files."""
    path = str(touch(tmp_path / "f", 3))
    assert wc.read(path) == 3
    first = wc._local.buf
    assert wc.read(path) == 3 and wc._local.buf is first and len(first) == wc.CHUNK


# ----------------------------------------------------------------------- Oscar, when used (#475)

DAY = 86400.0
#: The order `using Oscar` opened these (the trace on the development machine): libraries, caches,
#: sources
ONE_SESSION = [
    "lib/julia/libLLVM.so.18.1jl",
    "lib/julia/sys.so",
    "compiled/v1.12/Oscar/aJ3Pg_oa595.ji",
    "compiled/v1.12/Oscar/aJ3Pg_oa595.so",
    "artifacts/e85c/lib/libpolymake_julia.so",
    "packages/Oscar/Ab1/src/Oscar.jl",
]
ONE_SESSION_BYTES = 20 + 30 + 9 + 90 + 60 + 3


@dataclass
class Julia:
    now: float
    root: pathlib.Path
    depot: pathlib.Path
    marker: pathlib.Path

    def use(self, rel: str, when: float, *, write: bool = False) -> None:
        """A read by Julia sets the access time; Julia's touch of a cache, the modification
        time."""
        path = (self.root if rel.startswith(("bin/", "lib/", "share/")) else self.depot) / rel
        st = path.stat()
        os.utime(path, (st.st_atime, when) if write else (when, st.st_mtime))


@pytest.fixture
def julia(tmp_path, monkeypatch) -> Julia:
    """Julia's install behind its link and a depot, as the image has them, every file last read
    by the first boot's volume warm-up, which ended (its marker) ten days ago."""
    now = time.time()
    boot = now - 10 * DAY
    root, depot = tmp_path / "opt" / "julia-1.12.7", tmp_path / "depot"
    files = {
        root / "bin" / "julia": 2,
        root / "lib" / "julia" / "sys.so": 30,
        root / "lib" / "julia" / "libLLVM.so.18.1jl": 20,
        root / "share" / "doc" / "julia" / "index.html": 50,
        depot / "compiled" / "v1.12" / "Oscar" / "aJ3Pg_oa595.ji": 9,
        depot / "compiled" / "v1.12" / "Oscar" / "aJ3Pg_oa595.so": 90,
        depot / "compiled" / "v1.12" / "Hecke" / "lhBAK_oa595.so": 70,
        depot / "artifacts" / "e85c" / "lib" / "libpolymake_julia.so": 60,
        depot / "artifacts" / "e85c" / "share" / "doc.pdf": 80,
        depot / "packages" / "Oscar" / "Ab1" / "src" / "Oscar.jl": 3,
    }
    for path, size in files.items():
        touch(path, size)
        os.utime(path, (boot - 60, boot - 30 * DAY))
    link = tmp_path / "usr-local-bin" / "julia"
    link.parent.mkdir()
    link.symlink_to(root / "bin" / "julia")
    marker = touch(tmp_path / "zab-warm.done")
    os.utime(marker, (boot, boot))
    monkeypatch.setattr(wc, "JULIA", link)
    monkeypatch.setattr(wc, "DEPOT", depot)
    monkeypatch.setattr(wc, "FIRST_BOOT", marker)
    return Julia(now, root, depot, marker)


def name(path: str) -> str:
    return path.split("/julia-1.12.7/", 1)[-1].split("/depot/", 1)[-1]


def test_oscar_unused_since_the_first_boot_is_not_read(julia):
    assert wc.oscar_since(julia.now) == (None, "Oscar not read: not used in the last 7 days")
    julia.use("compiled/v1.12/Oscar/aJ3Pg_oa595.ji", julia.now - 8 * DAY)  # eight days ago
    assert wc.oscar_since(julia.now)[0] is None
    julia.use("compiled/v1.12/Hecke/lhBAK_oa595.so", julia.now - DAY)  # Hecke alone isn't Oscar
    assert wc.oscar_since(julia.now)[0] is None


def test_the_first_boots_own_reads_are_not_a_use(julia):
    """Ten days ago is outside the week anyway; with a first boot two days ago, its warm-up's
    reads (just before its marker) are inside it, and still don't count."""
    boot = julia.now - 2 * DAY
    os.utime(julia.marker, (boot, boot))
    julia.use("compiled/v1.12/Oscar/aJ3Pg_oa595.ji", boot - 60)
    assert wc.oscar_since(julia.now)[0] is None
    julia.use("compiled/v1.12/Oscar/aJ3Pg_oa595.ji", boot + 3600)  # an hour after it
    assert wc.oscar_since(julia.now) == (boot, "Oscar, used 47 hours ago")
    julia.marker.unlink()  # without the marker, a use can't be told from the first boot's reads
    assert wc.oscar_since(julia.now) == (
        None,
        f"Oscar not read: no {julia.marker}, so a use can't be told from the first boot",
    )


def test_a_use_reads_what_it_opened_in_that_order_and_nothing_else(julia):
    for n, rel in enumerate(ONE_SESSION):
        julia.use(rel, julia.now - 2 * DAY + n)
    since, note = wc.oscar_since(julia.now)
    assert since == julia.now - 7 * DAY and note == "Oscar, used 2 days ago"
    order = [name(p) for p, _ in wc.recent([wc.julia_root(), wc.DEPOT], since)]
    assert order == ONE_SESSION  # not the docs, Hecke or the first boot's other reads
    assert (wc.ago(47 * 3600), wc.ago(3 * DAY), wc.ago(-5)) == ("47 hours", "3 days", "0 hours")
    assert (wc.ago(3600), wc.ago(1.2 * DAY), wc.ago(36 * DAY)) == ("1 hour", "29 hours", "36 days")


def test_julias_touch_of_the_cache_counts_under_noatime(julia):
    """Julia 1.12 touches the requested package's cache file at most loads (`loading.jl`): a
    modification time shows a use where access times don't change."""
    julia.use("compiled/v1.12/Oscar/aJ3Pg_oa595.ji", julia.now - DAY, write=True)
    since, _ = wc.oscar_since(julia.now)
    assert since == julia.now - 7 * DAY
    found = [name(p) for p, _ in wc.recent([wc.DEPOT], since)]
    assert found == ["compiled/v1.12/Oscar/aJ3Pg_oa595.ji"]


def test_links_and_special_files_in_the_depot_are_never_read(julia, tmp_path):
    julia.use("compiled/v1.12/Oscar/aJ3Pg_oa595.ji", julia.now - DAY)
    secret = touch(tmp_path / "secret", 7)
    lib = julia.depot / "artifacts" / "e85c" / "lib"
    (lib / "link.so").symlink_to(secret)
    os.mkfifo(lib / "fifo.so")
    (julia.depot / "artifacts" / "linked").symlink_to(tmp_path, target_is_directory=True)
    for path in (secret, lib / "fifo.so", lib / "link.so"):
        os.utime(path, (julia.now - 60, julia.now - 60), follow_symlinks=False)
    since, _ = wc.oscar_since(julia.now)
    found = [p for p, _ in wc.recent([julia.depot], since)]
    assert found == [str(julia.depot / "compiled/v1.12/Oscar/aJ3Pg_oa595.ji")]


def test_an_oscar_cache_that_is_a_link_is_no_use(julia, tmp_path):
    cache = julia.depot / "compiled" / "v1.12" / "Oscar"
    elsewhere = tmp_path / "elsewhere"
    cache.rename(elsewhere)
    cache.symlink_to(elsewhere, target_is_directory=True)
    os.utime(elsewhere / "aJ3Pg_oa595.ji", (julia.now - DAY, julia.now - DAY))
    compiled = julia.depot / "compiled"
    assert wc.oscar_since(julia.now) == (None, f"Oscar not read: no cache under {compiled}")


def test_julias_install_is_the_links_target(julia, tmp_path, monkeypatch):
    assert wc.julia_root() == julia.root
    monkeypatch.setattr(wc, "JULIA", tmp_path / "none")
    assert wc.julia_root() is None
    monkeypatch.setattr(wc, "JULIA", touch(tmp_path / "odd"))
    assert wc.julia_root() is None


def test_a_run_reads_lean_then_oscar_within_one_budget(lean, julia, monkeypatch, capsys):
    packages, tc = lean
    monkeypatch.setattr(wc, "bridge_up", lambda: True)
    monkeypatch.setattr(wc, "SETTLE_S", 0)
    monkeypatch.setattr(wc, "THREADS", 1)  # the reads in the order they are handed out
    for n, rel in enumerate(ONE_SESSION):
        julia.use(rel, julia.now - DAY + n)
    lean_files = list(wc.entries(packages, tc))
    lean_bytes = sum(size for _, size in lean_files)
    order: list[str] = []
    monkeypatch.setattr(wc, "read", lambda path: order.append(path) or 1)
    assert wc.main([]) == 0
    out = capsys.readouterr().out
    assert order[: len(lean_files)] == [p for p, _ in lean_files]
    assert [name(p) for p in order[len(lean_files) :]] == ONE_SESSION
    assert "; Oscar, used 24 hours ago (0.00 GB in 6 files); waited 0 s for the bridge" in out
    total = lean_bytes + ONE_SESSION_BYTES
    assert wc.main(["--dry-run"]) == 0
    assert capsys.readouterr().out == (
        f"zab-warm-cache: would read {total / 1e9:.2f} GB in {len(order)} files: {NO_CLIS}; "
        f"Mathlib {TAG} and its toolchain {TOOLCHAIN}; Oscar, used 24 hours ago (0.00 GB in 6 "
        "files)\n"
    )
    # a small VM: Lean's files first, then Oscar's while they fit in half the available memory
    monkeypatch.setattr(wc, "available", lambda: 2 * (lean_bytes + 20 + 30))
    assert wc.main(["--dry-run"]) == 0
    assert capsys.readouterr().out == (
        f"zab-warm-cache: would read {(lean_bytes + 50) / 1e9:.2f} GB (half the available "
        f"memory) in {len(lean_files) + 2} files: {NO_CLIS}; Mathlib {TAG} and its toolchain "
        f"{TOOLCHAIN}; Oscar, used 24 hours ago (0.00 GB in 2 files)\n"
    )


def test_oscar_alone_is_read_without_a_shared_mathlib(julia, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wc, "LEAN", tmp_path / "no-lean")
    julia.use("compiled/v1.12/Oscar/aJ3Pg_oa595.ji", julia.now - DAY)
    assert wc.main(["--dry-run"]) == 0
    assert capsys.readouterr().out == (
        f"zab-warm-cache: would read 0.00 GB in 1 files: {NO_CLIS}; no shared Mathlib under "
        f"{tmp_path / 'no-lean'}; Oscar, used 24 hours ago (0.00 GB in 1 files)\n"
    )


def test_reads_leave_access_times_alone(tmp_path, monkeypatch):
    """O_NOATIME, so that the warm-up never makes Oscar look used; a file the kernel refuses it
    on (another user's, without CAP_FOWNER) is still read."""
    path = str(touch(tmp_path / "f", 3))
    flags: list[int] = []
    real = os.open

    def spy(p, f, *args):
        flags.append(f)
        if f & wc.NOATIME and p.endswith("other"):
            raise PermissionError(1, "Operation not permitted")
        return real(p, f, *args)

    monkeypatch.setattr(wc.os, "open", spy)
    assert wc.read(path) == 3 and wc.head(path) == "xxx"
    assert wc.NOATIME and all(f & wc.NOATIME for f in flags)
    other = str(touch(tmp_path / "other", 2))
    flags.clear()
    assert wc.read(other) == 2
    assert [bool(f & wc.NOATIME) for f in flags] == [True, False]


# ------------------------------------------------------------------ review round 1 on #479


@pytest.fixture(autouse=True)
def _no_fallbacks_yet(monkeypatch):
    monkeypatch.setattr(wc, "atime_fallbacks", 0)


@pytest.mark.parametrize("linked", ["depot", "compiled", "compiled/v1.12"])
def test_a_depot_reached_through_a_link_is_never_walked(julia, tmp_path, linked):
    """`agent` owns ~/.julia: with the depot, `compiled` or `compiled/v1.12` a link to a tree
    of recent files, Oscar isn't "used" and nothing there is read (`os.walk` follows a top that
    is a link, and a path through a linked directory)."""
    real = tmp_path / "elsewhere"
    target = julia.depot if linked == "depot" else julia.depot / linked
    target.rename(real)
    target.symlink_to(real, target_is_directory=True)
    for path in real.rglob("*"):
        if path.is_file():
            os.utime(path, (julia.now - 60, julia.now - 60))
    since, note = wc.oscar_since(julia.now)
    assert since is None and note == f"Oscar not read: no cache under {wc.DEPOT / 'compiled'}"
    assert list(wc.recent([julia.depot], julia.now - DAY)) == []
    assert wc.files_under(julia.depot, ("",)) == [] or linked != "depot"


def test_lean_trees_reached_through_a_link_are_never_walked(lean, tmp_path):
    """The same under ~/.elan, which `agent` owns too: the toolchain's `bin` and `lib/lean`."""
    packages, tc = lean
    for name in ("bin", "lib"):
        (tc / name).rename(tmp_path / f"real-{name}")
        (tc / name).symlink_to(tmp_path / f"real-{name}", target_is_directory=True)
    assert not any(p.startswith(str(tc)) for p, _ in wc.entries(packages, tc))
    mathlib = packages / "mathlib"
    mathlib.rename(tmp_path / "real-mathlib")
    mathlib.symlink_to(tmp_path / "real-mathlib", target_is_directory=True)
    assert not any("/mathlib/" in p for p, _ in wc.entries(packages, tc))


def test_oscar_planning_never_stops_mathlibs_reads(lean, monkeypatch, capsys):
    def boom(now):
        raise PermissionError(13, "Permission denied", "/home/agent/.julia/compiled")

    monkeypatch.setattr(wc, "oscar_since", boom)
    assert wc.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("zab-warm-cache: would read ") and f"Mathlib {TAG}" in out
    assert "; Oscar not read: PermissionError: [Errno 13] Permission denied" in out


def test_reads_without_o_noatime_are_counted_in_the_line(lean, monkeypatch, capsys):
    """Without CAP_FOWNER the warm-up's own reads would keep Oscar "used": the line says so."""
    real = os.open

    def no_noatime(path, flags, *args):
        if flags & wc.NOATIME:
            raise PermissionError(1, "Operation not permitted")
        return real(path, flags, *args)

    monkeypatch.setattr(wc.os, "open", no_noatime)
    monkeypatch.setattr(wc, "bridge_up", lambda: True)
    monkeypatch.setattr(wc, "SETTLE_S", 0)
    assert wc.main([]) == 0
    out = capsys.readouterr().out.rstrip()
    assert out.endswith(f"; {wc.atime_fallbacks} files read without O_NOATIME")
    assert wc.atime_fallbacks > 10


def test_a_real_read_leaves_the_access_time_alone(tmp_path):
    """On a file system that keeps access times (skipped where none moves: a `noatime` /tmp),
    a plain read moves a backdated access time and the warm-up's read doesn't."""
    for directory in (tmp_path, pathlib.Path("/dev/shm")):  # noqa: S108 - a tmpfs, if any
        if not directory.is_dir():
            continue
        path = directory / f"zab-atime-{os.getpid()}-{threading.get_ident()}"
        try:
            touch(path, 4096)
            old = time.time() - 10 * DAY
            os.utime(path, (old, old))
            path.read_bytes()  # the control
            if os.stat(path).st_atime == old:
                continue  # this file system doesn't move access times
            os.utime(path, (old, old))
            assert wc.read(str(path)) == 4096 and wc.head(str(path), 10) == "x" * 10
            assert os.stat(path).st_atime == old
            return
        finally:
            path.unlink(missing_ok=True)
    pytest.skip("no file system here moves access times")


# ------------------------------------------------------------------------- the agents' CLIs (#483)

#: The Codex package's members, as workshop/ami/build.sh extracts them, with sizes, in the order
#: of the walk (`files_under`: each directory's files before its subdirectories')
CODEX_FILES = {
    "codex-package.json": 1,
    "bin/codex": 40,
    "bin/codex-code-mode-host": 30,
    "codex-path/rg": 5,
    "codex-resources/bwrap": 2,
}


@dataclass
class Agents:
    bin: pathlib.Path
    versions: pathlib.Path
    package: pathlib.Path


@pytest.fixture
def agents(tmp_path, monkeypatch) -> Agents:
    """`~agent/.local` as the image has it: Claude Code's native install, two versions of which
    the link runs the pinned one, and the Codex package behind its link (build.sh steps 5, 6)."""
    local = tmp_path / "agent-local"
    versions, lib, bin_ = local / "share/claude/versions", local / "lib", local / "bin"
    touch(versions / "2.1.283", 24)
    touch(versions / "2.1.251", 23)  # an older version, never run: never read
    package = lib / "codex-0.157.1"
    for rel, size in CODEX_FILES.items():
        touch(package / rel, size)
    touch(lib / "codex-0.150.0" / "bin" / "codex", 9)  # nor an older package
    bin_.mkdir()
    (bin_ / "claude").symlink_to(versions / "2.1.283")  # the installer's link is absolute
    (bin_ / "codex").symlink_to("../lib/codex-0.157.1/bin/codex")  # a relative one works too
    touch(bin_ / "lean-lsp-mcp", 7)  # not an agent's CLI
    monkeypatch.setattr(wc, "AGENT_BIN", bin_)
    monkeypatch.setattr(wc, "CLAUDE_VERSIONS", versions)
    monkeypatch.setattr(wc, "CODEX_LIB", lib)
    return Agents(bin_, versions, package)


def test_the_clis_are_what_the_links_run_and_the_codex_package(agents):
    files, note = wc.clis()
    assert files == [
        (str(agents.versions / "2.1.283"), 24),
        *((str(agents.package / rel), size) for rel, size in CODEX_FILES.items()),
    ]
    assert note == "the CLIs claude 2.1.283 and codex-0.157.1"
    # programs copied into place, without links, are read as they are
    for name in ("claude", "codex"):
        (agents.bin / name).unlink()
        touch(agents.bin / name, 3)
    assert wc.clis() == ([(str(agents.bin / "claude"), 3), (str(agents.bin / "codex"), 3)],
                         "the CLIs claude and codex")  # fmt: skip
    for name in ("claude", "codex"):
        (agents.bin / name).unlink()
    assert wc.clis() == ([], f"no agent CLI under {agents.bin}")


def test_the_clis_are_read_first_then_lean_then_oscar(agents, lean, julia, monkeypatch, capsys):
    packages, tc = lean
    monkeypatch.setattr(wc, "bridge_up", lambda: True)
    monkeypatch.setattr(wc, "SETTLE_S", 0)
    monkeypatch.setattr(wc, "THREADS", 1)  # the reads in the order they are handed out
    julia.use("compiled/v1.12/Oscar/aJ3Pg_oa595.ji", julia.now - DAY)
    order: list[str] = []
    monkeypatch.setattr(wc, "read", lambda path: order.append(path) or 1)
    assert wc.main([]) == 0
    clis = [str(agents.versions / "2.1.283"), *(str(agents.package / r) for r in CODEX_FILES)]
    lean_files = [p for p, _ in wc.entries(packages, tc)]
    assert order == [*clis, *lean_files, str(julia.depot / "compiled/v1.12/Oscar/aJ3Pg_oa595.ji")]
    assert (
        ": the CLIs claude 2.1.283 and codex-0.157.1 (0.00 GB in 6 files); Mathlib "
        f"{TAG} and its toolchain {TOOLCHAIN}; Oscar, used 24 hours ago (0.00 GB in 1 files); "
    ) in capsys.readouterr().out


def test_the_clis_alone_are_read_after_the_bridge(agents, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(wc, "LEAN", tmp_path / "no-lean")
    asked: list[int] = []
    monkeypatch.setattr(wc, "bridge_up", lambda: asked.append(1) or True)
    monkeypatch.setattr(wc, "SETTLE_S", 0)
    total = 24 + sum(CODEX_FILES.values())
    assert wc.main(["--dry-run"]) == 0
    assert capsys.readouterr().out == (
        f"zab-warm-cache: would read 0.00 GB in 6 files: the CLIs claude 2.1.283 and "
        f"codex-0.157.1 (0.00 GB in 6 files); no shared Mathlib under {tmp_path / 'no-lean'}; "
        f"{NO_OSCAR}\n"
    )
    assert wc.main([]) == 0 and asked == [1]  # the answers a wake is for come first
    assert "read 0.00 GB in 6 files in " in capsys.readouterr().out
    assert sum(wc.read(p) for p, _ in wc.clis()[0]) == total


@pytest.mark.parametrize(
    "case", ["claude-elsewhere", "codex-elsewhere", "version-link", "versions-link", "bin-link"]
)
def test_links_out_of_the_installs_are_never_followed(agents, tmp_path, case):
    """`agent` owns the links and the trees, and this runs as root: a link pointing out of
    Claude's versions or Codex's packages, a version that is itself a link, or a directory on
    the way that is one, reads nothing; a link inside the package is skipped."""
    secret = touch(tmp_path / "secret" / "bin" / "codex", 99)
    if case == "claude-elsewhere":
        (agents.bin / "claude").unlink()
        (agents.bin / "claude").symlink_to(secret)
    elif case == "codex-elsewhere":  # a package-shaped path outside CODEX_LIB
        (agents.bin / "codex").unlink()
        (agents.bin / "codex").symlink_to(secret)
    elif case == "version-link":
        (agents.versions / "2.1.283").unlink()
        (agents.versions / "2.1.283").symlink_to(secret)
    elif case == "versions-link":
        real = agents.versions.with_name("real-versions")
        agents.versions.rename(real)
        agents.versions.symlink_to(real, target_is_directory=True)
    else:
        real = agents.bin.with_name("real-bin")
        agents.bin.rename(real)
        agents.bin.symlink_to(real, target_is_directory=True)
    (agents.package / "codex-path" / "rg").unlink()
    (agents.package / "codex-path" / "rg").symlink_to(secret)
    os.mkfifo(agents.package / "codex-path" / "fifo")
    files, note = wc.clis()
    paths = [p for p, _ in files]
    assert not [p for p in paths if "secret" in p or p.endswith(("/rg", "/fifo"))]
    claude, codex = str(agents.versions / "2.1.283"), str(agents.package / "bin" / "codex")
    expected = {
        "claude-elsewhere": (False, True),
        "codex-elsewhere": (True, False),
        "version-link": (False, True),
        "versions-link": (False, True),
        "bin-link": (False, False),
    }[case]
    assert (claude in paths, codex in paths) == expected
    assert note.startswith("the CLIs" if any(expected) else "no agent CLI under ")
