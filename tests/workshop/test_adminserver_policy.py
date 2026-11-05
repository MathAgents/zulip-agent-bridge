"""The admin server's instance-role policy (docs/ADMIN.md §5; issues #324, #422): golden output
for a fleet in one region and in two, no account anywhere, and its meaning, checked by a small
evaluator of the IAM rules it uses: explicit deny wins, then any allow, else denied. The scope
is the fleet-wide tag `zab-scope` (Drew, Oct 16), so the policy depends on `[global]` alone."""

import copy
import fnmatch
import json
import pathlib
import re
import tomllib

import pytest
import render_policy

HERE = pathlib.Path(__file__).parent
SERVER = HERE.parents[1] / "workshop" / "adminserver"
EXAMPLE = tomllib.loads((SERVER / "admin.example.toml").read_text())
GOLDEN = HERE / "adminserver"
H, N = "harvard-2026", "nt-agents-2026"  # us-east-2; us-west-2
S = "mathagents"  # the example's [global] scope


def one_region() -> dict:
    """The example's fleet in us-east-2 alone, as harvard-2026's only workshop."""
    config = copy.deepcopy(EXAMPLE)
    config["workshop"] = config["workshop"][:1]
    config["global"]["regions"] = ["us-east-2"]
    return config


def test_no_workshop_enters_the_policy():
    """A new workshop in a region that is set up changes nothing for IAM (§2.9)."""
    config = copy.deepcopy(EXAMPLE)
    config["workshop"] = [config["workshop"][0] | {"name": "mit-2027"}]
    assert render_policy.render(config) == render_policy.render(EXAMPLE)
    assert H not in json.dumps(render_policy.render(EXAMPLE))


@pytest.mark.parametrize(
    ("config", "golden"), [(one_region(), "policy-one.json"), (EXAMPLE, "policy-two.json")]
)
def test_golden(config, golden):
    rendered = json.dumps(render_policy.render(config), indent=2) + "\n"
    assert rendered == (GOLDEN / golden).read_text()


def test_no_account_in_the_output_or_the_server_files():
    text = json.dumps(render_policy.render(EXAMPLE))
    assert not re.search(r"\d{12}", text)
    arns = re.findall(r'"(arn:[^"]*)"', text)
    arn = re.compile(r"arn:aws:ec2:[a-z]+-[a-z]+-\d:\*:[a-z-]+/\*")
    assert arns and all(arn.fullmatch(a) for a in arns if a != render_policy.EXISTING_INTERFACE)
    assert render_policy.EXISTING_INTERFACE == "arn:aws:ec2:*:*:network-interface/eni-*"
    for path in SERVER.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            assert not re.search(r"(?<!\d)\d{12}(?!\d)", path.read_text()), path


def test_the_example_loads_with_the_bots_loader():
    """`admin.example.toml` loads with PR 5a's loader, and render_policy reads it alike."""
    config = pytest.importorskip("zab.admin.config", reason="zab.admin.config (PR 5a, #321)")
    loaded = config.load_admin_config(SERVER / "admin.example.toml")
    assert [ws.name for ws in loaded.workshops] == [H, N]
    assert loaded.global_bot is not None
    assert {
        "tags": dict(loaded.scope_tags),
        "regions": list(loaded.regions),
        "types": list(loaded.instance_types),
    } == render_policy.fleet(EXAMPLE)
    for ws in loaded.workshops:  # every workshop's scope: its name, then the fleet's
        assert ws.tags == (("workshop", ws.name), *loaded.scope_tags)
        assert ws.aws.region in loaded.regions
        assert set(ws.aws.instance_types) <= set(loaded.instance_types)


def decide(policy: dict, action: str, resource: str, context: dict) -> str:
    effects = set()
    for st in policy["Statement"]:
        actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
        resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
        if (
            any(fnmatch.fnmatch(action.lower(), a.lower()) for a in actions)
            and any(fnmatch.fnmatchcase(resource, r) for r in resources)
            and holds(st.get("Condition", {}), context)
        ):
            effects.add(st["Effect"])
    return "deny" if "Deny" in effects else "allow" if "Allow" in effects else "implicit"


def holds(condition: dict, context: dict) -> bool:
    """Condition key names match regardless of case, as in IAM (`aws:RequestTag/Workshop`)."""
    folded = {key.lower(): value for key, value in context.items()}
    return all(
        satisfied(
            operator, wanted if isinstance(wanted, list) else [wanted], folded.get(key.lower())
        )
        for operator, keys in condition.items()
        for key, wanted in keys.items()
    )


