"""A stop is never lost (#491). The cancel that stops the admin server can come just as what a
wait waits for ends. Python 3.11's `asyncio.wait_for` then returned the result and dropped the
cancel (gh-86296; 3.12 rebuilt it on `asyncio.timeout`): a loop around it went on, and
`AdminServer.run`'s TaskGroup waited for it forever. Each test makes that moment happen; with
`wait_for` in its place, each fails on 3.11."""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from zab.admin.runner import Capture, Ran, Runner

from .botkit import HARVARD, HARVARD_CH, OLGA, make_site, until


@pytest.fixture
async def server(tmp_path):
    site = make_site(tmp_path)
    srv = site.server()
    await srv.start()
    yield site, srv
    await srv.close()


async def stopped(task):
    """`task` ends cancelled, within 5 s."""
    with pytest.raises(asyncio.CancelledError):
        async with asyncio.timeout(5):
            await task


async def test_a_bots_queue_loop_stops_on_a_cancel_as_a_message_wakes_it(server):
    """The hang itself (`test_admin_ack`'s `running`): a post woke the bots' long polls in the
    fake realm as the TaskGroup cancelled their loops, and they polled on."""
    site, srv = server
    bot = next(b for b in srv.bots if b.user_id == HARVARD)
    task = asyncio.create_task(bot.listen())
    await until(lambda: (HARVARD, "GET", "events") in site.realm.requests)  # it waits
    site.realm.say(OLGA, HARVARD_CH, "hello")  # the realm's event is set ...
    await asyncio.sleep(0)  # ... and what waits on it ends, before the poll resumes
    task.cancel()
    await stopped(task)


async def test_the_wake_monitor_stops_on_a_cancel_as_a_pass_ends(server, monkeypatch):
    _, srv = server
    task: asyncio.Task[None] | None = None

    async def check_pass():
        asyncio.get_running_loop().call_soon(task.cancel)  # before the monitor resumes

    monkeypatch.setattr(srv.wakes, "check_pass", check_pass)
    task = asyncio.create_task(srv.wakes.run())
    await stopped(task)


async def test_the_runner_raises_a_stop_that_comes_as_its_script_exits(
    server, monkeypatch, tmp_path
):
    """Not the exit code: an apply would go on to its next step while the server stops."""
    _, srv = server
    wait, task = asyncio.subprocess.Process.wait, None

    async def exits(proc):
        code = await wait(proc)
        asyncio.get_running_loop().call_soon(task.cancel)  # before the runner resumes
        return code

    monkeypatch.setattr(asyncio.subprocess.Process, "wait", exits)
    argv, env = [sys.executable, "-c", "pass"], {"PATH": os.environ["PATH"]}
    run = Runner(srv).run(argv, env, str(tmp_path), time.monotonic() + 30, (Capture(),), Ran("-"))
    task = asyncio.create_task(run)
    await stopped(task)
