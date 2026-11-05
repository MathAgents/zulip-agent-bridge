"""The waker's start-up stays small (#717): its peak memory, measured by `load_rig`.

On Nov 1, with about 118 opted-in bots after two workshop days, each start was OOM-killed at
the unit's `MemoryMax=512M`, and peaked at about 1.3 GB once swap let it live. The rig serves
150 bots, each with 100 messages of 10,000 characters, and the waker runs in a child process
whose peak resident memory (`VmHWM`) these tests bound. The figures before and after the fix
are DECISIONS' verification log's (#717).

`slow`: each run starts a child process and serves its 150 bots for several seconds. On a pull
request, what bounds the start-up is covered by `test_catchup.py` (only what is new; the VM's
own bots left out; the turns, `AT_ONCE`) and `tests/zulip/test_client.py` (one TLS context).
"""

from pathlib import Path

import pytest

from .load_rig import measure

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(not Path("/proc/self/status").exists(), reason="VmHWM is Linux's"),
]
MB = 1024  # VmHWM is in kB
#: The criterion's reproduction (#717, 1): measured at 57 to 66 MB after the fix on Python 3.11
#: to 3.14, and at 439 MB before it (3.12, OpenSSL 3.0, as the admin server's).
BOUND = 150 * MB
#: A long outage (the marks 100 messages back, as after Nov 1's 90 minutes) in a channel the
#: VMs share, where another VM's bot's edited status messages are not left out: 132 to 201 MB
#: at 118 to 150 bots on Python 3.11 to 3.14; 555 to 816 MB with #727's first catch-up.
WORST = 300 * MB


@pytest.mark.parametrize("edits", [0, 20], ids=["plain", "edited status messages"])
def test_a_start_after_two_workshop_days_stays_small(edits):
    """The marks 3 messages back, a turn in three: the owner's prompt, which wakes the VM, then
    the bot's status message and its answer; the status messages with `edits` versions each in
    their edit history (a turn of about 3 minutes, edited every 10 s), as Zulip returns them.
    Before the fix: 439 and 874 MB."""
    report = measure(bots=150, messages=100, size=10_000, new=3, edits=edits)
    assert report["starts"] == 75  # every VM woke, once
    assert report["peak_kb"] < BOUND, report


def test_a_long_outage_in_a_shared_channel_stays_bounded():
    """The marks 100 messages back, the bots' messages another VM's, with 60 edits each: the
    VM's own bots can't be left out, and the pages (`PAGE`) and the turns (`AT_ONCE`) bound what
    is held at once. Each mark is a message (101 of them), so that every bot walks back to it."""
    report = measure(bots=150, messages=101, size=10_000, new=100, edits=60, foreign=True)
    assert report["starts"] == 75
    assert report["requests"]["GET messages"] >= 3 * 150  # pages, back to the 5 minutes' floor
    assert report["peak_kb"] < WORST, report