def satisfied(operator: str, wanted: list, have) -> bool:
    if operator == "Null":
        return (have is None) == (wanted == ["true"])
    fold = (lambda v: v.lower()) if operator.endswith("IgnoreCase") else (lambda v: v)
    wanted = [fold(w) for w in wanted]
    if operator in ("StringEquals", "StringEqualsIgnoreCase"):  # a missing key never equals
        return have is not None and fold(have) in wanted
    values = [fold(v) for v in have or []]
    if operator.startswith("ForAllValues:"):  # true of an empty or missing set
        return all(v in wanted for v in values)
    assert operator.startswith("ForAnyValue:"), operator
    return any(v in wanted for v in values)


def ask(policy, action, region, kind, *, tags=None, request=None, create=None, itype=None,
        rid="x-0123"):  # fmt: skip
    """One request: `kind` names the resource type (`*` for describe calls), `rid` its ID, `*`
    for one the call creates (EC2 knows no ID before it exists: an instance or interface that
    RunInstances creates is `instance/*`, `network-interface/*`)."""
    context: dict = {"aws:RequestedRegion": region}
    context |= {f"aws:ResourceTag/{k}": v for k, v in (tags or {}).items()}
    context |= {f"aws:RequestTag/{k}": v for k, v in (request or {}).items()}
    if request:
        context["aws:TagKeys"] = list(request)
    if create:
        context["ec2:CreateAction"] = create
    if itype:
        context["ec2:InstanceType"] = itype
    account = "" if kind == "image" else "ACCOUNT"  # an AMI's ARN names no account
    arn = "*" if kind == "*" else f"arn:aws:ec2:{region}:{account}:{kind}/{rid}"
    return decide(policy, f"ec2:{action}", arn, context)


