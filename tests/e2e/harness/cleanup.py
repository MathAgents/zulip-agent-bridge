"""What a scenario may leave behind on the test VM, undone (issue #263; reviewer on #283).

`run.py` runs these before it stops the VM, however the run ended: an interrupt can stop a
scenario before its own `finally` runs. Each step is idempotent, and harmless when there is
nothing to undo. Fixed scripts with no inputs (CONTRIBUTING.md, rule 3).

A scenario's own clean-up goes through `undo`: a few tries, a short pause apart. In #517's
run, 10 s after a boot, push_config's owner-check session reached the VM and ended; then either
push_config refused the owner check or its next connection never reached sshd. #517 item 4
saw the second kind since: connections from the development machine that never reached the VM, most
likely packet loss on its path, behind a home NAT; not shown to be the VM. The harness's own SSH
retries those (`Vm.call`); push_config's is the tested build's, so these tries are its
mitigation. One that still fails raises `Leftover`, which `conftest` makes
a run problem naming what was left; when the later scenarios can't run in that state, the run
stops after this scenario.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Callable

from e2e.harness.secret_files import described

#: Scenario 4's Zulip block: every OUTPUT rule tagged with the comment `zab-e2e`, in iptables
#: and ip6tables, deleted as iptables itself prints them; and any `# zab-e2e` line in
#: /etc/hosts (an earlier version of the block wrote one; it survived a stop).
UNBLOCK = r"""for t in iptables ip6tables; do
  "$t" -S OUTPUT 2>/dev/null | grep -- '--comment zab-e2e' | sed 's/^-A /-D /' |
    while read -r -a rule; do "$t" "${rule[@]}" || true; done
done
sed -i '/ # zab-e2e$/d' /etc/hosts
"""

#: What a failed clean-up leaves, as the run problem names it (#517).
IDLE_STOP_LEFT = "idle stop is still installed on the VM"
ZULIP_BLOCKED = "the VM is still cut off from Zulip"
BRIDGE_STOPPED = "the bridge is still stopped on the VM"
VM_STOPPED = "the VM is not running"
WARM_DISABLED = "the warm-up unit is still disabled on the VM"
#: A clean-up step's tries, and the seconds between them.
ATTEMPTS, PAUSE_S = 3, 15.0


class Leftover(RuntimeError):
    """A scenario's clean-up failed after its tries (#517): `what` it left on the VM. With
    `stops`, the later scenarios can't run in that state and the run ends after this one.
    `after`: the scenario's own failure, when its clean-up ran because of one; raised from a
    `finally`, this exception replaces it, so the report shows it from here (reviewer, N1)."""

    def __init__(
        self, what: str, cause: BaseException, *, stops: bool = True, after: str = ""
    ) -> None:
        super().__init__(f"{what} ({described(cause)})")
        self.what, self.stops, self.after = what, stops, after


def failed_with(exc: BaseException | None) -> str:
    """`Type: message`'s first line (`described`: the type alone for an empty message, as a
    cancelled body's), or "" for None."""
    if exc is None:
        return ""
    return (described(exc).strip().splitlines() or [""])[0][:200]


async def undo(
    what: str,
    step: Callable[[], object],
    *,
    stops: bool = True,
    attempts: int = ATTEMPTS,
    pause: float = PAUSE_S,
) -> None:
    """Run the clean-up `step` (a function, or one that returns an awaitable) until it
    succeeds, at most `attempts` times, `pause` seconds apart; then Leftover(what). Called in
    a `finally`, the exception that ran it (if any) is the Leftover's `after`.

    `after` is `sys.exc_info()`: any exception being handled up the call chain, not only the
    enclosing `finally`'s. A scenario driven from inside an `except` would get that one's;
    none is (pytest-asyncio drives them, each `undo` in a function-level `try/finally`)."""
    after = failed_with(sys.exc_info()[1])
    for attempt in range(1, attempts + 1):
        try:
            done = step()
            if inspect.isawaitable(done):
                await done
            return
        except Exception as exc:
            if attempt == attempts:
                raise Leftover(what, exc, stops=stops, after=after) from exc
            await asyncio.sleep(pause)


async def vm_up(ec2, instance_id: str, vm, *, tries: int = 30, pause: float = 10.0) -> None:
    """The VM running and answering SSH: started if it is stopped or stopping, then waited for;
    TimeoutError when SSH does not answer within `tries * pause` seconds. A stopping VM may
    read `pending` next, when the test waker starts it again: only a `stopped` one is started
    (#305)."""
    state = ec2.state(instance_id)
    if state == "stopping":
        state = ec2.wait_while(instance_id, "stopping", timeout=300)
    if state == "stopped":
        ec2.start(instance_id)
    if state != "running":
        ec2.wait_state(instance_id, "running", timeout=600)
    for _ in range(tries):
        if vm.reachable():
            return
        await asyncio.sleep(pause)
    raise TimeoutError(f"no SSH to the VM within {tries * pause:.0f} s")
