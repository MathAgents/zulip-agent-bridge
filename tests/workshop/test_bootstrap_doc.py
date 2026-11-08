"""docs/BOOTSTRAP.md, the bootstrap of an admin server, quotes what it runs.

A deployer types these commands once, on the day, against production. So:

- **The `admin.toml` template** (§7.2) loads with the strict loader (`zab.admin.config`) once its
  `<PLACEHOLDER>`s are filled with sample values, holds every key the loader reads, and is
  refused as written, so a placeholder left in can't load. The role's policy renders from it.
- **The secrets listing** (§8.1) is exactly what `deploy.sh --bootstrap` installs for that
  `admin.toml`: the two every server has, as `install_secrets` names them, and every zuliprc the
  loader lists. Every `~/zab-admin/secrets/…` path the runbook names is one of them.
- **The role's policy** rendered from it has the five denies and the scope §10 tells the
  deployer to check: every Allow on the fleet's scope tag, in `[global] regions`, `m7a.2xlarge`,
  nothing but `ec2:`.
- **The server and its first workshop** (#417, #427): the bootstrap makes the global admin bot
  and one workshop by hand, which `!new-workshop` needs for its first `like=`, and never another,
  which `!new-workshop` creates. §5.3's bots, §5.4's channels, the template's one workshop and
  §12's `bot check` count agree.
- **Deployment-neutral** (#427): no person, release, account, scope or resource ID of one
  deployment; the release is one variable, `$V`.
- **The commands.** Every `deploy.sh`, `render_policy.py`, `python -m zab.admin.config` and
  `python -m zab.admin bot` command quoted, in a code block or a code span, uses only options
  the command accepts: `deploy.sh`'s laptop half, read from its `case` arms, and the others'
  `--help`. An option that takes a value has one, and a positional is one of its choices. A
  `deploy.sh` command in a block is whole: a bootstrap or a wheel names the host, the `etc/`
  directory and the release, `$V`, a bootstrap its secrets.
- **Bot commands.** Each code span that starts with `!` parses with the bot's grammar, or names
  a verb, as prose does.
- **The ID reader** (§5.6) runs against a fake Zulip: it prints each key's identity, each bot's
  owner, its channels, whether each is public or private and with which history, and members,
  and never a key.
"""

import base64
import json
import re
import shlex
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
import render_policy

from zab.admin import config
from zab.admin.grammar import _GROUPS, LATER, SPECS, Command, Context, Roster, parse

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "BOOTSTRAP.md"
TEXT = DOC.read_text(encoding="utf-8")
DEPLOY = ROOT / "workshop" / "adminserver" / "deploy.sh"
FENCE = re.compile(r"^```(\w*)\n(.*?)^```", re.S | re.M)
#: A fenced block, at the margin or indented in a list item: (indent, language, body).
ANY_FENCE = re.compile(r"^( *)```(\w*)\n(.*?)^\1```", re.S | re.M)
TESTED = re.compile(r"<!-- tested: [^>]*-->\n```(\w+)\n(.*?)^```", re.S | re.M)
PLACEHOLDER = re.compile(r"<([A-Z][A-Z_]*)>")
#: A sample value for each placeholder of the template, valid for the loader.
SAMPLES = {
    "YOUR_USER_ID": "101",
    "ORGANIZER_USER_ID": "102",
    "HOST": "admin.example.org",
    "RELEASE_OWNER": "ExampleOrg",  # release_repo, !upgrade-server's (#569)
    "RELEASE_REPO": "zulip-agent-bridge",
    "SCOPE": "example",
    "WR": "us-west-2",
    "WS": "first-workshop",
    "GITHUB_ORG": "ExampleOrg",
    "FILE_SERVER_URL": "https://files.example.org",
    "GLOBAL_BOT_USER_ID": "900",
    "GLOBAL_BOT_EMAIL": "workshop-admin-bot@example.zulipchat.com",
    "ZAB_ADMINS_CHANNEL_ID": "40",
    "WS_BOT_USER_ID": "902",
    "WS_BOT_EMAIL": "first-workshop-admin-bot@example.zulipchat.com",
    "WS_ORGANIZERS_CHANNEL_ID": "43",
    "WS_ALERTS_CHANNEL_ID": "44",
    "WS_PARTICIPANTS_CHANNEL_ID": "45",
    "AMI_ID": "ami-0123456789abcdef0",
    "SG_ID": "sg-0123456789abcdef0",
}
WS = SAMPLES["WS"]  # the first workshop, which the bootstrap makes by hand (#427)
SCOPE = SAMPLES["SCOPE"]  # [global] scope: the fleet-wide tag zab-scope=<scope> (#422)
REGION = SAMPLES["WR"]  # the first workshop's region, [global]'s one region at the bootstrap
KEY = "zab-deployer"  # the dedicated key's file name in the runbook (§2.1; #429, #427)
#: One deployment's record (#427): its people, releases, account, scope, workshops and IDs: a
#: 12-digit account, a 7-digit Zulip Cloud user ID. The development machines' names, other people's
#: and the development process's are left to the public export's scan, which looks in every file
#: (#593).
SPECIFIC = re.compile(
    r"\bDrew\b|v0\.\d+\.\d+|nt-agents|mathagents|\bUCI\b|slmath|harvard"
    r"|--profile drew|\b(?:ami|sg|i|eni|snap|vol)-[0-9a-f]{8,17}\b|(?<!\d)\d{7}(?!\d)"
    r"|zab-drew|AndrewVSutherland|(?<!\d)\d{12}(?!\d)",  # review of #582
    re.IGNORECASE,
)
#: What ends a command on a shell line.
OPERATORS = frozenset({"&&", "||", "|", ";", "(", ")", "&", ">", ">>", "<"})  # and redirections


def marked_block(lang: str) -> str:
    """The one block of `lang` marked `<!-- tested: … -->`."""
    found = [body for kind, body in TESTED.findall(TEXT) if kind == lang]
    assert len(found) == 1, f"expected one tested {lang} block, got {len(found)}"
    return found[0]


def template() -> str:
    return marked_block("toml")


def fill(text: str) -> str:
    """`text` with each of the template's placeholders replaced by its sample."""
    return PLACEHOLDER.sub(lambda m: SAMPLES.get(m[1], m[0]), text)


def filled() -> str:
    return fill(template())


def loaded() -> config.AdminConfig:
    return config.parse_admin_config(filled(), "BOOTSTRAP.md §7.2")


# ------------------------------------------------------------------------- admin.toml