EAST, WEST = "us-east-2", "us-west-2"
HTAG, NTAG = {"workshop": H, "zab-scope": S}, {"workshop": N, "zab-scope": S}
SCOPED = {"zab-scope": S}  # the region's image and group, the account owner's: in no workshop
OLD = {"workshop": H}  # a v0.2.4 fleet, before the v0.2.5 policy change tagged it
FOREIGN = {"workshop": H, "zab-scope": "other"}  # another server's fleet in the account
ANY_WORKSHOP = SCOPED | {"workshop": "any"}  # IAM checks that a workshop is named, not which
SERVER_TAGS = HTAG | {"zab-admin": "admin-1"}  # even if someone tags the server by hand
LAUNCH = HTAG | {"Name": "bob", "participant": "bob"}
M7A = "m7a.2xlarge"
PARTICIPANT_AND_OWNER = {"participant": "x", "owner": "y"}  # every key must be retaggable
CASES = [
    # the fleet, in its regions
    ("allow", "StartInstances", EAST, "instance", {"tags": HTAG}),
    ("allow", "StopInstances", WEST, "instance", {"tags": NTAG}),
    ("allow", "TerminateInstances", EAST, "instance", {"tags": HTAG}),
    ("allow", "GetConsoleOutput", EAST, "instance", {"tags": HTAG}),
    ("allow", "ModifyInstanceAttribute", EAST, "instance", {"tags": HTAG}),
    ("allow", "DescribeInstanceAttribute", EAST, "instance", {"tags": HTAG}),
    ("allow", "RunInstances", EAST, "instance", {"request": LAUNCH, "itype": M7A}),
    ("allow", "RunInstances", EAST, "volume", {"request": LAUNCH}),
    ("allow", "RunInstances", EAST, "image", {"tags": SCOPED}),
    ("allow", "RunInstances", EAST, "security-group", {"tags": SCOPED}),
    ("allow", "RunInstances", EAST, "key-pair", {"tags": HTAG}),
    ("allow", "RunInstances", EAST, "subnet", {}),
    ("allow", "RunInstances", EAST, "network-interface", {"rid": "*"}),  # the one it creates
    ("allow", "RunInstances", WEST, "network-interface", {"rid": "*"}),
    ("allow", "CreateTags", EAST, "instance", {"request": LAUNCH, "create": "RunInstances"}),
    ("allow", "CreateTags", EAST, "instance", {"tags": HTAG, "request": {"participant": "eve"}}),
    ("allow", "AllocateAddress", EAST, "elastic-ip", {"request": HTAG}),
    ("allow", "AssociateAddress", EAST, "elastic-ip", {"tags": HTAG}),
    ("allow", "ReleaseAddress", WEST, "elastic-ip", {"tags": NTAG}),
    ("allow", "ImportKeyPair", EAST, "key-pair", {"request": HTAG}),
    ("allow", "CreateTags", EAST, "key-pair", {"request": HTAG, "create": "ImportKeyPair"}),
    ("allow", "DescribeInstances", WEST, "*", {}),
    # !grow-disk (#553): a fleet VM's volume, as launch.py tags it; its reads, unscoped
    ("allow", "ModifyVolume", EAST, "volume", {"tags": HTAG}),
    ("allow", "ModifyVolume", WEST, "volume", {"tags": NTAG | {"participant": "bob"}}),
    ("allow", "DescribeVolumes", EAST, "*", {}),
    ("allow", "DescribeVolumesModifications", WEST, "*", {}),
    ("implicit", "DescribeVolumes", "eu-central-1", "*", {}),
    ("implicit", "ModifyVolume", EAST, "volume", {}),
    ("implicit", "ModifyVolume", EAST, "volume", {"tags": OLD}),
    ("implicit", "ModifyVolume", EAST, "volume", {"tags": FOREIGN}),
    ("implicit", "ModifyVolume", EAST, "volume", {"tags": SCOPED}),
    ("implicit", "ModifyVolume", "us-east-1", "volume", {"tags": HTAG}),
    ("implicit", "ModifyVolume", EAST, "instance", {"tags": HTAG}),
    ("deny", "ModifyVolume", EAST, "volume", {"tags": SERVER_TAGS}),
    # IAM tells no workshop from another (§5): the server's code does, and A15 tests it
    ("allow", "StartInstances", EAST, "instance", {"tags": NTAG}),
    ("allow", "RunInstances", WEST, "instance", {"request": LAUNCH, "itype": M7A}),
    # outside the scope, its regions or its instance types
    ("implicit", "StartInstances", EAST, "instance", {"tags": OLD}),
    ("implicit", "StartInstances", EAST, "instance", {"tags": FOREIGN}),
    ("implicit", "TerminateInstances", EAST, "instance", {}),
    ("implicit", "AssociateAddress", EAST, "elastic-ip", {"tags": OLD}),
    ("implicit", "RunInstances", "us-east-1", "instance", {"request": LAUNCH, "itype": M7A}),
    ("implicit", "RunInstances", EAST, "instance", {"request": LAUNCH | FOREIGN, "itype": M7A}),
    ("implicit", "RunInstances", EAST, "image", {}),
    ("implicit", "RunInstances", EAST, "image", {"tags": OLD}),
    ("implicit", "RunInstances", EAST, "security-group", {"tags": {"zab-scope": "other"}}),
    ("implicit", "RunInstances", EAST, "volume", {}),
    ("implicit", "RunInstances", EAST, "key-pair", {}),
    ("implicit", "AllocateAddress", "us-east-1", "elastic-ip", {"request": HTAG}),
    ("implicit", "CreateTags", EAST, "instance", {"request": OLD, "create": "RunInstances"}),
    ("implicit", "DescribeInstances", "eu-central-1", "*", {}),
    ("implicit", "RunInstances", EAST, "instance", {"request": LAUNCH, "itype": "p5.48xlarge"}),
    # everything the role makes names a workshop, and everything it acts on does too
    ("implicit", "RunInstances", EAST, "instance", {"request": SCOPED, "itype": M7A}),
    ("implicit", "RunInstances", EAST, "volume", {"request": SCOPED}),
    ("implicit", "AllocateAddress", EAST, "elastic-ip", {"request": SCOPED}),
    ("implicit", "ImportKeyPair", EAST, "key-pair", {"request": SCOPED}),
    ("implicit", "CreateTags", EAST, "instance", {"request": SCOPED, "create": "RunInstances"}),
    ("implicit", "StopInstances", EAST, "instance", {"tags": SCOPED}),
    ("implicit", "CreateTags", EAST, "instance", {"tags": SCOPED, "request": {"Name": "x"}}),
    ("allow", "RunInstances", EAST, "instance", {"request": ANY_WORKSHOP, "itype": M7A}),
    # user data: only the fleet's instances, never another workload's
    ("implicit", "DescribeInstanceAttribute", EAST, "instance", {}),
    ("implicit", "DescribeInstanceAttribute", EAST, "*", {}),
    # never the server, nor a resource tagged as the server
    ("deny", "StopInstances", EAST, "instance", {"tags": SERVER_TAGS}),
    ("deny", "TerminateInstances", EAST, "instance", {"tags": SERVER_TAGS}),
    ("deny", "ModifyInstanceAttribute", EAST, "instance", {"tags": SERVER_TAGS}),
    ("deny", "DescribeInstanceAttribute", EAST, "instance", {"tags": SERVER_TAGS}),
    ("deny", "DisassociateAddress", EAST, "elastic-ip", {"tags": SERVER_TAGS}),
    ("deny", "RunInstances", EAST, "security-group", {"tags": SERVER_TAGS}),
    ("deny", "RunInstances", EAST, "instance", {"request": LAUNCH | {"zab-admin": "x"}}),
    ("deny", "CreateTags", EAST, "instance", {"tags": SERVER_TAGS, "request": {"Name": "x"}}),
    # never an existing interface: another service's address and groups (#356's V4 check)
    ("deny", "RunInstances", EAST, "network-interface", {"rid": "eni-0123456789abcdef0"}),
    ("deny", "RunInstances", WEST, "network-interface", {"rid": "eni-01234567"}),
    ("deny", "RunInstances", "eu-west-1", "network-interface", {"rid": "eni-01234567"}),
    ("deny", "RunInstances", EAST, "network-interface", {"rid": "eni-1", "tags": HTAG}),
    # tags never move: no scope or server key after creation, no other key, no removal; a
    # foreign resource is never retagged into the scope, nor a v0.2.4 one (its migration,
    # CHANGELOG 0.2.5's upgrade note, is the account owner's)
    ("deny", "CreateTags", EAST, "instance", {"tags": HTAG, "request": {"workshop": N}}),
    ("deny", "CreateTags", EAST, "volume", {"tags": HTAG, "request": {"Name": "x", "workshop": H}}),
    ("deny", "CreateTags", EAST, "instance", {"tags": HTAG, "request": {"zab-admin": "x"}}),
    ("deny", "CreateTags", EAST, "instance", {"request": {"workshop": H}}),
    ("deny", "CreateTags", EAST, "instance", {"request": {"zab-scope": S}}),
    ("deny", "CreateTags", EAST, "instance", {"tags": OLD, "request": {"zab-scope": S}}),
    ("deny", "CreateTags", EAST, "instance", {"tags": FOREIGN, "request": {"zab-scope": S}}),
    ("deny", "DeleteTags", EAST, "instance", {"tags": HTAG, "request": {"participant": "x"}}),
    ("implicit", "CreateTags", EAST, "instance", {"tags": HTAG, "request": {"owner": "x"}}),
    ("implicit", "CreateTags", EAST, "instance", {"tags": HTAG, "request": PARTICIPANT_AND_OWNER}),
    # in any case: IAM's tag condition keys ignore it, so `Workshop` is a scope key too
    ("deny", "CreateTags", EAST, "instance", {"tags": HTAG, "request": {"Workshop": N}}),
    ("deny", "CreateTags", EAST, "instance", {"tags": HTAG, "request": {"ZAB-ADMIN": "x"}}),
    ("deny", "CreateTags", EAST, "instance", {"tags": OLD, "request": {"Zab-Scope": S}}),
    ("implicit", "CreateTags", EAST, "instance", {"request": {"participant": "x"}}),
    # nothing else
    ("implicit", "CreateSecurityGroup", EAST, "security-group", {"request": HTAG}),
    ("implicit", "AuthorizeSecurityGroupIngress", EAST, "security-group", {"tags": SCOPED}),
    ("implicit", "CreateImage", EAST, "instance", {"tags": HTAG}),
]


