"""`admin.toml` (docs/ADMIN.md §2.1): several workshops, strict values, no shared files."""

import json
import re

import pytest

from zab.admin.config import (
    RELEASE_PATTERNS,
    AdminConfigError,
    Bot,
    Roots,
    load_admin_config,
    parse_admin_config,
)

HARVARD = """\
[[workshop]]
name = "harvard-2026"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "/var/lib/zab-admin/ws/harvard-2026/roster.csv"
out_dir = "/var/lib/zab-admin/ws/harvard-2026/out"
organizers = [103, 104]
alerts_channel = 45
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/harvard-2026.zuliprc"
user_id = 901
email = "harvard-admin-bot@example.zulipchat.com"
channels = [42]
[workshop.aws]
region = "us-west-2"
ami = "ami-0123456789abcdef0"
security_group = "sg-0123456789abcdef0"
instance_types = ["m7a.2xlarge", "m7a.xlarge"]
key_name = "zab-admin-harvard-2026"
admin_keys = ["/etc/zab-admin/break-glass/drew-laptop.pub"]
"""
KEPT = """\
[[workshop]]
name = "nt-agents-2026"
github_org = "MathAgents"
refserve_url = "https://files.mathagents.org"
roster = "/var/lib/zab-admin/ws/nt-agents-2026/roster.csv"
out_dir = "/var/lib/zab-admin/ws/nt-agents-2026/out"
[workshop.bot]
zuliprc = "/etc/zab-admin/secrets/nt-agents-2026.zuliprc"
user_id = 902
channels = [43, 44]
[workshop.aws]
region = "ap-northeast-1"
ami = "ami-00000000000000001"
security_group = "sg-00000000000000001"
instance_types = ["m7a.2xlarge"]
key_name = "zab-admin-nt-agents-2026"
admin_keys = ["/etc/zab-admin/break-glass/drew-laptop.pub"]
"""
TOP = """\
provider = "aws"
admins = [101, 102]
deployers = [101]
destructive_approvals = 2
plan_ttl_minutes = 10
release = "final"
console_url = "https://admin.example.org"

[global]
scope = "mathagents"
regions = ["us-west-2", "ap-northeast-1"]
instance_types = ["m7a.2xlarge", "m7a.xlarge", "r7a.xlarge"]
zuliprc = "/etc/zab-admin/secrets/global.zuliprc"
user_id = 900
channels = [40]

"""
EXAMPLE = TOP + HARVARD + "\n" + KEPT
GLOBAL_TYPES = 'instance_types = ["m7a.2xlarge", "m7a.xlarge", "r7a.xlarge"]'
SCOPED = ("zab-scope", "mathagents")


def test_the_example_parses():
    config = parse_admin_config(EXAMPLE)
    assert config.provider == "aws"
    assert config.admins == {101, 102}
    assert config.deployers == {101}
    assert (config.destructive_approvals, config.plan_ttl_minutes) == (2, 10)
    assert config.release == "final"
    assert config.release_pattern.pattern == RELEASE_PATTERNS["final"]
    assert config.console_url == "https://admin.example.org"
    harvard, kept = config.workshops
    assert harvard.name == "harvard-2026"
    assert harvard.tags == (("workshop", "harvard-2026"), SCOPED)  # the fleet's too (#422)
    assert config.scope_tags == (SCOPED,) and config.regions == ("us-west-2", "ap-northeast-1")
    assert config.instance_types == ("m7a.2xlarge", "m7a.xlarge", "r7a.xlarge")
    assert harvard.github_org == "MathAgents"
    assert harvard.roster == "/var/lib/zab-admin/ws/harvard-2026/roster.csv"
    assert harvard.out_dir == "/var/lib/zab-admin/ws/harvard-2026/out"
    assert harvard.organizers == {103, 104}
    assert harvard.bot == Bot(
        "/etc/zab-admin/secrets/harvard-2026.zuliprc",
        901,
        "harvard-admin-bot@example.zulipchat.com",
        frozenset({42}),
    )
    assert config.global_bot == Bot("/etc/zab-admin/secrets/global.zuliprc", 900, None, {40})
    assert harvard.aws.region == "us-west-2"
    assert harvard.aws.ami == "ami-0123456789abcdef0"
    assert harvard.aws.security_group == "sg-0123456789abcdef0"
    assert harvard.aws.instance_types == ("m7a.2xlarge", "m7a.xlarge")
    assert harvard.aws.key_name == "zab-admin-harvard-2026"
    assert harvard.aws.admin_keys == ("/etc/zab-admin/break-glass/drew-laptop.pub",)
    assert kept.tags == (("workshop", "nt-agents-2026"), SCOPED)
    assert kept.organizers == frozenset()
    assert (kept.bot.user_id, kept.bot.email) == (902, None)  # the e-mail is optional
    assert kept.aws.region == "ap-northeast-1"  # workshops may use different regions
    assert (harvard.alerts_channel, kept.alerts_channel) == (45, None)  # optional


