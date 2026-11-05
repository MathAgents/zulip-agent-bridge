"""Live tests require explicit opt-in; offline CI never opens credentials. A hung test fails by
name, with its stacks (#491)."""

import asyncio
import gc
import hashlib
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

#: The longest parameter that names itself in a test's id; a longer one is abbreviated.
ID_CHARS = 100
#: The timeout of a test marked `slow` (pytest-timeout; the ini's `timeout` is the others'):
#: twice the 1-GiB re-pack's own CPU bound of 150 s. Alone, in CI's `timing` job, that test
#: takes 33 to 69 s; beside another xdist worker, as before #492, it took up to 134 s.
SLOW_TIMEOUT = 300
#: After a test's timeout, the seconds its teardown gets before the process ends (#491).
TEARDOWN_GRACE = 30
BACKSTOP = pytest.StashKey[threading.Timer]()


def pytest_report_header() -> str:
    """The CPUs this run may use, which `-n auto` takes as its number of workers (#473)."""
    affinity = getattr(os, "sched_getaffinity", None)  # Linux only
    usable = len(affinity(0)) if affinity else os.cpu_count()
    return f"cpus: {usable} usable of {os.cpu_count()}"


def pytest_make_parametrize_id(config, val, argname):
    """A long string or bytes parameter is named by its head, its length and a digest, not in
    full: ids of up to a megabyte (a roster upload, a scrubber's input) went whole into every
    `-v` line, failure and `--durations` row, and wedged GitHub's runner on its log (#473)."""
    if not isinstance(val, str | bytes) or len(val) <= ID_CHARS:
        return None
    data = val.encode("utf-8", "surrogatepass") if isinstance(val, str) else val
    head = ascii(val[:24])[1:-1] if isinstance(val, str) else ascii(val[:24])[2:-1]
    return f"{head}...{len(val)}-{hashlib.sha256(data).hexdigest()[:8]}"


def pytest_itemcollected(item):
    """The ini's `timeout` (pytest-timeout, #491), but `SLOW_TIMEOUT` for a `slow` test, and none
    for the live suites, which wait for real VMs and agents; a test's own marker wins."""
    if not item.config.pluginmanager.hasplugin("timeout") or item.get_closest_marker("timeout"):
        return
    if any(item.get_closest_marker(m) for m in ("live", "e2e", "admin_e2e")):
        item.add_marker(pytest.mark.timeout(0))
    elif item.get_closest_marker("slow"):
        item.add_marker(pytest.mark.timeout(SLOW_TIMEOUT))


def pytest_collection_modifyitems(items):
    enabled = os.environ.get("ZAB_LIVE") or os.environ.get("ZAB_LIVE_ZULIP")
    e2e = os.environ.get("ZAB_LIVE") == "e2e"
    admin_e2e = os.environ.get("ZAB_LIVE") == "admin-e2e"
    in_worker = "PYTEST_XDIST_WORKER" in os.environ  # set by xdist in each of its workers
    for item in items:
        if item.get_closest_marker("timing") and in_worker:
            # beside another worker on the core's second thread its ratio fails shipped code
            # now and then; CI runs the timing tests alone (#485)
            alone = "timing: run alone, `uv run pytest -m timing -p no:xdist`"
            item.add_marker(pytest.mark.skip(reason=alone))
        if item.get_closest_marker("admin_e2e") and not admin_e2e:
            # the test admin server, its role, the admin bots (issue #356): never otherwise
            item.add_marker(
                pytest.mark.skip(reason="set ZAB_LIVE=admin-e2e for the admin end-to-end suite")
            )
        if item.get_closest_marker("live") and not enabled:
            item.add_marker(
                pytest.mark.skip(reason="set ZAB_LIVE or ZAB_LIVE_ZULIP for live tests")
            )
        if item.get_closest_marker("e2e") and not e2e:
            # the test VM, the test users and AWS (issue #263): never in an ordinary run
            item.add_marker(pytest.mark.skip(reason="set ZAB_LIVE=e2e for the end-to-end suite"))


@pytest.hookimpl(wrapper=True, optionalhook=True)
def pytest_timeout_set_timer(item, settings):
    """pytest-timeout's alarm (its `signal` method, which fails the test and goes on), wrapped
    (#491). First, every asyncio task's await chain: a hung async test's main thread shows only
    the event loop's `select`. Then a backstop, which ends the process if the teardown hangs
    too, as pytest-asyncio's does while a task it cancels runs on: xdist then names the test in
    the crash of its worker, and goes on with another. A thread's timer, as the plugin stops its
    alarm once the test has failed."""
    armed = yield
    timed_out = signal.getsignal(signal.SIGALRM)
    if callable(timed_out):

        def handler(signum, frame):
            __tracebackhide__ = True
            from pytest_timeout import is_debugging

            if not settings.disable_debugger_detection and is_debugging():
                return timed_out(signum, frame)  # which then does nothing: nor does this
            _print_tasks()
            try:
                timed_out(signum, frame)
            finally:
                backstop = item.stash[BACKSTOP] = threading.Timer(TEARDOWN_GRACE, _end, (item,))
                backstop.daemon = True
                backstop.start()

        signal.signal(signal.SIGALRM, handler)
    return armed


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item):
    try:
        return (yield)
    finally:
        if (backstop := item.stash.get(BACKSTOP, None)) is not None:
            backstop.cancel()