@pytest.mark.parametrize(("want", "action", "region", "kind", "how"), CASES)
def test_what_the_role_may_do(want, action, region, kind, how):
    assert ask(render_policy.render(EXAMPLE), action, region, kind, **how) == want


def test_the_test_server_scope_needs_every_tag():
    """The rig's `[global] tags = ["zab-role=test"]`: in every condition, and never retagged."""
    config = copy.deepcopy(EXAMPLE)
    config["global"] |= {"scope": "admin-e2e", "tags": ["zab-role=test"]}
    policy = render_policy.render(config)
    rig = {"workshop": "zab-admin-e2e", "zab-scope": "admin-e2e"}
    both = rig | {"zab-role": "test"}
    assert ask(policy, "StartInstances", WEST, "instance", tags=both) == "allow"
    assert ask(policy, "StartInstances", WEST, "instance", tags=rig) == "implicit"
    # #263's VM: the test role, but neither the rig's scope nor its workshop (A13)
    vm263 = {"workshop": "zab-test", "zab-role": "test"}
    assert ask(policy, "StopInstances", WEST, "instance", tags=vm263) == "implicit"
    launch = both | {"participant": "zab-test-a"}
    assert ask(policy, "RunInstances", WEST, "instance", request=launch, itype=M7A) == "allow"
    lacking = {k: v for k, v in launch.items() if k != "workshop"}  # A13's new probe
    assert ask(policy, "RunInstances", WEST, "instance", request=lacking, itype=M7A) != "allow"
    retag = {"zab-role": "x"}
    assert ask(policy, "CreateTags", WEST, "instance", tags=both, request=retag) == "deny"
    sids = [st["Sid"] for st in policy["Statement"]]
    assert "UsWest2Fleet" in sids and len(set(sids)) == len(sids)