def test_the_template_has_exactly_the_sampled_placeholders():
    assert set(PLACEHOLDER.findall(template())) == set(SAMPLES)
    # §7.4's check for a placeholder left in finds every one of them
    assert all(re.search(r"<[A-Z_]*>", f"<{name}>") for name in SAMPLES)


def test_the_template_is_refused_as_written():
    with pytest.raises(config.AdminConfigError):
        config.parse_admin_config(template())


def test_the_filled_template_loads_with_the_strict_loader():
    cfg = loaded()
    assert cfg.release == "final" and cfg.console_url == "https://admin.example.org"
    assert cfg.destructive_approvals == 2 and cfg.global_bot is not None
    assert cfg.admins == {101, 102} and cfg.deployers == {101}
    assert list(cfg.regions) == [REGION]
    assert [ws.name for ws in cfg.workshops] == [WS]  # the first workshop alone (#417, #427)
    for ws in cfg.workshops:
        assert ws.aws.region == REGION
        assert ws.aws.key_name == f"zab-admin-{ws.name}"
        assert dict(ws.tags) == {"workshop": ws.name, "zab-scope": SCOPE}  # #422
        assert ws.bot.zuliprc == f"/etc/zab-admin/secrets/bots/{ws.name}.zuliprc"
        assert ws.aws.admin_keys == (f"/etc/zab-admin/break-glass/{KEY}.pub",)
        assert ws.alerts_channel is not None
    first = cfg.workshop(WS)
    assert first is not None and first.organizers == frozenset()
    # the global admins approve its plans, each alone for a destructive one, and !new-workshop
    # (§7.1; #558)
    assert cfg.approvers(first) == cfg.admins and len(cfg.admins) == cfg.destructive_approvals
    assert f"~/zab-admin/etc/break-glass/{KEY}.pub" in TEXT  # §8.3 puts that key there


def test_the_runbook_is_deployment_neutral():
    """#427: roles for people, one variable for the release, placeholders for IDs, and the
    lookups of §6.1 and §6.2 for a region's image and group: nothing of one deployment's
    record, which ADMIN §8.4 at v0.2.15 keeps as history."""
    assert sorted({m[0] for m in SPECIFIC.finditer(TEXT)}) == []
    assert "V=<release>" in TEXT and '~/zab-"$V"' in TEXT  # §9's one variable, and its worktree
    # the pattern finds what it is for
    assert SPECIFIC.search("ami-0ba46381132495168") and SPECIFIC.search("Drew's v0.2.4")
    names = ("~/.ssh/zab-drew", "AndrewVSutherland2", "arn:aws:iam::123456789012:x", "ID 1234567")
    assert all(SPECIFIC.search(name) for name in names)


def test_every_key_file_named_is_the_dedicated_key():
    """§2.1: one key of zab's own, `~/.ssh/zab-deployer`, serves all four roles, never a personal
    key (#429, #427). Every `~/.ssh/…` file the runbook names is that key, its `.pub`, the ssh
    config, or, on the server, `authorized_keys` and its replacement (§3.8, §17); or the
    replaced key, set aside under its `.old` name while §17 replaces it everywhere (#516)."""
    named = set(re.findall(r"(?:~|\$HOME)/\.ssh/([A-Za-z0-9_.-]*[A-Za-z0-9])", TEXT))
    assert {KEY, f"{KEY}.pub", "config"} <= named
    old = {f"{KEY}.old", f"{KEY}.old.pub"}
    assert named <= {KEY, f"{KEY}.pub", "config", "authorized_keys", "ak.new", *old}, named
    assert f"  IdentityFile ~/.ssh/{KEY}\n" in TEXT  # §3.6
    assert f'user.signingkey "$HOME/.ssh/{KEY}"' in TEXT  # §2.3


def _asked_keys(monkeypatch) -> set[str]:
    """Every key the loader asks for, whether the file has it or not, as `a.b.c`."""
    asked: set[str] = set()
    original = config._Table.get

    def get(self, key, kind, default=config._MISSING):
        asked.add(re.sub(r"\[\d+\]", "", self.prefix) + key)
        return original(self, key, kind, default)

    monkeypatch.setattr(config._Table, "get", get)
    loaded()
    return asked


def _keys(data: object, prefix: str = "") -> set[str]:
    found: set[str] = set()
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            found.add(prefix + key)
            if isinstance(value, dict) or (
                isinstance(value, list) and value and isinstance(value[0], dict)
            ):
                found |= _keys(value, f"{prefix}{key}.")
    return found


def test_the_template_has_every_key_the_loader_reads(monkeypatch):
    asked = _asked_keys(monkeypatch)
    present = _keys(tomllib.loads(filled()))
    assert "workshop.aws.admin_keys" in asked and "global.email" in asked  # the walk works
    assert asked - present == set(), "the template leaves these to the loader's defaults"
    # every workshop has all of them, not only the first
    for entry in tomllib.loads(filled())["workshop"]:
        ws_keys = _keys(entry, "workshop.")
        assert {k for k in asked if k.startswith("workshop.")} <= ws_keys


def test_the_policy_renders_from_the_template(tmp_path):
    path = tmp_path / "admin.toml"
    path.write_text(filled(), encoding="utf-8")
    assert render_policy.main(["--config", str(path), "--out-dir", str(tmp_path / "policies")]) == 0
    docs = sorted((tmp_path / "policies").glob("zab-admin-*.json"))
    assert docs and all(json.loads(p.read_text()) for p in docs)
    text = " ".join(p.read_text() for p in docs)
    assert SCOPE in text and REGION in text
    assert WS not in text  # no workshop enters it (#422)
    assert not re.search(r"(?<!\d)\d{12}(?!\d)", text)  # no account


DENIES = {"DenyServer", "DenyServerTag", "DenyScopeRetag", "DenyUntag", "DenyExistingInterface"}
EXISTING_INTERFACE = "arn:aws:ec2:*:*:network-interface/eni-*"  # its resource, any region
UNSCOPED = {"Read", "LaunchNetwork"}  # ADMIN §5's two Allows without the workshop condition


def _listed(value: object) -> list:
    return list(value) if isinstance(value, list) else [value]


def _scope(condition: dict, key: str = "zab-scope") -> set[str]:
    """The values of the tag `key` a condition requires, under any operator."""
    return {
        v
        for operator in condition.values()
        for name, values in operator.items()
        if name.endswith(f"/{key}")
        for v in _listed(values)
    }