def _print_tasks() -> None:
    try:
        tasks = asyncio.all_tasks()
    except RuntimeError:  # no event loop running: not an async test
        return
    for task in tasks:
        print(f"--- {task.get_name()}, cancel requests {task.cancelling()}:", file=sys.stderr)
        coro = task.get_coro()
        while frame := getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None):
            code = frame.f_code
            where = f'  File "{code.co_filename}", line {frame.f_lineno}, in {code.co_name}'
            print(where, file=sys.stderr)
            coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)


def _end(item) -> None:
    capture = item.config.pluginmanager.getplugin("capturemanager")
    if capture is not None:
        capture.suspend_global_capture(in_=True)  # to the terminal, or xdist's worker's stderr
    print(f"\n{item.nodeid}: not done {TEARDOWN_GRACE} s after its timeout", file=sys.stderr)
    sys.stderr.flush()
    os._exit(1)


@pytest.fixture(autouse=True)
def _claude_files_in_tmp(tmp_path_factory, monkeypatch):
    """The Claude backend's own files (the `!usage` record, the model-check directory) go to a
    temporary directory in every test, never the real home (issue #36)."""
    from zab.backends import claude

    base = tmp_path_factory.mktemp("claude-files")
    monkeypatch.setattr(claude, "USAGE_FILE", str(base / "claude-usage.json"))
    monkeypatch.setattr(claude, "MODEL_CHECK_DIR", str(base / "model-check"))


@pytest.fixture(autouse=True)
def _vm_cpu_sample_off(monkeypatch):
    """`!vm` and `!usage` sample `/proc/stat` (1 s and 0.25 s); tests that reach the real
    collector take the load average instead (issue #204). `tests/tools/test_vmstat.py` and
    `test_usage_does_not_wait_for_the_vm_sample` sample."""
    from zab.core import vm

    monkeypatch.setattr(vm, "SAMPLE_S", 0.0)
    monkeypatch.setattr(vm, "USAGE_SAMPLE_S", 0.0)


def _cpu(call: Callable[[Any], object], arg: Any, repeat: int = 1) -> float:
    """The CPU of `repeat` calls of `call(arg)` in a row, with the garbage collector off."""
    gc.disable()
    try:
        start = time.process_time()
        for _ in range(repeat):
            call(arg)
        return time.process_time() - start
    finally:
        gc.enable()


def _cpu_ratio_below(
    call: Callable[[Any], object], reference: Any, text: Any, bound: float, repeat: int = 1
) -> float:
    """Assert that `call(text)` takes less than `bound` times the CPU of `repeat` calls of
    `call(reference)`, and return that ratio: a bound on how a cost grows, which a runner's
    speed doesn't move. An absolute bound of 1 s, ten times this machine's time, failed on a
    runner at 1.12 s (#707). For a text `repeat` times the reference's size, the two do the
    same linear work, and take about as long: a disturbance that comes every few milliseconds
    slows both alike, where it would slow one long call more than four short ones. One did, on
    a runner: 2,000 openers took 1.1 ms there each time, and 8,000 of them 7.1 ms, then 4.5 ms.

    Each is timed three times, in turn, and each one's least time counts. The garbage
    collector is off while they run: a full collection walks every object the process holds,
    and with three million held, as in an xdist worker late in a run, collections took a call
    of 0.1 s here to as much as 0.29 s. The reference is called once untimed first, since a
    fresh process's first calls ran slow, and a ratio over the bound is measured again, the
    test failing only if the second fails too (#508). A test that uses it is marked `timing`,
    and runs alone (#485)."""
    __tracebackhide__ = True
    call(reference)
    tries = []
    for _ in range(2):
        times = [(_cpu(call, reference, repeat), _cpu(call, text)) for _ in range(3)]
        tries.append([(round(r, 4), round(t, 4)) for r, t in times])
        ratio = min(t for _, t in times) / max(min(r for r, _ in times), 1e-9)
        if ratio < bound:
            return ratio
    raise AssertionError(f"CPU ratio {ratio:.2f}, bound {bound}; (reference, text) s: {tries}")


@pytest.fixture
def cpu_ratio_below() -> Callable[..., float]:
    """`cpu_ratio_below(call, reference, text, bound, repeat=1)`, for a test marked `timing`
    (#707)."""
    return _cpu_ratio_below
