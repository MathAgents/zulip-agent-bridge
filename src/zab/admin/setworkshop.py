"""`!set-workshop` and `!show-workshop` (docs/ADMIN.md §2.10; #1043): an existing workshop's
entry in admin.toml, changed from Zulip, and read. Server verbs, on the `[global]` bot.

`build` maps `!set-workshop NAME [ami=…] [gifts_step=…]` to one step,
`workshop/admin/set_workshop.py`, whose dry run is the plan and whose run sends the change to the
root helper (`zab.admin.confighelper.Change`), as `!new-workshop`'s apply sends its entry:

- the workshop must be one of admin.toml's, and each key named must change: an image that is the
  workshop's already, a gift step equal to its own, or the removal of one it lacks, is refused
  here, before any script. `ami=newest` is the region's newest image in the fleet's scope, which
  the script picks as `!new-workshop` does (`new_workshop.newest_image`), and says when it is the
  workshop's already. It is picked once, at the plan, and kept in the plan's own directory
  (`--record`, `runs/P-<n>/set-workshop.json`), so that the apply sets exactly the image the
  approver saw (review of #1046, N2);
- every word of the argument list is in the safe alphabet (§2.3): the image's ID, and the gift
  step base64url-encoded, as `!provision zulip` passes it (`--gifts-step-b64`); the values the
  plan read go with them, which the helper checks the entry still holds;
- a server plan, which one global admin approves (§2.5). Its apply needs every workshop's lock
  (§2.4 step 5), since it ends in the bots' restart, which loads the entry and would cut short
  any other apply; admin.toml is among its inputs, so a change of it in between makes it STALE.

`show` is `!show-workshop`'s answer: the entry as the bots loaded it, its files named by their
paths, never read.
"""

from __future__ import annotations

import base64
import json

from zab.admin.actions import DRY_RUN, Action, ActionError, Layout, Step, _value
from zab.admin.config import AdminConfig
from zab.admin.grammar import Command
from zab.admin.newworkshop import fleet_scope, run

SCRIPT = "admin/set_workshop.py"


def b64(text: str) -> str:
    """A gift step as an argument's word (§2.3): its UTF-8, base64url-encoded."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


def build(
    command: Command, config: AdminConfig, *, plan: str, bot: int, layout: Layout, config_path: str
) -> Action:
    """The action of `!set-workshop` for plan `plan` on the `[global]` bot `bot`; `config_path`
    is admin.toml, an input of the hash. Raises `ActionError`, one line, on anything else."""
    name, ami, gifts = command.workshop, command.ami, command.gifts_step
    if command.verb != "set-workshop" or not name or (ami is None and gifts is None):
        raise ActionError("not a whole !set-workshop command: refused")
    ws = config.workshop(name)
    if ws is None:
        raise ActionError(
            f"{name}: no workshop of that name on this server (`!version` lists them)"
        )
    if not config.admins:  # as !new-workshop's: a server plan, which a global admin approves
        raise ActionError(
            "this server has no global admin, and only one approves a server plan: nobody "
            "could approve it; a deployer adds a global admin first"
        )
    if ami == ws.aws.ami:
        raise ActionError(f"ami={ami} is {name}'s image already: nothing to change")
    if gifts == "" and ws.gifts_step is None:
        raise ActionError(f"{name} has no gifts_step: nothing to remove")
    if gifts is not None and gifts == ws.gifts_step:
        raise ActionError(f"gifts_step: {name}'s is that text already: nothing to change")
    values = [("ami-was", ws.aws.ami)]  # what the plan reads, which the helper checks
    if ws.gifts_step is not None:
        values.append(("gifts-step-was-b64", b64(ws.gifts_step)))
    if ami is not None:
        values.append(("ami", ami))
    if ami == "newest":  # the plan's pick, kept for its apply (review of #1046, N2)
        values.append(("record", f"{layout.runs}/{plan}/set-workshop.json"))
    if gifts:
        values.append(("gifts-step-b64", b64(gifts)))
    pairs = [
        ("plan", plan), ("name", name), ("region", ws.aws.region),
        ("security-group", ws.aws.security_group),
        *(("instance-type", t) for t in ws.aws.instance_types),
        ("tag", f"workshop={name}"), *(("scope", f"{k}={v}") for k, v in fleet_scope(config)),
        *values, ("request-dir", layout.config_request), ("result", layout.config_result),
    ]  # fmt: skip
    argv = run(layout, SCRIPT, *((f"--{flag}", _value(value)) for flag, value in pairs))
    if gifts == "":
        argv = (*argv, "--no-gifts-step")
    keys = " and ".join(
        key for key, value in (("ami", ami), ("gifts_step", gifts)) if value is not None
    )
    return Action(
        verb="set-workshop",
        workshop="",
        bot=bot,
        args=command.tokens(),
        steps=(Step(argv, (*argv, "--dry-run")),),
        hashed=DRY_RUN,
        targets=(name,),
        inputs=(config_path,),
        notes=(
            f"A change of **{name}**'s entry in admin.toml: its {keys}, through the config "
            "helper. The bots restart once the result is posted.",
        ),
    )


def show(config: AdminConfig, name: str) -> str:
    """`!show-workshop`'s text (#1043): the workshop's entry as the bots loaded it, in admin.toml's
    form, the defaults it takes included, then its scope; "" for no such workshop. Its files are
    named by their paths alone, as `!new-workshop`'s plan names them: never read."""
    ws = config.workshop(name)
    if ws is None:
        return ""
    bot, aws = ws.bot, ws.aws
    tables = {
        "[[workshop]]": [
            ("name", ws.name), ("github_org", ws.github_org), ("refserve_url", ws.refserve_url),
            ("roster", ws.roster), ("out_dir", ws.out_dir),
            ("organizers", sorted(ws.organizers)), ("alerts_channel", ws.alerts_channel),
            ("channel", ws.channel), ("gifts_step", ws.gifts_step),
            ("mention_after_s", ws.mention_after_s), ("spare_idle_stop", ws.spare_idle_stop),
        ],
        "[workshop.bot]": [
            ("zuliprc", bot.zuliprc), ("user_id", bot.user_id), ("email", bot.email),
            ("channels", sorted(bot.channels)),
        ],
        "[workshop.aws]": [
            ("region", aws.region), ("ami", aws.ami), ("security_group", aws.security_group),
            ("instance_types", list(aws.instance_types)), ("key_name", aws.key_name),
            ("admin_keys", list(aws.admin_keys)),
        ],
    }  # fmt: skip
    lines = []
    for table, pairs in tables.items():
        lines.append(table)
        lines += [f"{k} = {json.dumps(v, ensure_ascii=False)}" for k, v in pairs if v is not None]
    scope = ", ".join(f"{key}={value}" for key, value in ws.tags)
    return "\n".join([*lines, f"# its scope: {scope}"])