def fleet_toml(regions: list[str]) -> str:
    """A `[global]` table for `regions`, and no workshop: the policy needs none."""
    return f'[global]\nscope = "{S}"\nregions = {json.dumps(regions)}\ninstance_types = ["{M7A}"]\n'


def test_many_regions_split_into_managed_policies(tmp_path, capsys):
    regions = ["us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-northeast-1"]
    text = fleet_toml(regions)
    policy = render_policy.render(tomllib.loads(text))
    docs = render_policy.split(policy)
    assert len(docs) > 1
    assert all(render_policy.size(doc) <= render_policy.MANAGED_LIMIT for doc in docs)
    denies = [st for st in policy["Statement"] if st["Effect"] == "Deny"]
    assert len(denies) == 5 and all(doc["Statement"][:5] == denies for doc in docs)
    allows = [st for doc in docs for st in doc["Statement"][5:]]
    assert allows == [st for st in policy["Statement"] if st["Effect"] == "Allow"]
    # attached without the first, a later document still denies the server
    doc = next(d for d in docs[1:] if any(st["Sid"].endswith("Fleet") for st in d["Statement"]))
    fleet = next(st for st in doc["Statement"] if st["Sid"].endswith("Fleet"))
    region = fleet["Resource"][0].split(":")[3]
    tags = {"workshop": "w", "zab-scope": S}
    assert ask(doc, "StartInstances", region, "instance", tags=tags) == "allow"
    assert ask(doc, "StartInstances", region, "instance", tags=tags | {"zab-admin": "x"}) == "deny"
    assert ask(doc, "RunInstances", region, "network-interface", rid="eni-1") == "deny"
    path = tmp_path / "admin.toml"
    path.write_text(text)
    assert render_policy.main(["--config", str(path)]) == 2
    assert "pass --out-dir" in capsys.readouterr().err
    out = tmp_path / "out"
    out.mkdir()
    (out / f"zab-admin-{len(docs) + 1}.json").write_text("{}")  # left by a longer render
    others = ["notes.json", "zab-admin-policy.json", "zab-admin-01.json", "zab-admin-0.json"]
    for name in others:  # not what --out-dir writes: never touched
        (out / name).write_text("{}")
    assert render_policy.main(["--config", str(path), "--out-dir", str(out)]) == 0
    printed = capsys.readouterr()
    assert printed.out.count("removed") == 1
    assert f"zab-admin-{len(docs) + 1}.json: removed" in printed.out
    assert "quota" not in printed.err
    written = [out / f"zab-admin-{i}.json" for i in range(1, len(docs) + 1)]
    assert [json.loads(p.read_text()) for p in written] == docs
    assert sorted(p.name for p in out.iterdir()) == sorted([*others, *(p.name for p in written)])


def test_more_policies_than_a_role_holds_by_default_are_flagged(tmp_path, capsys):
    """30 regions need more documents than IAM's default of 10 managed policies per role."""
    path = tmp_path / "admin.toml"
    path.write_text(fleet_toml([f"xx-test-{i}" for i in range(30)]))
    assert render_policy.main(["--config", str(path), "--out-dir", str(tmp_path / "out")]) == 0
    count = len(list((tmp_path / "out").iterdir()))
    assert count > render_policy.ROLE_QUOTA
    assert f"{count} policies, over IAM's default of 10" in capsys.readouterr().err