def test_the_policy_holds_what_section_10_checks(tmp_path):
    """§10's checklist, on §7.2's template: the five denies in every file; every Allow scoped to
    `zab-scope=<SCOPE>` but `Read` and `LaunchNetwork`, and to some `workshop` (never one named)
    but `LaunchFrom`; `[global] regions` only; `m7a.2xlarge` only; no action outside `ec2:`."""
    path = tmp_path / "admin.toml"
    path.write_text(filled(), encoding="utf-8")
    assert render_policy.main(["--config", str(path), "--out-dir", str(tmp_path / "p")]) == 0
    docs = [json.loads(p.read_text()) for p in sorted((tmp_path / "p").glob("zab-admin-*.json"))]
    assert docs
    for sid in DENIES | UNSCOPED:
        assert f"`{sid}`" in TEXT, f"§10 doesn't name {sid}"
    types = set()
    for doc in docs:
        statements = {s["Sid"]: s for s in doc["Statement"]}
        assert {sid for sid, s in statements.items() if s["Effect"] == "Deny"} == DENIES
        cond = {sid: statements[sid].get("Condition", {}) for sid in DENIES}
        assert cond["DenyServer"] == {"Null": {"aws:ResourceTag/zab-admin": "false"}}
        assert cond["DenyServerTag"] == {"Null": {"aws:RequestTag/zab-admin": "false"}}
        retag = cond["DenyScopeRetag"]["ForAnyValue:StringEqualsIgnoreCase"]["aws:TagKeys"]
        assert {"workshop", "zab-admin", "zab-scope"} <= set(retag)
        assert cond["DenyScopeRetag"]["Null"] == {"ec2:CreateAction": "true"}
        assert _listed(statements["DenyUntag"]["Action"]) == ["ec2:DeleteTags"]
        attach = statements["DenyExistingInterface"]
        assert _listed(attach["Action"]) == ["ec2:RunInstances"] and "Condition" not in attach
        assert attach["Resource"] == EXISTING_INTERFACE and f"`{EXISTING_INTERFACE}`" in TEXT
        assert all(statements[sid]["Resource"] == "*" for sid in DENIES - {attach["Sid"]})
        assert UNSCOPED <= set(statements)
        for sid, s in statements.items():
            assert all(a.startswith("ec2:") for a in _listed(s["Action"])), sid
            if s["Effect"] != "Allow":
                continue
            resources = _listed(s["Resource"])
            condition = s.get("Condition", {})
            if sid == "Read":
                assert resources == ["*"]
                assert condition == {"StringEquals": {"aws:RequestedRegion": REGION}}
                continue
            assert all(r.startswith(f"arn:aws:ec2:{REGION}:*:") for r in resources), sid
            if sid == "LaunchNetwork":
                assert condition == {} and all(
                    r.split(":")[-1] in ("subnet/*", "network-interface/*") for r in resources
                )
                continue
            assert _scope(condition) == {SCOPE}, sid
            named = {"false"} if not sid.endswith("LaunchFrom") else set()
            assert _scope(condition, "workshop") == named, sid  # `Null`: any workshop, one
            assert set(condition.get("Null", {}).values()) <= {"false"}, sid
            for operator in condition.values():
                types.update(_listed(operator.get("ec2:InstanceType", [])))
    assert types == {"m7a.2xlarge"}


def section(number: str) -> str:
    """The text of `### <number> …`, up to the next heading of its level or above."""
    found = re.search(rf"^### {re.escape(number)} .*?(?=^##? |^### )", TEXT, re.S | re.M)
    assert found, f"BOOTSTRAP.md has no §{number}"
    return found[0]


def test_the_bootstrap_makes_the_server_and_its_first_workshop_only():
    """§5.3's bots and §5.4's channels are the global bot's and the first workshop's, as the
    template has them, and §12 counts them: every later workshop comes from `!new-workshop`
    (#417), which needs the first for its `like=` (#427)."""
    cfg = loaded()
    bots = re.findall(r"^\| `(Workshop admin[^`]*)` \|", fill(section("5.3")), re.M)
    assert bots == ["Workshop admin", f"Workshop admin · {WS}"]
    assert len(bots) == 1 + len(cfg.workshops)
    channels = re.findall(r"^\| `(#[^`]+)` \|", fill(section("5.4")), re.M)
    assert channels == ["#zab-admins", f"#{WS}-organizers", f"#{WS}-fleet-alerts"]
    counts = re.findall(r"bot check: OK \((\d+) bots\)", TEXT)  # §12's, on the new server
    assert counts and set(counts) == {str(len(bots))}
    assert re.findall(r"^\./bots/(.*)\.zuliprc$", fill(marked_block("text")), re.M) == [WS]
    # one workshop's entry, and the template's last line sends the others to §7.3
    assert template().count("[[workshop]]") == 1
    assert "added by !new-workshop: docs/BOOTSTRAP.md §7.3" in template()


# ---------------------------------------------------------------------------- secrets


def _expected_secrets(tmp_path, capsys) -> set[str]:
    path = tmp_path / "admin.toml"
    path.write_text(filled(), encoding="utf-8")
    capsys.readouterr()
    assert config.main(["--config", str(path), "zuliprcs"]) == 0
    zuliprcs = capsys.readouterr().out.split()
    root = "/etc/zab-admin/secrets/"
    assert zuliprcs and all(p.startswith(root) for p in zuliprcs)
    # deploy.sh's install_secrets: the two every server has, then admin.toml's zuliprcs
    found = re.search(r"local -a want=\(([^)]*)\)", DEPLOY.read_text(encoding="utf-8"))
    assert found, "deploy.sh's install_secrets changed: update this test and BOOTSTRAP.md §8.1"
    return set(found[1].split()) | {p.removeprefix(root) for p in zuliprcs}


def test_the_secrets_listing_is_what_deploy_installs(tmp_path, capsys):
    expected = _expected_secrets(tmp_path, capsys)
    listing = fill(marked_block("text")).split()
    assert all(line.startswith("./") for line in listing)
    assert listing == sorted(listing)  # as `find . -type f | sort` prints it
    assert {line[2:] for line in listing} == expected
    assert expected == {
        "provisioner.zuliprc",
        "github-token",
        "global.zuliprc",
        f"bots/{WS}.zuliprc",
    }