def test_admins_and_organizers_stay_distinct():
    """The admins act through every bot; a workshop's organizers only through its own (PR 7)."""
    config = parse_admin_config(EXAMPLE)
    harvard, kept = config.workshops
    assert config.approvers(harvard) == {101, 102, 103, 104}
    assert config.approvers(kept) == {101, 102}
    assert 103 not in config.approvers(kept)
    assert config.workshop("harvard-2026") is harvard
    assert config.workshop("other") is None


def test_defaults_and_the_test_server():
    text = (
        """\
provider = "aws"
admins = []
deployers = [5]
destructive_approvals = 1
release = "rc"
[global]
scope = "zab-test"
regions = ["us-west-2"]
instance_types = ["m7a.2xlarge", "m7a.xlarge"]
tags = ["zab-role=test"]
"""
        + HARVARD
    )
    config = parse_admin_config(text)
    assert config.plan_ttl_minutes == 10
    assert config.console_url == ""
    assert config.global_bot is None  # optional: [global] then holds the scope alone
    assert config.release == "rc"
    assert config.release_pattern.fullmatch("v0.3.0rc1")
    assert config.release_pattern.fullmatch("v0.3.0")
    assert config.workshops[0].tags == (
        ("workshop", "harvard-2026"),
        ("zab-scope", "zab-test"),
        ("zab-role", "test"),
    )
    assert parse_admin_config(EXAMPLE.replace('release = "final"\n', "")).release == "final"


@pytest.mark.parametrize("tag", ["v0.2.3rc1", "v0.2.٣", "main", "v1.2.3-dirty", "", "v1.2"])
def test_the_final_pattern_takes_final_tags_only(tag):
    pattern = parse_admin_config(EXAMPLE).release_pattern
    assert pattern.fullmatch("v0.2.3")
    assert not pattern.fullmatch(tag)