def test_main_prints_one_policy(tmp_path, capsys):
    """One region fits one document; the example's two need two since #553's statements."""
    path = tmp_path / "admin.toml"
    path.write_text(fleet_toml([EAST]))
    assert render_policy.main(["--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == render_policy.render(
        tomllib.loads(path.read_text())
    )
    assert len(render_policy.split(render_policy.render(EXAMPLE))) == 2


def test_production_s_shape_needs_two_managed_policies():
    """`mathagents`' `[global]` (ADMIN §5: us-east-2 and us-west-2, one instance type) no
    longer fits one document with the volume statements (#553): the v0.2.9 policy change added
    the second (CHANGELOG 0.2.10's upgrade notes)."""
    docs = render_policy.split(render_policy.render(tomllib.loads(fleet_toml([EAST, WEST]))))
    assert len(docs) == 2
    # what ADMIN §5's render shows, and what the v0.2.9 policy change's order (the second one
    # first) rested on
    assert [render_policy.size(doc) for doc in docs] == [5943, 1198]
    sids = [[st["Sid"] for st in doc["Statement"][5:]] for doc in docs]
    assert sids[1] == ["UsWest2Volume", "LaunchNetwork"]
    assert "LaunchNetwork" not in sids[0] and "UsEast2Volume" in sids[0]
    runbook = (HERE.parents[1] / "docs" / "ADMIN.md").read_text()  # §5; §8.4 until #700
    for size, n in zip((5943, 1198), (1, 2), strict=True):
        assert f"zab-admin/policies/zab-admin-{n}.json: {size} characters" in runbook
    assert f"# {sids[1]}" in runbook


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda g: g.pop("scope"), "since v0.2.5"),
        (lambda g: g.update(scope="Math Agents"), "[global] scope"),
        (lambda g: g.update(scope=5), "[global] scope"),
        (lambda g: g.update(regions=["us-west"]), "[global] regions"),
        (lambda g: g.update(regions=[]), "[global] regions"),
        (lambda g: g.pop("regions"), "[global] regions"),
        (lambda g: g.update(instance_types=[]), "instance_types"),
        (lambda g: g.update(instance_types=["a", "a"]), "instance_types"),
        (lambda g: g.update(tags="zab-role=test"), "extra tags"),
        (lambda g: g.update(tags=["zab-admin=x"]), "extra tags"),
        (lambda g: g.update(tags=["participant=x"]), "extra tags"),
        (lambda g: g.update(tags=["AWS:x=y"]), "extra tags"),
        (lambda g: g.update(tags=["ZAB-ADMIN=x"]), "extra tags"),
        (lambda g: g.update(tags=["name=x"]), "extra tags"),
        (lambda g: g.update(tags=["Workshop=x"]), "extra tags"),
        (lambda g: g.update(tags=["zab-scope=x"]), "extra tags"),
        (lambda g: g.update(tags=["zab-role=test", "Zab-Role=x"]), "extra tags"),
    ],
)
def test_bad_configs_are_refused(change, error):
    config = copy.deepcopy(EXAMPLE)
    change(config["global"])
    with pytest.raises(render_policy.PolicyError, match=re.escape(error)):
        render_policy.render(config)


@pytest.mark.parametrize("key", ["regionz", "instance_type", "tag", "extra_regions", "Scope"])
def test_an_unknown_global_key_is_refused(key):
    """A typo in `[global]` (`instance_type`, a singular `tag`) is refused, never ignored: the
    renderer can't then fall back to a wider policy than the bot's loader would accept."""
    config = copy.deepcopy(EXAMPLE)
    config["global"][key] = ["us-east-1"]
    with pytest.raises(render_policy.PolicyError, match=re.escape(f"unknown key {key!r}")):
        render_policy.render(config)
    loader = pytest.importorskip("zab.admin.config")
    known = {"scope", "regions", "instance_types", "tags"} | loader._GLOBAL_BOT_KEYS
    assert render_policy.GLOBAL_KEYS == known  # the two read [global] alike


def test_a_v024_config_is_refused_with_the_migration():
    """v0.2.4's admin.toml had no `[global]` scope: refused, naming its migration, never guessed."""
    config = copy.deepcopy(EXAMPLE)
    del config["global"]
    with pytest.raises(render_policy.PolicyError, match=re.escape("CHANGELOG.md 0.2.5's upgrade")):
        render_policy.render(config)
