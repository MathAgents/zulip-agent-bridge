#!/usr/bin/env python3
"""`set_workshop.py`: a change of an existing workshop's entry, for `!set-workshop` (docs/ADMIN.md
§2.10; #1043).

The admin server's global bot runs it, as `zab-admin`, with every value from admin.toml and the
approved command (`zab.admin.setworkshop`), never typed text:

    set_workshop.py --plan P-43 --name mit-2027 --region us-east-2 --security-group sg-… \\
      --instance-type m7a.2xlarge --tag workshop=mit-2027 --scope zab-scope=mathagents \\
      [--scope zab-role=test] --ami-was ami-… [--gifts-step-was-b64 B64] \\
      [--ami ami-… | --ami newest --record /var/lib/zab-admin/runs/P-43/set-workshop.json] \\
      [--gifts-step-b64 B64 | --no-gifts-step] \\
      --request-dir /var/lib/zab-admin-config/requests \\
      --result /var/lib/zab-admin-config/result.json [--dry-run]

`--ami-was` and `--gifts-step-was-b64` are what the entry holds; a gift step goes base64url-encoded
(`zulip_provision.decode_gifts_step`, which checks the loader's rule again).

**`--dry-run`, the plan,** changes nothing in admin.toml. It prints each line that changes, old
and new, and what the change affects, then the image's checks (`ok:` or `MISSING:`): a missing
one exits 1, so the plan is refused, never offered.
- `--ami newest`: at the plan, the region's newest image in the fleet's scope (`--scope`), picked
  as `!new-workshop` picks it (`new_workshop.newest_image`), two made at the same time refused,
  and one that is the workshop's image already refused too: nothing to change. The image picked
  is written to `--record`, in the plan's own directory (review of #1046, N2): the dry run the
  runner runs again at apply, for the hash, and the apply itself read it from there, so that
  what is applied is exactly the image the approver saw, never one tagged since. An image newer
  than it by then is said in the apply's output; the one the plan named is applied all the same.
- `--ami ami-…`: that image, available in the region and carrying the fleet's scope.
- Either way, a DryRun launch of each of the workshop's instance types from it, in its security
  group, tagged with its scope (`--tag` and `--scope`), as `!new-workshop` checks a region (V12).

**The apply** checks the image again, then sends the change to the root helper
(`zab.admin.confighelper.Change`): the new values and those the plan read, which the helper
checks the entry still holds; then it waits for the outcome as `!new-workshop`'s apply does
(`new_workshop.submit`). The bot restarts after the plan's result, which loads the entry.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from new_workshop import (
    Ec2,
    Setup,
    Words,
    error_line,
    image_line,
    may_launch,
    newest_image,
    parse_tags,
    scope_text,
    submit,
)
from zulip_provision import decode_gifts_step

CHANGES = Words("the change may be made", "nothing was changed", "")
AMI = re.compile(r"ami-[0-9a-f]{8,17}", re.ASCII)  # zab.admin.config's
#: What each key's change affects (ADMIN §2.10), as the plan says it.
AFFECTS = {
    "ami": "the VMs !launch makes from now on, spares included; a VM that exists keeps its "
    "image, running or stopped",
    "gifts_step": '"Start here" posted from now on (!provision zulip, !adopt-spare), and the '
    "next full !provision zulip, which rewrites each sandbox's description that differs; a "
    '"Start here" posted already stays as it is',
}
TABLES = {"ami": "[workshop.aws]", "gifts_step": "[[workshop]]"}


@dataclass
class Change:
    """The change, from the arguments: `ami` "newest" or an image's ID, `gifts` the new text or
    "" to remove it, each None when unchanged."""

    plan: str
    name: str
    region: str
    group: str
    instance_types: list[str]
    tags: dict[str, str]  # `workshop=<name>`
    scope: dict[str, str]  # the fleet's: the region's images carry it
    ami_was: str
    gifts_was: str | None
    ami: str | None
    gifts: str | None
    record: pathlib.Path | None = None  # `--ami newest`'s pick, from the plan's dry run on


def survey(ch: Change, ec2: Any, *, dry_run: bool) -> tuple[Setup, str | None]:
    """The image the change sets, and its checks: the lines, and its ID (None without one)."""
    setup, text = Setup(), scope_text(ch.scope)
    if ch.ami is None:
        return setup, None
    named = ch.ami if ch.ami != "newest" else recorded(ch, setup)
    if named is None and not setup.problems and dry_run:  # the plan's: picked, and recorded
        picking = Setup()
        image = newest_image(ec2, ch.region, ch.scope, picking)  # its own line, "of N", left
        setup.problems += picking.problems
        if image is not None and image["ImageId"] != ch.ami_was:
            keep(ch, image["ImageId"])
    elif named is None:  # the apply, with no pick of the plan's (or `recorded` said why)
        if not setup.problems:
            setup.problems.append("no image was picked at the plan: request it again")
        image = None
    else:
        image = next((i for i in ec2.find_images(ch.scope) if i.get("ImageId") == named), None)
        if image is None:
            setup.problems.append(f"no available image {named} tagged {text} in {ch.region}")
    if image is None:
        return setup, None
    when = ", the region's newest at the plan" if ch.ami == "newest" else ""
    setup.found.append(f"image {image_line(image)}{when}, tagged {text}")
    if image["ImageId"] == ch.ami_was:  # `newest` (the bot refuses the same ID named)
        setup.problems.append(
            f"ami=newest: {ch.ami_was}, the region's newest image, is {ch.name}'s already: "
            "nothing to change"
        )
        return setup, image["ImageId"]
    for kind in ch.instance_types:
        what = f"the role may launch {kind} from it in {ch.group}, in {ch.region}"
        may_launch(ec2, setup, image["ImageId"], ch.group, kind, {**ch.scope, **ch.tags}, what)
    return setup, image["ImageId"]


def recorded(ch: Change, setup: Setup) -> str | None:
    """The image the plan's dry run picked for `--ami newest` (`--record`); None when it has
    picked none yet, or, said in `setup`, when the record isn't this plan's."""
    assert ch.record is not None
    try:
        found = json.loads(ch.record.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        found = None
    ami = found.get("ami") if isinstance(found, dict) and found.get("plan") == ch.plan else None
    if isinstance(ami, str) and AMI.fullmatch(ami):
        return ami
    setup.problems.append(f"{ch.record} is not this plan's pick of an image: request it again")
    return None


def keep(ch: Change, image: str) -> None:
    """`image`, the plan's pick, in `--record`: 0600, in the plan's own directory, whole."""
    assert ch.record is not None
    ch.record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = ch.record.with_name(f"{ch.record.name}.{os.getpid()}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        json.dump({"plan": ch.plan, "ami": image}, f)
    os.replace(tmp, ch.record)


def newer(ch: Change, ec2: Any, image: str) -> str:
    """At the apply of `--ami newest`: a line when the region's newest image is no longer the
    plan's pick, which is applied all the same, as approved; "" otherwise."""
    try:
        now = newest_image(ec2, ch.region, ch.scope, Setup())
    except Exception as exc:  # never a reason not to apply what was approved
        return f"note: the region's newest image could not be read again ({error_line(exc)})"
    if now is None or now["ImageId"] == image:
        return ""
    return (
        f"note: {image_line(now)} is the region's newest image now; {image}, the plan's, is "
        "applied, as approved"
    )