#: `[global] tags` the loader refuses: not a list of KEY=VALUE, or a key it reserves, in any case
BAD_EXTRA_TAGS = (
    '"zab-role=test"', "[5]", '["zab-role"]', '["participant=x"]', '["Name=x"]',
    '["zab-admin=x"]', '["ZAB-ADMIN=x"]', '["name=x"]', '["Participant=x"]', '["Workshop=x"]',
    '["workshop=harvard-2026"]', '["zab-scope=x"]', '["Zab-Scope=x"]', '["a=1", "A=2"]',
    '["aws:x=y"]', '["AWS:x=y"]', '["-x=y"]', '["a b=y"]', '["=y"]', '["x=--yes"]',
    '["zab-role=test", "zab-role=test"]',
)  # fmt: skip
# (what to replace, its replacement, a fragment of the error)
BAD = [
    ('provider = "aws"', 'provider = "gcp"', "provider"),
    ("admins = [101, 102]", "", "admins is required"),
    ("admins = [101, 102]", "admins = [101, 101]", "admins"),
    ("admins = [101, 102]", "admins = [true]", "admins"),
    ("admins = [101, 102]", 'admins = ["101"]', "admins"),
    ("admins = [101, 102]", "admins = [0]", "admins"),
    ("admins = [101, 102]", "admins = 101", "admins"),
    ("deployers = [101]", "deployers = []", "deployers"),
    ("destructive_approvals = 2", "destructive_approvals = 0", "destructive_approvals"),
    ("destructive_approvals = 2", "destructive_approvals = true", "destructive_approvals"),
    ("plan_ttl_minutes = 10", "plan_ttl_minutes = 0", "plan_ttl_minutes"),
    ("plan_ttl_minutes = 10", "plan_ttl_minutes = 61", "plan_ttl_minutes"),
    ("plan_ttl_minutes = 10", "plan_ttl_minutes = 10.5", "plan_ttl_minutes"),
    # release: one of two names, never a free regex (§1.6)
    ('release = "final"', 'release = "rc2"', "release"),
    ('release = "final"', 'release = "FINAL"', "release"),
    ('release = "final"', "release = '^v.*$'", "release"),
    ('release = "final"', "release = true", "release"),
    ('release = "final"', "release_pattern = '.*'", "unknown key release_pattern"),
    ('console_url = "https://admin', 'console_url = "http://admin', "console_url"),
    ('"https://admin.example.org"', '"https://admin.example.org/$(id)"', "console_url"),
    ("console_url", "consol_url", "unknown key consol_url"),
    ("deployers = [101]", "deployers = [101]\norganizers = [5]", "unknown key organizers"),
    ("deployers = [101]", "deployers = [101]\norganizers_stream_id = 4", "unknown key organizers"),
    ('name = "harvard-2026"', 'name = "Harvard 2026"', "workshop[0].name"),
    ('name = "harvard-2026"', 'name = "-harvard"', "workshop[0].name"),
    # the fleet's scope, the same for every workshop (§5; Drew, Oct 16; #422)
    ('scope = "mathagents"\n', "", "[global] scope is required: since v0.2.5"),
    ('scope = "mathagents"', 'scope = "Math Agents"', "global.scope"),
    ('scope = "mathagents"', 'scope = "-mathagents"', "global.scope"),
    ('scope = "mathagents"', "scope = 5", "global.scope"),
    ('regions = ["us-west-2", "ap-northeast-1"]\n', "", "global.regions is required"),
    ('regions = ["us-west-2", "ap-northeast-1"]', "regions = []", "global.regions"),
    ('regions = ["us-west-2", "ap-northeast-1"]', 'regions = ["us-west"]', "global.regions"),
    ('regions = ["us-west-2", "ap-northeast-1"]', 'regions = "us-west-2"', "global.regions"),
    (GLOBAL_TYPES, "instance_types = []", "global.instance_types"),
    (GLOBAL_TYPES, 'instance_types = ["--yes"]', "global.instance_types"),
    # a workshop within them: the role allows nothing else
    ('region = "us-west-2"', 'region = "eu-west-1"', "'eu-west-1' is not in [global] regions"),
    ('["m7a.2xlarge", "m7a.xlarge"]', '["p5.48xlarge"]', "['p5.48xlarge'] not in [global]"),
    # a v0.2.4 workshop's own scope: refused, never guessed (CHANGELOG 0.2.5 migrates it)
    ('name = "harvard-2026"', 'name = "harvard-2026"\ntag = "workshop=harvard-2026"', "since"),
    # extra scope tags: never a reserved key, in any case (IAM ignores it: ADMIN §5, #329)
    *[
        ('scope = "mathagents"', f'scope = "mathagents"\ntags = {tags}', "global.tags")
        for tags in BAD_EXTRA_TAGS
    ],
    ('github_org = "MathAgents"', 'github_org = "-MathAgents"', "github_org"),
    ('github_org = "MathAgents"', 'github_org = "Math;Agents"', "github_org"),
    ('refserve_url = "https://files', 'refserve_url = "http://files', "refserve_url"),
    ("roster = ", "rosters = ", "roster is required"),
    ('roster = "/var', 'roster = "var', "roster"),
    ('roster = "/var/lib', 'roster = "/var/../lib', "roster"),
    ('roster = "/var/lib', 'roster = "/var/./lib', "roster"),
    ('out_dir = "/var/lib', 'out_dir = "/var/lib/.hidden', "out_dir"),
    ("organizers = [103, 104]", "organizers = [103, -1]", "organizers"),
    ("organizers = [103, 104]", "organisers = [103, 104]", "unknown key workshop[0].organisers"),
    ('instance_types = ["m7a.2xlarge", "m7a.xlarge"]', "instance_types = []", "instance_types"),
    ('instance_types = ["m7a.2xlarge", "m7a.xlarge"]', 'instance_types = ["--yes"]', "instance"),
    ('["m7a.2xlarge", "m7a.xlarge"]', '["m7a.2xlarge", "m7a.2xlarge"]', "instance_types"),
    ('["m7a.2xlarge", "m7a.xlarge"]', '"m7a.2xlarge"', "instance_types"),
    ('key_name = "zab-admin-harvard-2026"', 'key_name = "--yes"', "key_name"),
    ('key_name = "zab-admin-harvard-2026"', 'key_name = "a b"', "key_name"),
    ('["/etc/zab-admin/break-glass/drew-laptop.pub"]', "[]", "admin_keys"),
    ('["/etc/zab-admin/break-glass/drew-laptop.pub"]', '["drew.pub"]', "admin_keys"),
    ('["/etc/zab-admin/break-glass/drew-laptop.pub"]', '["/etc/../x.pub"]', "admin_keys"),
    ('["/etc/zab-admin/break-glass/drew-laptop.pub"]', '["/etc/zab-admin/key"]', "admin_keys"),
    ('ami = "ami-0123456789abcdef0"', 'ami = "ami-XY23456789abcdef0"', "ami"),
    ('security_group = "sg-0123456789abcdef0"', 'security_group = "sg-xyz"', "security_group"),
    (
        'security_group = "sg-0123456789abcdef0"',
        'security_grp = "sg-0123456789abcdef0"',
        "security_group is required",
    ),
    ('region = "us-west-2"', "", "workshop[0].aws.region is required"),
    ('region = "us-west-2"', 'region = "US-WEST-2"', "workshop[0].aws.region"),
    ('region = "us-west-2"', 'region = "us-west"', "workshop[0].aws.region"),
    ('region = "us-west-2"', 'region = "us-west-2;id"', "workshop[0].aws.region"),
    ('region = "us-west-2"', 'region = "--region"', "workshop[0].aws.region"),
    ('region = "us-west-2"', 'region = "-us-west-2"', "workshop[0].aws.region"),
    ('region = "us-west-2"', 'region = "us-west-٢"', "workshop[0].aws.region"),
    # one region per workshop (Drew, Oct 15)
    ('region = "us-west-2"', 'region = ["us-west-2", "us-east-2"]', "workshop[0].aws.region"),
    ('region = "us-west-2"', 'regions = ["us-west-2"]', "workshop[0].aws.region is required"),
    (
        'admin_keys = ["/etc/zab-admin/break-glass/drew-laptop.pub"]\n',
        'admin_keys = ["/etc/zab-admin/break-glass/drew-laptop.pub"]\n'
        '[workshop.aws.regions.us-east-2]\nami = "ami-00000000000000002"\n',
        "unknown key workshop[0].aws.regions",
    ),
    (
        '[workshop.aws]\nregion = "us-west-2"',
        '[workshop.gcp]\nx = 1\n[workshop.aws]\nregion = "us-west-2"',
        "unknown key workshop[0].gcp",
    ),
    ('[[workshop]]\nname = "harvard-2026"', '[workshop]\nname = "harvard-2026"', "not valid TOML"),
    ('provider = "aws"', 'provider = "aws"\nprovider = "aws"', "not valid TOML"),
    ('provider = "aws"', "x = " + "[" * 5000 + "]" * 5000, "not valid TOML"),
    # the admin bots
    (
        '[workshop.bot]\nzuliprc = "/etc/zab-admin/secrets/harvard',
        '[workshop.robot]\nzuliprc = "/etc/zab-admin/secrets/harvard',
        "workshop[0].bot is required",
    ),
    ("user_id = 901\n", "", "workshop[0].bot.user_id is required"),
    ("user_id = 900\n", "", "global.user_id is required"),
    ("user_id = 901", "user_id = 0", "workshop[0].bot.user_id"),
    ("user_id = 901", "user_id = true", "workshop[0].bot.user_id"),
    ("user_id = 901", 'user_id = "901"', "workshop[0].bot.user_id"),
    ('email = "harvard-admin-bot@', 'email = "harvard admin bot@', "workshop[0].bot.email"),
    ('email = "harvard-admin-bot@', 'email = "-harvard-admin-bot@', "workshop[0].bot.email"),
    ('zuliprc = "/etc/zab-admin/secrets/harvard', 'zuliprc = "secrets/harvard', "bot.zuliprc"),
    ("user_id = 901", 'user_id = 901\napi_key = "x"', "unknown key workshop[0].bot.api_key"),
    ("user_id = 900", 'user_id = 900\nname = "x"', "unknown key global.name"),
    ('zuliprc = "/etc/zab-admin/secrets/global', 'zuliprc = "global', "global.zuliprc"),
    ("channels = [40]", "channels = []", "global.channels"),
    ("channels = [42]\n", "channels = []\n", "channels"),
    ("channels = [42]\n", 'channels = ["organizers"]\n', "channels"),
    # the waker's alerts channel (#358): the workshop's own, and no bot's command channel
    ("alerts_channel = 45", "alerts_channel = 0", "workshop[0].alerts_channel"),
    ("alerts_channel = 45", "alerts_channel = true", "workshop[0].alerts_channel"),
    ("alerts_channel = 45", 'alerts_channel = "45"', "workshop[0].alerts_channel"),
    ("alerts_channel = 45", "alerts_channel = [45]", "workshop[0].alerts_channel"),
    ("alerts_channel = 45", "alerts_channel = 42", "a bot's command channel"),
    ("alerts_channel = 45", "alerts_channel = 40", "a bot's command channel"),  # [global]'s
    ("alerts_channel = 45", "alerts_channel = 44", "a bot's command channel"),  # another bot's
    # the participants' channel (#627): its own, and no command or alerts channel
    ("alerts_channel = 45", "alerts_channel = 45\nchannel = 0", "workshop[0].channel"),
    ("alerts_channel = 45", "alerts_channel = 45\nchannel = true", "workshop[0].channel"),
    ("alerts_channel = 45", 'alerts_channel = 45\nchannel = "x"', "workshop[0].channel"),
    ("alerts_channel = 45", "alerts_channel = 45\nchannel = 42", "a bot's command channel"),
    ("alerts_channel = 45", "alerts_channel = 45\nchannel = 40", "a bot's command channel"),
    ("alerts_channel = 45", "alerts_channel = 45\nchannel = 45", "or an alerts_channel"),
    # a spare's idle stop (#715): idle stop's own minutes, 10 to 240
    ("alerts_channel = 45", "alerts_channel = 45\nspare_idle_stop = 9", "spare_idle_stop"),
    ("alerts_channel = 45", "alerts_channel = 45\nspare_idle_stop = 0", "expected 10 to 240"),
    ("alerts_channel = 45", "alerts_channel = 45\nspare_idle_stop = 241", "expected 10 to 240"),
    ("alerts_channel = 45", "alerts_channel = 45\nspare_idle_stop = true", "expected int"),
    ("alerts_channel = 45", 'alerts_channel = 45\nspare_idle_stop = "10"', "expected int"),
    ("alerts_channel = 45", "alerts_channel = 45\nspare_idle_stop = 10.5", "expected int"),
]


