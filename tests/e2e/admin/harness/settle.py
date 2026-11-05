"""Reading AWS just after a change (issue #356): read again until the change shows, bounded.

EC2 is eventually consistent: a describe call made right after a change may be answered from
a replica that has not seen it yet, and a call that depends on the change may be refused for a
moment. The first live runs met it twice: A4 read `DisableApiTermination` False just after the
role had set it True (CloudTrail had the call succeed), for up to 157 s, and the teardown's
`TerminateInstances` right after lifting a protection was refused (`OperationNotPermitted`).

So whatever reads what was just changed reads through this one helper, again and again, at most
`within` seconds, until the value is the one wanted, and gets that value: the scenarios with
`until` (asyncio), the rig's setup and teardown with `until_blocking` (their own sleep and
clock). A value that never comes fails (`AssertionError`), or, with `strict=False`, is returned
as last read, for a caller that reports it itself. A read that raises is not retried: a refusal
is a finding, not a delay.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Generator
from typing import Any, TypeVar

T = TypeVar("T")
#: The bound for a read to show a change made just before (attributes, tags, launches, states).
WITHIN_S = 180.0
#: The bound for `disableApiTermination` to show a change: on the #263 test VM, a read gave
#: False for 157 s after a successful `--disable-api-termination` (live, Oct 15).
PROTECTION_S = 300.0
#: The pause between two reads.
EVERY_S = 5.0
#: Time as `until` reads and spends it, looked up at each call (the offline tests' fake clock).
clock: Callable[[], float] = time.monotonic
sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep


def _reads(
    read: Callable[[], T], wanted: Callable[[T], bool], what: str, within: float, every: float,
    now: Callable[[], float], strict: bool,
) -> Generator[float, None, T]:  # fmt: skip
    """The polling itself: yields each pause; returns the value (`StopIteration.value`)."""
    start = now()
    while True:
        value = read()
        if wanted(value):
            return value
        if now() - start >= within:
            if strict:
                raise AssertionError(f"{what}: still {repr(value)[:500]} after {within:.0f} s")
            return value
        yield every


async def until(
    read: Callable[[], T],
    wanted: Callable[[T], bool],
    *,
    what: str,
    within: float = WITHIN_S,
    every: float = EVERY_S,
) -> T:
    """`read()` until `wanted` holds of its value, which is returned; after `within` seconds,
    an `AssertionError` naming `what` and the last value read (its first 500 characters)."""
    reads = _reads(read, wanted, what, within, every, clock, True)
    while True:
        try:
            pause = next(reads)
        except StopIteration as done:
            return done.value
        await sleep(pause)


def until_blocking(
    read: Callable[[], T],
    wanted: Callable[[T], bool],
    *,
    what: str,
    within: float = WITHIN_S,
    every: float = EVERY_S,
    strict: bool = True,
    sleep: Callable[[float], Any] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """`until`, blocking: the rig's setup and teardown (`AwsRig.until`, its sleep and clock).
    With `strict=False`, the last value read is returned at the bound instead."""
    reads = _reads(read, wanted, what, within, every, clock, strict)
    while True:
        try:
            pause = next(reads)
        except StopIteration as done:
            return done.value
        sleep(pause)


def attribute(aws: Any, instance_id: str, name: str) -> Any:
    """An instance's attribute (`disableApiTermination`), as `describe_instance_attribute`
    gives its value; `aws` is an `AwsRig`."""
    found = aws.aws.ec2("describe_instance_attribute", InstanceId=instance_id, Attribute=name)
    return found[name[0].upper() + name[1:]]["Value"]