def test_every_secrets_path_named_is_one_deploy_installs(tmp_path, capsys):
    expected = _expected_secrets(tmp_path, capsys)
    # the first workshop's name as `<WS>` in prose and `"$WS"` in commands, the sample's here
    text = fill(TEXT).replace('"$WS"', WS)
    named = set(re.findall(r"~/zab-admin/secrets/([A-Za-z0-9_./-]*[A-Za-z0-9])", text))
    files = {n for n in named if "." in n or n == "github-token"}
    assert files >= expected - {"global.zuliprc"} - {"provisioner.zuliprc"} | {"github-token"}
    assert files <= expected, files - expected


def _function(name: str) -> str:
    """The body of deploy.sh's shell function `name`."""
    text = DEPLOY.read_text(encoding="utf-8")
    found = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}", text, re.S | re.M)
    assert found, f"deploy.sh has no {name}: update this test and BOOTSTRAP.md §16, §17"
    return found[1]


def test_a_replaced_secret_gets_the_owner_and_mode_deploy_gives_it():
    """#406: §16 installs a secret on a running server by hand, as `install_secrets` does at the
    bootstrap: `root:zab-admin`, 0640, under /etc/zab-admin/secrets/, which the bot's own start
    checks (`checks.check_files`). Every such install the runbook quotes says so, and §16's
    read-back shows it."""
    body = _function("install_secrets")
    assert 'install -m 0640 "$PAYLOAD/secrets/$rel" "$ETC/secrets/$rel"' in body
    assert 'own root:zab-admin "$ETC/secrets/$rel"' in body
    installs = re.findall(r"sudo install (-o \S+ -g \S+ -m \d+) \S+ (/etc/zab-admin/\S*)", TEXT)
    secrets = [flags for flags, dest in installs if dest.startswith("/etc/zab-admin/secrets/")]
    assert secrets and set(secrets) == {"-o root -g zab-admin -m 0640"}
    assert "# root:zab-admin 640 /etc/zab-admin/secrets/<F>" in section_text("16")
    assert "sudo systemctl restart zab-admin-bot.service" in section_text("16")


def test_the_trust_files_get_the_owner_and_mode_deploy_gives_them():
    """#516: §17 and §6.5 install `admin.toml`, `allowed_signers`, `backup_recipients` and the
    break-glass keys by hand, as `install_etc` does at the bootstrap: root's, 0644."""
    body = _function("install_etc")
    assert 'install -m 0644 "$PAYLOAD/etc/$f" "$ETC/$f"' in body
    assert 'install -m 0644 "$f" "$ETC/break-glass/"' in body
    installs = re.findall(r"sudo install (-o \S+ -g \S+ -m \d+) (.*?) (/etc/zab-admin/\S*)", TEXT)
    trust = [(flags, dest) for flags, _, dest in installs if "/secrets/" not in dest]
    assert {dest for _, dest in trust} >= {"/etc/zab-admin/", "/etc/zab-admin/break-glass/"}
    assert {flags for flags, _ in trust} == {"-o root -g root -m 0644"}


#: `age` as a shell function: `-R FILE` "encrypts" stdin unchanged, refusing an empty recipients
#: file as age refuses a bad one; `-d … FILE` "decrypts" FILE unchanged, or fails when $NOKEY.
AGE_STUB = r"""
age() {
  if [ "$1" = -R ]; then
    [ -s "$2" ] || { echo "age: no recipients" >&2; return 1; }
    cat; return
  fi
  [ -z "$NOKEY" ] || { echo "age: no identity matched" >&2; return 1; }
  eval "cat \"\${$#}\""
}
"""


def seal_block() -> str:
    """§14's block that seals `secrets/` into the bundle, without its `D=<…>` line."""
    found = [b for _, b in FENCE.findall(section_text("14")) if "secrets.tar.age.new" in b]
    assert len(found) == 1
    lines = found[0].splitlines()
    assert lines[0].startswith("D=<")
    return "\n".join(lines[1:]) + "\n"


@pytest.mark.parametrize("fault", [None, "recipients", "key"])
def test_the_bundle_is_replaced_only_once_the_new_one_opens(tmp_path, fault):
    """Codex on #586: sealing straight into `secrets.tar.age` lost the old bundle when `age`
    failed, and the `rm` after it the plaintext. §14's block, run with a stand-in `age`, seals
    into `.new`, under `pipefail`, compares the new bundle's listing with `secrets/`, and only
    then replaces the bundle and removes the plaintext. A bad recipients file, or a key that
    can't open the new bundle, leaves both as they were."""
    home = tmp_path / "home"
    (home / "zab-admin" / "etc").mkdir(parents=True)
    recipients = "" if fault == "recipients" else "ssh-ed25519 AAAAC3Nz new\n"
    (home / "zab-admin" / "etc" / "backup_recipients").write_text(recipients)
    (home / "zab-admin" / "secrets.tar.age").write_text("the old bundle\n")
    s = tmp_path / "s"
    (s / "secrets" / "bots").mkdir(parents=True)
    for name in ("github-token", "provisioner.zuliprc", "global.zuliprc", "bots/ws.zuliprc"):
        (s / "secrets" / name).write_text("secret\n")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "D": str(s)}
    env |= {"NOKEY": "1"} if fault == "key" else {}
    done = subprocess.run(  # noqa: S603 - the runbook's own block, with age stubbed
        ["/bin/bash", "-c", AGE_STUB + seal_block()], capture_output=True, text=True,
        env=env, timeout=60, check=False,
    )  # fmt: skip
    bundle = home / "zab-admin" / "secrets.tar.age"
    if fault is None:
        assert done.returncode == 0 and done.stdout.strip() == "sealed", done.stderr
        assert not (s / "secrets").exists() and not Path(f"{bundle}.new").exists()
        listed = subprocess.run(  # noqa: S603
            ["/usr/bin/env", "tar", "-tf", str(bundle)], capture_output=True, text=True, check=True
        ).stdout.split()
        assert "secrets/bots/ws.zuliprc" in listed and "secrets/github-token" in listed
    else:
        assert done.returncode != 0 and "sealed" not in done.stdout
        assert bundle.read_text() == "the old bundle\n"  # the old bundle stays
        assert (s / "secrets" / "github-token").read_text() == "secret\n"  # and the plaintext