def test_a_spares_idle_stop_is_ten_minutes_unless_the_entry_says():
    """#715: `spare_idle_stop`, per workshop, at least idle stop's own minimum (10; the bounds
    against idle_stop.py's: tests/workshop/test_admin_idle_stop.py)."""
    harvard, kept = parse_admin_config(EXAMPLE).workshops
    assert (harvard.spare_idle_stop, kept.spare_idle_stop) == (10, 10)
    text = EXAMPLE.replace("alerts_channel = 45", "alerts_channel = 45\nspare_idle_stop = 30", 1)
    assert [ws.spare_idle_stop for ws in parse_admin_config(text).workshops] == [30, 10]
    for minutes in (10, 240):
        text = EXAMPLE.replace("alerts_channel = 45", f"alerts_channel = 45\nspare_idle_stop = "
                               f"{minutes}", 1)  # fmt: skip
        assert parse_admin_config(text).workshops[0].spare_idle_stop == minutes


def test_a_workshops_channel_is_optional_and_its_own():
    """#627: the participants' channel, by ID; never another workshop's."""
    with_channel = HARVARD.replace("alerts_channel = 45", "alerts_channel = 45\nchannel = 47")
    harvard, kept = parse_admin_config(TOP + with_channel + "\n" + KEPT).workshops
    assert (harvard.channel, kept.channel) == (47, None)
    out_dir = 'out_dir = "/var/lib/zab-admin/ws/nt-agents-2026/out"\n'
    for channel, error in ((47, "another workshop's"), (48, None)):
        text = TOP + with_channel + "\n" + KEPT.replace(out_dir, f"{out_dir}channel = {channel}\n")
        if error:
            with pytest.raises(AdminConfigError, match=error):
                parse_admin_config(text)
        else:
            assert [ws.channel for ws in parse_admin_config(text).workshops] == [47, 48]


