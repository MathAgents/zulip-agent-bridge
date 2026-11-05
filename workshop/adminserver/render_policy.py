#!/usr/bin/env python3
"""Print the admin server's instance-role policy for the fleet of admin.toml (ADMIN §5).

    python3 workshop/adminserver/render_policy.py --config admin.toml --out-dir policies/

The role may act only on resources that carry the fleet's scope, `[global]`'s `zab-scope=<scope>`
and extra tags, and only in `[global] regions` (Drew, Oct 16: each region is set up once, so a
new workshop changes nothing here). Everything it creates also names a workshop (`workshop=…`,
any value). It may never act on anything tagged `zab-admin` (the server, its address, its
security group), may never add, change or remove a scope tag after creation, and may never
launch with an existing network interface (another service's address and groups). IAM doesn't
tell one workshop's resources from another's: the server's code does (ADMIN §5). The output
names no account (`arn:aws:ec2:<region>:*:…`). When one managed policy can't hold it, it is
split into several, each with every deny.

Stdlib only, as the rest of `workshop/` is. The bot's loader is `zab.admin.config`; a test checks
that the two read admin.toml alike.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

SERVER_TAG = "zab-admin"  # the server's own tag key, never part of a scope (ADMIN §1.1)
SCOPE_KEY = "zab-scope"  # the fleet-wide scope's key; its value is `[global] scope`
WORKSHOP_KEY = "workshop"  # every resource the role creates names its workshop
#: `[global]`'s keys: the fleet's scope, then the global admin bot's (zab.admin.config's too)
GLOBAL_KEYS = frozenset(
    {"scope", "regions", "instance_types", "tags", "zuliprc", "user_id", "channels", "email"}
)
RETAG_KEYS = ["Name", "participant"]  # all adopt_spare.py may set on a live resource
TAGGED = ["elastic-ip", "instance", "volume"]  # what launch.py tags, and adopt_spare.py retags
CREATE_ACTIONS = ["AllocateAddress", "ImportKeyPair", "RunInstances"]
#: On `*`, unscoped. `Volumes` and `VolumesModifications` for `!grow-disk` (#553), and the
#: cost estimate's volume sizes.
READ = [
    "Addresses", "Images", "Instances", "KeyPairs", "SecurityGroups", "Volumes",
    "VolumesModifications",
]  # fmt: skip
# DescribeInstanceAttribute is scoped to the fleet: it can return any instance's user data.
INSTANCE = ["DescribeInstanceAttribute", "GetConsoleOutput", "ModifyInstanceAttribute"] + [
    f"{verb}Instances" for verb in ("Start", "Stop", "Terminate")
]
ADDRESS = ["AssociateAddress", "DisassociateAddress", "ReleaseAddress"]
NETWORK = ["network-interface", "subnet"]  # chosen or created by RunInstances, never tagged
#: An existing interface, as RunInstances names it: by its own ID. The one a launch creates has no
#: ID yet, so EC2 checks it as `network-interface/*`, which this never matches (ADMIN §5, #356).
EXISTING_INTERFACE = "arn:aws:ec2:*:*:network-interface/eni-*"
MANAGED_LIMIT = 6144  # IAM's limit for one managed policy, whitespace not counted
ROLE_QUOTA = 10  # IAM's default for managed policies per role; AWS raises it to 20 on request
_DOCUMENT = re.compile(r"zab-admin-([1-9][0-9]*)\.json", re.ASCII)  # what --out-dir writes

# As in zab.admin.config (PR 5a); tests/workshop/test_adminserver_policy.py compares them.
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", re.ASCII)
_TAG_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,127}", re.ASCII)
_TAG_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+@-]{0,255}", re.ASCII)
_REGION = re.compile(r"[a-z]{2}(?:-[a-z]+)+-[0-9]{1,2}", re.ASCII)
_INSTANCE_TYPE = re.compile(r"[a-z][a-z0-9-]*\.[a-z0-9]+", re.ASCII)


class PolicyError(ValueError):
    pass


def fleet(config: dict[str, Any]) -> dict[str, Any]:
    """`[global]`'s scope tags (`zab-scope=<scope>`, then the extra ones), regions and instance
    types, validated as the bot's loader does. The workshops don't enter the policy."""
    glob = config.get("global")
    if not isinstance(glob, dict) or "scope" not in glob:
        raise PolicyError(
            "[global] scope is required: since v0.2.5 the role is scoped by the fleet-wide tag "
            "zab-scope (docs/ADMIN.md §5; to migrate a v0.2.4 admin.toml, CHANGELOG.md 0.2.5's "
            "upgrade note)"
        )
    unknown = sorted(set(glob) - GLOBAL_KEYS)  # a typo never widens the policy
    if unknown:
        raise PolicyError(f"[global]: unknown key {unknown[0]!r}")
    scope, regions, types = glob["scope"], glob.get("regions"), glob.get("instance_types")
    if not isinstance(scope, str) or not _NAME.fullmatch(scope):
        raise PolicyError(f"[global] scope: {scope!r}")
    checks = {"regions": (regions, _REGION), "instance_types": (types, _INSTANCE_TYPE)}
    for key, (values, pattern) in checks.items():
        if (
            not isinstance(values, list)
            or not values
            or len(set(values)) != len(values)
            or not all(isinstance(v, str) and pattern.fullmatch(v) for v in values)
        ):
            raise PolicyError(f"[global] {key}: {values!r}")
    tags = {SCOPE_KEY: scope} | _extra(glob.get("tags", []))
    return {"tags": tags, "regions": regions, "types": types}


def _extra(raw: Any) -> dict[str, str]:
    """`[global] tags`. IAM matches the key in `aws:ResourceTag/<key>` regardless of case, so
    keys compare in lower case: a reserved key in any case is refused, and so are two keys
    differing only so."""
    tags: dict[str, str] = {}
    reserved = {key.lower() for key in (*RETAG_KEYS, SERVER_TAG, SCOPE_KEY, WORKSHOP_KEY)}
    for spec in raw if isinstance(raw, list) else [None]:
        key, _, value = spec.partition("=") if isinstance(spec, str) else ("", "", "")
        low = key.lower()
        taken = low in reserved or low.startswith("aws:") or low in {k.lower() for k in tags}
        if not _TAG_KEY.fullmatch(key) or not _TAG_VALUE.fullmatch(value) or taken:
            raise PolicyError(f"[global] tags {raw!r}: expected KEY=VALUE extra tags, each once")
        tags[key] = value
    return tags


def _one(values: list[str]) -> str | list[str]:
    return values[0] if len(values) == 1 else values


def _arns(region: str, *kinds: str) -> str | list[str]:
    return _one([f"arn:aws:ec2:{region}:*:{kind}/*" for kind in sorted(kinds)])


def _statement(sid: str, effect: str, action: Any, resource: Any, condition: Any) -> dict:
    statement = {"Sid": sid, "Effect": effect, "Action": action, "Resource": resource}
    return statement | ({"Condition": condition} if condition else {})


def _group(sid: str, region: str, scope: dict[str, str], types: list[str]) -> list[dict]:
    """The statements of one region: `scope` is every scope tag (`zab-scope`, then the extra
    ones), `types` the instance types a launch may ask for. What the role creates must also
    name a workshop, any one (`Null`); what it acts on afterwards must too."""
    has = {"StringEquals": {f"aws:ResourceTag/{k}": v for k, v in scope.items()}}
    has["Null"] = {f"aws:ResourceTag/{WORKSHOP_KEY}": "false"}
    asks = {f"aws:RequestTag/{k}": v for k, v in scope.items()}
    named = {"Null": {f"aws:RequestTag/{WORKSHOP_KEY}": "false"}}
    launch = asks | {"ec2:InstanceType": _one(types)}
    created = asks | {"ec2:CreateAction": CREATE_ACTIONS}
    # The image, the group and the key pair: the account owner's and the bootstrap's, scoped
    # but in no workshop (a region is set up once), so without the `workshop` condition.
    source = {"StringEquals": has["StringEquals"]}
    only_retag_keys = {"aws:TagKeys": RETAG_KEYS}
    rows = [
        ("Launch", ["RunInstances"], ["instance"], {"StringEquals": launch} | named),
        ("LaunchVolume", ["RunInstances"], ["volume"], {"StringEquals": asks} | named),
        ("LaunchFrom", ["RunInstances"], ["image", "key-pair", "security-group"], source),
        ("Create", CREATE_ACTIONS[:2], ["elastic-ip", "key-pair"], {"StringEquals": asks} | named),
        ("TagAtCreation", ["CreateTags"], [*TAGGED, "key-pair"], {"StringEquals": created} | named),
        ("Retag", ["CreateTags"], TAGGED, has | {"ForAllValues:StringEquals": only_retag_keys}),
        # No action here applies to the other's resource type, except AssociateAddress (both).
        ("Fleet", INSTANCE + ADDRESS, ["elastic-ip", "instance"], has),
        # `!grow-disk` (#553): a fleet VM's volume, as launch.py tags it (`LaunchVolume`)
        ("Volume", ["ModifyVolume"], ["volume"], has),
    ]
    return [
        _statement(sid + name, "Allow", _one([f"ec2:{a}" for a in acts]), _arns(region, *kinds), c)
        for name, acts, kinds, c in rows
    ]


def render(config: dict[str, Any]) -> dict[str, Any]:
    found = fleet(config)
    regions = sorted(found["regions"])
    scope_keys = sorted({*found["tags"], WORKSHOP_KEY, SERVER_TAG})
    read = [f"ec2:Describe{what}" for what in READ]
    in_regions = {"StringEquals": {"aws:RequestedRegion": _one(regions)}}
    # EC2 tag keys are case-sensitive, IAM's tag condition keys are not: `Workshop` counts too.
    retag_scope = {"ForAnyValue:StringEqualsIgnoreCase": {"aws:TagKeys": scope_keys}}
    denies = [
        ("DenyServer", "ec2:*", {"Null": {f"aws:ResourceTag/{SERVER_TAG}": "false"}}),
        ("DenyServerTag", "ec2:*", {"Null": {f"aws:RequestTag/{SERVER_TAG}": "false"}}),
        ("DenyScopeRetag", "ec2:CreateTags", retag_scope | {"Null": {"ec2:CreateAction": "true"}}),
        ("DenyUntag", "ec2:DeleteTags", None),
    ]
    statements = [_statement(sid, "Deny", act, "*", c) for sid, act, c in denies]
    # LaunchNetwork's `network-interface/*` would otherwise attach any detached interface of the
    # account, with its private address and security groups, to a VM the server controls.
    attach = ("DenyExistingInterface", "Deny", "ec2:RunInstances", EXISTING_INTERFACE, None)
    statements.append(_statement(*attach))
    statements.append(_statement("Read", "Allow", read, "*", in_regions))  # denies first: split()
    for region in regions:
        sid = "".join(part.capitalize() for part in region.split("-"))
        statements += _group(sid, region, found["tags"], found["types"])
    network = [f"arn:aws:ec2:{region}:*:{kind}/*" for region in regions for kind in NETWORK]
    statements.append(_statement("LaunchNetwork", "Allow", "ec2:RunInstances", network, None))
    return {"Version": "2012-10-17", "Statement": statements}


def size(policy: dict[str, Any]) -> int:
    """IAM's measure: characters, not counting whitespace outside strings."""
    return len(json.dumps(policy, separators=(",", ":")))