def test_a_replaced_key_leaves_no_vm_with_the_old_one():
    """Codex on #586: `launch.py --admin-key` gives the break-glass key to the spares too, which
    `join_server.py` can't reach (it reads the roster only, and `read_roster` refuses a
    `zab-spare-N` row), so §17 swaps the key on the roster's VMs and replaces the spares."""
    launch = (ROOT / "workshop" / "aws" / "launch.py").read_text(encoding="utf-8")
    assert "for row in participants(rows) + list(spares or [])" in launch  # spares launched alike
    text = section_text("17")
    assert "join_server.py" in text and "--server-key ~/.ssh/zab-deployer.pub" in text
    assert "--server-key ~/.ssh/zab-deployer.old.pub --remove" in text
    assert "`!teardown spares`" in text and "`!launch all spares=2`" in text
    assert text.index("`!teardown spares`") < text.index("`!launch all spares=2`")
    with pytest.raises(ValueError, match="spare"):  # why join_server.py can't take the spares
        sys.path.insert(0, str(ROOT / "workshop" / "aws"))
        try:
            from aws_common import validate_row
        finally:
            sys.path.pop(0)
        validate_row({"slug": "zab-spare-1"})


def section_text(number: str) -> str:
    """The text of `## <number>. …`, up to the next heading of its level."""
    found = re.search(rf"^## {re.escape(number)}\. .*?(?=^## |\Z)", TEXT, re.S | re.M)
    assert found, f"BOOTSTRAP.md has no §{number}"
    return found[0]


# ------------------------------------------------------------------ the scripts' options


def deploy_options() -> tuple[set[str], set[str]]:
    """(every option of deploy.sh's laptop half, those that take a value), from its case arms."""
    text = DEPLOY.read_text(encoding="utf-8")
    body = re.search(r"^laptop_main\(\) \{\n(.*?)^\}", text, re.S | re.M)
    assert body, "deploy.sh has no laptop_main"
    # digits too: --expect-diff-sha256 (#772)
    arms = re.findall(r"^\s+((?:--[a-z0-9-]+)(?:\s*\|\s*--[a-z0-9-]+)*)\)(.*)$", body[1], re.M)
    every, valued = set(), set()
    for names, rest in arms:
        flags = {n.strip() for n in names.split("|")}
        every |= flags
        if "needs a value" in rest:
            valued |= flags
    assert {"--bootstrap", "--wheel", "--host", "--etc", "--secrets", "--review"} <= every
    assert valued >= {"--host", "--etc", "--secrets", "--review", "--since", "--out"}
    assert {"--with-wheel", "--expect-diff-sha256"} <= every and "--expect-diff-sha256" in valued
    return every, valued


def help_options(argv: list[str]) -> tuple[set[str], set[str], set[str]]:
    """(its options, those that take a value, its positional's choices) from `argv --help`."""
    done = subprocess.run(  # noqa: S603 - an argument list of this module's own
        [*argv, "--help"], capture_output=True, text=True, check=True, timeout=60, cwd=ROOT
    )
    every, valued = set(), set()
    option = re.compile(r"^\s+(--[a-z][a-z-]*)(?: ([A-Z][A-Z_=]*))?", re.M)
    for name, metavar in option.findall(done.stdout):
        every.add(name)
        if metavar:
            valued.add(name)
    found = re.search(r"\{([a-z,-]+)\}", done.stdout)
    return every, valued, set(found[1].split(",")) if found else set()


#: Where a token sequence names a command, the index of its first argument, or None.
Match = Callable[[list[str], int], int | None]


def ends_with(name: str) -> Match:
    return lambda tokens, i: i + 1 if tokens[i].endswith(name) else None


def module(name: str, sub: str | None = None) -> Match:
    """`-m NAME` (and its subcommand `sub`)."""

    def match(tokens: list[str], i: int) -> int | None:
        if i == 0 or tokens[i - 1] != "-m" or tokens[i] != name:
            return None
        if sub is None:
            return i + 1
        return i + 2 if tokens[i + 1 : i + 2] == [sub] else None

    return match


#: The argparse commands the runbook quotes: how to find each, and its `--help`.
ARGPARSE: dict[str, tuple[Match, list[str]]] = {
    "render_policy.py": (
        ends_with("render_policy.py"),
        [sys.executable, "-I", str(ROOT / "workshop" / "adminserver" / "render_policy.py")],
    ),
    "python -m zab.admin.config": (
        module("zab.admin.config"),
        [sys.executable, "-m", "zab.admin.config"],
    ),
    "python -m zab.admin bot": (
        module("zab.admin", "bot"),
        [sys.executable, "-m", "zab.admin", "bot"],
    ),
}


def code_texts() -> list[tuple[str, bool]]:
    """Every fenced block (lines joined at `\\`) and every inline code span of the runbook, each
    with whether it is a block, where a command is quoted whole. A block in a list item, indented,
    counts too, its indentation removed (review of #586: §17's upgrade is one)."""
    texts = []
    for indent, _, body in ANY_FENCE.findall(TEXT):
        body = re.sub(rf"^{indent}", "", body, flags=re.M)
        texts.append((body.replace("\\\n", " "), True))
    prose = ANY_FENCE.sub("", TEXT)
    texts += [(span, False) for span in re.findall(r"``(.+?)``", prose)]
    prose = re.sub(r"``.+?``", "", prose)
    texts += [(span, False) for span in re.findall(r"`([^`\n]+)`", prose)]
    return texts