def test_a_workshops_gift_step_is_one_line_of_text():
    """#709: "Start here"'s step 1, in the workshop's words; optional, and never more than a
    line of at most 500 bytes in UTF-8 that could pass for a flag or hide a control."""
    harvard, kept = parse_admin_config(EXAMPLE).workshops
    assert harvard.gifts_step is None and kept.gifts_step is None

    def with_step(toml_value: str) -> str:
        line = f"alerts_channel = 45\ngifts_step = {toml_value}"
        return EXAMPLE.replace("alerts_channel = 45", line, 1)

    good = (
        "Accept your gifts: Claude Max from Anthropic's e-mail, ChatGPT Pro at [a form](https://x)."
    )
    for text in (good, "é" * 250, "A"):
        assert parse_admin_config(with_step(json.dumps(text))).workshops[0].gifts_step == text
    bad = ("", " Accept", "Accept ", "-x", "two\nlines", "a\u0007b", "a\u2028b", "é" * 251)
    for value in [*map(json.dumps, bad), "1", '["x"]']:
        with pytest.raises(AdminConfigError, match=re.escape("workshop[0].gifts_step")):
            parse_admin_config(with_step(value))


def test_two_workshops_never_share_an_alerts_channel():
    """Its participant bots post a fleet's wake failures there: never another fleet's."""
    out_dir = 'out_dir = "/var/lib/zab-admin/ws/nt-agents-2026/out"\n'
    for channel, error in ((45, "another workshop's"), (46, None)):
        kept = KEPT.replace(out_dir, f"{out_dir}alerts_channel = {channel}\n")
        text = TOP + HARVARD + "\n" + kept
        if error:
            with pytest.raises(AdminConfigError, match=error):
                parse_admin_config(text)
        else:
            assert [ws.alerts_channel for ws in parse_admin_config(text).workshops] == [45, 46]