def show(ch: Change, setup: Setup, values: dict[str, str | None]) -> None:
    """The plan's text, or the apply's first lines."""
    was = {"ami": ch.ami_was, "gifts_step": ch.gifts_was}
    print(f"A change of {ch.name}'s entry in admin.toml (its region {ch.region}):")
    for key, value in values.items():
        print(f"\n  {TABLES[key]}")
        for mark, shown in (("-", was[key]), ("+", value)):
            print(f"{mark} {key} = {_toml(shown)}" if shown is not None else f"{mark} {key}: none")
        print(f"  affects: {AFFECTS[key]}")
    print("\nThen: the config helper writes it, keeping admin.toml.bak; the bots restart, and")
    print("load it.")
    if setup.found or setup.problems:
        print("\nChecks:")
        print("\n".join([*(f"  ok: {line}" for line in setup.found),
                         *(f"  MISSING: {line}" for line in setup.problems)]))  # fmt: skip


def _toml(value: str) -> str:
    """A TOML basic string, as admin.toml holds it (`zab.admin.confighelper._rewrite`)."""
    return json.dumps(value, ensure_ascii=False)


def change(args: argparse.Namespace) -> Change:
    """The change, its arguments checked: ValueError on anything else."""
    if parse_tags(args.tag) != {"workshop": args.name}:
        raise ValueError(f"--tag is workshop={args.name} alone; the fleet's scope is --scope")
    new = args.ami in (None, "newest") or AMI.fullmatch(args.ami)
    if not AMI.fullmatch(args.ami_was) or not new:
        raise ValueError("--ami and --ami-was: an image's ID, ami-<hex>; --ami newest too")
    was = decode_gifts_step(args.gifts_step_was_b64) if args.gifts_step_was_b64 else None
    gifts = decode_gifts_step(args.gifts_step_b64) if args.gifts_step_b64 else None
    gifts = "" if args.no_gifts_step else gifts
    if args.ami is None and gifts is None:
        raise ValueError("nothing to change: --ami, --gifts-step-b64 or --no-gifts-step")
    if args.ami == args.ami_was or (gifts is not None and gifts == (was or "")):
        raise ValueError("a value the entry holds already: nothing to change")
    if (args.ami == "newest") != (args.record is not None):
        raise ValueError("--record goes with --ami newest, and only with it")
    record = pathlib.Path(args.record) if args.record else None
    return Change(
        args.plan, args.name, args.region, args.security_group, args.instance_type,
        parse_tags(args.tag), parse_tags(args.scope), args.ami_was, was, args.ami, gifts, record,
    )  # fmt: skip


