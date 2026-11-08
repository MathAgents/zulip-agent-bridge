"""`!upgrade-server`'s action (docs/ADMIN.md §1.6; #569): a server verb, on the `[global]` bot.

`build` maps the parsed command to one step, `workshop/admin/upgrade_server.py`, which the bot
runs as `zab-admin` and which only writes requests to the upgrade's root helpers and reads their
outcomes (`zab.admin.upgradehelper`):

- its dry run is the plan: the plan helper fetches the release and the running one's tags from
  `release_repo`, checks them as deploy.sh does, has the running release's deploy.sh review the
  release, refuses unless that review's SHA-256 is the command's `sha256=`, the deployer's own,
  and prints the plan's text, which the plan is hashed on whole. The runner runs that dry run
  again before the apply, so a tag that moved since makes the plan STALE;
- its run is the apply: the apply helper installs the release with its own deploy.sh, as root,
  and restarts every unit but the bots'. The bot restarts itself once it has posted the result,
  whenever `current` moved (`planning.Planning._applied`).

Every value comes from the command's checked words (a version of the `release` pattern, a
SHA-256's 64 hex digits), the plan's name and the server's fixed paths (`Layout`). It only goes
forward: the grammar refuses `downgrade`, and the plan helper any release older than the
running one; a deployer goes back with `deploy.sh --downgrade` (BOOTSTRAP §13.4). Only a
deployer requests it (`planning`), and one deployer approves it (§2.5's deploy class,
`approvals.requirement`).
"""

from __future__ import annotations

from zab.admin.actions import DRY_RUN, Action, ActionError, Layout, Step, _value
from zab.admin.config import AdminConfig
from zab.admin.grammar import Command
from zab.admin.newworkshop import run

SCRIPT = "admin/upgrade_server.py"
#: The apply's deadline (runner.TIMEOUTS has the same): above the script's own wait for the
#: apply helper, whose unit stops it at 1500 s.
TIMEOUT = 1800.0


def build(command: Command, config: AdminConfig, *, plan: str, bot: int, layout: Layout) -> Action:
    """The action of `!upgrade-server` for plan `plan` on the `[global]` bot `bot`. Raises
    `ActionError`, one line, when it can't be planned on this server."""
    version, sha256 = command.version, command.sha256
    if command.verb != "upgrade-server" or not (version and sha256):
        raise ActionError("not a whole !upgrade-server command: refused")
    if not config.release_repo:
        raise ActionError(
            "the in-band upgrade is off on this server: admin.toml has no release_repo, so a "
            "deployer deploys with deploy.sh (BOOTSTRAP §13)"
        )
    if not config.deployers:  # never: the loader requires one
        raise ActionError("this server has no deployer to approve it")
    pairs = [("plan", plan), ("release", version), ("sha256", sha256), ("state", layout.upgrade)]
    flags = [part for flag, value in pairs for part in (f"--{flag}", _value(value))]
    argv = run(layout, SCRIPT, *flags)
    return Action(
        verb="upgrade-server",
        workshop="",
        bot=bot,
        args=command.tokens(),
        steps=(Step(argv, (*argv, "--dry-run")),),
        hashed=DRY_RUN,
        targets=(version,),
        inputs=(),
        notes=(
            f"The server's own upgrade to **{version}**: "
            "compare the plan's SHA-256 and release tree with your own review's, and read its "
            "privileged diff. Once applied, every unit restarts into it; the bots restart "
            "themselves after the result.",
        ),
        timeout=TIMEOUT,
    )
