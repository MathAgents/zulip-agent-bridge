"""`workshop/adminserver/ssh-rule.sh` (#674), offline: `aws`, `curl` and `ssh-add` are stand-ins
on PATH, with HOME in the test's directory, so no real account, network or agent is reached.

The stand-in `aws` keeps a security group's rules in a JSON file and answers the six calls the
script makes as the AWS CLI's text output does: the group lookups, the port-22 rule IDs, a
revoke, an authorize, and the read-back of BOOTSTRAP §3.1."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "workshop" / "adminserver" / "ssh-rule.sh"
BOOTSTRAP = (ROOT / "docs" / "BOOTSTRAP.md").read_text()
FILES_README = (ROOT / "workshop" / "files" / "README.md").read_text()

KEYGEN = shutil.which("ssh-keygen") or "ssh-keygen"
pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or shutil.which("bash") is None,
    reason="needs ssh-keygen and bash",
)

HERE_ADDR, HOME_ADDR = "192.0.2.7", "198.51.100.1"  # documentation addresses (RFC 5737)
PROFILE, REGION = "owner", "us-east-2"  # the saved values of every Laptop (`--save-defaults`)
SG, FILES_SG = "sg-0abc1234", "sg-0f11e5"

AWS = r"""
import json, os, sys

state_path = os.environ["AWS_STATE"]
state = json.load(open(state_path))
argv = sys.argv[1:]
with open(os.environ["AWS_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")
if state.get("fail") and state["fail"] in argv:
    print("An error occurred (UnauthorizedOperation): you can't", file=sys.stderr)
    sys.exit(254)

def opt(name):
    return argv[argv.index(name) + 1] if name in argv else None

assert argv[0] == "--profile" and argv[2] == "--region" and argv[4] == "ec2", argv
command = argv[5]
groups = state["groups"]

if command == "describe-security-groups" and "--filters" in argv:
    filters = argv[argv.index("--filters") + 1 : argv.index("--query")]
    assert filters[1] == "Name=tag-key,Values=zab-admin", filters  # whatever the name (#772)
    name = filters[0].removeprefix("Name=group-name,Values=")
    print("\t".join(g for g, v in groups.items() if v["name"] == name and v["tagged"]))
elif command == "describe-security-groups":
    rules = groups[opt("--group-ids")]["rules"]
    perms = {}
    for r in rules:
        perms.setdefault((r["proto"], r["from"], r["to"]), []).append(r)
    for (proto, a, b), rs in perms.items():
        cols = [proto, str(a), str(b)]
        for key in ("cidr", "cidr6", "group", "prefix"):
            cols.append(",".join(r[key] for r in rs if r.get(key)))
        print("\t".join(cols))
elif command == "describe-security-group-rules":
    group = opt("--filters").split("Values=")[1]
    ids = [r["id"] for r in groups[group]["rules"] if r["from"] == 22 and r["to"] == 22]
    print(state.get("ids_say", "\t".join(ids)))
elif command == "revoke-security-group-ingress":
    rules = groups[opt("--group-id")]["rules"]
    rid = opt("--security-group-rule-ids")
    groups[opt("--group-id")]["rules"] = [r for r in rules if r["id"] != rid]
    print(state.get("revoke_says", "True"))
elif command == "authorize-security-group-ingress":
    perm = opt("--ip-permissions")
    cidr = perm.split("CidrIp=")[1].split(",")[0]
    desc = perm.split("Description=")[1].split("}")[0]
    rules = groups[opt("--group-id")]["rules"]
    rules.append({"id": f"sgr-new{len(rules)}", "proto": "tcp", "from": 22, "to": 22,
                  "cidr": cidr, "desc": desc})
    print("True")
elif command == "describe-instances" and "--filters" in argv:
    print("\t".join(i["id"] for i in state["instances"] if i["state"] == "running"))
elif command == "describe-instances":
    print("\t".join(i["group"] for i in state["instances"] if i["id"] == opt("--instance-ids")))
else:
    sys.exit(f"unexpected: {argv}")
json.dump(state, open(state_path, "w"))
"""


def rule(rid: str, port: int | str, proto: str = "tcp", **source: str) -> dict:
    """An ingress rule: `port`, or `-1` for all traffic; its source as `cidr=`, `cidr6=` …"""
    if port == -1:
        return {"id": rid, "proto": "-1", "from": "None", "to": "None", **source}
    return {"id": rid, "proto": proto, "from": port, "to": port, **source}


WEB = [rule("sgr-0a1", 443, cidr="0.0.0.0/0"), rule("sgr-0a2", 80, cidr="0.0.0.0/0")]
OLD_22 = [rule("sgr-0b1", 22, cidr="203.0.113.9/32"), rule("sgr-0b2", 22, cidr6="2001:db8::/64")]


def new_key(path: Path) -> str:
    """A key without a passphrase at `path`, for the test alone; its `ssh-keygen -l` line."""
    make = [KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", path.name, "-f", str(path)]
    subprocess.run(make, check=True)  # noqa: S603
    read = [KEYGEN, "-l", "-f", f"{path}.pub"]
    return subprocess.run(read, capture_output=True, text=True, check=True).stdout.strip()  # noqa: S603


class Laptop:
    """The script's world: HOME, the stand-ins, the group's state, and what was called."""

    def __init__(self, tmp: Path) -> None:
        self.tmp, self.home, self.bin, self.tmpdir = tmp, tmp / "home", tmp / "bin", tmp / "t"
        for d in (self.home / ".ssh", self.bin, self.tmpdir):
            d.mkdir(parents=True)
        self.key = self.home / ".ssh" / "zab-deployer"
        listed = new_key(self.key)
        self.fingerprint = listed.split()[1]
        self.agent = (0, listed)  # ssh-add -l's exit code and lines: the key alone
        self.checkip = HERE_ADDR
        self.state_file, self.log = tmp / "aws.json", tmp / "aws.log"
        self.log.touch()
        self.state_file.touch()
        self.state = {
            "groups": {SG: {"name": "zab-admin", "tagged": True, "rules": WEB + OLD_22}},
            "instances": [{"id": "i-0f1e5", "state": "running", "group": FILES_SG}],
        }
        files = [*WEB, rule("sgr-0c1", 22, cidr="203.0.113.9/32")]
        self.state["groups"][FILES_SG] = {
            "name": "zab-central-files",
            "tagged": False,
            "rules": files,
        }
        self.homefile = self.home / ".config" / "zab" / "homeip"
        self.record_home(HOME_ADDR + "/32")
        self.conf = self.home / ".config" / "zab" / "ssh-rule.conf"
        self.conf.write_text(f"profile={PROFILE}\nregion={REGION}\nkey={self.key}\n")
        self.stub("aws", f"#!{sys.executable}\n{AWS}")
        self.stub("curl", '#!/bin/sh\n[ -n "$CURL_FAILS" ] && exit 22\necho "$CHECKIP_SAYS"\n')
        self.stub(
            "ssh-add",
            '#!/bin/sh\n[ "$1" = -l ] || exit 9\nprintf "%s" "$AGENT_SAYS"\nexit "$AGENT_RC"\n',
        )

    def stub(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def record_home(self, text: str | None) -> None:
        if text is None:
            self.homefile.unlink(missing_ok=True)
            return
        self.homefile.parent.mkdir(parents=True, exist_ok=True)
        self.homefile.write_text(text + "\n")

    def run(self, *args: str, curl_fails: bool = False) -> subprocess.CompletedProcess[str]:
        self.state_file.write_text(json.dumps(self.state))
        self.log.write_text("")
        rc, lines = self.agent
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "TMPDIR": str(self.tmpdir),
            "AWS_STATE": str(self.state_file),
            "AWS_LOG": str(self.log),
            "AWS_CONFIG_FILE": str(self.tmp / "no-aws-config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(self.tmp / "no-aws-credentials"),
            "CHECKIP_SAYS": self.checkip,
            "AGENT_RC": str(rc),
            "AGENT_SAYS": lines + ("\n" if lines else ""),
            **({"CURL_FAILS": "1"} if curl_fails else {}),
        }
        bash = shutil.which("bash", path="/usr/bin:/bin") or "bash"
        done = subprocess.run(  # noqa: S603 — the script under test, with stand-ins on PATH
            [bash, str(SCRIPT), *args], capture_output=True, text=True, env=env, cwd=self.tmp,
            check=False,
        )  # fmt: skip
        self.state = json.loads(self.state_file.read_text())
        return done

    def calls(self, command: str | None = None) -> list[list[str]]:
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in calls if command is None or c[5] == command]

    def changes(self) -> list[list[str]]:
        return [c for c in self.calls() if c[5].endswith("-ingress")]

    def ports_22(self, group: str = SG) -> list[dict]:
        return [r for r in self.state["groups"][group]["rules"] if r["from"] == 22]


@pytest.fixture
def laptop(tmp_path: Path) -> Laptop:
    return Laptop(tmp_path)


def said(done: subprocess.CompletedProcess[str]) -> str:
    return done.stdout + done.stderr


# ------------------------------------------------------------------------------- opening


def test_it_admits_this_address_alone_and_reads_the_group_back(laptop):
    done = laptop.run()
    assert done.returncode == 0, said(done)
    out = done.stdout
    assert f"group: {SG}, the admin server's group (profile {PROFILE}, region {REGION})" in out
    assert f"address: {HERE_ADDR}/32 (checkip)" in out
    assert f"key: {laptop.fingerprint}, alone in the agent" in out
    assert "revoke sgr-0b1: True\nrevoke sgr-0b2: True\n" in out  # IPv4 and IPv6 alike
    assert f"authorize tcp 22 from {HERE_ADDR}/32: True\n" in out
    assert re.search(rf"^  tcp\t22\t22\t{HERE_ADDR}/32\t*$", out, re.M), out
    assert out.endswith(f"ok: 443 and 80 from 0.0.0.0/0, 22 from {HERE_ADDR}/32 alone\n")
    assert [(r["cidr"], r["desc"]) for r in laptop.ports_22()] == [(f"{HERE_ADDR}/32", "deployer")]
    assert all(c[:4] == ["--profile", PROFILE, "--region", REGION] for c in laptop.calls())
    revokes = laptop.calls("revoke-security-group-ingress")
    assert [c[c.index("--security-group-rule-ids") + 1] for c in revokes] == ["sgr-0b1", "sgr-0b2"]
    assert all(c[c.index("--group-id") + 1] == SG for c in laptop.changes())
    assert HOME_ADDR not in said(done)  # the home address is never printed
    assert done.stderr == ""


def test_the_flags_override_the_saved_values(laptop):
    done = laptop.run("--profile", "other", "--region", "eu-west-2")
    assert done.returncode == 0, said(done)
    assert all(c[:4] == ["--profile", "other", "--region", "eu-west-2"] for c in laptop.calls())
    assert "(profile other, region eu-west-2)" in done.stdout


@pytest.mark.parametrize(
    ("args", "missing"),
    [([], "--profile --region --key"), (["--close"], "--profile --region"),
     (["--profile", "p"], "--region --key"),
     (["--profile", "p", "--region", "eu-west-2"], "--key")],
)  # fmt: skip
def test_without_a_saved_value_or_a_flag_it_stops_and_says_what_to_run(laptop, args, missing):
    """The review of #743: no built-in profile, region or key, one deployment's."""
    laptop.conf.unlink()
    done = laptop.run(*args)
    assert done.returncode == 1 and laptop.calls() == [], said(done)
    assert f"stopped: no {missing}: give them, or save them once with ssh-rule.sh" in done.stderr
    assert "--save-defaults --profile P --region R --key FILE" in done.stderr


def test_flags_alone_need_no_saved_file(laptop):
    laptop.conf.unlink()
    done = laptop.run("--profile", "p", "--region", "eu-west-2", "--key", str(laptop.key))
    assert done.returncode == 0, said(done)
    assert all(c[:4] == ["--profile", "p", "--region", "eu-west-2"] for c in laptop.calls())


def test_save_defaults_keeps_the_three_values_for_later_runs(laptop):
    laptop.conf.unlink()
    key = laptop.key.relative_to(laptop.tmp)  # relative to the caller's directory: kept whole
    done = laptop.run(
        "--save-defaults", "--profile", "p", "--region", "eu-west-2", "--key", str(key)
    )
    assert done.returncode == 0 and laptop.calls() == [], said(done)
    assert laptop.conf.read_text() == f"profile=p\nregion=eu-west-2\nkey={laptop.key}\n"
    assert stat.S_IMODE(laptop.conf.stat().st_mode) == 0o600
    assert (
        done.stdout == f"saved, in {laptop.conf}: profile p, region eu-west-2, key {laptop.key}\n"
    )
    done = laptop.run()
    assert done.returncode == 0, said(done)
    assert all(c[:4] == ["--profile", "p", "--region", "eu-west-2"] for c in laptop.calls())


def test_save_defaults_refuses_a_key_without_its_pub(laptop):
    done = laptop.run("--save-defaults", "--profile", "p", "--region", "eu-west-2",
                      "--key", str(laptop.home / "missing"))  # fmt: skip
    assert done.returncode == 1 and "stopped: no " in done.stderr
    assert laptop.conf.read_text().startswith(f"profile={PROFILE}\n")  # unchanged


@pytest.mark.parametrize("line", ["profile=a b", "region=us-east-2;x", "region=Mars"])
def test_a_saved_value_that_is_not_one_stops(laptop, line):
    name = line.split("=")[0]
    kept = [x for x in laptop.conf.read_text().splitlines() if not x.startswith(f"{name}=")]
    laptop.conf.write_text("\n".join([line, *kept]) + "\n")
    done = laptop.run()
    assert done.returncode == 1 and laptop.calls() == [], said(done)
    assert f"stopped: {laptop.conf}: {name} is not a" in done.stderr


def test_a_saved_key_may_start_with_a_tilde(laptop):
    rel = laptop.key.relative_to(laptop.home)
    laptop.conf.write_text(f"profile={PROFILE}\nregion={REGION}\nkey=~/{rel}\n")
    done = laptop.run()
    assert done.returncode == 0, said(done)
    done = laptop.run("--save-defaults", "--profile", "p", "--region", REGION, "--key", f"~/{rel}")
    assert done.returncode == 0, said(done)  # a quoted ~/ is saved as the shell would expand it
    assert laptop.conf.read_text() == f"profile=p\nregion={REGION}\nkey={laptop.key}\n"


def test_the_script_names_no_deployment():
    """It goes into the public export: no profile, key or region of one deployment."""
    text = SCRIPT.read_text()
    assert not re.search(r"drew|us-east-2|us-west-2|mathagents", text, re.IGNORECASE)


@pytest.mark.parametrize(
    "args", [["--region", "us-east-2;x"], ["--profile", "a b"], ["--key"], ["--bogus"],
             ["--record-home", "--close"], ["--record-home", "--files-host"],
             ["--record-home", "--profile", "p"], ["--record-home", "--key", "k"],
             ["--record-home", "--home-unknown"], ["--save-defaults", "--files-host"],
             ["--save-defaults", "--dry-run"], ["--save-defaults", "--profile", "p"],
             ["--save-defaults", "--close"], ["--close", "--home-unknown"],
             ["--group-name", "a b"], ["--group-name", "-x"], ["--group-name"],
             ["--group-name", "g", "--files-host"], ["--record-home", "--group-name", "g"],
             ["--save-defaults", "--profile", "p", "--region", "eu-west-2", "--key", "k",
              "--group-name", "g"]],
)  # fmt: skip
def test_a_usage_error_changes_nothing(laptop, args):
    done = laptop.run(*args)
    assert done.returncode == 2 and done.stderr.startswith("ssh-rule.sh: "), said(done)
    assert laptop.calls() == []


@pytest.mark.parametrize("close", [False, True])
def test_another_group_name_is_looked_up_with_the_same_tag(laptop, close):
    """#772: a test server's group, by its own name, carrying the `zab-admin` tag as the
    server's does; a group of that name without the tag is never found."""
    test_sg = "sg-0e2e"
    laptop.state["groups"][test_sg] = {"name": "zab-test-admin-server", "tagged": True,
                                       "rules": [*WEB, *OLD_22]}  # fmt: skip
    laptop.state["groups"]["sg-0bad"] = {"name": "zab-test-admin-server", "tagged": False,
                                         "rules": []}  # fmt: skip
    done = laptop.run("--group-name", "zab-test-admin-server", *(["--close"] if close else []))
    assert done.returncode == 0, said(done)
    found = f"group: {test_sg}, the admin server's group zab-test-admin-server (profile"
    assert found in done.stdout
    lookup = laptop.calls("describe-security-groups")[0]
    assert "Name=group-name,Values=zab-test-admin-server" in lookup
    assert "Name=tag-key,Values=zab-admin" in lookup
    assert len(laptop.ports_22(test_sg)) == (0 if close else 1)
    assert len(laptop.ports_22()) == 2  # zab-admin's group untouched
    laptop.state["groups"].pop(test_sg)
    done = laptop.run("--group-name", "zab-test-admin-server")
    assert done.returncode == 1 and laptop.changes() == [], said(done)
    assert "not one group for the admin server's group zab-test-admin-server: ''" in done.stderr


def test_the_files_host_is_found_through_its_instance(laptop):
    done = laptop.run("--files-host")
    assert done.returncode == 0, said(done)
    assert f"group: {FILES_SG}, the files host's group" in done.stdout
    lookup = laptop.calls("describe-instances")[0]
    assert "Name=tag:Name,Values=zab-central-files" in lookup
    assert "Name=instance-state-name,Values=running" in lookup
    assert [(r["cidr"], r["desc"]) for r in laptop.ports_22(FILES_SG)] == [
        (f"{HERE_ADDR}/32", "admin")
    ]
    assert len(laptop.ports_22()) == 2  # the admin server's group untouched


# ------------------------------------------------------------------------------- refusing


@pytest.mark.parametrize("args", [[], ["--group-name", "zab-test-admin-server"]])
def test_home_is_refused_before_any_aws_call(laptop, args):
    laptop.record_home(HERE_ADDR + "/32")
    done = laptop.run(*args)
    assert done.returncode == 1 and laptop.calls() == []
    assert "stopped: this network is home's, a network an agent machine shares." in done.stderr
    assert HERE_ADDR not in said(done)  # this address is home's: not printed either


@pytest.mark.parametrize("text", [None, "", "home", "198.51.100.1", "300.1.2.3/32", "/32"])
def test_no_home_or_a_malformed_one_stops(laptop, text):
    laptop.record_home(text)
    done = laptop.run()
    assert done.returncode == 1 and laptop.calls() == [], said(done)
    assert done.stderr.startswith("stopped: "), said(done)
    if text is None:
        assert "At home, run: ssh-rule.sh --record-home." in done.stderr
        assert "--home-unknown" in done.stderr


@pytest.mark.parametrize(("days", "warned"), [(29, False), (31, True)])
def test_an_old_home_record_gets_a_warning(laptop, days, warned):
    then = time.time() - days * 86400
    os.utime(laptop.homefile, (then, then))
    done = laptop.run()
    assert done.returncode == 0, said(done)
    assert ("recorded over 30 days ago" in done.stderr) is warned
    assert HOME_ADDR not in said(done)


@pytest.mark.parametrize("how", ["file says none", "--home-unknown"])
def test_no_home_to_check_warns_and_goes_on(laptop, how):
    laptop.record_home("none" if how == "file says none" else None)
    done = laptop.run(*(["--home-unknown"] if how == "--home-unknown" else []))
    assert done.returncode == 0, said(done)
    assert "warning: " in done.stderr and "no home address checked" in done.stderr
    assert laptop.ports_22()[0]["cidr"] == f"{HERE_ADDR}/32"


@pytest.mark.parametrize(
    "answer",
    ["", "<html>", "192.0.2", "192.0.2.256", "192.0.2.7 192.0.2.8", "192.0.2.7\n8", "192.0.2.7 8",
     "192.0.2.7\t8", "192.0.2.07", "01.2.3.4", "0.0.0.0", "192.0.2.7/32", "-1.2.3.4"],  # noqa: S104
)  # fmt: skip
def test_checkip_must_answer_one_address(laptop, answer):
    """The review of #743: blanks inside the answer refuse it, never join its parts (an answer
    of `192.0.2.7`, a line break and `8` read as 192.0.2.78); no leading zeros, no 0.0.0.0."""
    laptop.checkip = answer
    done = laptop.run()
    assert done.returncode == 1 and laptop.calls() == [], said(done)
    assert "stopped: checkip's answer is not one address" in done.stderr
    assert "192.0.2.78" not in said(done)


@pytest.mark.parametrize("answer", [" 192.0.2.7", "192.0.2.7 ", "192.0.2.7\r", "\t192.0.2.7\r"])
def test_checkip_s_line_end_and_outer_blanks_are_dropped(laptop, answer):
    laptop.checkip = answer
    done = laptop.run()
    assert done.returncode == 0, said(done)
    assert laptop.ports_22()[0]["cidr"] == f"{HERE_ADDR}/32"


def test_checkip_failing_stops(laptop):
    done = laptop.run(curl_fails=True)
    assert done.returncode == 1 and laptop.calls() == []
    assert "stopped: checkip (https://checkip.amazonaws.com) didn't answer" in done.stderr


@pytest.mark.parametrize(
    ("agent", "says"),
    [
        ((2, ""), 'eval "$(ssh-agent -s)"\n'),  # no agent in this terminal
        ((1, "The agent has no identities."), "the agent doesn't hold"),
        ((0, "256 SHA256:someoneelse other (ED25519)"), "the agent doesn't hold"),
    ],
)
def test_the_key_must_be_in_the_agent_and_the_command_to_load_it_is_printed(laptop, agent, says):
    laptop.agent = agent
    done = laptop.run()
    assert done.returncode == 1 and laptop.calls() == [], said(done)
    assert says in done.stderr
    assert f"  ssh-add -t 8h {laptop.key}\n" in done.stderr  # pasteable, as printed


def test_the_key_must_be_alone_in_the_agent(laptop):
    laptop.agent = (0, f"{laptop.agent[1]}\n256 SHA256:personal me@laptop (ED25519)")
    done = laptop.run()
    assert done.returncode == 1 and laptop.calls() == [], said(done)
    assert "the agent holds other keys besides" in done.stderr
    assert 'eval "$(ssh-agent -s)"' in done.stderr


def test_another_key_file_is_an_option(laptop):
    other = laptop.home / ".ssh" / "zab-other"
    listed = new_key(other)
    assert laptop.run("--key", str(other)).returncode == 1  # the agent holds the other key
    laptop.agent = (0, listed)
    assert laptop.run("--key", str(other)).returncode == 0
    assert laptop.run("--key", str(laptop.home / "missing")).returncode == 1


@pytest.mark.parametrize("found", ["", "None", f"{SG}\tsg-0def", "sg-0abc; true"])
def test_one_group_or_nothing_changes(laptop, found):
    group = laptop.state["groups"].pop(SG)
    if found == f"{SG}\tsg-0def":
        laptop.state["groups"][SG] = group
        laptop.state["groups"]["sg-0def"] = group
    elif found:
        laptop.stub("aws", f'#!/bin/sh\necho "$*" >> "$AWS_LOG"\necho \'{found}\'\n')
    done = laptop.run()
    assert done.returncode == 1, said(done)
    assert "stopped: not one group for the admin server's group" in done.stderr
    calls = laptop.log.read_text().splitlines()
    assert not [line for line in calls if "ingress" in line or "rules" in line], calls


def test_not_one_running_files_host_changes_nothing(laptop):
    laptop.state["instances"].append({"id": "i-0f1e6", "state": "running", "group": FILES_SG})
    done = laptop.run("--files-host")
    assert done.returncode == 1 and laptop.changes() == [], said(done)
    assert "stopped: not one running instance named zab-central-files" in done.stderr


def test_an_aws_error_stops_with_its_message(laptop):
    laptop.state["fail"] = "describe-security-group-rules"
    done = laptop.run()
    assert done.returncode == 1 and laptop.changes() == []
    assert "stopped: aws ec2 describe-security-group-rules failed: An error occurred" in done.stderr


def test_a_revoke_aws_refuses_stops_before_the_authorize(laptop):
    laptop.state["revoke_says"] = "False"
    done = laptop.run()
    assert done.returncode == 1 and "stopped: AWS didn't revoke sgr-0b1" in done.stderr
    assert "port 22 may admit nothing now: run this again" in done.stderr
    assert laptop.calls("authorize-security-group-ingress") == []


@pytest.mark.parametrize("ids", ["sgr-0b1\tjunk", "sgr-0b1\tsgr-0b2;x", "None"])
def test_every_rule_id_is_checked_before_the_first_revoke(laptop, ids):
    laptop.state["ids_say"] = ids
    done = laptop.run()
    assert done.returncode == 1 and "stopped: not a rule ID" in done.stderr, said(done)
    assert laptop.changes() == []


def test_an_authorize_that_fails_after_the_revokes_says_22_is_closed(laptop):
    laptop.state["fail"] = "authorize-security-group-ingress"
    done = laptop.run()
    assert done.returncode == 1, said(done)
    assert "revoke sgr-0b1: True\nrevoke sgr-0b2: True\n" in done.stdout
    assert "stopped: aws ec2 authorize-security-group-ingress failed: An error" in done.stderr
    assert "stopped: port 22 may admit nothing now: run this again" in done.stderr
    assert laptop.ports_22() == []
    del laptop.state["fail"]
    done = laptop.run()  # and running it again opens it
    assert done.returncode == 0 and "revoke" not in done.stdout.replace("would", ""), said(done)
    assert laptop.ports_22()[0]["cidr"] == f"{HERE_ADDR}/32"


@pytest.mark.parametrize(
    "extra",
    [rule("sgr-0d1", 8080, cidr="0.0.0.0/0"), rule("sgr-0d2", -1, cidr="0.0.0.0/0"),
     rule("sgr-0d3", 443, group="sg-0aaa"), rule("sgr-0d4", 80, cidr6="::/0")],
)  # fmt: skip
def test_any_other_rule_left_is_a_mismatch(laptop, extra):
    laptop.state["groups"][SG]["rules"].append(extra)
    done = laptop.run()
    assert done.returncode == 1, said(done)
    assert f"authorize tcp 22 from {HERE_ADDR}/32: True" in done.stdout  # done, then read back
    assert "stopped: the group doesn't hold just 443 and 80 from 0.0.0.0/0, 22 from" in done.stderr
    assert "remove the others by hand before you connect" in done.stderr


# ------------------------------------------------------------------- dry run, close, record


def test_a_dry_run_changes_nothing_and_says_what_the_run_would_leave(laptop):
    done = laptop.run("--dry-run")
    assert done.returncode == 0, said(done)
    assert laptop.changes() == [] and len(laptop.ports_22()) == 2
    out = done.stdout
    assert out.startswith("DRY RUN: nothing changes\n")
    assert "would revoke sgr-0b1\nwould revoke sgr-0b2\n" in out
    assert f"would authorize tcp 22 from {HERE_ADDR}/32\n" in out
    leaves = f"443 and 80 from 0.0.0.0/0, 22 from {HERE_ADDR}/32 alone"
    assert out.endswith(f"ok: the run would leave {leaves}\n")
    laptop.state["groups"][SG]["rules"].append(rule("sgr-0d1", 8080, cidr="0.0.0.0/0"))
    done = laptop.run("--dry-run")
    assert done.returncode == 1 and laptop.changes() == []
    assert "stopped: after the run the group would not hold just" in done.stderr


def test_a_dry_run_leaves_out_every_rule_the_run_revokes(laptop):
    """Codex round 2 of #743: RULES takes port 22 alone whatever the protocol, so the dry run
    leaves out a UDP 22 rule too, as the run revokes it."""
    laptop.state["groups"][SG]["rules"].append(rule("sgr-0e1", 22, "udp", cidr="0.0.0.0/0"))
    done = laptop.run("--dry-run")
    assert done.returncode == 0, said(done)
    assert "would revoke sgr-0e1\n" in done.stdout and laptop.changes() == []
    done = laptop.run()
    assert done.returncode == 0, said(done)
    assert "revoke sgr-0e1: True\n" in done.stdout
    assert [r["proto"] for r in laptop.ports_22()] == ["tcp"]


def test_a_dry_run_refuses_as_the_run_does(laptop):
    laptop.record_home(HERE_ADDR + "/32")
    done = laptop.run("--dry-run")
    assert done.returncode == 1 and laptop.calls() == []


@pytest.mark.parametrize("files", [False, True])
def test_close_revokes_every_22_rule_from_anywhere(laptop, files):
    laptop.record_home(None)  # needs no home address,
    laptop.agent = (2, "")  # nor the key,
    laptop.checkip = "home"  # nor checkip
    done = laptop.run("--close", *(["--files-host"] if files else []))
    assert done.returncode == 0, said(done)
    group = FILES_SG if files else SG
    assert laptop.ports_22(group) == [] and laptop.calls("authorize-security-group-ingress") == []
    assert done.stdout.endswith("ok: 443 and 80 from 0.0.0.0/0\n")
    assert len(laptop.ports_22(SG if files else FILES_SG)) == (2 if files else 1)


def test_record_home_writes_the_one_file_and_nothing_else(laptop):
    shutil.rmtree(laptop.home / ".config")  # nothing saved either: --record-home needs nothing
    before = sorted(p.relative_to(laptop.tmp) for p in laptop.tmp.rglob("*"))
    done = laptop.run("--record-home", "--dry-run")
    assert done.returncode == 0 and not laptop.homefile.exists() and laptop.calls() == []
    done = laptop.run("--record-home")
    assert done.returncode == 0, said(done)
    assert laptop.homefile.read_text() == f"{HERE_ADDR}/32\n"
    assert stat.S_IMODE(laptop.homefile.stat().st_mode) == 0o600
    assert stat.S_IMODE(laptop.homefile.parent.stat().st_mode) == 0o700
    assert HERE_ADDR not in said(done) and laptop.calls() == []
    after = sorted(p.relative_to(laptop.tmp) for p in laptop.tmp.rglob("*"))
    new = {Path("home/.config"), Path("home/.config/zab"), Path("home/.config/zab/homeip")}
    assert set(after) - set(before) == new
    done = laptop.run("--profile", PROFILE, "--region", REGION, "--key", str(laptop.key))
    assert done.returncode == 1 and "this network is home's" in done.stderr  # from home: refused


def test_a_run_leaves_no_file_behind(laptop):
    before = sorted(p.relative_to(laptop.tmp) for p in laptop.tmp.rglob("*"))
    assert laptop.run().returncode == 0
    assert sorted(p.relative_to(laptop.tmp) for p in laptop.tmp.rglob("*")) == before
    assert list(laptop.tmpdir.iterdir()) == []


# --------------------------------------------------------------------------- the runbooks


def test_its_queries_are_the_runbook_blocks_own():
    """The fallback blocks stay the same commands: BOOTSTRAP §3.1's refresh and the files host's
    Open and Close select the same rules, and read the same columns back."""
    text = SCRIPT.read_text()
    rules = re.search(r"^RULES='(.*)'$", text, re.M)[1]
    readback = re.search(r"^READBACK='(.*)'$", text, re.M)[1]
    for doc in (BOOTSTRAP, FILES_README):
        assert f"--query '{rules}' --output text" in doc
        assert f"--query '{readback}' --output text" in doc


def test_the_script_is_executable_and_its_usage_is_its_header():
    assert os.access(SCRIPT, os.X_OK)
    done = subprocess.run(  # noqa: S603
        [str(SCRIPT), "--help"], capture_output=True, text=True, check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
    )  # fmt: skip
    assert done.returncode == 0 and done.stdout.startswith(
        "  ssh-rule.sh --save-defaults --profile P"
    )
    assert "--home-unknown" in done.stdout and "#" not in done.stdout and done.stderr == ""


def test_every_command_in_the_docs_uses_options_it_accepts():
    """BOOTSTRAP §3.1, the files host's README, ADMIN §1.1, and the newest release's section of
    CHANGELOG and, where the tree has them, of the upgrade runbooks (#773: a private file, which
    the public tree leaves out); a release's older sections keep the options of their day."""
    text = SCRIPT.read_text()
    cases = re.findall(r"^  (-[-a-z |]+)\)", text, re.M)
    accepted = {option for alternatives in cases for option in alternatives.split(" | ")}
    used = {"--files-host", "--close", "--dry-run", "--record-home", "--key", "--group-name"}
    assert used <= accepted
    newest = [(ROOT / "CHANGELOG.md").read_text().split("\n## ", 2)[1]]
    runbooks = ROOT / "docs" / "runbooks" / "upgrades.md"
    if runbooks.exists():
        newest.append(runbooks.read_text().split("\n## ", 2)[1])
    admin = (ROOT / "docs" / "ADMIN.md").read_text()
    found = 0
    for doc in (BOOTSTRAP, FILES_README, admin, *newest):
        for line in re.findall(r"ssh-rule\.sh([^\n`]*)", doc):
            options = re.findall(r"(?<!\S)(--?[a-z][-a-z]*)", line)
            assert set(options) <= accepted, line
            found += bool(options)
    assert found >= 4
