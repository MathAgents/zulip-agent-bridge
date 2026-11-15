"""One plan for every workshop (#995; docs/ADMIN.md §2.2, §2.4): `!rollout VERSION every-workshop
[words]` and `!trust-fix every-workshop`, on the server's bot, from a global admin.

- **Its parts.** For each workshop of admin.toml, in its order, with a row that `all` names and
  a VM, exactly the action its own bot builds for `!rollout VERSION all [words]` (or `!trust-fix
  all`) against its fleet (`actions.build`): its roster, region, scope, `mention_after_s`,
  `github_org` and waker rows. A workshop with no such row is left out, and the plan says so.
- **One plan.** A server plan (workshop ""), bound to the server's bot: every part's steps in
  turn, their dry runs hashed together as each verb hashes its own (the rollout's normalized
  dry run; the trust fix, a reconciler, nothing more); its targets, the workshops; one approval
  from a global admin, with the phrase `rollout <n>`, n the rows of every part
  (`approvals.requirement`).
- **The apply** (`runner.Runner`) holds every workshop's lock, as any server plan does, and runs
  the parts one after the other, each under its own deadline: each writes `apply_start` and
  `apply_end` under its workshop, as that workshop's `!rollout … all` would, and posts its own
  result in the plan's topic, which says that its commands go to its workshop's own bot; one
  that fails doesn't stop the next. Their records say `part`; `plan_end` follows the last, so
  that the console shows the plan applying until then. Then one line for them all.
- **One breaker** (#1031). A part whose script halted its starts (the use of 3 VMs in a row
  that it started couldn't be read: `actions.STARTS_HALTED`) halts them for the whole plan:
  each later part runs with `--halted-in` (`actions.halt_starts`), starts no VM, and refuses
  its rows of a stopped VM; the last line names the halt (`HALTS`).
- **Bounds.** The rollout's dry runs together stay within the hash's limit (`runner.KEEP // 2`,
  some 500 VMs at about 900 characters each), or the plan is refused, saying to roll out through
  each workshop's own bot; the trust fix's, a reconciler's, get its dry-run deadline once a
  workshop.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from zab.admin.actions import Action, ActionError, Fleet, Layout, build
from zab.admin.grammar import FLEETWIDE, Command, Selection

#: The note of every fleet-wide plan: what holding every workshop's lock means.
LOCKS = (
    "**Every workshop's lock** is held from the approval to the last result: their organizers' "
    "approvals are answered `busy:` meanwhile. Each workshop's part posts its own result here."
)
#: The note on stopped VMs across the parts (#1031).
HALTS = (
    "**Stopped VMs, across the workshops:** once a part halts its starts (the use of 3 VMs in a "
    "row that it started couldn't be read), the later parts start no VM: their rows of a "
    "stopped VM read `not started: …`, and the last line names the halt."
)


def part_command(command: Command) -> Command:
    """The workshop's own command that a part runs: `all` in place of `every-workshop`."""
    return dataclasses.replace(command, selection=Selection(all=True))


def build_fleetwide(
    command: Command, fleets: Sequence[Fleet], *, layout: Layout, bot: int
) -> Action:
    """The server plan of `command` (above) against every workshop's `fleets`, bound to the
    server's bot `bot`. `ActionError` when a part doesn't build, naming its workshop, or when
    no workshop has a row with a VM."""
    sel = command.selection
    if command.verb not in FLEETWIDE or sel is None or not sel.every_workshop:
        raise ActionError(f"!{command.verb} is not a command of every workshop")
    one = part_command(command)
    parts: list[Action] = []
    left: list[str] = []
    for fleet in fleets:
        if not any(row.ip and fleet.in_all(row.slug) for row in fleet.rows):
            left.append(fleet.workshop.name)
            continue
        try:
            parts.append(build(one, fleet, layout=layout))
        except ActionError as exc:
            raise ActionError(f"{fleet.workshop.name}: {exc}") from None
    if not parts:
        raise ActionError("no workshop has a row with a VM: nothing to do")
    notes = [note for note in parts[0].notes if all(note in p.notes for p in parts)]
    for part in parts:  # what differs, by workshop: rows without the waker, say
        notes += [f"**{part.workshop}:** {n}" for n in part.notes if n not in notes]
    if left:
        notes.append(f"Left out, with no row in service that has a VM: {', '.join(left)}.")
    notes += [HALTS] * (len(parts) > 1) + [LOCKS]
    return Action(
        verb=command.verb,
        workshop="",
        bot=bot,
        args=command.tokens(),
        steps=tuple(step for part in parts for step in part.steps),
        hashed=parts[0].hashed,
        targets=tuple(part.workshop for part in parts),
        inputs=tuple(dict.fromkeys(path for part in parts for path in part.inputs)),
        notes=tuple(notes),
        parts=tuple(parts),
    )