def test_a_workshops_mention_after_s_is_whole_seconds_up_to_a_day():
    """#711: when a finished turn mentions its owner (SPEC §4.8), per workshop; 120, the
    bridge's default, when absent; 0 (a turn of any length) to 86400, an integer."""
    harvard, kept = parse_admin_config(EXAMPLE).workshops
    assert harvard.mention_after_s == kept.mention_after_s == 120

    def with_value(value: str) -> str:
        line = f"alerts_channel = 45\nmention_after_s = {value}"
        return EXAMPLE.replace("alerts_channel = 45", line, 1)

    for value in (0, 1, 300, 86400):
        harvard, kept = parse_admin_config(with_value(str(value))).workshops
        assert (harvard.mention_after_s, kept.mention_after_s) == (value, 120)
    for bad, rule in (("-1", "expected 0 to 86400"), ("86401", "expected 0 to 86400"),
                      ("1.5", "expected int"), ("true", "expected int"),
                      ('"120"', "expected int")):  # fmt: skip
        where = re.escape(f"workshop[0].mention_after_s: {rule}")
        with pytest.raises(AdminConfigError, match=where):
            parse_admin_config(with_value(bad))


@pytest.mark.parametrize(("old", "new", "error"), BAD, ids=[f"{i}" for i in range(len(BAD))])
def test_bad_values_are_refused(old, new, error):
    assert old in EXAMPLE, old
    with pytest.raises(AdminConfigError, match=re.escape(error)):
        parse_admin_config(EXAMPLE.replace(old, new, 1))


@pytest.mark.parametrize("entries", ["workshop = []", "workshop = [1]", 'workshop = ["x"]'])
def test_workshops_are_tables(entries):
    with pytest.raises(AdminConfigError, match=r"workshop"):
        parse_admin_config(entries + "\n" + TOP)


def test_one_workshop_table_is_not_a_list():
    text = TOP + HARVARD.replace("[[workshop]]", "[workshop]")
    with pytest.raises(AdminConfigError, match=r"expected \[\[workshop\]\] entries"):
        parse_admin_config(text)
    with pytest.raises(AdminConfigError, match=r"expected \[\[workshop\]\] entries"):
        parse_admin_config(TOP)


HOME = "/var/lib/zab-admin/ws/harvard-2026"


# Each kind of file lies under its own §1.3 directory, and no file serves two roles.
@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        # rosters and out_dirs strictly under /var/lib/zab-admin/ws/<name>/
        (f"{HOME}/roster.csv", "/tmp/roster.csv", f"roster: expected a path under {HOME}/"),  # noqa: S108
        (f"{HOME}/out", "/var/lib/zab-admin", f"out_dir: expected a path under {HOME}/"),
        (f"{HOME}/out", "/var/lib/zab-admin/ws", f"out_dir: expected a path under {HOME}/"),
        (f"{HOME}/out", HOME, f"out_dir: expected a path under {HOME}/"),
        (f"{HOME}/out", "/var", f"out_dir: expected a path under {HOME}/"),
        (f"{HOME}/roster.csv", "/var/lib/zab-admin/ws/nt-agents-2026/x.csv", "roster: expected"),
        (f"{HOME}/out", "/var/lib/zab-admin/ws/nt-agents-2026/out2", "out_dir: expected"),
        (f"{HOME}/out", "/var/lib/zab-admin/ws/harvard-2026x/out", "out_dir: expected"),
        # no file serves two roles
        (f"{HOME}/roster.csv", f"{HOME}/out/roster.csv", "(roster) lies at or in"),
        (f"{HOME}/roster.csv", f"{HOME}/out", "(roster) lies at or in"),
        # bot keys under /etc/zab-admin/secrets/
        ("/etc/zab-admin/secrets/harvard-2026.zuliprc", f"{HOME}/bot.zuliprc", "bot.zuliprc"),
        ("/etc/zab-admin/secrets/harvard-2026.zuliprc", f"{HOME}/roster.csv", "bot.zuliprc"),
        ("/etc/zab-admin/secrets/global.zuliprc", "/etc/zab-admin/admin.toml", "global.zuliprc"),
        ("/etc/zab-admin/secrets/global.zuliprc", "/etc/zab-admin/secrets", "global.zuliprc"),
        ("/etc/zab-admin/secrets/global.zuliprc", "/tmp/global.zuliprc", "global.zuliprc"),  # noqa: S108
        # admin keys, which get sudo on every VM, under /etc/zab-admin/break-glass/
        ("/etc/zab-admin/break-glass/drew-laptop.pub", "/tmp/x.pub", "admin_keys"),  # noqa: S108
        ("/etc/zab-admin/break-glass/drew-laptop.pub", "/dev/shm/x.pub", "admin_keys"),  # noqa: S108
        ("/etc/zab-admin/break-glass/drew-laptop.pub", f"{HOME}/out/k.pub", "admin_keys"),
        ("/etc/zab-admin/break-glass/drew-laptop.pub", "/etc/zab-admin/secrets/k.pub", "admin_k"),
    ],
)
def test_files_lie_in_their_own_directories(old, new, error):
    assert old in EXAMPLE, old
    with pytest.raises(AdminConfigError, match=re.escape(error)):
        parse_admin_config(EXAMPLE.replace(old, new, 1))