def split(policy: dict[str, Any], limit: int = MANAGED_LIMIT) -> list[dict[str, Any]]:
    """The allows, in order, packed into as few documents as fit `limit` each, with every deny
    repeated in each one. A role's permissions are the union of its policies' allows less every
    deny, and a document attached without the others must not grant anything without them."""
    denies = [st for st in policy["Statement"] if st["Effect"] == "Deny"]
    docs: list[list[dict[str, Any]]] = [list(denies)]
    for statement in policy["Statement"]:
        if statement["Effect"] == "Deny":
            continue
        longer = {**policy, "Statement": [*docs[-1], statement]}
        if len(docs[-1]) > len(denies) and size(longer) > limit:
            docs.append(list(denies))
        docs[-1].append(statement)
    found = [{**policy, "Statement": statements} for statements in docs]
    if any(size(doc) > limit for doc in found):
        raise PolicyError(f"a statement too long for IAM's {limit} characters with the denies")
    if any(doc["Statement"][: len(denies)] != denies for doc in found):
        raise PolicyError("a document without every deny")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the admin server's IAM policy (ADMIN §5).")
    parser.add_argument("--config", type=Path, default=Path("/etc/zab-admin/admin.toml"))
    parser.add_argument("--out-dir", type=Path, help="write zab-admin-N.json, one policy each")
    args = parser.parse_args(argv)
    try:
        docs = split(render(tomllib.loads(args.config.read_text(encoding="utf-8"))))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, PolicyError) as exc:
        print(f"render_policy: {args.config}: {exc}", file=sys.stderr)
        return 2
    if args.out_dir is None:
        if len(docs) > 1:
            print(f"render_policy: {len(docs)} policies needed: pass --out-dir", file=sys.stderr)
            return 2
        print(json.dumps(docs[0], indent=2))
        return 0
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for i, doc in enumerate(docs, start=1):
        path = args.out_dir / f"zab-admin-{i}.json"
        path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"{path}: {size(doc)} characters")
    for path in sorted(args.out_dir.iterdir()):
        found = _DOCUMENT.fullmatch(path.name)
        if found and int(found[1]) > len(docs):  # left by a render that needed more
            path.unlink()
            print(f"{path}: removed: detach and delete the managed policy {path.stem}")
    if len(docs) > ROLE_QUOTA:
        print(
            f"render_policy: {len(docs)} policies, over IAM's default of {ROLE_QUOTA} managed "
            "policies per role: ask AWS to raise the role's quota (up to 20) first",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
