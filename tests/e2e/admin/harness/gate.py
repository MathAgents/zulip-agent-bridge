"""Every AWS and Zulip call of the rig's setup and teardown, logged, and guarded (issue #356).

`Gate` prints each call as it is made: `read` for a lookup, `write` for a change. In a dry run
(`run.py --dry-run`) the lookups are made, so the plan says what would be created and what is
reused, and every change is only printed, with the placeholder its caller passed (`dry=`) standing
in for AWS's answer. `Aws` wraps boto3's EC2 and IAM clients: a change is sent only for test
infrastructure (resources tagged `zab-role=test`, IAM `zab-test-*`), whatever its caller passed: an
EC2 resource created carries `zab-role=test`; any other EC2 change names the resource it acts on
(`guarded=`, as EC2 described it), which must carry that tag and be the very resource the call
names; an instance terminated or unprotected, an Elastic IP released and a volume deleted (`ENDS`)
carry a test workshop's tag (`workshop=zab-admin-e2e*`, never #263's `zab-test`) and no `zab-admin`
tag (the server's), and a volume deleted is `available`; a network interface deleted is A13's
detached probe (`PROBE_ENI_TAGS`, no `workshop` tag), and `available`; an IAM name starts with
`zab-test-`, and every policy document put passes `check_policy` (EC2 only, scoped to
`zab-role=test`, every deny there). Secrets never reach the log: a parameter named in `HIDDEN` is
shown as its length only. The scenarios read AWS through `ReadOnly`, which refuses changes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from e2e.admin.harness.rig import PROBE_ENI_TAGS, SCOPE, TEST_ROLE, WORKSHOPS

#: Parameters never printed: shown as `<N chars>`.
HIDDEN = frozenset({"PolicyDocument", "AssumeRolePolicyDocument", "api_key", "key", "content"})
READ_PREFIXES = ("describe_", "get_", "list_")
#: The changes that end a resource (with lifting an instance's termination protection): only
#: of a test workshop's fleet (`TEST_WORKSHOPS`), never of the server (`zab-admin`).
ENDS = frozenset({"terminate_instances", "release_address", "delete_volume"})
TEST_WORKSHOPS = frozenset(ws.name for ws in WORKSHOPS)
#: The parameters that name the resource an EC2 change acts on.
SENT_IDS = (
    "InstanceIds", "InstanceId", "GroupId", "Resources", "AllocationId", "KeyPairId", "VolumeId",
    "NetworkInterfaceId",
)  # fmt: skip
#: A described resource's own ID, by kind, in this order (an instance also has an `ImageId`, an
#: address an `InstanceId` and a `NetworkInterfaceId`).
OWN_ID = (
    "KeyPairId", "AllocationId", "GroupId", "VolumeId", "InstanceId", "ImageId",
    "NetworkInterfaceId",
)  # fmt: skip
#: The denies every document of the role must carry, as render_policy.py writes them (ADMIN
#: §5): Sid → (action, resource, condition). DenyScopeRetag's tag keys are checked apart
#: (`_deny_ok`). DenyExistingInterface keeps LaunchNetwork's `network-interface/*` to the one a
#: launch creates: never an existing interface of Drew's account (#356's V4 check).
DENIES: dict[str, tuple[str, str, Any]] = {
    "DenyServer": ("ec2:*", "*", {"Null": {"aws:ResourceTag/zab-admin": "false"}}),
    "DenyServerTag": ("ec2:*", "*", {"Null": {"aws:RequestTag/zab-admin": "false"}}),
    "DenyScopeRetag": ("ec2:CreateTags", "*", None),
    "DenyUntag": ("ec2:DeleteTags", "*", None),
    "DenyExistingInterface": (
        "ec2:RunInstances", "arn:aws:ec2:*:*:network-interface/eni-*", None,
    ),
}  # fmt: skip
#: The keys DenyScopeRetag must keep from a retag: every scope key, and the server's.
RETAG_KEYS = frozenset({"workshop", "zab-scope", "zab-role", "zab-admin"})
#: What an Allow scoped only by a request tag may do: create, and only what each call creates
#: (an `aws:RequestTag` reaches only those). Anything else must carry `aws:ResourceTag`.
CREATES = {
    "ec2:RunInstances": {"instance", "volume"},
    "ec2:AllocateAddress": {"elastic-ip"},
    "ec2:ImportKeyPair": {"key-pair"},
    "ec2:CreateTags": {"instance", "volume", "elastic-ip", "key-pair"},
}
#: The resource types a creating call acts on, where fewer than every one: a type it never
#: touches may share its statement (AllocateAddress beside a key pair, for ImportKeyPair). For
#: RunInstances and CreateTags, which act on any type, every type named must be one they create.
TOUCHES = {
    "ec2:AllocateAddress": {"elastic-ip", "ipv4pool-ec2"},
    "ec2:ImportKeyPair": {"key-pair"},
}
#: The calls a tag may be added at creation by (`ec2:CreateAction`).
CREATE_ACTIONS = frozenset({"RunInstances", "AllocateAddress", "ImportKeyPair"})
#: The IAM changes the rig makes (`AwsRig.ensure_role`): no other is ever sent.
IAM_WRITES = frozenset(
    {
        "create_role",
        "put_role_policy",
        "delete_role_policy",
        "create_instance_profile",
        "add_role_to_instance_profile",
    }
)
#: What an unscoped Allow may grant: describe calls that can't be scoped (§5, "Read"). Not
#: DescribeInstanceAttribute, which returns any instance's user data (#329).
READ_ACTIONS = frozenset(
    f"ec2:Describe{what}"
    for what in (
        "Addresses", "Images", "Instances", "KeyPairs", "SecurityGroups", "Volumes",
        "VolumesModifications",  # `!grow-disk` (#553)
    )
)  # fmt: skip
_EC2_ACTION = re.compile(r"ec2:[A-Z][A-Za-z]+")
_NETWORK = re.compile(r"arn:aws:ec2:[a-z0-9-]+:\*:(subnet|network-interface)/\*")
_ARN = re.compile(r"arn:aws:ec2:[a-z0-9-]+:\*:([a-z-]+)/\*")


class NotTestInfrastructure(RuntimeError):
    """A change aimed at something that is not tagged, or named, as test infrastructure."""


def shown(params: Mapping[str, Any], limit: int = 300) -> str:
    """The parameters as the log shows them: hidden ones by length, the whole cut at `limit`."""
    parts = []
    for key, value in params.items():
        if key in HIDDEN:
            text = f"<{len(str(value))} chars>"
        else:
            text = value if isinstance(value, str) else json.dumps(value, default=str)
        parts.append(f"{key}={text}")
    line = " ".join(parts)
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _listed(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _deny_ok(st: Mapping[str, Any]) -> bool:
    """`st` is the deny its Sid names, as render_policy.py writes it, and nothing less."""
    action, resource, condition = DENIES[st["Sid"]]
    if _listed(st.get("Action")) != [action] or st.get("Resource") != resource:
        return False
    if {"NotAction", "NotResource"} & set(st):
        return False
    if st["Sid"] != "DenyScopeRetag":
        return st.get("Condition") == condition
    cond = st.get("Condition") or {}
    keys = (cond.get("ForAnyValue:StringEqualsIgnoreCase") or {}).get("aws:TagKeys")
    return (
        set(cond) == {"ForAnyValue:StringEqualsIgnoreCase", "Null"}
        and cond["Null"] == {"ec2:CreateAction": "true"}
        and RETAG_KEYS <= {str(k).lower() for k in _listed(keys)}
    )


def check_policy(document: Mapping[str, Any]) -> None:
    """A policy document for the role, refused (`NotTestInfrastructure`) unless (test
    infrastructure: policies limited to resources tagged `zab-role=test`):
    - the five denies are there, each as render_policy.py writes it (`DENIES`);
    - every Allow is EC2 only (named actions, no `NotAction` or `NotResource`) and either an
      unscoped describe, the launch's network interfaces (only the one it creates:
      DenyExistingInterface) and subnets, conditioned on
      `aws:ResourceTag/zab-role = test`, or, conditioned on the request tag alone, a creating
      call on what it creates (`CREATES`, explicit ARNs); either condition beside the rig's
      scope, `zab-scope=admin-e2e` (#422), in the same kind of tag;
    - an Allow of `CreateTags` keyed on a request tag names `ec2:CreateAction`, within the
      creating calls."""
    statements = _listed(document.get("Statement") or [])
    denied = {st.get("Sid"): st for st in statements if st.get("Effect") == "Deny"}
    if set(DENIES) - set(denied):
        raise NotTestInfrastructure(f"the policy lacks {sorted(set(DENIES) - set(denied))}")
    hollow = sorted(sid for sid in DENIES if not _deny_ok(denied[sid]))
    if hollow:
        raise NotTestInfrastructure(f"{hollow}: not the deny render_policy.py writes")
    for st in statements:
        sid = st.get("Sid", "?")
        if st.get("Effect") == "Deny":
            continue
        if st.get("Effect") != "Allow" or {"NotAction", "NotResource", "NotPrincipal"} & set(st):
            raise NotTestInfrastructure(f"{sid}: only Allow and Deny, with Action and Resource")
        actions = [str(a) for a in _listed(st.get("Action"))]
        if not actions or not all(_EC2_ACTION.fullmatch(a) for a in actions):
            raise NotTestInfrastructure(f"{sid}: {actions}: EC2 actions only, each named")
        equals = (st.get("Condition") or {}).get("StringEquals") or {}
        resources = [str(r) for r in _listed(st.get("Resource"))]
        network = actions == ["ec2:RunInstances"] and all(_NETWORK.fullmatch(r) for r in resources)
        for kind in ("ResourceTag", "RequestTag"):
            role = equals.get(f"aws:{kind}/{TEST_ROLE[0]}") == TEST_ROLE[1]
            if role and equals.get(f"aws:{kind}/{SCOPE[0]}") != SCOPE[1]:
                raise NotTestInfrastructure(f"{sid}: not scoped by {kind} {'='.join(SCOPE)}")
        if equals.get("aws:ResourceTag/zab-role") == "test":
            pass  # every resource it acts on already carries the test tag
        elif equals.get("aws:RequestTag/zab-role") == "test":
            if not set(actions) <= set(CREATES):
                raise NotTestInfrastructure(f"{sid}: {actions} by a request tag alone")
            kinds = {m.group(1) if (m := _ARN.fullmatch(r)) else "" for r in resources}
            if not kinds or not kinds <= set().union(*(CREATES[a] for a in actions)):
                raise NotTestInfrastructure(f"{sid}: {resources}: not what {actions} create")
            for action in actions:  # each on its own: RunInstances never on a key pair
                touched = kinds & TOUCHES[action] if action in TOUCHES else kinds
                if not touched <= CREATES[action]:
                    raise NotTestInfrastructure(f"{sid}: {action} on {sorted(touched)}")
            if "ec2:RunInstances" in actions and "instance" in kinds:
                if "ec2:InstanceType" not in equals:  # the code under test picks no type
                    raise NotTestInfrastructure(f"{sid}: RunInstances of any instance type")
        elif not (network or set(actions) <= READ_ACTIONS):
            raise NotTestInfrastructure(f"{sid}: {actions} without a zab-role=test condition")
        keyed = any(str(k).startswith("aws:RequestTag/") for k in equals)
        if "ec2:CreateTags" in actions and keyed:
            calls = {str(c) for c in _listed(equals.get("ec2:CreateAction") or [])}
            if not calls or not calls <= CREATE_ACTIONS:
                raise NotTestInfrastructure(f"{sid}: CreateTags by request tag, at {calls}")


class Gate:
    def __init__(self, *, dry_run: bool, out: Callable[[str], None] = print) -> None:
        self.dry_run, self.out = dry_run, out
        self.writes: list[str] = []  # every change, made or (dry run) planned

    def note(self, text: str) -> None:
        self.out(f"# {text}")

    def read(self, what: str) -> None:
        self.out(f"  read   {what}")

    def write(self, what: str) -> bool:
        """Log a change; True when it is to be made (not a dry run)."""
        self.writes.append(what)
        self.out(f"  {'would ' if self.dry_run else 'WRITE  '}{what}")
        return not self.dry_run


class ReadOnly(Gate):
    """The scenarios' view of AWS: lookups only; any change is refused before it is sent."""

    def __init__(self) -> None:
        super().__init__(dry_run=False, out=lambda line: None)

    def write(self, what: str) -> bool:
        raise NotTestInfrastructure(f"the scenarios only read AWS: {what}")


def tags_of(resource: Mapping[str, Any]) -> dict[str, str]:
    """A resource's tags as EC2 or IAM described it: under `Tags`, or `TagSet` for a network
    interface (`DescribeNetworkInterfaces`; Codex round 1 on #414)."""
    found = resource.get("Tags") or resource.get("TagSet") or []
    return {t["Key"]: t["Value"] for t in found}


def require_test(resource: Mapping[str, Any], *, what: str = "") -> None:
    """`resource` (as EC2 or IAM described it) carries `zab-role=test`; else nothing is sent."""
    if tags_of(resource).get(TEST_ROLE[0]) != TEST_ROLE[1]:
        ids = ("InstanceId", "GroupId", "NetworkInterfaceId")
        name = what or next((str(resource[k]) for k in ids if resource.get(k)), "?")
        raise NotTestInfrastructure(f"{name} is not tagged zab-role=test: left alone")


def _check_change(service: str, op: str, params: Mapping[str, Any], guarded: Any) -> None:
    if service == "ec2" and "TagSpecifications" not in params:
        if guarded is None:
            raise NotTestInfrastructure("an EC2 change names no resource it checked")
        require_test(guarded)
        own = next((str(guarded[k]) for k in OWN_ID if guarded.get(k)), None)
        sent = {str(v) for key in SENT_IDS if key in params for v in _listed(params[key])}
        if not sent or sent != {own}:
            raise NotTestInfrastructure(f"{op} names {sorted(sent)}; {own} was checked")
        unprotect = (params.get("DisableApiTermination") or {}).get("Value") is False
        if op in ENDS or unprotect:
            tags = tags_of(guarded)
            if "zab-admin" in tags:
                raise NotTestInfrastructure(f"{own} is the admin server's: never {op}")
            if tags.get("workshop") not in TEST_WORKSHOPS:
                where = f"workshop={tags['workshop']}" if "workshop" in tags else "no workshop"
                raise NotTestInfrastructure(f"{own} is of {where}, not a test one: never {op}")
        if op == "delete_volume" and guarded.get("State") != "available":
            raise NotTestInfrastructure(f"{own} is {guarded.get('State')}: deleted only available")
        if op == "delete_network_interface":  # only A13's probe, which nothing ever attaches
            tags = tags_of(guarded)
            if any(tags.get(k) != v for k, v in PROBE_ENI_TAGS.items()) or "workshop" in tags:
                raise NotTestInfrastructure(f"{own} is not A13's probe interface: never deleted")
            if guarded.get("Status") != "available":
                status = guarded.get("Status")
                raise NotTestInfrastructure(f"{own} is {status}: deleted only available")
    if service == "iam":
        if op not in IAM_WRITES:
            raise NotTestInfrastructure(f"IAM {op}: not one of the rig's {sorted(IAM_WRITES)}")
        for key in ("RoleName", "InstanceProfileName", "PolicyName"):
            if key in params and not str(params[key]).startswith("zab-test-"):
                raise NotTestInfrastructure(f"IAM {key} {params[key]!r}: only zab-test-* names")
        if op == "put_role_policy":
            check_policy(json.loads(params["PolicyDocument"]))
    for spec in params.get("TagSpecifications", []):
        if {t["Key"]: t["Value"] for t in spec.get("Tags", [])}.get(TEST_ROLE[0]) != TEST_ROLE[1]:
            raise NotTestInfrastructure(f"{spec.get('ResourceType')} created without zab-role=test")


class Aws:
    """EC2 and IAM through `Gate`: `aws.ec2("describe_instances", Filters=…)`."""

    def __init__(self, gate: Gate, ec2: Any, iam: Any) -> None:
        self.gate, self.clients = gate, {"ec2": ec2, "iam": iam}

    def ec2(self, op: str, dry: Any = None, guarded: Any = None, **params: Any) -> Any:
        return self._call("ec2", op, dry, guarded, params)

    def iam(self, op: str, dry: Any = None, **params: Any) -> Any:
        return self._call("iam", op, dry, None, params)

    def _call(self, service: str, op: str, dry: Any, guarded: Any, params: dict) -> Any:
        what = f"{service} {op} {shown(params)}".rstrip()
        client = self.clients[service]
        if op.startswith(READ_PREFIXES):
            self.gate.read(what)
            return getattr(client, op)(**params)
        _check_change(service, op, params, guarded)
        if not self.gate.write(what):
            return {} if dry is None else dry
        return getattr(client, op)(**params)


def error_code(exc: BaseException) -> str:
    """botocore's `ClientError` code (`InvalidGroup.NotFound`), or "" for anything else."""
    response = getattr(exc, "response", None)
    return str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""