def invocations(match: Match) -> list[tuple[list[str], bool]]:
    """The argument lists of every quoted command `match` finds, each with whether it is in a
    fenced block."""
    found = []
    for text, fenced in code_texts():
        for line in text.splitlines():
            line = re.sub(r"<[^<>\n]*>", "X", line)  # a placeholder is one word
            lex = shlex.shlex(line, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            try:
                tokens = list(lex)
            except ValueError:  # prose with an apostrophe, never a command
                continue
            for i in range(len(tokens)):
                start = match(tokens, i)
                if start is None:
                    continue
                args = []
                for arg in tokens[start:]:
                    if arg in OPERATORS:
                        break
                    args.append(arg)
                found.append((args, fenced))
    return found


def check_options(args: list[str], every: set[str], valued: set[str], *, eq: bool) -> list[str]:
    positional, i = [], 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            name, has_eq, _ = arg.partition("=")
            assert name in every, f"{args}: {name} is not an option of the script"
            assert eq or not has_eq, f"{args}: the script takes `{name} VALUE`, not `{arg}`"
            if name in valued and not has_eq:
                assert i + 1 < len(args) and not args[i + 1].startswith("--"), (
                    f"{args}: {name} needs a value"
                )
                i += 1
        else:
            positional.append(arg)
        i += 1
    return positional


def test_every_deploy_command_uses_options_deploy_accepts():
    every, valued = deploy_options()
    calls = invocations(ends_with("deploy.sh"))
    whole = set()
    for args, fenced in calls:
        positional = check_options(args, every, valued, eq=False)
        used = {a for a in args if a.startswith("--")}
        if not fenced or not used:  # a mention in prose, or `less …/deploy.sh`
            continue
        # a command in a block is quoted whole: the shape deploy.sh's laptop half requires
        if used & {"--bootstrap", "--wheel"}:
            assert {"--host", "--etc"} <= used and positional == ["$V"], args
            assert "--bootstrap" not in used or "--secrets" in used, args
            whole |= used & {"--bootstrap", "--wheel"}
        elif "--review" in used:  # a host only with --base-signers, which checks the base (#587)
            assert "--etc" in used and positional == [], args
            assert "--host" not in used or "--base-signers" in used, args
            assert "--secrets" not in used, args
            whole.add("--review")
        elif "--host" in used:  # an upgrade (§17's first after a key change)
            assert "--etc" in used and positional == ["$V"], args
            assert "--secrets" not in used, args
            whole.add("upgrade")
        else:
            pytest.fail(f"{args}: a block quotes a bootstrap, a wheel, a review or an upgrade")
    assert whole == {"--bootstrap", "--wheel", "--review", "upgrade"}


@pytest.mark.parametrize("name", sorted(ARGPARSE))
def test_every_command_uses_options_its_argparse_accepts(name):
    match, argv = ARGPARSE[name]
    every, valued, choices = help_options(argv)
    assert "--config" in every
    calls = invocations(match)
    assert any(fenced for _, fenced in calls), f"BOOTSTRAP.md quotes no {name} command"
    for args, _ in calls:
        positional = check_options(args, every, valued, eq=True)
        assert len(positional) == (1 if choices else 0), f"{args}: positionals {positional}"
        assert positional[0] in choices if choices else True, f"{args}: not one of {choices}"


def test_the_option_check_catches_a_wrong_option():
    every, valued = deploy_options()
    with pytest.raises(AssertionError):
        check_options(["--server", "x", "v1.2.3"], every, valued, eq=False)  # the server half's
    with pytest.raises(AssertionError):
        check_options(["--host"], every, valued, eq=False)
    with pytest.raises(AssertionError):
        check_options(["--etc=x"], every, valued, eq=False)


# ---------------------------------------------------------------------- bot commands

BOT = f"Workshop admin · {WS}"
CTX = Context(  # a workshop with one row and one spare (§17 tears the spares down)
    901, BOT, re.compile(config.RELEASE_PATTERNS["final"]), Roster(("alice",), ("zab-spare-1",))
)
#: A command named by its verb alone, as prose does.
NAMES = frozenset(
    ["!" + name for name in (*SPECS, *_GROUPS, *LATER)]
    + [f"!{verb} {on}" for verb in ("protect", "waker") for on in ("on", "off")]
)


#: The server's `[global]` bot, for its verbs (§13.4's `!upgrade-server`).
GLOBAL = Context(902, "Workshop admin", re.compile(config.RELEASE_PATTERNS["final"]), None)
#: A span's placeholders, as a deployer fills them in: a release, and their review's SHA-256.
FILLED = (
    (re.compile(r"\bvX\.Y\.Z\b"), "v1.2.3"),
    (re.compile(r"sha256=<[^>]*>"), "sha256=" + "0" * 64),
)


def test_every_bot_command_parses():
    spans = [s for s, _ in code_texts() if s.startswith("!") and "\n" not in s]
    assert {"!help", "!version", "!status all", "!roster show", "!plans"} <= set(spans)
    for span in spans:
        if span in NAMES:
            continue
        verb = span[1:].split()[0]
        context = GLOBAL if verb in SPECS and SPECS[verb].only_global else CTX
        for placeholder, value in FILLED:
            span = placeholder.sub(value, span)
        assert isinstance(parse(span, context), Command), span


# ---------------------------------------------------------------------- the ID reader


def id_reader() -> str:
    block = next(body for _, body in FENCE.findall(TEXT) if "users/me/subscriptions" in body)
    found = re.search(r"<<'EOF'\n(.*?)^EOF$", block, re.S | re.M)
    assert found, "§5.6's reader is a heredoc"
    return found[1]


class FakeZulip(BaseHTTPRequestHandler):
    KEYS: ClassVar = {"bot@example.org": "k-bot-secret", "prov@example.org": "k-prov-secret"}
    USERS: ClassVar = {101: "Drew", 950: "zab-provisioner", 901: BOT}
    OWNER: ClassVar = {"bot_owner_id": 950}  # the bot's owner field, as users/me returns it

    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        email, _, key = base64.b64decode(auth.removeprefix("Basic ")).decode().partition(":")
        if self.KEYS.get(email) != key:
            self.send_response(401)
            self.end_headers()
            return
        path = self.path.removeprefix("/api/v1/")
        bot = email == "bot@example.org"
        if path == "users/me":
            body = {"user_id": 901 if bot else 950, "full_name": BOT if bot else "zab-provisioner",
                    "email": email, "is_bot": bot, "role": 400 if bot else 300}  # fmt: skip
            if bot:  # Zulip's format_user_row gives a bot its bot_owner_id
                body.update(self.OWNER)
        elif path == "users/me/subscriptions":
            subs = [{"stream_id": 41, "name": f"{WS}-organizers", "invite_only": True,
                     "history_public_to_subscribers": True}]  # fmt: skip
            if not bot:
                subs = [{"stream_id": 42, "name": f"{WS}-fleet-alerts", "invite_only": True,
                         "history_public_to_subscribers": False},
                        {"stream_id": 7, "name": "general", "invite_only": False,
                         "history_public_to_subscribers": True, "is_web_public": False},
                        {"stream_id": 8, "name": "news", "invite_only": False,
                         "history_public_to_subscribers": True, "is_web_public": True}]  # fmt: skip
            body = {"subscriptions": subs}
        elif m := re.fullmatch(r"streams/(\d+)/members", path):
            body = {"subscribers": [101, 901] if m[1] == "41" else [950, 101]}
        elif m := re.fullmatch(r"users/(\d+)", path):
            body = {"user": {"full_name": self.USERS[int(m[1])]}}
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def read_ids(tmp_path, handler=FakeZulip) -> subprocess.CompletedProcess:
    """§5.6's reader on a bot's and the provisioner's zuliprc, against `handler`."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    site = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        (tmp_path / "bots").mkdir()
        for name, email in (("bots/x.zuliprc", "bot@example.org"),
                            ("provisioner.zuliprc", "prov@example.org")):  # fmt: skip
            key = FakeZulip.KEYS[email]
            (tmp_path / name).write_text(f"[api]\nemail={email}\nkey={key}\nsite={site}\n")
        return subprocess.run(
            [sys.executable, "-I", "-", "bots/x.zuliprc", "provisioner.zuliprc"],
            input=id_reader(), capture_output=True, text=True, cwd=tmp_path, timeout=60,
        )  # fmt: skip
    finally:
        server.shutdown()


def test_the_id_reader_prints_ids_and_never_a_key(tmp_path):
    done = read_ids(tmp_path)
    assert done.returncode == 0, done.stderr
    out = done.stdout
    bot = f'bots/x.zuliprc: user 901 "{BOT}" <bot@example.org>, bot, owner 950 zab-provisioner\n'
    assert bot in out  # §5.3: the provisioner owns the admin bots
    organizers = f"  #{WS}-organizers: channel 41, private, shared history, members: 101 Drew, "
    assert f"{organizers}901 {BOT}\n" in out
    assert 'provisioner.zuliprc: user 950 "zab-provisioner" <prov@example.org>, moderator\n' in out
    alerts = f"  #{WS}-fleet-alerts: channel 42, private, protected history, members: 101 Drew, "
    assert f"{alerts}950 zab-provisioner\n" in out
    # §5.6: a public channel says so, with no members (Drew's alerts channel, at first)
    assert "  #general: channel 7, public\n" in out
    assert "  #news: channel 8, web-public\n" in out
    assert "secret" not in out + done.stderr


@pytest.mark.parametrize("owner", [{"bot_owner_id": None}, {}], ids=["null", "absent"])
def test_the_id_reader_says_when_a_bot_has_no_owner(tmp_path, owner):
    """Review of #434 (#435): a bot without an owner, its `bot_owner_id` null or left out,
    reads `owner none`, an owner other than `zab-provisioner`, which §5.6 has fixed (§5.3)."""

    class NoOwner(FakeZulip):
        OWNER: ClassVar = owner

    done = read_ids(tmp_path, NoOwner)
    assert done.returncode == 0, done.stderr
    bot = f'bots/x.zuliprc: user 901 "{BOT}" <bot@example.org>, bot, owner none\n'
    assert bot in done.stdout
    assert "secret" not in done.stdout + done.stderr


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (None, "missing, or not a zuliprc (no [api] section)"),
        ("[other]\nkey=k-secret\n", "missing, or not a zuliprc (no [api] section)"),
        ("[api]\nemail=b@example.org\nkey=k-sec\n", "its [api] section lacks site, email or key"),
        ("key=k-secret\n", "not a zuliprc (it doesn't parse)"),  # no section header
        ("[api]\nsite=https://x.example\nk-secret\n", "not a zuliprc (it doesn't parse)"),
        (b"\xffkey=k-secret\n", "not a zuliprc (it doesn't parse)"),  # not UTF-8, in any locale
    ],
)
def test_the_id_reader_names_a_file_it_cant_use(tmp_path, text, error):
    """Drew's run of Oct 17: a zuliprc not where §5.3's table puts it gave `KeyError: 'api'`,
    since `configparser.read` skips a missing file. The reader now stops with one line naming
    the file, before any request. A file configparser can't parse stops it the same way: its
    error would quote the offending line, which may be the key (review of #430)."""
    if isinstance(text, bytes):
        (tmp_path / "global.zuliprc").write_bytes(text)
    elif text is not None:
        (tmp_path / "global.zuliprc").write_text(text)
    done = subprocess.run(
        [sys.executable, "-I", "-", "global.zuliprc"],
        input=id_reader(), capture_output=True, text=True, cwd=tmp_path, timeout=60,
    )  # fmt: skip
    assert done.returncode == 1 and done.stdout == ""
    assert done.stderr == f"global.zuliprc: {error}\n"
    assert "k-sec" not in done.stdout + done.stderr


# ------------------------------------------------------------ §6.5: the policies replaced

#: `aws` as a shell function over a fake IAM: `$IAM/<name>` holds a managed policy's versions,
#: one per line, the default last; `$IAM/attached` lists the role's policies; each call is logged
#: as `<subcommand> <policy>` in `$IAM/log`. IAM refuses to delete a policy that still has
#: versions besides its default. `$FAIL` names a call that fails, as `<subcommand> <policy>`.
IAM_STUB = r"""
aws() {
  local sub=$2 arn= name= q= vid= f n v
  shift 2
  while [ $# -gt 0 ]; do
    case $1 in
      --policy-arn) arn=$2; shift ;;
      --policy-name) name=$2; shift ;;
      --query) q=$2; shift ;;
      --version-id) vid=$2; shift ;;
    esac
    shift
  done
  n=${arn##*/}
  echo "$sub ${name:-$n}" >> "$IAM/log"
  [ "$FAIL" != "$sub ${name:-$n}" ] || return 1
  case $sub in
    list-policies)
      f=${q#*==\'}; f=${f%%\'*}
      [ ! -e "$IAM/$f" ] || echo "arn:aws:iam::123456789012:policy/$f" ;;
    list-policy-versions)
      case $q in
        'length(Versions)') wc -l < "$IAM/$n" | tr -d ' ' ;;
        sort_by*) head -n 1 "$IAM/$n" ;;
        *) sed '$d' "$IAM/$n" | tr '\n' ' ' ;;
      esac ;;
    delete-policy-version) grep -vx "$vid" "$IAM/$n" > "$IAM/t"; mv "$IAM/t" "$IAM/$n" ;;
    create-policy-version)
      [ "$(wc -l < "$IAM/$n")" -lt 5 ] || return 1
      v=$(( $(sed 's/v//' "$IAM/$n" | sort -n | tail -1) + 1 ))
      echo "v$v" >> "$IAM/$n"; echo "v$v" ;;
    create-policy) echo v1 > "$IAM/$name"; echo "arn:aws:iam::123456789012:policy/$name" ;;
    attach-role-policy) grep -qx "$n" "$IAM/attached" || echo "$n" >> "$IAM/attached" ;;
    detach-role-policy) grep -vx "$n" "$IAM/attached" > "$IAM/t"; mv "$IAM/t" "$IAM/attached" ;;
    delete-policy) [ "$(wc -l < "$IAM/$n")" -eq 1 ] || return 1; rm "$IAM/$n" ;;
    list-attached-role-policies) sort "$IAM/attached" | tr '\n' ' '; echo ;;
  esac
}
"""


def policy_blocks() -> tuple[str, str]:
    """§6.5's step 3: the block that applies the new render, and the one that removes its
    temporary copies once read back, each out of its list item."""
    fenced = [
        re.sub(r"^ {3}", "", b, flags=re.M)
        for b in re.findall(r"^ {3}```sh\n(.*?)^ {3}```", section("6.5"), re.S | re.M)
    ]
    apply = [b for b in fenced if "create-policy-version" in b]
    cleanup = [b for b in fenced if "zab-admin-next-*)" in b]
    assert len(apply) == 1 and len(cleanup) == 1
    return apply[0], cleanup[0]


def fake_iam(tmp_path, in_force, rendered, *, local=True, unattached=()) -> tuple[Path, dict]:
    """A fake IAM with `in_force`'s policies attached (`zab-admin-1` at five versions), and
    `unattached` ones that exist but aren't; the new render's files in `policies.new`, and the
    render in force in `policies/` when `local` (a first machine; a second has none)."""
    iam, policies = tmp_path / "iam", tmp_path / "zab-admin" / "policies"
    new = tmp_path / "zab-admin" / "policies.new"
    for path in (iam, new):
        path.mkdir(parents=True)
    if local:
        policies.mkdir()
    for i in in_force:
        if local:
            (policies / f"zab-admin-{i}.json").write_text("{}")
        (iam / f"zab-admin-{i}").write_text("v1\nv2\nv3\nv4\nv5\n" if i == 1 else "v1\nv2\n")
    for name in unattached:
        (iam / name).write_text("v1\n")
    (iam / "attached").write_text("".join(f"zab-admin-{i}\n" for i in in_force))
    (iam / "log").write_text("")
    for i in rendered:
        (new / f"zab-admin-{i}.json").write_text("{}")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "IAM": str(iam), "P": "prof",
           "NEWP": str(new)}  # fmt: skip
    return iam, env


def run_block(block: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - the runbook's own block, with aws stubbed
        ["/bin/bash", "-c", IAM_STUB + block], capture_output=True, text=True, env=env,
        timeout=60, check=False,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("in_force", "rendered", "deleted"),
    [
        ((1,), (1, 2), ()),  # a region added: a second file
        ((1, 2), (1, 2), ()),  # statements move between two existing policies (Codex on #582)
        ((1, 2), (1,), (2,)),  # a region removed: the extra policy goes (Codex on #582)
        ((1,), (1,), ()),
    ],
)
@pytest.mark.parametrize("local", [True, False], ids=["first-machine", "second-machine"])
def test_the_policies_in_force_become_the_new_render(tmp_path, in_force, rendered, deleted, local):
    """§6.5 step 3 against a fake IAM (Codex and the reviewer on #582, the reviewer on #586).
    The apply attaches the whole new render as temporary copies before it replaces any policy,
    so the role never holds less than either render, even while statements move between files;
    it replaces each policy that stays (at five versions, the oldest deleted first), creates and
    attaches each new one, and detaches and deletes each one the new render lacks, read from
    IAM, not from the machine's `policies/`, which a second machine lacks. The last block then
    removes the copies. Nothing printed is an ARN."""
    iam, env = fake_iam(tmp_path, in_force, rendered, local=local)
    apply, cleanup = policy_blocks()
    done = run_block(apply, env)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[-2] == "applied"
    names = [f"zab-admin-{i}" for i in rendered]
    assert lines[-1].split() == sorted(names + [f"zab-admin-next-{i}" for i in rendered])
    assert "zab-admin-1: deleted v1" in lines and "zab-admin-1: v6, now the default" in lines
    for i in rendered:
        assert (f"zab-admin-{i}: created" in lines) == (i not in in_force)
    calls = (iam / "log").read_text().splitlines()
    copies = [k for k, c in enumerate(calls) if c.startswith("attach-role-policy zab-admin-next-")]
    changes = [
        k
        for k, c in enumerate(calls)
        if c.split()[0] in ("create-policy-version", "detach-role-policy", "delete-policy")
        and not c.split()[1].startswith("zab-admin-next-")
    ]
    assert len(copies) == len(rendered) and max(copies) < min(changes)  # the union first
    for i in deleted:
        assert f"zab-admin-{i}: detached and deleted" in lines
        assert not (iam / f"zab-admin-{i}").exists()
    done = run_block(cleanup, env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[-1].split() == names
    assert not list(iam.glob("zab-admin-next-*"))
    assert "123456789012" not in done.stdout + done.stderr


def test_a_run_cut_short_after_a_create_is_finished_by_the_next(tmp_path):
    """Codex on #582: a policy that a run cut short created but didn't attach is attached by the
    next run, though it exists already, before any policy is replaced."""
    iam, env = fake_iam(tmp_path, (1,), (1, 2), unattached=("zab-admin-2", "zab-admin-next-1"))
    apply, _ = policy_blocks()
    done = run_block(apply, env)
    assert done.returncode == 0 and done.stdout.splitlines()[-2] == "applied", done.stderr
    assert "zab-admin-next-1: v2, now the default" in done.stdout
    calls = (iam / "log").read_text().splitlines()
    first = calls.index("create-policy-version zab-admin-1")
    assert calls.index("attach-role-policy zab-admin-next-1") < first
    assert calls.index("attach-role-policy zab-admin-2") > first  # in its own turn, attached


@pytest.mark.parametrize(
    "fail",
    ["attach-role-policy zab-admin-next-1", "create-policy-version zab-admin-1"],
    ids=["a-copy-not-attached", "a-replacement"],
)
def test_a_failure_stops_the_apply_there(tmp_path, fail):
    """Codex on #582: a failed step stops the block where it is. A copy not attached replaces
    nothing; a replacement that fails removes nothing, so `zab-admin-2`, which holds what the
    failed one would have taken over, stays attached."""
    iam, env = fake_iam(tmp_path, (1, 2), (1,))
    apply, _ = policy_blocks()
    done = run_block(apply, {**env, "FAIL": fail})
    assert "stopped: nothing after the failed step ran" in done.stdout
    calls = (iam / "log").read_text().splitlines()
    assert not [c for c in calls if c.startswith(("detach", "delete-policy "))]
    assert "zab-admin-2" in (iam / "attached").read_text().split()
    if fail.startswith("attach"):
        assert "create-policy-version zab-admin-1" not in calls