def run(ch: Change, ec2: Any, args: argparse.Namespace) -> int:
    setup, image = survey(ch, ec2, dry_run=args.dry_run)
    values: dict[str, str | None] = {}
    if ch.ami is not None:
        values["ami"] = image or f"<{ch.ami}: none found>"
    if ch.gifts is not None:
        values["gifts_step"] = ch.gifts or None
    show(ch, setup, values)
    if setup.problems:
        if not args.dry_run:
            print("\nNothing was changed: a check failed.")
        return 1
    if args.dry_run:
        return 0
    note = newer(ch, ec2, image) if ch.ami == "newest" and image else ""
    if note:  # the apply's output, never the hashed dry run's
        print(f"\n{note}")
    was = {"ami": ch.ami_was, "gifts_step": ch.gifts_was}
    body = {"version": 1, "plan": ch.plan, "workshop": ch.name, "set": values,
            "was": {key: was[key] for key in values}}  # fmt: skip
    return submit(ch.plan, body, args.request_dir, args.result, CHANGES)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="set_workshop.py", description=__doc__.split("\n\n")[0])
    for flag in ("plan", "name", "region", "security-group", "ami-was", "request-dir", "result"):
        p.add_argument(f"--{flag}", required=True)
    for flag in ("instance-type", "tag", "scope"):
        p.add_argument(f"--{flag}", action="append", required=True)
    p.add_argument("--gifts-step-was-b64", help="the entry's gift step, base64url")
    p.add_argument("--ami", help="the new image: ami-<hex>, or newest")
    p.add_argument("--record", help="with --ami newest: the plan's pick, kept from its dry run")
    gifts = p.add_mutually_exclusive_group()
    gifts.add_argument("--gifts-step-b64", help="the new gift step, base64url")
    gifts.add_argument("--no-gifts-step", action="store_true", help="remove the gift step")
    p.add_argument("--dry-run", action="store_true", help="the plan: read and print only")
    args = p.parse_args(argv)
    try:
        ch = change(args)
        ec2 = Ec2(args.region)
    except (ValueError, KeyError) as exc:
        print(f"set_workshop.py: {exc}", file=sys.stderr)
        return 2
    try:
        return run(ch, ec2, args)
    except Exception as exc:  # botocore's: one line, never a traceback
        print(f"set_workshop.py: {error_line(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