def test_workshops_share_no_name_or_scope():
    with pytest.raises(AdminConfigError, match="two workshops have the same name"):
        parse_admin_config(EXAMPLE.replace("nt-agents-2026", "harvard-2026").replace("902", "903"))


def test_the_roots_come_only_from_the_caller(tmp_path):
    """Tests may move the §1.3 directories through `roots=`; admin.toml can't."""
    roots = Roots(workshops="/srv/ws", secrets="/srv/secrets", break_glass="/srv/keys")
    moved = (
        EXAMPLE.replace("/var/lib/zab-admin/ws", "/srv/ws")
        .replace("/etc/zab-admin/secrets", "/srv/secrets")
        .replace("/etc/zab-admin/break-glass", "/srv/keys")
    )
    assert parse_admin_config(moved, roots=roots).workshops[0].roster == (
        "/srv/ws/harvard-2026/roster.csv"
    )
    with pytest.raises(AdminConfigError, match="expected a path under"):
        parse_admin_config(moved)
    path = tmp_path / "admin.toml"
    path.write_text(moved, encoding="utf-8")
    assert load_admin_config(path, roots=roots).global_bot.zuliprc == "/srv/secrets/global.zuliprc"
    with pytest.raises(AdminConfigError, match="unknown key roots"):
        parse_admin_config('roots = "/srv"\n' + EXAMPLE)


def test_one_file_in_two_roles_is_refused_whatever_the_roots():
    """With roots that overlap, a bot key that is also an admin key is still refused."""
    roots = Roots(secrets="/etc/zab-admin/keys", break_glass="/etc/zab-admin/keys")
    text = (
        EXAMPLE.replace("/etc/zab-admin/secrets", "/etc/zab-admin/keys")
        .replace("/etc/zab-admin/break-glass", "/etc/zab-admin/keys")
        .replace("keys/global.zuliprc", "keys/drew-laptop.pub")
    )
    with pytest.raises(AdminConfigError, match=r"\(bot zuliprc\) lies at or in"):
        parse_admin_config(text, roots=roots)


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        ('harvard-2026.zuliprc"', 'nt-agents-2026.zuliprc"', "two bots have the same zuliprc"),
        ('harvard-2026.zuliprc"', 'global.zuliprc"', "two bots have the same zuliprc"),
        ("user_id = 901", "user_id = 900", "two bots have the same user_id"),
        ("user_id = 902", "user_id = 901", "two bots have the same user_id"),
        (
            "user_id = 902\n",
            'user_id = 902\nemail = "harvard-admin-bot@example.zulipchat.com"\n',
            "two bots have the same email",
        ),
        # two bots in one channel would both obey its commands, each against its own fleet
        ("channels = [43, 44]", "channels = [42, 44]", "a channel is bound to two bots"),
        ("channels = [40]", "channels = [44]", "a channel is bound to two bots"),
        # bots are never obeyed (§2.1): none may stand on an allowlist
        ("organizers = [103, 104]", "organizers = [901, 104]", "user 901 is an admin bot"),
        ("organizers = [103, 104]", "organizers = [902, 104]", "user 902 is an admin bot"),
        ("admins = [101, 102]", "admins = [900, 102]", "user 900 is an admin bot"),
        ("deployers = [101]", "deployers = [901]", "user 901 is an admin bot"),
    ],
)
def test_bots_share_nothing_and_approve_nothing(old, new, error):
    assert old in EXAMPLE, old
    with pytest.raises(AdminConfigError, match=re.escape(error)):
        parse_admin_config(EXAMPLE.replace(old, new, 1))


def test_a_global_admin_completes_any_workshops_plans():
    """#558: one global admin's approval completes a destructive plan, so a workshop with
    fewer approvers than `destructive_approvals` is fine while there is a global admin; with
    none, it is refused."""
    text = EXAMPLE.replace("destructive_approvals = 2", "destructive_approvals = 3")
    config = parse_admin_config(text)
    kept = config.workshop("nt-agents-2026")
    assert kept is not None and len(config.approvers(kept)) == 2
    alone = text.replace("admins = [101, 102]", "admins = []")
    with pytest.raises(AdminConfigError, match="2 admins and organizers can approve its plans"):
        parse_admin_config(alone)


def test_every_workshop_has_an_approver():
    """No admins, and a workshop without organizers: nobody could approve its plans."""
    text = EXAMPLE.replace("admins = [101, 102]", "admins = []").replace(
        "destructive_approvals = 2", "destructive_approvals = 1"
    )
    with pytest.raises(AdminConfigError, match="nt-agents-2026: 0 admins and organizers"):
        parse_admin_config(text)
    kept_bot = '[workshop.bot]\nzuliprc = "/etc/zab-admin/secrets/nt-agents'
    assert kept_bot in text
    assert parse_admin_config(text.replace(kept_bot, "organizers = [7]\n" + kept_bot))
    # its own bot is no approver
    with pytest.raises(AdminConfigError, match="user 902 is an admin bot"):
        parse_admin_config(text.replace(kept_bot, "organizers = [902]\n" + kept_bot))


def test_errors_name_the_file(tmp_path):
    path = tmp_path / "admin.toml"
    with pytest.raises(AdminConfigError, match=re.escape(str(path))):
        load_admin_config(path)
    path.write_text(EXAMPLE.replace('provider = "aws"', 'provider = "gcp"'), encoding="utf-8")
    with pytest.raises(AdminConfigError, match=re.escape(f"{path}: provider")):
        load_admin_config(path)
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(AdminConfigError, match=re.escape(str(path))):
        load_admin_config(path)
    path.write_text(EXAMPLE, encoding="utf-8")
    assert load_admin_config(path).workshops[0].name == "harvard-2026"


def test_the_command_line_prints_what_deploy_sh_needs(tmp_path, capsys):
    """`python -m zab.admin.config WHAT` (deploy.sh, §1.6): one item per line, no whitespace in
    any field."""
    from zab.admin.config import main

    path = tmp_path / "admin.toml"
    scoped = 'scope = "mathagents"\ntags = ["zab-role=test"]'
    path.write_text(EXAMPLE.replace('scope = "mathagents"', scoped), encoding="utf-8")
    config = ["--config", str(path)]
    assert main([*config, "check"]) == 0
    assert capsys.readouterr().out == ""
    assert main([*config, "console-host"]) == 0
    assert capsys.readouterr().out == "admin.example.org\n"
    assert main([*config, "key-pairs"]) == 0
    assert capsys.readouterr().out == (
        "harvard-2026 us-west-2 zab-admin-harvard-2026 workshop=harvard-2026 "
        "zab-scope=mathagents zab-role=test\n"
        "nt-agents-2026 ap-northeast-1 zab-admin-nt-agents-2026 workshop=nt-agents-2026 "
        "zab-scope=mathagents zab-role=test\n"
    )
    assert main([*config, "zuliprcs"]) == 0
    assert capsys.readouterr().out == (
        "/etc/zab-admin/secrets/global.zuliprc\n"
        "/etc/zab-admin/secrets/harvard-2026.zuliprc\n"
        "/etc/zab-admin/secrets/nt-agents-2026.zuliprc\n"
    )
    path.write_text(EXAMPLE.replace('console_url = "https://admin.example.org"\n', ""))
    assert main([*config, "console-host"]) == 0
    assert capsys.readouterr().out == ""
    path.write_text(EXAMPLE.replace('provider = "aws"', 'provider = "gcp"'), encoding="utf-8")
    assert main([*config, "check"]) == 1
    assert "provider" in capsys.readouterr().err
